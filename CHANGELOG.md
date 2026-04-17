# Changelog

## 1.0.0 — 2026-04-17

First community-governed release.

### New : supply-chain signing

- `MAINTAINERS` file at repo root (K=1 initial, transitional to K>=2).
- `scripts/sign_release.py` — Ed25519 sign/verify for wheel + Docker image digest.
- `scripts/build_release.sh` — end-to-end build/sign/push pipeline.
- `iamine/core/release_signing.py` — boot-time verification.
  **Worker-first policy** : warning-only by default. Set
  `IAMINE_STRICT_SIGNING=1` to refuse unsigned releases.

### New : Docker channels

| Tag | Signed by | Use |
|-----|-----------|-----|
| `:pinned-1.0.0` | any single maintainer | reproducible, immutable |
| `:latest` | any single maintainer | newest |
| `:stable` | K>=2 maintainers | community-hardened |

### New : docs

- `docs/GOVERNANCE.md` — social contract (worker-first invariant, no
  master pool, no founder key, federation-ready forking).
- `docs/RUNBOOK_1_0_0_MIGRATION.md` — zero-downtime upgrade path.
- `docs/PLAN_1_0_0.md` — audit trail for the 7+1 architectural decisions.

### Changed

- **Transparency** : `iamine/core/federation_admin.py` AND
  `iamine/routes/admin.py` are now published on GitHub
  (`GITHUB_WHITELIST.txt`). A third-party operator who wants to
  federate can audit the full cross-pool admin flow and the local
  admin console. Previously kept private under "Option B" policy.
- **PII scrubbed** from `routes/admin.py` : hardcoded admin email
  replaced by `ROOT_ADMIN_EMAILS` env var (comma-separated list).
  Default empty (no root admin protection). Instance operators set
  the variable to protect specific admin emails from removal.
  Hardcoded SMTP/alert email fallbacks removed ; alerts now skipped
  cleanly if `ALERT_EMAIL`/`SMTP_FROM` not configured.
- `federation_admin_query_enabled` default flipped from `false` to
  `true`. Trust>=3 and field allowlist still enforced. Existing pools
  with the flag explicitly set keep their value.
- Python requirement raised to `>=3.12` (aligned with production).
- `docker/Dockerfile` now installs from a locally-signed wheel instead
  of pulling from `cellule.ai/pypi`. Ensures reproducibility.

### Removed

Nothing. All existing routes and behaviors are preserved. The
`/v1/federation/admin/{request,decide,callback,send,inbox,outbox}`
cluster is kept (writes gated off by default, reads gated on) after
token-guardian review confirmed it is the only automated operational
coordination channel between pools.

### Security

- Supply chain hardened via signed releases.
- `ADMIN_PASSWORD`, `DB_PASS`, `HF_TOKEN` rotated on the reference
  `cellule.ai` deployment. `HF_TOKEN` emptied pending regeneration.
- GitHub + Docker Hub PAT rotated, old ones revoked.
- Secrets moved to `/etc/iamine/secrets.env` (systemd `EnvironmentFile`).

### Not included (by design)

- `$IAMINE` token on-chain : postponed.
- M10-active settlement : stays in `dry_run`. `revenue_ledger`,
  `slashing_events`, `federation_settlements` all logged but no real
  settlement occurs.
- M11 split-brain detection : deferred until N>=5 WAN pools.
