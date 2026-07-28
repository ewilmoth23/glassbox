-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 004 — mcp_audit_log table
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Purpose:
--   Per HANDOFF_04 (R2), every MCP tool invocation writes one row here.
--   Powers per-agent audit trails, rate-limit attribution, and
--   post-incident forensics ("who called find_entity 4000× last hour?").
--
--   Glassbox MCP servers (entities, events, investigation) live in
--   21_GLASSBOX_AI/mcp_servers/ as separate processes. They share this
--   table — server_name discriminates by source.
--
-- Why a separate table (not a column on a generic audit_log):
--   * Different retention policies anticipated (MCP audit = 90d; sec-event
--     audit = 7y).
--   * MCP-specific fields (tool_name, cost_class, agent_id) wouldn't fit
--     a generic schema cleanly.
--   * Easy to drop / archive in one operation when retention hits.
--
-- Idempotency:
--   CREATE TABLE / CREATE INDEX IF NOT EXISTS make this safe to re-run.
--   The schema_migration insert is upserted.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS mcp_audit_log (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    server_name      TEXT         NOT NULL,
    tool_name        TEXT         NOT NULL,
    -- Stable agent identifier (claude-desktop session id, langgraph node id,
    -- etc.). NULL when called from an unauthenticated context (development /
    -- self-test). Rate-limit + per-agent reporting bucket on this.
    agent_id         TEXT,
    -- Full request payload for investigation-server tools (LLM-bearing,
    -- expensive). entities/events servers store just operation type +
    -- bbox dimensions to keep this table light at high call rates.
    request_payload  JSONB,
    -- Summary, NOT the full response — keeps row size bounded under
    -- agent-scale fan-out. Typical contents: {result_count: 42, types: [...]}
    response_summary JSONB,
    latency_ms       INTEGER,
    -- 'cheap'  — pure REST passthrough, ~50ms
    -- 'normal' — REST + light post-processing, ~50-300ms
    -- 'expensive' — LLM-bearing or multi-call (NL query, brief gen),
    --   counts 5× toward the agent's per-min budget
    cost_class       TEXT         CHECK (cost_class IN ('cheap', 'normal', 'expensive')),
    success          BOOLEAN      NOT NULL,
    error_message    TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Audit-trail lookups: "show me everything agent X did, newest first."
CREATE INDEX IF NOT EXISTS mcp_audit_agent_idx
    ON mcp_audit_log (agent_id, created_at DESC)
    WHERE agent_id IS NOT NULL;

-- Per-server rate-limit / overflow lookups: "how many cheap-class calls
-- has the entities server seen in the last 60s?"
CREATE INDEX IF NOT EXISTS mcp_audit_server_idx
    ON mcp_audit_log (server_name, created_at DESC);

-- Failure-rate lookups for ops dashboards.
CREATE INDEX IF NOT EXISTS mcp_audit_failures_idx
    ON mcp_audit_log (created_at DESC)
    WHERE success = false;

-- Migration tracking — schema_migration uses text version + description.
INSERT INTO schema_migration (version, description, applied_at, applied_by)
VALUES (
    '004-mcp-audit-log',
    'Audit log for MCP server tool invocations (HANDOFF_04 / R2). One row per tool call across the entities/events/investigation servers; powers per-agent audit trails + rate-limit attribution + post-incident forensics.',
    now(),
    current_user
)
ON CONFLICT (version) DO UPDATE
    SET description = EXCLUDED.description,
        applied_at  = EXCLUDED.applied_at;
