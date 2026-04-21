#!/bin/bash
# pull-vps-backup.sh — daily pull of VPS self-backup to Z2
# Runs as root on Z2 via systemd pull-vps-backup.service
# Source (VPS chroot SFTP) : vps-pull-backup@109.123.240.151:/
# Dest Z2 : /home/harpersat/cellule-vps-backup/
# Retention Z2 : 30 days (VPS keeps 7 locally, we keep 30 here)

set -euo pipefail

VPS_USER="vps-pull-backup"
VPS_HOST="109.123.240.151"
SSH_KEY="/root/.ssh/vps-pull-key"
DEST_ROOT="/home/harpersat/cellule-vps-backup"
RETENTION_DAYS=30
LOG_FILE="/var/log/pull-vps-backup.log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE" ; }

log "=== pull-vps-backup start ==="

mkdir -p "$DEST_ROOT"
chown harpersat:harpersat "$DEST_ROOT" 2>/dev/null || true

# Mirror via lftp over SFTP (rsync ne marche pas sur internal-sftp chroot :
# pas de shell pour lancer rsync --server, protocole mismatch. lftp parle
# nativement SFTP sans nécessiter shell côté serveur.)
# Script inline dans un fichier temp pour éviter les cauchemars de quoting.
LFTP_SCRIPT=$(mktemp)
cat > "$LFTP_SCRIPT" <<EOF
set sftp:connect-program "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -a -x"
open -u ${VPS_USER}, sftp://${VPS_HOST}
mirror --verbose --only-newer --no-perms / ${DEST_ROOT}/
bye
EOF
lftp -f "$LFTP_SCRIPT" 2>&1 | tee -a "$LOG_FILE"
rm -f "$LFTP_SCRIPT"

# Correct ownership : files created by root rsync → set to harpersat
chown -R harpersat:harpersat "$DEST_ROOT" 2>/dev/null || true

# Rotation locale Z2
log "rotate Z2 backups >$RETENTION_DAYS days ..."
find "$DEST_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +$RETENTION_DAYS -exec rm -rf {} + 2>/dev/null || true

SIZE=$(du -sh "$DEST_ROOT" | awk '{print $1}')
COUNT=$(find "$DEST_ROOT" -mindepth 1 -maxdepth 1 -type d | wc -l)
log "=== pull-vps-backup done (total $SIZE in $COUNT dir) ==="
