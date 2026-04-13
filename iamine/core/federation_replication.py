"""M11 replication scaffold — gossip + merkle diff + account creation quorum.

Implements the 6 decisions B-RAID arbitrated 2026-04-11:

- Q1 D-RAID-STRATEGY : (b) gossip eventually consistent + (d) merkle diff
  reconciliation. The merkle primitive is already live in core/federation_merkle.py
  (commit 8d779c0). This module adds the write half (sync loop, ingest endpoint)
  activated in M11.2.

- Q2 D-RAID-QUORUM : (a) quorum majoritaire SCOPED ACCOUNT CREATION ONLY.
  Ledger writes and inference stay AP. Account creation has CP semantics
  during partition. account_creation_quorum_precheck() is the gate helper
  that M11.1 will wire into /v1/auth/register.

- Q3 D-RAID-REBUILD : (b) merkle sync incremental + cross-verification
  >=2 bonded peers before marking COMPLETE. verify_rebuild_complete() is
  the skeleton activated in M11.3.

- Q4 Consistency level : FIXED floor(N/2)+1 via _quorum_size() helper.
- Q5 Partition detection : 60s (2 heartbeat cycles) via PARTITION_DETECTION_SEC.
- Q6 Minimum molecule size : N >= 3 via MOLECULE_MIN_PEERS_FOR_QUORUM.

Phase 1 scaffold scope :
- Flag readers work (REPLICATION_ENABLED, ACCOUNT_CREATION_QUORUM_ENABLED)
- Helpers compute live state (bonded_peers_reachable, molecule_size, etc.)
- Skeleton functions (pull_ledger_from_peer, verify_rebuild_complete,
  account_creation_quorum_precheck) return scaffold markers
- No background loops active
- No automatic enqueue
- /v1/federation/ledger/ingest endpoint returns 501 if disabled

M11.1 wires account_creation_quorum_precheck into /v1/auth/register.
M11.2 activates the ledger gossip sync loop.
M11.3 activates the rebuild loop with cross-verification.
M11.4 hardens split-brain detection.
M11.5 extends to conversations + RAG (separate session).

Invariants preserved (molecule-guardian validated 2026-04-11) :
- No master pool : gossip symmetric, quorum majority dynamic, rebuild pull-from-any
- Federation Ed25519 : ingest requires signed envelope + trust>=3 (M5 lock = double protection)
- Append-only ledger : ingest is INSERT only, never UPDATE
- Merkle v1 frozen : replication uses existing endpoints without touching canonical form
- Tolerance panne : inference always AP, only account creation CP during partition
- Atomes heterogenes : merkle sync is hardware-agnostic
- Split 60/20/10/10 : propagated verbatim in canonical form, tamper breaks root
- Worker statelessness : replication operates on DB tables
- N open : no hardcoded MAX_POOLS, chaque pool parle a ses bonded voisins
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from . import federation

log = logging.getLogger("iamine.replication")


# ---- Constants (from D-RAID decisions 2026-04-11) ----

MOLECULE_MIN_PEERS_FOR_QUORUM = 3       # Q6 : N >= 3 (RAID analogue per david)
PARTITION_DETECTION_SEC = 60             # Q5 : 2 heartbeat cycles (M7b = 30s each)

# M11.2 gossip cycle interval. Separate from PARTITION_DETECTION_SEC
# because anti-entropy frequency (data freshness) is a different concern
# from partition detection (reachability timeout). Configurable via
# REPLICATION_GOSSIP_INTERVAL_SEC env, default 60s.
REPLICATION_GOSSIP_INTERVAL_SEC = int(os.environ.get("REPLICATION_GOSSIP_INTERVAL_SEC", "60"))
# Q4 : QUORUM_FORMULA is floor(N/2)+1, computed inline by _quorum_size(n)


# ---- Env flag readers ----

def is_replication_enabled() -> bool:
    """Top-level kill switch for the M11 replication subsystem.

    Independent of SETTLEMENT_ENABLED, SLASHING_ENABLED, DISPUTE_SAMPLING_ENABLED.
    Default false (scaffold). M11.2 flips this in production after 2nd pool
    bonds (Gladiator .30 NAT, see project_todo_gladiator_pool2.md).
    """
    return os.environ.get("REPLICATION_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )


def is_account_creation_quorum_enabled() -> bool:
    """Kill switch for the Q2 account creation quorum check.

    Separate from REPLICATION_ENABLED so an admin can observe molecule size
    and reachability without activating the CP gate. Default false.
    M11.1 flips this in production after register path is wired.
    """
    return os.environ.get("ACCOUNT_CREATION_QUORUM_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _fed_disabled_by_kill_switch() -> bool:
    """Honor /etc/iamine/fed_disable (guardian rec #5 pattern)."""
    try:
        return federation.is_fed_disabled_by_fs()
    except Exception:
        return False


# ---- Quorum formula ----

def _quorum_size(n: int) -> int:
    """Q4 D-RAID-CONSISTENCY : floor(N/2)+1 fixed quorum formula.

    Examples :
        n=1 -> 1 (trivial, single pool)
        n=2 -> 2 (unanimous, perdre 1 peer = block)
        n=3 -> 2 (tolerate 1 loss, RAID analogue)
        n=5 -> 3 (tolerate 2 losses)

    For n < MOLECULE_MIN_PEERS_FOR_QUORUM (=3), the quorum is NOT activated
    at all — see is_molecule_quorum_active() below. This function is a pure
    mathematical helper.
    """
    if n <= 0:
        return 0
    return (n // 2) + 1


# ---- Bonded peer reachability ----

async def bonded_peers_reachable(pool) -> list[str]:
    """Return atom_ids of bonded peers with last_seen within PARTITION_DETECTION_SEC.

    Read-only. Used by account_creation_quorum_precheck and the future
    replication sync loops. A peer with stale last_seen is treated as
    unreachable for quorum purposes, even if federation_peers.revoked_at
    is NULL.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return []
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT atom_id FROM federation_peers
            WHERE revoked_at IS NULL
              AND last_seen IS NOT NULL
              AND last_seen >= (now() - interval '{PARTITION_DETECTION_SEC} seconds')
            ORDER BY atom_id
            """
        )
    return [r["atom_id"] for r in rows]


async def molecule_size(pool) -> int:
    """Total count of bonded peers (revoked_at IS NULL), regardless of reachability.

    This is the N in floor(N/2)+1. Unreachable peers are still counted
    because they're part of the molecule and may come back online.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return 0
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*)::BIGINT AS n FROM federation_peers WHERE revoked_at IS NULL"
        )
    return int(row["n"] or 0)


async def is_molecule_quorum_active(pool) -> bool:
    """Q6 check : molecule has at least MOLECULE_MIN_PEERS_FOR_QUORUM bonded peers.

    Below the threshold (N < 3), quorum is NOT activated at all — single-pool
    mode, no replication CP semantics. Above, quorum is active.
    """
    n = await molecule_size(pool)
    return n >= MOLECULE_MIN_PEERS_FOR_QUORUM


async def account_creation_quorum_precheck(pool) -> dict:
    """Q2 scaffold gate : snapshot state of the molecule for account creation.

    **Phase 1 scaffold — returns data only, never blocks. M11.1 will wire
    `blocked=True` as a gate in /v1/auth/register. This is an INTENTIONAL
    CP constraint from decision Q2 D-RAID-QUORUM (2026-04-11), NOT a
    violation of the AP doctrine which applies only to inference serving.**

    Returns dict with:
        - active : bool (quorum activated for this molecule size)
        - reachable_count : int (bonded peers with fresh last_seen)
        - total_molecule_size : int (all bonded, regardless of reachability)
        - required : int (floor(N/2)+1)
        - blocked : bool (would M11.1 block a new account creation NOW?)
        - phase : 1 for scaffold, 2 for active
        - scaffold : bool (always True in phase 1)
    """
    n_total = await molecule_size(pool)
    reachable = await bonded_peers_reachable(pool)
    n_reachable = len(reachable)
    active = n_total >= MOLECULE_MIN_PEERS_FOR_QUORUM
    required = _quorum_size(n_total)

    # In phase 2, blocked = active AND n_reachable < required.
    # In phase 1 scaffold, we compute the same logic but NEVER actually block.
    would_block = active and n_reachable < required

    return {
        "active": active,
        "reachable_count": n_reachable,
        "total_molecule_size": n_total,
        "required": required,
        "blocked": False,  # phase 1 NEVER blocks
        "would_block_in_phase_2": would_block,
        "phase": 1,
        "scaffold": True,
        "min_peers_threshold": MOLECULE_MIN_PEERS_FOR_QUORUM,
        "partition_detection_sec": PARTITION_DETECTION_SEC,
    }


# ---- M11.1 account replication helpers ----

async def _fetch_bonded_peers_with_trust(pool, min_trust: int = 2) -> list:
    """Return list of peer dicts (atom_id, url, pubkey_bytes) for bonded
    peers with trust_level >= min_trust and reachable last_seen.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return []
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT atom_id, url, pubkey
            FROM federation_peers
            WHERE revoked_at IS NULL
              AND trust_level >= $1
              AND last_seen IS NOT NULL
              AND last_seen >= (now() - interval '{PARTITION_DETECTION_SEC} seconds')
            ORDER BY atom_id
            """,
            min_trust,
        )
    return [dict(r) for r in rows]


async def replicate_account_to_peers(pool, account_row: dict, min_trust: int = 2) -> dict:
    try:
        return await _replicate_account_to_peers_impl(pool, account_row, min_trust)
    except Exception as _e:
        log.error(f"M11.1 replicate_account_to_peers EXCEPTION: {type(_e).__name__}: {_e}")
        import traceback as _tb
        log.error(_tb.format_exc()[:2000])
        return {"pushed": 0, "acked": 0, "failed": 0, "peer_acks": [], "error": str(_e)}


async def _replicate_account_to_peers_impl(pool, account_row: dict, min_trust: int = 2) -> dict:
    """Push an account to all bonded reachable peers via signed ingest.

    **Identity-only replication** (molecule-guardian 2026-04-11 hard rec):
    credit columns (total_credits, total_earned, total_spent) are
    EXCLUDED from the payload — they follow the ledger gossip path (M11.2).
    Ingesting a stale credit snapshot could silently overwrite a live
    balance.

    Fire-and-forget. Callers MUST wrap this in asyncio.create_task to
    preserve sub-second register latency (guardian required change #3).

    Returns dict with:
        - pushed : number of peers attempted
        - acked : number of peers that returned 200
        - failed : number that failed/timeout
        - peer_acks : list of {atom_id, status, duration_ms}

    Quorum semantics (N = bonded_peer_count + 1, self=1):
    required = floor(N/2)+1. Caller checks (acked + 1) >= required.
    """
    import aiohttp as _aiohttp
    import json as _json
    import time as _time

    if pool.federation_self is None:
        return {"pushed": 0, "acked": 0, "failed": 0, "peer_acks": [], "self_atom_id": None}

    peers = await _fetch_bonded_peers_with_trust(pool, min_trust=min_trust)
    if not peers:
        return {"pushed": 0, "acked": 0, "failed": 0, "peer_acks": [], "self_atom_id": pool.federation_self.atom_id}

    # Identity-only payload (EXCLUDE total_credits/total_earned/total_spent per guardian)
    payload = {
        "account_id": account_row.get("account_id"),
        "email": account_row.get("email"),
        "password_hash": account_row.get("password_hash"),
        "display_name": account_row.get("display_name"),
        "pseudo": account_row.get("pseudo"),
        "eth_address": account_row.get("eth_address"),
        "account_token": account_row.get("account_token"),
        "memory_enabled": bool(account_row.get("memory_enabled", False)),
        "created": account_row.get("created"),
        "origin_pool_id": pool.federation_self.atom_id,
        "schema_version": 1,
    }
    body = _json.dumps(payload, default=str).encode()

    # Load self privkey for signing
    from .federation import _load_privkey_from_disk, build_envelope_headers
    from pathlib import Path
    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return {"pushed": 0, "acked": 0, "failed": len(peers), "peer_acks": [], "self_atom_id": pool.federation_self.atom_id}

    async def _push_one(peer: dict) -> dict:
        t0 = _time.time()
        try:
            base = peer["url"].rstrip("/")
            headers = build_envelope_headers(
                priv_raw,
                pool.federation_self.atom_id,
                "POST",
                "/v1/federation/accounts/ingest",
                body,
                hop=0,
                chain=[],
            )
            headers["Content-Type"] = "application/json"
            timeout = _aiohttp.ClientTimeout(total=5.0)
            async with _aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{base}/v1/federation/accounts/ingest", data=body, headers=headers) as r:
                    dur_ms = int((_time.time() - t0) * 1000)
                    if r.status == 200:
                        return {"atom_id": peer["atom_id"], "status": "ack", "duration_ms": dur_ms}
                    else:
                        return {"atom_id": peer["atom_id"], "status": f"http_{r.status}", "duration_ms": dur_ms}
        except Exception as e:
            dur_ms = int((_time.time() - t0) * 1000)
            return {"atom_id": peer["atom_id"], "status": f"error: {str(e)[:100]}", "duration_ms": dur_ms}

    import asyncio as _asyncio
    results = await _asyncio.gather(*[_push_one(p) for p in peers], return_exceptions=False)

    acked = sum(1 for r in results if r["status"] == "ack")
    failed = len(results) - acked

    # Log to account_replication_log (best-effort, don't block)
    try:
        async with pool.store.pool.acquire() as conn:
            for r in results:
                status_col = "ack" if r["status"] == "ack" else "failed"
                err_col = None if r["status"] == "ack" else r["status"]
                await conn.execute(
                    """
                    INSERT INTO account_replication_log
                        (account_id, peer_atom_id, direction, status, error_message)
                    VALUES ($1, $2, 'push', $3, $4)
                    """,
                    payload["account_id"],
                    r["atom_id"],
                    status_col,
                    err_col,
                )
    except Exception as e:
        log.warning(f"account_replication_log insert failed: {e}")

    return {
        "pushed": len(results),
        "acked": acked,
        "failed": failed,
        "peer_acks": results,
        "self_atom_id": pool.federation_self.atom_id,
    }


async def log_account_recv_ack(pool, account_id: str, from_atom_id: str, status: str = "ack") -> None:
    """Record a received account ingest ACK for M11.3 rebuild targeting."""
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO account_replication_log
                    (account_id, peer_atom_id, direction, status)
                VALUES ($1, $2, 'recv', $3)
                """,
                account_id, from_atom_id, status,
            )
    except Exception as e:
        log.warning(f"log_account_recv_ack insert failed: {e}")


# ---- Replication state helpers ----

async def update_replication_state(pool, **kwargs) -> None:
    """UPSERT replication_state row keyed by pool.federation_self.atom_id.

    Creates the row on first call, updates existing on subsequent. The
    updated_at column is set to now() automatically. Any of the mutable
    columns can be updated via kwargs :
        - rebuild_status
        - last_synced_period
        - last_merkle_root_seen
        - last_replication_loop
        - molecule_size
        - quorum_active
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return
    ident = getattr(pool, "federation_self", None)
    if not ident:
        return

    valid_keys = {
        "rebuild_status", "last_synced_period", "last_merkle_root_seen",
        "last_replication_loop", "molecule_size", "quorum_active",
    }
    updates = {k: v for k, v in kwargs.items() if k in valid_keys}

    set_clauses = ["updated_at = now()"]
    args = [ident.atom_id]
    for k, v in updates.items():
        args.append(v)
        set_clauses.append(f"{k} = ${len(args)}")

    cols = ["self_atom_id"] + list(updates.keys())
    placeholders = [f"${i+1}" for i in range(len(cols))]

    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO replication_state ({', '.join(cols)})
            VALUES ({', '.join(placeholders)})
            ON CONFLICT (self_atom_id) DO UPDATE SET {', '.join(set_clauses)}
            """,
            *args,
        )


async def get_replication_state(pool) -> dict:
    """Read the current replication_state row for self. Returns dict or empty."""
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {}
    ident = getattr(pool, "federation_self", None)
    if not ident:
        return {}
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM replication_state WHERE self_atom_id = $1",
            ident.atom_id,
        )
    if not row:
        return {
            "self_atom_id": ident.atom_id,
            "rebuild_status": "idle",
            "last_synced_period": None,
            "last_merkle_root_seen": None,
            "last_replication_loop": None,
            "molecule_size": None,
            "quorum_active": False,
            "scaffold": True,
            "note": "no state row yet",
        }
    return {
        "self_atom_id": row["self_atom_id"],
        "rebuild_status": row["rebuild_status"],
        "last_synced_period": row["last_synced_period"].isoformat() if row["last_synced_period"] else None,
        "last_merkle_root_seen": row["last_merkle_root_seen"],
        "last_replication_loop": row["last_replication_loop"].isoformat() if row["last_replication_loop"] else None,
        "molecule_size": row["molecule_size"],
        "quorum_active": row["quorum_active"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "scaffold": True,
    }


# ---- Sync queue helpers ----

async def enqueue_sync_work(
    pool,
    peer_atom_id: str,
    table_name: str,
    row_id: Optional[int] = None,
    direction: str = "pull",
) -> Optional[int]:
    """Insert a replication_queue row. Returns queue_id or None.

    NOTE : caller is responsible for checking `is_fed_disabled_by_fs()` and
    `is_replication_enabled()` BEFORE calling — this function does NOT
    short-circuit on either of those. The rationale is that M11.2 will
    activate the worker loop and at that point, any row enqueued while
    the kill switch was active would be drained blindly. Keep the enqueue
    decision at the call site.

    Validates direction ('push' or 'pull') and non-empty args. Returns None
    on validation failure.
    """
    if direction not in ("push", "pull"):
        log.warning(f"enqueue_sync_work: invalid direction {direction!r}")
        return None
    if not peer_atom_id or not table_name:
        return None
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return None

    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO replication_queue
                (peer_atom_id, table_name, row_id, direction, status)
            VALUES ($1, $2, $3, $4, 'pending')
            RETURNING id
            """,
            peer_atom_id, table_name, row_id, direction,
        )
    return int(row["id"]) if row else None


async def get_queue_stats(pool) -> dict:
    """Read-only queue status breakdown."""
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"pending": 0, "in_progress": 0, "done": 0, "failed": 0, "total": 0}
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT status, COUNT(*)::BIGINT AS n
            FROM replication_queue
            GROUP BY status
            """
        )
    out = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
    for r in rows:
        out[r["status"]] = int(r["n"])
    out["total"] = sum(out.values())
    return out


# ---- Rebuild skeleton (M11.3) ----

async def pull_ledger_from_peer(
    pool,
    peer_atom_id: str,
    since_period=None,
    until_period=None,
) -> dict:
    """Pull a ledger range from a bonded peer (M11.3 active).

    Reuses the same signed /sync-pull flow as the gossip loop but targets
    a specific peer on admin demand (rebuild trigger). Unlike the gossip
    loop which iterates all bonded peers on a schedule, this helper
    synchronously pulls from one peer and returns the result directly.

    Order of verification (same as _gossip_pull_one_peer):
        1. Fetch peer row (url, pubkey, trust_level) from federation_peers
        2. GET peer.url/v1/federation/ledger/sync-pull
        3. Verify X-IAMINE-Signature via peer pubkey
        4. Parse ISO created_at -> datetime
        5. verify_ingest_payload merkle recompute
        6. INSERT append-only ON CONFLICT DO NOTHING
    """
    if _fed_disabled_by_kill_switch():
        return {"status": "skipped", "reason": "fed_disable FS kill switch"}
    if not is_replication_enabled():
        return {"status": "skipped", "reason": "REPLICATION_ENABLED=false"}
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"status": "error", "reason": "no_db"}

    async with pool.store.pool.acquire() as conn:
        peer_row = await conn.fetchrow(
            "SELECT atom_id, url, pubkey, trust_level FROM federation_peers "
            "WHERE atom_id = $1 AND revoked_at IS NULL",
            peer_atom_id,
        )
    if not peer_row:
        return {"status": "error", "reason": "peer_not_bonded", "peer_atom_id": peer_atom_id}

    peer = {
        "atom_id": peer_row["atom_id"],
        "url": peer_row["url"],
        "pubkey": bytes(peer_row["pubkey"]) if peer_row["pubkey"] else None,
        "trust_level": peer_row["trust_level"],
    }
    if peer["trust_level"] < 2:
        return {"status": "error", "reason": "trust_lt_2", "peer_atom_id": peer_atom_id}

    return await _gossip_pull_one_peer(pool, peer)


async def trigger_rebuild(pool, peer_atom_id: str = None) -> dict:
    """Admin-triggered rebuild (M11.3 phase 1, manual only).

    If peer_atom_id is None, pulls from all bonded reachable peers.
    Otherwise, targets a specific peer. Updates replication_state with
    rebuild_status=in_progress at start, complete/failed at end.

    Returns dict with pulled stats per peer + verdict.
    """
    import datetime as _dt

    await update_replication_state(pool, rebuild_status="in_progress")

    if peer_atom_id:
        result = await pull_ledger_from_peer(pool, peer_atom_id)
        per_peer = [result]
    else:
        peers = await _fetch_bonded_peers_with_trust(pool, min_trust=2)
        if not peers:
            await update_replication_state(pool, rebuild_status="failed")
            return {"status": "failed", "reason": "no_bonded_peers", "peer_results": []}
        per_peer = []
        for p in peers:
            per_peer.append(await _gossip_pull_one_peer(pool, p))

    # Cross-verify: count peers that returned a successful pull or in_sync
    ok_peers = [r for r in per_peer if r.get("status") in ("in_sync", "pulled")]
    match_count = len(ok_peers)
    verdict = "complete" if match_count >= 2 else "failed"

    await update_replication_state(pool, rebuild_status=verdict)

    return {
        "status": verdict,
        "match_count": match_count,
        "peer_results": per_peer,
        "rebuild_ts": _dt.datetime.utcnow().isoformat(),
    }


# ---- M11.3b wallet snapshot SKELETON (token-guardian session pending) ----

async def snapshot_worker_wallet(pool, worker_id: str) -> dict:
    """Strict skeleton — not implemented until token-guardian session defines:
       1. The envelope shape for cross-pool wallet-snapshot exchange
       2. The merge policy on worker return to origin
       3. The anti-double-spend invariant

    Returns not_implemented until M11.3b activation.
    """
    return {
        "status": "not_implemented",
        "scaffold": True,
        "reason": "token-guardian session required for envelope shape + merge policy",
        "worker_id": worker_id,
    }


async def verify_rebuild_complete(pool, expected_roots: dict) -> dict:
    """Cross-verify local merkle root against >=2 bonded peers.

    **SKELETON for M11-scaffold** — computes the local root via existing
    federation_merkle.compute_ledger_merkle_root and compares to the dict
    of expected roots (peer_id -> root hex). Returns match count. M11.3
    activates the full rebuild gate : rebuild is only marked COMPLETE
    when >=2 peers agree with the local root.

    Phase 1 : returns the comparison without marking rebuild_status.
    """
    from . import federation_merkle
    local = await federation_merkle.compute_ledger_merkle_root(pool)
    local_root = local.get("merkle_root", "")

    matches = []
    mismatches = []
    for peer_id, root in (expected_roots or {}).items():
        if root == local_root:
            matches.append(peer_id)
        else:
            mismatches.append({"peer_id": peer_id, "expected": root, "local": local_root})

    return {
        "local_root": local_root,
        "leaves_count": local.get("leaves_count", 0),
        "matches": matches,
        "match_count": len(matches),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "rebuild_complete_gate": len(matches) >= 2,
        "scaffold": True,
        "note": "M11.3 will activate the COMPLETE gate when match_count >= 2",
    }


# ---- M11.3 content fingerprint (bug A fix — cross-pool stable) ----
# Separate from the merkle root v1 which is FROZEN and includes the local
# BIGSERIAL id in the canonical form. The fingerprint is used by the gossip
# loop's fast-path "in_sync" check only. The merkle v1 stays authoritative
# for verify_ingest_payload.
#
# Per molecule-guardian Q3 correction : MUST include the 4 credits_* columns
# separately, not credits_total alone. An attacker could otherwise alter
# credits_worker/credits_exec/credits_origin/credits_treasury without
# changing credits_total (the split components can rearrange while summing
# to the same total). Using credits_total alone would give false-positive
# "in_sync" when the split has diverged.

async def compute_content_fingerprint(pool) -> dict:
    """Cross-pool stable fingerprint of the full revenue_ledger content.

    Excludes `id` (BIGSERIAL local) and mutable columns. Includes all 4
    credits_* fields separately per guardian Q3.

    Returns dict with:
        - fingerprint : hex string
        - row_count : int
        - since_id : None (full-scan)
        - until_id : None
        - version : 1

    NOT a replacement for merkle root v1. Helper for fast-path comparison
    in the gossip loop. The merkle v1 stays the authority for
    verify_ingest_payload.
    """
    import hashlib as _hashlib
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"fingerprint": "", "row_count": 0, "version": 1}
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT origin_pool_id, job_id, credits_total, credits_worker,
                   credits_exec, credits_origin, credits_treasury,
                   exec_pool_id, worker_id, model, tokens_in, tokens_out
            FROM revenue_ledger
            ORDER BY origin_pool_id, job_id
            """
        )
    h = _hashlib.sha256()
    for r in rows:
        # Sort-stable canonical per row (strings + ints + ASCII separator)
        row_bytes = (
            (r["origin_pool_id"] or "").encode() + b"\x1f" +
            (r["job_id"] or "").encode() + b"\x1f" +
            str(int(r["credits_total"] or 0)).encode() + b"\x1f" +
            str(int(r["credits_worker"] or 0)).encode() + b"\x1f" +
            str(int(r["credits_exec"] or 0)).encode() + b"\x1f" +
            str(int(r["credits_origin"] or 0)).encode() + b"\x1f" +
            str(int(r["credits_treasury"] or 0)).encode() + b"\x1f" +
            (r["exec_pool_id"] or "").encode() + b"\x1f" +
            (r["worker_id"] or "").encode() + b"\x1f" +
            (r["model"] or "").encode() + b"\x1f" +
            str(int(r["tokens_in"] or 0)).encode() + b"\x1f" +
            str(int(r["tokens_out"] or 0)).encode() + b"\x1e"
        )
        h.update(row_bytes)
    return {
        "fingerprint": h.hexdigest(),
        "row_count": len(rows),
        "version": 1,
    }


# ---- M11.2 signed GET response helpers ----

def sign_body_with_self(pool, body: bytes) -> str:
    """Sign sha256(body) with the pool self_ed25519 privkey. Returns hex.

    Used by GET /v1/federation/ledger/sync-pull to sign the response body
    so the gossip loop on the requesting peer can verify authenticity
    before trusting the merkle root recompute. Without this, a MITM
    could forge a coherent set of rows whose merkle matches.
    """
    import hashlib as _hashlib
    from .federation import _load_privkey_from_disk, sign as _ed25519_sign
    from pathlib import Path

    if pool.federation_self is None:
        return ""
    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return ""
    body_hash = _hashlib.sha256(body).digest()
    sig_bytes = _ed25519_sign(priv_raw, body_hash)
    return sig_bytes.hex()


def verify_peer_response_signature(peer_pubkey: bytes, body: bytes, sig_hex: str) -> bool:
    """Verify a peer-signed response body (Ed25519 over sha256(body)).

    Returns True iff the signature is valid. On any error (bad hex,
    wrong length, verify fail) returns False. Never raises — the caller
    decides how to handle a bad response (drop + log).
    """
    if not sig_hex or not peer_pubkey or len(peer_pubkey) != 32:
        return False
    try:
        import hashlib as _hashlib
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        sig_bytes = bytes.fromhex(sig_hex)
        body_hash = _hashlib.sha256(body).digest()
        pub = Ed25519PublicKey.from_public_bytes(peer_pubkey)
        pub.verify(sig_bytes, body_hash)
        return True
    except (ValueError, InvalidSignature, Exception):
        return False


# ---- M11.2 background gossip loop ----

async def _gossip_pull_one_peer(pool, peer: dict) -> dict:
    """Single peer pull cycle. Returns stats dict.

    Steps (guardian required ordering):
        1. GET peer.url/v1/federation/ledger/merkle-root (public, unsigned)
        2. Compare to local merkle root
        3. If match : return {status: in_sync}
        4. If diff : GET peer.url/v1/federation/ledger/sync-pull (signed)
        5. Verify X-IAMINE-Signature via federation_peers.pubkey
        6. verify_ingest_payload (recompute merkle from canonical v1)
        7. INSERT append-only ON CONFLICT (origin_pool_id, job_id) DO NOTHING
        8. Return rows_pulled, new_root after INSERT

    All steps are best-effort. Any failure logs a WARNING with the reason
    and returns a failed status without raising.
    """
    import aiohttp as _aiohttp
    from . import federation_merkle as _fm

    peer_atom_id = peer.get("atom_id", "?")
    peer_url = (peer.get("url") or "").rstrip("/")
    peer_pubkey = peer.get("pubkey")
    if isinstance(peer_pubkey, (bytes, bytearray, memoryview)):
        peer_pubkey = bytes(peer_pubkey)
    elif isinstance(peer_pubkey, str):
        try:
            peer_pubkey = bytes.fromhex(peer_pubkey)
        except Exception:
            peer_pubkey = None

    if not peer_url or not peer_pubkey:
        return {"peer_atom_id": peer_atom_id, "status": "missing_peer_metadata"}

    timeout = _aiohttp.ClientTimeout(total=10.0)
    local = await _fm.compute_ledger_merkle_root(pool)
    local_root = local.get("merkle_root", "")
    local_leaves = int(local.get("leaves_count", 0))

    try:
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{peer_url}/v1/federation/ledger/merkle-root") as r:
                if r.status != 200:
                    return {"peer_atom_id": peer_atom_id, "status": f"root_http_{r.status}"}
                peer_root_data = await r.json()
    except Exception as e:
        return {"peer_atom_id": peer_atom_id, "status": f"root_error: {str(e)[:80]}"}

    peer_root = peer_root_data.get("merkle_root", "")
    peer_leaves = int(peer_root_data.get("leaves_count", 0))

    if peer_root == local_root:
        return {
            "peer_atom_id": peer_atom_id,
            "status": "in_sync",
            "local_root": local_root,
            "peer_root": peer_root,
            "leaves": local_leaves,
        }

    # Mismatch : pull rows
    log.info(
        f"M11.2 gossip mismatch peer={peer_atom_id[:16]} "
        f"local_root={local_root[:16]} local_leaves={local_leaves} "
        f"peer_root={peer_root[:16]} peer_leaves={peer_leaves}"
    )

    try:
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{peer_url}/v1/federation/ledger/sync-pull?limit=500") as r:
                if r.status != 200:
                    return {"peer_atom_id": peer_atom_id, "status": f"pull_http_{r.status}"}
                body_bytes = await r.read()
                sig_hex = r.headers.get("X-IAMINE-Signature", "")
    except Exception as e:
        return {"peer_atom_id": peer_atom_id, "status": f"pull_error: {str(e)[:80]}"}

    # 5. Verify emitter signature BEFORE trusting anything
    if not verify_peer_response_signature(peer_pubkey, body_bytes, sig_hex):
        log.warning(
            f"M11.2 gossip peer={peer_atom_id[:16]} response signature INVALID — dropping"
        )
        return {"peer_atom_id": peer_atom_id, "status": "sig_invalid"}

    # 6. Parse + verify merkle
    import json as _json
    try:
        data = _json.loads(body_bytes.decode())
    except Exception as e:
        return {"peer_atom_id": peer_atom_id, "status": f"json_error: {str(e)[:80]}"}

    rows = data.get("rows") or []
    claimed_root = data.get("merkle_root", "")

    # Parse ISO strings -> datetime so canonical_row_bytes can compute micros.
    # Rows come from JSON (strings), canonical form expects datetime objects.
    import datetime as _dt
    for _r in rows:
        ca = _r.get("created_at")
        if isinstance(ca, str):
            try:
                _r["created_at"] = _dt.datetime.fromisoformat(ca.replace("Z", "+00:00"))
            except Exception:
                pass

    if not rows:
        return {"peer_atom_id": peer_atom_id, "status": "empty_rows"}

    verdict = verify_ingest_payload(rows, claimed_root)
    if not verdict.get("ok"):
        log.warning(
            f"M11.2 gossip peer={peer_atom_id[:16]} merkle mismatch "
            f"computed={verdict.get('computed_root','')[:16]} "
            f"claimed={claimed_root[:16]} — dropping"
        )
        return {"peer_atom_id": peer_atom_id, "status": "merkle_mismatch"}

    # 7. Idempotent append-only INSERT
    inserted = 0
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"peer_atom_id": peer_atom_id, "status": "no_db"}

    try:
        async with pool.store.pool.acquire() as conn:
            for row in rows:
                try:
                    r = await conn.execute(
                        """
                        INSERT INTO revenue_ledger (
                            job_id, origin_pool_id, exec_pool_id, worker_id,
                            model, tokens_in, tokens_out,
                            credits_total, credits_worker, credits_exec,
                            credits_origin, credits_treasury,
                            forward_chain, created_at, pending_worker_attribution
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,TRUE)
                        ON CONFLICT (origin_pool_id, job_id) DO NOTHING
                        """,
                        row.get("job_id"),
                        row.get("origin_pool_id"),
                        row.get("exec_pool_id"),
                        row.get("worker_id"),
                        row.get("model"),
                        int(row.get("tokens_in") or 0),
                        int(row.get("tokens_out") or 0),
                        int(row.get("credits_total") or 0),
                        int(row.get("credits_worker") or 0),
                        int(row.get("credits_exec") or 0),
                        int(row.get("credits_origin") or 0),
                        int(row.get("credits_treasury") or 0),
                        row.get("forward_chain") or [],
                        row.get("created_at"),
                    )
                    # asyncpg execute returns 'INSERT 0 N' where N is the count
                    if isinstance(r, str) and r.startswith("INSERT ") and not r.endswith(" 0"):
                        inserted += 1
                except Exception as _ie:
                    log.warning(f"M11.2 row insert failed: {_ie}")
    except Exception as e:
        log.warning(f"M11.2 pool acquire failed: {e}")
        return {"peer_atom_id": peer_atom_id, "status": f"insert_error: {str(e)[:80]}"}

    # 8. Recompute local root after insert for logging
    after = await _fm.compute_ledger_merkle_root(pool)
    new_local_root = after.get("merkle_root", "")

    log.info(
        f"M11.2 gossip pulled from peer={peer_atom_id[:16]} "
        f"rows_received={len(rows)} inserted={inserted} "
        f"new_local_root={new_local_root[:16]} "
        f"leaves={after.get('leaves_count', 0)}"
    )

    return {
        "peer_atom_id": peer_atom_id,
        "status": "pulled",
        "rows_received": len(rows),
        "inserted": inserted,
        "old_root": local_root,
        "new_root": new_local_root,
    }


async def replication_ledger_gossip_loop(pool):
    """M11.2 background anti-entropy loop.

    Runs forever at REPLICATION_GOSSIP_INTERVAL_SEC cadence. Each cycle
    iterates bonded peers (trust>=2, reachable) and pulls missing rows.
    Idempotent via UNIQUE (origin_pool_id, job_id) constraint.

    Exits gracefully if REPLICATION_ENABLED=false or FS kill switch set.
    Exceptions in any iteration are caught and logged, the loop continues.
    """
    import asyncio as _asyncio

    log.info(f"M11.2 gossip loop starting (interval={REPLICATION_GOSSIP_INTERVAL_SEC}s)")
    while True:
        try:
            if _fed_disabled_by_kill_switch():
                log.info("M11.2 gossip loop paused (FS kill switch)")
                await _asyncio.sleep(REPLICATION_GOSSIP_INTERVAL_SEC)
                continue

            if not is_replication_enabled():
                log.info("M11.2 gossip loop paused (REPLICATION_ENABLED=false)")
                await _asyncio.sleep(REPLICATION_GOSSIP_INTERVAL_SEC)
                continue

            peers = await _fetch_bonded_peers_with_trust(pool, min_trust=2)
            if not peers:
                log.info("M11.2 gossip loop : no bonded peers reachable, skipping")
            else:
                results = await _asyncio.gather(
                    *[_gossip_pull_one_peer(pool, p) for p in peers],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, Exception):
                        log.warning(f"M11.2 gossip peer exception: {r}")

                # Update replication_state last_replication_loop
                try:
                    await update_replication_state(
                        pool,
                        last_replication_loop=__import__("datetime").datetime.utcnow(),
                    )
                except Exception as _ue:
                    log.warning(f"M11.2 update_replication_state failed: {_ue}")

        except Exception as e:
            log.error(f"M11.2 gossip loop iteration exception: {e}")
            import traceback as _tb
            log.error(_tb.format_exc()[:1000])

        await _asyncio.sleep(REPLICATION_GOSSIP_INTERVAL_SEC)


# ---- Ingest verification helper (used by route) ----

def verify_ingest_payload(rows: list, claimed_root: str) -> dict:
    """Recompute merkle root from the canonical form of incoming rows and
    compare to the claimed root in the envelope.

    Returns dict with :
        - ok : bool
        - computed_root : hex string
        - claimed_root : hex string
        - leaves_count : int

    Called by the /v1/federation/ledger/ingest route. A mismatch triggers
    a WARNING log with BOTH roots (guardian rec #5) and the route rejects
    the ingest with 400.
    """
    from . import federation_merkle

    leaves = []
    for row in rows or []:
        try:
            leaves.append(federation_merkle.leaf_hash(row))
        except Exception as e:
            return {
                "ok": False,
                "computed_root": "",
                "claimed_root": claimed_root,
                "leaves_count": 0,
                "error": f"leaf_hash failed: {e}",
            }

    root_bytes = federation_merkle.merkle_root_from_leaves(leaves)
    computed_root = root_bytes.hex()
    ok = (computed_root == claimed_root)

    return {
        "ok": ok,
        "computed_root": computed_root,
        "claimed_root": claimed_root,
        "leaves_count": len(leaves),
    }
