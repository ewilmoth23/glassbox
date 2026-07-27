# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GDELT Events V2 CSV → ``GDELTEventForPrefilter`` parser.

Bridges the bulk-CSV download path
(data.gdeltproject.org/gdeltv2/<timestamp>.export.CSV.zip) into the
prefilter chain (HANDOFF_03) using the CAMEO lookup (HANDOFF_02). Pure
Python, no I/O — the downloader / unzipper land separately.

GDELT Events V2 schema is documented at
http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf.
The 58-column tab-separated row layout is laid out below in
``EVENTS_V2_COLUMNS``; we read by index (not header) because GDELT
ships the CSV without a header line.

Editorial choices baked into this parser:

  * Rows without a parseable ActionGeo lat/lng are dropped at the parse
    boundary. They cannot pass the geography_filter so we save the
    downstream rule chain a useless invocation.
  * Rows without a SOURCEURL are dropped. The source_quality_filter would
    score them at unknown_domain_score (default 0.30) and they'd carry
    no provenance — better to drop at parse than to pollute.
  * GDELT ActionGeo_Type → empire ``geocode_quality`` mapping is below.
  * Severity is the CAMEO lookup's editorial severity; Goldstein is the
    CAMEO codebook value (NOT GDELT's per-row scaled GoldsteinScale —
    we keep the codebook-canonical value so prefilter rules and tests
    stay deterministic).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Iterator, Optional

from glassbox_taxonomy import CAMEOLookup
from .prefilter.config import GDELTEventForPrefilter


_log = logging.getLogger(__name__)


# Column-index constants for the V2 Events table. GDELT publishes this
# as a tab-separated CSV with no header line.
class _Col:
    GLOBALEVENTID    = 0
    SQLDATE          = 1
    Actor1Name       = 6
    Actor1CountryCode = 7
    Actor2Name       = 16
    Actor2CountryCode = 17
    EventCode        = 26
    EventRootCode    = 28
    GoldsteinScale   = 30
    AvgTone          = 34
    ActionGeo_Type        = 51
    ActionGeo_FullName    = 52
    ActionGeo_CountryCode = 53
    ActionGeo_ADM1Code    = 54
    ActionGeo_ADM2Code    = 55
    ActionGeo_Lat         = 56
    ActionGeo_Long        = 57
    DATEADDED        = 59
    SOURCEURL        = 60

    EXPECTED_MIN_FIELDS = 61


# GDELT ActionGeo_Type values, per the Events V2 codebook:
#   1 = COUNTRY, 2 = USSTATE, 3 = USCITY, 4 = WORLDCITY, 5 = WORLDSTATE
# Map to the empire's GlassboxEvent.geocode_quality vocabulary
# (exact / city / region / country / unknown).
_GEO_TYPE_TO_QUALITY = {
    "1": "country",
    "2": "region",
    "3": "city",
    "4": "city",
    "5": "region",
}


def actiongeo_type_to_quality(raw: str) -> str:
    return _GEO_TYPE_TO_QUALITY.get((raw or "").strip(), "unknown")


def _parse_dateadded(raw: str) -> Optional[datetime]:
    """GDELT DATEADDED is YYYYMMDDHHMMSS in UTC."""
    raw = (raw or "").strip()
    if len(raw) != 14 or not raw.isdigit():
        return None
    try:
        return datetime(
            int(raw[0:4]), int(raw[4:6]), int(raw[6:8]),
            int(raw[8:10]), int(raw[10:12]), int(raw[12:14]),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _safe_float(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_events_csv(
    csv_text: str,
    *,
    cameo: CAMEOLookup,
    max_rows: Optional[int] = None,
) -> Iterator[GDELTEventForPrefilter]:
    """Parse GDELT Events V2 CSV text into prefilter-input events.

    Tab-separated; rows shorter than the expected column count are
    skipped (logged at DEBUG so a single bad row doesn't spam the log).
    The CAMEO lookup populates category/subcategory/severity/goldstein/
    flags via parent-code fallback; codes that fall through end up under
    the '999' unknown bucket.
    """
    reader = csv.reader(io.StringIO(csv_text), delimiter="\t")
    yielded = 0
    skipped_short = 0
    skipped_no_geo = 0
    skipped_no_source = 0
    skipped_no_date = 0

    for row in reader:
        if max_rows is not None and yielded >= max_rows:
            break
        if len(row) < _Col.EXPECTED_MIN_FIELDS:
            skipped_short += 1
            continue

        lat = _safe_float(row[_Col.ActionGeo_Lat])
        lng = _safe_float(row[_Col.ActionGeo_Long])
        if lat is None or lng is None:
            skipped_no_geo += 1
            continue

        source_url = (row[_Col.SOURCEURL] or "").strip()
        if not source_url:
            skipped_no_source += 1
            continue

        ts = _parse_dateadded(row[_Col.DATEADDED])
        if ts is None:
            skipped_no_date += 1
            continue

        cameo_code = (row[_Col.EventCode] or "").strip()
        entry = cameo.by_code(cameo_code) or cameo.by_code("999")
        # cameo.by_code("999") is guaranteed by the taxonomy invariant.

        try:
            yield GDELTEventForPrefilter(
                event_id=row[_Col.GLOBALEVENTID].strip() or f"GDELT-{yielded}",
                timestamp=ts,
                code=cameo_code or "999",
                category=entry.category,
                subcategory=entry.subcategory,
                severity=entry.severity,
                goldstein=entry.goldstein,
                flags=list(entry.flags),
                title=(row[_Col.ActionGeo_FullName] or "").strip(),
                source_url=source_url,
                actor1_name=(row[_Col.Actor1Name] or "").strip() or None,
                actor2_name=(row[_Col.Actor2Name] or "").strip() or None,
                lat=lat,
                lng=lng,
                geocode_quality=actiongeo_type_to_quality(row[_Col.ActionGeo_Type]),
                iso_country=(row[_Col.ActionGeo_CountryCode] or "").strip() or None,
            )
            yielded += 1
        except Exception as exc:
            # Defensive: a single malformed row never aborts the stream.
            _log.debug("[gdelt_bulk] row parse failed: %s", exc)
            continue

    _log.info(
        "[gdelt_bulk] parsed %d events; skipped %d short, %d no-geo, "
        "%d no-source, %d bad-date",
        yielded, skipped_short, skipped_no_geo, skipped_no_source,
        skipped_no_date,
    )
