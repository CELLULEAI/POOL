# CLAUDE_VPS_RESTORE.md — Runbook restauration cellule.ai sur VPS fresh

**Objectif** : permettre à une session Claude (ou tout opérateur) de
restaurer cellule.ai sur un VPS Ubuntu 24.04 LTS fresh en ~30-60 min,
à partir :
- IP du nouveau VPS (`NEW_VPS_IP`)
- SSH root access (clé ou mot de passe temporaire)
- Accès SSH à Z2 (`D:/IAMINE.org/clef/id_iamine` → `harpersat@192.168.1.199`)
- Passphrase GPG : `D:/IAMINE.org/.secret/z2-backup-gpg.key`

Ce runbook est **auto-suffisant** — il liste tous les paths, commandes,
et dépendances. Lit-le du haut en bas.

---

## 0. Pré-requis sur le nouveau VPS

- Ubuntu 24.04 LTS amd64 fresh
- SSH root enabled (clé publique ou mot de passe bootstrap)
- DNS `cellule.ai` A record à mettre à jour sur Cloudflare pour pointer vers `NEW_VPS_IP` (à faire en toute fin, après validation)
- Port ouverts : 22 (SSH), 80 (HTTP), 443 (HTTPS), 8080 (uvicorn derrière nginx)

## 1. Récupérer le dernier backup depuis Z2

```bash
# Depuis ta machine locale (Windows bash ou Linux/macOS)
Z2_KEY="D:/IAMINE.org/clef/id_iamine"      # ou $HOME/.ssh/id_iamine
LATEST=$(ssh -i $Z2_KEY harpersat@192.168.1.199 \
    "ls -1 /home/harpersat/cellule-vps-backup/ | sort | tail -1")
echo "Latest backup on Z2 : $LATEST"

# Download le dossier daté vers local
mkdir -p /tmp/cellule-restore
scp -i $Z2_KEY -r \
    harpersat@192.168.1.199:/home/harpersat/cellule-vps-backup/$LATEST \
    /tmp/cellule-restore/

ls /tmp/cellule-restore/$LATEST/
# Attendu : iamine-db.sql.gz, secrets.env.gpg, systemd-iamine.tar.gz,
#           etc-iamine-dir.tar.gz, pool-private.tar.gz, iamine-wip.diff,
#           iamine-head.txt, manifest.txt
```

## 2. Provisionner le nouveau VPS

```bash
NEW_VPS_IP="__REMPLACER__"
SSH_KEY_LOCAL="D:/IAMINE.org/clef/id_iamine"  # clé publique à déployer

# Initial : copier ta clé publique pour login root sans password
ssh-copy-id -i "$SSH_KEY_LOCAL.pub" root@$NEW_VPS_IP

# Upload le backup vers VPS
scp -i $SSH_KEY_LOCAL -r /tmp/cellule-restore/$LATEST \
    root@$NEW_VPS_IP:/tmp/cellule-restore/
```

## 3. Installer les dépendances système (sur VPS nouveau)

```bash
ssh -i $SSH_KEY_LOCAL root@$NEW_VPS_IP

# Update
apt update && apt upgrade -y

# Python 3.12 (devrait être par défaut sur 24.04)
apt install -y python3 python3-pip python3-venv python3-dev build-essential

# PostgreSQL 16 + pgvector extension
apt install -y postgresql-16 postgresql-contrib
apt install -y postgresql-16-pgvector || apt install -y postgresql-pgvector

# nginx + certbot (Let's Encrypt)
apt install -y nginx certbot python3-certbot-nginx

# Docker (pour Watchtower future si master-pool)
apt install -y docker.io docker-compose-plugin
systemctl enable --now docker

# outils backup (lftp pour pull futurs backups)
apt install -y lftp gpg

# user harpersatrage (owner du projet)
useradd -m -s /bin/bash -G sudo harpersatrage
# configurer sudo NOPASSWD si besoin (cf. memory feedback_sudo_nopasswd_harden)
echo "harpersatrage ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/harpersatrage
chmod 440 /etc/sudoers.d/harpersatrage

# Copier ta clé SSH pour harpersatrage
mkdir -p /home/harpersatrage/.ssh
cp /root/.ssh/authorized_keys /home/harpersatrage/.ssh/
chown -R harpersatrage:harpersatrage /home/harpersatrage/.ssh
chmod 700 /home/harpersatrage/.ssh
chmod 600 /home/harpersatrage/.ssh/authorized_keys
```

## 4. Restaurer PostgreSQL

```bash
# Créer role + DB
sudo -u postgres psql <<'SQL'
CREATE ROLE harpersatrage LOGIN PASSWORD '__DB_PASSWORD__';
CREATE DATABASE iamine OWNER harpersatrage;
\c iamine
CREATE EXTENSION IF NOT EXISTS vector;
SQL

# Restaurer le dump
gunzip < /tmp/cellule-restore/$LATEST/iamine-db.sql.gz \
    | sudo -u postgres psql -d iamine

# Vérifier
sudo -u postgres psql -d iamine -c "\dt" | head -20
sudo -u postgres psql -d iamine -c "SELECT count(*) FROM federation_peers;"
sudo -u postgres psql -d iamine -c "SELECT count(*) FROM accounts;"
```

Le DB dump restaure :
- `accounts`, `api_tokens`, `workers`, `federation_peers`
- `conversations`, `memories` (avec pgvector embeddings)
- `federation_ledger`, `federation_merkle` (M11.x replication)
- `pool_config` (system_prompt, checker config, etc.)

## 5. Restaurer les secrets

```bash
# Déchiffrer secrets.env
cd /tmp/cellule-restore/$LATEST
# Upload la passphrase GPG vers VPS si absente
scp -i $SSH_KEY_LOCAL \
    D:/IAMINE.org/.secret/z2-backup-gpg.key \
    root@$NEW_VPS_IP:/root/z2-backup-gpg.key
chmod 400 /root/z2-backup-gpg.key

# Decrypt
mkdir -p /etc/iamine
gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file /root/z2-backup-gpg.key \
    --decrypt secrets.env.gpg > /etc/iamine/secrets.env
chmod 640 /etc/iamine/secrets.env
chown root:root /etc/iamine/secrets.env

# Restaurer structure /etc/iamine (permissions + autres fichiers)
tar xzf etc-iamine-dir.tar.gz -C /etc/
```

Le `secrets.env` contient typiquement :
```
IAMINE_FED=active
IAMINE_FED_NAME=vps-iamine-prod
FORWARDING_ENABLED=true
FORWARDING_MODE=active
DB_HOST=localhost, DB_USER, DB_PASS, DB_NAME
Ed25519 federation keys paths
SERVER_SECRET (utilisé pour dériver les worker tokens)
```

## 6. Restaurer le code (iamine-work + pool-private)

```bash
# ---- iamine-work (public, depuis GitHub) ----
sudo -u harpersatrage -i
cd ~
git clone https://github.com/CELLULEAI/POOL.git iamine
cd iamine

# Installer les deps Python
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -e ".[pool]"
pip install llama-cpp-python  # CPU variant OK pour orchestrator

# Appliquer WIP diff s'il y en a (modifs non committées au moment du backup)
WIPDIFF=/tmp/cellule-restore/$LATEST/iamine-wip.diff
if [ -s "$WIPDIFF" ]; then
    git apply "$WIPDIFF" && echo "WIP appliqué" || echo "WIP conflict — check"
fi

# Vérifier HEAD vs backup
echo "WIP backup HEAD : $(cat /tmp/cellule-restore/$LATEST/iamine-head.txt)"
git log -1 --oneline

# ---- pool-private (privé, depuis archive backup) ----
cd ~
tar xzf /tmp/cellule-restore/$LATEST/pool-private.tar.gz
# structure : /home/harpersatrage/pool-private/...
ls pool-private/
cd pool-private
pip install -e .  # si pyproject.toml présent

exit  # retour en root
```

## 7. Restaurer systemd services

```bash
tar xzf /tmp/cellule-restore/$LATEST/systemd-iamine.tar.gz \
    -C /etc/systemd/system/
systemctl daemon-reload
systemctl enable iamine-pool.service iamine-watchdog.service 2>/dev/null || true
# NE PAS start maintenant — d'abord configurer nginx
```

## 8. Configurer nginx + Cloudflare

```bash
# Config nginx minimale (proxy vers uvicorn 8080)
cat > /etc/nginx/sites-available/cellule.ai <<'NGINX'
server {
    listen 80;
    server_name cellule.ai www.cellule.ai dl.cellule.ai gladiator.cellule.ai;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/cellule.ai /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Cloudflare : update DNS A record de cellule.ai vers $NEW_VPS_IP
# (manuel via dashboard Cloudflare OU API si token disponible)
# En mode Flexible SSL : Cloudflare termine HTTPS, nginx reçoit HTTP 80
```

## 9. Lancer le pool + vérifications

```bash
systemctl start iamine-pool.service
sleep 10
systemctl status iamine-pool.service --no-pager | head -5
journalctl -u iamine-pool -n 20 --no-pager

# Vérifications
curl -s http://127.0.0.1:8080/v1/status | python3 -m json.tool | head -20
curl -s http://127.0.0.1:8080/install-worker.sh | head -3
curl -s http://127.0.0.1:8080/docs/RUNBOOK_POOL_BOOTSTRAP.md | head -3

# Une fois Cloudflare DNS propagé (~5min max) :
curl -s https://cellule.ai/v1/status | python3 -m json.tool | head -10
```

**Résultat attendu** : `/v1/status` renvoie du JSON avec `pool: IAMINE`,
workers_online probablement 0 au début (workers Z2/Gladiator doivent se
reconnecter — ils utilisent le DNS donc redirigés automatiquement).

## 10. Resetup backup VPS → Z2 sur le nouveau VPS

```bash
# Uploader les scripts depuis le repo iamine-work
cd /home/harpersatrage/iamine
sudo cp /dev/null /usr/local/bin/vps-self-backup.sh
# (Le script est dans docs/ mais il faut aussi le re-push via git — cf §suite)

# Ou depuis Z2 : copier les scripts déjà déployés
scp -i ~/.ssh/id_rsa harpersat@192.168.1.199:/usr/local/bin/pull-vps-backup.sh /tmp/
# ... mais celui-là est le PULL Z2. Pour VPS il faut vps-self-backup.sh
# qui existe sur l'ancien VPS (via la même Z2 backup : c'est dans les
# fichiers iamine-work du repo sous scripts/vps-self-backup.sh)

cp /home/harpersatrage/iamine/scripts/vps-self-backup.sh /usr/local/bin/
chmod 700 /usr/local/bin/vps-self-backup.sh
cp /home/harpersatrage/iamine/scripts/vps-self-backup.service /etc/systemd/system/
cp /home/harpersatrage/iamine/scripts/vps-self-backup.timer /etc/systemd/system/

# Créer user pour SFTP chroot pull
useradd --system --no-create-home --home /var/backups/vps-self \
    --shell /usr/sbin/nologin vps-pull-backup

# sshd config pour ChrootDirectory + AuthorizedKeysFile
cat >> /etc/ssh/sshd_config <<'SSH'

Match User vps-pull-backup
  ChrootDirectory /var/backups/vps-self
  AuthorizedKeysFile /etc/ssh/authorized_keys.d/vps-pull-backup
  ForceCommand internal-sftp
  AllowTcpForwarding no
  X11Forwarding no
  PasswordAuthentication no
SSH
mkdir -p /etc/ssh/authorized_keys.d
# Récupérer la pubkey Z2 (connue : cat /root/.ssh/vps-pull-key.pub sur Z2)
ssh -i $SSH_KEY_LOCAL harpersat@192.168.1.199 \
    "sudo cat /root/.ssh/vps-pull-key.pub" \
    > /etc/ssh/authorized_keys.d/vps-pull-backup
chmod 644 /etc/ssh/authorized_keys.d/vps-pull-backup
systemctl reload ssh

# Activer timer
systemctl daemon-reload
systemctl enable --now vps-self-backup.timer

# Premier run
systemctl start vps-self-backup.service
# → devrait créer /var/backups/vps-self/YYYY-MM-DD/

# Côté Z2 : test pull manuel
ssh -i $SSH_KEY_LOCAL harpersat@192.168.1.199 \
    "sudo systemctl start pull-vps-backup.service"
```

## 11. Re-federation Z2 workers vers le nouveau VPS

Si l'IP `NEW_VPS_IP` ≠ ancien VPS, Cloudflare DNS doit être mis à jour.
Les workers utilisent `wss://cellule.ai/ws` (nom DNS), donc après
propagation DNS (~5 min), ils se reconnectent automatiquement.

Si latence ou bugs : forcer reconnect workers Z2 :
```bash
ssh -i $SSH_KEY_LOCAL harpersat@192.168.1.199 \
    "sudo systemctl restart iamine-worker-*.service"
```

## 12. Backup du backup : ramener le runbook sur Z2

Le backup VPS→Z2 va remarcher automatiquement demain matin. Mais ce
runbook lui-même est déjà dans `iamine-work/docs/CLAUDE_VPS_RESTORE.md`
→ push sur GitHub garantit que le prochain restore a un runbook à jour.

## Troubleshooting courant

| Symptôme | Cause probable | Fix |
|---|---|---|
| `iamine-pool.service` fail "No module iamine" | venv pas activé | `ExecStart=/home/harpersatrage/iamine/venv/bin/python -m iamine pool ...` |
| pgvector extension inexistante | Package `postgresql-16-pgvector` pas installé | `apt install postgresql-pgvector` puis `CREATE EXTENSION vector;` |
| Workers ne se reconnectent pas | DNS pas encore propagé | `nslookup cellule.ai` doit renvoyer `$NEW_VPS_IP`. Cloudflare TTL typ. 5min. |
| `curl cellule.ai` timeout | nginx pas started ou Cloudflare en mode "Cloudflare proxy" sans SSL | Check `systemctl status nginx`. Vérifier Cloudflare SSL = Flexible ou Full. |
| Federation peers inactifs | Keys Ed25519 non restaurées | Check `/etc/iamine/secrets.env` contient `IAMINE_FED_PRIVKEY_PATH=...` et le fichier existe. |

## Références

- Backup self VPS : `scripts/vps-self-backup.sh` + `.service` + `.timer`
- Backup pull Z2 : `scripts/pull-vps-backup.sh` + `.service` + `.timer`
- Memory : `reference_z2_backup_live.md`, `reference_claude_memory_backup_live.md`, `reference_pra_z2.md`
- Crash précédent : `project_vps_crash_20260417.md` (cause fsck, récupération grâce à master.86 backup)

---

**Dernière mise à jour** : 2026-04-21
**Par** : session Claude (setup initial backup VPS→Z2)
**Valide pour** : Ubuntu 24.04 LTS + PostgreSQL 16 + pgvector + Python 3.12 +
nginx + Cloudflare Flexible SSL
