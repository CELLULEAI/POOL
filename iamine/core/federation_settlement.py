"""M10-scaffold — Settlement tracking structure WITHOUT economic activation.

# FORMULA ASSUMPTION (scaffold — revisit in M10-active):
#   Net settlement between two pools = bilateral net delta of
#   (credits_worker + credits_exec + credits_origin) over the period.
#
#   Treasury share (credits_treasury, 10% bp 1000) is INTENTIONALLY
#   EXCLUDED from aggregate_period. The treasury is a multilateral
#   pool-independent account whose governance is NOT decided in scaffold.
#   Revisit in M10-active after David tranches:
#     - treasury governance (multisig? DAO? entité légale?)
#     - $IAMINE anchor (scrip / usage-backed / fiat)
#     - multilateral clearing vs bilateral
#     - anti-dumping minimum rate
#     - slashing policy (auto / progressive)
#     - dispute resolution
#
#   Alternatives NOT chosen here:
#   - Multilateral clearing via treasury as netting hub
#   - Per-worker settlement (requires worker Ed25519 = M7-worker)
#   - Merkle-root commit for gossip replication (= M11.2)

Validated by molecule-guardian 2026-04-10 (C2 obligatoire : FORMULA ASSUMPTION
header + treasury exclusion + pending_worker_attribution filter).

Triple flag :
- SETTLEMENT_ENABLED=false default (kill top-level)
- SETTLEMENT_MODE=dry_run|active default dry_run (dry_run writes rows with
  status='proposed', NEVER 'settled', NEVER transfers credits)
- SETTLEMENT_PERIOD_SEC=86400 default (1 day period)

Kill switch FS /etc/iamine/fed_disable has highest priority: loop no-op
immediately without even reading flags.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from . import federation as fed

log = logging.getLogger("iamine.settlement")


def is_settlement_enabled() -> bool:
    return os.environ.get("SETTLEMENT_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def get_settlement_mode() -> str:
    """Return 'dry_run' or 'active'. Default dry_run (scaffold)."""
    m = os.environ.get("SETTLEMENT_MODE", "dry_run").strip().lower()
    return m if m in ("dry_run", "active") else "dry_run"


def get_period_sec() -> int:
    try:
        return int(os.environ.get("SETTLEMENT_PERIOD_SEC", "86400"))
    except ValueError:
        return 86400


# ---- Aggregate period (core formula, scaffold) ----

async def aggregate_period(
    pool,
    peer_atom_id: str,
    period_start: datetime,
    period_end: datetime,
) -> dict:
    """Compute net bilateral settlement between self and one peer.

    Returns dict with:
      - peer_atom_id, period_start, period_end
      - self_owes_peer_credits : what we owe peer (jobs they forwarded to us)
      - peer_owes_self_credits : what peer owes us (jobs we forwarded to them)
      - net_credits : positive = peer owes us, negative = we owe peer
      - settlable_rows, pending_rows : count of rows included / excluded
      - treasury_excluded : bool = always true (scaffold invariant)

    TREASURY EXCLUSION : `credits_treasury` is NEVER summed here. See header.
    PENDING EXCLUSION  : rows where `pending_worker_attribution = true` are excluded.
    """
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return {"error": "no DB store", "treasury_excluded": True}

    self_atom_id = pool.federation_self.atom_id if pool.federation_self else None
    if not self_atom_id:
        return {"error": "self identity missing", "treasury_excluded": True}

    if peer_atom_id == self_atom_id:
        return {"error": "cannot settle with self", "treasury_excluded": True}

    async with pool.store.pool.acquire() as conn:
        # Part A : what peer owes us — we executed jobs originated by peer
        row_a = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(credits_worker + credits_exec + credits_origin), 0) AS total,
                COUNT(*) AS n
            FROM revenue_ledger
            WHERE exec_pool_id = $1
              AND origin_pool_id = $2
              AND pending_worker_attribution = false
              AND created_at >= $3
              AND created_at < $4
            """,
            self_atom_id, peer_atom_id, period_start, period_end,
        )

        # Part B : what we owe peer — peer executed jobs we originated
        row_b = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(credits_worker + credits_exec + credits_origin), 0) AS total,
                COUNT(*) AS n
            FROM revenue_ledger
            WHERE exec_pool_id = $2
              AND origin_pool_id = $1
              AND pending_worker_attribution = false
              AND created_at >= $3
              AND created_at < $4
            """,
            self_atom_id, peer_atom_id, period_start, period_end,
        )

        # Count of pending rows in the period (not included, for reporting)
        row_pending = await conn.fetchrow(
            """
            SELECT COUNT(*) AS n
            FROM revenue_ledger
            WHERE (
                (exec_pool_id = $1 AND origin_pool_id = $2)
                OR (exec_pool_id = $2 AND origin_pool_id = $1)
            )
            AND pending_worker_attribution = true
            AND created_at >= $3
            AND created_at < $4
            """,
            self_atom_id, peer_atom_id, period_start, period_end,
        )

    peer_owes_self = int(row_a["total"] or 0)
    self_owes_peer = int(row_b["total"] or 0)
    net = peer_owes_self - self_owes_peer  # + : peer owes us, - : we owe peer
    settlable_rows = int(row_a["n"] or 0) + int(row_b["n"] or 0)
    pending_rows = int(row_pending["n"] or 0)

    # Q3 consumer (scaffold) : expose burns applied against this peer during
    # the period as metadata ONLY. The formula above is UNCHANGED. The burns
    # are NOT deducted from net_credits. Invariant C2 (formula stable) is
    # preserved. A future chunk with token-guardian + molecule-guardian
    # invocation can propose how to integrate burns into settlement flow.
    from . import slashing as _slashing
    try:
        _burn = await _slashing.get_burn_total(
            pool, peer_atom_id, since_ts=period_start.isoformat()
        )
        burns_applied_peer = int(_burn.get("total_burned", 0) or 0)
        burns_event_count_peer = int(_burn.get("event_count", 0) or 0)
    except Exception as _e:
        log.warning(f"aggregate_period: burns readout failed: {_e}")
        burns_applied_peer = 0
        burns_event_count_peer = 0

    return {
        "peer_atom_id": peer_atom_id,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "self_owes_peer_credits": self_owes_peer,
        "peer_owes_self_credits": peer_owes_self,
        "net_credits": net,
        "settlable_rows": settlable_rows,
        "pending_rows": pending_rows,
        "treasury_excluded": True,  # scaffold invariant
        "formula": "bilateral_net_delta (worker+exec+origin, treasury excluded)",
        "burns_meta": {
            "total": burns_applied_peer,
            "count": burns_event_count_peer,
            "note": "scaffold metadata only — NOT deducted from net_credits (invariant C2 stable)",
            "source": "slashing_events table, period_start onward",
        },
    }


# ---- Propose settlement (dry_run writes status='proposed', never 'settled') ----

async def propose_settlement(
    pool,
    peer_atom_id: str,
    period_start: datetime,
    period_end: datetime,
) -> dict:
    """Create a federation_settlements row with status='proposed'.

    NEVER writes status='settled' in scaffold. NEVER transfers credits.
    Returns the row as dict + the aggregation details.
    """
    if fed.is_fed_disabled_by_fs():
        return {"error": "federation disabled by kill switch"}
    if not is_settlement_enabled():
        return {"error": "SETTLEMENT_ENABLED=false"}

    agg = await aggregate_period(pool, peer_atom_id, period_start, period_end)
    if "error" in agg:
        return agg

    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return {"error": "no DB store"}

    # Peer must exist and be bonded >=2 (scaffold = no trust 3 bonded yet)
    peer = await fed.load_peer(pool, peer_atom_id)
    if not peer:
        return {"error": "unknown peer"}
    if peer.get("trust_level", 0) < 2:
        return {"error": "peer not trusted >=2"}

    mode = get_settlement_mode()
    import json as _json
    proof = {
        "mode": mode,
        "formula": agg["formula"],
        "treasury_excluded": True,
        "authoritative": False,  # scaffold
        "settlable_rows": agg["settlable_rows"],
        "pending_rows": agg["pending_rows"],
        "self_owes_peer": agg["self_owes_peer_credits"],
        "peer_owes_self": agg["peer_owes_self_credits"],
    }

    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO federation_settlements
                (peer_id, period_start, period_end, net_credits, status, proof, proposed_at)
            VALUES ($1, $2, $3, $4, 'proposed', $5::jsonb, NOW())
            RETURNING id, peer_id, period_start, period_end, net_credits, status, proof, proposed_at, created_at
            """,
            peer_atom_id, period_start, period_end, agg["net_credits"], _json.dumps(proof),
        )

    log.info(
        f"settlement proposed (scaffold): peer={peer_atom_id[:16]}... "
        f"net={agg['net_credits']} settlable={agg['settlable_rows']} "
        f"pending={agg['pending_rows']} mode={mode}"
    )
    return {
        "ok": True,
        "scaffold": True,
        "authoritative": False,
        "mode": mode,
        "settlement_id": row["id"],
        "peer_atom_id": peer_atom_id,
        "net_credits": row["net_credits"],
        "aggregation": agg,
    }


# ---- Settlement loop (triple-flag gated, no-op by default) ----

async def settlement_loop(pool) -> None:
    """Background task scanning bonded peers and proposing settlements.

    Disabled by default. Gated by:
    - fed.is_fed_disabled_by_fs()  (kill switch highest priority)
    - is_settlement_enabled()      (top-level SETTLEMENT_ENABLED env)
    - fed.get_mode() != off        (federation must be at least observe)

    Symmetric: any bonded pool can propose, no role privilege.
    """
    if not is_settlement_enabled():
        log.info("settlement: loop disabled (SETTLEMENT_ENABLED=false)")
        return
    if fed.get_mode() == fed.FED_MODE_OFF:
        log.info("settlement: federation off, loop disabled")
        return

    period = get_period_sec()
    mode = get_settlement_mode()
    log.info(f"settlement: loop starting period={period}s mode={mode} (scaffold)")

    while True:
        try:
            if fed.is_fed_disabled_by_fs():
                log.warning("settlement: kill switch active, skip tick")
                await asyncio.sleep(period)
                continue

            peers = await fed.list_peers(pool, include_revoked=False)
            bonded = [p for p in peers if p.get("trust_level", 0) >= 2]

            now = datetime.utcnow()
            period_start = now - timedelta(seconds=period)
            period_end = now

            for peer in bonded:
                aid = peer["atom_id"]
                try:
                    result = await propose_settlement(pool, aid, period_start, period_end)
                    if result.get("ok"):
                        log.info(
                            f"settlement tick: peer={peer['name']!r} "
                            f"net={result.get('net_credits')} "
                            f"rows_settlable={result['aggregation']['settlable_rows']} "
                            f"rows_pending={result['aggregation']['pending_rows']}"
                        )
                except Exception as e:
                    log.warning(f"settlement tick for {aid[:16]}... failed: {e}")
        except Exception as e:
            log.error(f"settlement loop iteration error: {e}", exc_info=True)

        await asyncio.sleep(period)


# ---- State queries for admin UI ----

async def get_settlement_state(pool, limit: int = 50) -> dict:
    """Return recent settlement proposals for the admin dashboard."""
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return {
            "mode": "dry_run",
            "authoritative": False,
            "scaffold": True,
            "enabled": is_settlement_enabled(),
            "rows": [],
        }
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, peer_id, period_start, period_end, net_credits, status,
                   proof, proposed_at, settled_at, created_at
            FROM federation_settlements
            ORDER BY id DESC
            LIMIT $1
            """,
            limit,
        )
    import json as _json
    out = []
    for r in rows:
        proof = r["proof"]
        if isinstance(proof, str):
            try:
                proof = _json.loads(proof)
            except Exception:
                proof = {}
        out.append({
            "id": r["id"],
            "peer_id": r["peer_id"],
            "period_start": r["period_start"].isoformat() if r["period_start"] else None,
            "period_end": r["period_end"].isoformat() if r["period_end"] else None,
            "net_credits": r["net_credits"],
            "status": r["status"],
            "proof": proof,
            "proposed_at": r["proposed_at"].isoformat() if r["proposed_at"] else None,
            "settled_at": r["settled_at"].isoformat() if r["settled_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return {
        "mode": get_settlement_mode(),
        "authoritative": False,
        "scaffold": True,
        "enabled": is_settlement_enabled(),
        "period_sec": get_period_sec(),
        "kill_switch": fed.is_fed_disabled_by_fs(),
        "rows": out,
        "count": len(out),
    }
