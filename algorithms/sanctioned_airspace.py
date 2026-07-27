"""
Sanctioned-airspace routing detection — Phase 4 algorithm #7.

Strategic context: aircraft transiting heavily sanctioned airspace are doing
something specific. Most western carriers avoid Iran FIR, North Korea,
Syria, and Russian-occupied Crimea entirely. When an aircraft DOES appear
there, it's signal:
  - Iranian/Russian cargo carriers routing weapons or sanctioned goods
  - NK military overflights (extremely rare on public ADS-B)
  - Russian transport aircraft in occupied Crimea
  - Western-tagged aircraft "going dark" through Iran (sanctions evasion)
  - Diplomatic / leadership aircraft crossing for negotiations

2026-05-19 (P0-C audit #10): replaced crude axis-aligned bounding boxes
with tighter multi-vertex polygons. The original bboxes leaked badly into
non-sanctioned neighboring states — measured ground-truth on a 14-day live
sample showed 76.7% FP rate. Concrete leaks observed:
  * "iran" bbox (44/25 → 63/40) included entire Persian Gulf:
      ~60% of findings were Dubai/UAE proper (lat=25.25, lng=55.38);
      ~21% Saudi Arabia eastern province; ~7% Doha/Qatar; ~6% Bahrain.
      Only 4.5% (166/3685) actually inside Iran.
  * "syria" bbox (35/32 → 43/37) included Lebanon, the eastern Med Sea,
      Israel, Jordan, and parts of Turkey. Only ~28% actually in Syria.
  * "belarus" bbox (23.2/51.2 → 32.8/56.2) included most of Lithuania
      (NATO/EU). Of 110 findings only 5 (4.5%) actually in Belarus —
      ~92% in Lithuania.
  * "cuba" bbox extended north to lat=23.5, including the Florida Strait
      and Florida Keys (~15% of findings).
The bboxes also overlapped major civilian transit corridors and arrival
patterns for hub airports (Dubai DXB, Doha DOH, Bahrain BAH, Vilnius VNO,
Riyadh RUH) that have nothing to do with sanctioned airspace.

The fix below uses concave country polygons that hug actual political
borders more closely. They are still approximations (real ICAO FIRs are
more complex), but they exclude the Persian Gulf Arab states from "iran",
Lebanon/Israel/Jordan/Med from "syria", Lithuania from "belarus", and the
Florida Strait from "cuba". Test fixtures at lat=32/lng=53 (central
Iran), lat=22/lng=-79 (central Cuba), lat=40/lng=127 (central NK),
lat=45/lng=34 (central Crimea) remain inside the tightened polygons.

The brief surfaces these as a tier-1 callout grouped by zone with sample
callsigns + ICAO hex codes.

Idempotency: each (aircraft, zone, day) flagged at most once.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.sanctioned_airspace")


# Tightened sanctioned-airspace polygons (lng lat pairs, WKT-order).
# Each polygon is a concave hull approximating actual country borders.
# Vertices walked counter-clockwise starting near a recognizable landmark.
#
# Severity is set further down (was per-zone in the SQL CASE statement).
SANCTIONED_ZONE_POLYGONS: list[tuple[str, str, str]] = [
    # (name, polygon_wkt, label)
    (
        "iran",
        # Iran proper: excludes Persian Gulf Arab states (UAE, Qatar, Bahrain,
        # Saudi Arabia, Kuwait, Oman) and Iraq.  Vertices follow:
        # NW border with Turkey/Iraq, west along Iraqi border, down to
        # Persian Gulf at southern Iraq/Iran border, then along the Iranian
        # coast of the Persian Gulf (hugging the Iranian shore, NOT the
        # middle of the gulf), Strait of Hormuz, Gulf of Oman, southeast
        # corner near Pakistan, north along Pakistan/Afghanistan border, then
        # northwest along Turkmenistan border, back to the NW corner.
        "POLYGON(("
        "44.5 39.5, "   # NW corner (Turkey/Armenia/Iran border)
        "45.5 36.5, "   # Iran/Iraq border northern stretch
        "46.0 33.0, "   # central Iran/Iraq border
        "47.5 30.5, "   # southern Iran/Iraq border
        "49.0 30.0, "   # Iranian Persian Gulf coast, near Abadan
        "50.5 29.0, "   # Iranian Gulf coast (north of Kuwait, on Iran side)
        "52.5 27.5, "   # Iranian Gulf coast (north of Qatar/Bahrain)
        "55.0 26.5, "   # Iranian Gulf coast (north of UAE, Bandar Abbas area)
        "57.0 26.5, "   # Strait of Hormuz, Iranian side
        "59.5 25.4, "   # Gulf of Oman, Iranian coast
        "61.6 25.1, "   # Iranian coast near Chabahar (25.30N 60.65E)
        "61.85 26.2, "  # SE corner, Iran/Pakistan border at coast
        "62.7 26.7, "   # Iran/Pakistan border inland
        "63.3 29.5, "   # Iran/Pakistan border N
        "60.85 31.7, "  # Iran/Afghanistan border S (Sistan)
        "60.6 33.5, "   # Iran/Afghanistan border mid
        "60.85 35.5, "  # Iran/Afghanistan border N
        "60.5 36.6, "   # Iran/Turkmenistan/Afghanistan triangle (NE of Mashhad)
        "57.5 37.5, "   # Iran/Turkmenistan border N (south of Ashgabat which sits at 37.95N)
        "55.0 37.9, "   # Iran/Turkmenistan border N
        "53.5 37.2, "   # Iran/Caspian Sea coast (Gorgan area)
        "51.0 36.7, "   # Iran/Caspian Sea coast central
        "48.9 37.3, "   # Iran/Azerbaijan border on Caspian (Astara)
        "48.5 38.4, "   # Iran/Azerbaijan border
        "44.5 39.5"     # close
        "))",
        "Iran (OIIX FIR — multilateral sanctions, weapons embargo)",
    ),
    (
        "syria",
        # Syria proper: excludes Lebanon (lng<36.6 below lat=34.7), Israel,
        # Jordan, Med Sea offshore (lng<35.7 above lat=34.7).  Walks the
        # Syrian border roughly.
        "POLYGON(("
        "36.0 36.8, "   # NW corner, Turkey border near Mediterranean
        "35.85 35.9, "  # Latakia coast (Syrian Med shore)
        "35.85 34.9, "  # southern Syrian Med shore at Lebanese border
        "36.45 34.6, "  # Anti-Lebanon ridge crest (Lebanon/Syria border)
        "36.10 33.85, " # SW of Damascus (Lebanese/Syrian Anti-Lebanon).
                        # Damascus is at 33.51N 36.30E — vertex placed west
                        # of Damascus + the next vertex south-east keeps
                        # Damascus inside, Beirut (33.9N 35.5E) outside.
        "36.15 33.30, " # south of Damascus toward Jordan border
        "36.9 32.6, "   # Syria/Jordan border (Quneitra/Daraa area)
        "38.0 32.4, "   # Syria/Jordan border east
        "39.0 32.1, "   # SE corner near Iraq
        "40.5 32.0, "   # Iraq border south
        "42.4 33.0, "   # Iraq border central
        "42.4 36.0, "   # Iraq/Turkey/Syria triangle
        "40.0 37.0, "   # Turkey border NE
        "36.5 37.1, "   # Turkey border N
        "36.0 36.8"     # close
        "))",
        "Syria (multilateral arms embargo + civilian conflict)",
    ),
    (
        "north_korea",
        # NK actual borders.  China border NW, sea of Japan E, DMZ S.
        "POLYGON(("
        "124.3 39.8, "  # NW, Yalu River mouth (China/NK border)
        "124.3 40.5, "  # NW China border
        "127.5 41.7, "  # N China border (Mt. Paektu area)
        "129.8 42.5, "  # NE Russia/China/NK triple-point area
        "130.7 42.3, "  # NE coast (Sea of Japan, near Russia border)
        "129.5 39.5, "  # E coast (Wonsan area)
        "128.4 38.6, "  # SE coast near DMZ
        "126.7 37.9, "  # DMZ central
        "124.7 38.0, "  # SW coast near DMZ (Yellow Sea)
        "124.5 39.0, "  # W coast Yellow Sea
        "124.3 39.8"    # close
        "))",
        "North Korea (UN comprehensive sanctions, DPRK)",
    ),
    (
        "crimea",
        # Crimean peninsula proper, excludes the Sea of Azov and the
        # Kerch strait + Russian mainland.
        "POLYGON(("
        "32.5 45.4, "   # NW peninsula tip (Tarkhankut)
        "32.5 45.85, "  # N at the Perekop isthmus (UA mainland border)
        "34.0 46.15, "  # N coast near isthmus
        "35.4 45.95, "  # N Sea of Azov coast
        "36.6 45.45, "  # NE Kerch peninsula
        "36.6 44.95, "  # E coast at Kerch
        "35.5 44.55, "  # S coast Yalta area
        "33.5 44.4, "   # SW coast Sevastopol
        "32.5 44.7, "   # W coast
        "32.5 45.4"     # close
        "))",
        "Crimea (Russian-occupied, UN-recognized as Ukrainian)",
    ),
    (
        "eastern_donbas",
        # Donetsk + Luhansk occupied territory.
        "POLYGON(("
        "36.6 47.2, "   # SW corner, Sea of Azov coast (Mariupol area)
        "36.6 49.3, "   # NW corner near Kharkiv oblast
        "40.0 49.5, "   # NE corner Russia/Ukraine border
        "40.2 47.85, "  # E central Russian border
        "39.0 47.1, "   # SE Sea of Azov coast Russian border
        "36.6 47.2"     # close
        "))",
        "Russian-occupied eastern Ukraine (Donetsk + Luhansk)",
    ),
    (
        "cuba",
        # Cuba main island ONLY.  Excludes Florida Strait (north of
        # lat=23.3), Bahamas (east of lng=-74.5), and Yucatan Channel
        # (west of lng=-85).
        "POLYGON(("
        "-84.95 21.85, " # W tip (Cabo San Antonio)
        "-84.0 23.2, "   # NW coast Pinar del Rio
        "-82.5 23.25, "  # N coast Havana area
        "-80.5 23.15, "  # N coast Matanzas/Cardenas
        "-78.5 22.4, "   # N coast Cayo Coco area
        "-77.0 21.5, "   # NE coast Holguin
        "-75.6 21.0, "   # NE coast near Baracoa
        "-74.13 20.3, "  # E tip (Punta de Maisi)
        "-75.0 19.85, "  # SE coast Santiago de Cuba
        "-77.2 19.95, "  # S coast
        "-78.5 20.5, "   # S coast
        "-80.0 21.1, "   # S coast
        "-82.0 21.4, "   # SW coast Bay of Pigs
        "-83.5 21.6, "   # SW coast
        "-84.95 21.85"   # close
        "))",
        "Cuba (US OFAC comprehensive sanctions)",
    ),
    (
        "belarus",
        # Belarus proper.  Excludes Lithuania (which uses lng > 23.2 only
        # along narrow southeastern strip — the entire Vilnius area sits
        # at lng=25.28 lat=54.69 which is INSIDE the old bbox but actually
        # in Lithuania).  The fix walks the Belarus political border.
        "POLYGON(("
        "23.2 53.9, "   # NW corner, Belarus/Poland/Lithuania tripoint
        "25.5 54.35, "  # N border with Lithuania
        "26.8 55.15, "  # N-NE border with Lithuania
        "28.3 56.15, "  # N border with Latvia
        "30.0 55.85, "  # NE border with Russia
        "31.4 54.6, "   # E border with Russia
        "32.7 53.3, "   # SE border with Russia/Ukraine
        "31.5 52.1, "   # S border with Ukraine
        "30.0 51.4, "   # S Ukraine border (Chernobyl area)
        "27.7 51.6, "   # S Ukraine border
        "23.7 51.5, "   # SW corner with Poland/Ukraine
        "23.2 52.7, "   # W Poland border
        "23.2 53.9"     # close
        "))",
        "Belarus (EU + UK + US sanctions, Russian Belarus deployment)",
    ),
    (
        "south_sudan",
        # South Sudan proper.
        "POLYGON(("
        "24.15 8.7, "   # W border with CAR
        "24.8 12.0, "   # NW border with Sudan
        "27.9 9.6, "    # N border with Sudan
        "32.4 11.7, "   # N border with Sudan
        "33.9 10.0, "   # NE border with Sudan/Ethiopia
        "35.3 8.6, "    # E border with Ethiopia
        "35.95 5.0, "   # SE corner Ethiopia/Kenya
        "33.95 3.5, "   # S border with Kenya/Uganda
        "30.85 3.5, "   # S border with Uganda
        "29.9 4.4, "    # SW border with DRC
        "27.0 5.6, "    # SW border with DRC/CAR
        "25.3 7.9, "    # W border with CAR
        "24.15 8.7"     # close
        "))",
        "South Sudan (US arms embargo + UN sanctions)",
    ),
    (
        "yemen",
        # Yemen proper.  Excludes Bab-el-Mandeb maritime corridor in the
        # middle of the strait, but includes Yemeni airspace inland and
        # along the Yemeni coast.
        "POLYGON(("
        "43.0 17.5, "   # NW border with Saudi Arabia
        "43.5 16.7, "   # W Red Sea coast (Hodeidah)
        "43.2 15.2, "   # SW Red Sea coast
        "43.4 12.65, "  # SW coast Bab-el-Mandeb
        "44.8 12.5, "   # S coast Aden
        "47.0 13.0, "   # S coast Mukalla
        "49.5 14.4, "   # SE coast
        "52.5 15.5, "   # SE coast
        "53.1 16.6, "   # SE corner Oman border
        "52.0 19.0, "   # E border with Oman/Saudi Arabia
        "47.6 18.95, "  # N border with Saudi Arabia
        "44.5 18.2, "   # N border with Saudi Arabia
        "43.0 17.5"     # close
        "))",
        "Yemen (multilateral arms embargo)",
    ),
    (
        "libya",
        # Libya proper.  Excludes most of the Med Sea offshore corridor.
        "POLYGON(("
        "9.5 30.3, "    # W border with Algeria/Tunisia
        "10.5 33.2, "   # NW corner Med coast Tunisia
        "12.4 32.85, "  # N Med coast Tripoli
        "15.3 32.3, "   # N Med coast Misrata
        "20.0 32.4, "   # N Med coast Benghazi
        "23.0 32.6, "   # N Med coast Tobruk
        "24.95 31.6, "  # NE corner Egypt border at coast
        "25.0 29.0, "   # E border with Egypt
        "25.0 22.0, "   # E border with Egypt south
        "23.95 19.6, "  # SE corner Egypt/Sudan border
        "19.8 21.5, "   # S border with Chad
        "15.0 23.0, "   # S border with Chad/Niger
        "10.5 24.5, "   # SW border with Niger/Algeria
        "9.5 26.5, "    # W border with Algeria
        "9.5 30.3"      # close
        "))",
        "Libya (UN arms embargo, civil conflict)",
    ),
]

# Legacy bbox-form list (kept for any external callers that still reference
# SANCTIONED_ZONES as 6-tuples; populated from the polygons' bounding boxes
# for back-compat — used nowhere internally after the 2026-05-19 rewrite).
SANCTIONED_ZONES = [
    # name, bbox_min_lng, bbox_min_lat, bbox_max_lng, bbox_max_lat, label
    ("iran",            44.5, 25.5, 62.5, 39.5,
        "Iran (OIIX FIR — multilateral sanctions, weapons embargo)"),
    ("syria",           35.85, 32.0, 42.4, 37.1,
        "Syria (multilateral arms embargo + civilian conflict)"),
    ("north_korea",     124.3, 37.9, 130.7, 42.5,
        "North Korea (UN comprehensive sanctions, DPRK)"),
    ("crimea",          32.5, 44.4, 36.6, 46.15,
        "Crimea (Russian-occupied, UN-recognized as Ukrainian)"),
    ("eastern_donbas",  36.6, 47.1, 40.2, 49.5,
        "Russian-occupied eastern Ukraine (Donetsk + Luhansk)"),
    ("cuba",            -84.95, 19.85, -74.13, 23.25,
        "Cuba (US OFAC comprehensive sanctions)"),
    ("belarus",         23.2, 51.4, 32.7, 56.15,
        "Belarus (EU + UK + US sanctions, Russian Belarus deployment)"),
    ("south_sudan",     24.15, 3.5, 35.95, 12.0,
        "South Sudan (US arms embargo + UN sanctions)"),
    ("yemen",           43.0, 12.5, 53.1, 19.0,
        "Yemen (multilateral arms embargo)"),
    ("libya",           9.5, 19.6, 25.0, 33.2,
        "Libya (UN arms embargo, civil conflict)"),
]


# Build the SQL VALUES clause from the polygon constant.  Each row is
# (zone_name, geometry).  Use the proper WKT polygon rather than a bbox so
# that ST_Within excludes the formerly-overrun neighbor states.
def _build_zones_values_clause() -> str:
    parts = []
    for (name, polygon_wkt, _label) in SANCTIONED_ZONE_POLYGONS:
        parts.append(f"('{name}', ST_SetSRID(ST_GeomFromText('{polygon_wkt}'), 4326))")
    return ",\n        ".join(parts)


_ZONES_VALUES = _build_zones_values_clause()
_ZONE_LABELS = {z[0]: z[2] for z in SANCTIONED_ZONE_POLYGONS}


SANCTIONED_AIRSPACE_SQL = f"""
WITH zones(zone_name, zone_geom) AS (
    VALUES
        {_ZONES_VALUES}
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'aircraft_in_sanctioned_airspace'                          AS event_type,
    z.zone_name                                                AS event_subtype,
    NOW()                                                      AS event_time,
    e.current_geom                                             AS geom,
    -- Severity: NK is the highest-signal at 10; others 7-9.
    CASE
        WHEN z.zone_name = 'north_korea' THEN 10.0
        WHEN z.zone_name IN ('crimea', 'eastern_donbas', 'syria') THEN 9.0
        WHEN z.zone_name IN ('iran', 'libya', 'yemen', 'south_sudan') THEN 8.0
        ELSE 7.0
    END::real                                                   AS severity,
    'Aircraft in sanctioned airspace: ' ||
        COALESCE(e.display_name, 'ICAO ' || e.canonical_id) ||
        ' over ' || z.zone_name                                AS title,
    'Aircraft (ICAO ' || e.canonical_id || ') currently broadcasting from '
        'sanctioned airspace: ' || z.zone_name ||
        COALESCE(' as ' || e.display_name, '') ||
        '. Origin: ' || COALESCE(e.properties->>'origin_country', 'unknown') ||
        '. Mil flag: ' || COALESCE(e.properties->>'military', 'false')
                                                               AS description,
    jsonb_build_object(
        'algorithm',           $1::text,
        'icao24',              e.canonical_id,
        'callsign',            e.display_name,
        'zone',                z.zone_name,
        'origin_country',      e.properties->>'origin_country',
        'military',            e.properties->>'military',
        'last_seen',           e.current_position_time,
        'lat',                 ST_Y(e.current_geom::geometry),
        'lng',                 ST_X(e.current_geom::geometry)
    )                                                          AS properties,
    'aviation'                                                 AS domain,
    1440                                                       AS decay_half_life_min,
    e.id                                                       AS entity_id
FROM entity e
JOIN zones z ON ST_Within(e.current_geom::geometry, z.zone_geom)
WHERE e.entity_type = 'aircraft'
  AND e.current_geom IS NOT NULL
  AND e.current_position_time IS NOT NULL
  AND e.current_position_time >= $2::timestamptz
  AND ($4::text IS NULL OR e.canonical_id LIKE $4)
  AND NOT EXISTS (
      SELECT 1 FROM event finding
      WHERE finding.event_type = 'aircraft_in_sanctioned_airspace'
        AND finding.properties->>'algorithm' = $1
        AND finding.entity_id = e.id
        AND finding.event_subtype = z.zone_name
        AND finding.event_time >= $3::timestamptz
  )
"""


async def run_sanctioned_airspace_scan(
    *,
    lookback_min: int = 60,
    dedup_window_hours: int = 24,
    algorithm_tag: str = "sanctioned_airspace",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Find aircraft currently in sanctioned airspace.

    Args:
        lookback_min: aircraft must have broadcast within this many minutes.
            Default 60.
        dedup_window_hours: same (aircraft, zone) flagged at most once per
            this window. Default 24h. Re-entry to same zone next day = new
            event.
        algorithm_tag: tagged into properties.algorithm for dedup.
        entity_canonical_id_like: optional ICAO LIKE pattern for tests.

    Returns:
        Count of `aircraft_in_sanctioned_airspace` rows inserted.
    """
    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(minutes=lookback_min)
    dedup_cutoff  = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            SANCTIONED_AIRSPACE_SQL,
            algorithm_tag,             # $1
            active_cutoff,             # $2
            dedup_cutoff,              # $3
            entity_canonical_id_like,  # $4
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"sanctioned-airspace scan: {count} aircraft flagged "
            f"(lookback={lookback_min}min)"
        )
    return count
