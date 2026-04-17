# Cellule.ai Governance

> **Invariant 0 — worker-first.** Without workers, there is no Cellule.
> Every rule below is measured against one test : does it keep contributors
> willing to offer CPU/GPU time to the network ? If it doesn't, it's wrong.

## 1. What this document is (and isn't)

This is the **social contract** of the Cellule.ai network : the rules
every operator (pool host, worker contributor, maintainer) can expect
others to follow, and the mechanisms by which those rules are verified.

It is **not** a governance authority. There is no founder with
perpetual privileges in the code. The rules below are what the codebase
enforces today ; anyone can fork, modify and run a different set of rules.

## 2. The hard invariants (enforced by code + guardians)

| # | Invariant | Where |
|---|-----------|-------|
| 0 | Worker-first : signing/verification is WARNING-only, never blocks boot | `iamine/core/release_signing.py` |
| 1 | No master pool — every pool is administratively sovereign over itself | `routes/admin.py::_check_admin` |
| 2 | Ed25519 signatures are the sole authority for inter-pool identity | `core/federation.py::enforce_fed_policy` |
| 3 | RAID replication, RF>=2 on bonded peers (trust>=3) | `core/federation_replication.py` |
| 4 | Workers are stateless ; DB is the brain | `core/accounts.py`, `core/agent_memory.py` |
| 5 | Append-only ledger, `revoke` never cascades on `revenue_ledger` | `core/federation.py::revoke_worker_cert` |
| 6 | Cross-pool admin requests require **local** approval (no silent execution) | `routes/federation.py::federation_admin_decide` |
| 7 | Reciprocal observability : any bonded peer can query any bonded peer | `routes/federation.py::federation_peer_status` |

Violating any of these is a BLOCKER in the `molecule-guardian` review process.

## 3. Release signing (supply chain, NOT governance)

### 3.1 What it is

Every official wheel and Docker image is signed by one or more maintainers
listed in [`MAINTAINERS`](../MAINTAINERS). The pool verifies signatures at
boot via [`iamine/core/release_signing.py`](../iamine/core/release_signing.py).

### 3.2 What it is NOT

The key in `MAINTAINERS` has **zero privilege on the network**. A peer pool
does not need to trust that key to federate, nor does it grant any admin
right. This is purely a supply-chain authenticity check.

### 3.3 Worker-first verification policy

When a pool starts, `verify_release_at_boot()` :

- Logs `INFO` if the running wheel is signed by a known maintainer,
- Logs `WARNING` if unsigned, tampered, or signed by an unknown key,
- **Continues booting in both cases** — a lost signing key must never
  take workers offline.

Operators who want strict enforcement (refuse to boot on any failed
verification) set `IAMINE_STRICT_SIGNING=1`.

### 3.4 Channels

| Tag | Who signs | Purpose |
|-----|-----------|---------|
| `:pinned-<version>` | any single maintainer | Reproducible, immutable. Operators who want zero surprise pin to this. |
| `:latest` | any single maintainer | Newest build. Convenient, less paranoid. |
| `:stable` | **K>=2** maintainers, cross-signed | The community-hardened channel. Only promoted after review and independent signature. |

A single maintainer (including the bootstrap signer) can publish
`:latest` and `:pinned-<version>`, but **cannot** publish `:stable`.
`:stable` requires at least two independent signatures from keys listed
in `MAINTAINERS`.

### 3.5 Key rotation

1. A maintainer proposes a new key in a signed commit to `MAINTAINERS`.
2. The change is signed by **K>=2** existing maintainers (or by the single
   maintainer if the project is still in bootstrap, K=1).
3. Pools pull the updated `MAINTAINERS` via normal image update.
4. The old key is retained in the file under `# retired:` for historical verification.

### 3.6 Forking and running your own molecule

Cellule.ai is federation-native. A third-party operator who wants to run
their own network :

1. Forks the repo.
2. Replaces `MAINTAINERS` with their own Ed25519 keys.
3. Builds and signs their own images (`scripts/build_release.sh --signer <their_nick>`).
4. Sets `IAMINE_FED_MOLECULE=<their_molecule_name>` in pool configs to
   create a separate federation namespace.
5. Nothing in the codebase forces them to federate with `cellule.ai`.

The default `cellule.ai` molecule is one network among many that the
code enables — not a mandatory upstream.

## 4. Federation mechanics

### 4.1 No master pool

Every pool is the sole admin of itself. The admin password defined locally
is the only thing that grants administrative access (see `routes/admin.py::_check_admin`).
No amount of Ed25519 signatures from peers grants admin privileges over a pool.

### 4.2 Cross-pool admin requests

A bonded peer (trust>=3) can **request** an action on another pool
(`circuit_reset`, `query_events`) via `POST /v1/federation/admin/request`.
The request is **stored pending** and **never executes without local
approval** on the target pool.

This preserves sovereignty while allowing operational coordination
(e.g. another maintainer noticing your pool is stuck and asking you to
reset the circuit without SSH).

Write actions (`circuit_reset`) are off by default (`pool_config`
`federation_admin_writes_enabled=false`). Read actions (`query_events`)
are on by default from 1.0.0 onward (reciprocity).

### 4.3 Trust levels

| Level | Meaning | Capabilities |
|-------|---------|--------------|
| 0 | Unknown peer | None |
| 1 | Seen, handshake OK | Heartbeat, discovery |
| 2 | Account-bonded | Account replication (migration 016) |
| 3 | **Replication-bonded** | Full RAID replication, gossip, observability (`peer/status`), admin request/decide cluster |
| 4 | Settlement-bonded | Reserved for M10 when token economics go live |

Level 3 is the working ceiling for 1.0.0. Level 4 is LOCKED until
$IAMINE goes on-chain and M10-active graduates from dry_run.

## 5. Token economics (status : dry-run)

The `revenue_ledger`, `slashing_events`, and `federation_settlements`
tables are in place, schemas are stable, but **no real settlement occurs
in 1.0.0**. There is no $IAMINE token on-chain. All economic flows are
logged and auditable but not executed.

The intended distribution is documented as **60% workers / 20% users /
10% pool operators / 10% treasury**, subject to revision at token
on-chain launch. See `project_decisions_a_tranchees.md` in project memory
for the full 7-question economic framework.

This document will be updated before the first real settlement.

## 6. Incident response

### 6.1 A maintainer loses their key

1. Announce compromise publicly (social channels + signed commit).
2. Remaining K-1 maintainers sign a key rotation commit removing the compromised key.
3. Pools pull update. Old signatures remain valid for historical artifacts
   but new releases using the old key are rejected in strict mode.

### 6.2 All maintainer keys lost

Worker-first : pools keep running on their last pulled image.
Community forks the repo, establishes new `MAINTAINERS`, and announces the
continuity channel. The old `celluleai/pool:*` Docker Hub namespace is
orphaned, not hijacked.

### 6.3 A pool is caught behaving maliciously

No central mechanism demotes it. Each peer independently :

1. Uses `POST /v1/federation/peers/<atom_id>/revoke` to unbond locally.
2. Optionally shares observations via gossip for other pools to assess.

Reputation is local and social. The code does not enforce reputation.

## 7. Amendments

This document is versioned in Git. Amendments follow the same process as
`MAINTAINERS` rotation : proposed in a signed commit, merged with K>=2
maintainer signatures (or K=1 during bootstrap), and announced publicly.
