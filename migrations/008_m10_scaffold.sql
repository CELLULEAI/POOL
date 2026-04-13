-- 008 — M10 scaffold: pending_worker_attribution column
--
-- Ajoute une colonne explicite sur revenue_ledger pour marquer les rows
-- dont l'attribution worker (signature Ed25519) n'est pas encore backfillée.
-- Remplace le filtre fragile `worker_sig IS NULL` par un invariant auditable.
--
-- Validated by molecule-guardian 2026-04-10 (C1 obligatoire).
-- Voir project_m10_scaffold_invariants.md

ALTER TABLE revenue_ledger
    ADD COLUMN IF NOT EXISTS pending_worker_attribution BOOLEAN NOT NULL DEFAULT true;

-- Backfill: toutes les rows M7a existantes sont pending (worker_sig=NULL).
-- En M7-worker, le code passera explicitement à false après vérif signature.
UPDATE revenue_ledger
SET pending_worker_attribution = true
WHERE worker_sig IS NULL;

-- Index pour les queries settlement (aggregate_period filter on this column)
CREATE INDEX IF NOT EXISTS idx_revenue_ledger_pending
    ON revenue_ledger(pending_worker_attribution, exec_pool_id, origin_pool_id, created_at)
    WHERE pending_worker_attribution = false;
