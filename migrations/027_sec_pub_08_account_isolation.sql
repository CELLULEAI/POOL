-- sec-pub-08 Phase 2 — decouplage du token de compte.
--
-- Ajoute une cle de chiffrement DEDIEE par compte (enc_key), aleatoire et stable,
-- decouplee du bearer (account_token). Cela permet : (1) un bearer fuite ne suffit
-- plus a dechiffrer les memoires, (2) la rotation du bearer sans re-chiffrement,
-- (3) une suppression RGPD reellement irrecuperable (DELETE accounts emporte enc_key).
--
-- enc_key est genere cote applicatif au register (secrets), comme account_token.
-- Aucun DEFAULT SQL (la valeur doit etre maitrisee par le code). Aucun index
-- (toujours atteint via account_id PK). NON repliquee cross-pool (zero-knowledge :
-- seul le pool d'origine dechiffre ses propres blobs).
--
-- Coupe propre (prod sans utilisateur) : aucun backfill necessaire. Le runner
-- (db.py run_migrations) gere lui-meme schema_version — ne pas l'inserer ici.

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS enc_key TEXT;

COMMENT ON COLUMN accounts.enc_key IS
  'sec-pub-08: cle de chiffrement Fernet dediee, aleatoire stable par compte, decouplee de account_token. Generee cote app au register. NON repliquee cross-pool.';
