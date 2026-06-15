-- sec-pub-08 Phase 2b — re-key des memoires existantes.
--
-- L'isolation des memoires passe de token_hash = sha256(account_token) a account_id
-- (cle stable, decouplee du bearer). Cette migration convertit les lignes deja en
-- base : pour chaque ligne dont token_hash == sha256(account_token) d'un compte,
-- on remplace token_hash par account_id. La colonne garde son nom (token_hash) ;
-- seule la VALEUR stockee change.
--
-- IDEMPOTENT : account_id fait 32 hex chars, sha256 en fait 64 -> une ligne deja
-- migree (token_hash = account_id, 32 chars) ne peut plus matcher un sha256(token)
-- (64 chars), donc re-executer cette migration est un no-op.
--
-- /!\ [CROSS] : la cle d'isolation transite dans le protocole de replication
-- cross-pool. A deployer SIMULTANEMENT (code Phase 2b + cette migration) sur tous
-- les pools federes (VPS + .86 + .30), sinon les payloads sha256 vs account_id ne
-- matchent plus entre pools.
--
-- Les lignes orphelines (token_hash ne correspondant a aucun sha256(account_token) :
-- comptes supprimes, tokens worker/invite) restent inchangees et deviennent
-- invisibles aux requetes indexees par account_id (comportement attendu).
--
-- sha256() est natif PostgreSQL >= 11 (pas besoin de pgcrypto). Le runner gere
-- schema_version lui-meme.

UPDATE user_memories m
   SET token_hash = a.account_id
  FROM accounts a
 WHERE m.token_hash = encode(sha256(a.account_token::bytea), 'hex');

UPDATE agent_observations m
   SET token_hash = a.account_id
  FROM accounts a
 WHERE m.token_hash = encode(sha256(a.account_token::bytea), 'hex');

UPDATE agent_episodes m
   SET token_hash = a.account_id
  FROM accounts a
 WHERE m.token_hash = encode(sha256(a.account_token::bytea), 'hex');

UPDATE agent_procedures m
   SET token_hash = a.account_id
  FROM accounts a
 WHERE m.token_hash = encode(sha256(a.account_token::bytea), 'hex');

UPDATE memory_relationships m
   SET token_hash = a.account_id
  FROM accounts a
 WHERE m.token_hash = encode(sha256(a.account_token::bytea), 'hex');

UPDATE memory_consolidation_log m
   SET token_hash = a.account_id
  FROM accounts a
 WHERE m.token_hash = encode(sha256(a.account_token::bytea), 'hex');

-- ── Purge RGPD des orphelins (comptes supprimes) ─────────────────────────────
-- Apres le re-key, toute ligne dont token_hash n'est PAS un account_id de compte
-- existant = memoire d'un compte SUPPRIME, restee en base a cause de l'ancien bug
-- de suppression (except: pass silencieux). On la supprime = droit a l'oubli
-- effectif (validation live 2026-06-15 : 18 user_memories + 215 agent_observations
-- de comptes supprimes, ne correspondant ni a un account_token, ni au token derive
-- de l'email, ni a un token worker).
-- Idempotent : apres purge il ne reste que des lignes a account_id valide ; un
-- re-run ne supprime plus rien. A executer APRES le re-key ci-dessus (ordre du fichier).

DELETE FROM user_memories t            WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = t.token_hash);
DELETE FROM agent_observations t       WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = t.token_hash);
DELETE FROM agent_episodes t           WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = t.token_hash);
DELETE FROM agent_procedures t         WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = t.token_hash);
DELETE FROM memory_relationships t     WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = t.token_hash);
DELETE FROM memory_consolidation_log t WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = t.token_hash);
