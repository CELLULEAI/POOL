-- 012 — M11.1 account replication log
--
-- Tracks which peer pools have ACK'd an account ingest. Used by M11.3
-- rebuild loop to target which accounts need to be pushed to which peers
-- after a partition/rebuild event.
--
-- INVARIANTS (molecule-guardian validated 2026-04-11):
--
-- 1. INSERT-ONLY. Each ACK row is immutable once written. An account_id
--    may have multiple rows for the same peer_atom_id (one per gossip
--    cycle) — M11.3 reads the most recent.
--
-- 2. NO FK to accounts. An account may be deleted locally while its
--    replication log remains, for audit. The FK would force CASCADE DELETE
--    which is not what we want.
--
-- 3. EXCLUDED from merkle root v1. This table tracks the replication
--    protocol, not authoritative data. Adding it to the canonical form
--    would require a LEDGER_MERKLE_VERSION bump.
--
-- 4. LOCAL RF=1 (scaffold pattern). Each pool tracks ACKs it has
--    SENT and RECEIVED independently. No cross-pool synchronization
--    of this table itself.

CREATE TABLE IF NOT EXISTS account_replication_log (
    id               BIGSERIAL PRIMARY KEY,
    account_id       VARCHAR(64) NOT NULL,
    peer_atom_id     TEXT NOT NULL,
    direction        TEXT NOT NULL CHECK (direction IN ('push', 'recv')),
    status           TEXT NOT NULL DEFAULT 'ack' CHECK (status IN ('ack', 'failed', 'pending')),
    error_message    TEXT,
    created_at       TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_account_repl_log_account
    ON account_replication_log(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_repl_log_peer
    ON account_replication_log(peer_atom_id, status);

COMMENT ON TABLE account_replication_log IS
    'M11.1 account replication tracking. INSERT-ONLY. Tracks push/recv ACKs '
    'per (account_id, peer_atom_id). Used by M11.3 rebuild targeting. '
    'Excluded from merkle root v1. Local RF=1.';
