"""
Port-call detection — Phase 4 algorithm (port_call_v1).

Detects vessels that are currently *at* a major port (within `radius_m`
of the port's reference point AND moving slowly enough to indicate
in-port presence rather than transit). Each (vessel, port) pair fires
at most once per `cooldown_hours` so a vessel sitting at berth doesn't
spam the event timeline.

What "at port" means here (v1):
  - vessel.current_geom within `radius_m` (default 5 km) of a port's
    reference coordinate AND
  - last known velocity < `at_port_max_velocity_ms` (default 1.5 m/s ≈
    3 knots — typical anchored / berthed / docking speed)

We don't yet distinguish *arrival* vs *departure* (transition events
require state per (vessel, port) pair across cycles). v1 just surfaces
"vessel X is at port Y" as a continuous signal — sufficient to power
"who's at Bandar Abbas right now?" intel queries. v1.1 plan: track
state per pair and emit port_arrival / port_departure transitions.

Why this matters operationally:
  - "Sanctioned vessel arrives at Iranian port" is a high-signal
    intel event. The sanctions_match algorithm flags the vessel; this
    one places it.
  - "Russian-flagged tanker at Indian port" surfaces sanctions-evasion
    routing without any LLM-based reasoning.
  - "Empty days at Yangshan Deep-Water Port" (Shanghai) is a real-time
    proxy for China export volume — useful macro signal.

Performance:
  Single SQL per scan. The port reference is small (~75 rows, embedded
  in this module — no separate table to maintain). For each port we do
  one ST_DWithin query against entity.current_geom (GiST-indexed). At
  v1.0 scale (~18k vessels × 75 ports) the candidate set is tiny;
  expected runtime <500 ms per scan.

Idempotency:
  Same (vessel_id, port_id) pair flagged at most once per `cooldown_hours`
  (default 24 h) via a NOT EXISTS clause on prior `port_call` events with
  the same external_id. A vessel that loiters at berth for a week emits
  one event per day, not one per 5-min scan cycle.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from db import acquire_write


_log = logging.getLogger("algorithms.port_call")


# ─── Port reference data ─────────────────────────────────────────────────
# (port_id, name, country_iso2, lat, lng, kind)
#   kind = 'commercial' (top-100 container/general cargo throughput) OR
#          'strategic'  (military / sanctions-relevant)
#
# Sources: public-domain navigation reference data (UN/LOCODE locations,
# WPI = World Port Index from NGA which is US-government public domain).
# Coordinates are accurate to ~1-3 km — sufficient for the 5km radius
# match. Adding more ports is one row each + restart; no schema change.
PORTS: List[Tuple[str, str, str, float, float, str]] = [
    # ─── Top container ports (TEU throughput, 2024 data) ────────────
    ("CN_SHA", "Shanghai",            "CN", 31.2304, 121.4737, "commercial"),
    ("SG_SIN", "Singapore",           "SG",  1.2655, 103.8240, "commercial"),
    ("CN_NGB", "Ningbo-Zhoushan",     "CN", 29.9094, 121.7647, "commercial"),
    ("CN_SZX", "Shenzhen",            "CN", 22.5455, 114.0680, "commercial"),
    ("CN_CAN", "Guangzhou",           "CN", 23.0900, 113.4435, "commercial"),
    ("KR_PUS", "Busan",               "KR", 35.1018, 129.0775, "commercial"),
    ("CN_TAO", "Qingdao",             "CN", 36.0671, 120.3826, "commercial"),
    ("AE_JEA", "Jebel Ali (Dubai)",   "AE", 25.0167,  55.0500, "commercial"),
    ("CN_TXG", "Tianjin",             "CN", 39.0167, 117.7000, "commercial"),
    ("NL_RTM", "Rotterdam",           "NL", 51.9500,   4.1500, "commercial"),
    ("BE_ANR", "Antwerp",             "BE", 51.2667,   4.4000, "commercial"),
    ("MY_PKL", "Port Klang",          "MY",  3.0000, 101.4000, "commercial"),
    ("CN_XMN", "Xiamen",              "CN", 24.4500, 118.0667, "commercial"),
    ("DE_HAM", "Hamburg",             "DE", 53.5400,   9.9700, "commercial"),
    ("US_LAX", "Los Angeles",         "US", 33.7333,-118.2667, "commercial"),
    ("US_LGB", "Long Beach",          "US", 33.7500,-118.1830, "commercial"),
    ("MY_TPP", "Tanjung Pelepas",     "MY",  1.3667, 103.5500, "commercial"),
    ("TH_LCH", "Laem Chabang",        "TH", 13.0833, 100.9000, "commercial"),
    ("US_NYC", "New York / Newark",   "US", 40.6770, -74.1522, "commercial"),
    ("HK_HKG", "Hong Kong",           "HK", 22.3000, 114.1700, "commercial"),
    ("TW_KHH", "Kaohsiung",           "TW", 22.6167, 120.2667, "commercial"),
    ("VN_SGN", "Ho Chi Minh City",    "VN", 10.7700, 106.7000, "commercial"),
    ("ID_TPP", "Tanjung Priok",       "ID", -6.1000, 106.8833, "commercial"),
    ("ES_ALG", "Algeciras",           "ES", 36.1330,  -5.4500, "commercial"),
    ("ES_VLC", "Valencia",            "ES", 39.4500,  -0.3170, "commercial"),
    ("LK_CMB", "Colombo",             "LK",  6.9500,  79.8500, "commercial"),
    ("GR_PIR", "Piraeus",             "GR", 37.9333,  23.6333, "commercial"),
    ("IN_MUN", "Mundra",              "IN", 22.7333,  69.7167, "commercial"),
    ("SA_JED", "Jeddah",              "SA", 21.4833,  39.1667, "commercial"),
    ("DE_BRV", "Bremerhaven",         "DE", 53.5500,   8.5833, "commercial"),
    ("GB_FXT", "Felixstowe",          "GB", 51.9500,   1.3000, "commercial"),
    ("JP_YOK", "Yokohama",            "JP", 35.4500, 139.6500, "commercial"),
    ("JP_TYO", "Tokyo",               "JP", 35.6500, 139.7700, "commercial"),
    ("PH_MNL", "Manila",              "PH", 14.5833, 120.9833, "commercial"),
    ("OM_SLL", "Salalah",             "OM", 16.9500,  54.0167, "commercial"),
    ("BR_SSZ", "Santos",              "BR", -23.9670, -46.3300, "commercial"),
    ("MX_ZLO", "Manzanillo",          "MX", 19.0667,-104.3167, "commercial"),
    ("MA_TNG", "Tangier Med",         "MA", 35.8833,  -5.5000, "commercial"),
    ("CA_VAN", "Vancouver",           "CA", 49.2833,-123.1167, "commercial"),
    ("US_HOU", "Houston",             "US", 29.7333, -95.3167, "commercial"),
    ("US_CHS", "Charleston",          "US", 32.7833, -79.9333, "commercial"),
    ("US_SAV", "Savannah",            "US", 32.0833, -81.0833, "commercial"),
    ("JM_KIN", "Kingston",            "JM", 17.9833, -76.7833, "commercial"),
    ("CO_CTG", "Cartagena",           "CO", 10.4000, -75.5167, "commercial"),
    ("AR_BUE", "Buenos Aires",        "AR", -34.6000, -58.3667, "commercial"),
    ("FR_LEH", "Le Havre",            "FR", 49.4900,   0.1100, "commercial"),
    ("FR_MRS", "Marseille",           "FR", 43.3500,   5.3000, "commercial"),
    ("IT_GOA", "Genoa",               "IT", 44.4060,   8.9300, "commercial"),
    ("IT_GIT", "Gioia Tauro",         "IT", 38.4333,  15.9000, "commercial"),
    ("RO_CND", "Constanta",           "RO", 44.1500,  28.6500, "commercial"),
    ("EG_PSD", "Port Said",           "EG", 31.2667,  32.3000, "commercial"),
    ("EG_SUZ", "Suez",                "EG", 29.9667,  32.5500, "commercial"),
    ("TR_AMB", "Ambarli (Istanbul)",  "TR", 40.9833,  28.6833, "commercial"),
    ("PA_BLB", "Balboa (Panama)",     "PA",  8.9500, -79.5667, "commercial"),
    ("PA_MIT", "Manzanillo (Panama)", "PA",  9.3833, -79.8000, "commercial"),
    ("AU_SYD", "Sydney (Botany)",     "AU", -33.9667, 151.2333, "commercial"),
    ("AU_MEL", "Melbourne",           "AU", -37.8333, 144.9000, "commercial"),
    ("ZA_DUR", "Durban",              "ZA", -29.8667,  31.0500, "commercial"),
    ("NG_LOS", "Lagos (Apapa)",       "NG",  6.4500,   3.3500, "commercial"),
    # ─── Strategic / sanctions-relevant ports ───────────────────────
    # US Navy + Allied
    ("US_NRF", "Norfolk Naval Base",  "US", 36.9472, -76.3294, "strategic"),
    ("US_PEH", "Pearl Harbor",        "US", 21.3500,-157.9500, "strategic"),
    ("US_SDG", "San Diego Naval",     "US", 32.6900,-117.1700, "strategic"),
    ("JP_YOS", "Yokosuka (US Navy)",  "JP", 35.2900, 139.6700, "strategic"),
    ("BH_MNM", "NSA Bahrain",         "BH", 26.2050,  50.6000, "strategic"),
    ("IO_DGA", "Diego Garcia",        "IO", -7.3000,  72.4000, "strategic"),
    ("PH_SUB", "Subic Bay",           "PH", 14.7833, 120.2833, "strategic"),
    ("DJ_JIB", "Camp Lemonnier",      "DJ", 11.5478,  43.1597, "strategic"),
    # Russia
    ("RU_SVO", "Sevastopol",          "RU", 44.6166,  33.5254, "strategic"),
    ("RU_VVO", "Vladivostok",         "RU", 43.1056, 131.8735, "strategic"),
    ("RU_MMK", "Murmansk",            "RU", 68.9667,  33.0833, "strategic"),
    ("RU_NVS", "Novorossiysk",        "RU", 44.7167,  37.7667, "strategic"),
    ("RU_KGD", "Kaliningrad",         "RU", 54.7000,  20.4500, "strategic"),
    # Iran (sanctions watchlist)
    ("IR_BND", "Bandar Abbas",        "IR", 27.1833,  56.2833, "strategic"),
    ("IR_KIS", "Kish Island",         "IR", 26.5167,  53.9833, "strategic"),
    ("IR_BKH", "Bandar-e Khomeini",   "IR", 30.4500,  49.1000, "strategic"),
    ("IR_JSK", "Jask",                "IR", 25.6500,  57.7833, "strategic"),
    # DPRK (heavily sanctioned)
    ("KP_NMP", "Nampo",               "KP", 38.7333, 125.4000, "strategic"),
    ("KP_RJN", "Rajin",               "KP", 42.2500, 130.3000, "strategic"),
    ("KP_WSN", "Wonsan",              "KP", 39.1667, 127.4333, "strategic"),
    # Cuba / Venezuela (US sanctions watchlist)
    ("CU_HAV", "Havana",              "CU", 23.1333, -82.3333, "strategic"),
    ("VE_AMY", "Amuay (PDVSA)",       "VE", 11.7500, -70.2167, "strategic"),
    ("VE_LCA", "La Cruz (Cardon)",    "VE", 11.6667, -70.2167, "strategic"),
    # Syria
    ("SY_LTK", "Latakia",             "SY", 35.5167,  35.7833, "strategic"),
    ("SY_TUS", "Tartus (Russian)",    "SY", 34.8833,  35.8833, "strategic"),
    # ─── Baltic + N. European (matches our current AIS coverage from
    #     Digitraffic + BarentsWatch + DMA) ──────────────────────────
    ("SE_STO", "Stockholm",           "SE", 59.3300,  18.0667, "commercial"),
    ("SE_GOT", "Gothenburg",          "SE", 57.7000,  11.9667, "commercial"),
    ("FI_HEL", "Helsinki",            "FI", 60.1697,  24.9408, "commercial"),
    ("FI_TKU", "Turku",               "FI", 60.4333,  22.2167, "commercial"),
    ("EE_TLL", "Tallinn",             "EE", 59.4400,  24.7500, "commercial"),
    ("LV_RIX", "Riga",                "LV", 56.9500,  24.1000, "commercial"),
    ("LT_KLJ", "Klaipeda",            "LT", 55.7167,  21.1333, "commercial"),
    ("PL_GDN", "Gdansk",              "PL", 54.4000,  18.6667, "commercial"),
    ("PL_GDY", "Gdynia",              "PL", 54.5000,  18.5500, "commercial"),
    ("RU_SPB", "St. Petersburg",      "RU", 59.9400,  30.3100, "strategic"),
    ("RU_UST", "Ust-Luga",            "RU", 59.6700,  28.4000, "strategic"),
    ("RU_PRI", "Primorsk",            "RU", 60.3667,  28.6000, "strategic"),
    ("DK_CPH", "Copenhagen",          "DK", 55.7000,  12.5833, "commercial"),
    ("DK_AAR", "Aarhus",              "DK", 56.1500,  10.2000, "commercial"),
    ("DK_AAL", "Aalborg",             "DK", 57.0500,   9.9167, "commercial"),
    ("NO_OSL", "Oslo",                "NO", 59.9100,  10.7500, "commercial"),
    ("NO_BGO", "Bergen",              "NO", 60.3933,   5.3242, "commercial"),
    ("NO_TRD", "Trondheim",           "NO", 63.4400,  10.4000, "commercial"),
    ("NO_KKN", "Kirkenes",            "NO", 69.7244,  30.0451, "strategic"),
]


# In-flight reference: id -> (name, country, lat, lng, kind). Used by
# the SQL builder + tests.
PORT_INDEX: Dict[str, Tuple[str, str, float, float, str]] = {
    p[0]: (p[1], p[2], p[3], p[4], p[5]) for p in PORTS
}


PORT_CALL_SCAN_SQL = """
-- Port-call detection: vessels currently within radius_m of a port AND
-- moving slowly enough to be in-port (not in transit). One CTE per port
-- would be ugly; instead we materialize the ports list as a VALUES
-- clause, cross-join against entity, and filter spatially in one shot.
--
-- Uses entity.current_geom (GiST) so the spatial filter is fast even
-- with the cartesian against ~75 ports.
WITH ports(port_id, port_name, country, port_lat, port_lng, port_kind) AS (
    VALUES {VALUES_CLAUSE}
),
candidates AS (
    SELECT
        e.id              AS vessel_id,
        e.canonical_id    AS vessel_canonical_id,
        e.display_name    AS vessel_name,
        e.current_position_time,
        p.port_id, p.port_name, p.country, p.port_kind,
        p.port_lat, p.port_lng,
        ST_Distance(
            e.current_geom,
            ST_SetSRID(ST_MakePoint(p.port_lng, p.port_lat), 4326)::geography
        ) AS distance_m,
        -- Pull last velocity from position_track via lateral subquery
        (SELECT pt.velocity_ms
         FROM position_track pt
         WHERE pt.entity_id = e.id
         ORDER BY pt.time DESC
         LIMIT 1) AS last_velocity_ms
    FROM entity e
    CROSS JOIN ports p
    WHERE e.entity_type = 'vessel'
      AND e.current_geom IS NOT NULL
      AND e.current_position_time >= NOW() - ($1::int * INTERVAL '1 minute')
      AND ST_DWithin(
              e.current_geom,
              ST_SetSRID(ST_MakePoint(p.port_lng, p.port_lat), 4326)::geography,
              $2
      )
),
in_port AS (
    SELECT *
    FROM candidates
    WHERE last_velocity_ms IS NOT NULL
      AND last_velocity_ms < $3   -- stationary-enough to count as in-port
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, entity_id, domain, decay_half_life_min
)
SELECT
    'port_call'                                        AS event_type,
    cp.country                                         AS event_subtype,
    cp.current_position_time                           AS event_time,
    ST_SetSRID(ST_MakePoint(cp.port_lng, cp.port_lat), 4326)::geography AS geom,
    CASE
        WHEN cp.port_kind = 'strategic' THEN 6
        ELSE 3
    END                                                AS severity,
    COALESCE(cp.vessel_name, cp.vessel_canonical_id) ||
        ' at ' || cp.port_name                         AS title,
    'Vessel within ' || ROUND(cp.distance_m::numeric, 0) ||
        ' m of ' || cp.port_name || ' (' || cp.country ||
        '); v=' || ROUND(cp.last_velocity_ms::numeric, 1) || ' m/s' AS description,
    jsonb_build_object(
        'algorithm',          $4::text,
        'vessel_id',          cp.vessel_id::text,
        'vessel_canonical_id', cp.vessel_canonical_id,
        'vessel_name',        cp.vessel_name,
        'port_id',            cp.port_id,
        'port_name',          cp.port_name,
        'port_country',       cp.country,
        'port_kind',          cp.port_kind,
        'distance_m',         ROUND(cp.distance_m::numeric, 0)::int,
        'velocity_ms',        ROUND(cp.last_velocity_ms::numeric, 2)::float
    )                                                  AS properties,
    cp.vessel_id                                       AS entity_id,
    'maritime'                                         AS domain,
    1440                                               AS decay_half_life_min
FROM in_port cp
WHERE NOT EXISTS (
    SELECT 1 FROM event prior
    WHERE prior.event_type = 'port_call'
      AND prior.entity_id  = cp.vessel_id
      AND prior.properties->>'port_id' = cp.port_id
      AND prior.event_time >= NOW() - ($5::int * INTERVAL '1 hour')
)
"""


def _build_values_clause() -> str:
    """Render the PORTS list as a SQL VALUES clause body.
    Returns the inner '(row1), (row2), ...' string (no leading VALUES).
    Using parameter binding for ~75 ports would be 5 placeholders × 75 =
    375 binds; literal interpolation is simpler and the values are
    module-constants, not user input — safe from injection.
    """
    rows = []
    for pid, name, country, lat, lng, kind in PORTS:
        # Escape single quotes in name; nothing else needs escaping
        # because ids / countries / kinds are constants.
        safe_name = name.replace("'", "''")
        rows.append(
            f"('{pid}', '{safe_name}', '{country}', "
            f"{lat:.6f}::float, {lng:.6f}::float, '{kind}')"
        )
    return ", ".join(rows)


async def run_port_call_scan(
    *,
    radius_m: int = 5_000,
    fresh_window_min: int = 60,
    at_port_max_velocity_ms: float = 1.5,
    # 7-day cooldown (was 24h — caused 2.24x over-firing per audit
    # 2026-05-13). Vessels typically dock 1-7 days; previously a vessel
    # at berth emitted a fresh finding every 24h. Now: one finding per
    # arrival, no daily refire while sitting at the same port.
    cooldown_hours: int = 7 * 24,
    algorithm_tag: str = "port_call_v1",
) -> int:
    """Run one port-call detection pass. Returns count of new findings.

    Args:
        radius_m: a vessel within `radius_m` of a port reference point
            counts as 'at port'. Default 5 km — large enough to cover
            most port complexes without bleeding into anchorages outside
            the breakwater.
        fresh_window_min: only consider vessels with current_position_time
            in the last N minutes. Default 60.
        at_port_max_velocity_ms: vessel's last velocity must be below
            this to register as in-port. Default 1.5 m/s ≈ 3 knots.
            Filters out transiting vessels passing near a port.
        cooldown_hours: don't re-emit (vessel, port) within this window.
            Default 24 h — a vessel sitting at berth emits one event per
            day, not per scan.
        algorithm_tag: written to properties.algorithm. Production uses
            'port_call_v1'.
    """
    if not PORTS:
        return 0
    sql = PORT_CALL_SCAN_SQL.replace("{VALUES_CLAUSE}", _build_values_clause())
    async with acquire_write() as conn:
        result = await conn.execute(
            sql,
            fresh_window_min,
            radius_m,
            at_port_max_velocity_ms,
            algorithm_tag,
            cooldown_hours,
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"port_call scan: {count} findings "
            f"(radius={radius_m}m, fresh={fresh_window_min}min, "
            f"v_max={at_port_max_velocity_ms}m/s, cooldown={cooldown_hours}h)"
        )
    return count


# ─── Port arrival / departure transitions (v1.1) ─────────────────────────
#
# Transitions are derived from the port_call event timeline rather than
# tracked in a side table:
#
#   port_arrival  — vessel currently AT port + no port_call event for
#                   this (vessel, port) in the last `arrival_lookback_h`
#                   (default 7 days). Emitted ONCE per arrival event;
#                   the daily-cadence port_call cooldown then dampens
#                   continued-stay noise.
#
#   port_departure — vessel HAD a port_call event in the last
#                   `departure_lookback_h` (default 36 h) AND has a
#                   fresh current position AND is NOT currently within
#                   port_radius. The fresh-position guard distinguishes
#                   "left port" from "AIS went dark" (which the
#                   dark_ship algorithm handles separately).
#
# Both run as standalone SQL queries — no state machine needed.

PORT_ARRIVAL_SCAN_SQL = """
WITH ports(port_id, port_name, country, port_lat, port_lng, port_kind) AS (
    VALUES {VALUES_CLAUSE}
),
currently_at_port AS (
    SELECT
        e.id              AS vessel_id,
        e.canonical_id    AS vessel_canonical_id,
        e.display_name    AS vessel_name,
        e.current_position_time,
        p.port_id, p.port_name, p.country, p.port_kind,
        p.port_lat, p.port_lng,
        ST_Distance(
            e.current_geom,
            ST_SetSRID(ST_MakePoint(p.port_lng, p.port_lat), 4326)::geography
        ) AS distance_m,
        (SELECT pt.velocity_ms
         FROM position_track pt
         WHERE pt.entity_id = e.id
         ORDER BY pt.time DESC
         LIMIT 1) AS last_velocity_ms
    FROM entity e
    CROSS JOIN ports p
    WHERE e.entity_type = 'vessel'
      AND e.current_geom IS NOT NULL
      AND e.current_position_time >= NOW() - ($1::int * INTERVAL '1 minute')
      AND ST_DWithin(
              e.current_geom,
              ST_SetSRID(ST_MakePoint(p.port_lng, p.port_lat), 4326)::geography,
              $2
      )
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, entity_id, domain, decay_half_life_min
)
SELECT
    'port_arrival'                                     AS event_type,
    cap.country                                        AS event_subtype,
    cap.current_position_time                          AS event_time,
    ST_SetSRID(ST_MakePoint(cap.port_lng, cap.port_lat), 4326)::geography AS geom,
    CASE
        WHEN cap.port_kind = 'strategic' THEN 7
        ELSE 4
    END                                                AS severity,
    COALESCE(cap.vessel_name, cap.vessel_canonical_id) ||
        ' arrived at ' || cap.port_name                AS title,
    'Vessel newly within ' || ROUND(cap.distance_m::numeric, 0) ||
        ' m of ' || cap.port_name || ' (' || cap.country ||
        '); v=' || ROUND(COALESCE(cap.last_velocity_ms, 0)::numeric, 1) ||
        ' m/s; first port_call in window'                AS description,
    jsonb_build_object(
        'algorithm',          $4::text,
        'vessel_id',          cap.vessel_id::text,
        'vessel_canonical_id', cap.vessel_canonical_id,
        'vessel_name',        cap.vessel_name,
        'port_id',            cap.port_id,
        'port_name',          cap.port_name,
        'port_country',       cap.country,
        'port_kind',          cap.port_kind,
        'distance_m',         ROUND(cap.distance_m::numeric, 0)::int,
        'velocity_ms',        ROUND(COALESCE(cap.last_velocity_ms, 0)::numeric, 2)::float,
        'transition',         'arrival'
    )                                                  AS properties,
    cap.vessel_id                                      AS entity_id,
    'maritime'                                         AS domain,
    1440                                               AS decay_half_life_min
FROM currently_at_port cap
WHERE
    -- "stationary enough" check (matches port_call scan)
    cap.last_velocity_ms IS NOT NULL
    AND cap.last_velocity_ms < $3
    -- No prior port_call (or arrival) for this (vessel, port) within
    -- arrival_lookback_h. This is what makes it an "arrival" rather
    -- than a "still here" — the gap.
    AND NOT EXISTS (
        SELECT 1 FROM event prior
        WHERE prior.event_type IN ('port_call', 'port_arrival')
          AND prior.entity_id  = cap.vessel_id
          AND prior.properties->>'port_id' = cap.port_id
          AND prior.event_time >= NOW() - ($5::int * INTERVAL '1 hour')
    )
"""


PORT_DEPARTURE_SCAN_SQL = """
WITH ports(port_id, port_name, country, port_lat, port_lng, port_kind) AS (
    VALUES {VALUES_CLAUSE}
),
recently_at_port AS (
    -- Vessels that had a port_call OR port_arrival event in the last
    -- departure_lookback_h. We look up each one's CURRENT position to
    -- decide whether they've left.
    SELECT DISTINCT
        prior.entity_id                                 AS vessel_id,
        prior.properties->>'port_id'                    AS port_id,
        MAX(prior.event_time)                            AS last_at_port_time
    FROM event prior
    WHERE prior.event_type IN ('port_call', 'port_arrival')
      AND prior.event_time >= NOW() - ($4::int * INTERVAL '1 hour')
      AND prior.entity_id IS NOT NULL
    GROUP BY 1, 2
),
joined AS (
    SELECT
        rap.vessel_id,
        rap.port_id,
        rap.last_at_port_time,
        e.canonical_id    AS vessel_canonical_id,
        e.display_name    AS vessel_name,
        e.current_geom,
        e.current_position_time,
        p.port_name, p.country, p.port_kind, p.port_lat, p.port_lng,
        -- Distance from CURRENT vessel position to the port reference
        ST_Distance(
            e.current_geom,
            ST_SetSRID(ST_MakePoint(p.port_lng, p.port_lat), 4326)::geography
        ) AS distance_m
    FROM recently_at_port rap
    JOIN entity e ON e.id = rap.vessel_id
    JOIN ports  p ON p.port_id = rap.port_id
    WHERE e.current_geom IS NOT NULL
      -- The fresh-position guard: the vessel must have broadcast in
      -- the last fresh_window_min. Otherwise we can't distinguish
      -- "departed" from "AIS went dark" - dark_ship handles that.
      AND e.current_position_time >= NOW() - ($1::int * INTERVAL '1 minute')
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, entity_id, domain, decay_half_life_min
)
SELECT
    'port_departure'                                   AS event_type,
    j.country                                          AS event_subtype,
    j.current_position_time                            AS event_time,
    ST_SetSRID(ST_MakePoint(j.port_lng, j.port_lat), 4326)::geography AS geom,
    CASE
        WHEN j.port_kind = 'strategic' THEN 7
        ELSE 4
    END                                                AS severity,
    COALESCE(j.vessel_name, j.vessel_canonical_id) ||
        ' departed ' || j.port_name                    AS title,
    'Vessel last at port ' ||
        EXTRACT(EPOCH FROM (NOW() - j.last_at_port_time))::int / 60 ||
        ' min ago; now ' || ROUND(j.distance_m::numeric, 0) ||
        ' m from ' || j.port_name || ' (' || j.country || ')' AS description,
    jsonb_build_object(
        'algorithm',          $3::text,
        'vessel_id',          j.vessel_id::text,
        'vessel_canonical_id', j.vessel_canonical_id,
        'vessel_name',        j.vessel_name,
        'port_id',            j.port_id,
        'port_name',          j.port_name,
        'port_country',       j.country,
        'port_kind',          j.port_kind,
        'distance_m',         ROUND(j.distance_m::numeric, 0)::int,
        'last_at_port_ts',    to_char(j.last_at_port_time AT TIME ZONE 'UTC',
                                      'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
        'transition',         'departure'
    )                                                  AS properties,
    j.vessel_id                                        AS entity_id,
    'maritime'                                         AS domain,
    1440                                               AS decay_half_life_min
FROM joined j
WHERE
    -- vessel is now BEYOND the at-port radius (departed)
    j.distance_m > $2
    -- Idempotent: don't re-emit a departure already logged for this
    -- (vessel, port) since the most recent at-port event.
    AND NOT EXISTS (
        SELECT 1 FROM event existing
        WHERE existing.event_type = 'port_departure'
          AND existing.entity_id  = j.vessel_id
          AND existing.properties->>'port_id' = j.port_id
          AND existing.event_time > j.last_at_port_time
    )
"""


async def run_port_arrival_scan(
    *,
    radius_m: int = 5_000,
    fresh_window_min: int = 60,
    at_port_max_velocity_ms: float = 1.5,
    arrival_lookback_h: int = 168,        # 7 days
    algorithm_tag: str = "port_arrival_v1",
) -> int:
    """Detect vessel arrivals at major ports. Fires once per arrival
    (gap between port_call/port_arrival events for this pair must
    exceed arrival_lookback_h). Severity 7 for strategic ports, 4
    for commercial — higher than continuous port_call because the
    transition itself is the news."""
    if not PORTS:
        return 0
    sql = PORT_ARRIVAL_SCAN_SQL.replace("{VALUES_CLAUSE}",
                                         _build_values_clause())
    async with acquire_write() as conn:
        result = await conn.execute(
            sql,
            fresh_window_min,
            radius_m,
            at_port_max_velocity_ms,
            algorithm_tag,
            arrival_lookback_h,
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"port_arrival scan: {count} new arrivals "
            f"(lookback={arrival_lookback_h}h)"
        )
    return count


async def run_port_departure_scan(
    *,
    radius_m: int = 5_000,
    fresh_window_min: int = 60,
    departure_lookback_h: int = 36,
    algorithm_tag: str = "port_departure_v1",
) -> int:
    """Detect vessel departures from major ports. A vessel that had a
    port_call event in the last `departure_lookback_h` AND now has a
    fresh position OUTSIDE radius_m of that port → departure.

    The fresh-position guard distinguishes "departed" from "AIS went
    dark" — the latter is the dark_ship algorithm's territory. Without
    this guard, every dark vessel would emit a fake departure.
    """
    if not PORTS:
        return 0
    sql = PORT_DEPARTURE_SCAN_SQL.replace("{VALUES_CLAUSE}",
                                           _build_values_clause())
    async with acquire_write() as conn:
        result = await conn.execute(
            sql,
            fresh_window_min,
            radius_m,
            algorithm_tag,
            departure_lookback_h,
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"port_departure scan: {count} new departures "
            f"(lookback={departure_lookback_h}h)"
        )
    return count
