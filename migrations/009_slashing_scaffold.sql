-- 009 — M10-active slashing scaffold: slashing_events table
--
-- Append-only log of burn events for Q3 slashing policy (progressive + economic
-- slashing with BURN of confiscated credits, decided 2026-04-11 after
-- token-guardian validation and david arbitration).
--
-- INVARIANTS (molecule-guardian validated 2026-04-11):
--
-- 1. APPEND-ONLY : no UPDATE, no DELETE. Only INSERT. Auditable by design.
--
-- 2. MERKLE EXCLUSION : slashing_events is INTENTIONALLY excluded from the
--    revenue_ledger merkle root v1 (core/federation_merkle.py). The M11-scaffold
--    invariant 2 states the canonical form is FROZEN at LEDGER_MERKLE_VERSION=1.
--    Any inclusion of slashing_events in merkle hashing REQUIRES a version bump.
--    Do NOT silently extend the canonical form.
--
-- 3. SCAFFOLD RF=1 : no cross-pool replication. Each pool keeps its own burn
--    events locally. If the pool dies, its slashing_events are lost. M11.2 will
--    add gossip replication. Losing burn history is accepted in scaffold phase.
--
-- 4. NO WORKER_CERT FK : job_id stays TEXT, no FK to workers_certs. The M11
--    invariant 1 excludes worker_cert_id from merkle hashing precisely because
--    it is a mutable FK (re-enrollment can change it). Same reasoning applies
--    here. If a specific cert matters for the burn, encode it in reason text.
--
-- 5. ADMIN-LOCAL ONLY : in phase 1, burns are triggered by admin local endpoint
--    only. No peer-triggered auto-slash until M11-active (which will require
--    a cross-pool consensus policy not yet decided).

CREATE TABLE IF NOT EXISTS slashing_events (
    id          BIGSERIAL PRIMARY KEY,
    peer_id     TEXT NOT NULL,
    job_id      TEXT,
    amount      BIGINT NOT NULL CHECK (amount > 0),
    reason      TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_slashing_peer_ts
    ON slashing_events(peer_id, created_at);

COMMENT ON TABLE slashing_events IS
    'M10-active slashing scaffold. Append-only burn log. Local to pool (RF=1). '
    'Excluded from ledger merkle root v1 (M11-scaffold invariant 2 frozen format). '
    'Admin-local triggers only in phase 1.';
