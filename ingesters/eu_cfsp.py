"""
EU Common Foreign and Security Policy (CFSP) consolidated sanctions ingester.

Source:
  https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=<TOKEN>
License: EU Open Data — free for commercial use with attribution.
        ("EU sanctions: European Commission FSF")
Auth: a hash token issued by the EU Financial Sanctions Database (FSD/FSF).
      The well-known public token "token-2017" (base64: 'dG9rZW4tMjAxNw') is
      baked into the EU FSF download page and works without registration.
      Operators can override via the EU_FSF_TOKEN environment variable to
      use their own token.

The EU consolidated list catalogues all targets of EU restrictive measures
across ~30+ regimes (Russia/Ukraine, DPRK, Iran, Syria, Belarus, Myanmar,
Venezuela, etc.). Schema:

  <export>
    <sanctionEntity logicalId="N" euReferenceNumber="EU.X.Y">
      <subjectType code="enterprise|person" .../>
      <nameAlias wholeName="..." .../>          (one per spelling/translation)
      <identification identificationTypeCode="imo" number="..."/>
      <regulation programme="UKR|PRK|RUS|..." entryIntoForceDate="..."/>
      <birthdate ...>                            (persons only)
      <address ...>                              (where present)
    </sanctionEntity>
  </export>

For Glassbox v1.0 we only ingest enterprises with an IMO identification —
those are the locatable vessels. ~35 vessels listed currently (17 DPRK +
18 Ukraine/Russia). EU is highest-leverage for Russian Sovcomflot tankers
that are missing or differently-listed in OFAC SDN.
"""

from __future__ import annotations

import asyncio
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


_NS = {"ns": "http://eu.europa.ec/fpi/fsd/export"}

# On-disk fallback cache. EU's webgate server has been observed flapping with
# sustained 500 outages (12+ hours). The list itself only changes ~weekly, so
# replaying a recent good XML is preferable to serving zero entities. After
# _CACHE_MAX_AGE_SEC the cache is refused — we'd rather alert loud than mask
# a sustained EU outage that lets sanctions data drift.
_DEFAULT_CACHE_ROOT = "/Volumes/Mac Mini Expanded Storage/ewilmoth/glassbox-cache"
_CACHE_DIR = Path(os.environ.get("GLASSBOX_CACHE_DIR", _DEFAULT_CACHE_ROOT)) / "eu_cfsp"
_CACHE_FILE = _CACHE_DIR / "last_good.xml"
_CACHE_MAX_AGE_SEC = 7 * 24 * 3600  # 7 days


def _txt(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    t = (el.text or "").strip()
    return t or None


def _primary_name(entity: ET.Element) -> str:
    """EU emits one <nameAlias> per spelling/translation. Prefer one
    flagged Latin-only (most common stable form), then any non-empty
    wholeName, fall back to a synthesized name."""
    aliases = entity.findall("ns:nameAlias", _NS)
    if not aliases:
        return ""
    # Latin script tends to be the stable canonical form
    for a in aliases:
        if (a.attrib.get("nameLanguage") or "").lower() in ("en", "fr", "de"):
            wn = (a.attrib.get("wholeName") or "").strip()
            if wn:
                return wn
    for a in aliases:
        wn = (a.attrib.get("wholeName") or "").strip()
        if wn:
            return wn
    return ""


def _imo_for_entity(entity: ET.Element) -> Optional[int]:
    """Return the entity's IMO as int (digits only) if any identification
    element marks identificationTypeCode='imo'. Returns None otherwise."""
    for idd in entity.findall("ns:identification", _NS):
        if (idd.attrib.get("identificationTypeCode") or "").lower() != "imo":
            continue
        raw = idd.attrib.get("number") or idd.attrib.get("latinNumber") or ""
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    return None


def _programme(entity: ET.Element) -> Optional[str]:
    reg = entity.find("ns:regulation", _NS)
    if reg is None:
        return None
    return reg.attrib.get("programme") or None


# Map the EU's short programme codes to readable regime names.
_PROGRAMME_LABELS = {
    "UKR": "Russia/Ukraine",
    "RUS": "Russia",
    "PRK": "Democratic People's Republic of Korea",
    "IRA": "Iran",
    "SYR": "Syria",
    "BLR": "Belarus",
    "MYA": "Myanmar",
    "VEN": "Venezuela",
    "LBY": "Libya",
    "AFG": "Afghanistan",
    "ZWE": "Zimbabwe",
    "NIC": "Nicaragua",
    "TUR": "Turkey",
    "TUN": "Tunisia",
    "EGY": "Egypt",
    "IRQ": "Iraq",
}


# ─── Ingester ─────────────────────────────────────────────────────────────


class EuCfspIngester(Ingester):
    layer = "sanctions"
    source = "EU CFSP Consolidated Sanctions List"
    source_id = "eu_cfsp"
    poll_interval_sec = 3600.0   # 1h — matches OFAC + UK cadence

    BASE_URL = (
        "https://webgate.ec.europa.eu/fsd/fsf/public/files/"
        "xmlFullSanctionsList_1_1/content"
    )
    # Public token baked into the EU FSF download page (base64 'token-2017').
    # Operators can override via EU_FSF_TOKEN to use their own token.
    DEFAULT_TOKEN = "dG9rZW4tMjAxNw"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    def _url(self) -> str:
        token = os.environ.get("EU_FSF_TOKEN") or self.DEFAULT_TOKEN
        return f"{self.BASE_URL}?token={token}"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=180)  # 24 MB download
        headers = {"User-Agent": self.UA, "Accept": "application/xml"}
        xml_bytes: Optional[bytes] = None
        served_from_cache = False
        cache_age_hours: Optional[float] = None
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(self._url()) as r:
                    r.raise_for_status()
                    xml_bytes = await r.read()
            self._refresh_cache(xml_bytes)
        except (aiohttp.ClientResponseError,
                aiohttp.ClientConnectorError,
                asyncio.TimeoutError) as upstream_err:
            # 4xx is auth / URL change — surface it instead of masking with cache.
            if isinstance(upstream_err, aiohttp.ClientResponseError) and upstream_err.status < 500:
                raise
            cached = self._read_cache_if_fresh()
            if cached is None:
                raise
            xml_bytes, cache_age_hours = cached
            served_from_cache = True
            self.log.warning(
                f"[eu_cfsp] upstream {type(upstream_err).__name__}: {upstream_err}; "
                f"serving cached XML ({cache_age_hours:.1f}h old)"
            )

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            self.log.warning(f"[eu_cfsp] XML parse error: {e}")
            return []

        rows: List[Dict[str, Any]] = []
        seen_ids: set = set()
        enterprise_count = 0
        with_imo = 0

        for ent in root.findall("ns:sanctionEntity", _NS):
            sj = ent.find("ns:subjectType", _NS)
            if sj is None:
                continue
            sj_code = (sj.attrib.get("code") or "").lower()
            # Vessels in EU schema are filed as enterprises; persons are skipped.
            if sj_code != "enterprise":
                continue
            enterprise_count += 1

            imo = _imo_for_entity(ent)
            if imo is None:
                # Enterprises without an IMO are companies, not vessels.
                # Skip — same v1.0 scope rule as OFAC + UK ingesters.
                continue
            with_imo += 1

            logical_id = ent.attrib.get("logicalId")
            if not logical_id:
                continue
            if logical_id in seen_ids:
                continue
            seen_ids.add(logical_id)

            programme = _programme(ent)
            regime = _PROGRAMME_LABELS.get(programme or "", programme or "")
            eu_ref = ent.attrib.get("euReferenceNumber")
            display = _primary_name(ent) or f"EU CFSP {eu_ref or logical_id}"

            row: Dict[str, Any] = {
                "id":           logical_id,
                "type":         "vessel",
                "display_name": display,
                "imo":          imo,
                "regime":       regime,
                "programme":    programme,
                "eu_ref":       eu_ref,
            }
            if served_from_cache:
                row["served_from_cache"] = True
                row["cache_age_hours"] = round(cache_age_hours, 1)
            rows.append(row)

        suffix = (f" (from cache, {cache_age_hours:.1f}h old)"
                  if served_from_cache else "")
        self.log.info(
            f"[eu_cfsp] parsed {enterprise_count} enterprises; "
            f"{with_imo} with IMO; {len(rows)} active vessels emitted{suffix}"
        )
        return rows

    # ─── on-disk cache helpers ────────────────────────────────────────────

    def _refresh_cache(self, xml_bytes: bytes) -> None:
        """Atomically replace the on-disk cache with this fresh XML.

        Errors are logged but never raised — a cache write failure must not
        break a successful upstream fetch.
        """
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _CACHE_FILE.with_suffix(".xml.tmp")
            tmp.write_bytes(xml_bytes)
            tmp.replace(_CACHE_FILE)
        except OSError as e:
            self.log.warning(f"[eu_cfsp] cache write failed (non-fatal): {e}")

    def _read_cache_if_fresh(self) -> Optional[tuple]:
        """Return (xml_bytes, age_hours) if cache exists and is < 7 days old.
        Returns None if no cache exists, the cache is too stale to trust, or
        a read error occurs (treat as no cache)."""
        if not _CACHE_FILE.exists():
            return None
        try:
            age_sec = time.time() - _CACHE_FILE.stat().st_mtime
        except OSError:
            return None
        if age_sec > _CACHE_MAX_AGE_SEC:
            self.log.error(
                f"[eu_cfsp] cache is {age_sec/3600:.1f}h old, > "
                f"{_CACHE_MAX_AGE_SEC/3600:.0f}h limit — refusing stale data"
            )
            return None
        try:
            return _CACHE_FILE.read_bytes(), age_sec / 3600
        except OSError as e:
            self.log.warning(f"[eu_cfsp] cache read failed: {e}")
            return None

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """Mirrors OFAC + UK OFSI: kind='index' sentinel events that join
        against live AIS via IMO. canonical_id_type='eu_cfsp_id' so EU
        rows live alongside OFAC + UK without collision."""
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for r in raw_items:
            ext_id = r.get("id")
            if not ext_id:
                continue

            payload: Dict[str, Any] = {
                "type":                  "vessel",
                "display_name":          r.get("display_name"),
                "fcra_safe":             False,
                "_attribution":          "EU sanctions: European Commission FSF",
                "sanctioning_authority": "EU CFSP",
                "canonical_id_type":     "eu_cfsp_id",
                "regime":                r.get("regime"),
            }
            if r.get("imo") is not None:
                payload["imo"] = r["imo"]
            if r.get("programme"):
                payload["programme"] = r["programme"]
            if r.get("eu_ref"):
                payload["eu_ref"] = r["eu_ref"]
            if r.get("served_from_cache"):
                payload["served_from_cache"] = True
                payload["cache_age_hours"] = r.get("cache_age_hours")

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"eu_cfsp:vessel:{ext_id}",
                kind="index",
                lat=0.0,
                lng=0.0,
                ts=now,
                severity=10,
                source=self.source,
                payload=payload,
                domain="entity",
                geocode_quality="needs_match",
                decay_half_life_min=10080,   # 1 week
                market_tags=[],
                severity_for_market=0,
            ))

        return out
