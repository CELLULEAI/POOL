-- 024 — Phase 2.1 : split federation_admin_actions_enabled into query/writes
--
-- Token-guardian VALIDE 2026-04-15 avec 3 invariants :
--   11. Frontiere = "peut changer l'etat economique d'un acteur" (read ON / write OFF)
--   12. Gate admin Ed25519 distincte OBLIGATOIRE avant activation writes future (Phase 2.2)
--   13. Migration CONSERVATRICE : pools upgrades conservent leur valeur.
--       Nouveau defaut s'applique uniquement aux nouvelles installs.
--
-- Motivation David : "penser WALLET et eviter toute usurpation d'identite".
-- query_events = read-only, pas de flux monetaire => ON par defaut pour cohesion communautaire
-- circuit_reset + futures writes = impact indirect revenue_ledger/slashing => OFF par defaut

-- ========================================================================
-- 1) federation_admin_query_enabled
-- ========================================================================
-- Pool existant (flag legacy present) -> copie la valeur legacy (conservateur)
-- Pool neuf (pas de flag legacy)      -> 'true' (nouveau defaut communautaire)
INSERT INTO pool_config (key, value)
SELECT 'federation_admin_query_enabled',
       COALESCE(
           (SELECT value FROM pool_config WHERE key = 'federation_admin_actions_enabled'),
           'true'
       )
ON CONFLICT (key) DO NOTHING;

-- ========================================================================
-- 2) federation_admin_writes_enabled
-- ========================================================================
-- Pool existant -> copie la valeur legacy (conservateur, pas de flip silencieux)
-- Pool neuf     -> 'false' (comportement conservateur conserve pour les writes)
INSERT INTO pool_config (key, value)
SELECT 'federation_admin_writes_enabled',
       COALESCE(
           (SELECT value FROM pool_config WHERE key = 'federation_admin_actions_enabled'),
           'false'
       )
ON CONFLICT (key) DO NOTHING;

-- ========================================================================
-- 3) Conserver federation_admin_actions_enabled comme alias backward-compat
-- ========================================================================
-- Aucune suppression. Le code ne le lira plus directement (is_query_enabled et
-- is_writes_enabled priment), mais le flag reste en DB pour l'UI d'audit et
-- pour migration rollback eventuelle.
