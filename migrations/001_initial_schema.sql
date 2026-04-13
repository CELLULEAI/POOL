-- 001 — Tables principales : workers, api_tokens, jobs, contacts, accounts, sessions
-- Idempotent : IF NOT EXISTS partout

CREATE TABLE IF NOT EXISTS accounts (
    account_id      VARCHAR(64) PRIMARY KEY,
    email           VARCHAR(256) UNIQUE NOT NULL,
    password_hash   VARCHAR(256) NOT NULL,
    display_name    VARCHAR(128),
    eth_address     VARCHAR(42),
    total_credits   REAL DEFAULT 0.0,
    total_earned    REAL DEFAULT 0.0,
    total_spent     REAL DEFAULT 0.0,
    created         TIMESTAMP DEFAULT NOW(),
    last_login      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id       VARCHAR(64) PRIMARY KEY,
    account_id      VARCHAR(64) REFERENCES accounts(account_id) ON DELETE SET NULL,
    machine_hash    VARCHAR(64) NOT NULL,
    hostname        VARCHAR(128),
    cpu             VARCHAR(128),
    cpu_threads     INTEGER DEFAULT 0,
    ram_total_gb    REAL DEFAULT 0,
    ram_available_gb REAL DEFAULT 0,
    model_path      VARCHAR(256),
    bench_tps       REAL,
    first_seen      TIMESTAMP DEFAULT NOW(),
    last_seen       TIMESTAMP DEFAULT NOW(),
    is_online       BOOLEAN DEFAULT FALSE,
    total_jobs      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token           VARCHAR(64) PRIMARY KEY,
    worker_id       VARCHAR(64) NOT NULL REFERENCES workers(worker_id),
    account_id      VARCHAR(64) REFERENCES accounts(account_id) ON DELETE SET NULL,
    credits         REAL DEFAULT 0.0,
    total_earned    REAL DEFAULT 0.0,
    total_spent     REAL DEFAULT 0.0,
    requests_used   INTEGER DEFAULT 0,
    created         TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          VARCHAR(32) PRIMARY KEY,
    worker_id       VARCHAR(64) REFERENCES workers(worker_id),
    account_id      VARCHAR(64) REFERENCES accounts(account_id),
    api_token       VARCHAR(64) REFERENCES api_tokens(token),
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    tokens_per_sec  REAL,
    duration_sec    REAL,
    model           VARCHAR(128),
    credits_earned  REAL DEFAULT 0,
    credits_spent   REAL DEFAULT 0,
    status          VARCHAR(16) DEFAULT 'completed',
    created         TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS contacts (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128),
    email           VARCHAR(256) NOT NULL,
    message         TEXT,
    ip_address      VARCHAR(45),
    created         TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      VARCHAR(64) PRIMARY KEY,
    account_id      VARCHAR(64) NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    created         TIMESTAMP DEFAULT NOW(),
    expires         TIMESTAMP DEFAULT NOW() + INTERVAL '30 days'
);

CREATE TABLE IF NOT EXISTS conversations (
    conv_id         VARCHAR(64) PRIMARY KEY,
    api_token       VARCHAR(64),
    messages        JSONB DEFAULT '[]'::jsonb,
    last_activity   TIMESTAMP DEFAULT NOW(),
    expires         TIMESTAMP DEFAULT NOW() + INTERVAL '1 hour'
);

-- Index
CREATE INDEX IF NOT EXISTS idx_tokens_worker ON api_tokens(worker_id);
CREATE INDEX IF NOT EXISTS idx_tokens_account ON api_tokens(account_id);
CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker_id);
CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created DESC);
CREATE INDEX IF NOT EXISTS idx_workers_online ON workers(is_online) WHERE is_online = TRUE;
CREATE INDEX IF NOT EXISTS idx_workers_machine ON workers(machine_hash);
CREATE INDEX IF NOT EXISTS idx_workers_account ON workers(account_id);
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires);
CREATE INDEX IF NOT EXISTS idx_conv_expires ON conversations(expires);
CREATE INDEX IF NOT EXISTS idx_conv_token ON conversations(api_token);
