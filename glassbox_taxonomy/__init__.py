# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
#
# CAMEO event-code mapping. The CAMEO codebook itself is a freely-usable
# coding scheme published by the Penn State Event Data Project. This
# mapping into Glassbox's semantic taxonomy is original work.
"""
Glassbox semantic taxonomy.

Maps CAMEO event codes (the standard used by GDELT, ICEWS, Phoenix) into
Glassbox's internal category/subcategory shape with severity, Goldstein,
and contextual flags. Used by the GDELT bulk ingester (Phase 4.A) to
turn cryptic 3-4 digit event codes into queryable, UI-friendly events.

Public surface: ``CAMEOLookup`` and ``CAMEOEntry``.
"""

from .lookup import CAMEOEntry, CAMEOLookup

__all__ = ["CAMEOEntry", "CAMEOLookup"]
