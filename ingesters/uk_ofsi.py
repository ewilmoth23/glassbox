"""
UK OFSI consolidated sanctions list ingester.

Source: https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.xml
License: UK Crown Copyright, Open Government Licence v3.0 (commercial-OK with
attribution). See https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/
NO API key required.

The OFSI (Office of Financial Sanctions Implementation, HM Treasury)
consolidated list catalogues all targets of UK financial sanctions across
~30 active regimes (Russia, DPRK, Iran, Syria, Belarus, etc.). It includes:
  - Individuals (~14k name records, mostly aliases)
  - Entities (~6k records)
  - Ships (~80 records — all with IMO numbers)

For Glassbox v1.0 we only ingest the LOCATABLE entries (Ships). Aircraft are
not a category in the UK list (everything goes under "Entity"). Individuals
without a globe pin are out of scope for v1.0 — defer to a later
entity-resolution layer if/when needed.

Cross-domain value: UK OFSI lists Russian Sovcomflot vessels with IMO
precision under the "Russia" regime. Combined with OFAC SDN, the
sanctions_match algorithm now fires red on vessels listed by EITHER
authority (or both — a stronger signal). UK lists 81 vessels currently;
OFAC lists 1,481 — overlap is only partial.

Refresh cadence: OFSI updates intra-day after sanctions actions. We poll
hourly, matching the OFAC ingester. The XML is ~52 MB so the bandwidth
budget is real — don't drop below 30 min.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


_NS = {"ns": "http://schemas.hmtreasury.gov.uk/ofsi/consolidatedlist"}
_XSI = "{http://www.w3.org/2001/XMLSchema-instance}"


def _txt(el: Optional[ET.Element]) -> Optional[str]:
    """Extract trimmed text. Returns None for nil-tagged elements (i:nil='true')
    or empty/whitespace-only text. OFSI marks every absent field as nil rather
    than omitting the element."""
    if el is None:
        return None
    if el.attrib.get(f"{_XSI}nil") == "true":
        return None
    t = (el.text or "").strip()
    return t or None


def _compose_name(target: ET.Element) -> str:
    """OFSI splits names into name1..name6 + Name6. We concatenate non-empty
    parts in document order. (name1 is typically given name, Name6 surname,
    but vessel names use a single part — usually Name6.)"""
    parts: List[str] = []
    for tag in ("name1", "name2", "name3", "name4", "name5", "Name6"):
        v = _txt(target.find(f"ns:{tag}", _NS))
        if v:
            parts.append(v)
    return " ".join(parts).strip()


# ─── Ingester ─────────────────────────────────────────────────────────────


class UkOfsiIngester(Ingester):
    layer = "sanctions"
    source = "UK OFSI Consolidated Sanctions List"
    source_id = "uk_ofsi"
    poll_interval_sec = 3600.0   # 1h — matches OFAC cadence

    URL = (
        "https://ofsistorage.blob.core.windows.net/publishlive/"
        "2022format/ConList.xml"
    )
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=120)  # 52 MB file
        headers = {"User-Agent": self.UA, "Accept": "application/xml"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                xml_bytes = await r.read()

        # Parse from the in-memory bytes. OFSI publishes well-formed XML in
        # one document; iterparse not needed at this size.
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            self.log.warning(f"[uk_ofsi] XML parse error: {e}")
            return []

        rows: List[Dict[str, Any]] = []
        seen_groups: set = set()  # dedup by GroupID — OFSI emits one row per
                                  # alias, but ships rarely have aliases
        ship_count = 0
        with_imo = 0
        for tgt in root.findall("ns:FinancialSanctionsTarget", _NS):
            group_type = _txt(tgt.find("ns:GroupTypeDescription", _NS))
            if group_type != "Ship":
                continue
            ship_count += 1

            grp_status = _txt(tgt.find("ns:GrpStatus", _NS))
            if grp_status and grp_status.upper() != "A":
                # 'A' = Active. Anything else (R = removed, etc.) we skip.
                continue

            group_id = _txt(tgt.find("ns:GroupID", _NS))
            if not group_id:
                continue
            if group_id in seen_groups:
                continue
            seen_groups.add(group_id)

            imo_raw = _txt(tgt.find("ns:Ship_IMONumber", _NS))
            imo_int: Optional[int] = None
            if imo_raw:
                digits = "".join(ch for ch in imo_raw if ch.isdigit())
                if digits:
                    try:
                        imo_int = int(digits)
                        with_imo += 1
                    except ValueError:
                        imo_int = None

            display_name = _compose_name(tgt)
            regime = _txt(tgt.find("ns:RegimeName", _NS)) or ""
            flag = _txt(tgt.find("ns:Ship_Flag", _NS))
            ship_type = _txt(tgt.find("ns:Ship_Type", _NS))
            uk_ref = _txt(tgt.find("ns:UKSanctionsListRef", _NS))

            rows.append({
                "id":            group_id,
                "type":          "vessel",
                "display_name":  display_name or f"UK OFSI {uk_ref or group_id}",
                "imo":           imo_int,
                "regime":        regime,
                "flag":          flag,
                "ship_type":     ship_type,
                "uk_ref":        uk_ref,
            })

        self.log.info(
            f"[uk_ofsi] parsed {ship_count} ship records; {with_imo} with IMO; "
            f"{len(rows)} active emitted"
        )
        return rows

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """Emit one sanctions-index event per active UK-listed ship.

        Mirrors the OFAC ingester pattern: kind='index', sentinel coords (0,0).
        The sanctions_match algorithm joins these against live AIS feeds via
        IMO when available, falling back to fuzzy name match. The frontend
        treats kind='index' events as non-pin matching-index entries.
        """
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
                "_attribution":          "Sanctions: UK OFSI (Crown Copyright, OGL v3.0)",
                "sanctioning_authority": "UK OFSI",
                "canonical_id_type":     "uk_ofsi_id",
                "regime":                r.get("regime"),
            }
            if r.get("imo") is not None:
                payload["imo"] = r["imo"]
            if r.get("flag"):
                payload["flag"] = r["flag"]
            if r.get("ship_type"):
                payload["ship_type"] = r["ship_type"]
            if r.get("uk_ref"):
                payload["uk_ref"] = r["uk_ref"]

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"uk_ofsi:vessel:{ext_id}",
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
