-- 014 — M11.2 revenue_ledger idempotent gossip
--
-- Adds UNIQUE (origin_pool_id, job_id) to revenue_ledger so that the
-- M11.2 gossip loop can use ON CONFLICT DO NOTHING for idempotent
-- cross-pool replication. Without this, a gossip pull would re-insert
-- rows already received in previous cycles.
--
-- INVARIANTS (molecule-guardian validated 2026-04-11):
--
-- 1. Append-only preserved : ON CONFLICT DO NOTHING never updates
--    existing rows. Divergence on mutable columns (worker_sig,
--    pending_worker_attribution) across peers is BY DESIGN (canonical
--    form v1 excludes them from merkle).
--
-- 2. Safe to run : revenue_ledger is currently empty on all 3 pools
--    (observe mode, no settlement yet). The UNIQUE constraint is
--    applied on zero rows, so no duplicates to reconcile.
--
-- 3. A single job_id MAY exist from multiple origin_pool_id values
--    (e.g. M7a forwarding creates chained origin != exec records).
--    The UNIQUE is on the COMBINATION, not job_id alone.

CREATE UNIQUE INDEX IF NOT EXISTS idx_revenue_ledger_origin_job_unique
    ON revenue_ledger(origin_pool_id, job_id);

COMMENT ON INDEX idx_revenue_ledger_origin_job_unique IS
    'M11.2 gossip idempotency. Same (origin_pool_id, job_id) is the same '
    'logical ledger entry. ON CONFLICT DO NOTHING on this index is the '
    'gossip dedup primitive. Added 2026-04-11 per M11.2 molecule-guardian.';
