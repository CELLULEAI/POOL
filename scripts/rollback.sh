#!/bin/bash
# ========================================================
# IAMINE — Rollback depuis un snapshot
# Usage: ./rollback.sh <snapshot_name>
# ========================================================
IAMINE=$HOME/iamine
SNAP_DIR=$IAMINE/snapshots
SCRIPTS=$IAMINE/scripts
NAME=$1

if [ -z "$NAME" ] || [ ! -d "$SNAP_DIR/$NAME" ]; then
    echo "Usage: ./rollback.sh <snapshot_name>"
    echo "Snapshots disponibles:"
    ls -1d $SNAP_DIR/iamine-* 2>/dev/null | while read d; do
        meta=$d/metadata.json
        if [ -f "$meta" ]; then
            v=$(python3 -c "import json;print(json.load(open('$meta')).get('version','?'))" 2>/dev/null)
            echo "  $(basename $d) — v$v"
        else
            echo "  $(basename $d)"
        fi
    done
    exit 1
fi

echo "=== ROLLBACK to $NAME ==="

echo "  [1/4] Restauration code..."
cd $IAMINE
tar xzf $SNAP_DIR/$NAME/code.tar.gz 2>/dev/null

echo "  [2/4] Restauration PostgreSQL..."
zcat $SNAP_DIR/$NAME/postgres.sql.gz 2>/dev/null | psql -U harpersatrage -d iamine -q 2>/dev/null

echo "  [3/4] Rebuild + restart..."
cd $IAMINE && source venv/bin/activate
python -m build -w -q 2>/dev/null
sudo /usr/bin/systemctl restart iamine-pool
sudo /usr/bin/systemctl restart iamine-worker
sleep 10

echo "  [4/4] Verification..."
$SCRIPTS/verify-system.sh
RESULT=$?

if [ $RESULT -eq 0 ]; then
    echo "=== ROLLBACK OK ==="
else
    echo "=== ROLLBACK ECHOUE — intervention manuelle requise ==="
fi
exit $RESULT
