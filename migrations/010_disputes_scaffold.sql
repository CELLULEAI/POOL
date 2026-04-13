-- 010 — M10-active disputes scaffold: federation_disputes table
--
-- Q4 D-DISPUTE scaffold for sampled re-execution (1%) + admin fallback,
-- decided 2026-04-11 after token-guardian + david arbitration.
--
-- INVARIANTS (molecule-guardian validated 2026-04-11) :
--
-- 1. MERKLE EXCLUSION : federation_disputes is INTENTIONALLY excluded from the
--    revenue_ledger merkle root v1 (core/federation_merkle.py). The M11-scaffold
--    invariant 2 states the canonical form is FROZEN at LEDGER_MERKLE_VERSION=1.
--    Any inclusion of federation_disputes in merkle hashing REQUIRES a version
--    bump. Do NOT silently extend the canonical form.
--
-- 2. SCAFFOLD RF=1 : locale au pool. Perdre le pool = perdre l'historique des
--    disputes. M11.2 ajoutera replication gossip. Losing dispute history is
--    accepted in scaffold phase.
--
-- 3. NOT APPEND-ONLY (deliberate divergence from slashing_events) : disputes
--    have naturally stateful semantics (pending -> verified/invalid/expired).
--    The updated_at column traces the last mutation for post-hoc audit. If
--    append-only cross-pool audit becomes required, migrate to dispute_events
--    table with one row per transition — but NOT in scaffold.
--
-- 4. VERIFIER REMUNERATION DEFERRED : the 1% deduction from settlement for
--    the verifier peer is NOT implemented in this table. See FORMULA ASSUMPTION
--    comment in core/federation_disputes.py. Must be added in a future chunk
--    touching propose_settlement with guardian re-invocation.
--
-- 5. NO AUTO-TRIGGER : this scaffold records disputes raised by admin only.
--    No automatic dispute raising from the inference flow. M11-active will
--    add the hook in the inference path with policy validation.

CREATE TABLE IF NOT EXISTS federation_disputes (
    id                BIGSERIAL PRIMARY KEY,
    job_id            TEXT NOT NULL,
    contested_pool_id TEXT NOT NULL,
    origin_pool_id    TEXT,
    verifier_peer_id  TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'verified', 'invalid', 'expired')),
    result            TEXT,
    reason            TEXT,
    created_at        TIMESTAMP DEFAULT now(),
    updated_at        TIMESTAMP DEFAULT now(),
    verified_at       TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_disputes_status
    ON federation_disputes(status, created_at);
CREATE INDEX IF NOT EXISTS idx_disputes_job
    ON federation_disputes(job_id);

COMMENT ON TABLE federation_disputes IS
    'M10-active disputes scaffold (Q4). Mutable status, local RF=1, excluded from '
    'ledger merkle root v1 (M11-scaffold invariant 2 frozen). Verifier remuneration '
    'deferred to settlement.propose_settlement (future chunk).';
