#!/bin/bash
# vps-self-backup.sh — daily self-backup of cellule.ai VPS
# Runs as root via systemd vps-self-backup.service
# Target : /var/backups/vps-self/YYYY-MM-DD/
# Retention local VPS : 7 days (Z2 pulls and keeps 30)
#
# Content :
#   - iamine-db.sql.gz          PostgreSQL dump (26 MB → ~5 MB compressed)
#   - secrets.env.gpg           /etc/iamine/secrets.env chiffré AES256
#   - systemd-iamine.tar.gz     systemd services iamine-*
#   - iamine-wip.diff           git diff HEAD dans /home/harpersatrage/iamine
#   - pool-private.tar.gz       /home/harpersatrage/pool-private tree
#
# Rationale : le crash VPS 2026-04-17 avait mangé /etc /bin .py. Master.86
# qui servait alors de destination backup est aujourd'hui dead. Sans nouveau
# filet, une rechute fsck = perte totale non-récupérable de PostgreSQL
# (conversations, federation_peers, ledger), secrets.env (federation keys
# Ed25519), et pool-private (copie privée unique côté VPS).
# Cf. memory project_todo_backup_master86 (superseded by this).

set -euo pipefail

# --- config ---
DATE=$(date -u +%Y-%m-%d)
BACKUP_ROOT="/var/backups/vps-self"
DEST="$BACKUP_ROOT/$DATE"
GPG_PASSPHRASE="/root/z2-backup-gpg.key"
RETENTION_DAYS=7
LOG_FILE="/var/log/vps-self-backup.log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE" ; }

log "=== vps-self-backup start ($DATE) ==="

# --- preconditions ---
[ -r "$GPG_PASSPHRASE" ] || { log "FATAL: $GPG_PASSPHRASE not readable"; exit 1; }
mkdir -p "$DEST"
chmod 700 "$BACKUP_ROOT" "$DEST"

# --- 1. PostgreSQL dump ---
log "pg_dump iamine ..."
sudo -u postgres pg_dump iamine | gzip > "$DEST/iamine-db.sql.gz"
SIZE=$(stat -c%s "$DEST/iamine-db.sql.gz")
log "  -> iamine-db.sql.gz ($SIZE bytes)"

# --- 2. Secrets (chiffré GPG) ---
log "encrypt /etc/iamine/secrets.env ..."
if [ -f /etc/iamine/secrets.env ]; then
  gpg --batch --yes --pinentry-mode loopback \
      --passphrase-file "$GPG_PASSPHRASE" \
      --symmetric --cipher-algo AES256 \
      --output "$DEST/secrets.env.gpg" \
      /etc/iamine/secrets.env
  log "  -> secrets.env.gpg OK"
else
  log "  WARN: /etc/iamine/secrets.env absent, skipping"
fi

# --- 3. Systemd services iamine-* ---
log "tar systemd iamine-* ..."
if ls /etc/systemd/system/iamine-*.service 1>/dev/null 2>&1; then
  tar czf "$DEST/systemd-iamine.tar.gz" -C /etc/systemd/system/ \
      $(cd /etc/systemd/system/ && ls iamine-*.service 2>/dev/null)
  log "  -> systemd-iamine.tar.gz OK"
fi
# secrets.env lui-même est dans /etc/iamine — le chemin + permissions est utile aussi
if [ -d /etc/iamine ]; then
  tar czf "$DEST/etc-iamine-dir.tar.gz" --exclude=secrets.env -C /etc iamine 2>/dev/null || true
fi

# --- 4. iamine-work WIP diff (non-committé) ---
# Run git as harpersatrage (owner of the repo) to avoid
# "fatal: detected dubious ownership in repository" when root touches it.
log "git diff iamine WIP ..."
if [ -d /home/harpersatrage/iamine/.git ]; then
  sudo -u harpersatrage -- bash -c "cd /home/harpersatrage/iamine && git diff HEAD" > "$DEST/iamine-wip.diff" 2>/dev/null || true
  sudo -u harpersatrage -- bash -c "cd /home/harpersatrage/iamine && git log -1 --format='%H %s (%ci)'" > "$DEST/iamine-head.txt" 2>/dev/null || true
  log "  -> iamine-wip.diff + iamine-head.txt OK"
fi

# --- 5. pool-private tree (exclude .git packs, keep refs) ---
log "tar pool-private ..."
if [ -d /home/harpersatrage/pool-private ]; then
  tar czf "$DEST/pool-private.tar.gz" \
      --exclude='.git/objects/pack/*.pack' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='.pytest_cache' \
      -C /home/harpersatrage pool-private
  SIZE=$(stat -c%s "$DEST/pool-private.tar.gz")
  log "  -> pool-private.tar.gz ($SIZE bytes)"
fi

# --- 6. Manifest pour audit ---
log "write manifest ..."
PG_SIZE=$(sudo -u postgres psql -d iamine -tAc "SELECT pg_size_pretty(pg_database_size('iamine'));" 2>/dev/null || echo unknown)
IAMINE_HEAD=$(sudo -u harpersatrage -- bash -c 'cd /home/harpersatrage/iamine && git log -1 --format="%h %s"' 2>/dev/null || echo N/A)
POOLPRIV_HEAD=$(sudo -u harpersatrage -- bash -c 'cd /home/harpersatrage/pool-private && git log -1 --format="%h %s"' 2>/dev/null || echo N/A)
{
  echo "VPS backup manifest - $DATE"
  echo "hostname: $(hostname)"
  echo "kernel: $(uname -r)"
  echo "---"
  echo "files:"
  ls -la "$DEST" | awk '{print "  ", $9, $5"b"}'
  echo "---"
  echo "postgres size : $PG_SIZE"
  echo "iamine HEAD : $IAMINE_HEAD"
  echo "pool-private HEAD : $POOLPRIV_HEAD"
} > "$DEST/manifest.txt"

# --- 7. Rotation locale (garde 7 jours) ---
log "rotate old backups (>$RETENTION_DAYS days) ..."
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +$RETENTION_DAYS -exec rm -rf {} + 2>/dev/null || true

# --- 8. Permissions : SFTP chroot user will pull as vps-pull-backup
chown -R root:vps-pull-backup "$BACKUP_ROOT" 2>/dev/null || true
chmod -R g+rX "$BACKUP_ROOT"

log "=== vps-self-backup done ($DATE) size=$(du -sh $DEST | awk '{print $1}') ==="
