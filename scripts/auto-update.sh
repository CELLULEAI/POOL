#!/bin/bash
# ========================================================
# IAMINE — Mise a jour autonome intelligente
# Detecte, applique, verifie, rollback si echec
# Cron: 0 4 * * 1 (lundi 4h)
# ========================================================
set -o pipefail

IAMINE=$HOME/iamine
SCRIPTS=$IAMINE/scripts
SNAP_DIR=$IAMINE/snapshots
LOG=/home/harpersatrage/log/iamine/updates.log
MIGRATIONS_LOG=/home/harpersatrage/log/iamine/applied-migrations.log
EMAIL="david.mourgues@gmail.com"
START=$(date +%s)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a $LOG 2>/dev/null; }

log "========================================"
log "AUTO-UPDATE START"
log "========================================"

# ── 1. DETECTION — quoi mettre a jour ? ──
UPDATES=""
UPDATE_COUNT=0

# OS packages
APT_UPDATES=$(apt list --upgradable 2>/dev/null | grep -v Listing | head -20)
if [ -n "$APT_UPDATES" ]; then
    APT_COUNT=$(echo "$APT_UPDATES" | wc -l)
    UPDATES="${UPDATES}APT: $APT_COUNT paquets\n"
    UPDATE_COUNT=$((UPDATE_COUNT + APT_COUNT))
    log "Detecte: $APT_COUNT paquets OS a mettre a jour"
fi

# pip iamine-ai
cd $IAMINE && source venv/bin/activate 2>/dev/null
PIP_UPDATES=$(pip list --outdated 2>/dev/null | grep -i iamine)
if [ -n "$PIP_UPDATES" ]; then
    UPDATES="${UPDATES}PIP: $PIP_UPDATES\n"
    UPDATE_COUNT=$((UPDATE_COUNT + 1))
    log "Detecte: iamine-ai a mettre a jour"
fi

# git
cd $IAMINE
git fetch origin 2>/dev/null
GIT_UPDATES=$(git log HEAD..origin/master --oneline 2>/dev/null)
if [ -n "$GIT_UPDATES" ]; then
    GIT_COUNT=$(echo "$GIT_UPDATES" | wc -l)
    UPDATES="${UPDATES}GIT: $GIT_COUNT commits\n"
    UPDATE_COUNT=$((UPDATE_COUNT + GIT_COUNT))
    log "Detecte: $GIT_COUNT commits git"
fi

# Migrations SQL non appliquees
touch $MIGRATIONS_LOG 2>/dev/null
SQL_UPDATES=""
for sql in $IAMINE/migrations/*.sql; do
    [ -f "$sql" ] || continue
    basename_sql=$(basename "$sql")
    if ! grep -q "$basename_sql" $MIGRATIONS_LOG 2>/dev/null; then
        SQL_UPDATES="${SQL_UPDATES}$basename_sql "
        UPDATE_COUNT=$((UPDATE_COUNT + 1))
    fi
done
if [ -n "$SQL_UPDATES" ]; then
    UPDATES="${UPDATES}SQL: $SQL_UPDATES\n"
    log "Detecte: migrations SQL $SQL_UPDATES"
fi

# Rien a faire ?
if [ $UPDATE_COUNT -eq 0 ]; then
    log "Aucune mise a jour disponible — fin"
    log "========================================"
    exit 0
fi

log "Total: $UPDATE_COUNT mises a jour detectees"

# ── 2. SNAPSHOT avant modification ──
SNAP_NAME=iamine-$(date +%Y-%m-%d-%H%M)
log "Snapshot: $SNAP_NAME"
$SCRIPTS/snapshot.sh $SNAP_NAME >> $LOG 2>&1
if [ $? -ne 0 ]; then
    log "ERREUR: snapshot echoue — abandon"
    $SCRIPTS/notify.sh "ABORTED" "Snapshot failed, update cancelled" $EMAIL
    exit 1
fi

# ── 3. APPLICATION des mises a jour ──
log "Application des mises a jour..."

# APT
if [ -n "$APT_UPDATES" ]; then
    log "  APT upgrade..."
    sudo apt update -qq >> $LOG 2>&1
    sudo apt upgrade -y -qq >> $LOG 2>&1
fi

# PIP
if [ -n "$PIP_UPDATES" ]; then
    log "  PIP upgrade iamine-ai..."
    cd $IAMINE && source venv/bin/activate
    pip install --upgrade iamine-ai -i https://iamine.org/pypi --extra-index-url https://pypi.org/simple -q >> $LOG 2>&1
fi

# GIT
if [ -n "$GIT_UPDATES" ]; then
    log "  GIT pull..."
    cd $IAMINE && git pull origin master >> $LOG 2>&1
fi

# Rebuild wheel
log "  Rebuild wheel..."
cd $IAMINE && source venv/bin/activate
python -m build -w -q >> $LOG 2>&1

# Migrations SQL
for sql in $IAMINE/migrations/*.sql; do
    [ -f "$sql" ] || continue
    basename_sql=$(basename "$sql")
    if ! grep -q "$basename_sql" $MIGRATIONS_LOG 2>/dev/null; then
        log "  Migration: $basename_sql"
        psql -U harpersatrage iamine < "$sql" >> $LOG 2>&1
        echo "$basename_sql" >> $MIGRATIONS_LOG
    fi
done

# ── 4. RESTART ──
log "Restart services..."
sudo /usr/bin/systemctl restart iamine-pool
sudo /usr/bin/systemctl restart iamine-worker
log "Attente 10s reconnexion workers..."
sleep 10

# ── 5. VERIFICATION ──
log "Verification post-update..."
VERIFY_OUTPUT=$($SCRIPTS/verify-system.sh 2>&1)
VERIFY_OK=$?
echo "$VERIFY_OUTPUT" >> $LOG

DUR=$(( $(date +%s) - START ))

# ── 6. DECISION ──
if [ $VERIFY_OK -eq 0 ]; then
    STATUS="OK"
    log "UPDATE OK en ${DUR}s"
else
    STATUS="FAILED"
    log "UPDATE FAILED — ROLLBACK depuis $SNAP_NAME"
    $SCRIPTS/rollback.sh $SNAP_NAME >> $LOG 2>&1
fi

# ── 7. NOTIFICATION ──
$SCRIPTS/notify.sh "$STATUS" "$(echo -e "$UPDATES")" $EMAIL $DUR "$SNAP_NAME" "$VERIFY_OUTPUT"

log "========================================"
log "AUTO-UPDATE END — $STATUS (${DUR}s)"
log "========================================"

[ "$STATUS" = "OK" ] && exit 0 || exit 1
