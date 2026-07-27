# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GDELT downloader unit tests — pure parsing / unzipping. No live network
contact (network-gated tests would belong to a separate live_* module
that's collected only with an opt-in env var).
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.gdelt_bulk.downloader import (  # noqa: E402
    LastUpdate,
    extract_csv_from_zip,
    parse_lastupdate,
)


# ─── parse_lastupdate ────────────────────────────────────────────────────


def test_parse_lastupdate_three_known_kinds():
    body = (
        "1234567 abcdef http://data.gdeltproject.org/gdeltv2/20260510120000.export.CSV.zip\n"
        "987654  fedcba http://data.gdeltproject.org/gdeltv2/20260510120000.mentions.CSV.zip\n"
        "456789  123456 http://data.gdeltproject.org/gdeltv2/20260510120000.gkg.csv.zip\n"
    )
    out = parse_lastupdate(body)
    assert out.export is not None
    assert out.export.size_bytes == 1234567
    assert out.export.url.endswith(".export.CSV.zip")
    assert out.mentions is not None
    assert out.gkg is not None


def test_parse_lastupdate_handles_blank_and_unknown_lines():
    body = (
        "\n"
        "1234567 abcdef http://example.com/2026.export.CSV.zip\n"
        "junk row that is shorter\n"
        "777 hash http://example.com/2026.future.CSV.zip\n"   # unknown kind
        "\n"
    )
    out = parse_lastupdate(body)
    assert out.export is not None
    assert out.mentions is None
    assert out.gkg is None


def test_parse_lastupdate_skips_non_integer_size():
    body = "abc def http://example.com/x.export.CSV.zip\n"
    out = parse_lastupdate(body)
    assert out.export is None


def test_parse_lastupdate_empty_input():
    out = parse_lastupdate("")
    assert isinstance(out, LastUpdate)
    assert out.export is None and out.mentions is None and out.gkg is None


# ─── extract_csv_from_zip ────────────────────────────────────────────────


def _make_zip(filename: str, body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, body)
    return buf.getvalue()


def test_extract_csv_round_trip():
    zip_bytes = _make_zip("20260510120000.export.CSV", "row1\trow1\nrow2\trow2\n")
    text = extract_csv_from_zip(zip_bytes)
    assert "row1\trow1" in text


def test_extract_csv_handles_bad_utf8_with_replace():
    """A single bad byte (e.g. legacy windows-1252 in older articles)
    must NOT raise — decoding errors fall through with replacement."""
    # Build a zip whose CSV body has an invalid UTF-8 sequence
    bad_bytes = b"valid prefix \x80 valid suffix"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("x.export.CSV", bad_bytes)
    text = extract_csv_from_zip(buf.getvalue())
    assert "valid prefix" in text
    assert "valid suffix" in text


def test_extract_csv_picks_csv_member_when_multiple_files():
    """Defensive against a zip with extra members (e.g. README); pick
    the .csv one."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", "ignore me")
        zf.writestr("20260510.export.csv", "real csv body")
    text = extract_csv_from_zip(buf.getvalue())
    assert text == "real csv body"


def test_extract_csv_raises_on_empty_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED):
        pass
    with pytest.raises(ValueError, match="empty"):
        extract_csv_from_zip(buf.getvalue())
