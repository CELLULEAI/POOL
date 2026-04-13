-- 002 — Tables additionnelles : worker_benchmarks, worker_tasks, pool_config, etc.

CREATE TABLE IF NOT EXISTS worker_benchmarks (
    worker_id       VARCHAR(64) PRIMARY KEY,
    model           TEXT,
    bench_tps       REAL,
    cpu_info        TEXT,
    ram_gb          REAL,
    has_gpu         BOOLEAN DEFAULT FALSE,
    gpu_info        TEXT,
    gpu_vram_gb     REAL DEFAULT 0,
    version         TEXT,
    last_updated    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pool_config (
    key             VARCHAR(64) PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hardware_benchmarks (
    id              SERIAL PRIMARY KEY,
    cpu_model       VARCHAR(128) NOT NULL,
    gpu_model       VARCHAR(128) DEFAULT '',
    ram_gb          REAL DEFAULT 0,
    model_id        VARCHAR(64) NOT NULL,
    measured_tps    REAL NOT NULL,
    sample_count    INT DEFAULT 1,
    last_updated    TIMESTAMP DEFAULT NOW(),
    UNIQUE(cpu_model, gpu_model, model_id)
);

CREATE TABLE IF NOT EXISTS worker_tasks (
    task_id         VARCHAR(32) PRIMARY KEY,
    task_type       VARCHAR(32) NOT NULL,
    conv_id         VARCHAR(32),
    assigned_worker VARCHAR(64) NOT NULL,
    source_worker   VARCHAR(64),
    status          VARCHAR(16) DEFAULT 'pending',
    prompt          TEXT,
    result          TEXT,
    created         TIMESTAMP DEFAULT NOW(),
    completed       TIMESTAMP,
    duration_sec    REAL
);

CREATE TABLE IF NOT EXISTS loyalty_rewards (
    id              SERIAL PRIMARY KEY,
    worker_id       VARCHAR(64) NOT NULL,
    amount          REAL NOT NULL,
    label           VARCHAR(16) NOT NULL,
    created         TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_worker ON worker_tasks(assigned_worker);
CREATE INDEX IF NOT EXISTS idx_tasks_conv ON worker_tasks(conv_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON worker_tasks(status);
CREATE INDEX IF NOT EXISTS idx_rewards_worker ON loyalty_rewards(worker_id);

-- Vues
CREATE OR REPLACE VIEW worker_leaderboard AS
SELECT
    w.worker_id, w.account_id, w.hostname, w.cpu, w.ram_total_gb,
    w.total_jobs, w.is_online, t.credits, t.total_earned, t.token
FROM workers w
LEFT JOIN api_tokens t ON t.worker_id = w.worker_id
ORDER BY t.total_earned DESC;

CREATE OR REPLACE VIEW account_dashboard AS
SELECT
    a.account_id, a.email, a.display_name, a.eth_address,
    a.total_credits, a.total_earned, a.total_spent,
    COUNT(w.worker_id) AS worker_count,
    COUNT(w.worker_id) FILTER (WHERE w.is_online) AS workers_online,
    SUM(w.total_jobs) AS total_jobs
FROM accounts a
LEFT JOIN workers w ON w.account_id = a.account_id
GROUP BY a.account_id;

-- Trigger sync credits
CREATE OR REPLACE FUNCTION sync_account_credits()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.account_id IS NOT NULL THEN
        UPDATE accounts SET
            total_credits = (SELECT COALESCE(SUM(credits), 0) FROM api_tokens WHERE account_id = NEW.account_id),
            total_earned = (SELECT COALESCE(SUM(total_earned), 0) FROM api_tokens WHERE account_id = NEW.account_id),
            total_spent = (SELECT COALESCE(SUM(total_spent), 0) FROM api_tokens WHERE account_id = NEW.account_id)
        WHERE account_id = NEW.account_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_credits ON api_tokens;
CREATE TRIGGER trg_sync_credits
    AFTER INSERT OR UPDATE OF credits, total_earned, total_spent ON api_tokens
    FOR EACH ROW EXECUTE FUNCTION sync_account_credits();

-- Cleanup function
CREATE OR REPLACE FUNCTION cleanup_expired_conversations()
RETURNS INTEGER AS $$
DECLARE
    deleted INTEGER;
BEGIN
    DELETE FROM conversations WHERE expires < NOW();
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END;
$$ LANGUAGE plpgsql;
