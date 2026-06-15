# Changelog

## 1.0.1 — 2026-06-15

Security release — addresses the 2026-06-15 internal security audit (findings sec-pub-01..14).
No breaking API changes for users. Pools auto-apply DB migrations 027/028 at startup.

### Security fixes

- **Admin API** — every `/admin/api/*` mutator now requires admin authentication
  (`require_admin` dependency). Closes unauthenticated remote worker control.
- **Google OAuth** — the `id_token` RS256 signature is now verified against Google's
  JWKS (issuer/audience/expiry too). Previously the payload was decoded without
  signature verification (account-takeover risk). Stale-JWKS tolerant for resilience.
- **Admin passwords** — hashed with argon2 (were stored and compared in clear).
- **Sessions/cookies** — admin token no longer accepted via `?token=` (log/referer
  leak); use `Authorization: Bearer`. Admin cookies set `httponly` + `samesite=strict`.
- **Worker admission** — optional `IAMINE_POOL_JOIN_TOKEN` gates `/ws`. **Off by
  default — the public pool stays open to volunteer workers.**
- **Dev routes** — `/v1/dev/*` are admin-gated and only registered when `IAMINE_DEV=1`
  (404 in production by default).
- **Brute-force** — per-IP login throttling + activation-code lockout; `CF-Connecting-IP`
  preferred as the client identifier (anti-spoof behind Cloudflare).
- **MCP client** — TLS verified by default (`IAMINE_MCP_CA` / `IAMINE_MCP_INSECURE`
  to override for self-signed pools).
- **/v1/contact** — field size bounds + per-IP rate limiting.

### Account token hardening (sec-pub-08)

- Account tokens are now **random** (no longer derived from email): non-forgeable,
  and not recoverable after account deletion.
- A **dedicated per-account encryption key** (`accounts.enc_key`) is decoupled from
  the bearer token — a leaked bearer no longer yields the decryption key, and the
  bearer can be rotated without re-encrypting memories.
- **Bearer rotation** on password change (revokes the old token; `account_id` and
  `enc_key` are preserved so no data is orphaned).
- Memory isolation moved from `sha256(token)` to the stable `account_id`, which also
  fixes a latent cross-pool memory-orphaning bug.

### Database migrations (auto-applied at pool startup)

- `027_sec_pub_08_account_isolation.sql` — adds `accounts.enc_key`.
- `028_sec_pub_08_rekey_memories.sql` — re-keys existing memories from
  `sha256(token)` to `account_id`, then purges memories of deleted accounts
  (completes RGPD deletion the previous code silently failed to do).

### Docker

| Tag | Use |
|-----|-----|
| `:pinned-1.0.1` | reproducible, immutable (single-maintainer signed) |
| `:latest` | rolling |

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
