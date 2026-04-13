-- 011 — M11 replication scaffold: replication_queue + replication_state
--
-- Scaffold code for Q1-Q6 RAID decisions arbitrated 2026-04-11 after
-- molecule-guardian analysis and david validation.
-- See project_m11_raid_decisions.md + docs/DECISIONS_PENDING.md category B.
--
-- INVARIANTS (molecule-guardian validated 2026-04-11, verdict CONCERNS 7 recs applied) :
--
-- 1. MERKLE EXCLUSION : replication_queue and replication_state are INTENTIONALLY
--    excluded from the revenue_ledger merkle root v1 (core/federation_merkle.py).
--    The M11-scaffold invariant 2 states the canonical form is FROZEN at
--    LEDGER_MERKLE_VERSION=1. These tables track the replication protocol,
--    NOT authoritative data. Any inclusion in merkle REQUIRES version bump.
--    Do NOT silently extend the canonical form.
--
-- 2. SCAFFOLD RF=1 : these tables are LOCAL to this pool. Losing the pool
--    loses its replication queue + state. M11.2 will add gossip replication
--    for the authoritative tables (revenue_ledger, accounts). The protocol
--    tracking itself stays local — each pool has its own view of what it
--    has and hasn't synced.
--
-- 3. NOT APPEND-ONLY (divergence intentionnelle du pattern slashing_events) —
--    tracking de progression stateful. Both tables have mutable status columns
--    (replication_queue.status : pending/in_progress/done/failed,
--     replication_state.rebuild_status : idle/in_progress/complete/failed).
--    An append-only tracking table would explode in row count without value.
--    If a temporal audit becomes required (M11.4+), create a separate
--    replication_state_history table rather than migrating to append-only.
--
-- 4. NO worker_cert_id FK : the replication protocol operates at peer/pool
--    level, not worker identity. M11-scaffold invariant 1 excludes
--    worker_cert_id from merkle hashing because it's a mutable FK — same
--    reasoning applies here : no FK to workers_certs.
--
-- 5. NO AUTO-TRIGGER : in phase 1 scaffold, these tables are read by helpers
--    but nothing writes to them automatically. M11.2 activates the background
--    loops that consume replication_queue.

CREATE TABLE IF NOT EXISTS replication_queue (
    id              BIGSERIAL PRIMARY KEY,
    peer_atom_id    TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    row_id          BIGINT,
    direction       TEXT NOT NULL
                      CHECK (direction IN ('push', 'pull')),
    status          TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'in_progress', 'done', 'failed')),
    attempts        INT NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_replication_queue_status
    ON replication_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_replication_queue_peer
    ON replication_queue(peer_atom_id, table_name);

COMMENT ON TABLE replication_queue IS
    'M11-scaffold. Work queue for replication tasks (push/pull to/from bonded peers). '
    'NOT APPEND-ONLY (divergence intentionnelle pattern slashing_events) — mutable '
    'status tracks progression. Local RF=1 scaffold. Excluded from merkle root v1.';


CREATE TABLE IF NOT EXISTS replication_state (
    self_atom_id            TEXT PRIMARY KEY,
    rebuild_status          TEXT NOT NULL DEFAULT 'idle'
                              CHECK (rebuild_status IN ('idle', 'in_progress', 'complete', 'failed')),
    last_synced_period      TIMESTAMP,
    last_merkle_root_seen   TEXT,
    last_replication_loop   TIMESTAMP,
    molecule_size           INT,
    quorum_active           BOOLEAN NOT NULL DEFAULT false,
    created_at              TIMESTAMP DEFAULT now(),
    updated_at              TIMESTAMP DEFAULT now()
);

COMMENT ON TABLE replication_state IS
    'M11-scaffold. Single row per self_atom_id (UPSERT pattern). NOT APPEND-ONLY '
    '(divergence intentionnelle pattern slashing_events) — snapshot of current '
    'replication state. If temporal audit required (M11.4+), create separate '
    'replication_state_history table rather than migrating to append-only. '
    'Local RF=1 scaffold. Excluded from merkle root v1.';
