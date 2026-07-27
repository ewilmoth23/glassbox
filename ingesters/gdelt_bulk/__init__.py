# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GDELT bulk-CSV ingester — Phase 4.A.

The ``prefilter`` subpackage is the chokepoint between raw GDELT events
(~250K/day) and downstream LLM extraction (~1.5K/day budget). Built per
HANDOFF_03 in 00_MASTER_DOCS/research_2026_05_09/.

The actual ``gdelt_bulk.py`` ingester (polling
data.gdeltproject.org/gdeltv2/lastupdate.txt every 5 min) is the
remaining R1 sub-build.
"""
