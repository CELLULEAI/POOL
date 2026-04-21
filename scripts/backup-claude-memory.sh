#!/bin/bash
# backup-claude-project.sh — daily backup of the FULL Claude project dir to VPS + Z2
# Source : C:/Users/install/.claude/projects/D--IAMINE-org/ (~169 MB)
#   includes :
#     - memory/*.md                          (règles, feedback, project notes, ~1.4 MB)
#     - {uuid}.jsonl                         (transcripts user/assistant par session)
#     - {uuid}/tool-results/                 (outputs outils par session)
#   étendu 2026-04-21 : auparavant on ne tarrait que memory/ (1.4 MB)
#   → les conversations (~167 MB) étaient PERDUES en cas de crash Windows.
# Crypto : AES256 symmetric GPG (shared passphrase with Z2 backup)
# Retention : 30 days (handled server-side by cron.weekly)

set -euo pipefail

PROJECT_DIR="/c/Users/install/.claude/projects/D--IAMINE-org"
PASSPHRASE_FILE="/d/IAMINE.org/.secret/z2-backup-gpg.key"
SSH_KEY="/d/IAMINE.org/clef/id_iamine"
LOG_FILE="/d/IAMINE.org/scripts/backup-claude-memory.log"

VPS_DEST="harpersatrage@109.123.240.151:/home/harpersatrage/claude-backup/uploads/"
Z2_DEST="harpersat@192.168.1.199:/home/harpersat/claude-backup/uploads/"

DATE=$(date -u +%Y-%m-%d)
TMPDIR=$(mktemp -d)
# Nom d'archive changé : "claude-project" (plus large que "claude-memory")
ARCHIVE="$TMPDIR/claude-project-$DATE.tar.gz"
ENCRYPTED="$ARCHIVE.gpg"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

log "=== backup-claude-project start ($DATE) ==="

# 1. Archive tout le dossier projet
log "tar project dir ..."
tar -czf "$ARCHIVE" -C "$(dirname "$PROJECT_DIR")" "$(basename "$PROJECT_DIR")"
SIZE_TAR=$(stat -c%s "$ARCHIVE")
log "  -> $ARCHIVE ($SIZE_TAR bytes, $(numfmt --to=iec-i --suffix=B $SIZE_TAR 2>/dev/null || echo "$SIZE_TAR bytes"))"

# 2. Encrypt
log "gpg symmetric AES256 ..."
gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$PASSPHRASE_FILE" \
    --symmetric --cipher-algo AES256 \
    --output "$ENCRYPTED" "$ARCHIVE"
SIZE_GPG=$(stat -c%s "$ENCRYPTED")
log "  -> $ENCRYPTED ($SIZE_GPG bytes)"

# 3. Upload to VPS
log "scp -> VPS ..."
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -q "$ENCRYPTED" "$VPS_DEST"
log "  -> VPS OK"

# 4. Upload to Z2
log "scp -> Z2 ..."
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -q "$ENCRYPTED" "$Z2_DEST"
log "  -> Z2 OK"

log "=== backup-claude-project done ($DATE) ==="
