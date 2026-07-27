"""
Tests for the P1-C per-task model routing in llm_ollama.py.

Verifies the resolution priority:
  1. explicit model= argument (handled by call site, not _model_name)
  2. per-task env var (TASK_MODEL_ENVS)
  3. per-task chosen-winner default (TASK_DEFAULTS)
  4. global FULCRUM_LLM_MODEL env var
  5. module DEFAULT_MODEL constant

These are pure-function tests — no Ollama HTTP, no DB. Fast.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_ollama  # noqa: E402


# Capture-and-restore fixture so env mutations don't leak across tests.
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(llm_ollama.TASK_MODEL_ENVS.values()) + ["FULCRUM_LLM_MODEL"]:
        monkeypatch.delenv(k, raising=False)
    yield


def test_no_task_uses_default_model():
    assert llm_ollama._model_name() == "qwen2.5:14b"


def test_unknown_task_falls_through_to_default():
    assert llm_ollama._model_name("not_a_real_task") == "qwen2.5:14b"


def test_brief_llm_default_is_qwen():
    """The 2026-05-21 benchmark winner for brief_llm — qwen2.5:14b was
    more disciplined (caught both signals in vessel sample; said 'No
    priority items.' for empty data instead of hallucinating)."""
    assert llm_ollama._model_name("brief_llm") == "qwen2.5:14b"


def test_intel_query_default_is_llama():
    """The 2026-05-21 benchmark winner for intel_query — llama3.1:latest
    was 2× faster than qwen2.5:14b at equal quality on this tactical-brief
    shape. User-facing endpoint, so latency wins."""
    assert llm_ollama._model_name("intel_query") == "llama3.1:latest"


def test_forecast_default_is_llama():
    """The 2026-05-21 benchmark winner for forecast — llama3.1:latest was
    1.5× faster than qwen2.5:14b and validated JSON 3/3 samples."""
    assert llm_ollama._model_name("forecast") == "llama3.1:latest"


def test_sitrep_and_ask_stay_on_qwen():
    """sitrep and ask were NOT benchmarked in the 2026-05-21 pass; they
    keep the qwen2.5:14b default until a future benchmark covers them."""
    assert llm_ollama._model_name("sitrep") == "qwen2.5:14b"
    assert llm_ollama._model_name("ask") == "qwen2.5:14b"


def test_per_task_env_var_overrides_default(monkeypatch):
    """An operator setting GLASSBOX_LLM_MODEL_BRIEF=phi4:latest must take
    effect — that's the override mechanism for A/B testing without code
    changes."""
    monkeypatch.setenv("GLASSBOX_LLM_MODEL_BRIEF", "phi4:latest")
    assert llm_ollama._model_name("brief_llm") == "phi4:latest"


def test_per_task_env_var_only_affects_its_own_task(monkeypatch):
    """Setting GLASSBOX_LLM_MODEL_BRIEF must not bleed into intel_query."""
    monkeypatch.setenv("GLASSBOX_LLM_MODEL_BRIEF", "phi4:latest")
    assert llm_ollama._model_name("brief_llm") == "phi4:latest"
    assert llm_ollama._model_name("intel_query") == "llama3.1:latest"
    assert llm_ollama._model_name("forecast") == "llama3.1:latest"


def test_global_fulcrum_env_used_when_no_task(monkeypatch):
    """The pre-P1-C path — FULCRUM_LLM_MODEL was the only env var.
    Backwards-compat: passing no task and setting FULCRUM_LLM_MODEL
    still gives that model."""
    monkeypatch.setenv("FULCRUM_LLM_MODEL", "command-r:latest")
    assert llm_ollama._model_name() == "command-r:latest"


def test_per_task_default_beats_global_fulcrum_env(monkeypatch):
    """If the operator sets FULCRUM_LLM_MODEL=command-r:latest as a
    workspace-wide preference, the per-task chosen-winner defaults
    should STILL win — they are more specific. The operator who
    wants to fully override per-task must set the per-task env vars."""
    monkeypatch.setenv("FULCRUM_LLM_MODEL", "command-r:latest")
    assert llm_ollama._model_name("brief_llm") == "qwen2.5:14b"
    assert llm_ollama._model_name("intel_query") == "llama3.1:latest"


def test_per_task_env_overrides_both_per_task_default_and_global(monkeypatch):
    """Per-task env var is the highest-priority resolution (other than
    explicit model= passed by the caller, which short-circuits before
    _model_name even runs)."""
    monkeypatch.setenv("FULCRUM_LLM_MODEL", "command-r:latest")
    monkeypatch.setenv("GLASSBOX_LLM_MODEL_BRIEF", "phi4:latest")
    assert llm_ollama._model_name("brief_llm") == "phi4:latest"


def test_unknown_task_with_global_env_uses_global(monkeypatch):
    """If the task name isn't in TASK_MODEL_ENVS, the global
    FULCRUM_LLM_MODEL is the fallback (not the per-task defaults map)."""
    monkeypatch.setenv("FULCRUM_LLM_MODEL", "phi4:latest")
    assert llm_ollama._model_name("not_a_real_task") == "phi4:latest"


def test_task_defaults_map_keys_match_env_map_keys():
    """Sanity: every task in TASK_DEFAULTS must have a corresponding env
    var entry in TASK_MODEL_ENVS, and vice versa. Otherwise an operator
    setting an env var for a task with no default would not be honored."""
    assert set(llm_ollama.TASK_DEFAULTS.keys()) == set(llm_ollama.TASK_MODEL_ENVS.keys())


def test_no_empty_string_defaults():
    """Each TASK_DEFAULTS value must be a non-empty model spec."""
    for task, model in llm_ollama.TASK_DEFAULTS.items():
        assert isinstance(model, str) and model, f"{task} has empty default"
