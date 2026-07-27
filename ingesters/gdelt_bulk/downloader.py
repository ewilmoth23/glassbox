# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GDELT bulk-CSV downloader.

GDELT V2 publishes a fresh 15-minute snapshot every 15 minutes at
data.gdeltproject.org/gdeltv2/. The ``lastupdate.txt`` index gives 3
lines (one per file kind: events / mentions / gkg), each
``<size> <md5> <url>``. We pull the events file, extract the
single CSV inside the zip, and hand the decoded text to the parser.

Pure async I/O; no parsing of the CSV body itself. Network calls are
bounded by aiohttp timeouts. Operators can override the GDELT host via
``GDELT_BULK_HOST`` if mirroring through a proxy.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from dataclasses import dataclass
from typing import Optional

import aiohttp


_log = logging.getLogger(__name__)

_DEFAULT_HOST = "http://data.gdeltproject.org"
_LASTUPDATE_PATH = "/gdeltv2/lastupdate.txt"
# Per-file timeout. The largest events.export.CSV.zip is ~3 MB; budget
# ample headroom for transient slow links without blocking the ingester
# cycle for too long.
_DOWNLOAD_TIMEOUT_SEC = 60.0


@dataclass(frozen=True)
class LastUpdateEntry:
    """One row from lastupdate.txt."""
    size_bytes: int
    md5: str
    url: str


@dataclass(frozen=True)
class LastUpdate:
    """Parsed lastupdate.txt — three entries (events, mentions, gkg)."""
    export: Optional[LastUpdateEntry]
    mentions: Optional[LastUpdateEntry]
    gkg: Optional[LastUpdateEntry]


def parse_lastupdate(text: str) -> LastUpdate:
    """Parse the 3-line lastupdate.txt format. Robust to extra blank
    lines and unrecognized rows — anything we don't recognize is
    silently ignored so a future GDELT addition (v3, etc.) doesn't
    break the ingester."""
    export: Optional[LastUpdateEntry] = None
    mentions: Optional[LastUpdateEntry] = None
    gkg: Optional[LastUpdateEntry] = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            size_bytes = int(parts[0])
        except ValueError:
            continue
        entry = LastUpdateEntry(size_bytes=size_bytes, md5=parts[1], url=parts[2])
        url_lower = entry.url.lower()
        if url_lower.endswith(".export.csv.zip"):
            export = entry
        elif url_lower.endswith(".mentions.csv.zip"):
            mentions = entry
        elif url_lower.endswith(".gkg.csv.zip"):
            gkg = entry

    return LastUpdate(export=export, mentions=mentions, gkg=gkg)


def extract_csv_from_zip(zip_bytes: bytes) -> str:
    """Return the decoded text of the single CSV inside a GDELT zip.

    GDELT ships UTF-8 with occasional encoding errors in older articles;
    decode with errors='replace' so a single bad byte doesn't drop the
    whole batch.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if not names:
            raise ValueError("GDELT zip is empty")
        # Pick the .CSV / .csv member; GDELT bundles exactly one but
        # be defensive about case + path prefixes just in case.
        csv_member = next(
            (n for n in names if n.lower().endswith(".csv")),
            names[0],
        )
        with zf.open(csv_member) as f:
            return f.read().decode("utf-8", errors="replace")


async def fetch_lastupdate(session: aiohttp.ClientSession) -> LastUpdate:
    """Pull and parse data.gdeltproject.org/gdeltv2/lastupdate.txt."""
    url = _gdelt_host() + _LASTUPDATE_PATH
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15.0)) as r:
        r.raise_for_status()
        body = await r.text()
    return parse_lastupdate(body)


async def download_export_csv(
    session: aiohttp.ClientSession, entry: LastUpdateEntry
) -> str:
    """Download a single ``*.export.CSV.zip`` and return its CSV text.

    Verifies ``Content-Length`` against the entry's expected size when
    the server provides it; mismatches log a warning but do NOT fail
    (GDELT mirrors occasionally rewrite the entry between publication
    and download).
    """
    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SEC)
    async with session.get(entry.url, timeout=timeout) as r:
        r.raise_for_status()
        actual_len = r.headers.get("Content-Length")
        zip_bytes = await r.read()
    if actual_len is not None:
        try:
            advertised = int(actual_len)
            if advertised != entry.size_bytes:
                _log.warning(
                    "[gdelt_bulk] Content-Length %d != lastupdate size %d "
                    "for %s",
                    advertised, entry.size_bytes, entry.url,
                )
        except ValueError:
            pass
    if len(zip_bytes) != entry.size_bytes:
        _log.warning(
            "[gdelt_bulk] downloaded %d bytes != lastupdate size %d for %s",
            len(zip_bytes), entry.size_bytes, entry.url,
        )
    return extract_csv_from_zip(zip_bytes)


def _gdelt_host() -> str:
    return os.environ.get("GDELT_BULK_HOST", _DEFAULT_HOST).rstrip("/")
