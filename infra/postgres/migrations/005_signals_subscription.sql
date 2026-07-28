-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 005 — signals_subscription table
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Purpose:
--   Capture email signups from /signals + the public landing page so we
--   can ship a daily digest of algorithm-derived findings. Also persists
--   the subscriber's filter preferences (severity floor + category
--   allowlist) so the digest is personalized.
--
-- Lifecycle (v1.0):
--   1. POST /api/v1/signals/subscribe → INSERT ... ON CONFLICT DO UPDATE
--      idempotent on email (so repeated signups update prefs instead of
--      erroring). Sets verified=false + verify_token on first signup.
--   2. GET /api/v1/signals/verify?t=<token> flips verified=true.
--      (v1.0 ships the endpoint but no email sender yet — verification
--      links can be hand-issued via psql for the early-access list.)
--   3. Daily digest cron reads WHERE verified=true AND unsubscribed_at IS NULL.
--   4. POST /api/v1/signals/unsubscribe?t=<token> sets unsubscribed_at.
--
-- Design choices:
--   * email is CITEXT for case-insensitive uniqueness without surprising
--     the user about case in their address.
--   * filters is JSONB ({severity_floor: 'high', category_ids: [...]})
--     so we can iterate without a schema migration each time.
--   * verify_token / unsubscribe_token are random base64 (32 bytes).
--     Collisions are astronomically improbable but we still UNIQUE-index
--     them to fail loud if they do.
--   * No FK to anything — subscriptions are a leaf concept.
--
-- Why a separate table (not a column on a generic notification table):
--   * Public-page email signups have very different retention + GDPR
--     posture than internal alerts (a future generic notifications
--     table would not want anonymous emails alongside operator pager
--     state).
--
-- Reverse-out:
--   DROP TABLE IF EXISTS signals_subscription CASCADE;
--   DROP EXTENSION IF EXISTS citext;  -- only if no other table uses it
-- ═══════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS signals_subscription (
    id                  uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    email               citext        NOT NULL UNIQUE,
    -- Filter preferences. Default = match the /signals.rss default
    -- (high severity floor, all categories).
    filters             jsonb         NOT NULL DEFAULT '{"severity_floor":"high","category_ids":[]}'::jsonb,
    -- Source attribution: which page captured this email. Useful when
    -- A/B testing different landing copy.
    source              text          NOT NULL DEFAULT 'unknown',
    -- Double-opt-in plumbing. v1.0 endpoints exist; sender is wired
    -- in a follow-on commit (needs SMTP creds — operator action).
    verified            boolean       NOT NULL DEFAULT false,
    verify_token        text          NOT NULL UNIQUE,
    verified_at         timestamptz,
    unsubscribe_token   text          NOT NULL UNIQUE,
    unsubscribed_at     timestamptz,
    -- Provenance — useful for abuse attribution.
    created_ip          inet,
    user_agent          text,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    updated_at          timestamptz   NOT NULL DEFAULT now()
);

-- Common access patterns:
--   * Daily-digest selector: WHERE verified=true AND unsubscribed_at IS NULL
--   * Token lookup on verify/unsubscribe (already covered by UNIQUE)
CREATE INDEX IF NOT EXISTS signals_subscription_active_idx
    ON signals_subscription (verified, unsubscribed_at)
    WHERE verified = true AND unsubscribed_at IS NULL;

-- Auto-touch updated_at on UPDATE so we always know when filters changed.
CREATE OR REPLACE FUNCTION signals_subscription_touch()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS signals_subscription_touch_trg ON signals_subscription;
CREATE TRIGGER signals_subscription_touch_trg
    BEFORE UPDATE ON signals_subscription
    FOR EACH ROW EXECUTE FUNCTION signals_subscription_touch();

-- ─── schema_migration row ────────────────────────────────────────────────
-- Matches the convention used by 002 / 004 / 006. Belt-and-suspenders: the
-- migration runner also inserts on success, but this lets a hand-applied
-- psql run track the migration too. Idempotent via ON CONFLICT DO NOTHING.

INSERT INTO schema_migration (version, description, applied_by)
VALUES (
    '005-signals-subscription',
    'Email signup table for the daily digest — email (CITEXT) + filter prefs (JSONB) + verify/unsubscribe tokens. Powers /api/v1/signals/{subscribe,verify,unsubscribe} and the digest cron.',
    CURRENT_USER
)
ON CONFLICT (version) DO NOTHING;
