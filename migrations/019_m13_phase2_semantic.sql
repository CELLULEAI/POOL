-- 019 — M13 Phase 2: Semantic memory enhanced + Procedural + Relationships

-- Enhance user_memories with category, confidence, decay
ALTER TABLE user_memories
    ADD COLUMN IF NOT EXISTS category VARCHAR(64) DEFAULT 'fact',
    ADD COLUMN IF NOT EXISTS confidence REAL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS decay_factor REAL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS source_episode_id BIGINT,
    ADD COLUMN IF NOT EXISTS superseded_by BIGINT;

-- Relationship graph between memory items
CREATE TABLE IF NOT EXISTS memory_relationships (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      VARCHAR(64) NOT NULL,
    source_id       BIGINT NOT NULL,
    target_id       BIGINT NOT NULL,
    source_table    VARCHAR(32) NOT NULL
                      CHECK (source_table IN ('user_memories','agent_episodes')),
    target_table    VARCHAR(32) NOT NULL
                      CHECK (target_table IN ('user_memories','agent_episodes')),
    relation_type   VARCHAR(64) NOT NULL,
    strength        REAL DEFAULT 0.5,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rel_source ON memory_relationships(source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON memory_relationships(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_rel_token ON memory_relationships(token_hash);

-- Procedural memory (workflows, patterns, preferences)
CREATE TABLE IF NOT EXISTS agent_procedures (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      VARCHAR(64) NOT NULL,
    name            VARCHAR(256) NOT NULL,
    description_enc TEXT NOT NULL,
    salt            VARCHAR(32) NOT NULL,
    trigger_pattern TEXT,
    steps_enc       TEXT NOT NULL,
    embedding       vector(384) NOT NULL,
    success_rate    REAL DEFAULT 0.5,
    use_count       INTEGER DEFAULT 0,
    last_used       TIMESTAMPTZ,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_proc_token ON agent_procedures(token_hash) WHERE active = TRUE;

INSERT INTO schema_version (version, filename, applied_at)
VALUES (19, '019_m13_phase2_semantic.sql', now())
ON CONFLICT DO NOTHING;
