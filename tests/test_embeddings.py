"""
Phase 4a — sentence-transformers embeddings + similarity search.

Asserts:
  - embed_text returns a 384-dim float list for non-empty text
  - empty / whitespace text returns None
  - same text returns the same vector (cache hit; bit-equal)
  - to_pgvector_literal serializes correctly
  - embed_texts batches and falls through cache
  - event_text composes title + description; safe on missing parts
  - Backfill populates embedding column for matching rows
  - Backfill is a no-op once everything is populated

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_embeddings.py -v
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute, fetchval, fetch  # noqa: E402
from embeddings import (  # noqa: E402
    EMBED_DIM,
    embed_text,
    embed_texts,
    event_text,
    to_pgvector_literal,
    is_available,
    warm_up,
    backfill_event_embeddings,
)


TEST_TAG = "emb_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_events():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE properties->>'_test_tag' = $1",
            TEST_TAG,
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Pure functions ───────────────────────────────────────────────────────


def test_embed_text_returns_384_dims():
    assert is_available(), "sentence-transformers must be installed for these tests"
    v = embed_text("Iran tanker rendezvous off the coast of Venezuela")
    assert v is not None
    assert len(v) == EMBED_DIM
    assert all(isinstance(x, float) for x in v[:5])


def test_embed_text_empty_returns_none():
    assert embed_text("") is None
    assert embed_text("   ") is None
    assert embed_text(None) is None


def test_embed_text_is_deterministic():
    """Same text → same vector (model is deterministic + cache hit)."""
    a = embed_text("repeated test sentence")
    b = embed_text("repeated test sentence")
    assert a == b


def test_to_pgvector_literal_format():
    v = [0.1, -0.2, 0.3]
    s = to_pgvector_literal(v)
    assert s.startswith("[") and s.endswith("]")
    assert "0.100000" in s
    assert "-0.200000" in s
    assert to_pgvector_literal(None) is None


def test_embed_texts_batches_with_cache_mix():
    """Mix of cache-hit + cache-miss + empty entries."""
    embed_text("primer for cache hit")
    out = embed_texts([
        "primer for cache hit",          # hit
        "fresh string never seen",       # miss
        "",                              # empty → None
        "another fresh one",             # miss
    ])
    assert len(out) == 4
    assert out[0] is not None and len(out[0]) == EMBED_DIM
    assert out[1] is not None and len(out[1]) == EMBED_DIM
    assert out[2] is None
    assert out[3] is not None and len(out[3]) == EMBED_DIM


def test_warm_up_loads_model_and_is_idempotent():
    """warm_up() loads the model on first call; subsequent calls are no-ops.

    After warm_up(), is_available() must report True and embed_text() must
    succeed without paying the cold-load cost (the first HTTP request that
    used to time out at 30s should now be a fast path)."""
    assert warm_up() is True
    # Idempotent — second call short-circuits via the _model is not None gate.
    assert warm_up() is True
    assert is_available() is True
    v = embed_text("post-warmup probe")
    assert v is not None and len(v) == EMBED_DIM


def test_event_text_composes_title_and_description():
    assert event_text("hello", "world") == "hello. world"
    assert event_text("just title", None) == "just title"
    assert event_text(None, "just desc") == "just desc"
    assert event_text("", "") is None
    assert event_text(None, None) is None


# ─── Backfill against the live DB ─────────────────────────────────────────


async def _insert_test_event(*, title: str, description: str = "") -> uuid.UUID:
    """Insert a gdelt_topical row with NULL embedding and the test-tag marker."""
    eid = uuid.uuid4()
    ts = datetime.now(timezone.utc)
    import json
    props = json.dumps({"_test_tag": TEST_TAG, "external_id": f"emb_test:{eid}"})
    await execute(
        """
        INSERT INTO event
            (id, event_type, event_subtype, event_time,
             geom, severity, title, description, properties,
             domain, decay_half_life_min)
        VALUES
            ($1::uuid, 'gdelt_topical', 'test', $2,
             ST_SetSRID(ST_MakePoint(0, 0), 4326)::geography,
             5, $3, $4, $5::jsonb,
             'geo', 720)
        """,
        eid, ts, title, description, props,
    )
    return eid


async def test_backfill_populates_embeddings_for_text_events(_clean_test_events):
    """Insert NULL-embedding rows; backfill embeds them."""
    eids = []
    for i in range(3):
        eid = await _insert_test_event(
            title=f"Test breaking news {i}",
            description=f"Description body of article {i}",
        )
        eids.append(eid)

    # Confirm starting state — all NULL
    pre = await fetchval(
        "SELECT COUNT(*) FROM event WHERE properties->>'_test_tag' = $1 "
        "AND embedding IS NULL",
        TEST_TAG,
    )
    assert pre == 3

    result = await backfill_event_embeddings(batch_size=10, max_events=10)
    assert result["embedded"] >= 3

    # All test rows should now have embeddings
    post_null = await fetchval(
        "SELECT COUNT(*) FROM event WHERE properties->>'_test_tag' = $1 "
        "AND embedding IS NULL",
        TEST_TAG,
    )
    assert post_null == 0


async def test_backfill_skips_text_with_only_empty_fields(_clean_test_events):
    """A row with empty title and empty description has no text to embed; skip."""
    await _insert_test_event(title="", description="")
    result = await backfill_event_embeddings(batch_size=10, max_events=10)
    # `skipped_no_text` should account for this row
    assert result.get("skipped_no_text", 0) >= 1


async def test_backfill_idempotent_after_first_pass(_clean_test_events):
    """Run twice — second call should embed nothing new from this batch."""
    await _insert_test_event(
        title="Idempotency check article", description="Body text",
    )
    r1 = await backfill_event_embeddings(batch_size=10, max_events=10)
    r2 = await backfill_event_embeddings(batch_size=10, max_events=10)
    assert r1["embedded"] >= 1
    # r2 might still embed OTHER pre-existing un-embedded rows in the DB,
    # but our test rows should already be done. Confirm no test rows remain
    # NULL.
    leftover_null = await fetchval(
        "SELECT COUNT(*) FROM event WHERE properties->>'_test_tag' = $1 "
        "AND embedding IS NULL",
        TEST_TAG,
    )
    assert leftover_null == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
