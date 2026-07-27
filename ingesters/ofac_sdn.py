"""
OFAC SDN ingester — US Treasury sanctions list (Specially Designated Nationals).

Source: https://www.treasury.gov/ofac/downloads/sdn_advanced.xml
License: US public domain (US Government works, 17 USC §105)
Attribution: required ("Sanctions: US Treasury OFAC" in UI footer)
NO API KEY required.

OFAC SDN includes:
  - Sanctioned individuals (with addresses, aliases, ID numbers)
  - Sanctioned entities (companies, banks, government bodies)
  - Sanctioned vessels (IMO numbers, call signs, flags)
  - Sanctioned aircraft (registration numbers)

For Glassbox v1.0 we ONLY surface the LOCATABLE entries:
  - Vessels (matched against AIS feeds → vessel-position pin lights up red)
  - Aircraft (matched against ADS-B feeds → aircraft pin lights up red)
  - Entities with known address → office building pin

Individuals without addresses are loaded into the matching index
(used by other ingesters for sanction-flagging) but do NOT generate
their own pins. This avoids creating a "people on the globe" UI which
borders on surveillance.

CRITICAL legal posture (Vector #5 in LEGAL_COMPLIANCE_REGISTRY):
  - This is a PUBLIC list, mandatory for any US person doing business.
  - We are NOT scoring/profiling — we are RE-DISPLAYING the official US gov list.
  - We DO NOT use OFAC SDN for FCRA-covered decisions (employment, credit).
  - The fcra_safe field in the GlassboxEvent payload is always false.

Refresh cadence: SDN list updates intra-day after OFAC actions.
We poll hourly (3600s) — a sanctions action may take up to an hour
to surface, which is fine for an OSINT product.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# 2026-05-05: operator-managed local cache of the OFAC SDN XML file.
# Treasury's redesigned SPA at sanctionslist.ofac.treas.gov geo/UA-blocks
# automated downloads. Operator manually downloads sdn_advanced.xml from
# the OFAC website + saves to this path. Refresh weekly or after any
# major OFAC action (sanctions news will tell you).
_LOCAL_SDN_PATH = Path(__file__).resolve().parent.parent / "data" / "sdn_advanced.xml"
# How long the local file can be before we warn (in days)
_LOCAL_SDN_STALE_DAYS = 14


# OFAC XML uses a namespaced schema — handle the common prefix
_NS = {"ns": "http://www.un.org/sanctions/1.0"}


def _strip_ns(tag: str) -> str:
    """Drop the {namespace} prefix from an XML tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


# ─── Ingester ─────────────────────────────────────────────────────────────


class OfacSdnIngester(Ingester):
    layer = "sanctions"
    source = "US Treasury OFAC SDN List"
    source_id = "ofac_sdn"               # gates against infra/sources.yaml
    poll_interval_sec = 3600.0           # 1h — SDN updates intra-day after actions

    # OFAC publishes both v2 (sdn.xml — legacy) and v3 (sdn_advanced.xml — current).
    # We use sdn_advanced.xml because v2 is being phased out.
    URL = "https://www.treasury.gov/ofac/downloads/sdn_advanced.xml"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    async def fetch(self) -> List[Dict[str, Any]]:
        """Read the SDN XML — local file first, network fallback.

        2026-05-05: Treasury moved sdn_advanced.xml + new SPA at
        sanctionslist.ofac.treas.gov returns 403 to automated downloads.
        Operator manually saves the XML to data/sdn_advanced.xml; this
        ingester reads from disk. If the file is missing (or > 14 days
        old, indicating operator forgot to refresh), we log a warning
        but still parse what's there."""
        xml_bytes: bytes = b""

        # ─── Path 1: local operator-managed file ────────────────────
        if _LOCAL_SDN_PATH.exists():
            try:
                stat = _LOCAL_SDN_PATH.stat()
                age_days = (datetime.now().timestamp() - stat.st_mtime) / 86400.0
                if age_days > _LOCAL_SDN_STALE_DAYS:
                    self.log.warning(
                        f"[ofac_sdn] local file is {age_days:.1f} days old — "
                        f"refresh recommended. Re-download from: "
                        f"https://sanctionslist.ofac.treas.gov/Home/SdnList"
                    )
                xml_bytes = _LOCAL_SDN_PATH.read_bytes()
                self.log.info(
                    f"[ofac_sdn] loaded {len(xml_bytes):,} bytes from local file "
                    f"(age: {age_days:.1f} days)"
                )
            except Exception as e:
                self.log.warning(f"[ofac_sdn] local file read failed: {e}")

        # ─── Path 2: network fallback (will likely 403/404) ──────────
        if not xml_bytes:
            self.log.info("[ofac_sdn] no local file; trying URL (likely will fail)")
            timeout = aiohttp.ClientTimeout(total=120)
            headers = {"User-Agent": self.UA, "Accept": "application/xml"}
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                    async with s.get(self.URL) as r:
                        r.raise_for_status()
                        xml_bytes = await r.read()
            except Exception as e:
                self.log.warning(
                    f"[ofac_sdn] network fetch failed: {e}. "
                    f"Place sdn_advanced.xml at {_LOCAL_SDN_PATH} to populate."
                )
                return []

        # Stream-parse; OFAC XML is large but well-structured
        rows: List[Dict[str, Any]] = []
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            self.log.warning(f"[ofac_sdn] XML parse failed: {e}")
            return []

        # Walk DistinctParty nodes — each is one sanctioned entry.
        # Schema reference (verified by reading OFAC's own ReferenceValueSets
        # in sdn_advanced.xml on 2026-05-07):
        #   PartyType   1 = Individual
        #   PartyType   2 = Entity (organization, gov body, etc.)
        #   PartyType   4 = Asset
        #   PartySubType 1 = Vessel        (PartyType=4)
        #   PartySubType 2 = Aircraft      (PartyType=4)
        #   PartySubType 3 = Entity-Unknown (PartyType=2)
        #   PartySubType 4 = Individual-Unknown (PartyType=1)
        # The previous mapping in this ingester (1=individual, 4=vessel, 5=aircraft)
        # was inverted and silently mis-categorized 7,454 individuals as vessels.
        #
        # Names: extracted from <Alias Primary="true"> by joining all
        # <NamePartValue> text in document order.
        #
        # IDs: top-level <IDRegDocuments> contains <IDRegDocument> elements
        # linked to identities by IdentityID. We index those by identity_id →
        # {doc_type_id: id_value} in the first pass, then look them up while
        # walking parties. DocTypeIDs we care about (verified 2026-05-08):
        #   1626  = Vessel Registration Identification (IMO)
        #   91264 = MMSI
        #   1623  = Aircraft Serial Identification
        #
        # Smoke mode: cap at 500 records (full file is ~19k records, ~3s parse).

        # ─── Pass 1: build identity_id → {doc_type_id: id_value} index ────
        idreg_index: Dict[str, Dict[str, str]] = {}
        for idoc in root.iter():
            if _strip_ns(idoc.tag) != "IDRegDocument":
                continue
            identity_id = idoc.attrib.get("IdentityID")
            doc_type_id = idoc.attrib.get("IDRegDocTypeID")
            if not identity_id or not doc_type_id:
                continue
            id_value = ""
            for c in idoc:
                if _strip_ns(c.tag) == "IDRegistrationNo":
                    id_value = (c.text or "").strip()
                    break
            if not id_value:
                continue
            idreg_index.setdefault(identity_id, {})[doc_type_id] = id_value

        # ─── Pass 1b: build LegalBasisID → readable program label index ───
        # OFAC chains DistinctParty → Profile → SanctionsEntry → EntryEvent →
        # LegalBasisID, and each <LegalBasis> record carries a human-readable
        # `LegalBasisShortRef` like "Executive Order 13662 (Russia)".
        legal_basis_index: Dict[str, str] = {}
        for lb in root.iter():
            if _strip_ns(lb.tag) != "LegalBasis":
                continue
            lb_id = lb.attrib.get("ID")
            short = lb.attrib.get("LegalBasisShortRef")
            if lb_id and short:
                legal_basis_index[lb_id] = short

        # ─── Pass 1c: profile_id → set of (legal_basis_short_ref, program_code)
        # SanctionsEntry has ProfileID linking back to a party's Profile;
        # each <EntryEvent> child has LegalBasisID. A party may have several
        # entries across different EOs (e.g., Russian oligarchs frequently
        # sit under both EO13662 and EO14024).
        import re as _re_mod
        # Regex pulls "(Russia)" out of "Executive Order 13662 (Russia)".
        _paren_re = _re_mod.compile(r"\(([^)]+)\)\s*$")

        def _program_code(short_ref: str) -> str:
            """Compact program code: parenthesized topic, uppercased.
            Falls back to a stripped form of the short ref when no
            parenthesized region is present."""
            if not short_ref:
                return ""
            m = _paren_re.search(short_ref)
            if m:
                return m.group(1).strip().upper()
            return short_ref.strip().upper()[:32]

        profile_programs: Dict[str, list] = {}   # profile_id → ordered, dedup'd list
        for entry in root.iter():
            if _strip_ns(entry.tag) != "SanctionsEntry":
                continue
            profile_id = entry.attrib.get("ProfileID")
            if not profile_id:
                continue
            for ev in entry:
                if _strip_ns(ev.tag) != "EntryEvent":
                    continue
                lb_id = ev.attrib.get("LegalBasisID")
                if not lb_id:
                    continue
                short = legal_basis_index.get(lb_id)
                if not short:
                    continue
                code = _program_code(short)
                # "Unknown" appears as a placeholder LegalBasis on many parties; skip it
                if code == "UNKNOWN":
                    continue
                bucket = profile_programs.setdefault(profile_id, [])
                # Maintain insertion order, dedup
                if (short, code) not in bucket:
                    bucket.append((short, code))

        type_counts: Dict[str, int] = {}   # diagnostic
        smoke_cap = 500 if self.smoke_mode else None
        parsed_count = 0
        with_imo_count = 0  # diagnostic: how many vessels have an IMO?

        for party in root.iter():
            if _strip_ns(party.tag) != "DistinctParty":
                continue
            if smoke_cap is not None and parsed_count >= smoke_cap:
                break
            parsed_count += 1

            party_id = party.attrib.get("FixedRef") or ""
            if not party_id:
                continue

            # Read Profile.PartySubTypeID (the discriminator) and Identity.ID
            # (which links to IDRegDocument records).
            profile = next((c for c in party if _strip_ns(c.tag) == "Profile"), None)
            if profile is None:
                continue
            sub_type_id = profile.attrib.get("PartySubTypeID", "")
            primary_identity_id: Optional[str] = None
            for ident in profile:
                if _strip_ns(ident.tag) == "Identity":
                    if ident.attrib.get("Primary") == "true":
                        primary_identity_id = ident.attrib.get("ID")
                        break
                    # Fall back to the first Identity if none marked Primary.
                    if primary_identity_id is None:
                        primary_identity_id = ident.attrib.get("ID")

            # Per OFAC's own reference data: 1=vessel, 2=aircraft, 3=entity, 4=individual
            type_map = {"1": "vessel", "2": "aircraft", "3": "entity", "4": "individual"}
            party_type = type_map.get(sub_type_id, "other")
            type_counts[party_type] = type_counts.get(party_type, 0) + 1

            # Find the canonical name: <Alias Primary="true"> within the party.
            # Then join all <NamePartValue> text in document order.
            #
            # Phase 4c (2026-05-09): also capture every NON-primary alias as
            # an alt_name. OFAC uses these for documented AKAs, formerly-
            # known-as names, romanizations, and tradename variants. They
            # are critical inputs for fuzzy ER (a sanctioned vessel renamed
            # last year often broadcasts AIS under the new name; trigram
            # against the primary will miss it).
            display_name = ""
            alt_names: List[str] = []
            seen_alt_names_lower: set = set()
            for alias in party.iter():
                if _strip_ns(alias.tag) != "Alias":
                    continue
                parts = [
                    (npv.text or "").strip()
                    for npv in alias.iter()
                    if _strip_ns(npv.tag) == "NamePartValue" and (npv.text or "").strip()
                ]
                if not parts:
                    continue
                joined = " ".join(parts).strip()
                if not joined:
                    continue
                if alias.attrib.get("Primary") == "true":
                    if not display_name:
                        display_name = joined
                else:
                    # De-dup case-insensitively + don't repeat the primary
                    norm = joined.lower()
                    if norm == display_name.lower():
                        continue
                    if norm in seen_alt_names_lower:
                        continue
                    seen_alt_names_lower.add(norm)
                    alt_names.append(joined)

            # Look up IMO + MMSI + aircraft tail registration from the IDRegDocument index.
            ids_for_party = idreg_index.get(primary_identity_id or "", {}) if primary_identity_id else {}
            imo_raw = ids_for_party.get("1626")  # Vessel Registration ID (IMO)
            mmsi_raw = ids_for_party.get("91264")
            aircraft_serial = ids_for_party.get("1623")

            # OFAC formats IMO values like "IMO 8770261" (with prefix), not bare
            # "8770261". Strip prefix + non-digit chars before int conversion.
            # MMSI values may also have "MMSI " prefix in some entries.
            imo_int: Optional[int] = None
            if imo_raw:
                digits = "".join(ch for ch in imo_raw if ch.isdigit())
                if digits:
                    try:
                        imo_int = int(digits)
                    except ValueError:
                        imo_int = None
            mmsi_clean: Optional[str] = None
            if mmsi_raw:
                digits = "".join(ch for ch in mmsi_raw if ch.isdigit())
                mmsi_clean = digits or None

            if party_type == "vessel" and imo_int:
                with_imo_count += 1

            # Programs: party_id IS the FixedRef on DistinctParty, and the
            # Profile ID is the same string. profile_programs is keyed by
            # ProfileID = DistinctParty.FixedRef.
            programs_for_party = profile_programs.get(party_id, [])
            ofac_programs = [p[1] for p in programs_for_party]   # short codes
            ofac_legal_basis_refs = [p[0] for p in programs_for_party]
            # "Regime" = the most-listed/primary topic. If the party has
            # multiple, we take the first encountered (insertion order via
            # SanctionsEntry traversal); operators wanting all should look
            # at the array fields.
            primary_program = ofac_programs[0] if ofac_programs else None

            rows.append({
                "id":              party_id,
                "type":            party_type,
                "display_name":    display_name or "Unknown SDN entry",
                "alt_names":       alt_names,
                "imo":             imo_int,
                "mmsi":            mmsi_clean,
                "aircraft_serial": aircraft_serial.strip() if aircraft_serial else None,
                "ofac_programs":   ofac_programs,
                "ofac_legal_basis_refs": ofac_legal_basis_refs,
                "ofac_program":    primary_program,
            })

        # Surface type breakdown so operator can see what's happening.
        # IMO coverage is the precision multiplier for cross-domain matching;
        # log it explicitly so we know what fraction of vessels have it.
        self.log.info(
            f"[ofac_sdn] type breakdown: " + ", ".join(
                f"{k}={v}" for k, v in sorted(type_counts.items())
            ) + f"; vessels with IMO: {with_imo_count}"
        )

        return rows

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """SDN entries don't have lat/lng themselves — they need to be
        joined against AIS (vessels) / ADS-B (aircraft) / business addresses.

        For v1.0 ingestion we emit a SANCTIONS-LIST event with an
        unlocated kind='index' marker. The matcher in
        intelligence_loop.py / correlator.py joins these against live
        position feeds to surface red-flagged pins on the globe.

        We use a sentinel position (0,0) for index events. The frontend
        knows kind='index' means "not a pin, this is an entry in the
        sanctions matching index." Correlators pull these out by kind.
        """
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for r in raw_items:
            ext_id = r.get("id") or ""
            if not ext_id:
                continue

            # Only vessels + aircraft get put on the matching index for v1.0.
            # (Individuals/entities without known coordinates would just be
            # text records with no globe value — defer those to v1.1's
            # entity-resolution layer.)
            ptype = r.get("type")
            if ptype not in ("vessel", "aircraft"):
                continue

            payload: Dict[str, Any] = {
                "type":         ptype,
                "display_name": r.get("display_name"),
                "fcra_safe":    False,     # CRITICAL — never use for FCRA decisions
                "_attribution": "Sanctions: US Treasury OFAC",
            }
            # Phase 4c (2026-05-09): non-primary aliases (AKAs, romanizations,
            # tradename variants). Phase 4b's Splink pipeline reads this
            # field and creates additional candidate rows so a vessel
            # broadcasting under a renamed identity still resolves to the
            # sanctioned canonical entry. Empty list when OFAC has no AKAs.
            if r.get("alt_names"):
                payload["alt_names"] = r["alt_names"]
            # IMO + MMSI are gold for precision matching against AIS feeds —
            # IMO is the globally unique vessel identifier, MMSI is the radio
            # ID used in AIS broadcasts. Either alone is exact; together they
            # eliminate name-collision false positives.
            if r.get("imo") is not None:
                payload["imo"] = r["imo"]
            if r.get("mmsi") is not None:
                payload["mmsi"] = r["mmsi"]
            if r.get("aircraft_serial") is not None:
                payload["aircraft_serial"] = r["aircraft_serial"]
            # OFAC programs (regime codes like RUSSIA, IRAN, DPRK pulled
            # from the Executive-Order short-refs). Primary program goes
            # to `regime` so the writer's whitelist surfaces it on the
            # entity row alongside the EU+UK regime fields.
            if r.get("ofac_program"):
                payload["ofac_program"] = r["ofac_program"]
                payload["regime"] = r["ofac_program"]    # parallel UK/EU 'regime' field
            if r.get("ofac_programs"):
                payload["ofac_programs"] = r["ofac_programs"]
            if r.get("ofac_legal_basis_refs"):
                payload["ofac_legal_basis_refs"] = r["ofac_legal_basis_refs"]

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"ofac_sdn:{ptype}:{ext_id}",
                kind="index",                   # not a pin — see docstring
                lat=0.0,                        # sentinel; matcher resolves real lat/lng
                lng=0.0,
                ts=now,
                severity=10,                    # sanctioned = always max severity if matched
                source=self.source,
                payload=payload,
                domain="entity",                # entity-class, not geo
                geocode_quality="needs_match",
                decay_half_life_min=10080,      # 1 week — sanctions stay live until OFAC delists
                market_tags=[],                 # don't auto-fire markets on raw SDN entries
                severity_for_market=0,
            ))

        return out
