# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Ollama HTTP wrapper — single integration point for the LLM-bearing
call sites (forecaster + intelligence_loop SITREP today, brief.py
later).

Two transport options:

  * legacy `/api/generate` — what the call sites have used since the
    Phase 1.5 brief landed. ``"format": "json"`` enforces strict
    JSON. Supports ``options.num_ctx`` (e.g. 8192 for the
    intelligence_loop SITREP). Stable and battle-tested on the
    Mac Mini.

  * OpenAI-compat `/v1/chat/completions` — what `outlines.from_openai`
    will hook into when R3 unblocks (currently
    ``outlines>=1.0`` requires Python ≥3.10; main glassbox-server
    venv is 3.9.6). Uses ``response_format: {"type": "json_object"}``
    for strict JSON. **Note:** Ollama's OpenAI shim drops ``options``
    on the floor; the model's ``num_ctx`` is whatever its Modelfile
    sets. For prompts that need >2048 tokens of context (e.g. the
    SITREP), set the model's PARAMETER num_ctx in the Modelfile
    before flipping the feature flag.

Switch at runtime via env var:

  GLASSBOX_OLLAMA_USE_CHAT_API=1   # use /v1/chat/completions
  (unset, default)                 # use /api/generate

Default is **legacy** so production behavior is unchanged. Flip the
flag in a shadow context first — see
`OPERATOR_NEXT_STEPS_2026_05_10.md`.

Returns the response **text**. Both transports return the LLM's raw
output string; callers parse JSON via `llm_json.parse_with_schema`
(same module that consolidated the tolerant-parse pattern in R3
first-pass).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import aiohttp


_log = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen2.5:14b"
USE_CHAT_API_ENV = "GLASSBOX_OLLAMA_USE_CHAT_API"


# ─── Per-task model routing (P1-C, 2026-05-21) ────────────────────────────
#
# Each LLM task in production has different latency/quality tradeoffs. The
# 2026-05-21 benchmark (see docs/llm-benchmarks.md) measured 4 candidate
# models on 3 representative tasks (brief_llm, intel_query, forecast).
# These are the chosen-winner defaults:

TASK_DEFAULTS: Dict[str, str] = {
    "brief_llm":   "qwen2.5:14b",      # disciplined ≤25-word prose; 2.4s warm
    "intel_query": "llama3.1:latest",  # 2× faster than baseline, equal quality
    "forecast":    "llama3.1:latest",  # 1.5× faster, valid JSON 3/3
    # Not benchmarked 2026-05-21 — kept on qwen2.5:14b default. Re-benchmark
    # next pass; the routing infrastructure already supports overrides.
    "sitrep":      "qwen2.5:14b",
    "ask":         "qwen2.5:14b",
}

# Env-var override map. Set any of these to swap the model for that task
# without code changes. Useful for A/B testing or when a new model lands.
TASK_MODEL_ENVS: Dict[str, str] = {
    "brief_llm":   "GLASSBOX_LLM_MODEL_BRIEF",
    "intel_query": "GLASSBOX_LLM_MODEL_INTEL_QUERY",
    "forecast":    "GLASSBOX_LLM_MODEL_FORECAST",
    "sitrep":      "GLASSBOX_LLM_MODEL_SITREP",
    "ask":         "GLASSBOX_LLM_MODEL_ASK",
}


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def _model_name(task: Optional[str] = None) -> str:
    """Resolve the model name with priority:
      1. Per-task env var (e.g. GLASSBOX_LLM_MODEL_BRIEF for task='brief_llm')
      2. Per-task chosen-winner default (TASK_DEFAULTS)
      3. Global override env (FULCRUM_LLM_MODEL)
      4. Module default (DEFAULT_MODEL = qwen2.5:14b)

    Caller may also pass `model=...` directly to bypass all of this — that
    path remains backwards-compatible with pre-P1-C code.
    """
    if task and task in TASK_MODEL_ENVS:
        env_val = os.environ.get(TASK_MODEL_ENVS[task])
        if env_val:
            return env_val
        task_default = TASK_DEFAULTS.get(task)
        if task_default:
            return task_default
    return os.environ.get("FULCRUM_LLM_MODEL", DEFAULT_MODEL)


def use_chat_api() -> bool:
    """Single source of truth for the feature-flag check.

    Truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Anything else → legacy /api/generate.
    """
    val = (os.environ.get(USE_CHAT_API_ENV) or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


async def generate_json(
    *,
    system: str,
    prompt: str,
    model: Optional[str] = None,
    task: Optional[str] = None,
    temperature: float = 0.2,
    num_ctx: int = 4096,
    max_tokens: int = 400,
    timeout_total: float = 90.0,
    timeout_connect: float = 5.0,
) -> str:
    """Single entrypoint for "ask Ollama for a JSON-shaped response,
    return the raw text". Picks transport based on the env-var
    feature flag.

    Returns the LLM's textual output (unparsed). Caller is responsible
    for ``json.loads()`` or ``llm_json.parse_with_schema``.

    Args:
      system: system prompt.
      prompt: user prompt (or composite for /api/generate).
      model: explicit model override. If unset, resolves via `task`
        (see `_model_name`).
      task: production task name (one of TASK_MODEL_ENVS keys —
        'forecast', 'sitrep', etc.). Lets the call site say *what
        it's doing* instead of *which model to use*; the routing in
        `_model_name` picks the right model based on the 2026-05-21
        benchmark (see docs/llm-benchmarks.md).
      temperature: sampling temperature.
      num_ctx: context window. Honored by /api/generate only;
        /v1/chat/completions inherits the model's Modelfile setting.
        See module docstring for the migration caveat.
      max_tokens: max tokens to generate (num_predict on /api/generate).
      timeout_total: aiohttp timeout in seconds.
    """
    use_chat = use_chat_api()
    model = model or _model_name(task)
    timeout = aiohttp.ClientTimeout(total=timeout_total,
                                    connect=timeout_connect)
    url = _ollama_url()

    if use_chat:
        body: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{url}/v1/chat/completions", json=body) as r:
                r.raise_for_status()
                d = await r.json()
        choices = d.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()

    # Legacy /api/generate — preserves num_ctx semantics + matches
    # what the existing call sites have always sent.
    body = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": max_tokens,
        },
    }
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{url}/api/generate", json=body) as r:
            r.raise_for_status()
            d = await r.json()
    return (d.get("response") or "").strip()


async def generate_text(
    *,
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    task: Optional[str] = None,
    temperature: float = 0.3,
    num_ctx: int = 4096,
    max_tokens: int = 500,
    timeout_total: float = 120.0,
    timeout_connect: float = 5.0,
) -> str:
    """Like ``generate_json`` but for free-text generation (no
    JSON-format constraint). Used by brief.py / sentinel_runner.py /
    the empire-ask endpoints whose outputs are prose, not structured.

    Same feature flag — when GLASSBOX_OLLAMA_USE_CHAT_API=1, hits
    /v1/chat/completions WITHOUT the json_object response_format so
    the LLM is free to return prose.

    `system` is optional because some legacy call sites pass a
    composite prompt as the only input.

    `task` (optional): production task name that drives per-task
    routing — see `generate_json` docstring and `_model_name`.
    """
    use_chat = use_chat_api()
    model = model or _model_name(task)
    timeout = aiohttp.ClientTimeout(total=timeout_total,
                                    connect=timeout_connect)
    url = _ollama_url()

    if use_chat:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{url}/v1/chat/completions", json=body) as r:
                r.raise_for_status()
                d = await r.json()
        choices = d.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()

    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": max_tokens,
        },
    }
    if system:
        body["system"] = system
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{url}/api/generate", json=body) as r:
            r.raise_for_status()
            d = await r.json()
    return (d.get("response") or "").strip()
