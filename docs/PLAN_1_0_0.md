# 1.0.0 — Plan & justification of architectural choices

> This document is the audit trail. It captures **why** 1.0.0 exists in
> the shape it does, what alternatives were rejected, and the guardrails
> set before the code was written. Future maintainers must read this
> before proposing changes that relax any of these decisions.

## Context triggering the release

On 2026-04-17, after restoring the `cellule.ai` VPS from a filesystem
corruption incident (see `project_vps_crash_20260417.md`), it became
apparent that the existing release pipeline had three structural issues :

1. **Docker Hub central trust** : `celluleai/pool:latest` was pushed by
   a single maintainer account. A compromised PAT meant the whole
   federation would ingest a malicious image within ~1 hour via
   Watchtower. This is the centralization the project claims to avoid.

2. **Cross-pool admin perception** : federated peers saw `celluleai/pool`
   live and the Phase 2 admin console surfaced them in the UI, creating
   the perception that peers could administer each other. Technically
   this was already gated by pool_config flags (`federation_admin_*_enabled=false`)
   and explicit local approval, but the mere surface violated the
   sovereignty model.

3. **Naming incoherence** : version numbers had drifted (0.2.89 in git,
   0.2.91 in `__init__.py`, 0.2.90 cited in operator memory), making it
   hard to reason about deployed state.

The 1.0.0 release addresses all three in a way that is **optimised for
third-party federation** : any operator can fork the project, replace
the signing keys with their own, and run an independent molecule
without carrying artefacts of the original `cellule.ai` network.

## Core guiding invariant : worker-first

> *"Sans worker, pas de cellule."* — David, 2026-04-17

Every decision below passed this test : **does it make it harder to
keep workers online ?** If yes, the decision was rejected or
mitigated. Workers must be the load-bearing concern, not an
after-thought.

Operational consequences wired into the code :

- Release verification is **warning-only by default**. A missing or
  corrupted signature cannot take a pool (and its workers) offline.
- Strict enforcement is explicitly **opt-in** (`IAMINE_STRICT_SIGNING=1`)
  for operators who value supply-chain paranoia over continuity.
- No cross-pool kill switch. No federation can coerce another pool
  offline. Local admin is always sovereign.

## Decisions and rejected alternatives

### Decision 1 : `1.0.0`, not `1.3.0`, not `0.2.92`

**Chosen** : bump to `1.0.0`.

**Alternatives considered** :

- `0.2.92` — continuity. Rejected : the semantic weight of this release
  (community governance, supply-chain signing, federation-ready forking)
  is categorically different from minor version bumps. Staying in 0.2
  would understate the commitment.
- `1.3.0` — David's initial proposal. Rejected on SemVer semantics :
  implies versions 1.0/1.1/1.2 existed, which they don't. Future
  readers of the changelog would find an inexplicable gap — the very
  "artefact" this release aims to avoid.

**Implication** : `1.0.0` does NOT mean API is frozen forever. M10 and
M11 are still scaffold. `docs/GOVERNANCE.md` states this explicitly to
prevent operators from reading `1.0.0` as "feature-complete".

### Decision 2 : supply-chain signing via custom Ed25519, not sigstore/cosign

**Chosen** : custom Ed25519 signing, reusing the same primitives as
`core/federation.py::enforce_fed_policy`.

**Alternatives considered** :

- **sigstore/cosign** — modern industry standard, Docker-native
  verification. Rejected for 1.0.0 because :
  1. It introduces an external trust root (Fulcio/Rekor) that the
     project has no control over.
  2. It requires every pool host to trust a CA that is outside the
     federation's Ed25519 key material.
  3. Adds a binary dependency (`cosign`) that isn't in the current stack.
- **Docker Content Trust (notation v2)** — tied to Docker Hub's
  infrastructure. Rejected for the same centralization reason.
- **PGP / GnuPG** — RFC 4880 is a historical liability (key format
  complexity, bad tooling). Rejected.

Custom Ed25519 keeps the trust root inside the project, consistent with
the rest of the federation, and costs ~150 lines of Python with zero new
dependencies (`cryptography` already present).

### Decision 3 : warning-only verification, strict opt-in

**Chosen** : `verify_release_at_boot()` logs and returns status ; never
raises. `IAMINE_STRICT_SIGNING=1` turns warnings into `SystemExit`.

**Alternatives considered** :

- **Reject unsigned by default** — initial reflex. Rejected by
  `molecule-guardian` review as a `HARD VIOLATION` of the "no master
  pool" invariant : pools would depend on the maintainer key at boot,
  making them down-time-correlated to key availability. One lost key =
  every federated pool refuses to start.
- **Silent acceptance** — no warning. Rejected : operators need to know
  the state of their supply chain.

### Decision 4 : MAINTAINERS in Git, K=1 during bootstrap, K>=2 for `:stable`

**Chosen** : plaintext `MAINTAINERS` file at repo root, one line per
maintainer, K=1 initially (bootstrap signer under the `molecule`
nickname), `:stable` channel requires K>=2.

**Alternatives considered** :

- **Hardcode pubkey in Python code** — rejected as permanent
  centralization artefact (SQL-injection-proof but code-review-proof,
  still "founder key in perpetuity").
- **Database-stored maintainers** — rejected : a DB compromise could
  auto-promote an attacker's key. The file must be part of the
  immutable image.
- **On-chain maintainers set** — premature. No on-chain primitives in
  1.0.0.

K=1 is explicitly marked as **transitional** in `docs/GOVERNANCE.md`.
The `:stable` channel structurally requires K>=2, making single-signer
publication of the community-hardened channel impossible by design.

### Decision 5 : `federation_admin_query_enabled` default flipped to `true`

**Chosen** : fresh installs from 1.0.0 onward default to `true`.
Existing pools that explicitly set the flag keep their value.

**Alternatives considered** :

- **Keep false** — rejected : breaks reciprocity. An admin of pool A
  can query pool B but not vice versa without manual config. Asymmetric
  by accident, not design.
- **Force true via migration** — rejected : violates the "existing
  pool's local config is sovereign" principle. Operators who deliberately
  set it to false had a reason.

The route `POST /v1/federation/peer/status` already enforces trust>=3
strictly and limits response fields to aggregated tiers (never raw
`credits_*`). Token-guardian invariant 4 (M11-scaffold) preserved.

### Decision 6 : keep the `/v1/federation/admin/{request,decide,...}` cluster

**Chosen** : keep the Phase 2 Molecule Console cluster, writes gated
off by default, reads gated on.

**Alternatives considered** :

- **Delete the cluster entirely** — my initial proposal. Rejected by
  `token-guardian` (finding #2) : `circuit_reset` via the
  request/decide flow is the only automated operational coordination
  channel. Deleting it forces every pool op to intervene over SSH at
  every incident, silently shifting operational cost from the 30%
  pool-operator budget share to personal time. That violates the
  "distributed budget equilibrium" principle.

### Decision 7 bis : publish `iamine/core/federation_admin.py` to GitHub

**Chosen** : add `federation_admin.py` (the cross-pool admin
request/decide/callback flow) to `GITHUB_WHITELIST.txt`.

**Context** : pre-1.0.0, the "Option B — transparence ciblée" policy
kept all server-side route/flow files private on GitHub (only worker,
CLI, MCP and a subset of core libs were published). This made sense
when Cellule was pre-federation.

**Rationale for publication in 1.0.0** :

- `GOVERNANCE.md` claims the project is "fédérable par tout opérateur
  tiers". A forker who can't audit `federation_admin` cannot verify
  what the cluster `/v1/federation/admin/*` actually enforces. That
  undermines the claim.
- The flow is already observable from any bonded peer (HTTP endpoints
  exposed). Hiding the source is security-through-obscurity, not
  security.
- The `is_query_enabled` default fix (shipped in 1.0.0) is only
  visible to operators who read the source. Making the file public
  aligns with the reciprocity principle we're enshrining.

**Risk check** : reviewed the file for leaks (IPs, credentials, admin
emails, PII) before adding. Clean.

### Decision 7 : Docker `:stable` channel requires out-of-band cosigning

**Chosen** : `scripts/build_release.sh` publishes `:pinned-<version>`
and `:latest` with a single signature. `:stable` is a separate,
manual, K>=2 promotion.

**Alternatives considered** :

- **Auto-promote `:latest` to `:stable` after N days** — rejected :
  creates a time-based trust gradient unrelated to code review.
- **`:stable` = single maintainer with N-day delay** — rejected : same
  problem. Time is not a substitute for review.

K>=2 for `:stable` is the only social-layer enforcement in the
pipeline. Everything else is code-enforced.

## What was explicitly rejected

1. **Any "founder" or "root" role in the code.** There is no pubkey
   that the code recognizes as uniquely privileged. MAINTAINERS is a
   supply-chain file, read only by the signing/verifying logic,
   consulted never by the federation trust logic.

2. **Any central kill switch.** Pools can be revoked by their peers
   individually, never collectively. The kill switch file
   (`/etc/iamine/fed_disable`) is strictly local.

3. **Any requirement to federate with `cellule.ai`.** Forkers who
   change `MAINTAINERS` and `IAMINE_FED_MOLECULE` run an independent
   molecule with zero upstream dependency.

4. **Token economics activation.** 1.0.0 ships with `revenue_ledger`,
   `slashing_events`, `federation_settlements` all in dry_run. The
   token ($IAMINE) is not on-chain. No real settlement occurs. A
   future 1.x release will flip this when the on-chain primitives are
   in place. `docs/GOVERNANCE.md` §5 states this explicitly so
   operators don't mistake 1.0.0 for "economically stable".

## Guardrails for future changes

Any PR that :

- Adds a hardcoded pubkey check anywhere in `iamine/core/` other than
  `release_signing.py` → **BLOCKER** under "no master pool".
- Makes signing verification fatal without `IAMINE_STRICT_SIGNING=1` →
  **BLOCKER** under "worker-first".
- Grants admin capability to any peer based on Ed25519 identity alone →
  **BLOCKER** under "admin-local".
- Adds a `:stable` push path that doesn't require K>=2 signatures →
  **BLOCKER** under the MAINTAINERS rotation rules.
- Touches `revenue_ledger` flow without a `token-guardian` review →
  **BLOCKER** under distributed budget equilibrium.

## Execution trail

1. **2026-04-17 ~18:30 CEST** : rotated exposed GitHub + Docker Hub PAT,
   centralised secrets in `/etc/iamine/secrets.env` (systemd
   `EnvironmentFile=`). Old PATs revoked.
2. **2026-04-17 ~19:00 CEST** : guardian reviews (molecule + token) on
   the draft plan. Three arbitrations : warning-only, keep admin
   cluster, tier-gate query — all confirmed.
3. **2026-04-17 ~19:20 CEST** : Lot A — Ed25519 keypair generated,
   `MAINTAINERS` file created, `scripts/sign_release.py` written,
   `iamine/core/release_signing.py` wired into `startup.py`, 4 cases
   tested green.
4. **2026-04-17 ~19:40 CEST** : Lot B — `is_query_enabled` default
   fixed, version bumped to 1.0.0 in `__init__.py` and `pyproject.toml`,
   Python requirement raised to `>=3.12`.
5. **2026-04-17 ~19:50 CEST** : Lot C — `docs/GOVERNANCE.md`,
   `docs/RUNBOOK_1_0_0_MIGRATION.md`, `docs/PLAN_1_0_0.md` written.

## What's left before a clean push

- Run `scripts/build_release.sh --signer molecule` locally to produce the
  signed wheel + Docker image, verify end-to-end.
- Review diff against `main`.
- Tag `v1.0.0` in git.
- Push to `CELLULEAI/POOL` (David's explicit go required per project memory).
- `docker push celluleai/pool:pinned-1.0.0 celluleai/pool:latest`.
- Announce to bonded peers (master.86, Gladiator) — they auto-update
  via Watchtower on next `:53 UTC` poll.
- Announce publicly once the federation has confirmed the upgrade.
