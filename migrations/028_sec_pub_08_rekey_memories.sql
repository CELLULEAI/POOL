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
