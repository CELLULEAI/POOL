-- 013 — Align accounts schema with VPS prod state
--
-- Several columns were added to accounts via informal ALTER TABLE during
-- early development but never captured in migration files. This broke
-- M11.1 ingest UPSERT on freshly-migrated peer pools (master-pool,
-- gladiator-pool) because their accounts table was strictly the 001 schema.
--
-- Columns added:
--   account_token  : durable API token for account (unique)
--   pseudo         : display identifier
--   worker_ids     : JSON array of worker_ids linked to this account
--   memory_enabled : opt-in RAG memory toggle
--
-- INVARIANT: IDEMPOTENT. Uses ADD COLUMN IF NOT EXISTS so it's safe to
-- run on a DB where some columns already exist (VPS prod case).

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS account_token VARCHAR(128);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS pseudo        VARCHAR(128);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS worker_ids    TEXT DEFAULT '[]';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_token
    ON accounts(account_token) WHERE account_token IS NOT NULL;

COMMENT ON COLUMN accounts.account_token IS
    'Durable API token for the account, derived from email. Used as primary '
    'identity token in cross-pool M11.1 replication. MUST NOT be regenerated '
    'on ingest — the origin pool is the source of truth.';
COMMENT ON COLUMN accounts.pseudo IS
    'Human-readable pseudo chosen at registration, used as display fallback '
    'and RAG memory seed.';
COMMENT ON COLUMN accounts.worker_ids IS
    'JSON array of worker_ids owned by this account. LOCAL (not replicated '
    'via M11.1 ingest — workers are pool-scoped until M11.3 failover).';
COMMENT ON COLUMN accounts.memory_enabled IS
    'Opt-in toggle for RAG memory / user_memories table usage. Replicated '
    'via M11.1 ingest as part of identity.';
