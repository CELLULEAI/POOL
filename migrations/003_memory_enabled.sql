-- 003 — Opt-in memoire persistante par compte + conversations persistantes

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN DEFAULT FALSE;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title VARCHAR(256) DEFAULT '';
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS message_count INTEGER DEFAULT 0;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS total_tokens INTEGER DEFAULT 0;

ALTER TABLE workers ADD COLUMN IF NOT EXISTS pool_managed BOOLEAN DEFAULT TRUE;
