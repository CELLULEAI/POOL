# ARCHITECTURE.md — CELLULE.AI.org

> Pool de calcul distribue pour l inference LLM.
> Document vivant — derniere mise a jour : 2026-04-09.

## Metaphore atomique

CELLULE.AI suit une evolution biologique en trois phases :

| Phase | Unite | Analogie | Etat |
|-------|-------|----------|------|
| Atome | 1 VPS + N workers | Noyau + electrons | **ACTUEL** |
| Molecule | K atomes federes | Atomes lies par covalence | Design |
| Cellule | M molecules coordonnees | Organisme multi-cellulaire | Vision |

## Phase 1 — L Atome (actuel)

Noyau (VPS cellule.ai) + Electrons (workers CPU/GPU).
- Pool FastAPI + WebSocket
- PostgreSQL (L3, RAG pgvector, comptes, tokens)
- 4 LLM sur Z2 : RED 30B, Coder 30B, Tank 35B, Scout 9B
- Memoire L1 (RAM) → L2 (compaction) → L3 (DB) + RAG
- API OpenAI-compatible, tool-calls, think tool, pool_assist

## Phase 2 — La Molecule (federation)

Plusieurs atomes autonomes qui s entraident.
- Discovery : seed list, gossip, DNS SRV
- Trust : Ed25519 keys, challenge-response, 4 niveaux (unknown→bonded)
- Routing inter-atome : forward si sature ou capability manquante
- Comptes federes : token acc_* valide sur tous les atomes bonded
- Endpoints : /v1/federation/info, /job, /handshake, /verify

## Phase 3 — La Cellule (organisme)

Molecules coordonnees, specialisees.
- Molecule CHAT, CODE, RAG, EDGE, TRAINING
- Consensus distribue (Raft simplifie)
- Registre global de capacites
- Auto-routing inter-molecule

## Structure Git recommandee

iamine/
  core/         — noyau (pool, router, db)
  worker/       — electron (worker, proxy, engine)
  memory/       — RAG, L3, compaction
  api/          — routes (inference, auth, admin)
  federation/   — liaison entre atomes (Phase 2)
  agents/       — agents autonomes (RED)
  static/       — frontend
  tests/        — tests
  migrations/   — SQL versionne
  deploy/       — systemd, docker, nginx

## Prerequisites Phase 2

- [x] Pool fonctionnel WebSocket + routing
- [x] PostgreSQL source de verite
- [x] Tokens + credits
- [x] RAG pgvector
- [x] Worker auto-update OTA
- [x] Self-healing
- [x] QoS queue + pending jobs DB
- [ ] Tests automatises
- [ ] pool.py < 1000 lignes (refactoring)
- [ ] Schema DB versionne (migrations SQL)
- [ ] Rate limiting robuste
- [ ] Documentation API OpenAPI

## ADR (Architecture Decision Records)

1. DB-first : PostgreSQL = source de verite, RAM = cache
2. Petit contexte LLM + compactage distribue : 2K-4K ctx, DB gere le long terme
3. Worker-as-electron : contribution = droit d usage ()
4. Doctrine zero-503 : jamais d erreur sans reponse
5. RED exclu du routing public (admin only) — CHANGE: RED maintenant worker standard
