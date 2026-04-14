-- 021 — Admin users table (required for login + setup wizard)
--
-- This table was previously missing from the migration set, causing fresh
-- Docker installs to fail the setup wizard check ("relation admin_users
-- does not exist") and drop to the login page silently.

CREATE TABLE IF NOT EXISTS admin_users (
    email           VARCHAR(256) PRIMARY KEY,
    password_hash   TEXT,
    created_at      TIMESTAMP DEFAULT now(),
    last_login      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_admin_users_email ON admin_users (email);
