"""M10-active Q4 D-DISPUTE scaffold — re-execution sampling 1% + admin fallback.

Implements decision Q4 (D-DISPUTE) arbitrated 2026-04-11 :
    (c) re-execution sampling 1% by random bonded peer + (a) admin manual fallback.

Scope phase 1 (this module) :
- Append-only-ish federation_disputes table (status mutable by design, see migration 010)
- Deterministic non-manipulable verifier peer selection
  via sha256(job_id + epoch_hourly + salt) mod bonded_count, excluding contested
  AND origin pools (collusion mitigation — guardian recommendation 1).
- 1% hash-based sampling on job_id
- Admin-only record_dispute + mark_dispute_verified
- No automatic trigger from inference flow (M11-active)
- No HTTP cross-pool verifier call (M11-active)
- No actual re-execution (M11-active)

FORMULA ASSUMPTION (scaffold) : the 1% deduction from settlement for the
verifier peer on a successfully verified dispute is DEFERRED to the future
chunk touching settlement.propose_settlement with guardian re-invocation.
This module records WHO was picked and WHAT the outcome was, but does NOT
modify revenue_ledger or federation_settlements. Revisit M10-active phase 2.

SCAFFOLD INVARIANTS (molecule-guardian validated 2026-04-11) :

    DISPUTES SCAFFOLD — federation_disputes intentionally EXCLUDED from ledger
    merkle root v1 (M11-scaffold invariant 2, format frozen). Any extension
    requires LEDGER_MERKLE_VERSION bump. See migration 010 comments.

    SCAFFOLD RF=1 — dispute events are local to this pool. Losing the pool
    loses its dispute history. M11.2 will add gossip replication.

    DOUBLE EXCLUSION in pick_verifier_peer : both contested_pool_id AND
    origin_pool_id are removed from the candidate set. Collusion mitigation
    is the raison d'etre of Q4 (risk #2 architecture review v2).

Decision sources :
- project_decisions_a_tranchees.md (Q4 tranchée)
- project_m11_scaffold_invariants.md (invariant 2 frozen merkle)
- project_architecture_review_v2.md (risk #2 collusion A+B)
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Optional

from . import federation

log = logging.getLogger("iamine.disputes")


# Sampling constants (scaffold)
DISPUTE_SAMPLING_RATE_PCT = 1  # 1% of forwarded jobs eligible for re-execution
DISPUTE_EPOCH_SEC = 3600  # hourly epoch for deterministic verifier selection
DISPUTE_SALT = "iamine-dispute-v1"  # static salt, not secret — seeds hash domain separation


def is_dispute_sampling_enabled() -> bool:
    """Kill switch independent of SETTLEMENT_ENABLED and SLASHING_ENABLED."""
    return os.environ.get("DISPUTE_SAMPLING_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )


def should_sample(job_id: str) -> bool:
    """Hash-based 1% sampling — deterministic per job_id.

    Returns True iff sha256(job_id) % 100 < DISPUTE_SAMPLING_RATE_PCT.
    """
    if not job_id:
        return False
    h = hashlib.sha256(job_id.encode("utf-8")).digest()
    bucket = int.from_bytes(h[:4], "big") % 100
    return bucket < DISPUTE_SAMPLING_RATE_PCT


async def pick_verifier_peer(
    pool,
    job_id: str,
    contested_pool_id: str,
    origin_pool_id: Optional[str] = None,
    epoch: Optional[int] = None,
) -> Optional[str]:
    """Deterministic non-manipulable selection of a verifier peer.

    DOUBLE EXCLUSION (guardian rec #1) : both contested_pool_id and
    origin_pool_id are excluded from the candidate set. If the same pool
    is both contested and origin, the single exclusion still applies.

    Returns the atom_id of the selected peer, or None if fewer than 1
    eligible candidate. The epoch defaults to floor(now / 3600) so the
    selection changes hourly — short enough to resist grinding, long
    enough to be stable across a dispute lifecycle.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return None
    if not job_id:
        return None

    if epoch is None:
        epoch = int(time.time()) // DISPUTE_EPOCH_SEC

    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT atom_id FROM federation_peers
            WHERE revoked_at IS NULL
            ORDER BY atom_id
            """
        )

    candidates = [r["atom_id"] for r in rows]
    excluded = {contested_pool_id}
    if origin_pool_id:
        excluded.add(origin_pool_id)
    candidates = [c for c in candidates if c not in excluded]

    if not candidates:
        return None

    # Deterministic hash: sha256(job_id + epoch + salt) mod len(candidates)
    seed_material = f"{job_id}|{epoch}|{DISPUTE_SALT}".encode("utf-8")
    h = hashlib.sha256(seed_material).digest()
    idx = int.from_bytes(h[:8], "big") % len(candidates)
    return candidates[idx]


def _fed_disabled_by_kill_switch() -> bool:
    """Honor /etc/iamine/fed_disable (guardian rec #5)."""
    try:
        return federation.is_fed_disabled_by_fs()
    except Exception:
        return False


def sample_and_log_forwarded(
    job_id: str,
    origin_pool_id: str,
    exec_pool_id: str,
) -> bool:
    """Observer-only hook for the M7a forward path.

    Checks if a forwarded job is eligible for re-execution sampling (1% per
    should_sample) and emits a structured log line if so. **Does NOT write
    to federation_disputes**, **does NOT raise a dispute**, **does NOT make
    any HTTP call**. Pure observation to validate the sampling rate in live
    traffic before phase 2 activation enables real re-execution.

    Gated behind DISPUTE_SAMPLING_ENABLED env var (already used by
    record_dispute). If disabled, returns False immediately without side
    effects. If the FS kill switch is active, also returns False.

    Returns True iff the job is sampled (for test assertions).

    Phase 2 will replace this with record_dispute + cross-pool verifier call.
    Phase 1 scope : zero effect on inference flow, pure observability.
    """
    if _fed_disabled_by_kill_switch():
        return False
    if not is_dispute_sampling_enabled():
        return False
    if not job_id:
        return False
    if not should_sample(job_id):
        return False

    log.info(
        f"disputes: OBSERVE sample-eligible job_id={job_id} "
        f"origin={origin_pool_id} exec={exec_pool_id} "
        f"rate_pct={DISPUTE_SAMPLING_RATE_PCT} epoch_sec={DISPUTE_EPOCH_SEC} "
        f"phase=observer-only note=\"phase 2 will replace with record_dispute\""
    )
    return True


async def record_dispute(
    pool,
    job_id: str,
    contested_pool_id: str,
    origin_pool_id: Optional[str] = None,
    reason: Optional[str] = None,
    auto_pick_verifier: bool = True,
) -> dict:
    """Insert a dispute row (status=pending) with optional auto-picked verifier.

    Returns dict with status, id, verifier_peer_id, or error info.
    Never raises on disabled flag. If DISPUTE_SAMPLING_ENABLED=false or the
    FS kill switch is active, logs WARNING and returns skipped cleanly.
    """
    if _fed_disabled_by_kill_switch():
        log.warning(
            f"disputes: skipped by FS kill switch (/etc/iamine/fed_disable) "
            f"job_id={job_id}"
        )
        return {"status": "skipped", "reason_skipped": "fed_disable kill switch"}

    if not is_dispute_sampling_enabled():
        log.warning(
            f"disputes: record skipped (DISPUTE_SAMPLING_ENABLED=false) "
            f"job_id={job_id} contested={contested_pool_id}"
        )
        return {
            "status": "skipped",
            "reason_skipped": "DISPUTE_SAMPLING_ENABLED=false",
            "job_id": job_id,
            "contested_pool_id": contested_pool_id,
        }

    if not job_id:
        return {"error": "job_id is required"}
    if not contested_pool_id:
        return {"error": "contested_pool_id is required"}

    if not (hasattr(pool.store, "pool") and pool.store.pool):
        log.warning("disputes: no PG store, record dropped")
        return {"error": "no DB store"}

    verifier_peer_id = None
    if auto_pick_verifier:
        verifier_peer_id = await pick_verifier_peer(
            pool, job_id, contested_pool_id, origin_pool_id=origin_pool_id
        )

    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO federation_disputes (
                job_id, contested_pool_id, origin_pool_id,
                verifier_peer_id, status, reason
            )
            VALUES ($1, $2, $3, $4, 'pending', $5)
            RETURNING id, created_at
            """,
            job_id, contested_pool_id, origin_pool_id,
            verifier_peer_id, reason or "",
        )

    log.info(
        f"disputes: RECORD id={row['id']} job_id={job_id} "
        f"contested={contested_pool_id} origin={origin_pool_id} "
        f"verifier={verifier_peer_id}"
    )
    return {
        "status": "recorded",
        "id": row["id"],
        "job_id": job_id,
        "contested_pool_id": contested_pool_id,
        "origin_pool_id": origin_pool_id,
        "verifier_peer_id": verifier_peer_id,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def mark_dispute_verified(
    pool,
    dispute_id: int,
    result: str,
    verified_by_peer_id: Optional[str] = None,
    outcome: str = "verified",
) -> dict:
    """Update a dispute row status to verified/invalid/expired.

    outcome must be one of 'verified', 'invalid', 'expired' (enforced by DB CHECK).
    Sets verified_at = now() and updated_at = now().
    """
    if outcome not in ("verified", "invalid", "expired"):
        return {"error": f"invalid outcome: {outcome}"}
    if dispute_id is None or dispute_id <= 0:
        return {"error": "dispute_id must be positive"}

    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"error": "no DB store"}

    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE federation_disputes
            SET status = $2,
                result = $3,
                verifier_peer_id = COALESCE($4, verifier_peer_id),
                verified_at = now(),
                updated_at = now()
            WHERE id = $1
            RETURNING id, status, verifier_peer_id, verified_at
            """,
            dispute_id, outcome, result, verified_by_peer_id,
        )

    if not row:
        return {"error": f"dispute {dispute_id} not found"}

    log.info(
        f"disputes: MARKED id={dispute_id} outcome={outcome} "
        f"verifier={row['verifier_peer_id']}"
    )
    return {
        "status": "marked",
        "id": row["id"],
        "outcome": row["status"],
        "verifier_peer_id": row["verifier_peer_id"],
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
    }


async def get_dispute_state(
    pool,
    status: Optional[str] = None,
    job_id: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """Paginated read of disputes, with optional status/job filter.

    Returns dict with scaffold markers (authoritative=false) per
    guardian rec #6 — same pattern as M10-scaffold endpoints.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {
            "scaffold": True,
            "authoritative": False,
            "mode": "dry_run",
            "count": 0,
            "disputes": [],
        }

    limit = max(1, min(int(limit or 100), 1000))
    clauses = []
    params: list = []
    if status:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    if job_id:
        params.append(job_id)
        clauses.append(f"job_id = ${len(params)}")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    query = f"""
        SELECT id, job_id, contested_pool_id, origin_pool_id,
               verifier_peer_id, status, result, reason,
               created_at, updated_at, verified_at
        FROM federation_disputes
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ${len(params)}
    """

    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    disputes = [
        {
            "id": r["id"],
            "job_id": r["job_id"],
            "contested_pool_id": r["contested_pool_id"],
            "origin_pool_id": r["origin_pool_id"],
            "verifier_peer_id": r["verifier_peer_id"],
            "status": r["status"],
            "result": r["result"],
            "reason": r["reason"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None,
        }
        for r in rows
    ]
    return {
        "scaffold": True,
        "authoritative": False,
        "mode": "dry_run",
        "count": len(disputes),
        "limit": limit,
        "disputes": disputes,
    }
