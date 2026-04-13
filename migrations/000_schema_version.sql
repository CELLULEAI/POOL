-- Schema versioning table — tracks applied migrations
CREATE TABLE IF NOT EXISTS schema_version (
    version     INT PRIMARY KEY,
    filename    VARCHAR(255) NOT NULL,
    applied_at  TIMESTAMP DEFAULT NOW()
);
