"""Sanctions queries — `/sanctions/*` (extraction #2 of P3-H Phase 2).

Three read handlers, all delegating to a single `fetch_read` against
the `entity` table (filtered to `sanctioned_vessel` + `sanctioned_aircraft`):

  - GET /sanctions/breakdown — counts per authority + regime (~50ms on
    the ~1.5k sanctioned-entity corpus). Powers the regime-distribution UI.
  - GET /sanctions/search    — substring/trigram/IMO match across the 3
    authorities (OFAC + EU + UK). Tier-ordered: IMO exact, then trigram
    by similarity desc, then ILIKE fallback.
  - GET /sanctions/by-regime — list every entity tagged with a given
    regime (case-insensitive).

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.

`coerce_jsonb` is imported from `web/_jsonb.py` (lifted in commit
`<this-commit>` as the P3-H Phase 2 #7 prep for the core extraction —
core is its biggest consumer). Kept under the underscore-prefixed
local alias `_coerce_jsonb` only to minimize diff vs the pre-lift
extraction commit; future cleanup can drop the alias.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Query

from db import fetch_read

from web._jsonb import coerce_jsonb as _coerce_jsonb

router = APIRouter()


@router.get("/sanctions/breakdown")
async def sanctions_breakdown() -> Dict[str, Any]:
    """Aggregated counts per (canonical_id_type, regime). One call
    powers the regime-distribution UI + lets operators see at a
    glance what authorities have loaded what programs.

    Total ~1.5k rows in the sanctioned_* entity types — single index
    scan, returns in <50ms even with no caching.
    """
    rows = await fetch_read(
        """
        SELECT
            entity_type,
            canonical_id_type,
            properties->>'sanctioning_authority' AS authority,
            properties->>'regime'                AS regime,
            COUNT(*)                              AS n
        FROM entity
        WHERE entity_type IN ('sanctioned_vessel', 'sanctioned_aircraft')
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2, n DESC
        """
    )
    # Reshape into a nested structure that's friendly to the UI:
    #   { totals: {vessels: N, aircraft: N},
    #     by_authority: { 'US Treasury OFAC': {vessels: N, regimes: [...]}}}
    by_authority: Dict[str, Dict[str, Any]] = {}
    totals = {"sanctioned_vessel": 0, "sanctioned_aircraft": 0}
    for r in rows:
        totals[r["entity_type"]] = totals.get(r["entity_type"], 0) + r["n"]
        auth = r["authority"] or "Unknown"
        slot = by_authority.setdefault(auth, {
            "canonical_id_type": r["canonical_id_type"],
            "totals": {"sanctioned_vessel": 0, "sanctioned_aircraft": 0},
            "regimes": [],
        })
        slot["totals"][r["entity_type"]] += r["n"]
        slot["regimes"].append({
            "entity_type": r["entity_type"],
            "regime": r["regime"],
            "n": r["n"],
        })
    # Stable ordering: largest authority first, regimes within sorted by n desc.
    return {
        "totals": {
            "vessels": totals["sanctioned_vessel"],
            "aircraft": totals["sanctioned_aircraft"],
        },
        "authorities": [
            {"authority": a, **slot}
            for a, slot in sorted(
                by_authority.items(),
                key=lambda kv: -(kv[1]["totals"]["sanctioned_vessel"]
                                + kv[1]["totals"]["sanctioned_aircraft"]),
            )
        ],
    }


@router.get("/sanctions/search")
async def sanctions_search(
    q: str = Query(..., min_length=2, max_length=80,
        description="Substring or fuzzy term — vessel/aircraft name, IMO, "
                    "or MMSI. Min 2 chars, max 80."),
    limit: int = Query(50, ge=1, le=500),
):
    """Search the sanctions index across all 3 authorities (OFAC + EU
    + UK). Three matching paths:

    1. **IMO match (precise)**: if q is all digits and 6+ characters,
       match on properties.imo exactly.
    2. **Name fuzzy (trigram)**: similarity >= 0.4 against display_name,
       sorted by similarity desc.
    3. **Name substring**: ILIKE %q% as a fallback for short queries
       that wouldn't pass the trigram threshold.

    Results ordered: exact IMO match first, then fuzzy by similarity,
    then substring matches. Each entity returns its full row +
    canonical_id_type so the front-end can build a permalink.
    """
    q_clean = q.strip()
    if not q_clean:
        return {"query": q, "count": 0, "results": []}

    is_numeric_imo = q_clean.isdigit() and len(q_clean) >= 6
    # Trigram threshold — 0.4 is loose enough to find the fuzzy matches
    # we care about ("POLA SOFIA" → "POLA SOFI" etc.) without flooding
    # for very common substrings.
    sql = """
        SELECT id, entity_type, canonical_id_type, canonical_id,
               display_name, properties, last_seen,
               CASE
                   WHEN properties->>'imo' = $1 THEN 'imo_match'
                   WHEN display_name IS NOT NULL
                        AND similarity(upper(display_name), upper($2)) >= 0.4
                   THEN 'name_fuzzy'
                   WHEN display_name ILIKE '%' || $2 || '%' THEN 'name_substring'
                   ELSE 'other'
               END AS match_kind,
               CASE WHEN display_name IS NOT NULL
                    THEN similarity(upper(display_name), upper($2))
                    ELSE 0
               END AS sim
        FROM entity
        WHERE entity_type IN ('sanctioned_vessel', 'sanctioned_aircraft')
          AND (
              ($1 IS NOT NULL AND properties->>'imo' = $1)
              OR (display_name IS NOT NULL
                  AND length($2) >= 4
                  AND upper(display_name) % upper($2)
                  AND similarity(upper(display_name), upper($2)) >= 0.4)
              OR (display_name IS NOT NULL
                  AND display_name ILIKE '%' || $2 || '%')
          )
        ORDER BY
            (CASE WHEN properties->>'imo' = $1 THEN 0 ELSE 1 END),
            sim DESC NULLS LAST,
            display_name
        LIMIT $3
    """
    rows = await fetch_read(
        sql,
        q_clean if is_numeric_imo else None,
        q_clean,
        limit,
    )
    return {
        "query": q_clean,
        "count": len(rows),
        "limit": limit,
        "results": [
            {
                "id": str(r["id"]),
                "entity_type": r["entity_type"],
                "canonical_id_type": r["canonical_id_type"],
                "canonical_id": r["canonical_id"],
                "display_name": r["display_name"],
                "match_kind": r["match_kind"],
                "similarity": float(r["sim"]) if r["sim"] is not None else None,
                "properties": _coerce_jsonb(r["properties"]),
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
            for r in rows
        ],
    }


@router.get("/sanctions/by-regime")
async def sanctions_by_regime(
    regime: str = Query(..., description="Regime code (e.g., 'RUSSIA', 'IRAN')."),
    limit: int = Query(200, ge=1, le=2000),
):
    """List every sanctioned vessel/aircraft tagged with the given
    regime. Match is case-insensitive against properties.regime so
    'russia' / 'Russia' / 'RUSSIA' all work."""
    rows = await fetch_read(
        """
        SELECT
            id, entity_type, canonical_id_type, canonical_id,
            display_name, properties, last_seen
        FROM entity
        WHERE entity_type IN ('sanctioned_vessel', 'sanctioned_aircraft')
          AND upper(properties->>'regime') = upper($1)
        ORDER BY canonical_id_type, display_name
        LIMIT $2
        """,
        regime, limit,
    )
    return {
        "regime": regime,
        "count": len(rows),
        "limit": limit,
        "entities": [
            {
                "id": str(r["id"]),
                "entity_type": r["entity_type"],
                "canonical_id_type": r["canonical_id_type"],
                "canonical_id": r["canonical_id"],
                "display_name": r["display_name"],
                "properties": _coerce_jsonb(r["properties"]),
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
            for r in rows
        ],
    }
