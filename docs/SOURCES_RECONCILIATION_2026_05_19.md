# Sources Reconciliation — 2026-05-19

**Backlog item:** [P0-B](../../GLASSBOX_BACKEND_BACKLOG.md#p0-b-—-reconcile-infrasourcesyaml-against-actual-ingester-startup-list)
**Authored by:** P0-B reconciliation pass
**Inputs:**
- `infra/sources.yaml` (84 source entries)
- `21_GLASSBOX_AI/glassbox_server.py` `candidate_ingesters` list (30 ingester classes)
- Live daemon `/api/v1/health/full` `ingesters.items[]` snapshot at 2026-05-19 16:30 UTC

## TL;DR

**Before P0-B:** 86 source entries, 43 `enabled: true`, 30 registered ingesters → a 13-source over-claim where `enabled: true` named upstream sources that had no implementation. Plus two duplicate-id pairs (`gdacs`, `hacker_news`) producing silent last-write-wins behavior at YAML load.

**After P0-B:** 84 unique source ids, **30 `enabled: true`** matching 30 registered ingester classes, **54 `enabled: false`** all carrying a `disabled_reason`. No duplicates.

The daemon's `candidate_ingesters` startup list and the `enabled: true` set in `sources.yaml` are now in 1:1 agreement (modulo the multi-source `ships` and `sanctions` layers where one ingester serves multiple upstream sources, documented below).

## Reconciliation table

### Active — ingester registered, running, emitting (29 sources / 30 ingester classes)

| sources.yaml id | Ingester class | Layer in daemon | Cycles | Notes |
|---|---|---|---|---|
| `adsb_lol` | `PlanesIngester` | `planes` | 6,962 | Primary aircraft (replaced OpenSky as default) |
| `aisstream` | `AISStreamIngester` | `ships` | 1,519 | Global firehose; WebSocket |
| `barentswatch_ais` | `ShipsIngester` | `ships` | 7,792 | Multi-source ingester also serves Digitraffic + DMA |
| `bluesky_jetstream` | `BlueskyJetstreamIngester` | `social_bluesky` | 1,569 | ATProto public firehose; WebSocket |
| `celestrak` | `SatellitesIngester` | `satellites` | 14,937 | TLE + server-side SGP4 propagation |
| `digitraffic_finland` | `ShipsIngester` | `ships` | (shared) | One of three AIS sources combined in ShipsIngester |
| `dma_denmark_ais` | `ShipsIngester` | `ships` | (shared) | One of three AIS sources combined in ShipsIngester |
| `emsc_fdsn_event` | `EmscFdsnIngester` | `earthquakes` | 787 | CC BY 4.0; DOI 10.17616/R3N93X |
| `eu_cfsp` | `EuCfspIngester` | `sanctions` | 132 | EU Consolidated Sanctions |
| `gdacs` | `GdacsIngester` | `gdacs` | 787 | RSS feed variant (canonical); duplicate `gdacs` API block removed |
| `gdelt_bulk` | `GdeltBulkIngester` | `news` | 1,574 | 15-min bulk CSV; replaced rate-limit-hostile GDELT API |
| `hacker_news` | `HackerNewsIngester` | `news` | 1,574 | CC0; duplicate stale block removed |
| `nasa_donki` | `NasaDonkiIngester` | `space_weather` | 132 | DONKI Space Weather DB |
| `nasa_eonet` | `NasaEonetIngester` | `natural_events` | 263 | **CURRENTLY DOWN — see operational notes** |
| `nasa_firms` | `NasaFirmsIngester` | `wildfires` | 263 | MODIS + VIIRS active fire detections |
| `nasa_neo` | `NasaNeoIngester` | `neo_asteroids` | 22 | 6-hour poll cadence |
| `newsdata_io` | `NewsDataIoIngester` | `news` | 263 | Free tier, commercial OK |
| `noaa_aviation_weather` | `NoaaAviationWeatherIngester` | `metar` | 1,574 | aviationweather.gov |
| `noaa_nhc` | `NhcStormsIngester` | `tropical_storms` | 1,574 | National Hurricane Center |
| `noaa_nws` | `NoaaNwsIngester` | `weather_alerts` | 1,574 | api.weather.gov |
| `noaa_swpc` | `NoaaSwpcIngester` | `space_weather` | 1,574 | Space Weather Prediction Center |
| `ofac_sdn` | `OfacSdnIngester` | `sanctions` | 132 | US Treasury OFAC Specially Designated Nationals |
| `openfema` | `OpenFemaIngester` | `fema_declarations` | 263 | **CURRENTLY DOWN — see operational notes** |
| `ourairports` | `OurAirportsIngester` | `airports` | 6 | CC0; daily-cadence reference data |
| `sec_edgar` | `SecEdgarIngester` | `securities_filings` | 1,574 | US public domain |
| `uk_ofsi` | `UkOfsiIngester` | `sanctions` | 132 | UK Office of Financial Sanctions Implementation |
| `uk_sanctions_list` | `UkOfsiIngester` | `sanctions` | (shared) | Served by UkOfsiIngester (same upstream XML; deduplicated by ingester) |
| `usgs_earthquakes` | `EarthquakesIngester` | `earthquakes` | 7,866 | USGS Earthquake Hazards Program |
| `usgs_volcano` | `UsgsVolcanoIngester` | `volcanic_activity` | 525 | USGS Volcano Hazards Program |
| `waqi_aqi` | `WaqiAqiIngester` | `air_quality` | 787 | World Air Quality Index |

**Ingester classes registered but no `sources.yaml` source enabled for them:** *(none — full coverage)*.

**Note on multi-source layers:** the daemon's `health/full` output shows a `layer` field that doesn't map 1:1 to sources.yaml `id`. Two layers explicitly aggregate multiple upstreams:
- `ships` is fed by both `ShipsIngester` (combining Digitraffic + BarentsWatch + DMA national AIS feeds) AND `AISStreamIngester` (global firehose).
- `sanctions` is fed by `OfacSdnIngester` + `UkOfsiIngester` + `EuCfspIngester` (with `uk_sanctions_list` riding the same UkOfsiIngester pull).
- `news` is fed by `GdeltBulkIngester` + `HackerNewsIngester` + `NewsDataIoIngester`.

### Operational issues surfaced during P0-B (not P0-B scope to fix; flagged for ops triage)

| Source | Symptom | Likely root cause |
|---|---|---|
| `openfema` | `health=down`, `sla_breach=True`, last fetch 2026-05-19T09:45 UTC, error: `503 Service Unavailable from FEMA API` | Transient FEMA upstream outage; ingester is correctly bubbling up the failure. No action needed unless the 503 persists >24h. |
| `nasa_eonet` | `health=down`, `sla_breach=True` | Discovered by the P0-A.1 probe parser fix. Worth a separate look; could be NASA upstream issue or our parser breaking on a new response shape. |

These are not P0 correctness bugs (no false data being written; the daemon correctly marks the layer down). They're surfacings that the freshly-fixed probe parser is now catching — a side win from P0-A.1.

### Downgraded — 12 sources had `enabled: true` but no ingester implementation

All 12 changed to `enabled: false` with an explicit `disabled_reason`. Re-enabling any of them requires writing the ingester in `21_GLASSBOX_AI/ingesters/` and registering the class in `glassbox_server.py::candidate_ingesters`.

| sources.yaml id | License | What it would add |
|---|---|---|
| `marinecadastre` | CC0 | US historical AIS (NOAA + USCG); quarterly bulk drops |
| `faa_registry` | public_domain_us_gov | US aircraft owner data; daily CSV |
| `iem_iowa_mesonet` | public_university_open_data | US weather mesonet; high-density observations |
| `iris_dmc_event` | open_research_attribution | Seismic event metadata complementing USGS/EMSC |
| `wikipedia_rest` | cc_by_sa | Article summaries / change feeds for entity enrichment |
| `wayback_cdx` | free_with_attribution | Archive.org URL history; provenance of news source pages |
| `companies_house_uk` | open_government_licence_uk | UK corporate registry; shareholder/director links |
| `courtlistener` | free_with_attribution | US case law + RECAP federal docket events |
| `ripestat` | free_with_attribution | BGP / WHOIS / Internet number resources |
| `crt_sh` | free_with_attribution | Certificate Transparency logs (X.509 issuance) |
| `greynoise_community` | free_with_attribution | Internet-wide background-noise IP intelligence |
| `abuseipdb` | free_with_attribution | Abusive-IP feed for cyber-threat layer |

The bottom 4 (`ripestat`, `crt_sh`, `greynoise_community`, `abuseipdb`) overlap with the P2-A cyber-attack data layer scope in the backlog — they're natural candidates when that work begins.

### Duplicate-id cleanup

Two `id` values appeared twice in `sources.yaml` and were silently last-write-wins'd by any Python loader (`yaml.safe_load` produces a list of two dicts; downstream code that builds a `{s['id']: s}` map overwrites). Both removed in this pass:

| id | Removed block | Kept block | Reason |
|---|---|---|---|
| `gdacs` | API endpoint variant (`free_with_attribution`, vague attribution) | RSS-feed variant (`cc_by_4_0`, full European Commission JRC attribution) | The live `GdacsIngester` reads the RSS feed; kept block matches reality and has the better license tag. |
| `hacker_news` | Legacy "Phase 2 — not in initial vertical slice" placeholder | Active CC0 block with accurate attribution | Kept block matches the running `HackerNewsIngester`. |

Both removals left a single-line comment in `sources.yaml` at the old location explaining the deduplication.

## Verification

```bash
# Source count + duplicate check
python3 -c "
import yaml
from collections import Counter
with open('infra/sources.yaml') as f: d = yaml.safe_load(f)
src = d['sources']
print(f'Total: {len(src)} · Enabled: {sum(1 for s in src if s.get(\"enabled\") is True)}')
print(f'Disabled (with reason): {sum(1 for s in src if s.get(\"enabled\") is False and s.get(\"disabled_reason\"))}')
print(f'Duplicates: {[k for k,v in Counter(s[\"id\"] for s in src).items() if v>1]}')
"
# Expected: Total: 84 · Enabled: 30 · Disabled (with reason): 54 · Duplicates: []
```

Daemon startup log gate (next time the daemon restarts) should report `ingesters_activated=30 ingesters_refused=0`. P0-B does NOT restart the daemon — the change is to sources.yaml only and only affects future restarts; the current in-memory ingester registration is unchanged.

## Out of scope (deferred to other items)

- Writing the 12 missing ingesters → P2 territory (~4-8h each).
- Investigating the `openfema` 503 and `nasa_eonet` down state → ops triage, not P0-B.
- Auditing the `commercial_use_ok` and `redistributable` flags against current ToS for each upstream → separate license-audit pass (referenced in LICENSE_RISK_REGISTER).
