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
"""M11.5 extension — append to memory_replication.py

Adds:
- TRUST_BONDED constant (invariant 5)
- Circuit breaker helpers (latency tracking)
- Activity log helper
- conversations gossip (push/ingest/load)
- agent_episodes gossip (push/ingest/load, 5min rhythm)
- pull_since unified endpoint logic
- relationships regen hook (invariant 3)
- bootstrap resumable (invariant 2)
- extend handle_memory_forget to cover conversations

All functions respect the 5 invariants validated by molecule-guardian.
"""

# =============================================================================
# M11.5 EXTENSIONS (Conversations + Episodes gossip, Bootstrap, Circuit breaker)
# =============================================================================

# Invariant 5: trust level constant
TRUST_BONDED = 3

# Circuit breaker thresholds
CB_SLOW_THRESHOLD_MS = 500
CB_SLOW_FAILURES = 3
CB_RECOVERY_SUCCESS = 3
CB_FAST_THRESHOLD_MS = 200

# Rhythms (seconds)
RHYTHM_CONVERSATIONS = 30
RHYTHM_MEMORIES = 30
RHYTHM_EPISODES = 300  # 5min for T2 (less critical per guardian Q2)
RHYTHM_PROCEDURES = 30

# Pagination
PAGE_CONVERSATIONS = 100
PAGE_MEMORIES = 50
PAGE_EPISODES = 50
PAGE_BOOTSTRAP = 500


# ---------------------------------------------------------------------------
# Circuit breaker: track peer latency, mark slow peers
# ---------------------------------------------------------------------------

async def _record_peer_latency(pool, atom_id: str, latency_ms: int,
                                success: bool) -> None:
    """Update circuit breaker state for a peer after a request."""
    try:
        async with pool.store.pool.acquire() as conn:
            if success and latency_ms < CB_FAST_THRESHOLD_MS:
                # Fast success: decrement failures, potentially clear circuit
                row = await conn.fetchrow(
                    "SELECT circuit_failures FROM federation_peers WHERE atom_id=$1",
                    atom_id)
                if row and row["circuit_failures"] > 0:
                    new_fail = max(0, row["circuit_failures"] - 1)
                    clear_slow = (new_fail == 0)
                    await conn.execute("""
                        UPDATE federation_peers
                           SET circuit_failures = $1,
                               circuit_slow = CASE WHEN $2 THEN false
                                                   ELSE circuit_slow END,
                               latency_ms_avg = ($3 + COALESCE(latency_ms_avg,0))/2
                         WHERE atom_id = $4
                    """, new_fail, clear_slow, latency_ms, atom_id)
            elif not success or latency_ms > CB_SLOW_THRESHOLD_MS:
                # Slow or failed: increment failures
                await conn.execute("""
                    UPDATE federation_peers
                       SET circuit_failures = COALESCE(circuit_failures,0) + 1,
                           circuit_slow = CASE
                               WHEN COALESCE(circuit_failures,0) + 1 >= $1 THEN true
                               ELSE circuit_slow END,
                           latency_ms_avg = ($2 + COALESCE(latency_ms_avg,0))/2
                     WHERE atom_id = $3
                """, CB_SLOW_FAILURES, latency_ms, atom_id)
            else:
                # Normal speed, just update avg
                await conn.execute("""
                    UPDATE federation_peers
                       SET latency_ms_avg = ($1 + COALESCE(latency_ms_avg,0))/2
                     WHERE atom_id = $2
                """, latency_ms, atom_id)
    except Exception as e:
        log.warning(f"CB: latency record failed: {e}")


async def _peer_is_slow(pool, atom_id: str) -> bool:
    """Check if peer is currently marked slow (pull at reduced frequency)."""
    try:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT circuit_slow FROM federation_peers WHERE atom_id=$1",
                atom_id)
            return bool(row and row["circuit_slow"])
    except Exception:
        return False


async def _log_replication_activity(pool, peer_atom_id: str, direction: str,
                                     table_name: str, rows_count: int,
                                     latency_ms: int, success: bool,
                                     error_msg: str = None) -> None:
    """Append-only audit of replication activity."""
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO replication_activity_log
                    (peer_atom_id, direction, table_name, rows_count,
                     latency_ms, success, error_msg)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, peer_atom_id, direction, table_name, rows_count,
                latency_ms, success, error_msg)
    except Exception as e:
        log.warning(f"REPL LOG: insert failed: {e}")


# ---------------------------------------------------------------------------
# Conversations replication (push)
# ---------------------------------------------------------------------------

async def _load_conversations_for_replication(pool, token_hash: str = None,
                                                since_ts=None,
                                                limit: int = PAGE_CONVERSATIONS) -> list[dict]:
    """Load conversations rows for replication.

    messages JSONB is Fernet-encrypted (verified 14 apr) — passes as opaque blob.
    """
    try:
        async with pool.store.pool.acquire() as conn:
            if since_ts is not None:
                rows = await conn.fetch("""
                    SELECT conv_id, api_token, messages, total_tokens,
                           model_used, worker_id, created, last_activity,
                           expires, title, message_count
                      FROM conversations
                     WHERE last_activity > $1
                     ORDER BY last_activity ASC
                     LIMIT $2
                """, since_ts, limit)
            elif token_hash:
                # api_token stores derived account token — match indirectly
                rows = await conn.fetch("""
                    SELECT conv_id, api_token, messages, total_tokens,
                           model_used, worker_id, created, last_activity,
                           expires, title, message_count
                      FROM conversations c
                     WHERE EXISTS (
                         SELECT 1 FROM accounts a
                          WHERE a.api_token = c.api_token
                            AND a.token_hash = $1)
                     ORDER BY last_activity DESC
                     LIMIT $2
                """, token_hash, limit)
            else:
                return []

            result = []
            for r in rows:
                d = dict(r)
                # messages is already JSONB → convert for JSON transport
                if d.get("messages") is not None:
                    try:
                        import json as _json
                        d["messages"] = _json.loads(d["messages"]) if isinstance(
                            d["messages"], str) else d["messages"]
                    except Exception:
                        pass
                result.append(d)
            return result
    except Exception as e:
        log.warning(f"CONV REPL: load failed: {e}")
        return []


async def replicate_conversations_to_peers(pool, since_ts=None,
                                             max_conversations: int = PAGE_CONVERSATIONS) -> dict:
    """Push conversations updated since last sync to all bonded peers."""
    if not MEMORY_REPLICATION_ENABLED:
        return {"pushed": 0, "acked": 0, "skipped": "disabled"}

    from .federation_replication import _fetch_bonded_peers_with_trust

    if pool.federation_self is None:
        return {"pushed": 0, "skipped": "no_federation"}

    peers = await _fetch_bonded_peers_with_trust(pool, min_trust=TRUST_BONDED)
    if not peers:
        return {"pushed": 0, "skipped": "no_bonded_peers"}

    rows = await _load_conversations_for_replication(
        pool, since_ts=since_ts, limit=max_conversations)
    if not rows:
        return {"pushed": 0, "skipped": "no_conversations"}

    payload = {
        "table": "conversations",
        "origin_pool_id": pool.federation_self.atom_id,
        "schema_version": 1,
        "conversations": rows,
    }
    body = json.dumps(payload, default=str).encode()

    from .federation import _load_privkey_from_disk, build_envelope_headers
    from pathlib import Path
    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return {"pushed": 0, "skipped": "no_privkey"}

    import aiohttp

    async def _push_one(peer: dict) -> dict:
        # Circuit breaker: skip slow peers at normal freq
        if await _peer_is_slow(pool, peer["atom_id"]):
            return {"atom_id": peer["atom_id"], "status": "skipped_slow"}
        t0 = time.time()
        try:
            base = peer["url"].rstrip("/")
            headers = build_envelope_headers(
                priv_raw, pool.federation_self.atom_id,
                "POST", "/v1/federation/conversations/ingest",
                body, hop=0, chain=[])
            headers["Content-Type"] = "application/json"
            timeout = aiohttp.ClientTimeout(total=15.0)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{base}/v1/federation/conversations/ingest",
                                  data=body, headers=headers) as r:
                    dur_ms = int((time.time() - t0) * 1000)
                    success = (r.status == 200)
                    await _record_peer_latency(pool, peer["atom_id"], dur_ms, success)
                    await _log_replication_activity(
                        pool, peer["atom_id"], "push", "conversations",
                        len(rows), dur_ms, success,
                        None if success else f"http_{r.status}")
                    return {"atom_id": peer["atom_id"],
                            "status": "ack" if success else f"http_{r.status}",
                            "duration_ms": dur_ms}
        except Exception as e:
            dur_ms = int((time.time() - t0) * 1000)
            await _record_peer_latency(pool, peer["atom_id"], dur_ms, False)
            await _log_replication_activity(
                pool, peer["atom_id"], "push", "conversations",
                0, dur_ms, False, str(e)[:200])
            return {"atom_id": peer["atom_id"],
                    "status": f"error: {str(e)[:80]}",
                    "duration_ms": dur_ms}

    results = await asyncio.gather(*[_push_one(p) for p in peers])
    acked = sum(1 for r in results if r.get("status") == "ack")

    log.info(f"CONV REPL: pushed {len(rows)} conversations to "
             f"{len(peers)} peers ({acked} acked)")

    return {
        "pushed": len(peers),
        "acked": acked,
        "failed": len(peers) - acked,
        "conversations_count": len(rows),
        "peer_acks": results,
    }


async def ingest_conversations(pool, payload: dict, from_atom_id: str) -> dict:
    """Receive and upsert conversations from a peer."""
    if not MEMORY_REPLICATION_ENABLED:
        return {"ingested": 0, "skipped": 0, "error": "replication disabled"}

    convs = payload.get("conversations", [])
    if not convs:
        return {"ingested": 0, "skipped": 0}

    ingested = 0
    skipped = 0
    try:
        async with pool.store.pool.acquire() as conn:
            for c in convs:
                try:
                    msgs = c.get("messages")
                    if isinstance(msgs, (list, dict)):
                        msgs = json.dumps(msgs)
                    await conn.execute("""
                        INSERT INTO conversations
                            (conv_id, api_token, messages, total_tokens,
                             model_used, worker_id, created, last_activity,
                             expires, title, message_count,
                             replicated_from_atom_id)
                        VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8,
                                $9, $10, $11, $12)
                        ON CONFLICT (conv_id) DO UPDATE SET
                            messages = EXCLUDED.messages,
                            total_tokens = EXCLUDED.total_tokens,
                            last_activity = EXCLUDED.last_activity,
                            title = EXCLUDED.title,
                            message_count = EXCLUDED.message_count
                          WHERE conversations.last_activity
                                < EXCLUDED.last_activity
                    """, c["conv_id"], c.get("api_token"), msgs,
                        c.get("total_tokens", 0),
                        c.get("model_used"), c.get("worker_id"),
                        c.get("created"), c.get("last_activity"),
                        c.get("expires"), c.get("title", ""),
                        c.get("message_count", 0), from_atom_id)
                    ingested += 1
                except Exception as e:
                    log.warning(f"CONV INGEST: row {c.get('conv_id')} failed: {e}")
                    skipped += 1

        log.info(f"CONV REPL: ingested {ingested}/{len(convs)} conversations "
                 f"from {from_atom_id[:12]}...")
    except Exception as e:
        return {"ingested": 0, "skipped": len(convs), "error": str(e)}

    return {"ingested": ingested, "skipped": skipped}


# ---------------------------------------------------------------------------
# Agent episodes (T2) replication — 5min rhythm
# ---------------------------------------------------------------------------

async def _load_episodes_for_replication(pool, since_ts=None,
                                           limit: int = PAGE_EPISODES) -> list[dict]:
    """Load agent_episodes rows for replication."""
    try:
        async with pool.store.pool.acquire() as conn:
            if since_ts is not None:
                rows = await conn.fetch("""
                    SELECT id, token_hash, episode_type, summary_enc, salt,
                           embedding::text as embedding, observation_ids,
                           started_at, ended_at, metadata, created_at
                      FROM agent_episodes
                     WHERE created_at > $1
                     ORDER BY created_at ASC
                     LIMIT $2
                """, since_ts, limit)
            else:
                rows = await conn.fetch("""
                    SELECT id, token_hash, episode_type, summary_enc, salt,
                           embedding::text as embedding, observation_ids,
                           started_at, ended_at, metadata, created_at
                      FROM agent_episodes
                     ORDER BY created_at DESC
                     LIMIT $1
                """, limit)
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"EPISODE REPL: load failed: {e}")
        return []


async def replicate_episodes_to_peers(pool, since_ts=None,
                                        max_episodes: int = PAGE_EPISODES) -> dict:
    """Push agent_episodes updated since last sync to bonded peers."""
    if not MEMORY_REPLICATION_ENABLED:
        return {"pushed": 0, "skipped": "disabled"}

    from .federation_replication import _fetch_bonded_peers_with_trust

    if pool.federation_self is None:
        return {"pushed": 0, "skipped": "no_federation"}

    peers = await _fetch_bonded_peers_with_trust(pool, min_trust=TRUST_BONDED)
    if not peers:
        return {"pushed": 0, "skipped": "no_bonded_peers"}

    rows = await _load_episodes_for_replication(pool, since_ts=since_ts,
                                                  limit=max_episodes)
    if not rows:
        return {"pushed": 0, "skipped": "no_episodes"}

    payload = {
        "table": "agent_episodes",
        "origin_pool_id": pool.federation_self.atom_id,
        "schema_version": 1,
        "episodes": rows,
    }
    body = json.dumps(payload, default=str).encode()

    from .federation import _load_privkey_from_disk, build_envelope_headers
    from pathlib import Path
    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return {"pushed": 0, "skipped": "no_privkey"}

    import aiohttp

    async def _push_one(peer: dict) -> dict:
        if await _peer_is_slow(pool, peer["atom_id"]):
            return {"atom_id": peer["atom_id"], "status": "skipped_slow"}
        t0 = time.time()
        try:
            base = peer["url"].rstrip("/")
            headers = build_envelope_headers(
                priv_raw, pool.federation_self.atom_id,
                "POST", "/v1/federation/episodes/ingest",
                body, hop=0, chain=[])
            headers["Content-Type"] = "application/json"
            timeout = aiohttp.ClientTimeout(total=15.0)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{base}/v1/federation/episodes/ingest",
                                  data=body, headers=headers) as r:
                    dur_ms = int((time.time() - t0) * 1000)
                    success = (r.status == 200)
                    await _record_peer_latency(pool, peer["atom_id"], dur_ms, success)
                    await _log_replication_activity(
                        pool, peer["atom_id"], "push", "agent_episodes",
                        len(rows), dur_ms, success,
                        None if success else f"http_{r.status}")
                    return {"atom_id": peer["atom_id"],
                            "status": "ack" if success else f"http_{r.status}",
                            "duration_ms": dur_ms}
        except Exception as e:
            dur_ms = int((time.time() - t0) * 1000)
            await _record_peer_latency(pool, peer["atom_id"], dur_ms, False)
            return {"atom_id": peer["atom_id"],
                    "status": f"error: {str(e)[:80]}", "duration_ms": dur_ms}

    results = await asyncio.gather(*[_push_one(p) for p in peers])
    acked = sum(1 for r in results if r.get("status") == "ack")

    log.info(f"EPISODE REPL: pushed {len(rows)} episodes to "
             f"{len(peers)} peers ({acked} acked)")

    return {"pushed": len(peers), "acked": acked,
            "episodes_count": len(rows), "peer_acks": results}


async def ingest_episodes(pool, payload: dict, from_atom_id: str) -> dict:
    """Receive and upsert agent_episodes from a peer."""
    if not MEMORY_REPLICATION_ENABLED:
        return {"ingested": 0, "skipped": 0, "error": "replication disabled"}

    episodes = payload.get("episodes", [])
    if not episodes:
        return {"ingested": 0, "skipped": 0}

    ingested = 0
    skipped = 0
    try:
        async with pool.store.pool.acquire() as conn:
            for ep in episodes:
                try:
                    meta = ep.get("metadata")
                    if isinstance(meta, (list, dict)):
                        meta = json.dumps(meta)
                    obs_ids = ep.get("observation_ids") or []
                    await conn.execute("""
                        INSERT INTO agent_episodes
                            (token_hash, episode_type, summary_enc, salt,
                             embedding, observation_ids, started_at, ended_at,
                             metadata, created_at)
                        VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8,
                                $9::jsonb, $10)
                        ON CONFLICT DO NOTHING
                    """, ep["token_hash"], ep.get("episode_type", "generic"),
                        ep["summary_enc"], ep["salt"], ep.get("embedding"),
                        obs_ids, ep.get("started_at"), ep.get("ended_at"),
                        meta, ep.get("created_at"))
                    ingested += 1
                except Exception as e:
                    log.warning(f"EPISODE INGEST: row failed: {e}")
                    skipped += 1

        log.info(f"EPISODE REPL: ingested {ingested}/{len(episodes)} episodes "
                 f"from {from_atom_id[:12]}...")
    except Exception as e:
        return {"ingested": 0, "skipped": len(episodes), "error": str(e)}

    return {"ingested": ingested, "skipped": skipped}


# ---------------------------------------------------------------------------
# Relationships regen (invariant 3 — tracked via relationships_rebuilt_at)
# ---------------------------------------------------------------------------

async def regenerate_relationships_for_token(pool, token_hash: str) -> dict:
    """Rebuild memory_relationships after T3 (user_memories) sync.

    Connects facts with cosine similarity > 0.7 via embedding.
    Tracked in memory_consolidation_log.relationships_rebuilt_at (invariant 3).
    """
    if not MEMORY_REPLICATION_ENABLED:
        return {"rebuilt": 0, "skipped": "disabled"}

    built = 0
    try:
        async with pool.store.pool.acquire() as conn:
            # Find fact pairs with high cosine similarity (pgvector <=> operator
            # returns cosine distance; similarity = 1 - distance)
            await conn.execute("""
                INSERT INTO memory_relationships
                    (token_hash, source_id, target_id, source_table,
                     target_table, relation_type, strength)
                SELECT a.token_hash, a.id, b.id,
                       'user_memories', 'user_memories', 'similar_to',
                       (1 - (a.embedding <=> b.embedding))::real
                  FROM user_memories a
                  JOIN user_memories b
                    ON a.token_hash = b.token_hash
                   AND a.id < b.id
                 WHERE a.token_hash = $1
                   AND (1 - (a.embedding <=> b.embedding)) > 0.7
                ON CONFLICT DO NOTHING
            """, token_hash)

            # Track invariant 3: relationships were rebuilt
            await conn.execute("""
                INSERT INTO memory_consolidation_log
                    (token_hash, event_type, details, relationships_rebuilt_at)
                VALUES ($1, 'relationships_regen', $2, now())
            """, token_hash, json.dumps({"source": "replication_trigger"}))

        log.info(f"RELATIONSHIPS: regen complete for {token_hash[:8]}...")
        return {"rebuilt": "ok", "token_hash_prefix": token_hash[:8]}
    except Exception as e:
        log.warning(f"RELATIONSHIPS: regen failed for {token_hash[:8]}: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Pull-based sync: fetch updates since last sync from each peer
# Invariant 4: since_ts + count fingerprint (NOT merkle — avoids Bug A)
# ---------------------------------------------------------------------------

async def pull_from_peer(pool, peer: dict, table: str,
                          limit: int = None) -> dict:
    """Pull updates since last_sync_*_ts for a specific table from one peer.

    Returns {ingested, skipped, duration_ms, fingerprint_match}.
    """
    if not MEMORY_REPLICATION_ENABLED:
        return {"skipped": "disabled"}

    if table not in ("conversations", "agent_episodes",
                      "user_memories", "agent_procedures"):
        return {"error": f"invalid table: {table}"}

    ts_column = {
        "conversations": "last_sync_conv_ts",
        "agent_episodes": "last_sync_episode_ts",
        "user_memories": "last_sync_memory_ts",
        "agent_procedures": "last_sync_memory_ts",  # same cursor as T3
    }[table]

    limit = limit or {
        "conversations": PAGE_CONVERSATIONS,
        "agent_episodes": PAGE_EPISODES,
        "user_memories": PAGE_MEMORIES,
        "agent_procedures": PAGE_MEMORIES,
    }[table]

    # Get our current cursor for this peer/table
    try:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {ts_column} FROM federation_peers WHERE atom_id=$1",
                peer["atom_id"])
            since_ts = row[ts_column] if row else None
    except Exception as e:
        log.warning(f"PULL: cursor fetch failed: {e}")
        return {"error": str(e)}

    # Build request
    from .federation import _load_privkey_from_disk, build_envelope_headers
    from pathlib import Path
    if pool.federation_self is None:
        return {"skipped": "no_federation"}
    priv_raw = _load_privkey_from_disk(Path(pool.federation_self.privkey_path))
    if priv_raw is None:
        return {"skipped": "no_privkey"}

    since_str = since_ts.isoformat() if since_ts else ""
    path = f"/v1/federation/memory/since?table={table}&since={since_str}&limit={limit}"
    body = b""
    import aiohttp

    t0 = time.time()
    try:
        base = peer["url"].rstrip("/")
        headers = build_envelope_headers(
            priv_raw, pool.federation_self.atom_id,
            "GET", path, body, hop=0, chain=[])
        timeout = aiohttp.ClientTimeout(total=20.0)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{base}{path}", headers=headers) as r:
                dur_ms = int((time.time() - t0) * 1000)
                if r.status != 200:
                    await _record_peer_latency(pool, peer["atom_id"], dur_ms, False)
                    return {"error": f"http_{r.status}", "duration_ms": dur_ms}
                data = await r.json()

        await _record_peer_latency(pool, peer["atom_id"], dur_ms, True)

        # Ingest the received rows
        rows = data.get("rows", [])
        latest_ts = data.get("latest_ts")  # cursor for next pull
        count = data.get("count", len(rows))

        # Dispatch to correct ingest
        if table == "conversations":
            res = await ingest_conversations(
                pool, {"conversations": rows, "origin_pool_id": peer["atom_id"]},
                peer["atom_id"])
        elif table == "agent_episodes":
            res = await ingest_episodes(
                pool, {"episodes": rows, "origin_pool_id": peer["atom_id"]},
                peer["atom_id"])
        else:  # user_memories or agent_procedures (use existing ingest)
            res = await ingest_memories(
                pool, {"table": table, "memories": rows,
                       "origin_pool_id": peer["atom_id"]}, peer["atom_id"])

        # Update cursor if we received rows
        if latest_ts and rows:
            try:
                async with pool.store.pool.acquire() as conn:
                    await conn.execute(
                        f"UPDATE federation_peers SET {ts_column}=$1 "
                        f"WHERE atom_id=$2", latest_ts, peer["atom_id"])
            except Exception as e:
                log.warning(f"PULL: cursor update failed: {e}")

        # Trigger relationships regen if T3 was pulled
        if table == "user_memories" and res.get("ingested", 0) > 0:
            # Regen relationships for affected token_hashes
            token_hashes = {r.get("token_hash") for r in rows if r.get("token_hash")}
            for th in token_hashes:
                await regenerate_relationships_for_token(pool, th)

        await _log_replication_activity(
            pool, peer["atom_id"], "pull", table,
            res.get("ingested", 0), dur_ms, True)

        return {"table": table, "ingested": res.get("ingested", 0),
                "skipped": res.get("skipped", 0),
                "server_count": count, "duration_ms": dur_ms,
                "cursor_advanced": bool(latest_ts and rows)}
    except Exception as e:
        dur_ms = int((time.time() - t0) * 1000)
        await _record_peer_latency(pool, peer["atom_id"], dur_ms, False)
        await _log_replication_activity(
            pool, peer["atom_id"], "pull", table, 0, dur_ms, False, str(e)[:200])
        return {"error": str(e)[:200], "duration_ms": dur_ms}


# ---------------------------------------------------------------------------
# Bootstrap: resumable initial sync from a peer
# Invariant 2: replication_bootstrap_state per table/peer
# ---------------------------------------------------------------------------

async def bootstrap_from_peer(pool, peer_atom_id: str,
                                peer_url: str = None) -> dict:
    """Bootstrap all replicated tables from a newly-bonded peer.

    Resumable: uses replication_bootstrap_state to track progress per table.
    Sequential by table (parallel = FK incoherence, per guardian Q5).
    """
    if not MEMORY_REPLICATION_ENABLED:
        return {"skipped": "disabled"}

    # Mark bootstrap in progress
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                "UPDATE federation_peers SET bootstrap_state='in_progress' "
                "WHERE atom_id=$1", peer_atom_id)

            if peer_url is None:
                r = await conn.fetchrow(
                    "SELECT url FROM federation_peers WHERE atom_id=$1",
                    peer_atom_id)
                peer_url = r["url"] if r else None

        if not peer_url:
            return {"error": "peer not found"}

        peer = {"atom_id": peer_atom_id, "url": peer_url}

        # Sequential bootstrap by table (accounts already handled by M11.1)
        results = {}
        for table in ("user_memories", "agent_procedures",
                       "agent_episodes", "conversations"):
            log.info(f"BOOTSTRAP: {table} from {peer_atom_id[:12]}...")
            res = await _bootstrap_table(pool, peer, table)
            results[table] = res

        # Mark bootstrap complete
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                "UPDATE federation_peers SET bootstrap_state='complete' "
                "WHERE atom_id=$1", peer_atom_id)

        log.info(f"BOOTSTRAP COMPLETE for {peer_atom_id[:12]}: {results}")
        return {"status": "complete", "tables": results}

    except Exception as e:
        log.error(f"BOOTSTRAP FAILED for {peer_atom_id[:12]}: {e}")
        try:
            async with pool.store.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE federation_peers SET bootstrap_state='failed' "
                    "WHERE atom_id=$1", peer_atom_id)
        except Exception:
            pass
        return {"error": str(e)}


async def _bootstrap_table(pool, peer: dict, table: str) -> dict:
    """Bootstrap a single table with resumable pagination."""
    peer_id = peer["atom_id"]
    total_received = 0
    pages = 0
    max_pages = 10000  # safety cap

    # Resume from existing cursor if present
    try:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT cursor, rows_received, completed_at
                  FROM replication_bootstrap_state
                 WHERE peer_atom_id=$1 AND table_name=$2
            """, peer_id, table)
            if row and row["completed_at"]:
                return {"status": "already_complete",
                        "rows": row["rows_received"]}
            cursor = row["cursor"] if row else None
            total_received = row["rows_received"] if row else 0
            if not row:
                await conn.execute("""
                    INSERT INTO replication_bootstrap_state
                        (peer_atom_id, table_name, cursor)
                    VALUES ($1, $2, NULL)
                """, peer_id, table)
    except Exception as e:
        return {"error": f"state init: {e}"}

    while pages < max_pages:
        res = await pull_from_peer(pool, peer, table, limit=PAGE_BOOTSTRAP)
        if res.get("error"):
            # Save failure and return — bootstrap is resumable later
            try:
                async with pool.store.pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE replication_bootstrap_state
                           SET last_error = $1, rows_received = $2
                         WHERE peer_atom_id = $3 AND table_name = $4
                    """, res["error"], total_received, peer_id, table)
            except Exception:
                pass
            return {"status": "interrupted", "rows": total_received,
                    "error": res["error"]}

        ingested = res.get("ingested", 0)
        server_count = res.get("server_count", 0)
        total_received += ingested
        pages += 1

        # Stop when peer returns fewer rows than page size (caught up)
        if server_count < PAGE_BOOTSTRAP:
            break
        if not res.get("cursor_advanced"):
            # Cursor didn't advance — stuck, stop to avoid infinite loop
            break

    # Mark complete
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute("""
                UPDATE replication_bootstrap_state
                   SET completed_at = now(), rows_received = $1
                 WHERE peer_atom_id = $2 AND table_name = $3
            """, total_received, peer_id, table)
    except Exception:
        pass

    return {"status": "complete", "rows": total_received, "pages": pages}


# ---------------------------------------------------------------------------
# Unified /memory/since pull endpoint helper
# ---------------------------------------------------------------------------

async def serve_since_query(pool, table: str, since_ts_str: str,
                              limit: int = PAGE_BOOTSTRAP) -> dict:
    """Serve a GET /memory/since query — return rows + latest cursor + count."""
    if not MEMORY_REPLICATION_ENABLED:
        return {"rows": [], "count": 0, "latest_ts": None, "error": "disabled"}

    from datetime import datetime
    since_ts = None
    if since_ts_str:
        try:
            since_ts = datetime.fromisoformat(since_ts_str)
        except Exception:
            since_ts = None

    if table == "conversations":
        rows = await _load_conversations_for_replication(
            pool, since_ts=since_ts, limit=limit)
        ts_field = "last_activity"
    elif table == "agent_episodes":
        rows = await _load_episodes_for_replication(
            pool, since_ts=since_ts, limit=limit)
        ts_field = "created_at"
    elif table == "user_memories":
        rows = await _load_memories_for_replication(
            pool, token_hash="_all_", memory_ids=None, table="user_memories")
        # Filter by since_ts in-memory for memories
        if since_ts:
            rows = [r for r in rows if r.get("created") and r["created"] > since_ts]
        rows = rows[:limit]
        ts_field = "created"
    elif table == "agent_procedures":
        rows = await _load_memories_for_replication(
            pool, token_hash="_all_", memory_ids=None, table="agent_procedures")
        if since_ts:
            rows = [r for r in rows if r.get("created_at")
                    and r["created_at"] > since_ts]
        rows = rows[:limit]
        ts_field = "created_at"
    else:
        return {"rows": [], "count": 0, "error": f"unknown table {table}"}

    latest_ts = None
    if rows:
        latest = max((r.get(ts_field) for r in rows if r.get(ts_field)),
                      default=None)
        latest_ts = latest.isoformat() if latest else None

    return {
        "table": table,
        "rows": rows,
        "count": len(rows),
        "latest_ts": latest_ts,
    }


# ---------------------------------------------------------------------------
# Extend propagate_forget to cover conversations
# (patches the existing handle_memory_forget by adding conversations delete)
# ---------------------------------------------------------------------------

async def handle_memory_forget_v2(pool, payload: dict, from_atom_id: str) -> dict:
    """Extended version — also purges conversations tied to this token.

    Kept as v2 function to avoid modifying the original signature.
    The routes/memory.py layer should call this one.
    """
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
                "DELETE FROM memory_relationships WHERE token_hash = $1", th)
            deleted["relationships"] = int(r.split()[-1])
            r = await conn.execute(
                "DELETE FROM memory_consolidation_log WHERE token_hash = $1", th)
            deleted["consolidation_logs"] = int(r.split()[-1])
            # Conversations — match via accounts.token_hash relationship
            r = await conn.execute("""
                DELETE FROM conversations c
                 WHERE EXISTS (
                     SELECT 1 FROM accounts a
                      WHERE a.api_token = c.api_token
                        AND a.token_hash = $1)
            """, th)
            deleted["conversations"] = int(r.split()[-1])

        log.info(f"MEMORY REPL v2: forget executed for {th[:8]}... "
                 f"from {from_atom_id[:12]}... — {deleted}")
    except Exception as e:
        return {"error": str(e)}

    return {"status": "purged", "deleted": deleted}
