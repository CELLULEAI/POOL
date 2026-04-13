# Publication Docker Hub celluleai/pool:0.2.50

## Pré-requis
1. Compte Docker Hub avec l'org `celluleai` (ou user personnel)
2. Token d'accès Docker Hub (Settings -> Security -> New Access Token)

## Étapes (depuis une machine avec docker installé)

### Option A — Build direct et push

```bash
# 1. Récupérer les 2 fichiers docker
cd /tmp && mkdir iamine-docker && cd iamine-docker
scp user@cellule.ai:/home/harpersatrage/iamine/docker/Dockerfile .
scp user@cellule.ai:/home/harpersatrage/iamine/docker/docker-entrypoint.sh .
chmod +x docker-entrypoint.sh

# 2. Build
docker build -t celluleai/pool:0.2.50 -t celluleai/pool:latest .

# 3. Login Docker Hub
docker login -u celluleai
# (paste token)

# 4. Push
docker push celluleai/pool:0.2.50
docker push celluleai/pool:latest
```

### Option B — Depuis le tarball pré-buildé sur Gladiator

Une image a déjà été buildée et validée E2E sur Gladiator (.30) le 2026-04-11.

```bash
# 1. Récupérer le tarball depuis Gladiator (via .86 jump host)
scp -J harpersat@192.168.1.86 harpersat@192.168.1.30:/tmp/celluleai-pool-0.2.50.tar .

# 2. Charger dans docker local
docker load -i celluleai-pool-0.2.50.tar
# Loaded image: celluleai/pool:0.2.50

# 3. Login + push
docker login -u celluleai
docker push celluleai/pool:0.2.50

# 4. Tag + push latest
docker tag celluleai/pool:0.2.50 celluleai/pool:latest
docker push celluleai/pool:latest
```

## Après la publication

### Test depuis une machine tierce
```bash
docker pull celluleai/pool:0.2.50
curl -O https://cellule.ai/docs/docker-compose.yml
curl -O https://cellule.ai/docs/.env.example
cp .env.example .env
# edit .env
docker compose up -d
```

### Validation E2E sur Gladiator (déjà effectuée 2026-04-11 19:28)
- Build multi-stage : OK (581 MB disk, 134 MB content)
- Postgres pgvector spin up : OK
- 14 migrations appliquées auto : OK (000->014)
- federation_self généré : OK (atom_id=c05e4c1a0a7abe7f)
- /v1/federation/info : HTTP 200 OK
- Tous les background loops schedulés

## TODO post-publication
- [ ] Créer l'org iamineorg sur Docker Hub
- [ ] Uploader le logo
- [ ] Description README : "Cellule.ai Pool — decentralized LLM network (preprod testnet)"
- [ ] Link vers https://cellule.ai/docs/pool-docker.html
