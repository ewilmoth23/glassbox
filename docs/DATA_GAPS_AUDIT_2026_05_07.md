# DATA INGESTION GAP AUDIT — 2026-05-07

**Strategic goal:** Find events before mainstream news and 99% of people.
**Audit scope:** Compare current ingestion vs. signals that beat MSM by hours-to-weeks.
**Honest verdict:** Strong on tactical mid-layer signals (planes, ships, quakes, news). **Catastrophically missing Tier-1 early-warning signals** that break events 24–72h before MSM pickup.

---

## Prioritized action list

| # | Source / Algorithm | Why it matters | Lead vs MSM | Effort |
|---|---|---|---|---|
| ~~1~~ | ~~**AIS gap detection** (algorithm)~~ | ✅ **SHIPPED 2026-05-08** as `algorithms/dark_ship.py`. Detects vessels silent 6h–14d while moving (>0.5 m/s). Dedup 24h, severity scales with hours_dark. | real-time | DONE |
| ~~2~~ | ~~NetBlocks~~ | ❌ **BLOCKED**: ToS = research-only, no commercial. Replaced with NOAA SWPC (item #5). | n/a | n/a |
| ~~3~~ | ~~ProMED-mail~~ | ❌ **BLOCKED**: ISID requires paid commercial-redistribution license. | n/a | n/a |
| 4 | **Leadership flight tracking** (algorithm) | Military/exec aircraft unusual departures = coup/conflict precursor | 12–36h | 3h |
| ~~5~~ | ~~**NOAA SWPC space-weather**~~ | ✅ **SHIPPED 2026-05-08** as `ingesters/noaa_swpc.py` + `write_space_weather_events`. K/G/R/S alert categories anchored geographically; 59 alerts persisted on first cycle including K6 storms. | 1–3h | DONE |
| 6 | **Cloudflare Radar Internet outages** (ingester) | Real-time BGP hijacks, DDoS, DNS failures | hours | 2h |
| 7 | **Blitzortung lightning network** (ingester) | Severe convection + hail before NWS warns | 30 min | 2h |
| 8 | **GMDSS maritime distress** (ingester via NAVTEX/LRIT) | Ships in actual distress, real-time | 2–8h | 3h |
| 9 | **Multi-source aviation** (refactor planes.py) | adsb.lol single-source = SPOF; +adsb.fi/airplanes.live → 40k aircraft | continuous | 4h |
| ~~9b~~ | ~~**AIS static-info merging**~~ | **✅ SHIPPED 2026-05-08.** Digitraffic `/v1/vessels` now refreshed hourly; name + IMO + callsign + ship_type merged into every position record. **18,327/18,334 vessels populated. Cross-domain match against OFAC SDN now fires** — 20+ live AIS vessels confirmed matching sanctioned vessels by name (ASTRA, ANTEY, POLA SOFIA, TAIMYR, BALTIYSK, …). | continuous | DONE |
| 10 | **EIA-930 power grid real-time** (ingester) | Sudden blackouts, cascade-failure precursor | 30–60 min | 1.5h |
| 11 | **Whale Alert / Etherscan** (algorithm) | Large crypto liquidations = market shock precursor | 2–6h | 2h |
| 12 | **NHC tropical cyclones direct** (ingester) | Beats GDACS/EONET routing | 3h+ | 1h |
| 13 | **EU + UK sanctions list** (ingester) | Cross-border financial watch beyond OFAC | continuous | 2h |
| 14 | **Smithsonian GVP + NASA OMI volcanic SO2** (ingester) | Volcanic activity tracked by satellite SO2 detection | 4–12h | 1.5h |
| 15 | **CDC HAN + WHO emergency alerts** (algorithm on existing data) | Structured outbreak feeds | 24–72h | 1h |

**Key insight:** Items 1, 4, 11, 15 are **algorithms on existing data**, not new ingesters. All five algorithms = <14h total work, would unlock "before news" across military, maritime, financial, health.

## Shipped 2026-05-08 (in addition to items above)

### Algorithms (Phase 4)
- `algorithms/sanctions_match.py` — live AIS vessels ↔ OFAC SDN cross-reference. **923 sanctioned-vessel-underway findings on first run** (677 IMO-precise + 246 name-fuzzy), including renamed shadow-fleet vessels (e.g. ADMIRAL ↔ "HS Star" by IMO).
- `algorithms/dark_ship.py` — vessels going dark on AIS while underway.
- `algorithms/military_flights.py` — military aircraft currently broadcasting (153 live).
- `algorithms/loitering.py` — vessels/aircraft staying within ~1km for 4h+ while moving.
- `algorithms/rendezvous.py` — pairs of moving entities within 1km, both at 0.5–3 m/s. **Catches Russian shadow-fleet STS transfers**: VF TANKER-17 ↔ VELES (15m), VOLGO-DON 117 ↔ GEROY IGOR ASEEV (18m), etc.
- `algorithms/sanctioned_airspace.py` — aircraft transiting Iran FIR, North Korea, Crimea, Syria, Cuba, Belarus, Yemen, Libya, South Sudan, eastern Donbas. Detects German Air Force flights in Belarus, Royal Jordanian / Etihad in Syria, Qatar Airways / Emirates in Iran.

### New ingesters
- `ingesters/noaa_swpc.py` — real-time space-weather alerts (K-index, R-flares, S-radiation events). 59 alerts on first cycle.
- `ingesters/nhc_storms.py` — Atlantic + East Pacific tropical cyclones. 0 active pre-season but defensive infra ready for June 1.
- `ingesters/gdacs.py` — global disaster alerts with Green/Orange/Red severity. 82 events on first cycle (Indonesian volcanic eruption, multi-country Central African drought, etc.).
- `ingesters/hacker_news.py` — top-N HN firehose. Cloudflare layoffs, security breaches, vulnerabilities surface 4-24h before MSM.

### Brief / API improvements
- `brief.py` — 7 tier-1 ALERT categories now lead the brief: sanctioned vessels, dark vessels, military aircraft, sanctioned airspace, rendezvous, loitering, space weather.
- `api_v1.py` — split tier-1 events into separate query (1000-row sanctions cap + 500-row rare-events cap) so SWPC / military / sanctioned-airspace findings don't get crowded out by high-volume sanctions findings.
- OFAC SDN IMO+MMSI extraction unlocked. 1,479/1,481 sanctioned vessels now have IMO populated. Match precision multiplier.
- Static-info merge for Digitraffic AIS (`/v1/vessels` endpoint). 18,327/18,334 vessels now have name + IMO + callsign.

---

## Coverage gaps by signal class

### Aviation
- **In:** `adsb.lol` primary (~14,750 aircraft persisted, ODbL license, commercial-OK). METAR + airports.
- **Gap:** Single-source = SPOF. No cross-validation. Known geographic gaps in CONUS, South America, southern Africa.
- **License posture for alternatives** (verified against planes.py docstring 2026-05-07):
  - `adsb.fi` — **ToS prohibits commercial use.** Out for v1.0 commercial product.
  - `airplanes.live` — license unclear; needs verification before adoption.
  - `OpenSky Network` — academic; commercial use restricted; rate-limit hostile.
  - `ADS-B Exchange` — paid commercial license required.
  - `FlightAware Firehose` — paid commercial only.
  - `adsb.one` — aggregator; ToS unclear.
  - `flightradar24` — commercial-only.
- **Path to "more planes" for v1.0**: (a) optimize current adsb.lol tile coverage (currently 85 × 250nm tiles, 60s cycle — could use smaller tiles + tighter polling for denser coverage), (b) pursue ADS-B Exchange paid license (this is the gold standard for serious users), (c) investigate airplanes.live ToS specifically for commercial OSINT use. **Each is multi-session research-and-license work, not a quick win.**
- **Algorithm gap:** No leadership-flight tracking, no sanctioned-airspace routing detection.
- **Risk:** Public ADS-B firehose is in MSM's hands too via Flightradar24. **The leverage is algorithms (leadership tracking, sanctions evasion), not raw count.**

### Maritime
- **In:** Digitraffic (FI), Danish DMI, BarentsWatch (no creds, dormant), AISStream (dormant). ~2–4K vessels visible at any time vs ~70K real global active. **18,334 vessels persisted but ALL have NULL display_name + missing IMO** as of 2026-05-07.
- **Static-info gap (CRITICAL — discovered during Phase 2-G):** ships ingester only fetches Digitraffic `/v1/locations` (the position firehose, AIS Types 1/2/3) which carries lat/lng/sog but NOT name/IMO/callsign. Static info (AIS Type-5) lives on a separate endpoint that we don't fetch. **Cross-domain match against OFAC SDN cannot fire without this.** Same gap likely affects BarentsWatch + DMA branches. (Pre-fix bug: navStat enum was being written as the name field, producing display_names like "5"/"15"/"0" for thousands of vessels — fixed 2026-05-07.)
- **Gap:** No global AIS coverage; no Spire/MarineTraffic (paid). Most ships invisible to us.
- **Critical missing algorithm:** AIS gap detection (vessel goes dark in coverage zone = evasion/staging).
- **Missing:** GMDSS distress, NAVTEX, LRIT.
- **Risk:** **Highest-leverage "before MSM" domain if we solve AIS gap detection AND fix the static-info gap.**

### Seismic
- **In:** USGS primary, EMSC FDSN secondary. Coverage adequate.
- **Gap:** Volcanic SO2 (Smithsonian GVP, NASA OMI) not ingested. Volcanic activity precedes eruption news 4–12h.

### Weather / atmospheric
- **In:** NOAA NWS, NOAA SWPC (code present, dormant), WAQI AQI (rate-limited demo token).
- **Critical gaps:** Blitzortung lightning network (severe-storm precursor, free WebSocket). NOAA SWPC code exists but isn't running. Open-Meteo gated off (non-commercial).

### News / events
- **In:** GDELT general + topical (CURRENTLY GATED OFF, rate-limit hostile). NewsData.io built but no creds. ProMED referenced in frontend only.
- **Critical gap:** ProMED ingester. Disease outbreaks 72h–2wk before authorities.
- **Missing:** Hacker News (tech leads news 4–24h), Wayback Machine CDX (mass-deletion = document destruction signal).

### Social
- **In:** Bluesky public search, Reddit unauth (10 QPM), YouTube (no creds), Telegram (no creds, gray ToS).
- **Gap:** Bluesky Jetstream code ready but not wired (real-time WebSocket, free).
- **Note:** Social is parallel to MSM, not ahead. Coverage adequate if Jetstream wires up.

### Satellites
- **In:** Celestrak (TLEs+SGP4), NASA NEO, EONET, DONKI, FIRMS (creds not set, degraded).
- **Adequate** for v1.0; main miss is OMI SO2 for volcanic.

### Sanctions / financial
- **In:** OFAC SDN (just wired this session — see DB writer Phase 2-G), SEC EDGAR, FRED (no creds), Odds API. NewsData missing creds.
- **Gap:** EU + UK sanctions lists. No financial anomaly algorithm (whale movements, exchange inflows, liquidation cascades).

### Network / cyber
- **In:** **Nothing dedicated.** CISA KEV proxied; ThreatFox/URLhaus dormant.
- **Tier-1 gap:** NetBlocks (BGP + DNS), Cloudflare Radar (DDoS + outages). **These are the earliest warning of authoritarian crackdowns or cyberwar — currently invisible to Glassbox.**

### Health / surveillance
- **In:** Nothing.
- **Tier-1 gap:** ProMED, CDC HAN, WHO emergency alerts. **72h+ lead time on outbreak news currently zero-captured.**

---

## Algorithmic gaps

**Current:** `proximity.py` only. Late-stage (event already published). Needed:

1. **AIS gap detection** — temporal deque, flag >6h dark in coverage zone (~2h)
2. **Leadership flight tracking** — filter by mil/exec hex ranges, unusual departure detector (~3h)
3. **Whale movement anomaly** — Etherscan WebSocket, large transfers to exchange wallets (~2h)
4. **Loitering detection** — entity stays in bbox >N hours (~1h)
5. **Rendezvous detection** — two entities converge <1km within 30min (~2h)
6. **Crypto consolidation** — multiple wallets → single address within 1h (~1h)
7. **Military repositioning** — aircraft from peacetime to forward staging (~3h)

---

## Dormancy / credential issues — fix in <1h for free

| Layer | Ingester | State | Easy fix |
|---|---|---|---|
| news | NewsDataIngester | Built, no creds | Set NEWSDATA_TOKEN |
| wildfires | NASA_FIRMS | No creds, silent fail | Set NASA_FIRMS_KEY |
| space_weather | NOAA_SWPC | Code dormant | Wire `/alerts` parser |
| conflict | ACLED | No creds | Set ACLED creds (or leave gated) |
| macro | FRED | No creds | Set FRED_API_KEY |

**Activating these = 8 early-warning signals unlocked in <1h.**

---

## Bottom line

To achieve "before 99% of people":
- **5 algorithms** (AIS gap, leadership flight, whale moves, loitering, military reposition): **<14h total**, zero new data sources, multi-domain unlock.
- **5 ingesters** (NetBlocks, ProMED, Blitzortung, GMDSS, Cloudflare Radar): **<10h total**, free APIs.
- **Activate dormant code** (SWPC, FIRMS, NewsData): **<1h**.
- **Multi-source aviation** redundancy: **~4h**.

**Total to unlock "before news" tier-1 signals: ~27h focused work.** The hardest gap is in pattern recognition (algorithms) and wiring up dormant credentialed code, not in finding new data sources.
