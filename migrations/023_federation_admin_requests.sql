-- 023 — Federation admin actions (console phase 2 Request/Approve)
--
-- MVP cross-pool admin actions avec approbation manuelle du pool cible.
-- Aucun master pool : chaque pool garde veto absolu sur les actions entrantes.
--
-- 3 tables :
--   federation_admin_identities     — EPHEMERAL-ACCEPTABLE (locale, re-enregistrable)
--   federation_admin_requests       — EPHEMERAL-ACCEPTABLE (TTL 24h, transitoire)
--   federation_admin_actions_log    — LOCAL APPEND-ONLY (audit, TODO RF>=2 Phase 3)
--
-- Guardians :
--   molecule-guardian : CONCERNS non-bloquants (2026-04-15)
--   token-guardian    : CONCERNS non-bloquants (2026-04-15)
--
-- Invariants load-bearing :
--   1. Opt-in pool_config.federation_admin_actions_enabled (off par defaut)
--   2. circuit_reset bloque si slashing_events pending/contested (override explicite trace)
--   3. Cooldown 6h par pair/action_type
--   4. query_events : whitelist event_types + filter target_atom_id != self + fenetre 7j
--   5. X-IAMINE-Admin-Email = display-only, JAMAIS dans condition if-auth
--   6. Signature Ed25519 du pool seule autorite d'identification
--   7. Status executed_no_callback distinct pour cas split-brain

-- =========================================================================
-- 1) Admin identities connues par ce pool (LOCALE, non gossipee)
-- =========================================================================
-- replication_strategy: ephemeral-acceptable
-- Raison : gossiper des admin identities creerait surface d'attaque (un peer
-- compromis propagerait une fausse identite). La signature Ed25519 du pool
-- (federation_peers.pubkey) suffit comme source d'autorite. L'email est
-- purement cosmetique pour l'affichage dans l'UI cible.
-- En cas de rebuild d'un pool (M11.3), les admin identities sont
-- re-enregistrables manuellement. Pas de data critique.

CREATE TABLE IF NOT EXISTS federation_admin_identities (
    id                  BIGSERIAL    PRIMARY KEY,
    pool_atom_id        TEXT         NOT NULL,
    admin_email         TEXT         NOT NULL,                 -- display-only
    ed25519_pubkey      TEXT,                                  -- optionnel Phase 2 (reserve si admin key distincte en Phase 3)
    added_at            TIMESTAMP    NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMP,
    notes               TEXT,
    UNIQUE (pool_atom_id, admin_email)
);

CREATE INDEX IF NOT EXISTS idx_admin_identities_pool ON federation_admin_identities (pool_atom_id);

-- =========================================================================
-- 2) Admin requests pending/approved/rejected/expired/executed/executed_no_callback
-- =========================================================================
-- replication_strategy: ephemeral-acceptable
-- Raison : TTL 24h strict. Etats transitoires, pas de consequence economique
-- irreversible. Apres expiration les lignes peuvent etre purgees.
-- Cas split-brain : statut executed_no_callback = action executee cote cible
-- mais callback jamais arrive cote emetteur (visible dans UI pour investigation).

CREATE TABLE IF NOT EXISTS federation_admin_requests (
    request_id          TEXT         PRIMARY KEY,             -- uuid4 genere cote emetteur
    created_at          TIMESTAMP    NOT NULL DEFAULT now(),
    expires_at          TIMESTAMP    NOT NULL,                 -- now() + 24h

    -- Direction : 'outbound' = ce pool a envoye, 'inbound' = recue d'un peer
    direction           VARCHAR(16)  NOT NULL CHECK (direction IN ('outbound', 'inbound')),

    -- Identification cryptographique (Ed25519 du pool emetteur)
    from_atom_id        TEXT         NOT NULL,
    to_atom_id          TEXT         NOT NULL,
    from_admin_email    TEXT,                                  -- display-only, jamais utilise en auth

    -- Action whitelisted
    action_type         VARCHAR(32)  NOT NULL CHECK (action_type IN ('circuit_reset', 'query_events')),
    action_params       JSONB        NOT NULL DEFAULT '{}'::jsonb,

    -- Statut lifecycle
    status              VARCHAR(32)  NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'executed_no_callback', 'failed')),
    decided_at          TIMESTAMP,
    decided_by_email    TEXT,                                  -- display-only
    decision_note       TEXT,

    -- Resultat execution (cote cible)
    execution_result    JSONB,                                 -- payload retour (ex: events pour query_events)
    execution_error     TEXT,

    -- Signature envelope (pour audit, pas pour verification en ligne)
    envelope_sig        TEXT,
    envelope_nonce      TEXT,

    -- Override explicite si slashing_events pending bloque circuit_reset
    slashing_block_override BOOLEAN  NOT NULL DEFAULT FALSE,
    slashing_pending_at_decision JSONB                         -- snapshot des events pending au moment de l'approve
);

CREATE INDEX IF NOT EXISTS idx_admin_requests_status     ON federation_admin_requests (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_requests_direction  ON federation_admin_requests (direction, status);
CREATE INDEX IF NOT EXISTS idx_admin_requests_from       ON federation_admin_requests (from_atom_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_requests_to         ON federation_admin_requests (to_atom_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_requests_expires    ON federation_admin_requests (expires_at) WHERE status = 'pending';

-- =========================================================================
-- 3) Append-only audit log (actions executees des 2 cotes)
-- =========================================================================
-- replication_strategy: local-audit-append-only
-- TODO Phase 3 : evaluer RF>=2 via gossip dedie si ce log devient le seul
-- temoin d'une action contestee cross-pool. Pour MVP : local suffit, les
-- 2 pools (emetteur + cible) detiennent chacun leur copie independante.

CREATE TABLE IF NOT EXISTS federation_admin_actions_log (
    id                  BIGSERIAL    PRIMARY KEY,
    ts                  TIMESTAMP    NOT NULL DEFAULT now(),
    request_id          TEXT         NOT NULL,                 -- FK soft vers federation_admin_requests (pas de ON DELETE CASCADE)
    side                VARCHAR(16)  NOT NULL CHECK (side IN ('emitter', 'target')),
    event_type          VARCHAR(64)  NOT NULL,                 -- 'created', 'approved', 'rejected', 'executed', 'callback_sent', 'callback_received', 'expired', 'override_applied'
    actor_email         TEXT,                                  -- display-only
    actor_atom_id       TEXT,                                  -- pool atom_id qui a declenche l'event
    action_type         VARCHAR(32),
    payload             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_log_request ON federation_admin_actions_log (request_id, ts);
CREATE INDEX IF NOT EXISTS idx_admin_log_ts      ON federation_admin_actions_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_admin_log_event   ON federation_admin_actions_log (event_type, ts DESC);

-- =========================================================================
-- 4) Opt-in flag dans pool_config (off par defaut)
-- =========================================================================
-- Si ce flag est FALSE, les 3 endpoints /v1/federation/admin/* retournent 403.
-- L'operateur doit explicitement activer cette capacite pour son pool.

INSERT INTO pool_config (key, value)
VALUES ('federation_admin_actions_enabled', 'false')
ON CONFLICT (key) DO NOTHING;

-- Cooldown par defaut (secondes) pour circuit_reset par pair emetteur
INSERT INTO pool_config (key, value)
VALUES ('federation_admin_cooldown_circuit_reset_seconds', '21600')
ON CONFLICT (key) DO NOTHING;

-- Whitelist event_types autorises dans query_events cross-pool (JSON list)
INSERT INTO pool_config (key, value)
VALUES ('federation_admin_query_events_whitelist', '["circuit_opened","circuit_closed","worker_joined","worker_left","peer_handshake","overview","peer_status"]')
ON CONFLICT (key) DO NOTHING;

-- Rate limit pending par pair emetteur
INSERT INTO pool_config (key, value)
VALUES ('federation_admin_max_pending_per_peer', '10')
ON CONFLICT (key) DO NOTHING;
