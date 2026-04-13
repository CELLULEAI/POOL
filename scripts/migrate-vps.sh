#!/bin/bash
# ========================================================
# IAMINE — Migration VPS (export/import)
# Usage: ./migrate-vps.sh export          → cree iamine-migration-DATE.tar.gz
#        ./migrate-vps.sh import FILE.gz  → restaure tout sur nouveau VPS
# ========================================================
IAMINE=$HOME/iamine
ACTION=$1

case $ACTION in
export)
    NAME=iamine-migration-$(date +%Y-%m-%d)
    OUTDIR=/tmp/$NAME
    mkdir -p $OUTDIR

    echo "=== EXPORT IAMINE ($NAME) ==="

    echo "  [1/6] Code..."
    tar czf $OUTDIR/code.tar.gz -C $IAMINE         --exclude='models' --exclude='venv' --exclude='*.gguf'         --exclude='dist' --exclude='__pycache__' --exclude='.git'         --exclude='snapshots' . 2>/dev/null

    echo "  [2/6] PostgreSQL..."
    pg_dump -U harpersatrage iamine 2>/dev/null | gzip > $OUTDIR/postgres.sql.gz

    echo "  [3/6] Configs systemd + nginx..."
    mkdir -p $OUTDIR/configs
    cp /etc/systemd/system/iamine-*.service $OUTDIR/configs/ 2>/dev/null
    cp /etc/nginx/sites-enabled/iamine* $OUTDIR/configs/ 2>/dev/null
    cp /etc/nginx/sites-available/iamine* $OUTDIR/configs/ 2>/dev/null
    cp $IAMINE/.env $OUTDIR/configs/ 2>/dev/null
    crontab -l > $OUTDIR/configs/crontab.bak 2>/dev/null

    echo "  [4/6] Cles SSH testeurs..."
    mkdir -p $OUTDIR/ssh-keys
    for user in david-tester regis-tester wasa-tester; do
        if [ -d /home/$user/.ssh ]; then
            cp /home/$user/.ssh/authorized_keys $OUTDIR/ssh-keys/$user.pub 2>/dev/null
        fi
    done

    echo "  [5/6] Metadata..."
    VERSION=$(cd $IAMINE && source venv/bin/activate && python -c 'import iamine;print(iamine.__version__)' 2>/dev/null || echo '?')
    cat > $OUTDIR/metadata.json << META
{
  "date": "$(date -Iseconds)",
  "version": "$VERSION",
  "hostname": "$(hostname)",
  "ip": "$(hostname -I | awk '{print $1}')",
  "domain": "iamine.org",
  "postgres_db": "iamine",
  "postgres_user": "harpersatrage"
}
META

    echo "  [6/6] Archive finale..."
    tar czf /tmp/$NAME.tar.gz -C /tmp $NAME
    rm -rf $OUTDIR
    SIZE=$(du -sh /tmp/$NAME.tar.gz | cut -f1)
    echo ""
    echo "=== EXPORT OK: /tmp/$NAME.tar.gz ($SIZE) ==="
    echo ""
    echo "Transferer vers le nouveau VPS:"
    echo "  scp /tmp/$NAME.tar.gz user@new-vps:/tmp/"
    echo "  ssh user@new-vps"
    echo "  ./migrate-vps.sh import /tmp/$NAME.tar.gz"
    ;;

import)
    FILE=$2
    if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
        echo "Usage: ./migrate-vps.sh import <migration-file.tar.gz>"
        exit 1
    fi

    echo "=== IMPORT IAMINE ==="
    TMPDIR=/tmp/iamine-import-$$
    mkdir -p $TMPDIR
    tar xzf $FILE -C $TMPDIR

    # Trouver le dossier extrait
    SRCDIR=$(find $TMPDIR -maxdepth 1 -type d -name 'iamine-migration-*' | head -1)
    if [ -z "$SRCDIR" ]; then
        echo "ERREUR: archive invalide"
        exit 1
    fi

    echo "  [1/8] Installation dependances..."
    sudo apt update -qq
    sudo apt install -y python3 python3-venv python3-pip postgresql nginx certbot python3-certbot-nginx -qq

    echo "  [2/8] Creation utilisateur et structure..."
    mkdir -p $IAMINE
    cd $IAMINE

    echo "  [3/8] Restauration code..."
    tar xzf $SRCDIR/code.tar.gz -C $IAMINE

    echo "  [4/8] Environnement Python..."
    python3 -m venv $IAMINE/venv
    source $IAMINE/venv/bin/activate
    pip install --upgrade pip -q
    pip install -r $IAMINE/requirements.txt -q 2>/dev/null
    python -m build -w -q 2>/dev/null

    echo "  [5/8] PostgreSQL..."
    sudo -u postgres createdb iamine 2>/dev/null
    sudo -u postgres psql -c "ALTER USER harpersatrage WITH SUPERUSER;" 2>/dev/null
    zcat $SRCDIR/postgres.sql.gz | psql -U harpersatrage -d iamine -q 2>/dev/null

    echo "  [6/8] Configs systemd + nginx..."
    sudo cp $SRCDIR/configs/iamine-*.service /etc/systemd/system/ 2>/dev/null
    sudo cp $SRCDIR/configs/iamine* /etc/nginx/sites-available/ 2>/dev/null
    sudo ln -sf /etc/nginx/sites-available/iamine* /etc/nginx/sites-enabled/ 2>/dev/null
    cp $SRCDIR/configs/.env $IAMINE/.env 2>/dev/null
    sudo systemctl daemon-reload

    echo "  [7/8] Demarrage services..."
    sudo systemctl enable iamine-pool iamine-worker
    sudo systemctl start iamine-pool iamine-worker
    sudo systemctl restart nginx
    sleep 5

    echo "  [8/8] Verification..."
    $IAMINE/scripts/verify-system.sh

    rm -rf $TMPDIR

    echo ""
    echo "=== CHECKLIST MANUELLE ==="
    echo "  [ ] DNS: pointer iamine.org vers la nouvelle IP"
    echo "  [ ] SSL: sudo certbot --nginx -d iamine.org"
    echo "  [ ] Cron: restaurer avec crontab configs/crontab.bak"
    echo "  [ ] SSH testeurs: creer les comptes david-tester, regis-tester"
    echo "  [ ] Modeles GGUF: transferer depuis l ancien VPS (~79 GB)"
    echo "  [ ] Workers: ils se reconnecteront automatiquement si meme domaine"
    echo "  [ ] Test complet: lancer le test Alice/Bob/Charlie"
    ;;

*)
    echo "IAMINE VPS Migration"
    echo "Usage:"
    echo "  ./migrate-vps.sh export   — exporte tout dans un tar.gz"
    echo "  ./migrate-vps.sh import FILE.tar.gz — importe sur un nouveau VPS"
    ;;
esac
