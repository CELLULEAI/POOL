"""M13 Agent Memory — 4-tier memory system for distributed sub-agents.

Phase 1: Working memory (observations) + Episodic memory (session summaries).
Auto-capture on every inference, sub-agent review, and compaction.
Consolidation: observations -> episodes via distributed LLM summarization.

Feature flag: AGENT_MEMORY_ENABLED env var (default: false).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from ..memory import embed_text, embed_batch, token_hash

log = logging.getLogger("iamine.agent_memory")

ENABLED = os.environ.get("AGENT_MEMORY_ENABLED", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Encryption helpers (same pattern as db.py — zero-knowledge)
# ---------------------------------------------------------------------------

def _encrypt(text: str, api_token: str) -> tuple[str, str]:
    """Encrypt text with PBKDF2 + random salt. Returns (ciphertext, salt_b64)."""
    import base64
    from ..db import _derive_key, _SALT_SIZE
    salt = os.urandom(_SALT_SIZE)
    key = _derive_key(api_token, salt)
    from cryptography.fernet import Fernet
    f = Fernet(key)
    encrypted = f.encrypt(text.encode("utf-8")).decode("ascii")
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    return encrypted, salt_b64


def _decrypt(enc: str, salt_b64: str, api_token: str) -> str:
    """Decrypt text. Returns empty string on failure."""
    from ..db import _decrypt_fact
    return _decrypt_fact(enc, salt_b64, api_token)


# ---------------------------------------------------------------------------
# TIER 1: Working Memory — capture observations
# ---------------------------------------------------------------------------

async def capture_observation(
    store, api_token: str, source_type: str, content: str,
    conv_id: str = "", job_id: str = "", source_id: str = "",
    metadata: dict | None = None, importance: float = 0.5
) -> int | None:
    """Capture a raw observation from any pool activity.

    Returns observation id or None if disabled/failed.
    """
    if not ENABLED:
        return None
    if not content or len(content.strip()) < 10:
        return None

    th = token_hash(api_token)
    enc, salt = _encrypt(content, api_token)

    # Embed async (non-blocking for short content)
    embedding = embed_text(content[:512])  # cap embedding input

    try:
        async with store.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO agent_observations
                    (token_hash, conv_id, job_id, source_type, source_id,
                     content_enc, salt, metadata, embedding, importance)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb,
                        $9::vector, $10)
                RETURNING id
            """, th, conv_id or "", job_id or "", source_type,
                source_id or "", enc, salt,
                __import__("json").dumps(metadata or {}),
                str(embedding) if embedding else None,
                importance)
            obs_id = row["id"]
            log.info(f"MEMORY: observation #{obs_id} captured "
                     f"[{source_type}] for {th[:8]}... ({len(content)} chars)")
            return obs_id
    except Exception as e:
        log.warning(f"MEMORY: capture_observation failed: {e}")
        return None


async def get_unconsolidated(store, token_hash_val: str,
                             conv_id: str = "", limit: int = 50) -> list[dict]:
    """Get unconsolidated observations for a user/conversation."""
    try:
        async with store.pool.acquire() as conn:
            query = """
                SELECT id, conv_id, source_type, source_id, content_enc,
                       salt, metadata, importance, created_at
                FROM agent_observations
                WHERE token_hash = $1 AND consolidated = FALSE
            """
            params = [token_hash_val]
            if conv_id:
                query += " AND conv_id = $2"
                params.append(conv_id)
            query += " ORDER BY created_at ASC LIMIT $" + str(len(params) + 1)
            params.append(limit)
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"MEMORY: get_unconsolidated failed: {e}")
        return []


# ---------------------------------------------------------------------------
# TIER 2: Episodic Memory — consolidate observations into episodes
# ---------------------------------------------------------------------------

CONSOLIDATION_PROMPT = """Summarize these observations from a coding session into a concise episode.
Focus on: what was done, what was the outcome, key decisions made.
Output a title (max 60 chars) on line 1, then a 2-4 sentence summary.

Observations:
{observations}"""


async def consolidate_to_episode(
    pool, store, api_token: str, conv_id: str,
    observations: list[dict] | None = None
) -> int | None:
    """Compress unconsolidated observations into an episode summary.

    Uses a pool worker to generate the summary (distributed LLM call).
    Returns episode id or None.
    """
    if not ENABLED:
        return None

    th = token_hash(api_token)

    if observations is None:
        observations = await get_unconsolidated(store, th, conv_id)

    if len(observations) < 3:
        return None  # too few to consolidate

    # Decrypt observations for summarization
    obs_texts = []
    obs_ids = []
    participants = set()
    for obs in observations:
        text = _decrypt(obs["content_enc"], obs["salt"], api_token)
        if text:
            src = obs.get("source_type", "?")
            obs_texts.append(f"[{src}] {text[:300]}")
            obs_ids.append(obs["id"])
            sid = obs.get("source_id", "")
            if sid:
                participants.add(sid)

    if not obs_texts:
        return None

    # Build consolidation prompt
    prompt = CONSOLIDATION_PROMPT.format(
        observations="\n".join(obs_texts[:20])  # cap at 20
    )

    # Use pool worker to summarize
    t0 = time.time()
    summary_text = None
    try:
        worker = pool.get_idle_worker(prefer_stronger=True)
        if worker:
            summary_text = await pool.delegate_task(
                helper=worker,
                task_type="memory_consolidation",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=256
            )
    except Exception as e:
        log.warning(f"MEMORY: consolidation LLM call failed: {e}")

    if not summary_text or len(str(summary_text)) < 20:
        # Fallback: simple concatenation
        summary_text = f"Session with {len(obs_texts)} observations. " + \
                       " | ".join(obs_texts[:5])[:500]

    summary_str = str(summary_text)
    duration_ms = int((time.time() - t0) * 1000)

    # Parse title from first line
    lines = summary_str.strip().split("\n", 1)
    title = lines[0][:256].strip("# ").strip()
    summary_body = lines[1].strip() if len(lines) > 1 else summary_str

    # Encrypt and embed
    enc, salt = _encrypt(summary_body, api_token)
    embedding = embed_text(summary_body[:512])
    if not embedding:
        log.warning("MEMORY: consolidation embedding failed, skipping episode")
        return None

    try:
        async with store.pool.acquire() as conn:
            # Insert episode
            row = await conn.fetchrow("""
                INSERT INTO agent_episodes
                    (token_hash, conv_id, title, summary_enc, salt,
                     embedding, participants, observation_count, importance)
                VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8, $9)
                RETURNING id
            """, th, conv_id, title, enc, salt,
                str(embedding), list(participants),
                len(obs_ids), 0.5)
            episode_id = row["id"]

            # Mark observations as consolidated
            await conn.execute("""
                UPDATE agent_observations SET consolidated = TRUE
                WHERE id = ANY($1::bigint[])
            """, obs_ids)

            # Audit log
            await conn.execute("""
                INSERT INTO memory_consolidation_log
                    (token_hash, consolidation_type, input_count,
                     output_id, tokens_used, duration_ms)
                VALUES ($1, 'observation_to_episode', $2, $3, 0, $4)
            """, th, len(obs_ids), episode_id, duration_ms)

            log.info(f"MEMORY: episode #{episode_id} created from "
                     f"{len(obs_ids)} observations for {th[:8]}... "
                     f"({duration_ms}ms)")
            return episode_id

    except Exception as e:
        log.warning(f"MEMORY: consolidate_to_episode DB failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Retrieval — search episodes for context injection
# ---------------------------------------------------------------------------

async def search_episodes(store, api_token: str, query: str,
                          limit: int = 3) -> list[dict]:
    """Search episodic memory by vector similarity."""
    if not ENABLED:
        return []

    embedding = embed_text(query)
    if not embedding:
        return []

    th = token_hash(api_token)
    try:
        async with store.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_episodes WHERE token_hash = $1", th)
            if not count:
                return []

            rows = await conn.fetch("""
                SELECT id, title, summary_enc, salt, conv_id,
                       1 - (embedding <=> $2::vector) AS similarity,
                       observation_count, outcome, created_at
                FROM agent_episodes
                WHERE token_hash = $1
                  AND decay_factor > 0.1
                  AND 1 - (embedding <=> $2::vector) > 0.3
                ORDER BY embedding <=> $2::vector
                LIMIT $3
            """, th, str(embedding), limit)

            if rows:
                ids = [r["id"] for r in rows]
                await conn.execute("""
                    UPDATE agent_episodes
                    SET last_accessed = NOW(), access_count = access_count + 1
                    WHERE id = ANY($1::bigint[])
                """, ids)

            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"MEMORY: search_episodes failed: {e}")
        return []


async def get_episode_context(store, api_token: str, query: str,
                              limit: int = 3) -> str:
    """Get formatted episode context for system prompt injection."""
    episodes = await search_episodes(store, api_token, query, limit)
    if not episodes:
        return ""

    parts = []
    for ep in episodes:
        text = _decrypt(ep["summary_enc"], ep["salt"], api_token)
        if text:
            title = ep.get("title", "Session")
            parts.append(f"- [{title}] {text[:200]}")

    if not parts:
        return ""

    result = "[Episodic memory — relevant past sessions]\n" + "\n".join(parts)
    if len(result) > 800:
        result = result[:800] + "..."
    return result


# ---------------------------------------------------------------------------
# Memory stats
# ---------------------------------------------------------------------------

async def get_stats(store, api_token: str) -> dict:
    """Get memory statistics for a user."""
    th = token_hash(api_token)
    try:
        async with store.pool.acquire() as conn:
            obs_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_observations WHERE token_hash = $1", th)
            obs_unconsolidated = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_observations "
                "WHERE token_hash = $1 AND consolidated = FALSE", th)
            ep_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_episodes WHERE token_hash = $1", th)
            sem_count = await conn.fetchval(
                "SELECT COUNT(*) FROM user_memories WHERE token_hash = $1", th)
            return {
                "observations": obs_count or 0,
                "observations_pending": obs_unconsolidated or 0,
                "episodes": ep_count or 0,
                "semantic_facts": sem_count or 0,
                "enabled": ENABLED,
            }
    except Exception as e:
        log.warning(f"MEMORY: get_stats failed: {e}")
        return {"observations": 0, "episodes": 0, "semantic_facts": 0,
                "enabled": ENABLED, "error": str(e)}


# ---------------------------------------------------------------------------
# Background consolidation loop
# ---------------------------------------------------------------------------

async def consolidation_loop(pool):
    """Background task: periodically consolidate observations into episodes.

    Runs every 5 minutes. Only consolidates when pool is not overloaded.
    """
    if not ENABLED:
        log.info("MEMORY: agent memory disabled, consolidation loop skipped")
        return

    log.info("MEMORY: consolidation loop started (interval=300s)")

    while True:
        await asyncio.sleep(300)

        try:
            # Skip if pool is busy (>80% workers occupied)
            busy = sum(1 for w in pool.workers.values() if w.busy)
            total = len(pool.workers)
            if total > 0 and busy / total > 0.8:
                log.debug("MEMORY: pool busy, skipping consolidation")
                continue

            store = pool.store
            async with store.pool.acquire() as conn:
                # Find users with pending observations
                rows = await conn.fetch("""
                    SELECT DISTINCT token_hash, conv_id, COUNT(*) as cnt
                    FROM agent_observations
                    WHERE consolidated = FALSE
                    GROUP BY token_hash, conv_id
                    HAVING COUNT(*) >= 5
                       OR MIN(created_at) < NOW() - INTERVAL '30 minutes'
                    LIMIT 10
                """)

            for row in rows:
                th = row["token_hash"]
                cid = row["conv_id"]
                # We need the api_token to decrypt — but we only have the hash.
                # Solution: observations are consolidated when the user's next
                # request comes in (they provide their token). For background
                # consolidation, we only mark observations as stale.
                # See: trigger_consolidation() called from pool.submit_job()
                log.debug(f"MEMORY: {row['cnt']} pending observations "
                          f"for {th[:8]}... conv={cid}")

        except Exception as e:
            log.warning(f"MEMORY: consolidation loop error: {e}")


async def trigger_consolidation(pool, store, api_token: str, conv_id: str):
    """Trigger consolidation for a specific user (called when they send a request).

    This is the primary consolidation path — the user provides their token
    so we can decrypt observations for LLM summarization.
    """
    if not ENABLED:
        return

    th = token_hash(api_token)
    try:
        async with store.pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM agent_observations
                WHERE token_hash = $1 AND consolidated = FALSE
            """, th)

        if count and count >= 5:
            log.info(f"MEMORY: triggering consolidation for {th[:8]}... "
                     f"({count} pending observations)")
            asyncio.create_task(
                consolidate_to_episode(pool, store, api_token, conv_id)
            )
    except Exception as e:
        log.warning(f"MEMORY: trigger_consolidation failed: {e}")


# --- Phase 2 re-exports ---
try:
    from .agent_memory_phase2 import (hybrid_retrieve, extract_semantic_facts,
                                       extract_procedures, apply_decay, full_consolidation)
except ImportError:
    pass  # Phase 2 not deployed yet
