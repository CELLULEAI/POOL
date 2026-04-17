#!/bin/sh
# IAMINE Pool entrypoint — wait for postgres then launch

set -e

# Default env
: "${DB_HOST:=iamine-pg}"
: "${DB_PORT:=5432}"
: "${DB_USER:=iamine}"
: "${DB_NAME:=iamine}"
: "${IAMINE_DB:=postgres}"
: "${IAMINE_FED:=observe}"
: "${IAMINE_FED_MOLECULE:=iamine-testnet}"
: "${POOL_NAME:=${IAMINE_FED_NAME:-my-pool}}"
: "${POOL_URL:=${IAMINE_FED_URL:-http://localhost:8080}}"

export DB_HOST DB_PORT DB_USER DB_NAME DB_PASS
export IAMINE_DB IAMINE_FED IAMINE_FED_MOLECULE
export IAMINE_FED_NAME="${POOL_NAME}"
export IAMINE_FED_URL="${POOL_URL}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-change-me-in-production}"

echo ">>> Cellule.ai Pool container booting"
echo ">>> POOL_NAME=${POOL_NAME}"
echo ">>> POOL_URL=${POOL_URL}"
echo ">>> DB_HOST=${DB_HOST}:${DB_PORT}/${DB_NAME}"

# Release signing verification runs inside initialize_pool() at boot (WARNING-only).
# Env vars IAMINE_MAINTAINERS, IAMINE_RELEASE_SIG, IAMINE_RELEASE_ARTIFACT are
# set by the Dockerfile. Operators who want strict enforcement can pass
# -e IAMINE_STRICT_SIGNING=1 to refuse to boot on unsigned/tampered images.

# Wait up to 60s for postgres to accept connections
MAX_TRIES=60
TRIES=0
while ! PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c '\q' 2>/dev/null; do
    TRIES=$((TRIES + 1))
    if [ $TRIES -ge $MAX_TRIES ]; then
        echo "FATAL: postgres not ready after ${MAX_TRIES}s" >&2
        exit 1
    fi
    sleep 1
done
echo ">>> postgres ready after ${TRIES}s"

# Launch pool
exec python -m iamine pool --host 0.0.0.0 --port 8080
