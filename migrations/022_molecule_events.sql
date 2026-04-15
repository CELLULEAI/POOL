-- 022 — Molecule admin audit events (console phase 1 read-only)
--
-- EPHEMERAL-ACCEPTABLE — local audit trail, RF=1 intentionnel, not gossiped.
-- This table records every admin query launched from the Molecule Console
-- (fan-out overviews, peer status fetches). It is scoped to the pool that
-- hosts the admin session. Peer pools keep their own molecule_events row
-- independently; there is no cross-pool replication, no merkle, no gossip.
--
-- Guardian: validé par molecule-guardian 2026-04-15 (Phase 1 design).

CREATE TABLE IF NOT EXISTS molecule_events (
    id              BIGSERIAL   PRIMARY KEY,
    ts              TIMESTAMP   NOT NULL DEFAULT now(),
    admin_email     TEXT        NOT NULL,
    query_type      VARCHAR(64) NOT NULL,                -- 'overview', 'peer_status', 'workers', 'ledger_summary', 'events'
    target_atom_id  TEXT,                                 -- NULL if fan-out to all peers; else specific atom_id queried
    result_status   VARCHAR(16) NOT NULL DEFAULT 'ok',    -- 'ok', 'partial', 'error'
    unreachable     JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- list of atom_ids that did not respond
    summary         TEXT,                                 -- short human-readable summary (<512 chars)
    latency_ms      INTEGER                               -- aggregate wall-clock of the fan-out
);

CREATE INDEX IF NOT EXISTS idx_molecule_events_ts   ON molecule_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_molecule_events_type ON molecule_events (query_type, ts DESC);
