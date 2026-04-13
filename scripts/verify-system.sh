#!/bin/bash
# ========================================================
# IAMINE — Verification complete du systeme
# Retourne 0 si tout passe, 1 si echec
# ========================================================
POOL=http://localhost:8080
PASS=0; FAIL=0; TOTAL=0; DETAILS=""

check() {
    TOTAL=$((TOTAL+1))
    if [ "$2" = "OK" ]; then
        PASS=$((PASS+1))
        echo "  PASS  $1"
        DETAILS="${DETAILS}PASS $1\n"
    else
        FAIL=$((FAIL+1))
        echo "  FAIL  $1 — $2"
        DETAILS="${DETAILS}FAIL $1: $2\n"
    fi
}

echo "=== IAMINE System Verification ==="
echo "Date: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Pool HTTP
code=$(curl -s -o /dev/null -w '%{http_code}' $POOL/v1/status 2>/dev/null || echo 0)
check "Pool HTTP" "$([ "$code" = "200" ] && echo OK || echo "HTTP $code")"

# Workers en ligne
workers=0
if [ "$code" = "200" ]; then
    workers=$(curl -s $POOL/v1/status 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("workers_online",0))' 2>/dev/null || echo 0)
    if [ "$workers" -eq 0 ]; then
        echo "  ...attente 30s reconnexion workers"
        sleep 30
        workers=$(curl -s $POOL/v1/status 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("workers_online",0))' 2>/dev/null || echo 0)
    fi
    check "Workers (>=1)" "$([ "$workers" -ge 1 ] && echo OK || echo "only $workers")"
fi

# Inference
resp=$(curl -s -m 60 -X POST $POOL/v1/chat/completions     -H 'Content-Type: application/json'     -d '{"model":"auto","messages":[{"role":"user","content":"ping"}],"max_tokens":16}' 2>/dev/null)
has_text=$(echo "$resp" | python3 -c 'import sys,json;d=json.load(sys.stdin);c=d.get("choices",[{}])[0].get("message",{}).get("content","");print("OK" if len(c)>0 else "empty")' 2>/dev/null || echo "error")
check "Inference" "$has_text"

# PostgreSQL tables
for table in conversations worker_benchmarks pipeline_workspace api_tokens; do
    pg=$(psql -U harpersatrage -d iamine -t -c "SELECT count(*) FROM $table" 2>/dev/null | tr -d ' ')
    check "PostgreSQL $table" "$([ -n "$pg" ] && [ "$pg" != "" ] && echo OK || echo "inaccessible")"
done

# API endpoints
for ep in /v1/status /v1/models/available /install.sh /install.ps1; do
    c=$(curl -s -o /dev/null -w '%{http_code}' $POOL$ep 2>/dev/null || echo 0)
    check "Endpoint $ep" "$([ "$c" = "200" ] && echo OK || echo "HTTP $c")"
done

# Site HTTPS
for url in https://iamine.org https://iamine.org/m; do
    c=$(curl -s -o /dev/null -w '%{http_code}' $url 2>/dev/null || echo 0)
    check "HTTPS $(echo $url | sed 's|https://iamine.org||' || echo /)" "$([ "$c" = "200" ] && echo OK || echo "HTTP $c")"
done

echo ""
echo "=== RESULT: $PASS/$TOTAL PASS ($FAIL failures) ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
