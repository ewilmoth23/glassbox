"""
Embeddings — Phase 4a (V2 plan: pgvector + semantic similarity).

Loads `sentence-transformers/all-MiniLM-L6-v2` once per process and exposes
two helpers:

    embed_text(text)         → list[float] (length 384) or None
    embed_texts([t1, t2..])  → list[list[float] | None]   (batched)

The model is 80 MB on disk and produces 384-dim vectors that map directly
into the existing `event.embedding VECTOR(384)` column with the HNSW
cosine-ops index. The first call materializes the model — subsequent
calls reuse it. A small in-process LRU caches identical-text embeddings
because re-emit cycles often produce the same titles back to back.

Why all-MiniLM-L6-v2 and not a larger model:
  - 384 dims keeps the index footprint manageable (~1.5 KB/event on disk).
  - The schema (init.sql:339) was designed for this exact model — both the
    column dim and the HNSW config target its similarity profile.
  - 80 MB on-disk + ~250 MB resident, vs gigabytes for larger models that
    would barely improve recall on the kinds of news/filing snippets we
    embed.

Fail-soft: if the import fails (model not yet installed) all helpers
return None / list-of-None. Writers that opt into embedding can continue
without it; the row is persisted with embedding=NULL and will be picked
up by a backfill the next time this module loads cleanly.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import List, Optional


_log = logging.getLogger("embeddings")

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

# Lazy-loaded global model + the thread lock that guards instantiation.
_model = None
_model_load_attempted = False
_model_load_error: Optional[str] = None
_load_lock = threading.Lock()

# Tiny LRU on the hot path — re-emit cycles often produce the same title.
_CACHE_CAP = 2048
_cache: "OrderedDict[str, List[float]]" = OrderedDict()
_cache_lock = threading.Lock()


def _load_model():
    """Idempotent loader. Returns the model or None on permanent failure."""
    global _model, _model_load_attempted, _model_load_error
    if _model is not None:
        return _model
    if _model_load_attempted and _model is None:
        # Earlier attempt failed; don't keep retrying inside the hot path.
        return None
    with _load_lock:
        if _model is not None:
            return _model
        if _model_load_attempted and _model is None:
            return None
        _model_load_attempted = True
        try:
            from sentence_transformers import SentenceTransformer  # noqa
            _log.info(f"loading {MODEL_NAME} (first call — may take a moment)")
            _model = SentenceTransformer(MODEL_NAME)
            _log.info(f"{MODEL_NAME} loaded; embedding dim={EMBED_DIM}")
        except Exception as e:
            _model_load_error = f"{type(e).__name__}: {e}"
            _log.warning(f"sentence-transformers load failed: {_model_load_error}")
            _model = None
    return _model


def is_available() -> bool:
    """True if the model loaded (or can lazy-load) and embeddings are usable."""
    if _model is not None:
        return True
    if _model_load_attempted and _model is None:
        return False
    # First call — try to load now
    return _load_model() is not None


def warm_up() -> bool:
    """Eagerly load the sentence-transformers model.

    Call from glassbox-server startup so the first `embed_text(q)` from an
    HTTP handler doesn't pay the 5-15s cold-load (which used to time out
    `GET /api/v1/events/similar?q=…` clients at 30s). Idempotent — a second
    call after a successful first is a no-op.

    Returns True when the model is loaded after the call, False if loading
    failed permanently (caller should log; embedding-bearing endpoints
    will return 503 in that case).
    """
    return _load_model() is not None


def status() -> dict:
    """Status snapshot — surfaces whether embeddings are functional."""
    return {
        "model_name":  MODEL_NAME,
        "embed_dim":   EMBED_DIM,
        "loaded":      _model is not None,
        "load_attempted": _model_load_attempted,
        "load_error":  _model_load_error,
        "cache_size":  len(_cache),
        "cache_cap":   _CACHE_CAP,
    }


def _cache_get(key: str) -> Optional[List[float]]:
    with _cache_lock:
        v = _cache.get(key)
        if v is not None:
            _cache.move_to_end(key)
        return v


def _cache_set(key: str, value: List[float]) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_CAP:
            _cache.popitem(last=False)


def embed_text(text: str) -> Optional[List[float]]:
    """Embed a single text. Returns 384-element float list or None.

    Returns None when:
      - model is not loadable (e.g. sentence-transformers missing)
      - text is empty or all-whitespace
      - encoding raises (caller should treat as best-effort)
    """
    if not text or not text.strip():
        return None
    cached = _cache_get(text)
    if cached is not None:
        return cached
    model = _load_model()
    if model is None:
        return None
    try:
        vec = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
        out = vec.astype(float).tolist()
        if len(out) != EMBED_DIM:
            _log.warning(f"unexpected embedding dim {len(out)} (want {EMBED_DIM})")
            return None
        _cache_set(text, out)
        return out
    except Exception as e:
        _log.info(f"embed_text failed: {type(e).__name__}: {e}")
        return None


def embed_texts(texts: List[str]) -> List[Optional[List[float]]]:
    """Batch-embed. Result list is parallel to input. None for empty/failed entries.

    Cache hits are filled first; only the cache-misses are sent to the model
    in one batched call.
    """
    out: List[Optional[List[float]]] = [None] * len(texts)
    if not texts:
        return out

    miss_idx: List[int] = []
    miss_text: List[str] = []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            continue
        cached = _cache_get(t)
        if cached is not None:
            out[i] = cached
        else:
            miss_idx.append(i)
            miss_text.append(t)

    if not miss_text:
        return out

    model = _load_model()
    if model is None:
        return out

    try:
        vecs = model.encode(miss_text, convert_to_numpy=True,
                            show_progress_bar=False, batch_size=32)
        for j, vec in enumerate(vecs):
            v = vec.astype(float).tolist()
            if len(v) != EMBED_DIM:
                continue
            out[miss_idx[j]] = v
            _cache_set(miss_text[j], v)
    except Exception as e:
        _log.info(f"embed_texts batch failed ({len(miss_text)} items): "
                  f"{type(e).__name__}: {e}")

    return out


def to_pgvector_literal(vec: Optional[List[float]]) -> Optional[str]:
    """Serialize a vector to the pgvector text format: '[0.123,0.456,...]'.

    asyncpg doesn't have a native pgvector codec; this string form is what
    you bind for `INSERT INTO event (..., embedding) VALUES (..., $N::vector)`.
    """
    if vec is None:
        return None
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# ─── Backfill helper ───────────────────────────────────────────────────────


# Event types whose title+description carries enough signal to be worth
# embedding. Keep this list narrow — globe-position events (planes, ships,
# satellites) have no useful text; embedding them would burn cycles and
# pollute the vector space.
TEXT_EVENT_TYPES = (
    "newsdata",
    "gdelt_topical",
    "hn_story",
    "sec_filing",
    "noaa_alert",
    "gdacs_alert",
    "donki",
    "nasa_eonet",
)


def event_text(title: Optional[str], description: Optional[str]) -> Optional[str]:
    """Compose the text we feed to the embedder. Pure helper — no side effects."""
    parts = [s.strip() for s in (title, description) if s and s.strip()]
    if not parts:
        return None
    return ". ".join(parts)


async def backfill_event_embeddings(*, batch_size: int = 64,
                                    max_events: Optional[int] = None) -> dict:
    """Populate event.embedding for rows where it is currently NULL.

    Operates only on event_types in TEXT_EVENT_TYPES. Processes in batches
    of `batch_size` so a single backfill pass doesn't blow memory or hold a
    long-running transaction.

    Returns:
        {"scanned": N, "embedded": M, "skipped_no_text": K, "skipped_no_model": L}
    """
    from db import acquire_write   # local import to avoid cycle on module load

    if not is_available():
        return {"scanned": 0, "embedded": 0, "skipped_no_text": 0,
                "skipped_no_model": 0, "error": "embeddings unavailable"}

    scanned = 0
    embedded = 0
    skipped_no_text = 0
    type_list = list(TEXT_EVENT_TYPES)

    while True:
        async with acquire_write() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event_time, title, description
                FROM event
                WHERE embedding IS NULL
                  AND event_type = ANY($1::text[])
                ORDER BY event_time DESC
                LIMIT $2
                """,
                type_list, batch_size,
            )
        if not rows:
            break
        scanned += len(rows)

        texts: List[Optional[str]] = []
        for r in rows:
            texts.append(event_text(r["title"], r["description"]))
        nonnull_idx = [i for i, t in enumerate(texts) if t]
        nonnull_texts = [texts[i] for i in nonnull_idx]
        skipped_no_text += len(rows) - len(nonnull_idx)

        if nonnull_texts:
            vecs = embed_texts(nonnull_texts)
        else:
            vecs = []

        async with acquire_write() as conn:
            async with conn.transaction():
                for j, idx in enumerate(nonnull_idx):
                    vec = vecs[j] if j < len(vecs) else None
                    if vec is None:
                        continue
                    lit = to_pgvector_literal(vec)
                    await conn.execute(
                        "UPDATE event SET embedding = $1::vector "
                        "WHERE id = $2 AND event_time = $3",
                        lit, rows[idx]["id"], rows[idx]["event_time"],
                    )
                    embedded += 1

        if max_events is not None and scanned >= max_events:
            break
        # If this batch returned fewer than batch_size rows, we're done.
        if len(rows) < batch_size:
            break

    return {
        "scanned":           scanned,
        "embedded":          embedded,
        "skipped_no_text":   skipped_no_text,
        "skipped_no_model":  0,
    }
