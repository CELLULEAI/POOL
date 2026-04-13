-- 005 — Tampon DB anti-saturation + cached responses

CREATE TABLE IF NOT EXISTS pending_jobs (
    job_id          TEXT PRIMARY KEY,
    conv_id         TEXT,
    api_token       TEXT,
    messages        JSONB,
    max_tokens      INT DEFAULT 256,
    requested_model TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    created_at      TIMESTAMP DEFAULT NOW(),
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    worker_id       TEXT,
    response        JSONB,
    error           TEXT,
    priority        INT DEFAULT 0,
    webhook_url     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_jobs(status, created_at);

-- Migration : webhook_url (idempotent)
ALTER TABLE pending_jobs ADD COLUMN IF NOT EXISTS webhook_url TEXT;

CREATE TABLE IF NOT EXISTS cached_responses (
    id              SERIAL PRIMARY KEY,
    pattern         TEXT NOT NULL,
    response        TEXT NOT NULL,
    lang            TEXT DEFAULT 'fr',
    priority        INT DEFAULT 0
);

-- Seed si vide
INSERT INTO cached_responses (pattern, response, lang, priority)
SELECT * FROM (VALUES
    ('bonjour|salut|hello|hi|hey', 'Bonjour ! Je suis IAMINE, un assistant IA distribue. Le pool est actuellement tres charge, mais votre message a ete recu. Reessayez dans quelques instants.', 'fr', 0),
    ('aide|help|comment|how', 'IAMINE est un reseau d''inference IA distribue. Chaque machine qui rejoint le pool contribue sa puissance de calcul et gagne des credits. Pour participer : pip install iamine-ai && python -m iamine worker --auto', 'fr', 0),
    ('what is|c''est quoi|qu''est-ce', 'IAMINE est une plateforme de calcul distribue basee sur des workers LLM. Les machines contribuent leur puissance en rejoignant un pool via iamine-ai.', 'fr', 0),
    ('credit|token|gagner|earn', 'Chaque requete servie par votre worker vous rapporte 1 credit $IAMINE. Ces credits permettent d''utiliser le pool pour vos propres requetes.', 'fr', 0),
    ('modele|model|qwen|llama', 'Le pool utilise principalement des modeles Qwen 3.5 (0.8B a 30B) optimises en GGUF. Le modele est attribue automatiquement selon les capacites de votre machine.', 'fr', 0),
    ('error|erreur|bug|probleme|problem', 'Le pool rencontre un probleme temporaire. Notre equipe travaille a le resoudre. Vos messages sont sauvegardes et seront traites des que possible.', 'fr', 1)
) AS seed(pattern, response, lang, priority)
WHERE NOT EXISTS (SELECT 1 FROM cached_responses LIMIT 1);
