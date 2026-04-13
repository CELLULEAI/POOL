"""Molecule v2 — revenue ledger writer (M3 no-op, real in M7).

Pool-first 60/20/10/10 split:
    worker   60%
    exec     20% (30% if origin == exec)
    origin   10% (0% if origin == exec)
    treasury 10%

M3 : module loaded, write_ledger_entry() returns early if IAMINE_FED=off.
M7 : hot-path calls write_ledger_entry() on every completed inference.
M10: settlement loop consumes unsettled rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import federation

log = logging.getLogger("iamine.revenue")


# Split ratios in integer basis points (bp) — sum = 10000.
SPLIT_WORKER_BP = 6000
SPLIT_EXEC_BP = 2000
SPLIT_ORIGIN_BP = 1000
SPLIT_TREASURY_BP = 1000
assert SPLIT_WORKER_BP + SPLIT_EXEC_BP + SPLIT_ORIGIN_BP + SPLIT_TREASURY_BP == 10000


# ---- Q1 D-TREASURY-1 : treasury address getter (migration-ready) ----
#
# Decision 2026-04-11 (david arbitration) :
#   Phase 1 : single-sig david, accepted SPOF for friends-and-family.
#   Phase 2 : asso loi 1901 + multisig 2/3 at N>=20 peers OR external pool ops.
#
# Guardian invariant : treasury address MUST be injected via env var or
# pool_config, NEVER hardcoded, so the migration to multisig is a config flip
# not a redeploy. See project_decisions_a_tranchees.md.
#
# Read order : env var IAMINE_TREASURY_ADDRESS wins, pool_config.treasury_address
# is fallback, None if neither is set (settlement remains scaffold-only).

import os as _os


def get_treasury_address(pool=None) -> str | None:
    """Return the effective treasury address or None if unset.

    Env var wins over pool_config. pool=None is allowed for callers who only
    need the env var path (startup, config diagnostics).
    """
    env = _os.environ.get("IAMINE_TREASURY_ADDRESS", "").strip()
    if env:
        return env
    if pool is not None:
        addr = getattr(pool, "_treasury_address", None)
        if addr:
            return str(addr).strip() or None
    return None



@dataclass
class LedgerEntry:
    job_id: str
    origin_pool_id: str
    exec_pool_id: str
    worker_id: str
    model: str
    tokens_in: int
    tokens_out: int
    credits_total: int
    worker_sig: Optional[bytes] = None
    worker_cert_id: Optional[int] = None
    forward_chain: Optional[list] = None


def split_credits(total: int, origin_pool_id: str, exec_pool_id: str) -> dict:
    """Return dict with credits_{worker,exec,origin,treasury} — integer math."""
    worker = (total * SPLIT_WORKER_BP) // 10000
    treasury = (total * SPLIT_TREASURY_BP) // 10000
    if origin_pool_id == exec_pool_id:
        # exec absorbs the origin share
        exec_share = (total * (SPLIT_EXEC_BP + SPLIT_ORIGIN_BP)) // 10000
        origin_share = 0
    else:
        exec_share = (total * SPLIT_EXEC_BP) // 10000
        origin_share = (total * SPLIT_ORIGIN_BP) // 10000
    return {
        "credits_worker": worker,
        "credits_exec": exec_share,
        "credits_origin": origin_share,
        "credits_treasury": treasury,
    }


async def write_ledger_entry(pool, entry: LedgerEntry) -> None:
    """Persist a row in revenue_ledger. No-op if IAMINE_FED=off."""
    mode = federation.get_mode()
    if mode == federation.FED_MODE_OFF:
        return
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        log.warning("revenue: no PG store, entry dropped")
        return

    split = split_credits(entry.credits_total, entry.origin_pool_id, entry.exec_pool_id)
    chain = entry.forward_chain or []

    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO revenue_ledger (
                job_id, origin_pool_id, exec_pool_id, worker_id, worker_cert_id,
                model, tokens_in, tokens_out,
                credits_total, credits_worker, credits_exec, credits_origin, credits_treasury,
                worker_sig, forward_chain
            )
            VALUES ($1,$2,$3,$4,$5, $6,$7,$8, $9,$10,$11,$12,$13, $14,$15)
            """,
            entry.job_id,
            entry.origin_pool_id,
            entry.exec_pool_id,
            entry.worker_id,
            entry.worker_cert_id,
            entry.model,
            entry.tokens_in,
            entry.tokens_out,
            entry.credits_total,
            split["credits_worker"],
            split["credits_exec"],
            split["credits_origin"],
            split["credits_treasury"],
            entry.worker_sig,
            chain,
        )


# ---- M7a : forward entry helper ----
# Écrit une ligne ledger pour un job forwardé. worker_sig=NULL car M7-worker
# (signing côté worker) est différé M9b. Le settlement M10 DOIT rejeter les
# lignes où worker_sig IS NULL jusqu'au backfill.

async def write_forward_entry(
    pool,
    job_id: str,
    origin_pool_id: str,
    exec_pool_id: str,
    worker_id: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    credits_total: int,
    forward_chain: Optional[list] = None,
) -> None:
    """Write a revenue_ledger row for a forwarded job (M7a pre-worker-signing).

    worker_sig is intentionally NULL — settlement M10 must filter these out
    until M7-worker backfills the signatures.
    """
    mode = federation.get_mode()
    if mode == federation.FED_MODE_OFF:
        return
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        log.warning("revenue: no PG store, forward entry dropped")
        return

    entry = LedgerEntry(
        job_id=job_id,
        origin_pool_id=origin_pool_id,
        exec_pool_id=exec_pool_id,
        worker_id=worker_id,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        credits_total=credits_total,
        worker_sig=None,  # explicit: M7-worker pending
        worker_cert_id=None,
        forward_chain=forward_chain or [],
    )
    await write_ledger_entry(pool, entry)
    # M10-scaffold : column pending_worker_attribution defaults to true (migration 008).
    # M7-worker will UPDATE to false after verifying the worker Ed25519 signature.
    log.info(
        f"ledger+ forward job_id={job_id} origin={origin_pool_id[:8]}... "
        f"exec={exec_pool_id[:8]}... worker={worker_id} "
        f"tokens={tokens_in}/{tokens_out} credits={credits_total} "
        f"[pending_worker_attribution=true M7-worker pending]"
    )
