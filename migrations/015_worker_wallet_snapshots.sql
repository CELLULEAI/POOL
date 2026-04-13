-- 015 — M11.3 worker wallet snapshots (SCHEMA ONLY)
--
-- Scaffold table for the future worker wallet sync primitive (M11.3b).
-- NO helper writes to this table in M11.3 phase 1. The snapshot_worker_wallet
-- function is a strict skeleton returning not_implemented until a dedicated
-- token-guardian session defines:
--   1. The envelope shape for GET /v1/federation/worker/wallet-snapshot
--   2. The merge policy when a worker returns to its origin pool with a
--      different balance earned on the failover pool
--   3. The anti-double-spend invariant
--
-- INVARIANTS (molecule-guardian M11.3 verdict 2026-04-11):
--
-- 1. EPHEMERAL-ACCEPTABLE until M11.3b : if origin pool dies before snapshot
--    gossip, the snapshot is lost. Explicitly acceptable in scaffold phase
--    because flag is off and no automatic sync runs.
--
-- 2. INSERT-ONLY. Each snapshot row is immutable once written. Amending
--    a snapshot requires writing a new row with a newer ts.
--
-- 3. NO FK to api_tokens or workers. An archived worker may have snapshots
--    persisted for audit even after the worker row is deleted.
--
-- 4. Excluded from merkle root v1. Replication protocol tracking, not
--    authoritative ledger data.

CREATE TABLE IF NOT EXISTS worker_wallet_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    worker_id       VARCHAR(64) NOT NULL,
    origin_pool_id  TEXT NOT NULL,
    credits         BIGINT NOT NULL DEFAULT 0,
    total_earned    BIGINT NOT NULL DEFAULT 0,
    total_spent     BIGINT NOT NULL DEFAULT 0,
    ts              TIMESTAMP DEFAULT now(),
    signature       BYTEA,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_wallet_snapshot_worker
    ON worker_wallet_snapshots(worker_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_snapshot_origin
    ON worker_wallet_snapshots(origin_pool_id, ts DESC);

COMMENT ON TABLE worker_wallet_snapshots IS
    'M11.3 scaffold. SCHEMA ONLY, no active helper writes. Future wallet '
    'sync M11.3b : when a worker fails over, the accepting pool requests '
    'the latest signed snapshot from the origin to bootstrap balance. '
    'EPHEMERAL-ACCEPTABLE until M11.3b activates the merge policy. '
    'INSERT-ONLY. Excluded from merkle v1. Token-guardian session required '
    'before activation.';
