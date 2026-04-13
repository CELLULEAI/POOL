-- 018 — M13 Agent Memory: 4-tier memory system (Phase 1: Working + Episodic)
-- Feature flag: AGENT_MEMORY_ENABLED env var (default off)

-- pg_trgm for keyword search (BM25-like trigram matching)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- TIER 1: WORKING MEMORY (raw observations from tool/sub-agent)
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_observations (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      VARCHAR(64) NOT NULL,
    conv_id         VARCHAR(128),
    job_id          VARCHAR(64),
    source_type     VARCHAR(32) NOT NULL
                      CHECK (source_type IN (
                        'inference','tool_call','sub_agent','compaction',
                        'review','pipeline','federation'
                      )),
    source_id       VARCHAR(128),
    content_enc     TEXT NOT NULL,
    salt            VARCHAR(32) NOT NULL,
    metadata        JSONB DEFAULT '{}'::jsonb,
    embedding       vector(384),
    importance      REAL DEFAULT 0.5,
    consolidated    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_obs_token ON agent_observations(token_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_source ON agent_observations(source_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_unconsolidated ON agent_observations(token_hash)
    WHERE consolidated = FALSE;

-- ============================================================
-- TIER 2: EPISODIC MEMORY (session summaries with decay)
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_episodes (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      VARCHAR(64) NOT NULL,
    conv_id         VARCHAR(128),
    title           VARCHAR(256),
    summary_enc     TEXT NOT NULL,
    salt            VARCHAR(32) NOT NULL,
    embedding       vector(384) NOT NULL,
    outcome         VARCHAR(32) DEFAULT 'neutral'
                      CHECK (outcome IN ('success','failure','neutral','partial')),
    participants    TEXT[] DEFAULT '{}',
    observation_count INTEGER DEFAULT 0,
    importance      REAL DEFAULT 0.5,
    access_count    INTEGER DEFAULT 0,
    last_accessed   TIMESTAMPTZ,
    decay_factor    REAL DEFAULT 1.0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ep_token ON agent_episodes(token_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ep_decay ON agent_episodes(token_hash, decay_factor DESC);

-- ============================================================
-- CONSOLIDATION LOG (append-only audit)
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_consolidation_log (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      VARCHAR(64) NOT NULL,
    consolidation_type VARCHAR(32) NOT NULL
                      CHECK (consolidation_type IN (
                        'observation_to_episode','episode_to_semantic',
                        'semantic_merge','procedure_extract','decay_sweep'
                      )),
    input_count     INTEGER DEFAULT 0,
    output_id       BIGINT,
    worker_id       VARCHAR(128),
    tokens_used     INTEGER DEFAULT 0,
    duration_ms     INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_consol_token ON memory_consolidation_log(token_hash, created_at DESC);

-- IVFFlat indexes (deferred — need rows first, created by consolidation loop)
-- CREATE INDEX idx_obs_embedding ON agent_observations USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);
-- CREATE INDEX idx_ep_embedding ON agent_episodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);

INSERT INTO schema_version (version, filename, applied_at)
VALUES (18, '018_m13_agent_memory.sql', now())
ON CONFLICT DO NOTHING;
