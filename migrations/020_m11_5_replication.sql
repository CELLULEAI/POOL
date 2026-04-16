-- 020 — M11.5: Conversations + memory replication gossip

-- Bootstrap state tracker (per table, per peer) — resumable after interruption
CREATE TABLE IF NOT EXISTS replication_bootstrap_state (
    peer_atom_id    VARCHAR(64) NOT NULL,
    table_name      VARCHAR(64) NOT NULL,
    cursor          VARCHAR(128),              -- PK or ts of last received row
    rows_received   BIGINT DEFAULT 0,
    started_at      TIMESTAMP DEFAULT now(),
    completed_at    TIMESTAMP,
    last_error      TEXT,
    PRIMARY KEY (peer_atom_id, table_name)
);

CREATE INDEX IF NOT EXISTS idx_boot_incomplete
    ON replication_bootstrap_state (peer_atom_id)
    WHERE completed_at IS NULL;

-- Federation peers: add bootstrap and incremental sync tracking columns
ALTER TABLE federation_peers
    ADD COLUMN IF NOT EXISTS bootstrap_state VARCHAR(32) DEFAULT 'pending',
    -- pending | in_progress | complete | failed
    ADD COLUMN IF NOT EXISTS last_sync_memory_ts TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_sync_conv_ts TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_sync_episode_ts TIMESTAMP,
    -- Circuit breaker state (latency tracking)
    ADD COLUMN IF NOT EXISTS latency_ms_avg INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS circuit_slow BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS circuit_failures INTEGER DEFAULT 0;

-- Conversations: track last modification for incremental pull
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS replicated_from_atom_id VARCHAR(64);
-- note: last_activity already exists and is our since_ts cursor

CREATE INDEX IF NOT EXISTS idx_conv_last_activity
    ON conversations (last_activity);

-- Track when memory_relationships were rebuilt post-T3-sync (invariant 3)
ALTER TABLE memory_consolidation_log
    ADD COLUMN IF NOT EXISTS relationships_rebuilt_at TIMESTAMP;

-- Agent episodes: mark timestamp for incremental pull
-- (assume created_at already exists in agent_episodes from M13 migration 018)
CREATE INDEX IF NOT EXISTS idx_episodes_created
    ON agent_episodes (created_at);

-- Audit log of replication activity (append-only)
CREATE TABLE IF NOT EXISTS replication_activity_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMP DEFAULT now(),
    peer_atom_id    VARCHAR(64),
    direction       VARCHAR(16) NOT NULL,      -- 'push' | 'pull' | 'ingest' | 'forget'
    table_name      VARCHAR(64) NOT NULL,
    rows_count      INTEGER DEFAULT 0,
    latency_ms      INTEGER,
    success         BOOLEAN DEFAULT true,
    error_msg       TEXT
);

CREATE INDEX IF NOT EXISTS idx_rep_log_ts ON replication_activity_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_rep_log_peer ON replication_activity_log (peer_atom_id, ts DESC);
