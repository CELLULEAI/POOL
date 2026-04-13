-- Migration 016: email verification flow for signup
--
-- Goal: Block ghost accounts on email/password signup by requiring a 6-digit
-- code sent via SMTP. Google OAuth remains auto-verified (email trusted by provider).
--
-- ====================================================================
-- INVARIANT — LOAD-BEARING (guardian validated, see memory:
-- project_activation_email_design.md):
--
-- The 3 columns below are LOCAL to the origin pool.
-- They are EXCLUDED from the /v1/federation/accounts/ingest payload (M11.1).
-- A replicated account (received via M11.1 ingest) is ALWAYS treated as
-- email_verified=TRUE implicitly, because the origin pool only pushes to
-- peers AFTER successful verification.
--
-- DO NOT add these columns to the ingest UPSERT without a dedicated
-- molecule-guardian review — doing so would create a ghost-account bypass
-- across pools (race: user registers on Pool A, pulls replicated row on
-- Pool B with email_verified=false, bypasses verification).
--
-- The DEFAULT TRUE on email_verified ensures:
--   - Existing 17 accounts stay usable (backward compat).
--   - Replicated accounts from peers default to verified.
--   - Only fresh register() calls override to FALSE.
-- ====================================================================

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS verification_code VARCHAR(16),
    ADD COLUMN IF NOT EXISTS verification_expires BIGINT;

-- Index to cleanup expired pending verifications (cron job, phase B).
CREATE INDEX IF NOT EXISTS idx_accounts_verification_expires
    ON accounts (verification_expires)
    WHERE email_verified = FALSE;
