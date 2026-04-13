"""M10-active slashing scaffold — BURN of confiscated credits.

Implements decision Q3 (D-SLASHING) arbitrated 2026-04-11 :
    (e) progressive + economic slashing WITH BURN of confiscated credits.

Scope phase 1 (this module) :
- Append-only slashing_events table, admin-triggered burns only.
- No automatic cross-peer slashing.
- No propagation of burns across pools (RF=1, M11.2 gossip will add that).
- Decoupled from SETTLEMENT_ENABLED via its own SLASHING_ENABLED env flag.

SCAFFOLD INVARIANTS (molecule-guardian validated 2026-04-11) :

    SLASHING SCAFFOLD — slashing_events intentionally EXCLUDED from ledger
    merkle root v1 (M11-scaffold invariant 2, format frozen).
    Cross-peer auditability of burns = future scope (M11-active or later).
    Any extension requires LEDGER_MERKLE_VERSION bump — do not silently include.

    SCAFFOLD RF=1 — burn events are local to this pool. Losing the pool
    loses its burn history. M11.2 will add replication.

    NO worker_cert_id FK — if a specific cert matters, encode it inside
    the reason field as structured text.

Decision sources :
- project_decisions_a_tranchees.md (Q3 BURN retained, triangle Q1+Q3+Q6 resolved)
- project_m11_scaffold_invariants.md (invariant 2: frozen merkle canonical form)
- project_m10_scaffold_invariants.md (append-only, pending_worker_attribution)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from . import federation

log = logging.getLogger("iamine.slashing")


def is_slashing_enabled() -> bool:
    """Kill switch independent of SETTLEMENT_ENABLED."""
    return os.environ.get("SLASHING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


async def burn_credits(
    pool,
    peer_id: str,
    amount: int,
    reason: str,
    job_id: Optional[str] = None,
) -> dict:
    """Append a burn event to slashing_events.

    Returns dict with status, or error info. Never raises on disabled flag.
    If SLASHING_ENABLED=false, logs WARNING and returns skipped=true so that
    test harnesses can observe the no-op cleanly.

    Invariants:
    - amount > 0 (enforced by DB CHECK constraint)
    - peer_id corresponds to federation_peers.atom_id / signed envelope peer id
    - job_id is free TEXT (no FK to workers_certs per guardian invariant 4)
    """
    if amount <= 0:
        return {"error": "amount must be positive", "peer_id": peer_id, "amount": amount}
    if not peer_id:
        return {"error": "peer_id is required"}
    if not reason:
        return {"error": "reason is required"}

    if not is_slashing_enabled():
        log.warning(
            "slashing: burn skipped (SLASHING_ENABLED=false) "
            f"peer_id={peer_id} amount={amount} reason={reason!r} job_id={job_id}"
        )
        return {
            "status": "skipped",
            "reason_skipped": "SLASHING_ENABLED=false",
            "peer_id": peer_id,
            "amount": amount,
        }

    if not (hasattr(pool.store, "pool") and pool.store.pool):
        log.warning("slashing: no PG store, burn dropped")
        return {"error": "no DB store", "peer_id": peer_id}

    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO slashing_events (peer_id, job_id, amount, reason)
            VALUES ($1, $2, $3, $4)
            RETURNING id, created_at
            """,
            peer_id, job_id, amount, reason,
        )

    log.info(
        f"slashing: BURN id={row['id']} peer_id={peer_id} amount={amount} "
        f"reason={reason!r} job_id={job_id}"
    )
    return {
        "status": "burned",
        "id": row["id"],
        "peer_id": peer_id,
        "amount": amount,
        "reason": reason,
        "job_id": job_id,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def get_burn_total(pool, peer_id: str, since_ts=None) -> dict:
    """Aggregate total burned amount for a peer (future aggregate_period input).

    Accepts `since_ts` as either a datetime object or an ISO-8601 string.
    Strings are parsed via datetime.fromisoformat() ; invalid strings fall
    back to None (no filter) with a warning log.

    Accepting Union[str, datetime] is required because :
    - HTTP route passes a query param (string)
    - aggregate_period passes a datetime object directly
    - asyncpg's $2::TIMESTAMP cast refuses strings (prepared statement type
      resolution happens before the cast)

    Returns {peer_id, total_burned, event_count, since}.
    Read-only, does not trigger any write even if SLASHING_ENABLED=false.
    """
    from datetime import datetime as _dt

    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"error": "no DB store", "peer_id": peer_id}

    # Normalize since_ts to datetime or None
    since_dt = None
    if since_ts is not None:
        if isinstance(since_ts, _dt):
            since_dt = since_ts
        elif isinstance(since_ts, str):
            try:
                since_dt = _dt.fromisoformat(since_ts)
            except ValueError:
                log.warning(f"slashing: bad since_ts iso string, ignoring: {since_ts!r}")
                since_dt = None

    async with pool.store.pool.acquire() as conn:
        if since_dt is not None:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount), 0)::BIGINT AS total, COUNT(*)::BIGINT AS n
                FROM slashing_events
                WHERE peer_id = $1 AND created_at >= $2
                """,
                peer_id, since_dt,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount), 0)::BIGINT AS total, COUNT(*)::BIGINT AS n
                FROM slashing_events
                WHERE peer_id = $1
                """,
                peer_id,
            )

    return {
        "peer_id": peer_id,
        "total_burned": int(row["total"] or 0),
        "event_count": int(row["n"] or 0),
        "since": since_dt.isoformat() if since_dt else None,
    }


async def revoke_and_burn(
    pool,
    peer_id: str,
    amount: int,
    reason: str,
) -> dict:
    """Combined action for Q3 (e) progressive + economic slashing.

    Soft-revokes the peer in federation_peers AND burns credits in one call.
    Either step can be skipped via its own kill switch; both are logged.
    Returns a summary dict with both sub-results.
    """
    burn_result = await burn_credits(pool, peer_id, amount, reason, job_id=None)

    revoke_result = {"revoked": False, "reason_skipped": None}
    if not federation.is_federation_enabled() if hasattr(federation, 'is_federation_enabled') else False:
        revoke_result["reason_skipped"] = "federation disabled"
    else:
        try:
            if hasattr(federation, "revoke_peer"):
                await federation.revoke_peer(pool, peer_id, reason=reason)
                revoke_result["revoked"] = True
            else:
                revoke_result["reason_skipped"] = "federation.revoke_peer not available"
        except Exception as e:
            log.error(f"slashing: revoke step failed peer_id={peer_id}: {e}")
            revoke_result["error"] = str(e)

    return {
        "peer_id": peer_id,
        "amount": amount,
        "reason": reason,
        "burn": burn_result,
        "revoke": revoke_result,
    }
