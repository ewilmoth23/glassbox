"""
forecaster.py — 48-hour hotspot predictions from anomaly time-series.

Input: the last 72 hours of `cognition_decision` / `intel_cycle` events in
the Brain, plus the `glassbox/anomaly` records they produced.

Output: ranked list of regions × layers most likely to see escalation in
the next 48 hours, with confidence + reasoning.

Method (Phase 5b, 2026-05-10 — SARIMAX + recency-weighted fallback):
    1. Bucket historical anomaly events by (layer, region, hour).
    2. Per bucket, assemble an hourly severity time-series spanning
       the lookback window (NaN-filled at empty hours, zero-imputed
       for fitting).
    3. Fit statsmodels SARIMAX(order=(1,1,1), seasonal_order=(1,0,1,24))
       and predict the next 48 hours.
    4. Score = sum of the forecasted-severity series clipped at >=0.
       This rewards buckets with sustained predicted activity (the
       SARIMAX forecast naturally bakes in trend + 24h diurnal seasonality).
    5. If SARIMAX fits fail (insufficient history, singular matrix,
       constant series), fall back to the previous recency-weighted
       exponential-decay score per bucket so the API never starves
       on a freshly-installed system.
    6. For each top bucket, compose a natural-language prediction via
       Ollama grounded in the evidence trail. The Ollama narrative
       layer is unchanged — the math is what we upgraded.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from llm_json import parse_with_schema


class _ForecastSchema(BaseModel):
    """Pydantic schema for the LLM forecast JSON. Bounds the existing
    tolerant-parse behavior with type + range validation; on failure
    callers fall back to the same neutral defaults the prior bracket-
    extraction path used."""
    forecast: str = Field(default="", max_length=600)
    escalation_likelihood: float = Field(default=0.0, ge=0.0, le=1.0)
    category: str = Field(default="other", max_length=16)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "20_HOLDING_BRAIN" / "memory") not in sys.path:
    sys.path.insert(0, str(_ROOT / "20_HOLDING_BRAIN" / "memory"))

try:
    from brain import Brain  # type: ignore
    _BRAIN_OK = True
except Exception:
    _BRAIN_OK = False

log = logging.getLogger("forecaster")


# ─── Historical anomaly ingestion ──────────────────────────────────────────

def _load_recent_anomalies(hours_back: int = 72) -> List[Dict[str, Any]]:
    """Read persisted anomaly records from the Brain."""
    if not _BRAIN_OK:
        return []
    try:
        brain = Brain()
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
        con = sqlite3.connect(str(brain.db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT subject, object, created_at FROM facts "
            "WHERE namespace='glassbox' AND predicate='anomaly' "
            "AND created_at >= ? ORDER BY created_at ASC",
            (since_iso,),
        ).fetchall()
        con.close()
    except Exception as e:
        log.info(f"anomaly load failed: {e}")
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            d = json.loads(r["object"])
            d["_logged_at"] = r["created_at"]
            out.append(d)
        except Exception:
            continue
    return out


# ─── Scoring ───────────────────────────────────────────────────────────────

def _age_hours(iso: str, now: Optional[datetime] = None) -> float:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except Exception:
        return 9999.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - t).total_seconds() / 3600.0)


def _bucket_anomalies(
    anomalies: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Tuple[Dict[Tuple[str, str], List[Tuple[datetime, int]]],
           Dict[Tuple[str, str], List[Dict[str, Any]]],
           Dict[Tuple[str, str], int]]:
    """Group anomalies by (layer, region). Returns (samples, trails, max_severity)."""
    now = now or datetime.now(timezone.utc)
    samples: Dict[Tuple[str, str], List[Tuple[datetime, int]]] = defaultdict(list)
    trails: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    max_sev: Dict[Tuple[str, str], int] = defaultdict(int)
    for a in anomalies:
        layer = a.get("layer") or "unknown"
        region = a.get("region") or "other"
        sev = int(a.get("anomaly_severity") or 0)
        logged = a.get("_logged_at") or a.get("detected_at") or now.isoformat()
        try:
            t = datetime.fromisoformat(logged.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except Exception:
            t = now
        key = (layer, region)
        samples[key].append((t, sev))
        max_sev[key] = max(max_sev[key], sev)
        if len(trails[key]) < 6:
            age = max(0.0, (now - t).total_seconds() / 3600.0)
            trails[key].append({
                "age_hours": round(age, 2),
                "severity": sev,
                "z_score": a.get("z_score"),
                "direction": a.get("direction"),
                "logged_at": logged,
                "sample": (a.get("sample") or [{}])[0],
            })
    return samples, trails, max_sev


def _hourly_severity_series(
    samples: List[Tuple[datetime, int]],
    history_window_h: int,
    now: datetime,
) -> List[float]:
    """Bucket samples into one cell per hour of history_window. Returns oldest→newest."""
    buckets = [0.0] * history_window_h
    for t, sev in samples:
        age = (now - t).total_seconds() / 3600.0
        if age < 0 or age >= history_window_h:
            continue
        idx = history_window_h - 1 - int(age)
        if 0 <= idx < history_window_h:
            buckets[idx] += float(sev)
    return buckets


def _sarimax_forecast(
    series: List[float],
    horizon_h: int = 48,
    *,
    order: Tuple[int, int, int] = (1, 1, 1),
    seasonal_order: Tuple[int, int, int, int] = (1, 0, 1, 24),
) -> Optional[List[float]]:
    """
    Fit a SARIMAX model on the hourly series and forecast the next horizon_h.
    Returns the forecast list (length horizon_h) or None if the fit fails or
    the series is too short / degenerate.
    """
    # Need at least 2 full seasonal cycles + a few extra points for the model
    # to estimate trend + season + AR + MA. The seasonal period in the
    # default config is 24, so 48 is the practical minimum.
    seasonal_period = seasonal_order[3] if seasonal_order and len(seasonal_order) == 4 else 0
    min_required = max(2 * seasonal_period, 12) if seasonal_period else 12
    if len(series) < min_required:
        return None
    # Constant series (or all-zero history) — SARIMAX will either error or
    # produce useless forecasts. Bail out so the caller can use the fallback.
    if max(series) - min(series) <= 1e-9:
        return None

    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX  # noqa: E402
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = SARIMAX(
                series,
                order=order,
                seasonal_order=seasonal_order,
                enforce_stationarity=False,
                enforce_invertibility=False,
            )
            fit = model.fit(disp=False, maxiter=50)
            fc = fit.forecast(steps=horizon_h)
        out = [float(max(0.0, x)) for x in list(fc)]
        return out
    except Exception as e:
        log.info(f"SARIMAX fit failed: {e}")
        return None


def _recency_weighted_score(
    samples: List[Tuple[datetime, int]],
    now: datetime,
    *,
    half_life_h: float = 12.0,
    recent_weight_boost: float = 1.5,
    recent_window_h: float = 6.0,
) -> float:
    """Original Phase D.1 recency-weighted score; preserved as the SARIMAX fallback."""
    score = 0.0
    for t, sev in samples:
        age = max(0.0, (now - t).total_seconds() / 3600.0)
        weight = math.exp(-age / max(1.0, half_life_h))
        if age <= recent_window_h:
            weight *= recent_weight_boost
        score += sev * weight
    return score


def score_hotspots(
    anomalies: List[Dict[str, Any]],
    *,
    history_window_h: int = 72,
    horizon_h: int = 48,
    half_life_h: float = 12.0,
    recent_weight_boost: float = 1.5,
    recent_window_h: float = 6.0,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Rank (layer, region) buckets by SARIMAX-forecasted future severity.

    For each bucket, builds an hourly severity history over `history_window_h`
    hours, fits a SARIMAX(order=(1,1,1), seasonal_order=(1,0,1,24)) model,
    and forecasts the next `horizon_h` hours. The score is the sum of the
    forecasted series — interpretable as "expected severity over the
    forecast window."

    For buckets without enough history (or where SARIMAX fits fail —
    constant series, singular matrix, etc.) the legacy recency-weighted
    score is used as the deterministic fallback. Each result records
    `method` ('sarimax' or 'recency_weighted_fallback') so consumers can
    audit which path produced the score.

    Returns list sorted descending by score.
    """
    now = now or datetime.now(timezone.utc)
    samples, trails, max_sev = _bucket_anomalies(anomalies, now=now)

    out: List[Dict[str, Any]] = []
    for (layer, region), bucket_samples in samples.items():
        series = _hourly_severity_series(bucket_samples, history_window_h, now)
        forecast = _sarimax_forecast(series, horizon_h=horizon_h)
        if forecast is not None:
            score = float(sum(forecast))
            method = "sarimax"
            forecast_summary: Dict[str, Any] = {
                "horizon_h": horizon_h,
                "history_h": history_window_h,
                "forecast_max": round(max(forecast), 3),
                "forecast_mean": round(sum(forecast) / len(forecast), 3),
            }
        else:
            score = _recency_weighted_score(
                bucket_samples, now,
                half_life_h=half_life_h,
                recent_weight_boost=recent_weight_boost,
                recent_window_h=recent_window_h,
            )
            method = "recency_weighted_fallback"
            forecast_summary = {
                "horizon_h": horizon_h,
                "history_h": history_window_h,
                "fallback_reason": "insufficient_history_or_degenerate_series",
            }

        out.append({
            "layer": layer,
            "region": region,
            "score": round(score, 3),
            "method": method,
            "forecast": forecast_summary,
            "max_historical_severity": max_sev[(layer, region)],
            "evidence_count": len(trails[(layer, region)]),
            "evidence_trail": trails[(layer, region)],
        })
    out.sort(key=lambda b: b["score"], reverse=True)
    return out


# ─── AI narrative per top hotspot ──────────────────────────────────────────

async def narrate_hotspot(hotspot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Produce a natural-language 48-hour forecast for one hotspot via Ollama.
    Runs through cognition.process_content so every forecast gets
    recall-injected + self-critiqued + logged to the Brain.
    """
    try:
        sys.path.insert(0, str(_ROOT / "20_HOLDING_BRAIN"))
        from cognition import process_content  # type: ignore
    except Exception:
        process_content = None

    prompt = json.dumps({
        "task": (
            "You are the Glassbox 48-hour hotspot forecaster. Given the "
            "layer, region, recent anomaly evidence, and decay-weighted "
            "score, produce a 2-3 sentence forecast of what's likely to "
            "develop in the next 48 hours. Be specific, cite concrete "
            "regions/mechanics, avoid vague hedging. If the evidence is "
            "too sparse to forecast, say so and set confidence 0.2-0.35."
        ),
        "return_format": (
            "JSON only: {\"forecast\":\"<2-3 sentences>\","
            "\"escalation_likelihood\":0.0-1.0,"
            "\"category\":\"natural|conflict|social|economic|infra|other\","
            "\"confidence\":0.0-1.0}"
        ),
        "hotspot": {
            "layer": hotspot["layer"],
            "region": hotspot["region"],
            "recency_weighted_score": hotspot["score"],
            "max_historical_severity": hotspot["max_historical_severity"],
            "evidence_count": hotspot["evidence_count"],
            "evidence_trail": hotspot["evidence_trail"],
        },
    }, separators=(",", ":"))

    async def _gen(enhanced_prompt: str) -> str:
        # Routes through llm_ollama which picks /api/generate (default,
        # legacy) or /v1/chat/completions (set GLASSBOX_OLLAMA_USE_CHAT_API=1).
        # Feature-flag intentionally defaults to legacy so production
        # behavior is unchanged until the operator explicitly opts in.
        from llm_ollama import generate_json
        return await generate_json(
            system=(
                "You are the Glassbox 48-hour forecaster. Produce a short, "
                "specific, evidence-grounded prediction. Return JSON only."
            ),
            prompt=enhanced_prompt,
            task="forecast",
            temperature=0.25,
            num_ctx=4096,
            max_tokens=400,
            timeout_total=90,
        )

    if process_content:
        try:
            result = await process_content(
                content_type="glassbox_intel",
                prompt=prompt,
                generator_fn=_gen,
                recall_k=3,
                recall_namespaces=["glassbox", "claude_meta"],
                recall_extra_terms=f"{hotspot['layer']} {hotspot['region']} forecast anomaly",
                context={"forecast_target": f"{hotspot['layer']}/{hotspot['region']}"},
            )
            parsed = _parse_forecast(result.get("output") or "")
            parsed["_cognition_action"] = result.get("action")
            parsed["_cognition_confidence"] = (result.get("critique") or {}).get("confidence")
            return parsed
        except Exception as e:
            log.info(f"cognition forecast failed ({hotspot['layer']}/{hotspot['region']}): {e}")

    # Fallback: direct Ollama
    try:
        raw = await _gen(prompt)
        return _parse_forecast(raw)
    except Exception as e:
        return {
            "forecast": f"Forecast generation unavailable: {e}",
            "escalation_likelihood": 0.0,
            "category": "other",
            "confidence": 0.0,
        }


def _parse_forecast(raw: str) -> Dict[str, Any]:
    """Schema-bound parse via llm_json.parse_with_schema. Logs the parse
    error category but never raises — callers depend on always getting
    a dict with the four expected keys."""
    parsed, err = parse_with_schema(
        raw, _ForecastSchema, fallback=_ForecastSchema(),
    )
    if err is not None:
        log.debug("forecaster: parse_with_schema -> %s", err)
    instance = parsed or _ForecastSchema()
    out = instance.model_dump()
    out["category"] = (out.get("category") or "other").lower()[:16]
    return out


# ─── Orchestrator ──────────────────────────────────────────────────────────

async def forecast_next_48h(top_k: int = 5, hours_back: int = 72) -> Dict[str, Any]:
    """
    End-to-end forecast: load history, score hotspots, narrate the top K.
    Returns a single object safe to persist or push to the front-end.
    """
    anomalies = _load_recent_anomalies(hours_back=hours_back)
    if not anomalies:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 48,
            "hotspots": [],
            "message": "Not enough anomaly history yet — forecast will populate after ~12 hours of intel-loop cycles.",
        }
    scored = score_hotspots(anomalies)
    top = scored[:top_k]
    forecasts: List[Dict[str, Any]] = []
    for hs in top:
        narrative = await narrate_hotspot(hs)
        forecasts.append({**hs, **narrative})

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": 48,
        "history_window_hours": hours_back,
        "anomalies_analyzed": len(anomalies),
        "hotspots": forecasts,
    }

    # Persist to Brain
    if _BRAIN_OK and forecasts:
        try:
            brain = Brain()
            for f in forecasts:
                brain.remember(
                    namespace="glassbox",
                    predicate="forecast",
                    subject=f"forecast:{f['layer']}:{f['region']}:{report['generated_at'][:13]}",
                    object=json.dumps({
                        "layer": f["layer"], "region": f["region"],
                        "score": f["score"],
                        "escalation_likelihood": f.get("escalation_likelihood"),
                        "forecast": f.get("forecast"),
                        "confidence": f.get("confidence"),
                    })[:3800],
                    source="forecaster.py",
                    tags=f"forecast,48h,{f['layer']},{f['region']}",
                )
            brain.log_event(
                namespace="glassbox", kind="forecast_cycle",
                summary=f"48h forecast: top {len(forecasts)} hotspots",
                detail={"anomalies": len(anomalies), "top": [
                    (f["layer"], f["region"], f["score"], f.get("escalation_likelihood"))
                    for f in forecasts
                ]},
                severity="info", source="forecaster.py",
            )
        except Exception as e:
            log.warning(f"brain persist failed: {e}")

    return report
