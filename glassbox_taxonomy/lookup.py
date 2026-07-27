# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
#
# CAMEO event-code mapping. The CAMEO codebook itself is a freely-usable
# coding scheme published by the Penn State Event Data Project. This
# mapping into Glassbox's semantic taxonomy is original work.
"""
CAMEOLookup — thread-safe, immutable in-process lookup over the CAMEO
event-code → Glassbox-taxonomy mapping checked into ``data/cameo_lookup.json``.

Loaded once at startup; lookups are O(1) on a dict. ``by_code()`` falls back
to parent codes (4-digit → 3-digit → 2-digit) so the lookup is robust to
GDELT-extended codes that are not in the CAMEO 1.1b3 base codebook.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


_DEFAULT_JSON_PATH = Path(__file__).parent / "data" / "cameo_lookup.json"


class CAMEOEntry(BaseModel):
    """One CAMEO code mapped to Glassbox's semantic taxonomy.

    All fields are required. ``severity`` is in [0.0, 1.0]; ``goldstein``
    follows CAMEO's published scale of [-10.0, +10.0].
    """

    code: str = Field(..., min_length=2, max_length=4)
    name: str
    category: str
    subcategory: str
    label: str
    goldstein: float = Field(..., ge=-10.0, le=10.0)
    severity: float = Field(..., ge=0.0, le=1.0)
    flags: List[str] = Field(default_factory=list)


class CAMEOLookup:
    """Thread-safe, immutable in-process CAMEO lookup.

    Loaded once at startup. Lookups are O(1) on the primary index; parent-code
    fallback is at most 3 dict probes (4-digit → 3-digit → 2-digit).

    Args:
        json_path: Optional override for the JSON data path. Defaults to the
            ``cameo_lookup.json`` shipped alongside this module.
    """

    def __init__(self, json_path: Optional[str | Path] = None) -> None:
        path = Path(json_path) if json_path else _DEFAULT_JSON_PATH
        with path.open("r", encoding="utf-8") as f:
            doc = json.load(f)

        self._version: str = doc.get("version", "0.0")
        self._source_codebook: str = doc.get("source_codebook", "")

        self._entries: Dict[str, CAMEOEntry] = {}
        for raw in doc["entries"]:
            entry = CAMEOEntry(**raw)
            self._entries[entry.code] = entry

        # Reverse index: subcategory → list of entries.
        self._by_subcategory: Dict[str, List[CAMEOEntry]] = {}
        for entry in self._entries.values():
            self._by_subcategory.setdefault(entry.subcategory, []).append(entry)

        self._categories = sorted({e.category for e in self._entries.values()})
        self._subcategories = sorted(self._by_subcategory.keys())

    # ─── Public API ──────────────────────────────────────────────────────

    @property
    def version(self) -> str:
        return self._version

    @property
    def source_codebook(self) -> str:
        return self._source_codebook

    def by_code(self, code: str) -> Optional[CAMEOEntry]:
        """Direct lookup. Falls back to parent code if exact not found.

        ``'01234'`` → ``'0123'`` → ``'012'`` → ``'01'``. Returns ``None`` only
        if no ancestor (down to the 2-digit root) is in the table.
        """
        if not code:
            return None
        candidate = code.strip()
        while len(candidate) >= 2:
            hit = self._entries.get(candidate)
            if hit is not None:
                return hit
            candidate = candidate[:-1]
        return None

    def by_subcategory(self, subcategory: str) -> List[CAMEOEntry]:
        """All CAMEO codes mapped to a Glassbox subcategory. Empty list if
        the subcategory is not in the taxonomy."""
        return list(self._by_subcategory.get(subcategory, ()))

    def all_subcategories(self) -> List[str]:
        """Sorted, unique list of every subcategory used."""
        return list(self._subcategories)

    def all_categories(self) -> List[str]:
        """Sorted, unique list of every top-level category used."""
        return list(self._categories)

    def __len__(self) -> int:
        return len(self._entries)
