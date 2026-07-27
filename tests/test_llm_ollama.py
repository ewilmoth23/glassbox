"""
21_GLASSBOX_AI/llm_ollama.py — Ollama transport wrapper tests.

These tests don't hit a live Ollama; they mock aiohttp and verify the
HTTP request shape + response unwrapping for both the legacy
/api/generate path and the new /v1/chat/completions path.

The point: prove that flipping GLASSBOX_OLLAMA_USE_CHAT_API=1
genuinely changes the wire format (different URL, different body
shape, different response shape) — so the operator's feature-flag
flip is meaningful, not a no-op.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_llm_ollama.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm_ollama import generate_json, use_chat_api, USE_CHAT_API_ENV  # noqa: E402


# ─── Feature flag ────────────────────────────────────────────────────────


def test_use_chat_api_defaults_false(monkeypatch):
    monkeypatch.delenv(USE_CHAT_API_ENV, raising=False)
    assert use_chat_api() is False


def test_use_chat_api_truthy_values(monkeypatch):
    """Several common 'enabled' spellings should all flip the flag.
    Belt-and-suspenders so an operator's `setenv … 1` and
    `setenv … true` and `setenv … on` all work."""
    for val in ["1", "true", "yes", "on", "TRUE", "Yes", "ON"]:
        monkeypatch.setenv(USE_CHAT_API_ENV, val)
        assert use_chat_api() is True, f"value {val!r} should be truthy"


def test_use_chat_api_falsy_values(monkeypatch):
    """Anything not in the truthy set is falsy — including '0',
    'false', 'no', empty string, garbage."""
    for val in ["0", "false", "no", "", "off", "garbage", "False"]:
        monkeypatch.setenv(USE_CHAT_API_ENV, val)
        assert use_chat_api() is False, f"value {val!r} should be falsy"


# ─── Helpers — fake aiohttp session/response ────────────────────────────


class _FakeResponse:
    """Async-context-manager-shaped fake. Records what URL and body
    we got called with so the test can assert on the wire shape."""

    def __init__(self, json_payload):
        self._payload = json_payload
        self.url = None
        self.body = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, response_payload):
        self._payload = response_payload
        self.last_url = None
        self.last_body = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json):
        self.last_url = url
        self.last_body = json
        return _FakeResponse(self._payload)


# ─── /api/generate path (legacy default) ────────────────────────────────


async def test_legacy_api_generate_path_default(monkeypatch):
    """With the env var unset (default), generate_json hits
    /api/generate with the legacy body shape (system + prompt at
    top level, format:json, options.num_ctx + options.num_predict),
    and reads response.response from the JSON body."""
    monkeypatch.delenv(USE_CHAT_API_ENV, raising=False)

    fake = _FakeSession(response_payload={"response": "  hello world  "})
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        out = await generate_json(
            system="sys", prompt="prompt", temperature=0.3,
            num_ctx=4096, max_tokens=400,
        )

    assert out == "hello world"
    assert fake.last_url.endswith("/api/generate")
    assert fake.last_body["prompt"] == "prompt"
    assert fake.last_body["system"] == "sys"
    assert fake.last_body["format"] == "json"
    assert fake.last_body["stream"] is False
    assert fake.last_body["options"]["temperature"] == 0.3
    assert fake.last_body["options"]["num_ctx"] == 4096
    assert fake.last_body["options"]["num_predict"] == 400
    # Legacy path doesn't use messages array
    assert "messages" not in fake.last_body


# ─── /v1/chat/completions path (feature flag on) ────────────────────────


async def test_chat_completions_path_when_flag_on(monkeypatch):
    """With GLASSBOX_OLLAMA_USE_CHAT_API=1, generate_json hits
    /v1/chat/completions with the OpenAI-shape body (messages array,
    response_format json_object, temperature + max_tokens at top
    level), and reads choices[0].message.content."""
    monkeypatch.setenv(USE_CHAT_API_ENV, "1")

    fake = _FakeSession(response_payload={
        "id": "chatcmpl-x", "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "  json output  "},
            "finish_reason": "stop",
        }],
    })
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        out = await generate_json(
            system="sys", prompt="prompt", temperature=0.3,
            num_ctx=4096, max_tokens=400,
        )

    assert out == "json output"
    assert fake.last_url.endswith("/v1/chat/completions")
    assert fake.last_body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user",   "content": "prompt"},
    ]
    assert fake.last_body["response_format"] == {"type": "json_object"}
    assert fake.last_body["temperature"] == 0.3
    assert fake.last_body["max_tokens"] == 400
    assert fake.last_body["stream"] is False
    # OpenAI-shape body should NOT carry the legacy keys.
    assert "system" not in fake.last_body
    assert "prompt" not in fake.last_body
    assert "format" not in fake.last_body
    # num_ctx is NOT honored on the OpenAI shim (documented caveat;
    # see module docstring) — confirm we don't accidentally send it
    # in a place that would get silently dropped.
    assert "options" not in fake.last_body


async def test_chat_completions_handles_empty_choices(monkeypatch):
    """An Ollama OpenAI-shim degenerate response (no choices) → return
    empty string rather than raising, matching the legacy path's
    fail-soft behavior on empty `response`."""
    monkeypatch.setenv(USE_CHAT_API_ENV, "1")
    fake = _FakeSession(response_payload={"choices": []})
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        out = await generate_json(system="s", prompt="p")
    assert out == ""


# ─── Cross-path defaults ────────────────────────────────────────────────


async def test_default_model_is_qwen(monkeypatch):
    """When neither model kwarg nor FULCRUM_LLM_MODEL env is set,
    falls back to the qwen2.5:14b default."""
    monkeypatch.delenv(USE_CHAT_API_ENV, raising=False)
    monkeypatch.delenv("FULCRUM_LLM_MODEL", raising=False)
    fake = _FakeSession(response_payload={"response": "x"})
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        await generate_json(system="s", prompt="p")
    assert fake.last_body["model"] == "qwen2.5:14b"


async def test_explicit_model_kwarg_wins_over_env(monkeypatch):
    """A model passed as a kwarg beats the FULCRUM_LLM_MODEL env var."""
    monkeypatch.delenv(USE_CHAT_API_ENV, raising=False)
    monkeypatch.setenv("FULCRUM_LLM_MODEL", "from-env-model")
    fake = _FakeSession(response_payload={"response": "x"})
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        await generate_json(model="kwarg-model", system="s", prompt="p")
    assert fake.last_body["model"] == "kwarg-model"


async def test_url_respects_ollama_url_env(monkeypatch):
    """OLLAMA_URL env (e.g. for a remote Ollama) is respected."""
    monkeypatch.delenv(USE_CHAT_API_ENV, raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://10.0.0.5:11434/")
    fake = _FakeSession(response_payload={"response": "x"})
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        await generate_json(system="s", prompt="p")
    assert fake.last_url == "http://10.0.0.5:11434/api/generate"


# ─── generate_text — free-text variant ──────────────────────────────────


async def test_generate_text_legacy_path_omits_format_json(monkeypatch):
    """Free-text path must NOT include `format: json` — that's
    JSON-mode and constrains the output even when callers want prose
    (the brief.py analyst note + the empire-ask endpoint)."""
    from llm_ollama import generate_text
    monkeypatch.delenv(USE_CHAT_API_ENV, raising=False)
    fake = _FakeSession(response_payload={"response": "free prose response"})
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        out = await generate_text(prompt="prose please", system="prose-mode")
    assert out == "free prose response"
    assert fake.last_url.endswith("/api/generate")
    assert fake.last_body["prompt"] == "prose please"
    assert fake.last_body["system"] == "prose-mode"
    assert "format" not in fake.last_body, \
        "free-text path leaked the json format constraint"


async def test_generate_text_legacy_omits_system_when_unset(monkeypatch):
    """Some legacy call sites pass only a composite prompt (no
    separate system message). Helper must NOT emit `system: null`
    or empty string — that confuses Ollama."""
    from llm_ollama import generate_text
    monkeypatch.delenv(USE_CHAT_API_ENV, raising=False)
    fake = _FakeSession(response_payload={"response": "x"})
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        await generate_text(prompt="solo")
    assert "system" not in fake.last_body


async def test_generate_text_chat_api_drops_response_format(monkeypatch):
    """OpenAI-compat path for free-text must NOT set
    response_format: json_object — that's the JSON-mode constraint
    we want OFF for prose."""
    from llm_ollama import generate_text
    monkeypatch.setenv(USE_CHAT_API_ENV, "1")
    fake = _FakeSession(response_payload={
        "choices": [{"message": {"role": "assistant",
                                  "content": "free prose"}}],
    })
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        out = await generate_text(prompt="prose please", system="prose-mode")
    assert out == "free prose"
    assert fake.last_url.endswith("/v1/chat/completions")
    assert "response_format" not in fake.last_body
    assert fake.last_body["messages"] == [
        {"role": "system", "content": "prose-mode"},
        {"role": "user", "content": "prose please"},
    ]


async def test_generate_text_chat_api_omits_system_message_when_unset(monkeypatch):
    """When `system` kwarg is None, the messages array contains only
    the user message — sending an empty system message has occasionally
    confused open-source models."""
    from llm_ollama import generate_text
    monkeypatch.setenv(USE_CHAT_API_ENV, "1")
    fake = _FakeSession(response_payload={
        "choices": [{"message": {"content": "x"}}],
    })
    with patch("llm_ollama.aiohttp.ClientSession",
               lambda timeout=None: fake):
        await generate_text(prompt="solo")
    assert len(fake.last_body["messages"]) == 1
    assert fake.last_body["messages"][0]["role"] == "user"
