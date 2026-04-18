-- Migration 026: flip memory_enabled default to TRUE
--
-- Why: persistent conversation memory is THE differentiator of cellule.ai
-- ('contexte infini'). Keeping it opt-in created a silent failure mode where
-- new users got empty conversations and never knew why. RGPD compliance is
-- preserved via the dashboard toggle (auth.py::set_memory) + DELETE
-- /v1/account/conversations endpoint (GDPR erasure).
--
-- Scope:
--   1. ALTER column default TRUE (new accounts get memory ON out of the box)
--   2. UPDATE existing accounts that still have FALSE (opt-out remains
--      possible via dashboard; user explicitly set FALSE is NOT overridden
--      because column doesn't track 'user intent' vs 'default')
--
-- Decision 2026-04-18 David : 'absolument garder l'option pour l'utilisateur
-- de pouvoir selectionner ou pas la memoire persistante (RGPD)'
-- => default ON, toggle UI preserved, conv delete preserved.

ALTER TABLE accounts ALTER COLUMN memory_enabled SET DEFAULT TRUE;

UPDATE accounts SET memory_enabled = TRUE WHERE memory_enabled IS FALSE OR memory_enabled IS NULL;

COMMENT ON COLUMN accounts.memory_enabled IS
  'Persistent conversation memory (infinite context). Default TRUE since migration 026 (2026-04-18). Users can opt out via dashboard toggle (auth.py::set_memory) — RGPD compliant.';
