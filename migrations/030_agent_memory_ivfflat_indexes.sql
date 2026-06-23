-- 030 — Index ivfflat sur les embeddings de la memoire agent (M13)
--
-- 018 laissait ces index en commentaire ("deferred — created by consolidation
-- loop"), mais aucune migration ni boucle ne les a jamais crees. Resultat : les
-- recherches semantiques (ORDER BY embedding <=> $vector) sur agent_observations,
-- agent_episodes et agent_procedures faisaient un scan sequentiel + tri complet
-- O(n) par requete, degradant la latence a mesure que la memoire s'accumule
-- (audit 2026-06-22). Seuls user_memories (006) et jobs.prompt_embedding (025)
-- avaient leur ivfflat.
--
-- vector_cosine_ops correspond a l'operateur <=> utilise au runtime (comme 006).
-- lists = 20 (intention d'origine documentee en 018). Garde par to_regclass pour
-- les pools dont les tables M13 n'existent pas encore. Idempotent (IF NOT EXISTS).
-- pgvector est garanti present : la migration 006 cree l'extension + un ivfflat.

DO $$
BEGIN
    IF to_regclass('agent_observations') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS idx_obs_embedding
            ON agent_observations USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);
    END IF;

    IF to_regclass('agent_episodes') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS idx_ep_embedding
            ON agent_episodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);
    END IF;

    IF to_regclass('agent_procedures') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS idx_proc_embedding
            ON agent_procedures USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);
    END IF;
END $$;
