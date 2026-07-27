"""
Static-scan regression test — prevents the slow
`properties->'entity_ids' ? $UUID::text` form from re-entering production.

History:
  On 2026-05-21 the cross_domain endpoint was clocked at 220s wall on a
  single entity. EXPLAIN showed the planner scanning 13.7M rows because
  no index could be used for the `?`-on-extracted form. Switching to
  `@> jsonb_build_object('entity_ids', jsonb_build_array($1::text))`
  engaged the EXISTING `event_props_gin` and dropped the query to
  5-30ms — ~7,000× faster.

  The same bad pattern existed in 3 algorithm files (proximity,
  rendezvous, sanctioned_rendezvous) where it was used for finding
  dedup. The same rewrite was applied to all 7 instances.

This test scans every production .py file in 21_GLASSBOX_AI/ (excluding
.venv, tests/, _versions/, _archive/) for any new instance of the bad
pattern. Comments are stripped so the explanatory historical references
in the rewritten code don't trip the scanner.

If a future commit accidentally re-introduces `?`-on-extracted (e.g. via
copy-paste from an older version), this test fails the same day.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# The bad pattern is `properties -> '<key>' ? <something>::text` — extracting
# a sub-document FIRST and then asking for key/element existence on it.
# Postgres GIN can't accelerate this form against the standard
# event_props_gin (which indexes the whole `properties`).
#
# This regex matches:
#   `properties -> 'KEY' ? VALUE`  (with optional whitespace anywhere)
#
# The fix shape (`@>`) reads:
#   `properties @> jsonb_build_object('KEY', jsonb_build_array(VALUE))`
BAD_PATTERN = re.compile(
    r"properties\s*->\s*'[a-zA-Z_]+'\s*\?\s*",
    re.MULTILINE,
)


def _strip_python_and_sql_comments(src: str) -> str:
    """Strip lines that are pure Python comments (`# ...`) OR pure SQL
    comments (`-- ...`). The SQL strip is needed because the bad pattern
    can legitimately appear inside an explanatory `-- ...` line within a
    triple-quoted SQL string, as a historical "this is what we DON'T do"
    reference.

    Doesn't try to handle # or -- inside string literals on a code line —
    for our use case (looking for production SQL patterns), the bad
    pattern would never legitimately appear inside a `#`-leading or
    `--`-leading line as runnable code, so a conservative line-based
    strip is enough."""
    return "\n".join(
        line for line in src.splitlines()
        if not re.match(r'^\s*(#|--)', line)
    )


def _production_python_files():
    """Iterate every production .py under 21_GLASSBOX_AI/, excluding
    test fixtures + vendored deps + archived snapshots."""
    excluded_dirs = {".venv", "tests", "_versions", "_archive",
                     "node_modules", "scripts"}
    for p in ROOT.rglob("*.py"):
        if any(part in excluded_dirs for part in p.parts):
            continue
        yield p


def test_no_unindexable_jsonb_question_mark_pattern_in_production_code():
    """Scan every production .py file. If `properties->'KEY' ? VALUE` appears
    in CODE (not just comments), fail with the file:line list so the author
    knows exactly what to rewrite.

    Catches:
      - properties->'entity_ids' ? <uuid>::text       (the 2026-05-21 regression)
      - properties->'event_ids' ? <uuid>::text        (same shape, same problem)
      - properties->'anything_else' ? <whatever>      (any future variant)

    Doesn't catch:
      - `properties ? '<key>'`  — that's key-existence on the top-level
                                   `properties` jsonb, which DOES use
                                   event_props_gin via jsonb_ops. Safe.
      - `properties @> jsonb_build_object(...)` — the FIX shape. Safe.

    To extend this test for a new jsonb key: nothing — the regex catches
    any extracted-key form regardless of the key name.
    """
    hits = []
    for p in _production_python_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        code_only = _strip_python_and_sql_comments(text)
        for m in BAD_PATTERN.finditer(code_only):
            literal = m.group(0)
            try:
                idx = text.index(literal)
                line_no = text[:idx].count('\n') + 1
            except ValueError:
                line_no = "?"
            rel = p.relative_to(ROOT.parent)
            hits.append(f"  {rel}:{line_no}  {literal.strip()}")

    assert not hits, (
        "Unindexable `properties->'KEY' ? VALUE` pattern detected in "
        "production code. This form can't use event_props_gin and was "
        "clocked at 220s on a 30M-row hypertable in the 2026-05-21 audit.\n"
        "Use the containment form instead:\n"
        "  properties @> jsonb_build_object('KEY', jsonb_build_array(VALUE))\n"
        "Offending sites:\n" + "\n".join(hits)
    )
