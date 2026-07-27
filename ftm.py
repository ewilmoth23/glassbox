# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
FollowTheMoney schema bridge — empire ``entity`` rows ↔ FtM JSON shape.

The empire's ``entity`` table is the durable home for every entity type
the platform tracks (vessels, aircraft, satellites, sanctioned vessels,
sanctioned aircraft). The OCCRP / OpenSanctions ecosystem standardizes
on the FollowTheMoney schema (https://followthemoney.tech/) — adopting
its on-the-wire shape buys interoperability with every tool downstream
of the OCCRP graph (yente, OpenAleph, Zavod, Aleph, etc.) without any
schema migration to the internal table.

Today this module only handles the OUTBOUND translation
(``entity_to_ftm``). The inbound path (``ftm_to_entity``) is deferred
until we have an actual external FtM source to consume — Splink ER's
existing ``entity_relation`` table covers cross-list deduplication for
the bespoke OFAC + EU CFSP + UK OFSI ingesters today, so importing
external FtM-shaped data is a Phase 4-onwards concern.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from followthemoney import model


_log = logging.getLogger(__name__)


# Empire entity_type → FtM schema name. Sanctioned-* variants land in
# the same FtM schema as their non-sanctioned counterparts, with
# ``topics: ["sanction"]`` flagging the regime.
_ENTITY_TYPE_TO_FTM_SCHEMA = {
    "vessel":              "Vessel",
    "sanctioned_vessel":   "Vessel",
    "aircraft":            "Airplane",
    "sanctioned_aircraft": "Airplane",
    # Satellites have no clean FtM equivalent (FtM's transportation
    # schema family is human/cargo-focused). Translation returns None;
    # if a future commit needs satellite interop we'd extend FtM with
    # a Glassbox-defined custom schema rather than coerce.
}


# Per-FtM-property handling. ``identifier`` types are passed with
# ``cleaned=True`` because the empire stores already-canonical IDs and
# FtM's strict identifier cleaner rejects unprefixed IMO numbers etc.
_IDENTIFIER_PROPS = {
    "imoNumber", "mmsi", "callSign", "registrationNumber",
    "icaoCode", "serialNumber",
}


def entity_to_ftm(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate one empire ``entity`` row into the FtM JSON shape.

    Args:
        row: Row from the ``entity`` table. Required keys:
            ``entity_type``, ``canonical_id``. Optional but consumed:
            ``properties`` (JSONB-derived dict of ingester payload).

    Returns:
        FtM JSON ``{"id": ..., "schema": ..., "properties": {...}}`` or
        None if ``entity_type`` is unsupported. Empty-property entities
        DO produce a valid skeleton (id + schema) — caller can decide
        whether to surface those.
    """
    entity_type = row.get("entity_type")
    canonical_id = row.get("canonical_id")
    if not entity_type or not canonical_id:
        return None

    schema_name = _ENTITY_TYPE_TO_FTM_SCHEMA.get(entity_type)
    if schema_name is None:
        return None

    proxy = model.make_entity(schema_name)
    proxy.id = str(canonical_id)

    props = row.get("properties") or {}
    is_sanctioned = entity_type.startswith("sanctioned_")

    # Universal: name. Most ingesters store this under either
    # 'display_name' or 'name'; tolerate both.
    name = props.get("display_name") or props.get("name")
    if name:
        proxy.add("name", str(name))

    if schema_name == "Vessel":
        _populate_vessel(proxy, props)
    elif schema_name == "Airplane":
        _populate_airplane(proxy, props)

    if is_sanctioned:
        proxy.add("topics", "sanction")
        # Regime label (Russia/Ukraine, DPRK, etc.) goes in ``program``,
        # the FtM-canonical slot for sanctions-program names.
        regime = props.get("regime") or props.get("programme")
        if regime:
            proxy.add("program", str(regime))
        # Authority + reference IDs go in description so consumers see
        # the full provenance string without us needing a sub-entity.
        bits = []
        if props.get("sanctioning_authority"):
            bits.append(f"Authority: {props['sanctioning_authority']}")
        for ref_key, label in (
            ("eu_ref", "EU"), ("uk_ofsi_id", "UK OFSI"),
            ("ofac_uid", "OFAC"),
        ):
            if props.get(ref_key):
                bits.append(f"{label} ref: {props[ref_key]}")
        if bits:
            proxy.add("description", "; ".join(bits))

    return proxy.to_dict()


def _populate_vessel(proxy, props: Dict[str, Any]) -> None:
    imo = props.get("imo") or props.get("imoNumber")
    if imo is not None:
        proxy.add("imoNumber", str(imo), cleaned=True)
    mmsi = props.get("mmsi")
    if mmsi is not None:
        proxy.add("mmsi", str(mmsi), cleaned=True)
    callsign = props.get("callsign") or props.get("call_sign")
    if callsign:
        proxy.add("callSign", str(callsign), cleaned=True)
    flag = props.get("flag") or props.get("flag_state")
    if flag:
        proxy.add("flag", str(flag))
    vessel_type = props.get("vessel_type") or props.get("ship_type")
    if vessel_type:
        proxy.add("type", str(vessel_type))
    if props.get("registration_number"):
        proxy.add("registrationNumber", str(props["registration_number"]),
                  cleaned=True)


def _populate_airplane(proxy, props: Dict[str, Any]) -> None:
    icao = props.get("icao24") or props.get("icaoCode")
    if icao:
        proxy.add("icaoCode", str(icao), cleaned=True)
    callsign = props.get("callsign") or props.get("call_sign")
    if callsign:
        proxy.add("callSign", str(callsign), cleaned=True) \
            if "callSign" in proxy.schema.properties else None
    reg = (props.get("registration") or props.get("reg")
           or props.get("tail_number"))
    if reg:
        proxy.add("registrationNumber", str(reg), cleaned=True)
    if props.get("aircraft_type") or props.get("model"):
        proxy.add("model", str(props.get("model") or props.get("aircraft_type")))
    if props.get("operator"):
        # Operator is an entity-typed prop that FtM expects to point at
        # another entity ID. We don't have the operator as an empire
        # entity yet, so encode as a description fragment instead.
        cur = proxy.first("description") or ""
        suffix = f"Operator: {props['operator']}"
        proxy.add("description", f"{cur}; {suffix}".strip("; "))


def supported_entity_types() -> List[str]:
    """Sorted list of empire ``entity_type`` values this module
    translates. Useful for an ``/api/v1/entities/{id}?format=ftm``
    endpoint to 415-out-of-band on unsupported types."""
    return sorted(_ENTITY_TYPE_TO_FTM_SCHEMA.keys())
