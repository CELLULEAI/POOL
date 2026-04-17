# Runbook : migrate a live pool from 0.2.x to 1.0.0

> Worker-first invariant : **do not stop the pool** during migration if at
> all possible. A worker disconnected for more than ~30s will renegotiate
> elsewhere and may not come back. All steps below are designed to be
> zero-downtime.

## Prerequisites

- Running pool on version 0.2.89+ (git HEAD `e47a72f` or later).
- Admin access to the host (`ssh` + `sudo`).
- Credentials rotated if they've leaked (see `project_vps_crash_20260417.md` in project memory).

## Step 1 : back up before touching anything

```bash
# On the pool host :
sudo -u postgres pg_dump iamine | gzip > ~/iamine-pre-1.0.0-$(date +%Y%m%d-%H%M).sql.gz

# Snapshot the project tree
tar czf ~/iamine-src-pre-1.0.0-$(date +%Y%m%d-%H%M).tar.gz \
    -C /home/<user> iamine --exclude=iamine/venv --exclude=iamine/dist
```

If you run on `master.86` or a federated peer, also snapshot `/root/.iamine/federation/`.

## Step 2 : pull the signed 1.0.0 image (Docker path)

Operators running via Docker :

```bash
docker pull celluleai/pool:pinned-1.0.0

# Optional : verify the image digest against the published signature
# (sig file distributed via GitHub release attachment)
# scripts/sign_release.py verify <wheel_path> --maintainers MAINTAINERS
```

Watchtower operators get the new image automatically at the next poll
(:53 UTC hourly). **If you want the signed 1.0.0 specifically**, switch
the compose file to `image: celluleai/pool:pinned-1.0.0` before Watchtower
runs, otherwise `:latest` may advance past 1.0.0 without signature.

## Step 3 : install via pip (host-based path)

```bash
cd /path/to/iamine && git pull
source venv/bin/activate
pip install --upgrade pip
pip install --upgrade "iamine-ai[pool]==1.0.0" \
    -i https://cellule.ai/pypi \
    --extra-index-url https://pypi.org/simple
```

## Step 4 : apply the new `query_enabled` default (optional)

1.0.0 changes the default of `federation_admin_query_enabled` from
`false` to `true`. Existing pools that have the flag set explicitly
**keep their current value**. If your pool is on an older version and
has `federation_admin_query_enabled=false` stored from the legacy
default, it will stay false. To adopt the new reciprocity default :

```sql
DELETE FROM pool_config WHERE key = 'federation_admin_query_enabled';
-- next pool restart picks up the new default (true)
```

Trust>=3 gate and field allowlist still apply — no economic data
(`credits_*`) is ever exposed.

## Step 5 : restart gracefully

```bash
sudo systemctl restart iamine-pool.service
sudo journalctl -u iamine-pool -n 80 --no-pager
```

Expected log lines on clean boot :

```
iamine.signing: release verified : iamine_ai-1.0.0-py3-none-any.whl signed by <nickname> at <iso8601>
iamine.pool: L3 PostgreSQL store active — production mode
iamine.federation: federation identity loaded atom_id=<...>
iamine.pool: POOL STATUS <N> workers ...
```

If you see `release signature not found — running unverified code` it's
a WARNING, not an error : the pool still runs. Set
`IAMINE_STRICT_SIGNING=1` in your service environment if you want to
refuse unsigned releases.

## Step 6 : verify federation reconnection

```bash
curl -s http://127.0.0.1:8080/v1/federation/peers | jq
```

You should see your bonded peers with `trust_level >= 3` and recent
`last_seen`. If a peer missed heartbeats during the restart window,
they will recover on the next gossip cycle (60s default).

## Step 7 : revert plan (if something breaks)

```bash
# Stop 1.0.0
sudo systemctl stop iamine-pool.service

# Reinstall previous version
pip install "iamine-ai[pool]==0.2.89" -i https://cellule.ai/pypi \
    --extra-index-url https://pypi.org/simple

# Rollback DB if you applied migrations (unlikely — 1.0.0 adds no new migrations)
gunzip < ~/iamine-pre-1.0.0-<timestamp>.sql.gz | sudo -u postgres psql iamine

# Restart
sudo systemctl start iamine-pool.service
```

## What 1.0.0 changes at a glance

| Change | Impact |
|--------|--------|
| Release signing (Ed25519, warning-only) | WARNING log if unsigned ; no functional change |
| `federation_admin_query_enabled` default flipped to `true` | Bonded peers (trust>=3) can query your aggregated status ; no credits data exposed |
| `MAINTAINERS` file bundled in image | `/opt/cellule/MAINTAINERS` available for verification |
| Python >=3.12 required | Matches production VPS + Docker image (already bookworm + 3.12) |
| Version bump 0.2.89 → 1.0.0 | Signals "community-governed release", API not API-stable (M10/M11 still scaffold) |

## What 1.0.0 does NOT change

- No schema migration.
- No breaking API changes on `/v1/*` routes.
- Admin console continues to require local password.
- Workers running 0.2.x wheel continue to connect to 1.0.0 pools normally (protocol unchanged).
- Treasury / settlement remain in dry_run (M10-active not live).
