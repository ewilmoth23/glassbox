"""Health + metrics — `/health/*` + `/metrics*` + `/system-state`
(extraction #3 of P3-H Phase 2).

Five routes plus the three module-level helpers they share:

  GET /health/db         — DB ping (SELECT 1)
  GET /metrics           — Prometheus exposition of ingester health
  GET /metrics/prefilter — Prometheus exposition of GDELT prefilter stats
  GET /health/full       — rich JSON aggregate health snapshot
  GET /system-state      — single-poll ops snapshot (DB + sanctions + entities)

Helpers (also exposed at api_v1.* for backward-compat with 2 test files
that import them directly — `test_health_full.py` imports
`build_health_snapshot`, `test_metrics_endpoint.py` imports all three):

  build_health_snapshot(ing_list, recent_cycle, *, last_splink_run, sla_multiplier)
      — assembles the /health/full payload from ingester status() reads
        and DB heartbeat queries. Pure-async; takes deps as args so tests
        can drive it without HTTP plumbing.

  _render_prometheus(snap) -> str
      — renders a snapshot dict into Prometheus text exposition format
        (line-based, HELP+TYPE comments per family).

  _esc_label(value) -> str
      — escapes a label value per Prometheus spec (backslash, double-quote,
        newline).

`_TIER1_EVENT_TYPES_FOR_POLL` is read inside `/system-state` via a
deferred `from api_v1 import _TIER1_EVENT_TYPES_FOR_POLL` — it lives in
api_v1.py because the alerts cluster (not yet extracted) also reads it.
Will lift to a shared constants module when alerts extracts.

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.
"""

from __future__ import annotations

import time as time_mod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from db import (
    fetch_read,
    fetchval_read,
    pool_stats as _pool_stats,
)


# ─── /health/full helper (extracted for testability) ──────────────────────


async def build_health_snapshot(ing_list, recent_cycle, *,
                                 last_splink_run: Optional[Dict[str, Any]] = None,
                                 sla_multiplier: float = 3.0) -> Dict[str, Any]:
    """Assemble the /health/full payload.

    Returns the dict directly; the HTTP wrapper just JSON-encodes it.
    Pure-async + caller-supplies-deps shape lets tests drive it without
    spinning up the FastAPI HTTP layer.

    `last_splink_run` is optional so the helper degrades cleanly when the
    Splink loop hasn't fired yet (or is disabled). Passed through into
    `algorithms.last_splink_run` so monitors can confirm the ER index
    is being kept fresh.

    Phase 6 (2026-05-09) - per-ingester SLA grading. Each ingester reports
    `poll_interval_sec` (its expected fetch cadence). If `last_fetch_ts`
    is older than `sla_multiplier * poll_interval_sec` (default 3x), the
    item's `sla_breach` field is True and the snapshot's aggregate
    `status` falls to 'degraded'. This catches silent stalls (the
    ingester didn't crash + last_error is None but no successful fetch
    in 30 minutes for a 5-minute-cadence ingester).
    """
    # 1. DB
    db_ok = False
    db_err = None
    db_started_ms = time_mod.time()
    try:
        v = await fetchval_read("SELECT 1")
        db_ok = (v == 1)
    except Exception as e:  # noqa: BLE001
        db_err = f"{type(e).__name__}: {e}"
    db_latency_ms = int((time_mod.time() - db_started_ms) * 1000)

    # 2. Pool
    pool = _pool_stats()

    # 3. Ingesters
    ingesters = []
    ok_count = 0
    degraded_count = 0
    down_count = 0
    sla_breach_count = 0
    now_dt = datetime.now(timezone.utc)
    for ing in ing_list:
        try:
            st = ing.status() or {}
        except Exception as e:  # noqa: BLE001
            st = {"layer": getattr(ing, "layer", "?"),
                  "health": "down", "last_error": str(e)}
        health = (st.get("health") or "ok").lower()

        # Per-ingester SLA grading. An ingester is "in breach" when its
        # last successful fetch is older than sla_multiplier * its own
        # advertised poll_interval_sec. We grade against last_fetch_ts
        # (not last_emit) so an ingester whose source is genuinely quiet
        # (e.g. NHC during off-season) still passes as long as it's
        # successfully calling out.
        secs_since_last_fetch: Optional[int] = None
        sla_breach = False
        last_fetch = st.get("last_fetch_ts")
        poll = st.get("poll_interval_sec")
        # Per-ingester override (set on streaming / websocket-style
        # ingesters where the default formula's "90 s since last
        # fetch == breach" would always fire because cycle() takes
        # minutes by design). None / non-positive => use the formula.
        sla_override = st.get("sla_breach_threshold_sec")
        if last_fetch and isinstance(poll, (int, float)) and poll > 0:
            try:
                last_dt = datetime.fromisoformat(
                    last_fetch.replace("Z", "+00:00")
                )
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                secs_since_last_fetch = int((now_dt - last_dt).total_seconds())
                if isinstance(sla_override, (int, float)) and sla_override > 0:
                    sla_threshold_sec = float(sla_override)
                else:
                    sla_threshold_sec = max(60.0, sla_multiplier * float(poll))
                if secs_since_last_fetch > sla_threshold_sec:
                    sla_breach = True
            except (ValueError, TypeError):
                pass
        elif poll and not last_fetch:
            # Ingester registered but never fetched once. For
            # stream-style ingesters whose first batch genuinely
            # takes minutes (AISStream's 5-min websocket cycle,
            # Bluesky Jetstream's 5-min listen window), don't flag
            # breach until the override-grace-period has elapsed
            # since registration. Without this, every daemon reload
            # produces ~5 minutes of bogus SLA-breach noise.
            created_at = st.get("created_at")
            if (isinstance(sla_override, (int, float)) and sla_override > 0
                    and created_at):
                try:
                    created_dt = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    secs_since_creation = (now_dt - created_dt).total_seconds()
                    # Grace = the explicit override (e.g. 600s for streams).
                    # After that, no first fetch = real breach.
                    if secs_since_creation > float(sla_override):
                        sla_breach = True
                except (ValueError, TypeError):
                    sla_breach = True
            else:
                # No override configured → poll-based ingester that
                # claims to fetch every <poll>s but never has. Real
                # breach.
                sla_breach = True

        if sla_breach:
            sla_breach_count += 1
            # Promote health to at least degraded; keep 'down' as-is
            if health == "ok":
                health = "degraded"

        if health == "ok":
            ok_count += 1
        elif health == "degraded":
            degraded_count += 1
        else:
            down_count += 1

        ingesters.append({
            "layer":               st.get("layer"),
            "source":              st.get("source"),
            "health":              health,
            "poll_interval_sec":   poll,
            "last_fetch_ts":       last_fetch,
            "secs_since_last_fetch": secs_since_last_fetch,
            "sla_breach":          sla_breach,
            "last_fetch_count":    st.get("last_fetch_count"),
            "last_emit_ts":        st.get("last_emit_ts"),
            "last_cycle_ms":       st.get("last_cycle_ms"),
            "tracked_entities":    st.get("tracked_entities"),
            "cycles_run":          st.get("cycles_run"),
            "cycles_failed":       st.get("cycles_failed"),
            "last_error":          st.get("last_error"),
            "db_write_enabled":    st.get("db_write_enabled"),
            "last_db_write_count": st.get("last_db_write_count"),
            "db_write_failures":   st.get("db_write_failures"),
            "last_db_error":       st.get("last_db_error"),
        })

    # 4. Findings written in the last 5 + 60 minutes — durable-archive
    # heartbeat. Confirms algorithms + writers are actually persisting.
    # Filter on event_time (the TimescaleDB hypertable dimension, indexed
    # DESC) instead of created_at, so the planner can chunk-prune to the
    # most recent chunk only. Filtering on created_at forced a seq scan
    # over every chunk (16GB+ for the largest), which stacked up under
    # repeated /status polling and starved the asyncpg pool until every
    # read hung. event_time ≈ created_at for live ingest; for back-filled
    # historical events the heartbeat correctly measures "fresh real-world
    # events landing" rather than "any writes happening at all".
    findings = {"5m": None, "60m": None, "err": None}
    try:
        row5 = await fetchval_read(
            "SELECT count(*) FROM event "
            "WHERE event_time >= NOW() - INTERVAL '5 minutes'"
        )
        row60 = await fetchval_read(
            "SELECT count(*) FROM event "
            "WHERE event_time >= NOW() - INTERVAL '60 minutes'"
        )
        findings["5m"] = int(row5) if row5 is not None else 0
        findings["60m"] = int(row60) if row60 is not None else 0
    except Exception as e:  # noqa: BLE001
        findings["err"] = f"{type(e).__name__}: {e}"

    # 5. Aggregate top-level status. Empty ingester list cannot grade
    # ingester health so it doesn't degrade.
    if not db_ok:
        top_status = "down"
    elif ingesters and down_count > 0 and ok_count == 0:
        top_status = "down"
    elif degraded_count > 0 or down_count > 0:
        top_status = "degraded"
    else:
        top_status = "ok"

    return {
        "status":     top_status,
        "ts":         datetime.now(timezone.utc).isoformat(),
        "db": {
            "ok":          db_ok,
            "latency_ms":  db_latency_ms,
            "error":       db_err,
        },
        "pool":       pool,
        "ingesters": {
            "total":      len(ingesters),
            "ok":         ok_count,
            "degraded":   degraded_count,
            "down":       down_count,
            "sla_breach": sla_breach_count,
            "items":      ingesters,
        },
        "algorithms": {
            "recent_cycle":     recent_cycle,
            "last_splink_run":  last_splink_run,
        },
        "findings": findings,
    }


def _esc_label(value: Any) -> str:
    """Escape a label value for Prometheus exposition format.
    Replaces backslashes, double-quotes, and newlines per spec."""
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_prometheus(snap: Dict[str, Any]) -> str:
    """Convert a build_health_snapshot dict into Prometheus text format.

    Spec: line-based, comments start with '#', metric families have
    HELP + TYPE comments, samples are 'name{label="v"} value timestamp_ms'.
    Body must end with a newline.
    """
    out: List[str] = []
    push = out.append

    # DB
    push("# HELP glassbox_db_up 1 if the SELECT 1 health probe succeeded")
    push("# TYPE glassbox_db_up gauge")
    push(f"glassbox_db_up {1 if snap['db']['ok'] else 0}")

    push("# HELP glassbox_db_query_latency_ms last health-probe latency")
    push("# TYPE glassbox_db_query_latency_ms gauge")
    push(f"glassbox_db_query_latency_ms {int(snap['db']['latency_ms'])}")

    # Pool
    pool = snap.get("pool") or {}
    if pool.get("initialized"):
        push("# HELP glassbox_pool_size current asyncpg pool size")
        push("# TYPE glassbox_pool_size gauge")
        push(f"glassbox_pool_size {int(pool.get('size', 0))}")
        push("# HELP glassbox_pool_in_use connections currently checked out")
        push("# TYPE glassbox_pool_in_use gauge")
        push(f"glassbox_pool_in_use {int(pool.get('in_use', 0))}")

    # Ingesters
    push("# HELP glassbox_ingester_health 1 if ingester health == status")
    push("# TYPE glassbox_ingester_health gauge")
    for ing in snap["ingesters"]["items"]:
        layer = _esc_label(ing.get("layer"))
        h = (ing.get("health") or "ok").lower()
        for status in ("ok", "degraded", "down"):
            push(
                f'glassbox_ingester_health{{layer="{layer}",status="{status}"}} '
                f'{1 if h == status else 0}'
            )

    push("# HELP glassbox_ingester_cycles_total ingester cycles run")
    push("# TYPE glassbox_ingester_cycles_total counter")
    for ing in snap["ingesters"]["items"]:
        push(
            f'glassbox_ingester_cycles_total{{layer="{_esc_label(ing.get("layer"))}"}} '
            f'{int(ing.get("cycles_run") or 0)}'
        )

    push("# HELP glassbox_ingester_cycles_failed_total ingester cycles that raised")
    push("# TYPE glassbox_ingester_cycles_failed_total counter")
    for ing in snap["ingesters"]["items"]:
        push(
            f'glassbox_ingester_cycles_failed_total{{layer="{_esc_label(ing.get("layer"))}"}} '
            f'{int(ing.get("cycles_failed") or 0)}'
        )

    push("# HELP glassbox_ingester_db_write_failures_total dual-write writer failures")
    push("# TYPE glassbox_ingester_db_write_failures_total counter")
    for ing in snap["ingesters"]["items"]:
        push(
            f'glassbox_ingester_db_write_failures_total{{layer="{_esc_label(ing.get("layer"))}"}} '
            f'{int(ing.get("db_write_failures") or 0)}'
        )

    push("# HELP glassbox_ingester_tracked_entities entities currently tracked per layer")
    push("# TYPE glassbox_ingester_tracked_entities gauge")
    for ing in snap["ingesters"]["items"]:
        push(
            f'glassbox_ingester_tracked_entities{{layer="{_esc_label(ing.get("layer"))}"}} '
            f'{int(ing.get("tracked_entities") or 0)}'
        )

    push("# HELP glassbox_ingester_sla_breach 1 if last_fetch_ts older than SLA threshold")
    push("# TYPE glassbox_ingester_sla_breach gauge")
    for ing in snap["ingesters"]["items"]:
        push(
            f'glassbox_ingester_sla_breach{{layer="{_esc_label(ing.get("layer"))}"}} '
            f'{1 if ing.get("sla_breach") else 0}'
        )

    push("# HELP glassbox_ingester_secs_since_last_fetch seconds since last successful fetch")
    push("# TYPE glassbox_ingester_secs_since_last_fetch gauge")
    for ing in snap["ingesters"]["items"]:
        v = ing.get("secs_since_last_fetch")
        if v is not None:
            push(
                f'glassbox_ingester_secs_since_last_fetch{{layer="{_esc_label(ing.get("layer"))}"}} '
                f'{int(v)}'
            )

    # Findings
    f = snap.get("findings") or {}
    if isinstance(f.get("5m"), int):
        push("# HELP glassbox_findings_5m events written to durable archive in last 5 minutes")
        push("# TYPE glassbox_findings_5m gauge")
        push(f'glassbox_findings_5m {f["5m"]}')
    if isinstance(f.get("60m"), int):
        push("# HELP glassbox_findings_60m events written to durable archive in last 60 minutes")
        push("# TYPE glassbox_findings_60m gauge")
        push(f'glassbox_findings_60m {f["60m"]}')

    # Splink loop
    splink = snap.get("algorithms", {}).get("last_splink_run") or {}
    if splink.get("predicted") is not None:
        push("# HELP glassbox_splink_predicted matches predicted by last Splink cycle")
        push("# TYPE glassbox_splink_predicted gauge")
        push(f'glassbox_splink_predicted {int(splink["predicted"])}')
    if splink.get("persisted") is not None:
        push("# HELP glassbox_splink_persisted new entity_relation rows from last Splink cycle")
        push("# TYPE glassbox_splink_persisted gauge")
        push(f'glassbox_splink_persisted {int(splink["persisted"])}')
    if splink.get("duration_ms") is not None:
        push("# HELP glassbox_splink_duration_ms last Splink cycle duration")
        push("# TYPE glassbox_splink_duration_ms gauge")
        push(f'glassbox_splink_duration_ms {int(splink["duration_ms"])}')

    return "\n".join(out) + "\n"


# ─── Routes ───────────────────────────────────────────────────────────────


router = APIRouter()


@router.get("/health/db")
async def health_db():
    try:
        val = await fetchval_read("SELECT 1")
        assert val == 1
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unreachable: {type(e).__name__}: {e}")


@router.get("/metrics", response_class=Response)
async def prometheus_metrics(request: Request):
    """Phase 6 follow-up — Prometheus exposition format.

    Returns plain text in the line-based Prometheus format so
    node_exporter / prometheus / vmagent / OpenObserve / etc. can
    scrape this endpoint directly without a translation layer.

    Metric families exported:
      glassbox_db_up                              1/0
      glassbox_db_query_latency_ms                gauge
      glassbox_pool_in_use                        gauge
      glassbox_pool_size                          gauge
      glassbox_ingester_health{layer,status}      gauge (0/1 per status)
      glassbox_ingester_cycles_total{layer}       counter
      glassbox_ingester_cycles_failed_total{layer} counter
      glassbox_ingester_db_write_failures_total{layer} counter
      glassbox_ingester_tracked_entities{layer}   gauge
      glassbox_findings_5m                        gauge
      glassbox_findings_60m                       gauge
      glassbox_splink_predicted                   gauge
      glassbox_splink_persisted                   gauge
      glassbox_splink_duration_ms                 gauge

    Content-Type: text/plain; version=0.0.4 per Prometheus spec.
    """
    from fastapi.responses import PlainTextResponse

    ing_list = getattr(request.app.state, "ingesters", None) or []
    last_splink_run = None
    try:
        from glassbox_server import _LAST_SPLINK_RUN as _splink
        last_splink_run = dict(_splink)
    except Exception:
        last_splink_run = None
    recent_cycle = getattr(request.app.state, "recent_cycle", None)
    snap = await build_health_snapshot(
        ing_list, recent_cycle, last_splink_run=last_splink_run,
    )
    return PlainTextResponse(
        _render_prometheus(snap),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/metrics/prefilter", response_class=Response)
async def prefilter_metrics(request: Request):
    """Per-rule + per-reason prefilter Prometheus exposition.

    Separate endpoint from /metrics so operators can scrape this on
    a longer cadence (~30 s) than the ingester health snapshot (5 s).
    Returns ``# prefilter metrics disabled`` plain text when
    prometheus-client is missing or the gdelt_bulk ingester isn't
    registered (e.g. registry refused at startup).

    Metric families:
      glassbox_prefilter_pass_total
      glassbox_prefilter_drop_total{rule}
      glassbox_prefilter_drop_by_reason_total{reason}
      glassbox_prefilter_queue_depth
      glassbox_prefilter_queue_max_depth
      glassbox_prefilter_queue_tail_dropped_total
      glassbox_prefilter_queue_new_event_dropped_total
      glassbox_prefilter_priority_score (histogram)
    """
    from fastapi.responses import PlainTextResponse

    ing_list = getattr(request.app.state, "ingesters", None) or []
    gdelt_bulk = next(
        (i for i in ing_list
         if i.__class__.__name__ == "GdeltBulkIngester"),
        None,
    )
    if gdelt_bulk is None:
        return PlainTextResponse(
            "# prefilter metrics disabled — gdelt_bulk ingester not registered\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
    body = gdelt_bulk.engine.metrics.render_prometheus()
    if not body:
        return PlainTextResponse(
            "# prefilter metrics disabled — prometheus-client not installed\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
    return PlainTextResponse(
        body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/health/full")
async def health_full(request: Request):
    """Aggregated production-monitoring snapshot — Phase 6 starter.

    Designed to be polled every 10–60 s by an external monitor.
    See module-level `build_health_snapshot()` for the helper that
    assembles the payload (testable without HTTP plumbing).
    """
    ing_list = getattr(request.app.state, "ingesters", None) or []
    last_splink_run = None
    if not ing_list:
        try:
            from glassbox_server import _ingesters as _ings
            ing_list = list(_ings)
        except Exception:
            ing_list = []
    # Pull the Splink loop's last-run snapshot when running inside the
    # production server. Tests + standalone consumers pass None.
    try:
        from glassbox_server import _LAST_SPLINK_RUN as _splink
        last_splink_run = dict(_splink)
    except Exception:
        last_splink_run = None
    recent_cycle = getattr(request.app.state, "recent_cycle", None)
    return await build_health_snapshot(
        ing_list, recent_cycle, last_splink_run=last_splink_run,
    )


@router.get("/system-state")
async def system_state():
    """One-shot consolidated ops snapshot — combines DB health,
    sanctions index size + per-authority breakdown, last 24h tier-1
    event counts per type, total entity counts. Useful as a single
    polling target for monitoring tools or status pages.

    Round-trip cost: ~2 small SELECTs plus a COUNT — sub-100ms cold.
    """
    # Reading the Tier-1 event-type tuple directly from the alerts
    # cluster — post-Phase-3 cleanup 2026-05-27 (was previously a
    # deferred `from api_v1 import _TIER1_EVENT_TYPES_FOR_POLL` shim
    # via api_v1.py, but the alerts cluster is now extracted and the
    # shim was dropped in the post-Phase-3 audit-cleanup commit).
    from web.routes.api_v1.alerts import _TIER1_EVENT_TYPES_FOR_POLL

    # Tier-1 event counts (last 24h)
    tier1_rows = await fetch_read(
        """
        SELECT event_type, COUNT(*) AS n
        FROM event
        WHERE event_type = ANY($1::text[])
          AND event_time >= NOW() - INTERVAL '24 hours'
        GROUP BY event_type
        ORDER BY n DESC
        """,
        list(_TIER1_EVENT_TYPES_FOR_POLL),
    )
    # Sanctions counts per authority
    sanc_rows = await fetch_read(
        """
        SELECT entity_type,
               properties->>'sanctioning_authority' AS authority,
               COUNT(*) AS n
        FROM entity
        WHERE entity_type IN ('sanctioned_vessel', 'sanctioned_aircraft')
        GROUP BY 1, 2
        """
    )
    # Total per-entity-type live counts
    ent_rows = await fetch_read(
        """
        SELECT entity_type, COUNT(*) AS n
        FROM entity
        GROUP BY entity_type
        ORDER BY n DESC
        """
    )

    sanctions_by_authority: Dict[str, Dict[str, int]] = {}
    sanctions_total = {"sanctioned_vessel": 0, "sanctioned_aircraft": 0}
    for r in sanc_rows:
        auth = r["authority"] or "Unknown"
        slot = sanctions_by_authority.setdefault(auth, {
            "sanctioned_vessel": 0, "sanctioned_aircraft": 0,
        })
        slot[r["entity_type"]] = r["n"]
        sanctions_total[r["entity_type"]] += r["n"]

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tier1_events_24h": {r["event_type"]: r["n"] for r in tier1_rows},
        "tier1_total_24h": sum(r["n"] for r in tier1_rows),
        "sanctions": {
            "totals": sanctions_total,
            "by_authority": sanctions_by_authority,
        },
        "entities": {r["entity_type"]: r["n"] for r in ent_rows},
    }
