#!/bin/bash
# ========================================================
# IAMINE — Snapshot complet (code + PostgreSQL + configs)
# Usage: ./snapshot.sh [nom]
# ========================================================
IAMINE=$HOME/iamine
SNAP_DIR=$IAMINE/snapshots
NAME=${1:-iamine-$(date +%Y-%m-%d-%H%M)}

mkdir -p $SNAP_DIR/$NAME
echo "Creating snapshot: $NAME"

# Code (sans modeles, venv, dist, git, pycache)
echo "  [1/4] Code..."
tar czf $SNAP_DIR/$NAME/code.tar.gz     -C $IAMINE     --exclude='models' --exclude='venv' --exclude='*.gguf'     --exclude='dist' --exclude='__pycache__' --exclude='.git'     --exclude='snapshots'     . 2>/dev/null

# PostgreSQL dump
echo "  [2/4] PostgreSQL..."
pg_dump -U harpersatrage iamine 2>/dev/null | gzip > $SNAP_DIR/$NAME/postgres.sql.gz

# Configs
echo "  [3/4] Configs..."
mkdir -p $SNAP_DIR/$NAME/configs
cp /etc/systemd/system/iamine-*.service $SNAP_DIR/$NAME/configs/ 2>/dev/null
cp /etc/nginx/sites-enabled/iamine* $SNAP_DIR/$NAME/configs/ 2>/dev/null
cp $IAMINE/.env $SNAP_DIR/$NAME/configs/ 2>/dev/null

# Metadata
echo "  [4/4] Metadata..."
VERSION=$(cd $IAMINE && source venv/bin/activate && python -c 'import iamine;print(iamine.__version__)' 2>/dev/null || echo '?')
WORKERS=$(curl -s http://localhost:8080/v1/status 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("workers_online",0))' 2>/dev/null || echo 0)
cat > $SNAP_DIR/$NAME/metadata.json << METADATA
{
  "name": "$NAME",
  "date": "$(date -Iseconds)",
  "version": "$VERSION",
  "workers": $WORKERS,
  "hostname": "$(hostname)"
}
METADATA

SIZE=$(du -sh $SNAP_DIR/$NAME | cut -f1)
echo "Snapshot OK: $SNAP_DIR/$NAME ($SIZE)"
