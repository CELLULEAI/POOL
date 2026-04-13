"""M13 Phase 4 — Memory replication across federated pools.

Replicates Tier 3 (semantic facts / user_memories) and Tier 4 (procedures)
via the M11 gossip mechanism. T1 (observations) and T2 (episodes) stay LOCAL.

Key invariants:
- Zero-knowledge preserved: encrypted content replicates as opaque bytes
- Ed25519 signed envelopes (same as account replication)
- Append-only: ingest is INSERT or UPDATE (upsert), never DELETE
- RGPD delete propagates as tombstone markers
- Feature flag: MEMORY_REPLICATION_ENABLED (default false)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("iamine.memory_replication")

MEMORY_REPLICATION_ENABLED = os.environ.get(
    "MEMORY_REPLICATION_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Push: replicate memory to bonded peers
# ---------------------------------------------------------------------------

async def replicate_memories_to_peers(pool, token_hash: str,
                                       memory_ids: list[int] = None,
                                       table: str = "user_memories") -> dict:
    """Push semantic facts or procedures to all bonded peers.

    Encrypted content passes as opaque bytes — receiving pool cannot decrypt.
    Only replicates to peers with trust >= 3 (M5 hard lock).

    Returns {pushed, acked, failed, peer_acks}.
    """
    if not MEMORY_REPLICATION_ENABLED:
        return {"pushed": 0, "acked": 0, "failed": 0, "skipped": "disabled"}

    from .federation_replication import _fetch_bonded_peers_with_trust

    if pool.federation_self is None:
        return {"pushed": 0, "acked": 0, "failed": 0, "skipped": "no_federation"}

    peers = await _fetch_bonded_peers_with_trust(pool, min_trust=3)
    if not peers:
        return {"pushed": 0, "acked": 0, "failed": 0, "skipped": "no_bonded_peers"}

    # Load memories to replicate
    rows = await _load_memories_for_replication(pool, token_hash, memory_ids, table)
    if not rows:
        return {"pushed": 0, "acked": 0, "failed": 0, "skipped": "no_memories"}

    # Build payload
    payload = {
        "table": table,
        "origin_pool_id": pool.federation_self.atom_id,
        "token_hash": token_hash,
        "schema_version": 1,
        "memories": rows,
    }
    body = json.dumps(payload, default=str).encode()

    # Sign and push
    from .federation import _load_privkey_from_disk, build_envelope_headers
    from pathlib import Path
    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return {"pushed": 0, "acked": 0, "failed": len(peers), "skipped": "no_privkey"}

    import aiohttp

    async def _push_one(peer: dict) -> dict:
        t0 = time.time()
        try:
            base = peer["url"].rstrip("/")
            headers = build_envelope_headers(
                priv_raw,
                pool.federation_self.atom_id,
                "POST",
                "/v1/federation/memory/ingest",
                body, hop=0, chain=[],
            )
            headers["Content-Type"] = "application/json"
            timeout = aiohttp.ClientTimeout(total=10.0)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{base}/v1/federation/memory/ingest",
                                  data=body, headers=headers) as r:
                    dur_ms = int((time.time() - t0) * 1000)
                    if r.status == 200:
                        return {"atom_id": peer["atom_id"], "status": "ack",
                                "duration_ms": dur_ms}
                    return {"atom_id": peer["atom_id"],
                            "status": f"http_{r.status}", "duration_ms": dur_ms}
        except Exception as e:
            return {"atom_id": peer["atom_id"],
                    "status": f"error: {str(e)[:80]}",
                    "duration_ms": int((time.time() - t0) * 1000)}

    results = await asyncio.gather(*[_push_one(p) for p in peers])
    acked = sum(1 for r in results if r["status"] == "ack")

    log.info(f"MEMORY REPL: pushed {len(rows)} {table} rows to "
             f"{len(peers)} peers ({acked} acked) for {token_hash[:8]}...")

    return {
        "pushed": len(peers),
        "acked": acked,
        "failed": len(peers) - acked,
        "memories_count": len(rows),
        "peer_acks": results,
    }


async def _load_memories_for_replication(pool, token_hash: str,
                                          memory_ids: list[int] = None,
                                          table: str = "user_memories") -> list[dict]:
    """Load memory rows for replication. Encrypted content passes as-is."""
    try:
        async with pool.store.pool.acquire() as conn:
            if table == "user_memories":
                if memory_ids:
                    rows = await conn.fetch("""
                        SELECT id, token_hash, embedding::text as embedding,
                               fact_text_enc, salt, conv_id, category,
                               confidence, decay_factor, created
                        FROM user_memories WHERE id = ANY($1::bigint[])
                    """, memory_ids)
                else:
                    rows = await conn.fetch("""
                        SELECT id, token_hash, embedding::text as embedding,
                               fact_text_enc, salt, conv_id, category,
                               confidence, decay_factor, created
                        FROM user_memories
                        WHERE token_hash = $1
                        ORDER BY created DESC LIMIT 100
                    """, token_hash)
            elif table == "agent_procedures":
                if memory_ids:
                    rows = await conn.fetch("""
                        SELECT id, token_hash, name, description_enc, salt,
                               trigger_pattern, steps_enc,
                               embedding::text as embedding,
                               success_rate, use_count, active, created_at
                        FROM agent_procedures WHERE id = ANY($1::bigint[])
                    """, memory_ids)
                else:
                    rows = await conn.fetch("""
                        SELECT id, token_hash, name, description_enc, salt,
                               trigger_pattern, steps_enc,
                               embedding::text as embedding,
                               success_rate, use_count, active, created_at
                        FROM agent_procedures
                        WHERE token_hash = $1 AND active = TRUE
                        ORDER BY created_at DESC LIMIT 50
                    """, token_hash)
            else:
                return []

            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"MEMORY REPL: load failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Ingest: receive memory from a peer
# ---------------------------------------------------------------------------

async def ingest_memories(pool, payload: dict, from_atom_id: str) -> dict:
    """Ingest replicated memories from a peer pool.

    Upserts into local DB. Encrypted content stored as-is (zero-knowledge).
    Returns {ingested, skipped, errors}.
    """
    if not MEMORY_REPLICATION_ENABLED:
        return {"ingested": 0, "skipped": 0, "error": "replication disabled"}

    table = payload.get("table", "")
    memories = payload.get("memories", [])
    origin = payload.get("origin_pool_id", "")
    th = payload.get("token_hash", "")

    if not memories:
        return {"ingested": 0, "skipped": 0}

    ingested = 0
    skipped = 0

    try:
        async with pool.store.pool.acquire() as conn:
            for mem in memories:
                try:
                    if table == "user_memories":
                        await conn.execute("""
                            INSERT INTO user_memories
                                (token_hash, embedding, fact_text_enc, salt,
                                 conv_id, category, confidence, decay_factor, created)
                            VALUES ($1, $2::vector, $3, $4, $5, $6, $7, $8, $9)
                            ON CONFLICT DO NOTHING
                        """, mem.get("token_hash", th),
                            mem.get("embedding"),
                            mem["fact_text_enc"], mem["salt"],
                            mem.get("conv_id", ""),
                            mem.get("category", "fact"),
                            mem.get("confidence", 1.0),
                            mem.get("decay_factor", 1.0),
                            mem.get("created"))
                        ingested += 1

                    elif table == "agent_procedures":
                        await conn.execute("""
                            INSERT INTO agent_procedures
                                (token_hash, name, description_enc, salt,
                                 trigger_pattern, steps_enc, embedding,
                                 success_rate, use_count, active, created_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7::vector,
                                    $8, $9, $10, $11)
                            ON CONFLICT DO NOTHING
                        """, mem.get("token_hash", th),
                            mem["name"], mem["description_enc"], mem["salt"],
                            mem.get("trigger_pattern", ""),
                            mem["steps_enc"], mem.get("embedding"),
                            mem.get("success_rate", 0.5),
                            mem.get("use_count", 0),
                            mem.get("active", True),
                            mem.get("created_at"))
                        ingested += 1
                    else:
                        skipped += 1

                except Exception as e:
                    log.warning(f"MEMORY REPL: ingest row failed: {e}")
                    skipped += 1

        log.info(f"MEMORY REPL: ingested {ingested}/{len(memories)} {table} "
                 f"from {from_atom_id[:12]}... for {th[:8]}...")

    except Exception as e:
        log.warning(f"MEMORY REPL: ingest failed: {e}")
        return {"ingested": 0, "skipped": len(memories), "error": str(e)}

    return {"ingested": ingested, "skipped": skipped}


# ---------------------------------------------------------------------------
# RGPD: propagate forget-all across federation
# ---------------------------------------------------------------------------

async def propagate_forget(pool, token_hash: str) -> dict:
    """Propagate a RGPD forget-all to all bonded peers.

    Sends a signed tombstone — receiving pools delete all memory for this token_hash.
    """
    if not MEMORY_REPLICATION_ENABLED:
        return {"pushed": 0, "skipped": "disabled"}

    from .federation_replication import _fetch_bonded_peers_with_trust

    if pool.federation_self is None:
        return {"pushed": 0, "skipped": "no_federation"}

    peers = await _fetch_bonded_peers_with_trust(pool, min_trust=3)
    if not peers:
        return {"pushed": 0, "skipped": "no_peers"}

    payload = {
        "action": "forget_all",
        "token_hash": token_hash,
        "origin_pool_id": pool.federation_self.atom_id,
    }
    body = json.dumps(payload).encode()

    from .federation import _load_privkey_from_disk, build_envelope_headers
    from pathlib import Path
    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return {"pushed": 0, "skipped": "no_privkey"}

    import aiohttp

    async def _push_forget(peer):
        try:
            base = peer["url"].rstrip("/")
            headers = build_envelope_headers(
                priv_raw, pool.federation_self.atom_id,
                "POST", "/v1/federation/memory/forget",
                body, hop=0, chain=[])
            headers["Content-Type"] = "application/json"
            timeout = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{base}/v1/federation/memory/forget",
                                  data=body, headers=headers) as r:
                    return {"atom_id": peer["atom_id"], "status": r.status}
        except Exception as e:
            return {"atom_id": peer["atom_id"], "status": str(e)[:80]}

    results = await asyncio.gather(*[_push_forget(p) for p in peers])
    acked = sum(1 for r in results if r.get("status") == 200)

    log.info(f"MEMORY REPL: forget propagated to {len(peers)} peers "
             f"({acked} acked) for {token_hash[:8]}...")

    return {"pushed": len(peers), "acked": acked, "peer_results": results}


# ---------------------------------------------------------------------------
# Ingest endpoint handler (called from routes)
# ---------------------------------------------------------------------------

async def handle_memory_ingest(pool, payload: dict, from_atom_id: str) -> dict:
    """Route handler for /v1/federation/memory/ingest."""
    return await ingest_memories(pool, payload, from_atom_id)


async def handle_memory_forget(pool, payload: dict, from_atom_id: str) -> dict:
    """Route handler for /v1/federation/memory/forget (RGPD tombstone)."""
    th = payload.get("token_hash", "")
    if not th:
        return {"error": "missing token_hash"}

    deleted = {}
    try:
        async with pool.store.pool.acquire() as conn:
            r = await conn.execute(
                "DELETE FROM agent_observations WHERE token_hash = $1", th)
            deleted["observations"] = int(r.split()[-1])
            r = await conn.execute(
                "DELETE FROM agent_episodes WHERE token_hash = $1", th)
            deleted["episodes"] = int(r.split()[-1])
            r = await conn.execute(
                "DELETE FROM user_memories WHERE token_hash = $1", th)
            deleted["semantic_facts"] = int(r.split()[-1])
            r = await conn.execute(
                "DELETE FROM agent_procedures WHERE token_hash = $1", th)
            deleted["procedures"] = int(r.split()[-1])
            r = await conn.execute(
                "DELETE FROM memory_consolidation_log WHERE token_hash = $1", th)
            deleted["consolidation_logs"] = int(r.split()[-1])

        log.info(f"MEMORY REPL: forget executed for {th[:8]}... "
                 f"from {from_atom_id[:12]}... — {deleted}")
    except Exception as e:
        return {"error": str(e)}

    return {"status": "purged", "deleted": deleted}


# ---------------------------------------------------------------------------
# Auto-push hook: called after semantic extraction creates new facts
# ---------------------------------------------------------------------------

async def auto_replicate_new_facts(pool, token_hash: str,
                                    fact_ids: list[int]) -> None:
    """Fire-and-forget: push new semantic facts to federation.

    Called from agent_memory_phase2.extract_semantic_facts() after new facts.
    """
    if not MEMORY_REPLICATION_ENABLED or not fact_ids:
        return
    try:
        await replicate_memories_to_peers(
            pool, token_hash, memory_ids=fact_ids, table="user_memories")
    except Exception as e:
        log.warning(f"MEMORY REPL: auto-push failed: {e}")


async def auto_replicate_procedures(pool, token_hash: str,
                                     proc_ids: list[int]) -> None:
    """Fire-and-forget: push new procedures to federation."""
    if not MEMORY_REPLICATION_ENABLED or not proc_ids:
        return
    try:
        await replicate_memories_to_peers(
            pool, token_hash, memory_ids=proc_ids, table="agent_procedures")
    except Exception as e:
        log.warning(f"MEMORY REPL: auto-push procedures failed: {e}")
