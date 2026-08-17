# Glassbox


**Real-time OSINT fusion — 40 licensed data feeds, 13 correlation algorithms, one queryable event model.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Postgres](https://img.shields.io/badge/postgres-PostGIS%20%7C%20TimescaleDB%20%7C%20pgvector-336791?logo=postgresql&logoColor=white)](https://postgis.net/)

## Project status

> **Actively developed. Extracted from a private monorepo, so commit history starts at extraction.** This is a personal project built in the open, published so the
> work can be read and run. It is not a supported product.

Known gaps and caveats, stated up front:

- Single-node by design; no authentication layer. Not intended for public exposure without one.
- Several ingesters ship disabled pending licence evidence. See `infra/sources.yaml`.
- Requires Postgres with PostGIS, TimescaleDB and pgvector; first-run setup is non-trivial.

Issues and pull requests are welcome. If something breaks on first run, that is
useful information — please open an issue rather than assuming it works for
everyone else.


Glassbox ingests heterogeneous public data — vessel AIS, aircraft ADS-B, satellite
TLEs, seismic events, wildfires, weather, conflict events, SEC filings, sanctions
lists — normalises it into a single event model, and derives higher-order events
from correlations across those streams.

The interesting part is not the map. It's that *"this vessel went dark inside a
sanctioned port's approach and rendezvoused with a second vessel four hours
later"* is a **derived** event, computed from three independent feeds that have no
knowledge of each other.

```
76,564 LOC   ·   555 tests   ·   40 ingesters   ·   13 correlation algorithms
```

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
   40 sources  ───► │  ingesters/     fetch() → normalize()   │
   AIS · ADS-B      │  base.Ingester, license-gated at boot   │
   NASA · NOAA      └──────────────────┬──────────────────────┘
   GDELT · SEC                         │
   OFAC · EU CFSP                      ▼
                    ┌─────────────────────────────────────────┐
                    │  writers/     24 per-domain clusters     │
                    │  idempotent UPSERT, deterministic UUIDs  │
                    └──────────────────┬──────────────────────┘
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │   Postgres · PostGIS · TimescaleDB       │
                    │            · pgvector                    │
                    │   event hypertable, jsonb properties     │
                    └────────┬───────────────────┬─────────────┘
                             │                   │
                             ▼                   ▼
              ┌──────────────────────┐  ┌────────────────────────┐
              │ algorithms/  13 ×    │  │ web/routes/   FastAPI  │
              │ derived-event        │  │ viewport · signals ·   │
              │ detection            │  │ alerts (SSE) · metrics │
              └──────────────────────┘  └────────────────────────┘
```

**Storage.** PostGIS for spatial predicates, TimescaleDB hypertables for
time-partitioned events, pgvector for similarity search over event embeddings.

**Ingestion.** Every source subclasses `ingesters/base.Ingester` and implements
`fetch()` / `normalize()`. Sources are declared in `infra/sources.yaml` and
validated at startup by `sources_registry.py`.

**Derivation.** `algorithms/` reads the event table and writes new events back to
it: `dark_ship`, `loitering`, `rendezvous`, `port_call`, `proximity`,
`shadow_fleet_cluster`, `sanctioned_port_arrival`, `sanctioned_airspace`,
`sanctioned_rendezvous`, `sanctioned_dark_vessel`, `sanctions_match`,
`sanctions_multijurisdictional`, `military_flights`.

---

## Three things worth reading the code for

### 1. The license gate

Most OSINT projects quietly violate the terms of the feeds they scrape. Glassbox
refuses to start a source that lacks documented commercial rights.

`infra/sources.yaml` carries 87 entries, each with a `commercial_use_ok` flag and
a `disabled_reason` when false. `sources_registry.py` enforces this at boot — a
source without license evidence does not run, regardless of whether its code
works.

OpenSky and ACLED are disabled in this repo for precisely that reason. That is
not a missing feature; it is the control working.

### 2. The false-positive audit

All 13 algorithms were audited against production data. Result: **~12.7M
historical false positives withdrawn, 6 algorithms corrected.**

The most instructive case was `dark_ship`, which flags a vessel that stops
transmitting AIS. It had produced 209,903 events, **99.6% of them false** — an
AIS receiver going offline is indistinguishable at the database level from every
vessel in its coverage area going dark at once. The fix was cohort suppression:
if an entire receiver cohort drops simultaneously, that is infrastructure, not
evasion.

Found the same way:

| Algorithm | FPs withdrawn | Root cause |
|---|---:|---|
| `proximity` | 12,072,548 | deny-list didn't exclude algorithm-derived event types, so derived events recursively triggered further derivations |
| `rendezvous` | 452,100 | no sustained-proximity requirement; fixed by also requiring no recent high-speed transit, which removed the airport-taxi class |
| `dark_ship` | 209,903 | AIS receiver downtime read as evasion |
| `loitering` | 28,238 | zero-bbox stale pings |
| `port_call` | 7,413 | — |
| `sanctioned_airspace` | 6,118 | axis-aligned bounding boxes replaced with concave hulls |
| `shadow_fleet_cluster` | 2,225 | unbounded DBSCAN cluster diameter |

Two passed clean: `military_flights` (trusts an authoritative upstream) and
`sanctions_multijurisdictional`.

Detection code that has never been checked against ground truth is a demo. This
one has been.

### 3. The refactor

Three modules had grown past the point of safe modification:

| Module | Before | After | Change |
|---|---:|---:|---:|
| `glassbox_server.py` | 81 routes | 4 routes | −95.1% |
| `api_v1.py` | 3,257 lines | 131 lines | −94.0% |
| `writers.py` | 2,842 lines | 78 lines | −95.6% |

Done incrementally — one route group or writer cluster per commit — behind a
route-manifest smoke test that asserts every known route is still registered and
still returns < 500. A silently-dropped route fails CI instead of production.
Tests stayed green at every step.

---

## Layout

| Path | Contents |
|---|---|
| `ingesters/` | 40 source adapters + `base.Ingester` |
| `writers/` | 24 per-domain persistence clusters |
| `algorithms/` | 13 derived-event detectors |
| `web/routes/` | FastAPI route packages (post-refactor) |
| `entity/` | entity resolution (splink) |
| `embeddings.py` | pgvector embedding pipeline |
| `mcp_servers/` | Model Context Protocol servers over the event store |
| `infra/sources.yaml` | the license gate |
| `tests/` | 97 modules, 555 test functions |

## Running it

The event store is not optional — the API reads from Postgres on every request, so
the database has to exist before the server is worth starting.

```bash
# 1. Postgres 16+ with PostGIS, TimescaleDB and pgvector.
#    Full install walkthrough, including a Docker option: docs/POSTGRES_SETUP.md
psql -h 127.0.0.1 -U glassbox -d glassbox -f infra/postgres/init.sql
python infra/postgres/run_migrations.py        # applies infra/postgres/migrations/

# 2. Configuration.
cp .env.example .env          # GLASSBOX_DB_URL + optional per-source credentials

# 3. Run.
pip install -r requirements.txt
python glassbox_server.py     # serves on :8790
pytest                        # 555 tests
```

`init.sql` installs the extensions and creates the durable schema — 10 tables,
TimescaleDB hypertables for `position_track` and `event` with retention policies,
PostGIS `GEOGRAPHY(4326)` columns throughout, and `glassbox_writer` /
`glassbox_reader` roles. It is idempotent; re-running it on an initialised
database is a no-op.

Sources requiring credentials stay disabled until configured. The system runs on
whatever subset you hold rights to — a missing key disables one ingester and
nothing else.

## Honest limitations

- Single-node deployment. Designed for one operator, not multi-tenant.
- No authentication layer. Not intended for public exposure without one.
- `infra/postgres/init.sql` covers the core event store — everything the ingesters,
  the `/api/v1` surface and the globe read. Four reference tables are **not** in it
  and were maintained out of band in the original deployment: `ports`, `zones` and
  `sanction_lookup`, which the `port_call`, `sanctioned_airspace` and
  `sanctioned_rendezvous` algorithms join against, and `facts`, which two
  `/api/v1` routes read from a separate knowledge-graph service. Those four
  algorithms and routes will error on a fresh database until you define and
  populate them; the rest of the system runs.
- The 3D frontend is a large single-file CesiumJS application, kept out of this
  repo. The measured optimisation there — Entity → Primitive API plus clustering,
  **1–2 FPS → 18 FPS** at full layer density — is described in `docs/`.
- **Two packages did not survive extraction.** `infra/ml/` (isolation-forest anomaly
  detection) and `infra/er/` (the Splink entity-resolution pipeline) are absent from
  this repository, so `tests/test_anomaly_isoforest.py` and
  `tests/test_splink_pipeline.py` have no subject to import. They are excluded in CI
  rather than deleted, so the gap stays visible.
- **Two lookup files are absent** and nine further test modules read one of them:
  `glassbox_taxonomy/data/cameo_lookup.json` and
  `ingesters/gdelt_bulk/prefilter/data/source_quality.json`. A broad `data/` rule in
  `.gitignore` had been excluding both; the rule now carries explicit exceptions, but
  the files themselves still need to be restored.
- **What CI actually proves.** The workflow stands up Postgres 17 with PostGIS,
  TimescaleDB and pgvector, applies `init.sql` plus the four migrations, and runs the
  suite on Python 3.12 and 3.13 — **995 tests green**. The eleven modules named above
  are skipped for the reasons given. A green badge here means the event model,
  ingesters, writers, algorithms and `/api/v1` surface work against a real database;
  it does not mean every test in the repository ran.
- Several sources ship disabled pending license evidence. See `infra/sources.yaml`.

## License

MIT — see [LICENSE](LICENSE). The MIT license covers *this code*. Data retrieved
through it remains subject to each provider's own terms, which is what the
license gate exists to enforce.
