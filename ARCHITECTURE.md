# ARCHITECTURE.md — IAMINE.org

> Pool de calcul distribue pour l inference LLM.
> Document vivant — derniere mise a jour : 2026-04-10 (session M3->M10-scaffold).

## Metaphore atomique

IAMINE suit une evolution biologique en trois phases :

| Phase | Unite | Analogie | Etat |
|-------|-------|----------|------|
| Atome | 1 VPS + N workers | Noyau + electrons | **LIVE** |
| Molecule | K atomes federes | Atomes lies par covalence | **BACKPLANE LIVE (preprod observe)** |
| Cellule | M molecules coordonnees | Organisme multi-cellulaire | Vision |

## Phase 1 — L Atome (actuel)

Noyau (VPS iamine.org) + Electrons (workers CPU/GPU).
- Pool FastAPI + WebSocket
- PostgreSQL (L3, RAG pgvector, comptes, tokens)
- 4 LLM sur Z2 : RED 30B, Coder 30B, Tank 35B, Scout 9B
- Memoire L1 (RAM) -> L2 (compaction) -> L3 (DB) + RAG
- API OpenAI-compatible, tool-calls, think tool, pool_assist

---

## Phase 2 — La Molecule (federation) — **LIVE preprod**

Plusieurs atomes autonomes qui s entraident via un backplane crypto signe.

### Feature flag

`IAMINE_FED=off|observe|active`
- **off** : modules charges, zero endpoints actifs, keygen inhibe
- **observe** (defaut preprod) : endpoints repondent, signatures verifiees, en cas d echec log-only
- **active** : endpoints rejettent 401 sur signature invalide

Kill switch **FS prioritaire** : `/etc/iamine/fed_disable` present -> federation forcee off (cache 5s).

### Identite atomique (M3)

Chaque pool a une identite Ed25519 persistee.
- `atom_id` = `sha256(pubkey)` hex 64 chars
- privkey : `~/.iamine/federation/self_ed25519.key` (mode 600)
- pubkey : colonne BYTEA dans `federation_self`
- name : human-readable (ex: `vps-iamine-prod`, `master86-test`)
- `molecule_id` : segment logique (ex: `iamine-testnet`, `iamine-mainnet`)
- `capabilities` : computed live depuis `pool.workers` (pas statique DB)

### Schema DB (migrations 007, 008)

6 tables fédération + 1 colonne M10-scaffold :

```
federation_self         atom_id PK, name, pubkey BYTEA, privkey_path,
                        url, molecule_id, capabilities JSONB, created_at

federation_peers        atom_id PK, name, pubkey BYTEA, url, molecule_id,
                        capabilities JSONB, trust_level INT [0..3],
                        last_seen, added_at, added_by, revoked_at

federation_nonces       (atom_id, nonce) PK, seen_at        # anti-replay

workers_certs           id BIGSERIAL, worker_id, pubkey BYTEA,
                        pool_signer, signature BYTEA,
                        enrolled_at, revoked_at             # R2, M7-worker

revenue_ledger          id BIGSERIAL, job_id, origin_pool_id, exec_pool_id,
                        worker_id, worker_cert_id FK,
                        model, tokens_in, tokens_out,
                        credits_total, credits_worker, credits_exec,
                        credits_origin, credits_treasury,
                        worker_sig BYTEA, forward_chain TEXT[],
                        settled BOOL, settled_at, created_at,
                        pending_worker_attribution BOOL DEFAULT true  # M10 C1

federation_settlements  id BIGSERIAL, peer_id FK, period_start, period_end,
                        net_credits, status, proof JSONB,
                        proposed_at, settled_at, created_at
```

Indexes partiels pour perfs : `idx_federation_peers_trust`, `idx_revenue_ledger_pending`, etc.

### Trust levels (4 niveaux)

| Level | Nom | Privileges | Code check |
|---|---|---|---|
| 0 | unknown | refus | default |
| 1 | known | handshake recu, read-only `/info` | assigne par handshake valide |
| 2 | trusted | forward jobs (M7a), pas accounts | admin promote explicit |
| 3 | bonded | accounts partages, settlement credits | **HARD-LOCKED** code jusqu a M10-active |

`promote_peer()` refuse toute promotion `target_level >= 3` avec message
`"trust level 3 (bonded) requires M10 settlement protocol - not yet available"`.

### Signed envelope (M4)

Headers HTTP inclus dans la signature :

```
X-IAMINE-Atom-Id        <self atom_id>
X-IAMINE-Timestamp      <unix epoch>
X-IAMINE-Nonce          <16 hex chars, single-use 60s window>
X-IAMINE-Hop            <int, max 2>               # R1
X-IAMINE-Forward-Chain  <csv atom_ids>             # R1
X-IAMINE-Signature      base64(Ed25519(sha256(canonical_body)))
```

Canonical body :
```
{method}\n{path}\n{timestamp}\n{nonce}\n{hop}\n{chain_csv}\n{body_bytes}
```

Garanties :
- Anti-replay : nonce stocke dans `federation_nonces` pendant 60s
- Anti-MITM : `atom_id == sha256(pubkey)` verifie a chaque handshake
- Anti-loop (R1) : hop counter `<= HOP_MAX=2`, rejet si `self in forward_chain`, rejet si `chain_len != hop`

### Endpoints /v1/federation/* (13 routes)

**PUBLIC unsigned** (no auth) :
- `GET /info` — identity card (atom_id, pubkey, mode, molecule_id, live capabilities, hop_max)
- `GET /molecule` — discovery : `{self, trusted_peers: [...]}` filtre `trust >= 2` + reachable, ordre last_seen DESC (intentionally NOT hierarchical)

**SIGNED** (envelope Ed25519 verifiee via `enforce_fed_policy`) :
- `POST /handshake` — peer self-declaration. `request_reciprocation: bool` opt-in (cap global 64 in-flight, idempotence skip si L1+ deja connu). `added_by` = `handshake_initial` ou `handshake_reciprocal`.
- `POST /verify` — challenge-response liveness probe
- `POST /job` — inter-atom forwarded inference (M7a, trust >= 2, hop rejet >= HOP_MAX)

**ADMIN** (cookie `admin_token` ou `?token=xxx`) :
- `GET /peers[?include_revoked=1]` — list + pagination
- `GET /peers/{atom_id}` — detail + pubkey_hex
- `DELETE /peers/{atom_id}` — hard-delete (refuse 409 si settlements existent)
- `POST /peers/{atom_id}/promote` (max level=2, hard-lock >=3)
- `POST /peers/{atom_id}/demote`
- `POST /peers/{atom_id}/revoke` (soft, revoked_at=NOW)
- `GET /heartbeat` — metrics peer ping (missed_beats, last_success)
- `GET /ledger?limit=N` — tail revenue_ledger
- `GET /settlement/state` — proposals recent (scaffold: true, authoritative: false)
- `POST /settlement/propose/{peer_atom_id}` — trigger manuel (debug)
- `POST /admin/register` — CLI `iamine pool register` proxy (Model B : pool signs, CLI sans privkey)

### Doctrine "no SPOF middleware" (M6)

Au lieu d un middleware FastAPI global qui intercepte toutes les requetes
(blast radius = tout le pool), enforcement via helper per-route
`fed.enforce_fed_policy(pool, request, method, path, body, peer_pubkey)`.

Un bug local casse 1 endpoint, pas tout le pool. Aligne doctrine
`"toujours repondre"` et mental model RAID.

### Heartbeat peer-to-peer (M7b-server)

Background task toutes les 30s :
- `GET peer.url/v1/federation/info` pour chaque peer `trust >= 2`
- Sanity check : `info.atom_id == peer.atom_id` (detection identity swap)
- Succes -> `mark_peer_seen(last_seen=NOW)`, `missed_beats = 0`
- Echec -> `missed_beats++`, log warning sur 1st/5th/9th miss
- Threshold unreachable : 120s (cooldown)
- Metrics exposees via `GET /heartbeat` admin

### Forwarding inter-atom (M7a server-side)

Scope M7a = server-side pur, sans toucher wheel worker.
- `core/forwarding.py`
- Double opt-in : `FORWARDING_ENABLED=false` default + `FORWARDING_MODE=log_only` default
- `should_forward(pool, model, queue_size)` :
  - Case A : model demande non local + un peer bonded l a dans capabilities
  - Case B : queue locale saturee (threshold default 5, env `POOL_SATURATION_THRESHOLD`)
- `forward_job(pool, peer, ...)` : POST signe a `peer/v1/federation/job`
- Hot-path hook minimal dans `routes/inference.py` (5-10 lignes, try/except fallback local -> doctrine toujours repondre)
- Semaphore dedie `_forwarding_sem = 128`

Le client-side (M7b-client) = pool.urls liste, reconnect loop, seed list = **couple au wheel republish M9b**, differed.

### Settlement scaffold (M10-scaffold)

**M10-scaffold NE fait pas de vrai transfert.** Triple flag :
- `SETTLEMENT_ENABLED=false` default
- `SETTLEMENT_MODE=dry_run|active` default `dry_run`
- `SETTLEMENT_PERIOD_SEC=86400` default (1 jour)

**FORMULA ASSUMPTION** (documente en tete de `federation_settlement.py`) :
> Net settlement = bilateral net delta of (credits_worker + credits_exec + credits_origin).
> **Treasury share (10% bp 1000) is INTENTIONALLY EXCLUDED** from aggregate_period.
> Alternatives not chosen : multilateral clearing via treasury, per-worker settlement,
> merkle-root commit for gossip replication.

**Critique** : `aggregate_period()` filtre `WHERE pending_worker_attribution = false`
(colonne explicite, pas `worker_sig IS NULL`). Rows M7a sans signature worker
verifiee sont **ignorees** jusqu au backfill M7-worker.

### Tests (M9) — 32/32 PASS

```
tests/federation/
  conftest.py                 — helpers (keypair, sign_envelope, post_signed)
  test_envelope.py            — 12 unit tests purs (keygen, hop R1, split 60/20/10/10)
  test_endpoints_chaos.py     — 12 integration (info/molecule public, forged sig,
                                 old ts, replay nonce, atom_id mismatch, hop>2,
                                 trust level 3 HARD-LOCKED, auto-cleanup via DELETE)
  test_settlement.py          — 8 unit/integration (bilateral net, treasury EXCLUDED,
                                 FORMULA header, SETTLEMENT_ENABLED default false)
```

Scripts standalone (pas de pytest). Run :
```bash
cd /home/harpersatrage/iamine
venv/bin/python tests/federation/test_envelope.py
venv/bin/python tests/federation/test_endpoints_chaos.py
venv/bin/python tests/federation/test_settlement.py
```

### CLI `iamine pool *` (M5)

Modele B : CLI sans privkey, pool signe lui-meme via endpoint admin.

```
iamine pool register <url> [--reciprocate] [--name NAME]
iamine pool peers [--all]
iamine pool show <atom_id>
iamine pool promote <atom_id> [--level 2]     # refuse >= 3
iamine pool demote <atom_id> [--level 1]
iamine pool revoke <atom_id>                  # soft
```

Env vars : `IAMINE_POOL_URL` (default `http://127.0.0.1:8080`), `IAMINE_ADMIN_TOKEN` ou `ADMIN_PASSWORD`.

### Admin dashboard (M8)

Onglet **Federation** dans `/admin` :
- Identity card (atom_id, pubkey, mode, live capabilities)
- Add peer form (URL + name + reciprocate)
- Peers table (trust badges, actions promote/demote/revoke/hard-delete)
- Purge revoked bulk button
- Heartbeat metrics table
- Revenue ledger tail (30 rows) avec warning `∅ PENDING` sur worker_sig NULL
- Settlement panel avec badges DISABLED/DRY_RUN/ACTIVE + SCAFFOLD

---

## Phase 3 — La Cellule (organisme)

Molecules coordonnees, specialisees.
- Molecule CHAT, CODE, RAG, EDGE, TRAINING
- Consensus distribue (Raft simplifie)
- Registre global de capacites
- Auto-routing inter-molecule

## Structure Git recommandee

```
iamine/
  core/
    federation.py              — M3/M4 identity + envelope + peers + enforcement
    federation_heartbeat.py    — M7b-server peer ping loop
    federation_settlement.py   — M10-scaffold bilateral net delta
    forwarding.py              — M7a inter-atom job routing
    revenue.py                 — split 60/20/10/10 + forward entry
    startup.py                 — initialize_pool + initialize_federation hook
    ...
  routes/
    federation.py              — 13 endpoints /v1/federation/*
    inference.py               — hot-path /v1/chat/completions + forwarding hook
    ...
  cli/
    federation.py              — iamine pool * subcommands
  static/
    admin_federation.js        — dashboard federation panel
    ...
migrations/
  007_federation.sql           — 6 tables fédération
  008_m10_scaffold.sql         — pending_worker_attribution column
tests/
  federation/
    test_envelope.py           — 12 unit
    test_endpoints_chaos.py    — 12 integration
    test_settlement.py         — 8 scaffold
```

## Prerequisites Phase 2 (DONE session 10 avril)

- [x] Pool fonctionnel WebSocket + routing
- [x] PostgreSQL source de verite
- [x] Tokens + credits
- [x] RAG pgvector
- [x] Worker auto-update OTA
- [x] Self-healing
- [x] QoS queue + pending jobs DB
- [x] Schema DB versionne (migrations SQL 001-008)
- [x] Identite Ed25519 + handshake (M3/M4)
- [x] CLI admin + peer management (M5)
- [x] Enforcement + kill switch FS (M6)
- [x] Forwarding server-side (M7a)
- [x] Admin dashboard UI (M8)
- [x] Tests automatises 32/32 (M9)
- [x] Settlement scaffold (M10-scaffold)
- [ ] **M7-worker** : worker Ed25519 signing (couple M9b wheel)
- [ ] **M7b-client** : failover + seed list (couple M9b wheel)
- [ ] **M10-active** : decisions economiques (treasury, $IAMINE anchor, slashing)
- [ ] **M11 RAID** : replication RF>=2 accounts + ledger + settlements
- [ ] pool.py < 1000 lignes (refactoring, dette M7-worker)

## ADR (Architecture Decision Records)

1. **DB-first** : PostgreSQL = source de verite, RAM = cache
2. **Petit contexte LLM + compactage distribue** : 2K-4K ctx, DB gere le long terme
3. **Worker-as-electron** : contribution = droit d usage ()
4. **Doctrine zero-503** : jamais d erreur sans reponse
5. **Feature flag IAMINE_FED=off|observe|active** : preserve strength #1 review v2
6. **PAS de middleware FastAPI global federation** : per-route helper pour limiter blast radius
7. **Kill switch FS `/etc/iamine/fed_disable`** prioritaire sur env var, fail-open sur perm error
8. **Modele B CLI** : pool signe, CLI sans privkey. Zero duplication de secret crypto
9. **Trust level 3 HARD-LOCKED en code** jusqu a M10-active (pas juste convention)
10. **FORMULA ASSUMPTION documentee** + treasury EXCLU de aggregate_period settlement (evite decision economique baked en scaffold)
11. **pending_worker_attribution colonne explicite** vs `worker_sig IS NULL` convention fragile
12. **NTP Requires=systemd-timesyncd.service** : pool down plutot que clock drift casse nonce window
13. **Split revenu 60/20/10/10 bp** : worker 60%, exec 20%, origin 10%, treasury 10% (edge case exec==origin -> exec 30%)
14. **Mental model RAID** pour tolerance panne molecule (goal contractuel M11 : perte 1 pool >=3 = 0 data loss)
