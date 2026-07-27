"""`/api/v1/infrastructure/*` — strategic geography GeoJSON layers.

Extracted from `glassbox_server.py` 2026-05-22 as P3-H extraction #8.
Five static-file GeoJSON handlers, all the same shape:

  GET /api/v1/infrastructure/military-bases  — DoD/MOD installations
  GET /api/v1/infrastructure/nuclear         — reactors, enrichment,
                                               weapons labs, storage
  GET /api/v1/infrastructure/cables          — submarine telecom cables
  GET /api/v1/infrastructure/trafficking     — drug/human/arms routes
  GET /api/v1/infrastructure/pipelines       — oil + gas pipelines

Per-route docstrings preserve the source + license metadata (CC-BY-4.0
or CC-BY-SA-4.0 across the set) — important for the LICENSE_RISK_REGISTER
audit trail, do not collapse into a generic comment.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

from web._geojson_db import build_db_geojson_response

router = APIRouter()

# `web/routes/infrastructure.py` → parent.parent.parent is `21_GLASSBOX_AI/`.
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def _serve_geojson(filename: str) -> Response:
    """Read `_DATA_DIR / filename` and return as application/geo+json
    with a 1-hour cache. 404 returns `{}` so the cockpit JS can detect
    a missing dataset without parser errors."""
    p = _DATA_DIR / filename
    if not p.exists():
        return Response(
            "{}",
            status_code=404,
            media_type="application/geo+json",
        )
    return Response(
        content=p.read_bytes(),
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get(
    "/api/v1/infrastructure/military-bases",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_military_bases_geojson() -> Response:
    """Strategically-important military installations as GeoJSON Points.
    Hand-curated from Wikipedia + public DoD/MOD postings +
    Globalsecurity.org; ONLY locations already public in OSINT.
    License: CC-BY-SA-4.0.

    Categories: us_overseas (15), nato, russia (5), china (8 inc Spratly
    artificial islands), iran (2), north_korea (2), israel (2),
    india, pakistan (1). Notes field documents context (e.g. B61
    storage at Incirlik/Aviano/RAF Lakenheath, Chinese island
    fortifications, NK SLBM test platform)."""
    return _serve_geojson("military_bases.geojson")


@router.get(
    "/api/v1/infrastructure/nuclear",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_nuclear_geojson() -> Response:
    """Major nuclear facilities globally — civilian reactors, enrichment,
    weapons labs, storage, disaster sites. Sourced from IAEA PRIS
    public registry + ISIS-Online + NRC + open reporting.
    License: CC-BY-4.0.

    Categories: reactor_active (Zaporizhzhia, Bushehr, Olkiluoto,
    Vogtle, Taishan, Beloyarsk, etc.), reactor_decommissioned
    (Indian Point), enrichment (Natanz, Fordow, Kahuta), weapons_lab
    (Yongbyon, Mayak, Pantex, LANL, Y-12, Sellafield, Dimona, Trombay),
    storage (Onkalo, Yucca-suspended), disaster_site (Chernobyl,
    Fukushima)."""
    return _serve_geojson("nuclear_infrastructure.geojson")


@router.get(
    "/api/v1/infrastructure/cables",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_undersea_cables_geojson() -> Response:
    """Submarine telecom cable routes as GeoJSON. Hand-curated from
    TeleGeography Submarine Cable Map open-data + operator filings —
    license CC-BY-SA-4.0.

    Categories: trans-atlantic, trans-pacific, asia-europe, regional.
    Each Feature: LineString of approximate landing-point geometry +
    properties { name, operator, capacity_tbps, landing_a, landing_b,
    status, length_km, year, notes }.

    Strategic value: cuts to these cables = global-comms-disruption
    events (Red Sea attacks Feb 2024 took out 3-4 cables affecting
    25% of Asia-Europe traffic). Chokepoint awareness for the
    operator (Bab-el-Mandeb, Suez approaches, Malacca, Hormuz)."""
    return _serve_geojson("undersea_cables.geojson")


@router.get(
    "/api/v1/infrastructure/trafficking",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_trafficking_routes_geojson() -> Response:
    """Drug + human + arms trafficking corridors as GeoJSON. Hand-curated
    from UNODC World Drug Report 2024 + UNODC Global Report on Trafficking
    in Persons 2024 + SIPRI Arms Transfers Database + DEA NDTA 2024 —
    license CC-BY-4.0.

    Categories carried in each Feature.properties.category:
      cocaine, heroin, methamphetamine, fentanyl, humans, arms.

    Operator-requested 2026-05-13 NIGHT: "The old glassbox show drug,
    human trafficking, gun routes and more. We need to show this as
    well or it be an option to turn on."

    Each Feature: LineString geometry + properties { name, category,
    source, destination, volume_estimate, primary_actors, notes }."""
    return _serve_geojson("trafficking_routes.geojson")


@router.get(
    "/api/v1/infrastructure/pipelines",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_pipelines_geojson() -> Response:
    """Strategic oil + gas pipeline routes + maritime chokepoints
    as a GeoJSON FeatureCollection. Hand-curated dataset under
    CC-BY-4.0 — see data/pipelines.geojson for sourcing notes.

    Each Feature: LineString geometry + properties { name, commodity,
    status, owner, length_km, operator, notes }. Status values include
    operational, damaged_2022, suspended_*, proposed, under_construction.
    Map this onto the cockpit by toggling the Pipelines layer; a
    forthcoming `/api/v1/infrastructure/cables` will add undersea
    telecom cables on the same shape.

    Operator-requested 2026-05-13 NIGHT: "i would also like to map oil
    and gas pipelines and things like that". Long cache TTL since
    pipeline geometry doesn't change daily."""
    return _serve_geojson("pipelines.geojson")


# ─── Cyber-attack data layers (P2-A Phase 1 MVP, 2026-05-27) ──────────────
#
# These two routes serve seed geojson snapshots of the cyber data feeds.
# Each Feature carries a SENTINEL Point geometry [0, 0] because the data
# is not geographically positioned (CVE entries don't map to a single
# location; Spamhaus blocks describe IP ranges, not places). The cockpit
# atlas.js renders these via a SIDE-PANEL LIST VIEW (not a globe overlay)
# — the feature's `properties` carry the real signal.
#
# Live data refreshes flow:
#   ingesters/cisa_kev.py → writers/cisa_kev.py → Postgres `event` table
#   ingesters/spamhaus_drop.py → writers/spamhaus_drop.py → Postgres `event`
#
# The route here serves the curated static snapshot at first load. A
# follow-on session can switch the route to DB-derived (read latest rows
# from `event` where event_type IN ('kev_disclosure', 'spamhaus_block_entry'))
# without changing the URL shape or the frontend contract.


@router.get(
    "/api/v1/infrastructure/cyber-kev",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_cyber_kev_geojson() -> Response:
    """CISA Known Exploited Vulnerabilities catalog — exploited-in-the-wild
    CVEs the US gov has flagged for federal-agency remediation. License: CC0
    (US Government public domain, Title 17 USC § 105).

    Each Feature: sentinel Point geometry [0, 0] + properties {
      cve_id, vendor_project, product, vulnerability_name,
      short_description, required_action, date_added, due_date,
      known_ransomware_campaign_use, cwes, link
    }.

    NOT a globe overlay — atlas.js renders this layer via a side-panel
    list view (see `?kev=1` toggle). The sentinel geometry is intentional;
    forcing CVE entries onto a single vendor HQ would be misleading.

    Hybrid response: query the `event` hypertable for the latest
    kev_disclosure rows (set by the live CisaKevIngester). If the DB
    has rows, return them; if empty (pre-restart, DB outage, etc.),
    fall back to the static seed file unchanged."""
    return await build_db_geojson_response(
        event_type="kev_disclosure",
        static_filename="cyber_kev.geojson",
        distinct_on_subtype=False,
        limit=2000,
        source_note="live cisa_kev ingester",
    )


@router.get(
    "/api/v1/infrastructure/cyber-spamhaus-drop",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_cyber_spamhaus_drop_geojson() -> Response:
    """Spamhaus DROP/EDROP — hijacked + criminal IP block lists.
    License: free with attribution; Spamhaus DROP/EDROP are explicitly
    designed for redistribution within block-list use.

    Each Feature: sentinel Point geometry [0, 0] + properties {
      cidr, sbl_id, list_name (DROP|EDROP), title, link
    }.

    NOT a globe overlay — atlas.js renders this layer via a side-panel
    list view (see `?spamhaus=1` toggle). A /24 block doesn't map to a
    single point; the sentinel geometry is intentional.

    Hybrid response: queries the event hypertable for the latest
    spamhaus_block_entry rows (set by the live SpamhausDropIngester);
    falls back to the static seed when the DB is empty / unreachable."""
    return await build_db_geojson_response(
        event_type="spamhaus_block_entry",
        static_filename="cyber_spamhaus_drop.geojson",
        distinct_on_subtype=False,
        limit=2000,
        source_note="live spamhaus_drop ingester",
    )


# ─── Conflict-zone curated overlay (P2-B Phase 1, 2026-05-27 NIGHT) ───────


@router.get(
    "/api/v1/infrastructure/conflict-zones",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_conflict_zones_geojson() -> Response:
    """Ongoing armed-conflict / insurgency / terror zones as a curated
    point overlay. Ported from v1 glassbox's `terrorIncidents` layer
    (hand-curated array at `glassbox_v2.html:10692-10700`) and renamed
    `conflict_zones` because the data is a mix of Insurgency / Civil War
    / Armed Group / Terror categories — `conflict_zones` is more
    accurate than the v1 name. Live GDELT terrorism-tagged events flow
    in parallel via the existing `gdelt_topical` ingester (topic =
    'terrorism', shipped in earlier commits) — no new ingester needed.
    License: OSINT-curated, CC-BY-SA-4.0 consistent with the rest of
    the infrastructure-layer set.

    Each Feature: Point geometry at the conflict zone's geographic
    centroid + properties { name, category (Insurgency | Terror |
    Armed Group | Civil War), notes, source }. The categories carry
    different connotations in OSINT reporting — preserved as-is from
    GTD / ACLED conventions rather than collapsing into a single
    label."""
    return _serve_geojson("conflict_zones.geojson")


@router.get(
    "/api/v1/infrastructure/diplomatic-posts",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_diplomatic_posts_geojson() -> Response:
    """Major diplomatic clusters worldwide — capitals + diplomatic
    quarters where embassies and missions concentrate. Ported from v1
    glassbox's `diplomaticPosts` layer (hand-curated array at
    `glassbox_v2.html:14856-14885`). NOT individual embassies (that'd
    be ~3,000 low-signal pins) — each Feature is a regional hub with
    its approximate mission count. License: aggregated from Vienna
    Convention rosters + US State Department public listings, rendered
    under CC-BY-SA-4.0 consistent with the rest of the
    infrastructure-layer set.

    Each Feature: Point geometry at the diplomatic-quarter centroid +
    properties { name, category (capital_hub | un_org_hub |
    regional_hub), country, missions (count), notes }. The category
    distinction matters: capital_hub = primary bilateral-diplomacy
    site, un_org_hub = multilateral UN-agency cluster (Geneva, NYC,
    Nairobi), regional_hub = secondary regional aggregator (Rio,
    Cape Town)."""
    return _serve_geojson("diplomatic_posts.geojson")


@router.get(
    "/api/v1/infrastructure/un-missions",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_un_missions_geojson() -> Response:
    """Active UN peacekeeping, political, observer missions + system HQ.
    Ported from v1 glassbox's `unMissions` layer (hand-curated array at
    `glassbox_v2.html:14747-14778`). Ended missions (MINUSMA Mali,
    UNIKOM Kuwait) deliberately omitted — the cockpit shows
    currently-deployed operations, not historical-context. License: UN
    Open Data, free with attribution.

    Each Feature: Point geometry at the mission HQ + properties {
    name, category (peacekeeping | political | observers | hq),
    country, personnel (count snapshot), notes }. Personnel counts
    are UN PKO point-in-time snapshots — render-time should treat
    the count as approximate. Categories carry distinct UN-system
    mandates: peacekeeping (Chapter VII deployments with troops),
    political (Chapter VI political/support), observers
    (truce-supervision missions like UNTSO, UNMOGIP), hq (UN-system
    headquarters offices like UN Geneva, UNEP Nairobi)."""
    return _serve_geojson("un_missions.geojson")


@router.get(
    "/api/v1/infrastructure/disputed-zones",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_disputed_zones_geojson() -> Response:
    """Long-running territorial disputes — sovereignty contests, occupied
    regions, strategic flashpoints. Ported from v1 glassbox's
    `disputedZones` layer (hand-curated array at
    `glassbox_v2.html:11993-12013`). Rendered as point markers at the
    disputed region's geographic centroid (v1 also drew a 200km radius
    ellipse — that's deferred to a follow-on session's enhanced
    renderer). License: CC-BY-SA-4.0 consistent with the rest of the
    infrastructure-layer set; data compiled from International Crisis
    Group + IISS + CFR Conflict Tracker reporting.

    Each Feature: Point geometry at the dispute centroid + properties {
    name, category (active | frozen | flashpoint), parties, notes }.
    Category is a rough intensity tier (active = currently kinetic;
    frozen = unresolved but not active fighting; flashpoint = major
    strategic risk regardless of current intensity) — necessarily
    coarse-grained, revisit annually as situations evolve."""
    return _serve_geojson("disputed_zones.geojson")


@router.get(
    "/api/v1/infrastructure/state-media",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_state_media_geojson() -> Response:
    """Major state-owned broadcasters + state-funded media + documented
    coordinated disinformation operations. Ported from v1 glassbox's
    `propagandaCenters` layer (hand-curated array at
    `glassbox_v2.html:12093-12120`), renamed `state_media` to match the
    v1 user-facing display label "State Media Hubs" (the `propagandaCenters`
    internal id editorialized in a way the v1 UI didn't). The original
    v1 layer mixed three governance models under one label; this port
    separates them into category sub-tags so the operator can read the
    difference at a glance.

    Each Feature: Point geometry at the outlet HQ + properties { name,
    category (state_owned | state_funded | disinfo_ops), country,
    notes }. Categories:
      - state_owned    — direct gov control (RT, CCTV, KCNA, Press TV,
                          SANA, Granma, TeleSUR)
      - state_funded   — gov funded but editorial independence claimed
                          (Al Jazeera, TRT, Al Arabiya, Al-Mayadeen)
      - disinfo_ops    — documented coordinated-inauthentic-behavior
                          operations per Stanford Internet Observatory
                          + DFRLab (IRA Moscow + St Petersburg, Shanghai
                          Cyberspace Admin)

    License: CC-BY-SA-4.0 consistent with the rest of the
    infrastructure-layer set; data compiled from EUvsDisinfo + RAND +
    Stanford Internet Observatory + DFRLab reporting."""
    return _serve_geojson("state_media.geojson")


@router.get(
    "/api/v1/infrastructure/sanction-targets",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_sanction_targets_geojson() -> Response:
    """Country-level / regime-level summary of who is sanctioned by
    whom. Strategic overlay distinct from the entity-level OFAC SDN +
    UK OFSI + EU CFSP data already in the `sanctioned_vessel` +
    `sanctioned_aircraft` + entity tables (which are tactical
    asset-level data); this layer surfaces the political-geography
    view: which national governments are subject to comprehensive
    regimes, which are under targeted measures, etc. Ported from v1
    glassbox's `sanctionTargets` layer (hand-curated array at
    `glassbox_v2.html:12030-12063`); 2 v1 errors fixed during port
    (Myanmar entry was geolocated to HCMC/Vietnam — moved to Naypyidaw;
    duplicate Mogadishu entry deduped). 19 entries instead of v1's 20.
    License: CC-BY-SA-4.0 consistent with the rest of the
    infrastructure-layer set.

    Each Feature: Point geometry at the capital (or relevant landmark
    for the regime) + properties { name, category (comprehensive |
    targeted | arms_embargo | terrorism | monitoring | legacy),
    sanctioning_bodies, notes }. Categories carry distinct legal
    meanings: comprehensive = whole-economy regime; targeted =
    specific individuals / entities only; arms_embargo = lethal-
    equipment trade prohibition only; terrorism = CT designations
    (FTO etc.); monitoring = not directly targeted but watched for
    sanctions-evasion / sanctions-bypass risk; legacy = residual
    measures from a prior fuller regime."""
    return _serve_geojson("sanction_targets.geojson")


@router.get(
    "/api/v1/infrastructure/noaa-buoys",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_noaa_buoys_geojson() -> Response:
    """Curated subset of high-traffic / strategic NOAA NDBC ocean
    monitoring buoys. Static layer surfacing buoy LOCATIONS only —
    live observation data (wave height, wind, SST) is deferred to a
    future enhancement that would add a separate `ndbc.py` ingester
    pulling per-station observations. The location layer alone gives
    the operator a geographic reference for where ocean monitoring
    is concentrated (Pacific NW, SoCal, Gulf, US Atlantic, Bering Sea,
    Hawaii). License: NOAA NDBC = US gov public domain (Title 17 USC
    § 105).

    Ported from v1 glassbox's `noaaBuoys` layer (hand-curated array
    at `glassbox_v2.html:7813-7829`). One v1 dupe fixed during port:
    station 46006 appeared twice with slightly different coords;
    kept the first instance. 14 unique stations.

    Each Feature: Point geometry at the buoy's anchored location +
    properties { name, category (pacific_nw | pacific | gulf |
    atlantic | alaska | hawaii), station_id (NDBC 5-digit), link
    (NDBC station page), notes }.

    Hybrid response: queries the event hypertable for the latest
    ndbc_observation per station (DISTINCT ON event_subtype) — set
    by the live NoaaNdbcIngester. Each station emits a fresh
    observation every 10-30 min; the DISTINCT-ON projects only the
    most-recent observation per station so the response stays a
    14-feature snapshot rather than thousands of historical rows.
    Falls back to the static seed (14 station locations, no live
    measurements) when the DB has no observations yet."""
    return await build_db_geojson_response(
        event_type="ndbc_observation",
        static_filename="noaa_buoys.geojson",
        distinct_on_subtype=True,
        limit=50,
        source_note="live noaa_ndbc ingester (latest per station)",
    )


@router.get(
    "/api/v1/infrastructure/climate-forecast",
    include_in_schema=True,
    tags=["infrastructure"],
)
async def serve_climate_forecast_geojson() -> Response:
    """Daily climate / weather snapshot for 15 major world cities.
    Ported from v1 glassbox's `climateForecast` layer (fallback array
    at `glassbox_v2.html:18504-18519` — v1 also fetched live from
    Open-Meteo at runtime; this static seed represents the typical
    seasonal range). License: Open-Meteo CC-BY 4.0 (commercial use
    permitted with attribution per
    https://open-meteo.com/en/license).

    Static layer — live Open-Meteo refresh per city is deferred to a
    follow-on `open_meteo_forecast.py` ingester polling at 6h cadence
    (Open-Meteo's forecast update frequency). The seed gives the
    cockpit visible content immediately + a fallback when the live
    source is unreachable.

    Each Feature: Point geometry at the city centroid + properties {
    name, category (cold | temperate | warm | hot), temp_max_c,
    temp_min_c, precipitation_mm, notes }. Categories drive the
    color encoding (blue → green → yellow → red on the cockpit).

    Hybrid response: queries the event hypertable for the latest
    climate_forecast per city (DISTINCT ON event_subtype) — set by
    the live OpenMeteoForecastIngester. Falls back to the static
    seed (typical seasonal ranges) when the DB has no forecasts yet."""
    return await build_db_geojson_response(
        event_type="climate_forecast",
        static_filename="climate_forecast.geojson",
        distinct_on_subtype=True,
        limit=50,
        source_note="live open_meteo_forecast ingester (latest per city)",
    )
