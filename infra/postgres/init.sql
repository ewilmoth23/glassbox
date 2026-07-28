-- ═══════════════════════════════════════════════════════════════════════════
-- GLASSBOX V2 — PostgreSQL bootstrap
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Purpose: Install all extensions + create the durable schema for Glassbox.
--          This is the database that turns Glassbox from a "live globe that
--          forgets" into "the OSINT moat" — every event observed lives here
--          forever (with TimescaleDB compression + retention policy).
--
-- Run this AFTER installing Postgres 16 + PostGIS + TimescaleDB + pgvector
-- on the Mac Mini. See 09_SETUP_GUIDES/POSTGRES_SETUP.md for install steps.
--
-- Run this script ONCE per fresh database with:
--    psql -h 127.0.0.1 -U glassbox -d glassbox -f infra/postgres/init.sql
--
-- Idempotency: every CREATE uses IF NOT EXISTS where supported. Re-running
-- the script on an already-initialized DB is safe and a no-op.
--
-- Versioning: see schema_migration table at the bottom. Update it as schema
-- evolves. Future migrations live in infra/postgres/migrations/NNN_*.sql.
--
-- Hard rules locked from the V2 plan:
--   • All times UTC, stored as TIMESTAMPTZ. No naive timestamps anywhere.
--   • All geometry WGS84 (SRID 4326), stored as GEOGRAPHY (not GEOMETRY)
--     for accurate distance / bearing / area calculations.
--   • Every fact has a source. Every source has a fetched_at timestamp.
--   • Multi-user architecture from day one — user_id column everywhere,
--     defaults to 'system' for v1.0 single-user. Adding real auth later
--     is one env var flip + Auth0/Clerk integration.
--   • No hard deletes on time-series data. Use TimescaleDB retention policies.
--   • Every table has created_at + updated_at for audit.
-- ═══════════════════════════════════════════════════════════════════════════

-- ─── 1. Extensions ─────────────────────────────────────────────────────────
-- These must succeed for the rest of the script to make sense.
-- If any of these errors, abort and fix the install per POSTGRES_SETUP.md.

CREATE EXTENSION IF NOT EXISTS postgis;
-- PostGIS gives us geography/geometry types, spatial indexes, ST_Intersects,
-- ST_DWithin (proximity), ST_Distance, ST_MakeEnvelope (bbox), and ~3,000
-- other spatial functions. The bedrock for "what's near what."

CREATE EXTENSION IF NOT EXISTS timescaledb;
-- TimescaleDB gives us hypertables for time-series data:
--   • Auto-partitioning by time (chunks per day/week)
--   • Compression policy (10x storage savings on chunks > N days old)
--   • Retention policy (auto-drop chunks > N days old)
--   • Continuous aggregates (pre-computed rollups)

CREATE EXTENSION IF NOT EXISTS vector;
-- pgvector gives us VECTOR(N) column type + HNSW indexes for fast
-- semantic similarity search. Used on event.embedding for "find me events
-- similar to this one" queries via sentence-transformer embeddings.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- pg_trgm gives us trigram indexes for fuzzy text matching.
-- Used on entity.display_name for "find aircraft by partial callsign" etc.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- UUID generation for primary keys. Postgres 13+ has gen_random_uuid()
-- built-in but uuid-ossp covers older clients.

CREATE EXTENSION IF NOT EXISTS btree_gist;
-- Required for spatial+temporal compound indexes (gist over geom + time).

-- Note on Apache AGE (graph extension): EXPLICITLY NOT INSTALLED.
-- Per V2 plan reject list. Use entity_relation table + recursive CTEs for
-- 95% of our graph needs. Add AGE only if/when graph queries become the
-- dominant workload.


-- ─── 2. Roles + grants (multi-user architecture from day 1) ────────────────
-- Three roles even though v1.0 only has one human user. Architecting now =
-- one config flip later when Pro tier launches.
--
-- glassbox        — full DDL + DML (server's role; runs migrations)
-- glassbox_writer — INSERT/UPDATE/SELECT (ingester role; cannot ALTER tables)
-- glassbox_reader — SELECT only (consumer role; what API tokens map to)
--
-- The DO block makes this idempotent — won't error if roles already exist.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'glassbox_writer') THEN
    CREATE ROLE glassbox_writer NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'glassbox_reader') THEN
    CREATE ROLE glassbox_reader NOLOGIN;
  END IF;
END
$$;


-- ─── 3. Source provenance ──────────────────────────────────────────────────
-- The citation moat. Every fact in this DB links back to a source row.
-- Without this, "we know" is meaningless. With this, "we know because <source>
-- said so at <fetched_at>" is auditable forever.

CREATE TABLE IF NOT EXISTS source (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL,
        -- Canonical source identifier — match the ingester's `source` attribute.
        -- Examples: 'opensky', 'aisstream', 'celestrak', 'gdelt_v2_geo',
        -- 'gdelt_v2_geo_topical', 'usgs_quakes', 'noaa_nws', 'reliefweb',
        -- 'gdacs', 'eonet', 'acled', 'fred', 'bluesky', 'reddit'.
    source_url TEXT,
        -- Full URL fetched, or NULL for sources without per-record URLs (e.g.
        -- streaming firehoses where the URL is just the WS endpoint).
    fetched_at TIMESTAMPTZ NOT NULL,
        -- When THIS server pulled the data. Distinct from event_time below.
        -- Latency = event_time → fetched_at = how late we found out about it.
    raw_payload JSONB,
        -- The original record exactly as the source returned it. Lets us
        -- replay normalization without re-fetching. Large blobs (news article
        -- bodies) should reference an external store path instead of inlining.
        -- Soft cap: 1 MB per row. If you need bigger, use the future
        -- raw_payload_external_path column (added when MinIO comes online,
        -- post-v1.0).
    confidence_prior REAL NOT NULL DEFAULT 1.0,
        -- 0.0..1.0 — how much we trust THIS source by default. Used by the
        -- Bayesian confidence aggregator (replaces LLM-based scoring per
        -- the V2 LLM-to-code migration). Examples:
        --   USGS earthquakes: 0.99 (authoritative, near zero false-positive)
        --   GDELT events:     0.65 (broad recall, mediocre precision)
        --   Bluesky firehose: 0.40 (raw social signal, high noise)
    user_id TEXT NOT NULL DEFAULT 'system',
        -- Multi-user scaffold. v1.0 always 'system'. v1.2 Pro = user-supplied.
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS source_type_fetched_idx
    ON source (source_type, fetched_at DESC);
CREATE INDEX IF NOT EXISTS source_user_idx
    ON source (user_id, fetched_at DESC);


-- ─── 4. Entity ontology ────────────────────────────────────────────────────
-- The universal table — every "thing" we observe gets a row here, regardless
-- of type. An aircraft, a vessel, a satellite, a news event, a location, an
-- organization, a person — all entities. This is what makes cross-domain
-- queries possible ("show me everything related to MMSI X").

CREATE TABLE IF NOT EXISTS entity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,
        -- Top-level type. Stable, small set. Add new types via migration only.
        -- Current set: 'aircraft', 'vessel', 'satellite', 'event',
        -- 'location', 'organization', 'person', 'infrastructure'.
        -- Future-reserved: 'sensor', 'narrative', 'document'.
    canonical_id TEXT NOT NULL,
        -- The source-system ID. ICAO24 hex for aircraft, MMSI for vessels,
        -- NORAD catalog number for satellites, GDELT GLOBALEVENTID for news,
        -- OSM ID for static infrastructure, etc.
    canonical_id_type TEXT NOT NULL,
        -- Which scheme canonical_id is in. Critical for dedup. Examples:
        -- 'icao24', 'mmsi', 'norad', 'gdelt_event', 'osm_node', 'osm_way',
        -- 'usgs_event_id', 'acled_event_id', 'wikidata_qid', 'gdelt_topical'.
    display_name TEXT,
        -- Human-readable name when known. Aircraft: callsign or registration.
        -- Vessel: ship name. Satellite: satellite name. Event: short headline.
    properties JSONB NOT NULL DEFAULT '{}',
        -- Type-specific attributes. NOT historical — for current state.
        -- Historical attribute changes go in entity_attribute (below).
        -- Examples for aircraft: {"icao_type": "B738", "operator": "DAL",
        --                         "country_of_reg": "US", "is_military": false}
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        -- Bookend timestamps for the entity's observation window.
        -- last_seen updated on every position/attribute update.
    confidence REAL NOT NULL DEFAULT 1.0,
        -- For entity-resolution edge cases (e.g. two MMSIs that might be the
        -- same vessel after a flag change). 1.0 = certain, lower = ambiguous.
    user_id TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Same canonical_id under same scheme = same entity. Hard uniqueness.
    UNIQUE (entity_type, canonical_id_type, canonical_id)
);
CREATE INDEX IF NOT EXISTS entity_type_idx ON entity (entity_type);
CREATE INDEX IF NOT EXISTS entity_props_gin ON entity USING gin (properties);
CREATE INDEX IF NOT EXISTS entity_last_seen_idx ON entity (last_seen DESC);
CREATE INDEX IF NOT EXISTS entity_display_trgm
    ON entity USING gin (display_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entity_user_idx ON entity (user_id);


-- ─── 5. Time-versioned attributes ──────────────────────────────────────────
-- When an aircraft's callsign changes, when a vessel's flag state changes,
-- when an event's severity gets updated as new info arrives — we don't
-- overwrite. We close the prior row (set valid_to) and insert a new one.
-- This means: the DB is queryable AS-OF any past timestamp.

CREATE TABLE IF NOT EXISTS entity_attribute (
    id BIGSERIAL PRIMARY KEY,
    entity_id UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    attribute_name TEXT NOT NULL,
        -- Examples: 'callsign', 'flag_state', 'operator', 'mmsi_class',
        -- 'transponder_squawk', 'severity_score', 'current_port'.
    attribute_value JSONB NOT NULL,
        -- JSON to allow scalars, arrays, or structured values without
        -- schema-table per attribute.
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
        -- NULL = currently valid. Closed when superseded.
    source_id UUID REFERENCES source(id),
        -- Provenance for THIS attribute value.
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ea_entity_attr_time_idx
    ON entity_attribute (entity_id, attribute_name, valid_from DESC);
CREATE INDEX IF NOT EXISTS ea_entity_currently_valid_idx
    ON entity_attribute (entity_id, attribute_name) WHERE valid_to IS NULL;


-- ─── 6. Position tracks (TimescaleDB hypertable) ───────────────────────────
-- Every position observation across all moving entity types. Hypertable means:
--   • Auto-partitioned into 1-day chunks by 'time'
--   • Old chunks compressed automatically (10x smaller after 7 days)
--   • Chunks dropped automatically after 90 days (v1.0 retention)
--
-- Volume estimate: aircraft alone is ~20K state vectors every 10s = 173M
-- rows/day uncompressed. With TimescaleDB compression on chunks > 7d old,
-- compressed footprint is ~5-8GB/day → ~500MB/day after compression.

CREATE TABLE IF NOT EXISTS position_track (
    time TIMESTAMPTZ NOT NULL,
    entity_id UUID NOT NULL,
        -- No FK constraint here — TimescaleDB hypertables don't support
        -- inbound FKs. Enforce referential integrity at the app layer.
    geom GEOGRAPHY(POINT, 4326) NOT NULL,
        -- Always WGS84. Geography (not Geometry) so distance is correct on
        -- the actual Earth, not on a flat projection.
    altitude_m REAL,
        -- Meters above mean sea level. NULL for surface vessels / events.
    velocity_ms REAL,
        -- Meters per second. NULL when source doesn't report.
    heading_deg REAL,
        -- 0..360 compass. NULL when source doesn't report.
    properties JSONB DEFAULT '{}',
        -- Per-type extras: aircraft {squawk, on_ground, vertical_rate};
        -- vessel {nav_status, draught, destination};
        -- satellite {right_ascension, declination, eccentricity}.
    source_id UUID,
        -- No FK — same reason as entity_id. App-layer integrity.
    user_id TEXT NOT NULL DEFAULT 'system'
);

-- Make it a hypertable. 1-day chunks (TimescaleDB default for IoT-style
-- workloads). The if_not_exists guard makes the script idempotent.
SELECT create_hypertable(
    'position_track',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS pt_entity_time_idx
    ON position_track (entity_id, time DESC);
CREATE INDEX IF NOT EXISTS pt_geom_gist
    ON position_track USING gist (geom);
CREATE INDEX IF NOT EXISTS pt_geom_time_idx
    ON position_track USING gist (geom, time);
CREATE INDEX IF NOT EXISTS pt_user_time_idx
    ON position_track (user_id, time DESC);

-- Compression policy — compress chunks older than 7 days.
-- Segment by entity_id so per-entity historical queries stay fast.
ALTER TABLE position_track SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'entity_id'
);

-- Idempotent compression policy add. The DO block silences the "already
-- exists" notice on re-runs.
DO $$
BEGIN
  PERFORM add_compression_policy('position_track', INTERVAL '7 days');
EXCEPTION WHEN duplicate_object THEN
  NULL;
END
$$;

-- Retention policy — drop chunks older than 90 days.
-- v1.0 starts conservative; raise to 365 days when storage allows.
DO $$
BEGIN
  PERFORM add_retention_policy('position_track', INTERVAL '90 days');
EXCEPTION WHEN duplicate_object THEN
  NULL;
END
$$;


-- ─── 7. Events (TimescaleDB hypertable + vector embeddings) ────────────────
-- Every "thing that happened" — GDELT articles, USGS earthquakes, NOAA alerts,
-- ACLED incidents, AND algorithm-detected findings (loitering, rendezvous,
-- AIS gaps, anomalies) all land here. The same query that fetches "events in
-- this bbox" also returns algorithmic findings — UI doesn't need a special
-- code path for them.

CREATE TABLE IF NOT EXISTS event (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id UUID,
        -- The event itself is also an entity (entry in the entity table).
        -- This back-reference lets the Loop's market matching find events
        -- by entity properties.
    event_type TEXT NOT NULL,
        -- Top-level type. Stable, small set:
        --   'gdelt_event', 'gdelt_topical', 'usgs_quake', 'usgs_volcano',
        --   'noaa_alert', 'gdacs', 'eonet', 'acled_event', 'reliefweb_report',
        --   'detected_loiter', 'detected_rendezvous', 'detected_ais_gap',
        --   'detected_flight_anomaly', 'detected_event_cluster',
        --   'detected_proximity', 'sitrep'.
    event_subtype TEXT,
        -- Finer-grained: for 'gdelt_topical' values like 'terrorism',
        -- 'oil_spills', 'mining_disaster'; for 'noaa_alert' values like
        -- 'tornado_warning', 'hurricane_watch'.
    event_time TIMESTAMPTZ NOT NULL,
        -- When the event actually occurred (per the source). Distinct from
        -- created_at (when we recorded it).
    geom GEOGRAPHY(POINT, 4326),
        -- Optional: events without geo (Federal Reserve announcement) have NULL.
    severity REAL,
        -- Normalized 0..10 across all sources. Per-source severity formula
        -- documented in each ingester. NULL = unscored.
    severity_for_market REAL,
        -- 0..10 — distinct from severity. How much THIS event should move a
        -- related prediction market. Decoupled because a 9.0 quake in an
        -- unpopulated region is severity=10 but severity_for_market=2.
        -- Populated by the (now code-based, NOT LLM-based) classifier.
    title TEXT,
    description TEXT,
    properties JSONB DEFAULT '{}',
        -- Source-specific structured fields. For algorithm findings:
        -- {entity_ids: [uuid, uuid], algorithm: 'loitering',
        --  duration_min: 47, distance_km: 0.8}.
    embedding VECTOR(384),
        -- sentence-transformers/all-MiniLM-L6-v2 embedding of title+description.
        -- 384-dim, fast. Used for "find events semantically similar to X."
    source_id UUID REFERENCES source(id),
    confidence REAL NOT NULL DEFAULT 1.0,
        -- Combined event confidence (provenance × source reliability ×
        -- corroboration). Computed by the Bayesian aggregator (NOT LLM).
    domain TEXT NOT NULL DEFAULT 'unknown',
        -- High-level grouping for fan-out routing.
        -- 'sports' | 'politics' | 'weather' | 'macro' | 'geo' | 'tech' | 'unknown'
    decay_half_life_min INT NOT NULL DEFAULT 60,
        -- Minutes after which this signal's relevance halves. Earthquakes
        -- decay fast (~30min), elections decay slow (~1440min).
    user_id TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Composite primary key required by TimescaleDB (must include partitioning column)
    PRIMARY KEY (id, event_time)
);

SELECT create_hypertable(
    'event',
    'event_time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS event_geom_gist
    ON event USING gist (geom);
CREATE INDEX IF NOT EXISTS event_type_time_idx
    ON event (event_type, event_time DESC);
CREATE INDEX IF NOT EXISTS event_subtype_time_idx
    ON event (event_subtype, event_time DESC) WHERE event_subtype IS NOT NULL;
CREATE INDEX IF NOT EXISTS event_user_time_idx
    ON event (user_id, event_time DESC);
CREATE INDEX IF NOT EXISTS event_props_gin
    ON event USING gin (properties);

-- HNSW vector index on embeddings — fast cosine similarity for
-- "find similar events" queries.
CREATE INDEX IF NOT EXISTS event_embedding_hnsw
    ON event USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Compression after 14d (events have richer payload than positions, less
-- compression benefit until aged out). Retention 90d for v1.0.
ALTER TABLE event SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'event_type'
);

DO $$
BEGIN
  PERFORM add_compression_policy('event', INTERVAL '14 days');
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
  PERFORM add_retention_policy('event', INTERVAL '90 days');
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;


-- ─── 8. Entity relationships (graph, no Apache AGE) ────────────────────────
-- Per V2 plan: AGE rejected for v1. Use entity_relation table + recursive
-- CTEs for the 95% of graph queries we actually run (1-3 hops).

CREATE TABLE IF NOT EXISTS entity_relation (
    id BIGSERIAL PRIMARY KEY,
    from_entity_id UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    to_entity_id UUID NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
        -- Examples:
        --   'NEAR'              — within X km within Y minutes (algo finding)
        --   'COOCCURS_WITH'     — appeared in same event
        --   'OPERATED_BY'       — vessel/aircraft → org
        --   'FLAGGED_FROM'      — vessel → country
        --   'MENTIONED_IN'      — entity → event
        --   'FOLLOWED_BY'       — event → event (causal/temporal chain)
        --   'SAME_AS'           — entity-resolution merge
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    properties JSONB DEFAULT '{}',
        -- Per-edge data: NEAR includes {distance_km, duration_min}.
    confidence REAL NOT NULL DEFAULT 1.0,
    source_id UUID REFERENCES source(id),
    user_id TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Prevent dup edges of same type between same pair (active edges).
    UNIQUE (from_entity_id, to_entity_id, relation_type, valid_from)
);
CREATE INDEX IF NOT EXISTS er_from_type_idx
    ON entity_relation (from_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS er_to_type_idx
    ON entity_relation (to_entity_id, relation_type);
CREATE INDEX IF NOT EXISTS er_currently_valid_idx
    ON entity_relation (from_entity_id, to_entity_id) WHERE valid_to IS NULL;


-- ─── 9. API auth scaffolding (multi-user architecture) ─────────────────────
-- v1.0 has no auth UX, but the seam exists. v1.2 Pro tier wires real auth.

CREATE TABLE IF NOT EXISTS api_token (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash TEXT NOT NULL UNIQUE,
        -- SHA-256 of the actual token. Never store plaintext tokens.
    user_id TEXT NOT NULL,
    name TEXT,
        -- Operator-supplied label for the token's purpose.
    scopes TEXT[] NOT NULL DEFAULT ARRAY['read'],
        -- Future: 'read', 'write', 'admin'. v1.0 only 'read' issued.
    rate_limit_per_min INT NOT NULL DEFAULT 60,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS api_token_user_idx
    ON api_token (user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS api_token_active_idx
    ON api_token (token_hash) WHERE revoked_at IS NULL;


-- ─── 10. Subscription scaffolding (v1.2 Pro tier preparation) ──────────────
-- Architected now so v1.2 Pro launch is one config flip + Stripe webhook
-- wire-up, not a schema migration.

CREATE TABLE IF NOT EXISTS subscription (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    tier TEXT NOT NULL,
        -- 'free' | 'pro' | 'intel' | 'enterprise'.
    stripe_subscription_id TEXT UNIQUE,
        -- NULL for 'free'. Set by Stripe webhook handler in v1.2.
    stripe_customer_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
        -- 'active' | 'past_due' | 'canceled' | 'trial'
    current_period_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS subscription_user_idx ON subscription (user_id);
CREATE INDEX IF NOT EXISTS subscription_status_idx ON subscription (status);


-- ─── 11. Audit log (every API call, for usage analytics + abuse detection) ─
-- v1.0 logs everything. v1.2+ Pro tier uses for billing + rate enforcement.

CREATE TABLE IF NOT EXISTS api_audit_log (
    time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id TEXT NOT NULL DEFAULT 'system',
    api_token_id UUID REFERENCES api_token(id),
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INT,
    response_time_ms INT,
    ip_addr INET,
    user_agent TEXT,
    request_id TEXT,
        -- Correlation ID threaded across services for distributed tracing.
    properties JSONB DEFAULT '{}'
);

SELECT create_hypertable(
    'api_audit_log',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS aal_user_time_idx
    ON api_audit_log (user_id, time DESC);
CREATE INDEX IF NOT EXISTS aal_token_time_idx
    ON api_audit_log (api_token_id, time DESC);

-- Retention: 30 days for audit logs (privacy) — adjust after legal review.
DO $$
BEGIN
  PERFORM add_retention_policy('api_audit_log', INTERVAL '30 days');
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;


-- ─── 12. Schema migration tracking ─────────────────────────────────────────
-- Records which version of the schema is installed. Future migrations live
-- in infra/postgres/migrations/NNN_description.sql and INSERT into this table.

CREATE TABLE IF NOT EXISTS schema_migration (
    id SERIAL PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by TEXT NOT NULL DEFAULT current_user
);

INSERT INTO schema_migration (version, description)
VALUES ('001-init', 'Initial schema — entity ontology, position_track + event hypertables, source provenance, multi-user auth/subscription scaffolding')
ON CONFLICT (version) DO NOTHING;


-- ─── 13. Grants ────────────────────────────────────────────────────────────
-- Apply role permissions to all tables. Future tables need similar grants —
-- handled by future migrations.

GRANT USAGE ON SCHEMA public TO glassbox_writer, glassbox_reader;

GRANT SELECT, INSERT, UPDATE ON
    source, entity, entity_attribute, position_track, event,
    entity_relation, api_audit_log
TO glassbox_writer;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO glassbox_reader;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
    TO glassbox_writer;

-- Reader role can never see auth tokens (only the server role can).
REVOKE ALL ON api_token, subscription FROM glassbox_reader;


-- ─── 14. Healthcheck view ──────────────────────────────────────────────────
-- Cheap to query, surfaces enough for /api/v1/dbhealth.

CREATE OR REPLACE VIEW v_db_health AS
SELECT
    (SELECT COUNT(*) FROM entity) AS total_entities,
    (SELECT COUNT(*) FROM source WHERE fetched_at > NOW() - INTERVAL '1 hour') AS sources_last_hour,
    (SELECT COUNT(*) FROM position_track WHERE time > NOW() - INTERVAL '1 hour') AS positions_last_hour,
    (SELECT COUNT(*) FROM event WHERE event_time > NOW() - INTERVAL '1 hour') AS events_last_hour,
    (SELECT MAX(event_time) FROM event) AS latest_event_time,
    (SELECT pg_size_pretty(pg_total_relation_size('position_track'))) AS position_track_size,
    (SELECT pg_size_pretty(pg_total_relation_size('event'))) AS event_table_size,
    (SELECT pg_size_pretty(pg_database_size(current_database()))) AS total_db_size;

GRANT SELECT ON v_db_health TO glassbox_reader, glassbox_writer;


-- ═══════════════════════════════════════════════════════════════════════════
-- DONE. Verify by running:
--    SELECT * FROM schema_migration;
--    SELECT * FROM v_db_health;
--    SELECT PostGIS_Version();
--    SELECT extversion FROM pg_extension WHERE extname IN
--      ('postgis', 'timescaledb', 'vector', 'pg_trgm');
-- ═══════════════════════════════════════════════════════════════════════════
