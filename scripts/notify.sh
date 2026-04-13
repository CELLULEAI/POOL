#!/bin/bash
# ========================================================
# IAMINE — Notification email
# Usage: ./notify.sh STATUS CHANGES EMAIL [DURATION] [SNAPSHOT] [VERIFY_OUTPUT]
# ========================================================
STATUS=$1
CHANGES=$2
EMAIL=${3:-david.mourgues@gmail.com}
DURATION=${4:-0}
SNAPSHOT=${5:-none}
VERIFY=${6:-}

SUBJECT="[IAMINE] Update $STATUS — $(date +%Y-%m-%d)"

BODY="=== IAMINE Auto-Update Report ===

Date: $(date '+%Y-%m-%d %H:%M:%S')
Duration: ${DURATION}s
Status: $STATUS
Snapshot: $SNAPSHOT

--- Changes Applied ---
$CHANGES

--- Verification ---
$VERIFY

--- Pool Status ---
$(curl -s http://localhost:8080/v1/status 2>/dev/null | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin)
    print(f"Workers: {d.get(\"workers_online\",0)}")
    print(f"Version: {d.get(\"version\",\"?\")}")
    print(f"Uptime: {d.get(\"uptime_sec\",0)//60}min")
except: print("Pool unavailable")
' 2>/dev/null)

--- End Report ---
"

# Essayer msmtp, puis mail, puis logger
if command -v msmtp &>/dev/null; then
    echo -e "Subject: $SUBJECT\nTo: $EMAIL\n\n$BODY" | msmtp $EMAIL 2>/dev/null && echo "Email sent via msmtp" && exit 0
fi
if command -v mail &>/dev/null; then
    echo -e "$BODY" | mail -s "$SUBJECT" $EMAIL 2>/dev/null && echo "Email sent via mail" && exit 0
fi
# Fallback: webhook ou log
echo "WARNING: email non envoye (msmtp/mail non configure)"
echo "Subject: $SUBJECT"
echo "$BODY"
