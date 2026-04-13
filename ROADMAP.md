# ROADMAP IAMINE

Derniere mise a jour : 2026-04-06

## Vision

IAMINE = reseau d'inference LLM distribue, inspire de XMRig.
Un exe, un pool, un config → ca tourne. N'importe qui contribue sa puissance de calcul.

## Etat actuel — v0.2.45

| Composant | Statut | Details |
|-----------|--------|---------|
| Pool orchestrateur | PROD | FastAPI, 60+ endpoints, routing cooperatif |
| Workers WebSocket | PROD | --auto, bench-first, self-update 24h |
| Bench-first attribution | PROD | Inference reelle 0.8B → attribution par ratio |
| Conversations persistantes | PROD | Sauvegarde auto, pas d'expiration |
| RAG vectorise | PROD | pgvector + MiniLM-L6-v2, chiffrement zero-knowledge |
| Proxy GPU (Z2) | PROD | 3 backends, 228+ t/s, pool_managed=false |
| Dashboard | PROD | Stats live, leaderboard, auto-refresh |
| API OpenAI-compatible | PROD | /v1/chat/completions |
| Site iamine.org | PROD | Login Google, dashboard, API docs |

**Infrastructure :** 14 workers (10 CPU 0.8B-2B, 4 GPU 4B-30B), 1 VPS orchestrateur

---

## Phase 1 — Stabilisation (maintenant → semaine 2)

### 1.1 Seuil qualite 8 t/s
- [ ] Remonter MIN_TPS dans models.py de 3-6 → 8 t/s
- [ ] Exclure du routing les workers < 8 t/s
- [ ] Nettoyer CPU_SCORES / GPU_SCORES de config.py (code mort)
- [ ] Le bench inference reelle est la SEULE metrique

### 1.2 Nettoyage code
- [ ] Supprimer best_model_for_mobile() — mobile abandonne
- [ ] Supprimer SmolLM2 de MODEL_FAMILIES
- [ ] Retirer les references Android dans pool.py

### 1.3 Securite pre-GitHub
- [ ] Purger credentials de l'historique git (filter-repo)
- [ ] Rotation mots de passe VPS + PostgreSQL + token HF
- [ ] Mettre a jour .gitignore (docs sensibles, reports/claude/)
- [ ] Audit : aucun secret dans le code source

### 1.4 Streaming SSE
- [ ] Reponses token par token (/v1/chat/completions stream=true)
- [ ] Compatible standard OpenAI (data: chunks)
- [ ] Prerequis : workers doivent streamer via WebSocket

---

## Phase 2 — Qualite et macOS (semaines 2-4)

### 2.1 Support macOS / Metal
- [ ] Auto-detection Metal dans config.py
- [ ] Installateur simple (brew ou pip)
- [ ] Test iMac M1/M2 : objectif 20-40 t/s sur 7B-14B
- [ ] Les iMac = cle pour la qualite du pool

### 2.2 Identite unifiee
- [ ] Masquer worker_id dans les reponses API
- [ ] Le pool = un seul LLM "IAMINE" pour l'utilisateur
- [ ] Parametre style: concise/detailed

### 2.3 RAG stabilise
- [ ] Benchmark recall : faits rappeles cross-conversation (objectif 90%+)
- [ ] Tuning extraction de faits (prompt compaction)
- [ ] Monitoring : alertes si RAG recall < 80%

### 2.4 Monitoring prod
- [ ] Alertes pool_load > 80% pendant 5 min
- [ ] Backup DB automatique (PostgreSQL + user_memories)
- [ ] Logs structures pour post-mortem

---

## Phase 3 — RAG distribue (semaines 4-8)

### Prerequis (valides en Phase 2)
- RAG centralise stable, recall > 90%
- Au moins 3 workers GPU (7B+) pour embeddings
- pgvector performant sous charge

### 3.1 Embeddings sur GPU
- [ ] Deporter sentence-transformers sur Z2 GPU (au lieu du VPS CPU)
- [ ] API interne /v1/embed pour les workers
- [ ] Batch embeddings asynchrone

### 3.2 Sharding par utilisateur
- [ ] Partitionner user_memories par hash token
- [ ] Chaque partition sur un noeud different
- [ ] Routing embed : requete → noeud qui a les vecteurs

### 3.3 Vector search distribue
- [ ] Qdrant ou pgvector multi-noeud
- [ ] Scatter-gather : query → N noeuds → merge resultats
- [ ] Latence cible : < 100ms pour top-5 retrieval

---

## Phase 4 — Economie et onboarding (semaines 8-12)

### 4.1 Billing
- [ ] Activer credits reels (desactiver PREPROD_MODE)
- [ ] Stripe / crypto pour acheter des $IAMINE
- [ ] Dashboard credits workers (motivation contributeurs)

### 4.2 Onboarding public
- [ ] Page inscription avec pseudo sur le site
- [ ] Documentation API publique (Swagger/OpenAPI)
- [ ] Guide "contribute your PC" en 60 secondes
- [ ] Frontend chat ameliore (rivaliser avec ChatGPT visuellement)

### 4.3 GitHub public
- [ ] Historique git propre (pas de credentials)
- [ ] README + CONTRIBUTING + LICENSE
- [ ] CI/CD basique (tests + lint)

---

## Phase 5 — Molecules (semaines 12+)

### 5.1 Pool-to-pool (prototype)
- [ ] Protocole de decouverte entre pools
- [ ] Inter-pool routing : job traverse si modele absent localement
- [ ] Machine "open" comme second pool (molecule V2)

### 5.2 Specialisation
- [ ] Pool A (petits modeles rapides), Pool B (GPU gros modeles)
- [ ] DB federee pour contexte inter-pool
- [ ] Annuaire decentralise (DNS-based ou gossip protocol)

### 5.3 La Cellule (vision long terme)
- [ ] Self-healing, self-scaling, self-optimizing
- [ ] Clusters specialises (inference, stockage, routing, verification)
- [ ] Bittensor sans blockchain — mesh P2P de pools IA

---

## Decisions strategiques

| Decision | Date | Raison |
|----------|------|--------|
| Minimum 2B en prod | 2026-04-05 | 0.8B inutile pour du chat, qualite trop faible |
| Mobile abandonne | 2026-04-05 | 0.4 t/s, freine le projet, focus multi-plateforme desktop |
| Bench inference seule | 2026-04-06 | CPU/GPU scores sont des approximations, seul le bench reel compte |
| Seuil 8 t/s | 2026-04-06 | Experience utilisateur minimale pour du chat interactif |
| 3 sous-agents | 2026-04-06 | Plan + Architecture + Securite pour structurer le dev |

---

*Maintenu par l'Agent Plan — mis a jour a chaque milestone.*
