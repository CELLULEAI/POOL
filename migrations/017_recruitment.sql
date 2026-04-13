-- Migration 017: M12 recruitment — capability gap detection
--
-- EPHEMERAL-ACCEPTABLE: not subject to M11 RAID replication.
-- All rows can be recomputed from live worker connections after a pool
-- restart or rebuild. This table is intentionally pool-local.
--
-- Token-guardian invariants (2026-04-12):
--   - Routing is the sole incentive lever (no rate premium stored here)
--   - Gap corroboration required before influencing routing (corroborated_by)
--   - Flag-gated: IAMINE_RECRUITMENT env var (default off)

CREATE TABLE IF NOT EXISTS recruitment_needs (
    id              SERIAL PRIMARY KEY,
    capability_kind VARCHAR(64)  NOT NULL,   -- e.g. 'llm.chat', 'llm.tool-call'
    model_class     VARCHAR(64)  NOT NULL,   -- e.g. 'proxy-agent', 'reasoning-30b+'
    priority        VARCHAR(16)  NOT NULL DEFAULT 'medium',  -- critical/high/medium/low
    reason          TEXT,
    detected_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ  NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
    corroborated_by JSONB        NOT NULL DEFAULT '[]',
    status          VARCHAR(16)  NOT NULL DEFAULT 'open',    -- open/filled/expired

    -- Only one open gap per (kind, model_class) at a time
    CONSTRAINT uq_recruitment_open UNIQUE (capability_kind, model_class) 
);

CREATE INDEX IF NOT EXISTS idx_recruitment_status ON recruitment_needs (status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_recruitment_expires ON recruitment_needs (expires_at) WHERE status = 'open';
