#!/usr/bin/env python3
"""
Smoke-test script for every Glassbox ingester.

What it does:
  1. Loads infra/sources.yaml via SourcesRegistry
  2. For each ingester:
     - Runs gate_ingester() — prints PASS/REFUSE
     - If PASS: runs fetch() once + normalize() once (NO broadcaster, NO classifier)
     - Reports raw_count, normalized_count, latency_ms, error if any
  3. Exits 0 if all PASS-gated ingesters either return events OR cleanly return [] without exception
  4. Exits 1 if any PASS-gated ingester threw an exception

Usage:
    cd "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC"
    python3 21_GLASSBOX_AI/scripts/smoke_test_ingesters.py

Optional env vars (otherwise the embedded fallback keys are used):
    NASA_API_KEY=...
    NASA_FIRMS_MAP_KEY=...
    WAQI_API_TOKEN=...
    BARENTSWATCH_CLIENT_ID=... + BARENTSWATCH_CLIENT_SECRET=...
    AISSTREAM_API_KEY=...

Exit codes:
    0 = all good (PASS-gated ingesters returned data without exception)
    1 = at least one PASS-gated ingester threw

This is INTENDED to hit live APIs. It will:
    - Make 17 outbound HTTP calls
    - Run for ~60 seconds total
    - Generate real network traffic
Don't run on a metered connection.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Any, List, Tuple

# Make the local 21_GLASSBOX_AI package importable when run as a script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sources_registry import SourcesRegistry, gate_ingester             # noqa: E402

# Import every ingester
from ingesters.planes import PlanesIngester                              # noqa: E402
from ingesters.ships import ShipsIngester                                # noqa: E402
from ingesters.earthquakes import EarthquakesIngester                    # noqa: E402
from ingesters.satellites import SatellitesIngester                      # noqa: E402
from ingesters.gdelt import GDELTIngester                                # noqa: E402
from ingesters.gdelt_topical import GDELTTopicalIngester                 # noqa: E402
from ingesters.citizen_adapter import CitizenOSINTAdapter, TrafficCamsAdapter  # noqa: E402
from ingesters.police_incidents import PoliceIncidentsIngester           # noqa: E402
from ingesters.noaa_nws import NoaaNwsIngester                           # noqa: E402
from ingesters.nasa_eonet import NasaEonetIngester                       # noqa: E402
from ingesters.emsc_fdsn import EmscFdsnIngester                         # noqa: E402
from ingesters.ofac_sdn import OfacSdnIngester                           # noqa: E402
from ingesters.nasa_firms import NasaFirmsIngester                       # noqa: E402
from ingesters.waqi_aqi import WaqiAqiIngester                           # noqa: E402
from ingesters.nasa_neo import NasaNeoIngester                           # noqa: E402
from ingesters.nasa_donki import NasaDonkiIngester                       # noqa: E402
from ingesters.ourairports import OurAirportsIngester                    # noqa: E402
from ingesters.noaa_aviation_weather import NoaaAviationWeatherIngester  # noqa: E402
from ingesters.sec_edgar import SecEdgarIngester                         # noqa: E402
from ingesters.bluesky_jetstream import BlueskyJetstreamIngester          # noqa: E402
from ingesters.newsdata_io import NewsDataIoIngester                       # noqa: E402


logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
log = logging.getLogger("smoke")


def _build_all() -> List[Any]:
    """Instantiate every ingester with smoke_mode=True so they reduce work
    (fewer tiles, single query, capped result count). Production runs them
    with smoke_mode=False (default) for full data pulls.

    Architectural separation:
      - smoke_mode=True  → ~30s total verification (this test)
      - smoke_mode=False → minutes-long full pulls (per-ingester poll cycle in prod)

    See base.Ingester docstring for details."""
    kwargs = {"smoke_mode": True}
    return [
        PlanesIngester(**kwargs),               # 8 tiles vs 85
        ShipsIngester(**kwargs),                # already fast
        EarthquakesIngester(**kwargs),
        SatellitesIngester(**kwargs),           # 1 group vs 4
        GDELTIngester(**kwargs),                # 1 query vs 2
        GDELTTopicalIngester(**kwargs),         # 1 query vs 3
        CitizenOSINTAdapter(),                  # refused at gate; no smoke arg needed
        TrafficCamsAdapter(),
        PoliceIncidentsIngester(),
        NoaaNwsIngester(**kwargs),
        NasaEonetIngester(**kwargs),
        EmscFdsnIngester(**kwargs),
        OfacSdnIngester(**kwargs),              # cap 500 records vs 19k
        NasaFirmsIngester(**kwargs),
        WaqiAqiIngester(**kwargs),
        NasaNeoIngester(**kwargs),
        NasaDonkiIngester(**kwargs),
        OurAirportsIngester(**kwargs),          # 100 records vs 3,308
        NoaaAviationWeatherIngester(**kwargs),
        SecEdgarIngester(**kwargs),
        NewsDataIoIngester(**kwargs),           # 5 articles vs 10 in smoke
        # Bluesky listens on a WebSocket for 5min per cycle in production.
        # Even smoke mode would block. Skip entirely.
        # BlueskyJetstreamIngester(**kwargs),
    ]


async def _smoke_one(ing: Any) -> Tuple[str, int, int, int, str]:
    """Returns (verdict, raw_count, normalized_count, latency_ms, error)."""
    t0 = time.time()
    try:
        raw = await ing.fetch()
        raw_count = len(raw) if raw else 0
        normalized = ing.normalize(raw or [])
        norm_count = len(normalized) if normalized else 0
        elapsed_ms = int((time.time() - t0) * 1000)
        return ("OK", raw_count, norm_count, elapsed_ms, "")
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        msg = f"{type(e).__name__}: {e}"
        return ("ERROR", 0, 0, elapsed_ms, msg[:200])


async def main() -> int:
    print("=" * 80)
    print(" GLASSBOX INGESTER SMOKE TEST")
    print(" Loading infra/sources.yaml + running fetch+normalize on every ingester")
    print(" Note: this hits live APIs. Do not run on metered connection.")
    print("=" * 80)
    print()

    registry = SourcesRegistry.load()
    if not registry.loaded_ok:
        print(f"FATAL: sources.yaml did not load — {registry.load_error}")
        return 1
    print(f"Registry: {registry.enabled_count()} enabled / {registry.disabled_count()} disabled")
    print()

    candidates = _build_all()

    # Phase 1: gate-test all
    print(f"{'Ingester':<32} {'source_id':<32} {'gate':<8}")
    print("-" * 80)
    activated: List[Any] = []
    for ing in candidates:
        sid = getattr(ing, "source_id", "")
        ok, reason = gate_ingester(ing, registry)
        verdict = "PASS" if ok else "REFUSE"
        print(f"{ing.__class__.__name__:<32} {sid:<32} {verdict:<8}")
        if ok:
            activated.append(ing)
        else:
            print(f"{'':32} {'':32} reason: {reason[:60]}")
    print()
    print(f"Gate result: {len(activated)} activated, {len(candidates) - len(activated)} refused")
    print()

    # Phase 2: fetch+normalize each activated ingester
    print(f"{'Ingester':<32} {'verdict':<10} {'raw':<8} {'norm':<8} {'ms':<8}  error")
    print("-" * 80)
    any_error = False
    for ing in activated:
        verdict, raw, norm, ms, err = await _smoke_one(ing)
        print(f"{ing.__class__.__name__:<32} {verdict:<10} {raw:<8} {norm:<8} {ms:<8}  {err}")
        if verdict == "ERROR":
            any_error = True

    print()
    print("=" * 80)
    if any_error:
        print("RESULT: at least one ingester THREW. See ERROR rows above.")
        print("Exit code 1.")
        return 1
    print(f"RESULT: all {len(activated)} activated ingesters completed without exception.")
    print("Note: 0-count rows are NOT failures — endpoint may legitimately have no data right now.")
    print("Exit code 0.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
