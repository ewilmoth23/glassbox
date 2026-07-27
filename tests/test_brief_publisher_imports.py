"""
Regression test for `glassbox_server._brief_publisher_loop` deferred
imports.

CONTEXT
-------
The brief-publisher loop runs once per hour, generating an empire-wide
brief by calling `query_viewport` and rendering markdown. It uses a
deferred (inside-function) import because the brief publisher is only
needed when the daemon is up — module load shouldn't pay the cost of
importing the viewport handler graph.

During the P3-H Phase 2 refactor (commit `e4b63c8`, 2026-05-27),
`query_viewport` moved from `api_v1.py` to `web/routes/api_v1/core.py`.
The brief-publisher loop's deferred import was not updated; on daemon
boot the asyncio task crashed with:

    ImportError: cannot import name 'query_viewport' from 'api_v1'

The rest of the server kept running so the regression was silent —
the brief just stopped publishing. Fixed in the same commit as this
test.

WHAT THIS TEST DOES
-------------------
1. Resolves the deferred import path the function actually uses
   (`web.routes.api_v1.core.query_viewport`) AT MODULE LOAD TIME so
   any future api_v1 split that breaks it surfaces immediately in CI
   instead of at runtime after deploy.

2. Inspects the function's source to assert the production code
   actually uses the path this test pins. A drift between this test's
   imports and the function body's deferred imports would silently
   re-open the regression — the source-scan check catches that.

If glassbox_server's deferred imports are ever lifted to module
level, this test can be deleted (the static import would fail at
module load and that'd be the real check). Until then, this test is
the safety net.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_brief_publisher_deferred_query_viewport_resolves():
    """The exact import the function uses must resolve at test time —
    catches the api_v1 -> web.routes.api_v1.core move that previously
    silently broke brief publishing."""
    # If this line raises ImportError, the brief-publisher loop will
    # crash at first invocation. Catching it here means the test
    # fails BEFORE the regression ships.
    from web.routes.api_v1.core import query_viewport  # noqa: F401
    assert callable(query_viewport), (
        "web.routes.api_v1.core.query_viewport must be callable; "
        "if it became a coroutine that's fine — inspect.iscoroutinefunction "
        "is checked elsewhere."
    )


def test_brief_publisher_source_uses_pinned_import_path():
    """The function body's deferred import line must match the path
    this test pins. A drift between the two (someone reverts to
    `from api_v1 import query_viewport` without updating the test)
    is exactly the bug class we're guarding against, so we assert on
    the source text directly."""
    import glassbox_server
    src = inspect.getsource(glassbox_server._brief_publisher_loop)

    # Must use the post-Phase-2 path. Allow any whitespace around the
    # `from ... import ...` to be tolerant of formatter changes.
    pinned = re.compile(
        r"from\s+web\.routes\.api_v1\.core\s+import\s+query_viewport",
        re.MULTILINE,
    )
    assert pinned.search(src), (
        "_brief_publisher_loop no longer imports `query_viewport` from "
        "`web.routes.api_v1.core`. Either: (1) the import path moved "
        "again — update both the function body AND this test's pinned "
        "regex, or (2) someone reverted to the stale "
        "`from api_v1 import query_viewport` which will crash the loop "
        "at runtime."
    )

    # And the stale path MUST NOT be present.
    stale = re.compile(
        r"from\s+api_v1\s+import\s+.*\bquery_viewport\b",
        re.MULTILINE,
    )
    assert not stale.search(src), (
        "_brief_publisher_loop is back to `from api_v1 import "
        "query_viewport`. That path was removed in P3-H Phase 2 "
        "(commit e4b63c8) and crashes the brief loop on first tick. "
        "Use `from web.routes.api_v1.core import query_viewport` instead."
    )


def test_all_deferred_api_v1_imports_in_glassbox_server_resolve():
    """Audit every deferred `from api_v1 import …` / `from web.routes.api_v1…
    import …` line inside glassbox_server.py and confirm each resolves.

    Catches the broader bug class: any future api_v1 refactor that
    moves a symbol the daemon's deferred-imports rely on. Module-level
    imports already fail loudly at startup; only deferred ones risk
    silent regression.

    Note: this test reads the source text and dynamically imports —
    deliberately heavy-weight, but it's the only way to exercise a
    deferred import without instantiating the coroutine.
    """
    import glassbox_server
    src = Path(glassbox_server.__file__).read_text(encoding="utf-8")

    # Match INDENTED imports — the indentation flags them as inside-function.
    # Match BOTH `from api_v1 import ...` and `from web.routes.api_v1...
    # import ...` because both forms are valid post-refactor.
    deferred_re = re.compile(
        r"^[ \t]+from\s+(api_v1|web\.routes\.api_v1(?:\.[A-Za-z_0-9]+)*)\s+import\s+([A-Za-z_0-9, \t]+)",
        re.MULTILINE,
    )
    found = deferred_re.findall(src)
    assert found, (
        "Sanity check: at least one deferred `from api_v1...` import "
        "should exist in glassbox_server.py (the _brief_publisher_loop "
        "deferred import). If none, this audit test is no longer "
        "load-bearing and can be deleted."
    )

    for module_path, names_blob in found:
        # Names blob is comma-separated; strip whitespace + trailing comment.
        names = [
            n.strip().split()[0]
            for n in names_blob.split(",")
            if n.strip()
        ]
        for name in names:
            try:
                mod = __import__(module_path, fromlist=[name])
            except ImportError as e:
                pytest.fail(
                    f"Deferred import in glassbox_server.py refers to a "
                    f"module that no longer exists: `from {module_path} "
                    f"import {name}` -> {type(e).__name__}: {e}. "
                    f"Find the line in glassbox_server.py and update "
                    f"the import path to where {name} now lives."
                )
            if not hasattr(mod, name):
                pytest.fail(
                    f"Deferred import in glassbox_server.py: "
                    f"`from {module_path} import {name}` — module imports "
                    f"OK but the name `{name}` is missing. The symbol "
                    f"likely moved during a refactor; update the deferred "
                    f"import path."
                )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
