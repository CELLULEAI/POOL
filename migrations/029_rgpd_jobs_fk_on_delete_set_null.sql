-- 029 — RGPD : jobs.account_id / jobs.api_token en ON DELETE SET NULL
--
-- Avant : les FK jobs.account_id -> accounts et jobs.api_token -> api_tokens
-- etaient declarees SANS clause ON DELETE (= NO ACTION / RESTRICT). Consequence :
-- impossible de supprimer un compte ayant genere au moins un job (cas normal) ->
-- violation de contrainte FK. Cote admin l'erreur etait avalee silencieusement
-- (compte fantome : disparu de la RAM mais survivant en DB avec email +
-- password_hash) ; cote self-service elle renvoyait un 500 -> droit a l'oubli
-- (RGPD art. 17) non honore.
--
-- Un job anonymise (account_id / api_token = NULL) reste comptable pour le
-- revenue_ledger, donc SET NULL est le bon comportement.
--
-- Idempotent : on supprime TOUTE FK existante portant sur ces colonnes (le nom
-- auto-genere peut varier d'un pool a l'autre) puis on re-cree avec ON DELETE
-- SET NULL. Rejouable sans effet de bord.

DO $$
DECLARE
    c record;
BEGIN
    FOR c IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_attribute att
             ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey)
        WHERE rel.relname = 'jobs'
          AND con.contype = 'f'
          AND att.attname IN ('account_id', 'api_token')
    LOOP
        EXECUTE format('ALTER TABLE jobs DROP CONSTRAINT %I', c.conname);
    END LOOP;

    ALTER TABLE jobs ADD CONSTRAINT jobs_account_id_fkey
        FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE SET NULL;

    ALTER TABLE jobs ADD CONSTRAINT jobs_api_token_fkey
        FOREIGN KEY (api_token) REFERENCES api_tokens(token) ON DELETE SET NULL;
END $$;
