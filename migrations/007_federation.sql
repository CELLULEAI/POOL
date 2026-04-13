-- 007 — Molecule v2 : federation Ed25519 + revenue ledger (M3)
-- IAMINE_FED=off au deploiement : tables creees, zero usage runtime.
-- R1 hop counter + forward_chain, R2 workers_certs, R3 molecule_id + capabilities JSONB.

-- ---- federation_self : identite crypto du pool courant --------------------
CREATE TABLE IF NOT EXISTS federation_self (
    atom_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    pubkey          BYTEA NOT NULL,
    privkey_path    TEXT NOT NULL,
    url             TEXT NOT NULL,
    molecule_id     TEXT,
    capabilities    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ---- federation_peers : peers connus + trust level ------------------------
CREATE TABLE IF NOT EXISTS federation_peers (
    atom_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    pubkey          BYTEA NOT NULL,
    url             TEXT NOT NULL,
    molecule_id     TEXT,
    capabilities    JSONB NOT NULL DEFAULT '[]'::jsonb,
    trust_level     INT NOT NULL DEFAULT 0,
    last_seen       TIMESTAMP,
    added_at        TIMESTAMP DEFAULT NOW(),
    added_by        TEXT,
    revoked_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_federation_peers_trust
    ON federation_peers(trust_level) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_federation_peers_molecule
    ON federation_peers(molecule_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_federation_peers_caps
    ON federation_peers USING GIN (capabilities);

-- ---- federation_nonces : anti-replay --------------------------------------
CREATE TABLE IF NOT EXISTS federation_nonces (
    atom_id         TEXT NOT NULL,
    nonce           TEXT NOT NULL,
    seen_at         TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (atom_id, nonce)
);

CREATE INDEX IF NOT EXISTS idx_federation_nonces_seen
    ON federation_nonces(seen_at);

-- ---- workers_certs (R2) : pubkey worker countersignee par son pool --------
CREATE TABLE IF NOT EXISTS workers_certs (
    id              BIGSERIAL PRIMARY KEY,
    worker_id       TEXT NOT NULL,
    pubkey          BYTEA NOT NULL,
    pool_signer     TEXT NOT NULL,
    signature       BYTEA NOT NULL,
    enrolled_at     TIMESTAMP DEFAULT NOW(),
    revoked_at      TIMESTAMP,
    UNIQUE (worker_id, pubkey)
);

CREATE INDEX IF NOT EXISTS idx_workers_certs_worker
    ON workers_certs(worker_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workers_certs_signer
    ON workers_certs(pool_signer);

-- ---- revenue_ledger : journal append-only des inferences -----------------
-- Ecrit no-op en M3 (IAMINE_FED=off), reel a partir de M7.
CREATE TABLE IF NOT EXISTS revenue_ledger (
    id              BIGSERIAL PRIMARY KEY,
    job_id          TEXT NOT NULL,
    origin_pool_id  TEXT NOT NULL,
    exec_pool_id    TEXT NOT NULL,
    worker_id       TEXT NOT NULL,
    worker_cert_id  BIGINT REFERENCES workers_certs(id),
    model           TEXT NOT NULL,
    tokens_in       INT DEFAULT 0,
    tokens_out      INT DEFAULT 0,
    credits_total   BIGINT DEFAULT 0,
    credits_worker  BIGINT DEFAULT 0,
    credits_exec    BIGINT DEFAULT 0,
    credits_origin  BIGINT DEFAULT 0,
    credits_treasury BIGINT DEFAULT 0,
    worker_sig      BYTEA,
    forward_chain   TEXT[] NOT NULL DEFAULT '{}',
    settled         BOOLEAN DEFAULT FALSE,
    settled_at      TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_revenue_ledger_job
    ON revenue_ledger(job_id);
CREATE INDEX IF NOT EXISTS idx_revenue_ledger_unsettled
    ON revenue_ledger(settled, created_at) WHERE settled = FALSE;
CREATE INDEX IF NOT EXISTS idx_revenue_ledger_exec
    ON revenue_ledger(exec_pool_id, created_at);
CREATE INDEX IF NOT EXISTS idx_revenue_ledger_origin
    ON revenue_ledger(origin_pool_id, created_at);

-- ---- federation_settlements : reglements periodiques (M10) ---------------
CREATE TABLE IF NOT EXISTS federation_settlements (
    id              BIGSERIAL PRIMARY KEY,
    peer_id         TEXT NOT NULL REFERENCES federation_peers(atom_id),
    period_start    TIMESTAMP NOT NULL,
    period_end      TIMESTAMP NOT NULL,
    net_credits     BIGINT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    proof           JSONB,
    proposed_at     TIMESTAMP,
    settled_at      TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_federation_settlements_peer
    ON federation_settlements(peer_id, period_end);
CREATE INDEX IF NOT EXISTS idx_federation_settlements_status
    ON federation_settlements(status)
    WHERE status IN ('pending','proposed','disputed');
