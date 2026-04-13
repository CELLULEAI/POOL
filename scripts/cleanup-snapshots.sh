#!/bin/bash
# ========================================================
# IAMINE — Nettoyage snapshots > 7 jours
# Garde toujours le dernier valide
# ========================================================
SNAP_DIR=$HOME/iamine/snapshots
KEEP_DAYS=7

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleanup snapshots > $KEEP_DAYS jours"

# Lister les snapshots a supprimer
TO_DELETE=$(find $SNAP_DIR -maxdepth 1 -type d -name 'iamine-*' -mtime +$KEEP_DAYS | sort)
TOTAL=$(ls -1d $SNAP_DIR/iamine-* 2>/dev/null | wc -l)

if [ -z "$TO_DELETE" ]; then
    echo "  Rien a supprimer ($TOTAL snapshots, tous < $KEEP_DAYS jours)"
    exit 0
fi

# Toujours garder au moins le dernier
LAST=$(ls -1d $SNAP_DIR/iamine-* 2>/dev/null | sort | tail -1)
DELETED=0

echo "$TO_DELETE" | while read d; do
    [ -z "$d" ] && continue
    if [ "$d" = "$LAST" ]; then
        echo "  KEEP (dernier): $(basename $d)"
    else
        echo "  DELETE: $(basename $d)"
        rm -rf "$d"
        DELETED=$((DELETED+1))
    fi
done

REMAINING=$(ls -1d $SNAP_DIR/iamine-* 2>/dev/null | wc -l)
echo "  Snapshots restants: $REMAINING"
