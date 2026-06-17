# Changelog

## Unreleased

### Routing — quality floor (q75 / 9B-equivalent)

- Non-trivial prompts are no longer routed to a model below `quality_score` 75
  (≈9B in the Qwen3.5 family) as the **final** answerer while an adequate worker is
  idle. Fixes occasional wrong answers from very small models (e.g. a 1B answering
  arithmetic incorrectly), especially in stateless mode where the post-draft review
  pipeline is off. Short arithmetic/precision prompts are now classified as
  non-trivial, and a downward KNN/LLM tier re-classification can no longer push a
  non-trivial prompt back to the smallest tier.
- **Graceful degrade, never 503**: when no worker at or above the floor is idle, the
  strongest available worker answers (and, in stateless, a stronger worker reviews it
  if one frees up). Small workers still serve trivial prompts, act as classifiers, and
  answer non-trivial prompts when nothing stronger is free.
- The floor is `quality_score`-based (handles MoE models such as 35B-A3B correctly)
  and tunable via `IAMINE_MIN_ANSWER_QUALITY` (default 75). Also fixes a latent
  `UnboundLocalError` in the near-full-context routing penalty.
- **Community note**: this skews non-trivial traffic toward stronger workers; very
  small contributors earn relatively more on trivial prompts than on hard ones. The
  60/20/10/10 reward split is unchanged — only routing preference.

## 1.0.3 — 2026-06-16

Maintenance release — domain migration cleanup, a docs/security pass. No breaking
API changes; no DB migrations.

### Maintenance — domain migration `iamine.org` → `cellule.ai`

- Replaced all stale `iamine.org` URLs/endpoints with `cellule.ai` across docs,
  install scripts, and code defaults (the `iamine.org` domain is retired, DNS gone).
  This also fixes the admin model-assignment download URL, which still pointed at the
  dead `dl.iamine.org` host (now `dl.cellule.ai`). The backward-compat shim that
  detects a legacy `iamine.org` pool URL in an existing worker config and rewrites it
  to `cellule.ai` is intentionally preserved, as is the model-download allowlist that
  still accepts the legacy host.
- Removed a dead pre-migration `iamine/index.html` duplicate (the live homepage is
  `iamine/static/index.html`).
- Fixed a duplicate `https://cellule.ai` entry introduced in the default
  `IAMINE_CORS_ORIGINS` list.

### Security

- Removed `iamine/static/docs/CLAUDE_VPS_RESTORE.md` — an internal disaster-recovery
  runbook that was served publicly at `/docs/CLAUDE_VPS_RESTORE.md`. It exposed
  internal operational paths (no live credentials). Internal ops docs no longer ship
  in the public served docs directory.

### Docs

- New `docs/CONNECT.md` + the served page `/docs/connect.html` (linked from the
  homepage API section) — a community onboarding card for connecting any
  OpenAI-compatible tool (Nextcloud `integration_openai`, Open WebUI, the Python
  `openai` SDK, Claude Code) to the pool, with `iamine/raw` stateless-model guidance.

### Docker

| Tag | Use |
|-----|-----|
| `:pinned-1.0.3` | reproducible, immutable (single-maintainer signed) |
| `:latest` | rolling |

## 1.0.2 — 2026-06-15

Maintenance release — OpenAI stateless mode + SEO. No breaking changes.

### New

- **OpenAI stateless mode** for `/v1/chat/completions` — trigger via header
  `X-Iamine-Stateless: 1` **or** model `iamine/raw` (now advertised in `/v1/models`).
  Disables long-term memory injection, the assist pipeline (sub-agents / auto-review),
  sticky `conv_id` and special memory commands, returning a plain OpenAI response.
  Fixes parasitic output (JSON `follow_ups`, prompt-token bloat, "roles must alternate")
  with stateless clients such as Nextcloud `integration_openai` and Open WebUI. The
  rich behavior (memory + assist) stays the default for the iamine CLI and clients
  that don't opt in.

### SEO / site

- JSON-LD structured data (Organization, WebSite, SoftwareApplication), keyword-
  optimized `<title>` and meta description, canonical + `hreflang` on the bilingual
  architecture docs, `/app` canonical, and corrected social handle (`@celluleai`).

### Docker

| Tag | Use |
|-----|-----|
| `:pinned-1.0.2` | reproducible, immutable (single-maintainer signed) |
| `:latest` | rolling |

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
