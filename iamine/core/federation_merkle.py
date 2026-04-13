"""M11-scaffold — Ledger Merkle primitive (read-only, neutral).

CANONICAL FORM v1 — DO NOT CHANGE WITHOUT VERSION BUMP

Layout (big-endian int64, UTF-8 strict, separator = 0x1f ASCII Unit Separator) :

    <id int64 BE>                0x1f
    <job_id UTF-8 bytes>         0x1f
    <origin_pool_id UTF-8 bytes> 0x1f
    <exec_pool_id UTF-8 bytes>   0x1f
    <worker_id UTF-8 bytes>      0x1f
    <model UTF-8 bytes>          0x1f
    <tokens_in int64 BE>         0x1f
    <tokens_out int64 BE>        0x1f
    <credits_total int64 BE>     0x1f
    <credits_worker int64 BE>    0x1f
    <credits_exec int64 BE>      0x1f
    <credits_origin int64 BE>    0x1f
    <credits_treasury int64 BE>  0x1f
    <forward_chain joined "," UTF-8> 0x1f
    <created_at micros int64 BE>

EXCLUDED from hash (mutable post-insert) :
    worker_sig, pending_worker_attribution, settled, settled_at, worker_cert_id

Merkle tree : RFC 6962 strict (leaf prefix 0x00, node prefix 0x01).
PROSCRIT : Bitcoin-style dup-last-leaf on odd count (CVE-2012-2459).

Constants :
    LEDGER_MERKLE_VERSION = 1
    EMPTY_MERKLE_ROOT = sha256(b"")

Validated by molecule-guardian 2026-04-11 (4 invariants load-bearing).
See project_m11_scaffold_invariants.md.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

log = logging.getLogger("iamine.federation.merkle")


LEDGER_MERKLE_VERSION = 1
EMPTY_MERKLE_ROOT = hashlib.sha256(b"").hexdigest()
_SEP = b"\x1f"  # ASCII Unit Separator — impossible to inject in reasonable text fields
_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"

# Hard server-side limit to prevent timeout on pathological ranges
MERKLE_SNAPSHOT_MAX_LIMIT = 10000


def _i64_be(n: int) -> bytes:
    """Pack an int as signed big-endian int64. Raises on overflow."""
    return int(n).to_bytes(8, byteorder="big", signed=True)


def _utf8(s: Optional[str]) -> bytes:
    if s is None:
        return b""
    return s.encode("utf-8", errors="strict")


def canonical_row_bytes(row: dict) -> bytes:
    """Deterministic canonical serialization of a revenue_ledger row.

    `row` is a dict (asyncpg Record cast) with at least these keys :
        id, job_id, origin_pool_id, exec_pool_id, worker_id, model,
        tokens_in, tokens_out, credits_total, credits_worker,
        credits_exec, credits_origin, credits_treasury, forward_chain,
        created_at (datetime with tzinfo or naive)

    Returns the exact bytes that get hashed into a Merkle leaf.
    """
    chain = row.get("forward_chain") or []
    if not isinstance(chain, list):
        chain = list(chain)
    chain_str = ",".join(str(c) for c in chain)

    created = row.get("created_at")
    if created is None:
        created_micros = 0
    else:
        # Convert datetime to micros since epoch. Assume UTC for naive datetimes.
        import datetime as _dt
        if isinstance(created, _dt.datetime):
            if created.tzinfo is None:
                created_utc = created.replace(tzinfo=_dt.timezone.utc)
            else:
                created_utc = created.astimezone(_dt.timezone.utc)
            epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
            delta = created_utc - epoch
            created_micros = int(delta.total_seconds() * 1_000_000) + (delta.microseconds % 1_000_000)
            # Simpler: use total_seconds directly
            created_micros = int((created_utc - epoch).total_seconds() * 1_000_000)
        else:
            created_micros = int(created)

    parts = [
        _i64_be(int(row.get("id", 0))),
        _utf8(row.get("job_id", "")),
        _utf8(row.get("origin_pool_id", "")),
        _utf8(row.get("exec_pool_id", "")),
        _utf8(row.get("worker_id", "")),
        _utf8(row.get("model", "")),
        _i64_be(int(row.get("tokens_in", 0) or 0)),
        _i64_be(int(row.get("tokens_out", 0) or 0)),
        _i64_be(int(row.get("credits_total", 0) or 0)),
        _i64_be(int(row.get("credits_worker", 0) or 0)),
        _i64_be(int(row.get("credits_exec", 0) or 0)),
        _i64_be(int(row.get("credits_origin", 0) or 0)),
        _i64_be(int(row.get("credits_treasury", 0) or 0)),
        _utf8(chain_str),
        _i64_be(created_micros),
    ]
    return _SEP.join(parts)


def leaf_hash(row: dict) -> bytes:
    """Hash a row as a Merkle leaf (RFC 6962 : sha256(0x00 || canonical))."""
    return hashlib.sha256(_LEAF_PREFIX + canonical_row_bytes(row)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """Hash two child nodes into a parent (RFC 6962 : sha256(0x01 || l || r))."""
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def merkle_root_from_leaves(leaves: list) -> bytes:
    """Compute Merkle root from a list of leaf hashes. RFC 6962 strict.

    - Empty -> sha256(b"") (EMPTY_MERKLE_ROOT)
    - Singleton -> the single leaf
    - Odd count -> NO duplication (non-balanced tree, RFC 6962 style)
    """
    if not leaves:
        return hashlib.sha256(b"").digest()
    if len(leaves) == 1:
        return leaves[0]

    # RFC 6962 style: split at largest power of 2 <= len(leaves) / 2
    # Actually RFC 6962 splits such that left subtree is a perfect subtree.
    # Find k = largest power of 2 strictly less than n
    n = len(leaves)
    k = 1
    while k * 2 < n:
        k *= 2
    left = merkle_root_from_leaves(leaves[:k])
    right = merkle_root_from_leaves(leaves[k:])
    return node_hash(left, right)


async def compute_ledger_merkle_root(
    pool,
    since_id: Optional[int] = None,
    until_id: Optional[int] = None,
    limit: int = MERKLE_SNAPSHOT_MAX_LIMIT,
) -> dict:
    """Compute the Merkle root over a range of revenue_ledger rows.

    Returns dict with :
        - merkle_root (hex string, 64 chars)
        - leaves_count
        - since_id, until_id (actual bounds used)
        - version (LEDGER_MERKLE_VERSION)
        - self_atom_id (if available, for forward compat M11-real signature)

    Read-only. Never writes. Never modifies ledger rows.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {
            "merkle_root": EMPTY_MERKLE_ROOT,
            "leaves_count": 0,
            "since_id": since_id,
            "until_id": until_id,
            "version": LEDGER_MERKLE_VERSION,
            "error": "no DB store",
        }

    limit = max(1, min(int(limit or MERKLE_SNAPSHOT_MAX_LIMIT), MERKLE_SNAPSHOT_MAX_LIMIT))

    # Query : bounds inclusive
    where = []
    args = []
    if since_id is not None:
        args.append(int(since_id))
        where.append(f"id >= ${len(args)}")
    if until_id is not None:
        args.append(int(until_id))
        where.append(f"id <= ${len(args)}")
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    limit_placeholder = f"${len(args)}"

    q = f"""
        SELECT id, job_id, origin_pool_id, exec_pool_id, worker_id, model,
               tokens_in, tokens_out,
               credits_total, credits_worker, credits_exec, credits_origin, credits_treasury,
               forward_chain, created_at
        FROM revenue_ledger
        {where_clause}
        ORDER BY id ASC
        LIMIT {limit_placeholder}
    """

    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(q, *args)

    leaves = [leaf_hash(dict(r)) for r in rows]
    root_bytes = merkle_root_from_leaves(leaves)
    root_hex = root_bytes.hex()

    actual_since = int(rows[0]["id"]) if rows else None
    actual_until = int(rows[-1]["id"]) if rows else None

    self_atom_id = pool.federation_self.atom_id if getattr(pool, "federation_self", None) else None

    return {
        "merkle_root": root_hex,
        "leaves_count": len(leaves),
        "since_id": actual_since,
        "until_id": actual_until,
        "version": LEDGER_MERKLE_VERSION,
        "self_atom_id": self_atom_id,
    }


async def snapshot_ledger_range(
    pool,
    since_id: Optional[int] = None,
    until_id: Optional[int] = None,
    limit: int = 500,
) -> dict:
    """Return rows + merkle root. Same invariants as compute_ledger_merkle_root."""
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {
            "merkle_root": EMPTY_MERKLE_ROOT,
            "rows": [],
            "count": 0,
            "since_id": since_id,
            "until_id": until_id,
            "version": LEDGER_MERKLE_VERSION,
            "error": "no DB store",
        }

    limit = max(1, min(int(limit or 500), 500))

    where = []
    args = []
    if since_id is not None:
        args.append(int(since_id))
        where.append(f"id >= ${len(args)}")
    if until_id is not None:
        args.append(int(until_id))
        where.append(f"id <= ${len(args)}")
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    limit_placeholder = f"${len(args)}"

    q = f"""
        SELECT id, job_id, origin_pool_id, exec_pool_id, worker_id, model,
               tokens_in, tokens_out,
               credits_total, credits_worker, credits_exec, credits_origin, credits_treasury,
               forward_chain, created_at
        FROM revenue_ledger
        {where_clause}
        ORDER BY id ASC
        LIMIT {limit_placeholder}
    """

    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(q, *args)

    leaves = [leaf_hash(dict(r)) for r in rows]
    root_bytes = merkle_root_from_leaves(leaves)

    # Serialize rows for JSON response
    out_rows = []
    for r in rows:
        out_rows.append({
            "id": r["id"],
            "job_id": r["job_id"],
            "origin_pool_id": r["origin_pool_id"],
            "exec_pool_id": r["exec_pool_id"],
            "worker_id": r["worker_id"],
            "model": r["model"],
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "credits_total": r["credits_total"],
            "credits_worker": r["credits_worker"],
            "credits_exec": r["credits_exec"],
            "credits_origin": r["credits_origin"],
            "credits_treasury": r["credits_treasury"],
            "forward_chain": list(r["forward_chain"] or []),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    actual_since = int(rows[0]["id"]) if rows else None
    actual_until = int(rows[-1]["id"]) if rows else None
    self_atom_id = pool.federation_self.atom_id if getattr(pool, "federation_self", None) else None

    return {
        "merkle_root": root_bytes.hex(),
        "rows": out_rows,
        "count": len(out_rows),
        "since_id": actual_since,
        "until_id": actual_until,
        "version": LEDGER_MERKLE_VERSION,
        "self_atom_id": self_atom_id,
    }
