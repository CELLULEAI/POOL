-- Migration 025 : Instrumentation smart routing (Phase 1)
-- Ajoute les colonnes nécessaires pour tracer les décisions de routing
-- et préparer le KNN pgvector (Phase 3).
--
-- Référence : project_todo_smart_routing.md
-- David 2026-04-16 : "l'idée est de router suivant le prompt"
-- Priorité : P1 — avant ouverture massive

-- Extension pgvector (idempotent — déjà installée mais safety)
CREATE EXTENSION IF NOT EXISTS vector;

-- Colonnes d'instrumentation sur jobs
-- Tier classifié AVANT routing (heuristique ou KNN selon phase active)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS routed_tier VARCHAR(16);

-- Confidence de la classification [0.0, 1.0] — < 0.7 = ambiguité (fallback)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS route_confidence REAL;

-- Méthode de classification utilisée : 'heuristic', 'knn', 'llm_idle', 'fallback'
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS route_method VARCHAR(32);

-- Embedding du prompt (prêt pour Phase 3 KNN). Dim 384 = all-MiniLM-L6-v2 standard.
-- NULL tant que Phase 3 n'est pas activée. Populé async.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS prompt_embedding vector(384);

-- Index sur routed_tier pour stats agrégées rapides (workers par tier, distribution trafic)
CREATE INDEX IF NOT EXISTS idx_jobs_routed_tier ON jobs(routed_tier);

-- Index ivfflat sur embedding (Phase 3) — créé maintenant avec lists=100 (base,
-- affinable quand > 10k rows). Préféré à hnsw pour notre volume actuel.
-- Ignore-if-exists via DO block (pas de IF NOT EXISTS pour index ivfflat).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_jobs_prompt_embedding') THEN
        CREATE INDEX idx_jobs_prompt_embedding ON jobs
            USING ivfflat (prompt_embedding vector_cosine_ops)
            WITH (lists = 100);
    END IF;
END$$;

-- Table dédiée au feedback routing (Phase 5)
-- Log les signaux "mal-routé" (re-prompt < 30s, user regenerate, etc.)
CREATE TABLE IF NOT EXISTS routing_feedback (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    feedback_signal VARCHAR(32) NOT NULL, -- 'reprompt_fast', 'regenerate', 'user_flag', 'success'
    detected_at TIMESTAMP DEFAULT now(),
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_routing_feedback_job ON routing_feedback(job_id);
CREATE INDEX IF NOT EXISTS idx_routing_feedback_signal ON routing_feedback(feedback_signal);

-- Commentaires pour audit/clarté
COMMENT ON COLUMN jobs.routed_tier IS 'Tier classifié du prompt : small, medium, code, large. Phase 1+';
COMMENT ON COLUMN jobs.route_confidence IS 'Confidence de la classification [0.0, 1.0]. < 0.7 = ambiguité.';
COMMENT ON COLUMN jobs.route_method IS 'Méthode : heuristic | knn | llm_idle | fallback';
COMMENT ON COLUMN jobs.prompt_embedding IS 'Embedding 384d du prompt (all-MiniLM-L6-v2). Phase 3+.';
COMMENT ON TABLE routing_feedback IS 'Feedback signals routing (Phase 5). Re-prompt rapide = mis-routed.';
