# RUNBOOK — Bootstrap d'un nouveau pool IAMINE fédéré

**Origine** : ce runbook est le résultat du bootstrap réel de Gladiator (.30) comme pool #2 le 2026-04-11, depuis un pool fresh jusqu'au handshake fédéré bi-directionnel avec iamine.org. Chaque commande a été exécutée en production et le résultat est documenté. Si une étape échoue, la section **Troubleshooting** en fin de doc liste les erreurs réelles rencontrées et leur fix.

**Public cible** : un membre de la communauté qui veut exposer sa machine comme **pool** fédéré (pas juste worker) pour participer au maillage molécule.

**Scope** : voie manuelle. Une voie rapide via `docker compose` est en préparation (`docker-compose.yml` + image `iamineorg/pool`) — voir TODO image docker dans memory. Ce runbook reste la référence pour déboguer et comprendre ce qui se passe en dessous.

---

## Pré-requis

| Élément | Exigence | Vérification |
|---|---|---|
| OS | Linux (Ubuntu/Debian/AnduinOS 24.x+) | `cat /etc/os-release` |
| Python | 3.11+ (3.13 testé) | `python3 --version` |
| sudo | NOPASSWD ou admin disponible | `sudo -n true` |
| Disque libre | >= 10 Go | `df -h /home` |
| RAM libre | >= 2 Go (pool + postgres) | `free -h` |
| Port 8080 | libre en entrée | `ss -tlnp | grep 8080` |
| Port 5432 | libre en loopback | `ss -tlnp | grep 5432` |
| NAT inbound | TCP public:8080 -> LAN:8080 | depuis internet : `curl http://<ip-publique>:8080` |
| URL publique | HTTP/S résolvable ou IP publique | au choix |

**Note coexistence worker+pool** : si la machine tourne déjà un `iamine worker`, le pool et le worker cohabitent sans conflit (worker = client WebSocket sortant, pool = serveur HTTP entrant, ports distincts). Les deux services systemd sont indépendants.

---

## Phase 0 — Reconnaissance (safe, zéro modif)

Objectif : confirmer que la machine a bien toutes les ressources et que rien n'empêche l'install.

```bash
# Identité machine
whoami
hostname
uname -a
uptime

# Ressources
df -h /
free -h
lscpu | grep 'Model name'
nvidia-smi --query-gpu=name,memory.total --format=csv 2>/dev/null || echo 'no GPU'

# Environnement logiciel
python3 --version
which python3
sudo -n true && echo 'sudo OK' || echo 'sudo needs password'
docker --version 2>&1 || echo 'docker absent - sera installé Phase 2'

# Ports libres
ss -tlnp 2>/dev/null | grep -E ':8080|:5432' || echo 'ports libres'

# Worker existant ? (si oui, vérifier env)
systemctl is-active iamine.service 2>&1
# Si actif, vérifier l'env :
cat /proc/$(pgrep -f 'iamine worker' | head -1)/environ 2>/dev/null | tr '\0' '\n' | grep -i IAMINE
# ATTENTION : si IAMINE_FED est set côté worker, l'unset avant de continuer
# (risque de collision federation_self puisque worker et pool partageraient ~/.iamine/federation/)
```

**Critères go/no-go** : tout vert ? Phase 1. Un rouge ? Corriger avant.

---

## Phase 1 — Arborescence + venv pool isolé

Objectif : créer un venv dédié au pool, isolé du worker existant (si présent) et du système. Zéro impact sur l'install Python globale.

```bash
cd ~
mkdir -p iamine-pool
cd iamine-pool

python3 -m venv venv
. venv/bin/activate

pip install --upgrade pip
pip install 'iamine-ai==0.2.49' \
    -i https://iamine.org/pypi \
    --extra-index-url https://pypi.org/simple

# BUG WHEEL CONNU : asyncpg n'est pas dans les deps officielles
# Bypass manuel jusqu'au fix 0.2.50 :
pip install asyncpg
```

**Vérif** :

```bash
./venv/bin/python -m iamine --help
# doit afficher : worker, pool, bench, recommend, download, wallet, ask, init, proxy
```

**Rollback** : `rm -rf ~/iamine-pool`

---

## Phase 2 — Postgres docker local (image pgvector)

**Piège critique** : l'image `postgres:16` vanilla ne contient pas l'extension `vector`. La migration 006 (`user_memories` + RAG) exige `CREATE EXTENSION vector` et échoue sinon. Utiliser impérativement `pgvector/pgvector:pg16`.

### 2a — Installer docker si absent

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # prend effet au prochain login
```

### 2b — Lancer le container postgres + pgvector

```bash
# Variables à personnaliser
POSTGRES_PASSWORD=change_me_strong_password

sudo docker volume create iamine-pg-data

sudo docker run -d \
    --name iamine-pg \
    --restart=unless-stopped \
    -p 127.0.0.1:5432:5432 \
    -e POSTGRES_USER=iamine \
    -e POSTGRES_PASSWORD=$POSTGRES_PASSWORD \
    -e POSTGRES_DB=iamine \
    -v iamine-pg-data:/var/lib/postgresql/data \
    pgvector/pgvector:pg16
```

**Note** : `-p 127.0.0.1:5432:5432` restreint au loopback local, postgres n'est jamais exposé au réseau.

**Vérif** :

```bash
sleep 5
sudo docker logs iamine-pg 2>&1 | tail -5
# Doit afficher : 'database system is ready to accept connections'

sudo docker exec iamine-pg psql -U iamine -d iamine \
    -c "SELECT * FROM pg_available_extensions WHERE name = 'vector';"
# Doit afficher une ligne avec default_version >= 0.8
```

**Rollback** : `sudo docker rm -f iamine-pg && sudo docker volume rm iamine-pg-data`

---

## Phase 3 — Migrations DB (workaround bug wheel)

**Piège critique #2** : le wheel `iamine-ai` **ne contient pas le dossier `migrations/`**. Sans intervention, le pool tombe dans un fallback legacy qui crée un schéma dégradé SANS les tables de fédération, de ledger, de slashing, de disputes, de replication. C'est invisible au boot (pas d'erreur) mais casse toute utilisation réelle.

**Fix temporaire** (jusqu'au wheel 0.2.50 qui embarquera les migrations) : copier le dossier `migrations/` depuis un checkout git du dépôt `iamine-ai` ou depuis un pool existant.

```bash
# Option A : depuis un checkout git
git clone <repo-url> /tmp/iamine-git
cp -r /tmp/iamine-git/migrations \
    ~/iamine-pool/venv/lib/python3.13/site-packages/migrations

# Option B : depuis un pool existant via scp
scp -r user@existing-pool:/path/to/iamine/migrations \
    ~/iamine-pool/venv/lib/python3.13/site-packages/migrations

# Option C : depuis iamine.org (tarball à publier, TODO)
# curl -sL https://iamine.org/pool/migrations-0.2.49.tgz | \
#     tar -xz -C ~/iamine-pool/venv/lib/python3.13/site-packages/
```

**Vérif** :

```bash
ls ~/iamine-pool/venv/lib/python3.13/site-packages/migrations/ | wc -l
# Doit être >= 12 (000_schema_version.sql à 011_replication_scaffold.sql)
```

---

## Phase 4 — Fichier .env pool

```bash
cat > ~/iamine-pool/.env <<EOF
IAMINE_DB=postgres
DB_HOST=127.0.0.1
DB_PORT=5432
DB_USER=iamine
DB_PASS=$POSTGRES_PASSWORD
DB_NAME=iamine

# Fédération
IAMINE_FED=observe
IAMINE_FED_NAME=my-pool-name
IAMINE_FED_URL=http://<ip-publique>:8080
IAMINE_FED_MOLECULE=iamine-testnet
EOF

chmod 600 ~/iamine-pool/.env
```

**Valeurs à personnaliser** :
- `DB_PASS` : le mot de passe choisi en 2b
- `IAMINE_FED_NAME` : nom lisible de ton pool (ex: `gladiator-pool`, `alice-lab-pool`)
- `IAMINE_FED_URL` : l'URL publique à laquelle iamine.org doit pouvoir te joindre. **Obligatoire en http(s)://**, pas d'IP nue. Exemples valides :
  - `http://<ip-publique-v4>:8080`
  - `https://monpool.mondomaine.fr`
- `IAMINE_FED_MOLECULE` : `iamine-testnet` pour le réseau actuel.

**Note sécurité** : `chmod 600` pour protéger le mot de passe.

---

## Phase 5 — Boot test en foreground

Objectif : valider que le pool démarre proprement, applique les 11 migrations, initialise `federation_self`, et répond sur `/v1/federation/info`.

```bash
cd ~/iamine-pool
set -a && . ./.env && set +a
./venv/bin/python -m iamine pool --host 0.0.0.0 --port 8080
```

**Logs attendus** (succès) :

```
 * IAMINE POOL  v0.2.49
 * LISTEN       0.0.0.0:8080
 ...
iamine.db          PostgreSQL connected: 127.0.0.1:5432/iamine
iamine.db          Migration 001_initial_schema.sql applied successfully
iamine.db          Migration 002_extensions.sql applied successfully
... (jusqu'à 011)
iamine.db          11 migration(s) applied, now at version 11
iamine.pool        L3 PostgreSQL store active - production mode
iamine.federation  federation: generating new Ed25519 keypair at /home/<user>/.iamine/federation/self_ed25519.key
iamine.federation  federation: mode=observe atom_id=<64hex>... name='<POOL_NAME>' molecule=iamine-testnet
iamine.federation  federation: peer heartbeat loop scheduled
iamine.pool        Heartbeat loop started (every 30s, timeout 90s)
```

**Test en parallèle** (depuis un autre shell sur la même machine) :

```bash
curl http://127.0.0.1:8080/v1/federation/info
# Doit retourner JSON {mode, atom_id, pubkey_hex, name, url, molecule_id, ...}
```

**Si tu vois** `Migrations dir not found` -> retour Phase 3, les migrations n'ont pas été copiées correctement.

**Si tu vois** `extension "vector" is not available` -> retour Phase 2, tu utilises la mauvaise image postgres.

**Si tout est vert**, Ctrl+C pour arrêter, on passe au handshake.

---

## Phase 6 — Handshake fédération avec iamine.org

Deux moitiés : (1) test du NAT/accès public, (2) handshake VPS -> ton pool.

### 6a — Vérifier accès public

Depuis **une autre machine** sur internet (par exemple le VPS iamine.org, ou ton téléphone en 4G si tu veux un test indépendant de ton LAN) :

```bash
curl -v http://<ton-ip-publique>:8080/v1/federation/info
```

**Attendu** : HTTP 200 + JSON `atom_id`/`pubkey_hex`/`name`. Si pas de réponse, la règle NAT TCP public:8080 -> LAN:8080 n'est pas en place sur ton routeur.

### 6b — Demander le handshake à iamine.org

Depuis le **VPS iamine.org** (par un admin iamine.org qui dispose de `IAMINE_ADMIN_TOKEN`) :

```bash
IAMINE_ADMIN_TOKEN=<token> \
IAMINE_POOL_URL=http://127.0.0.1:8080 \
python3 -m iamine pool register http://<ton-ip-publique>:8080 \
    --name <ton-pool-name> \
    --reciprocate
```

**Attendu** :

```
handshake ok : 'my-pool-name' (<16hex>...)
  our_trust_level_on_target: 1
  signature_verified_by_target: True
  reciprocation: requested - check `iamine pool peers` in ~5s
```

### 6c — Vérifier la réciprocité

**Côté iamine.org** :

```bash
IAMINE_ADMIN_TOKEN=<token> python3 -m iamine pool peers
# Doit lister ton pool
```

**Côté ton pool** (local) :

```bash
curl 'http://127.0.0.1:8080/v1/federation/peers?token=<ton-admin-token>'
# Doit lister vps-iamine-prod avec capabilities LLM
```

---

## Phase 7 — Systemd service (persistance reboot)

Objectif : le pool redémarre automatiquement au reboot de la machine.

```bash
sudo tee /etc/systemd/system/iamine-pool.service > /dev/null <<EOF
[Unit]
Description=IAMINE Pool (local federation node)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=/home/$USER/iamine-pool
EnvironmentFile=/home/$USER/iamine-pool/.env
ExecStartPre=/usr/bin/docker start iamine-pg
ExecStart=/home/$USER/iamine-pool/venv/bin/python -m iamine pool --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable iamine-pool.service
sudo systemctl start iamine-pool.service
systemctl status iamine-pool.service
```

**Points importants** du unit :
- `Requires=docker.service` : si docker tombe, le pool tombe
- `ExecStartPre=docker start iamine-pg` : au reboot, redémarrer aussi le container postgres (inutile si `--restart=unless-stopped` fait son boulot, mais ceinture+bretelles)
- Pas de `Requires=postgresql.service` car postgres est conteneurisé, pas un service systemd hôte

**Vérif restart resilience** :

```bash
sudo systemctl restart iamine-pool.service
sleep 3
curl http://127.0.0.1:8080/v1/federation/info | jq -r .atom_id
# Doit retourner le MÊME atom_id qu'avant le restart (persistance Ed25519)
```

L'atom_id identique confirme que le keypair `~/.iamine/federation/self_ed25519.key` est bien persisté au bon endroit.

---

## Troubleshooting — pièges réels rencontrés 2026-04-11

### Erreur 1 : `Migrations dir not found`
**Cause** : wheel `iamine-ai` n'embarque pas `migrations/`.
**Fix** : Phase 3, copier `migrations/` dans `site-packages/`.
**Long terme** : wheel 0.2.50 embarquera le dossier (fix upstream `MANIFEST.in` + `package_data`).

### Erreur 2 : `extension "vector" is not available`
**Cause** : image `postgres:16` vanilla utilisée.
**Fix** : Phase 2b, utiliser `pgvector/pgvector:pg16`. Drop + recreate la DB si nécessaire (`docker volume rm iamine-pg-data`).

### Erreur 3 : `ModuleNotFoundError: No module named 'asyncpg'`
**Cause** : `asyncpg` manque dans les deps du wheel.
**Fix** : `pip install asyncpg` dans le venv.
**Long terme** : wheel 0.2.50 l'ajoutera aux deps.

### Erreur 4 : handshake HTTP 0 / connection refused
**Cause** : NAT pas en place ou pare-feu router/pool.
**Fix** : vérifier la règle `TCP public:8080 -> LAN:8080`, puis `sudo ufw allow 8080/tcp` si ufw actif sur la machine pool.

### Erreur 5 : `atom_id does not match sha256(pubkey) - possible MITM`
**Cause** : un proxy ou un reverse proxy modifie le body de `/v1/federation/info` en route.
**Fix** : vérifier que le reverse proxy (caddy/nginx/cloudflare) ne réécrit pas le content-type ni le body. Test direct sans proxy.

---

## Post-install : upgrade trust level

Par défaut après handshake, le peer est à **trust_level=1** (bare handshake). Pour participer à la fédération active (M3+ endpoints, futures phases M10/M11), un admin iamine.org doit promouvoir :

```bash
# Depuis iamine.org
IAMINE_ADMIN_TOKEN=<token> python3 -m iamine pool promote <atom_id> --level 2
# Puis éventuellement :
# python3 -m iamine pool promote <atom_id> --level 3   # bonded (M5 HARD-LOCK actuel)
```

**Note** : en phase M11-scaffold actuelle, trust=3 est hard-lock côté iamine.org. Contacter david pour débloquer temporairement ou attendre M11.2.

---

## Références architecture

- `docs/ARCHITECTURE.md` — vision molécule + atomes
- `docs/RUNBOOK_M10_GO_LIVE.md` — go-live settlement M10
- memory `project_m11_scaffold_done.md` — état actuel M11 replication
- memory `project_vision_molecule_raid.md` — pourquoi plusieurs pools = tolérance panne

---

**Version runbook** : 1.0 (2026-04-11)
**Testé sur** : Gladiator .30 (AnduinOS 1.4.2, Python 3.13, iamine-ai 0.2.49, pgvector/pgvector:pg16)
**Mainteneur** : david + claude-vps
