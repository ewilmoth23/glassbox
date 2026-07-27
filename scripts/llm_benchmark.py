"""
LLM model selection benchmark — P1-C from GLASSBOX_BACKEND_BACKLOG.md.

Benchmarks each candidate Ollama model on the actual production LLM tasks:
  1. brief_llm   — one-sentence analyst note over a deterministic brief (prose, ≤80t)
  2. intel_query — tactical intelligence brief over live globe context (prose, ≤500t)
  3. forecast    — JSON 48-hour hotspot prediction (structured, ≤400t)

Measures per (model, task, sample):
  - cold latency (first call after model unload)
  - warm latency (subsequent calls — the production-relevant number)
  - output bytes
  - validity (for JSON tasks: parses to a dict with the expected keys?)
  - sample output (first 200 chars — for the operator's quality eyeball)

Writes raw JSON results to docs/llm-benchmarks-raw.json.

Usage (from MEWR root):
    21_GLASSBOX_AI/.venv/bin/python 21_GLASSBOX_AI/scripts/llm_benchmark.py
    21_GLASSBOX_AI/.venv/bin/python 21_GLASSBOX_AI/scripts/llm_benchmark.py --models qwen2.5:14b,llama3.1:latest
    21_GLASSBOX_AI/.venv/bin/python 21_GLASSBOX_AI/scripts/llm_benchmark.py --samples 1   # quick smoke

Default config: 4 models × 3 tasks × 3 samples = 36 inferences + 4 cold loads.
Expect 30–40 minutes wall-clock on a Mac M4 Pro 24GB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import init_pools, close_pools, fetch_read  # noqa: E402


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
RESULTS_PATH = ROOT / "docs" / "llm-benchmarks-raw.json"


# ─── Default candidates (chat-capable models only; nomic-embed excluded) ──

DEFAULT_MODELS = [
    "qwen2.5:14b",       # current production baseline
    "llama3.1:latest",   # smallest of the chat models (~8B), latency benchmark
    "qwen3.5:9b",        # newer, smaller qwen
    "deepseek-r1:14b",   # reasoning-tuned, hypothesis: best for JSON-structured
]
# Excluded from default to stay under 40-min wall budget; re-add via --models:
#   phi4:latest          (~14B Microsoft Phi-4 — prose-tuned)
#   qwen2.5-coder:14b    (code-specialized; not relevant for these tasks)
#   command-r:latest     (18 GB; would need solo cycle, possibly OOM)


# ─── Task fixtures (mirror the production prompts exactly) ────────────────

BRIEF_LLM_SYSTEM = None  # brief_llm uses bare prompt with no system

BRIEF_LLM_PROMPT = (
    "Below is a structured situational brief built from telemetry. In ONE "
    "sentence (max 25 words), tell an analyst what to investigate FIRST. "
    "Be specific — reference entity names, event titles, or magnitudes "
    "directly when relevant. Do NOT add numbers or names not in the data. "
    "Do NOT pad with adjectives. If nothing is notable, say 'No priority "
    "items.'\n\n"
    "DATA:\n{deterministic_brief}\n\n"
    "ANALYST NOTE:"
)

INTEL_QUERY_SYSTEM = (
    "You are Glassbox, a live OSINT intelligence analyst. "
    "Answer directly and tactically in under 300 words."
)
INTEL_QUERY_PROMPT_TPL = (
    "GLOBE CONTEXT:\n{context}\n\nQUERY: {question}\n\nINTEL BRIEF:"
)

FORECAST_SYSTEM = (
    "You are the Glassbox 48-hour forecaster. Produce a short, "
    "specific, evidence-grounded prediction. Return JSON only."
)
FORECAST_PROMPT_TPL = (
    'Forecast 48 hours of activity for hotspot {layer}/{region}. '
    'Recent evidence:\n{evidence}\n\n'
    'Return ONLY a JSON object with keys: prediction (string ≤30 words), '
    'confidence (float 0..1), watch_items (list of strings, ≤3). No prose.'
)


# ─── Real-data sample fetchers — pull live evidence from Postgres ─────────

async def fetch_brief_samples(n: int = 3) -> List[Dict[str, Any]]:
    """Build n deterministic-brief-shaped strings from recent DB activity.
    Each sample summarizes the last hour's activity in a different layer."""
    out: List[Dict[str, Any]] = []
    layer_specs = [
        ("aircraft", "aircraft_underway,aircraft_landed,military_aircraft_underway"),
        ("vessel",   "vessel_underway,detected_proximity,port_call"),
        ("nature",   "earthquake_event,wildfire_event,weather_alert"),
    ]
    for label, event_types in layer_specs[:n]:
        type_list = event_types.split(",")
        rows = await fetch_read("""
            SELECT event_type, COUNT(*) AS n,
                   MAX(severity) AS max_sev,
                   string_agg(DISTINCT split_part(title, ':', 1), '; ' ORDER BY split_part(title, ':', 1)) AS subjects
            FROM event
            WHERE event_type = ANY($1::text[])
              AND event_time >= now() - interval '1 hour'
              AND title IS NOT NULL
            GROUP BY event_type
            ORDER BY n DESC
            LIMIT 4
        """, type_list)
        # Construct a deterministic-brief-shaped string
        lines = [f"== {label.upper()} ACTIVITY (last 1h) =="]
        for r in rows:
            lines.append(
                f"  {r['event_type']}: {r['n']} events, "
                f"max_severity={r['max_sev']}, "
                f"subjects={(r['subjects'] or '')[:120]}"
            )
        if not rows:
            lines.append("  (no events)")
        out.append({
            "label": f"brief_{label}",
            "deterministic_brief": "\n".join(lines),
        })
    return out


async def fetch_intel_query_samples(n: int = 3) -> List[Dict[str, Any]]:
    """Build n realistic /api/intel/query inputs.

    Each is a globe-context summary + a real-world analyst question."""
    layer_summary_rows = await fetch_read("""
        SELECT event_type, COUNT(*) AS n
        FROM event
        WHERE event_time >= now() - interval '24 hours'
        GROUP BY event_type
        ORDER BY n DESC
        LIMIT 10
    """)
    context = "Active layers (last 24h):\n" + "\n".join(
        f"  - {r['event_type']}: {r['n']:,} events" for r in layer_summary_rows
    )
    top_severity = await fetch_read("""
        SELECT event_type, title, severity, event_time
        FROM event
        WHERE event_time >= now() - interval '24 hours'
          AND severity IS NOT NULL
          AND title IS NOT NULL
        ORDER BY severity DESC
        LIMIT 8
    """)
    context += "\n\nTop severity events:\n" + "\n".join(
        f"  - [{r['event_type']} sev={r['severity']}] {(r['title'] or '')[:80]}"
        for r in top_severity
    )

    questions = [
        "What is the most concerning maritime activity in the last 24 hours, and why?",
        "Are there any patterns in earthquake activity that suggest an emerging risk?",
        "Summarize the top 3 priority watch items for the next 12 hours.",
    ]
    return [
        {"label": f"intel_q{i+1}", "context": context, "question": q}
        for i, q in enumerate(questions[:n])
    ]


def build_forecast_samples(n: int = 3) -> List[Dict[str, Any]]:
    """Construct n forecast prompts. These are deterministic synthetic
    hotspots rather than DB-pulled, because the anomaly hotspot pipeline
    isn't always producing fresh data and we want repeatable benchmarks."""
    samples = [
        {
            "label": "forecast_seismic_japan",
            "layer": "earthquakes",
            "region": "japan_korea",
            "evidence": (
                "- M5.2 earthquake at 35.6N 139.8E (Tokyo Bay) 4h ago\n"
                "- M4.8 earthquake at 35.4N 140.1E (Chiba) 6h ago\n"
                "- Cluster z-score over baseline: 3.7\n"
                "- Historical: this fault produced M7+ events in 1923 and 2011"
            ),
        },
        {
            "label": "forecast_wildfire_west",
            "layer": "wildfires",
            "region": "north_america_west",
            "evidence": (
                "- 23,371 VIIRS heat detections in 24h (vs 18,400 baseline)\n"
                "- 4,438 MODIS detections in 24h\n"
                "- Cluster z-score: 2.1\n"
                "- Recent NOAA fire weather watch issued for OR/ID/MT\n"
                "- Persistent ridge of high pressure forecast 5-7 days"
            ),
        },
        {
            "label": "forecast_maritime_baltic",
            "layer": "vessels",
            "region": "baltic_sea",
            "evidence": (
                "- 14 dark-ship events vs 4/day baseline (3.5× spike)\n"
                "- 3 proximity events involving sanctioned vessels\n"
                "- Cluster z-score: 2.8\n"
                "- Historical: this region had similar spike pattern 2 weeks "
                "before the Dec-2023 Estonia cable cut incident"
            ),
        },
    ]
    return samples[:n]


# ─── Ollama call ──────────────────────────────────────────────────────────

async def call_ollama(
    *,
    model: str,
    system: Optional[str],
    prompt: str,
    json_mode: bool,
    max_tokens: int,
    timeout_total: float,
    num_ctx: int = 4096,
) -> Tuple[str, float]:
    """Calls /api/generate. Returns (text, wall_seconds)."""
    body: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": num_ctx,
            "num_predict": max_tokens,
        },
    }
    if system:
        body["system"] = system
    if json_mode:
        body["format"] = "json"
    timeout = aiohttp.ClientTimeout(total=timeout_total, connect=5.0)
    t0 = time.perf_counter()
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{OLLAMA_URL}/api/generate", json=body) as r:
            r.raise_for_status()
            d = await r.json()
    dt = time.perf_counter() - t0
    return (d.get("response") or "").strip(), dt


async def unload_model(model: str) -> None:
    """Force-unload a model to measure the next call's cold latency.
    Ollama supports POST /api/generate with keep_alive=0 to drop the model."""
    body = {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    timeout = aiohttp.ClientTimeout(total=30.0)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{OLLAMA_URL}/api/generate", json=body) as r:
                if r.status == 200:
                    await r.json()
    except Exception:
        pass  # best-effort — if it fails, the next call still works


# ─── Benchmark driver ─────────────────────────────────────────────────────

async def benchmark_one(
    *,
    model: str,
    task: str,
    sample: Dict[str, Any],
    call_idx: int,
) -> Dict[str, Any]:
    """One (model, task, sample) inference. Returns metrics + sample output."""
    if task == "brief_llm":
        system, prompt = BRIEF_LLM_SYSTEM, BRIEF_LLM_PROMPT.format(**sample)
        json_mode, max_tokens = False, 80
    elif task == "intel_query":
        system = INTEL_QUERY_SYSTEM
        prompt = INTEL_QUERY_PROMPT_TPL.format(
            context=sample["context"], question=sample["question"])
        json_mode, max_tokens = False, 500
    elif task == "forecast":
        system = FORECAST_SYSTEM
        prompt = FORECAST_PROMPT_TPL.format(
            layer=sample["layer"], region=sample["region"],
            evidence=sample["evidence"])
        json_mode, max_tokens = True, 400
    else:
        raise ValueError(f"unknown task: {task}")

    timeout = 240.0  # generous — biggest model + cold load can hit 60-90s
    try:
        out, dt = await call_ollama(
            model=model, system=system, prompt=prompt,
            json_mode=json_mode, max_tokens=max_tokens,
            timeout_total=timeout, num_ctx=4096,
        )
        result: Dict[str, Any] = {
            "model": model, "task": task, "sample_label": sample["label"],
            "call_idx": call_idx, "wall_s": round(dt, 2),
            "output_bytes": len(out),
            "output_preview": out[:200],
            "error": None,
        }
        # JSON-task validity check
        if task == "forecast":
            try:
                parsed = json.loads(out)
                has_keys = all(k in parsed for k in ("prediction", "confidence", "watch_items"))
                result["json_valid"] = bool(has_keys)
                result["json_parsed_keys"] = sorted(parsed.keys()) if isinstance(parsed, dict) else None
            except Exception as e:
                result["json_valid"] = False
                result["json_error"] = f"{type(e).__name__}: {e}"
        return result
    except asyncio.TimeoutError:
        return {
            "model": model, "task": task, "sample_label": sample["label"],
            "call_idx": call_idx, "wall_s": timeout,
            "output_bytes": 0, "output_preview": "",
            "error": "TIMEOUT",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "model": model, "task": task, "sample_label": sample["label"],
            "call_idx": call_idx, "wall_s": -1,
            "output_bytes": 0, "output_preview": "",
            "error": f"{type(e).__name__}: {e}",
        }


async def run_benchmark(
    models: List[str],
    tasks: List[str],
    samples_per_task: Dict[str, List[Dict[str, Any]]],
    cold_start_each_model: bool = True,
) -> Dict[str, Any]:
    """For each model: unload it (if cold_start), then run all (task,sample)
    pairs back-to-back. The FIRST call per model is the cold-load measurement.
    Subsequent calls within the same model are warm."""
    log = logging.getLogger("bench")
    all_results: List[Dict[str, Any]] = []
    started_at = time.time()

    for m_idx, model in enumerate(models):
        log.info(f"\n══════ MODEL {m_idx+1}/{len(models)}: {model} ══════")
        if cold_start_each_model:
            log.info(f"unloading {model} to force cold start...")
            await unload_model(model)
            await asyncio.sleep(2)
        call_idx = 0
        for task in tasks:
            for sample in samples_per_task[task]:
                call_idx += 1
                phase = "COLD" if call_idx == 1 else "warm"
                log.info(f"  [{call_idx:2d}] {phase:4s} {task:13s} {sample['label']:30s} ...")
                r = await benchmark_one(
                    model=model, task=task, sample=sample, call_idx=call_idx)
                r["phase"] = phase
                if r.get("error"):
                    log.info(f"       ERROR: {r['error']}  [{r['wall_s']}s]")
                else:
                    extra = ""
                    if task == "forecast":
                        extra = f"  json_valid={r.get('json_valid')}"
                    log.info(
                        f"       {r['wall_s']:6.2f}s  "
                        f"{r['output_bytes']:4d} bytes{extra}"
                    )
                all_results.append(r)

    elapsed = time.time() - started_at
    return {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started_at)),
        "elapsed_s": round(elapsed, 1),
        "models": models,
        "tasks": tasks,
        "samples_per_task": {k: [s["label"] for s in v] for k, v in samples_per_task.items()},
        "results": all_results,
    }


# ─── Summary ──────────────────────────────────────────────────────────────

def summarize(results: List[Dict[str, Any]]) -> str:
    """Markdown summary table grouped by task → model."""
    by_task_model: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in results:
        by_task_model.setdefault((r["task"], r["model"]), []).append(r)

    lines: List[str] = []
    for task in sorted({r["task"] for r in results}):
        lines.append(f"\n## {task}\n")
        lines.append("| Model | Cold | Warm p50 | Warm max | Bytes (avg) | JSON-valid | Errors |")
        lines.append("|---|---:|---:|---:|---:|---|---|")
        for model in sorted({r["model"] for r in results if r["task"] == task}):
            rs = sorted(by_task_model[(task, model)], key=lambda x: x["call_idx"])
            cold = rs[0]["wall_s"] if rs else None
            warm = [r["wall_s"] for r in rs[1:] if r["wall_s"] > 0]
            warm_p50 = sorted(warm)[len(warm)//2] if warm else None
            warm_max = max(warm) if warm else None
            bytes_avg = round(sum(r["output_bytes"] for r in rs) / max(len(rs), 1))
            errs = sum(1 for r in rs if r.get("error"))
            json_valid_marker = ""
            if task == "forecast":
                valid = sum(1 for r in rs if r.get("json_valid"))
                json_valid_marker = f"{valid}/{len(rs)}"
            lines.append(
                f"| {model} | {cold:.1f}s | "
                f"{(warm_p50 if warm_p50 else 0):.1f}s | "
                f"{(warm_max if warm_max else 0):.1f}s | "
                f"{bytes_avg} | {json_valid_marker} | {errs} |"
            )
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────

async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=str, default=",".join(DEFAULT_MODELS),
                        help="comma-separated list of Ollama models")
    parser.add_argument("--samples", type=int, default=3,
                        help="samples per task (default 3)")
    parser.add_argument("--tasks", type=str,
                        default="brief_llm,intel_query,forecast",
                        help="comma-separated task names")
    parser.add_argument("--cold-start", type=int, default=1,
                        help="1=unload each model before its run; 0=skip")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("bench")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    n = args.samples

    log.info(f"Fetching {n} samples per task from DB / synthetic...")
    await init_pools()
    try:
        samples_per_task: Dict[str, List[Dict[str, Any]]] = {}
        if "brief_llm" in tasks:
            samples_per_task["brief_llm"] = await fetch_brief_samples(n)
        if "intel_query" in tasks:
            samples_per_task["intel_query"] = await fetch_intel_query_samples(n)
        if "forecast" in tasks:
            samples_per_task["forecast"] = build_forecast_samples(n)
        for t, ss in samples_per_task.items():
            log.info(f"  {t}: {len(ss)} samples")
    finally:
        await close_pools()

    log.info(f"\nRunning benchmark: {len(models)} models × {len(tasks)} tasks × {n} samples")
    log.info(f"Estimated wall time: {(len(models)*len(tasks)*n + len(models)) * 30}s")

    out = await run_benchmark(
        models=models, tasks=tasks,
        samples_per_task=samples_per_task,
        cold_start_each_model=bool(args.cold_start),
    )

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log.info(f"\nRaw results: {RESULTS_PATH}")
    log.info(f"Total wall time: {out['elapsed_s']}s ({out['elapsed_s']/60:.1f} min)")

    print()
    print("════════════════════════════ SUMMARY ════════════════════════════")
    print(summarize(out["results"]))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
