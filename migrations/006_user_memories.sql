-- 006 — pgvector + user_memories (RAG vectorise chiffre)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS user_memories (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      VARCHAR(64) NOT NULL,
    embedding       vector(384) NOT NULL,
    fact_text_enc   TEXT NOT NULL,
    salt            VARCHAR(32) NOT NULL,
    conv_id         VARCHAR(64),
    created         TIMESTAMP DEFAULT NOW(),
    last_accessed   TIMESTAMP DEFAULT NOW(),
    access_count    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memories_token ON user_memories(token_hash);
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON user_memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
