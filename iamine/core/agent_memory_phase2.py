"""M13 Agent Memory — Phase 2 additions.

Semantic extraction (episodes -> facts), procedural patterns,
memory relationships, hybrid retrieval, decay sweep.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from ..memory import embed_text, embed_batch, token_hash

log = logging.getLogger("iamine.agent_memory")


# ---------------------------------------------------------------------------
# Prompts for LLM-driven extraction
# ---------------------------------------------------------------------------

SEMANTIC_EXTRACTION_PROMPT = """Extract durable facts from this session episode.
Output one fact per line, prefixed with a category tag.
Categories: [preference], [pattern], [tool], [architecture], [bug], [decision], [context]

Episode title: {title}
Episode summary: {summary}

Output format (one per line):
[category] fact text"""

PROCEDURE_EXTRACTION_PROMPT = """Analyze these session episodes and identify recurring workflows or decision patterns.
For each pattern found, output:
NAME: short name (max 60 chars)
TRIGGER: when this pattern applies (regex-safe description)
STEPS: numbered steps of the workflow
OUTCOME: typical outcome (success/failure/mixed)
---

Episodes:
{episodes}"""


# ---------------------------------------------------------------------------
# TIER 3: Semantic Memory — extract facts from episodes
# ---------------------------------------------------------------------------

async def extract_semantic_facts(pool, store, api_token: str,
                                  episode_id: int) -> list[int]:
    """Extract durable facts from an episode using LLM.

    Returns list of new user_memories ids.
    """
    from .agent_memory import _encrypt, _decrypt, ENABLED
    if not ENABLED:
        return []

    th = token_hash(api_token)

    # Load episode
    try:
        async with store.pool.acquire() as conn:
            ep = await conn.fetchrow(
                "SELECT title, summary_enc, salt FROM agent_episodes WHERE id = $1",
                episode_id)
    except Exception as e:
        log.warning(f"MEMORY P2: load episode {episode_id} failed: {e}")
        return []

    if not ep:
        return []

    summary = _decrypt(ep["summary_enc"], ep["salt"], api_token)
    if not summary:
        return []

    prompt = SEMANTIC_EXTRACTION_PROMPT.format(
        title=ep["title"] or "Untitled",
        summary=summary
    )

    # LLM extraction
    extracted_text = None
    try:
        worker = pool.get_idle_worker(prefer_stronger=True)
        if worker:
            extracted_text = await pool.delegate_task(
                helper=worker,
                task_type="semantic_extraction",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=512
            )
    except Exception as e:
        log.warning(f"MEMORY P2: semantic extraction LLM failed: {e}")

    if not extracted_text:
        return []

    # Parse facts
    fact_ids = []
    lines = str(extracted_text).strip().split("\n")
    facts_to_store = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 10:
            continue
        # Parse [category] fact
        category = "fact"
        text = line
        if line.startswith("[") and "]" in line:
            bracket_end = line.index("]")
            category = line[1:bracket_end].strip().lower()
            text = line[bracket_end+1:].strip()
        if len(text) > 5:
            facts_to_store.append((category, text))

    if not facts_to_store:
        return []

    # Embed all facts
    texts = [f[1] for f in facts_to_store]
    embeddings = embed_batch(texts)
    if not embeddings:
        return []

    # Store facts
    for (category, text), emb in zip(facts_to_store, embeddings):
        enc, salt = _encrypt(text, api_token)
        try:
            async with store.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO user_memories
                        (token_hash, embedding, fact_text_enc, salt, category,
                         confidence, source_episode_id)
                    VALUES ($1, $2::vector, $3, $4, $5, 0.8, $6)
                    RETURNING id
                """, th, str(emb), enc, salt, category, episode_id)
                fact_ids.append(row["id"])
        except Exception as e:
            log.warning(f"MEMORY P2: store fact failed: {e}")

    if fact_ids:
        log.info(f"MEMORY P2: {len(fact_ids)} facts extracted from episode "
                 f"#{episode_id} for {th[:8]}...")

        # --- M13 P4: Auto-replicate new facts to federation ---
        try:
            from .memory_replication import auto_replicate_new_facts
            asyncio.create_task(auto_replicate_new_facts(pool, th, fact_ids))
        except Exception:
            pass

        # Build relationships between new facts
        asyncio.create_task(
            build_relationships(store, th, fact_ids))

    return fact_ids


# ---------------------------------------------------------------------------
# Memory relationships — automatic edge creation
# ---------------------------------------------------------------------------

async def build_relationships(store, token_hash_val: str,
                               new_fact_ids: list[int],
                               similarity_threshold: float = 0.7) -> int:
    """Create relationship edges between new facts and existing memories.

    Uses embedding cosine similarity to detect related facts.
    Returns number of relationships created.
    """
    if not new_fact_ids:
        return 0

    created = 0
    try:
        async with store.pool.acquire() as conn:
            for fact_id in new_fact_ids:
                # Find similar existing facts (not self)
                rows = await conn.fetch("""
                    SELECT id, 1 - (embedding <=> (
                        SELECT embedding FROM user_memories WHERE id = $1
                    )) AS similarity
                    FROM user_memories
                    WHERE token_hash = $2 AND id != $1
                      AND 1 - (embedding <=> (
                          SELECT embedding FROM user_memories WHERE id = $1
                      )) > $3
                    ORDER BY similarity DESC
                    LIMIT 5
                """, fact_id, token_hash_val, similarity_threshold)

                for r in rows:
                    await conn.execute("""
                        INSERT INTO memory_relationships
                            (token_hash, source_id, target_id,
                             source_table, target_table, relation_type, strength)
                        VALUES ($1, $2, $3, 'user_memories', 'user_memories',
                                'related_to', $4)
                        ON CONFLICT DO NOTHING
                    """, token_hash_val, fact_id, r["id"],
                        float(r["similarity"]))
                    created += 1

        if created:
            log.info(f"MEMORY P2: {created} relationships created for {token_hash_val[:8]}...")
    except Exception as e:
        log.warning(f"MEMORY P2: build_relationships failed: {e}")

    return created


# ---------------------------------------------------------------------------
# TIER 4: Procedural Memory — detect workflow patterns
# ---------------------------------------------------------------------------

async def extract_procedures(pool, store, api_token: str,
                              episode_ids: list[int] | None = None) -> list[int]:
    """Detect recurring patterns across episodes. LLM-driven.

    Returns list of new procedure ids.
    """
    from .agent_memory import _encrypt, _decrypt, ENABLED
    if not ENABLED:
        return []

    th = token_hash(api_token)

    # Load recent episodes
    try:
        async with store.pool.acquire() as conn:
            if episode_ids:
                rows = await conn.fetch("""
                    SELECT id, title, summary_enc, salt, outcome
                    FROM agent_episodes WHERE id = ANY($1::bigint[])
                """, episode_ids)
            else:
                rows = await conn.fetch("""
                    SELECT id, title, summary_enc, salt, outcome
                    FROM agent_episodes
                    WHERE token_hash = $1
                    ORDER BY created_at DESC LIMIT 10
                """, th)
    except Exception as e:
        log.warning(f"MEMORY P2: load episodes for procedure extraction failed: {e}")
        return []

    if len(rows) < 3:
        return []  # Need at least 3 episodes for patterns

    # Decrypt summaries
    episode_texts = []
    for r in rows:
        text = _decrypt(r["summary_enc"], r["salt"], api_token)
        if text:
            episode_texts.append(
                f"[{r['title'] or 'Untitled'}] ({r['outcome']}): {text[:200]}")

    if len(episode_texts) < 3:
        return []

    prompt = PROCEDURE_EXTRACTION_PROMPT.format(
        episodes="\n".join(episode_texts)
    )

    # LLM extraction
    result_text = None
    try:
        worker = pool.get_idle_worker(prefer_stronger=True)
        if worker:
            result_text = await pool.delegate_task(
                helper=worker,
                task_type="procedure_extraction",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=512
            )
    except Exception as e:
        log.warning(f"MEMORY P2: procedure extraction LLM failed: {e}")

    if not result_text:
        return []

    # Parse procedures
    procedure_ids = []
    blocks = str(result_text).split("---")

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        name = ""
        trigger = ""
        steps = ""
        for line in block.split("\n"):
            if line.startswith("NAME:"):
                name = line[5:].strip()[:256]
            elif line.startswith("TRIGGER:"):
                trigger = line[8:].strip()
            elif line.startswith("STEPS:"):
                steps = line[6:].strip()
            elif steps and (line.startswith("  ") or line[0:1].isdigit()):
                steps += "\n" + line.strip()

        if not name or not steps:
            continue

        desc_enc, desc_salt = _encrypt(name, api_token)
        steps_enc, _ = _encrypt(steps, api_token)
        # Reuse desc_salt for steps (same user key)
        embedding = embed_text(f"{name} {trigger} {steps}"[:512])
        if not embedding:
            continue

        try:
            async with store.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO agent_procedures
                        (token_hash, name, description_enc, salt,
                         trigger_pattern, steps_enc, embedding)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                    RETURNING id
                """, th, name, desc_enc, desc_salt, trigger,
                    steps_enc, str(embedding))
                procedure_ids.append(row["id"])
        except Exception as e:
            log.warning(f"MEMORY P2: store procedure failed: {e}")

    if procedure_ids:
        log.info(f"MEMORY P2: {len(procedure_ids)} procedures extracted "
                 f"for {th[:8]}...")

        # --- M13 P4: Auto-replicate procedures to federation ---
        try:
            from .memory_replication import auto_replicate_procedures
            asyncio.create_task(auto_replicate_procedures(pool, th, procedure_ids))
        except Exception:
            pass

    return procedure_ids


# ---------------------------------------------------------------------------
# Hybrid Retrieval — vector + trigram + graph
# ---------------------------------------------------------------------------

async def hybrid_retrieve(store, api_token: str, query: str,
                           limit: int = 10) -> str:
    """Hybrid retrieval across all memory tiers.

    Combines:
    - Vector similarity on semantic facts (user_memories)
    - Vector similarity on episodes (agent_episodes)
    - Relationship graph traversal
    - Procedural memory matching

    Returns formatted context string for system prompt injection.
    """
    from .agent_memory import _decrypt, ENABLED
    from ..db import _decrypt_fact
    if not ENABLED:
        # Fallback to original RAG
        from ..memory import retrieve_context
        return await retrieve_context(store, api_token, query, limit)

    query_emb = embed_text(query)
    if not query_emb:
        return ""

    th = token_hash(api_token)
    parts = []

    try:
        async with store.pool.acquire() as conn:
            # --- 1. Semantic facts (vector search) ---
            fact_count = await conn.fetchval(
                "SELECT COUNT(*) FROM user_memories WHERE token_hash = $1", th)
            if fact_count:
                facts = await conn.fetch("""
                    SELECT id, fact_text_enc, salt, category,
                           1 - (embedding <=> $2::vector) AS similarity
                    FROM user_memories
                    WHERE token_hash = $1
                      AND 1 - (embedding <=> $2::vector) > 0.3
                      AND (superseded_by IS NULL)
                      AND decay_factor > 0.1
                    ORDER BY embedding <=> $2::vector
                    LIMIT $3
                """, th, str(query_emb), limit)

                if facts:
                    await conn.execute("""
                        UPDATE user_memories SET last_accessed = NOW(),
                               access_count = access_count + 1
                        WHERE id = ANY($1::bigint[])
                    """, [f["id"] for f in facts])

                    fact_lines = []
                    for f in facts:
                        text = _decrypt_fact(f["fact_text_enc"], f["salt"], api_token)
                        if text:
                            cat = f.get("category", "fact")
                            fact_lines.append(f"- [{cat}] {text}")
                    if fact_lines:
                        parts.append("[Semantic memory — facts]\n" + "\n".join(fact_lines[:7]))

            # --- 2. Episodes (vector search) ---
            ep_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_episodes WHERE token_hash = $1", th)
            if ep_count:
                episodes = await conn.fetch("""
                    SELECT id, title, summary_enc, salt,
                           1 - (embedding <=> $2::vector) AS similarity
                    FROM agent_episodes
                    WHERE token_hash = $1
                      AND decay_factor > 0.1
                      AND 1 - (embedding <=> $2::vector) > 0.3
                    ORDER BY embedding <=> $2::vector
                    LIMIT 3
                """, th, str(query_emb))

                if episodes:
                    await conn.execute("""
                        UPDATE agent_episodes SET last_accessed = NOW(),
                               access_count = access_count + 1
                        WHERE id = ANY($1::bigint[])
                    """, [e["id"] for e in episodes])

                    ep_lines = []
                    for e in episodes:
                        text = _decrypt(e["summary_enc"], e["salt"], api_token)
                        if text:
                            ep_lines.append(f"- [{e['title'] or 'Session'}] {text[:150]}")
                    if ep_lines:
                        parts.append("[Episodic memory — past sessions]\n" + "\n".join(ep_lines))

            # --- 3. Related facts via graph (1-hop from top fact) ---
            if facts:
                top_fact_id = facts[0]["id"]
                related = await conn.fetch("""
                    SELECT DISTINCT m.id, m.fact_text_enc, m.salt, m.category,
                           r.relation_type, r.strength
                    FROM memory_relationships r
                    JOIN user_memories m ON (
                        (r.target_table = 'user_memories' AND r.target_id = m.id)
                        OR (r.source_table = 'user_memories' AND r.source_id = m.id)
                    )
                    WHERE r.token_hash = $1
                      AND (r.source_id = $2 OR r.target_id = $2)
                      AND m.id != $2
                    LIMIT 3
                """, th, top_fact_id)

                if related:
                    rel_lines = []
                    for r in related:
                        text = _decrypt_fact(r["fact_text_enc"], r["salt"], api_token)
                        if text:
                            rel_lines.append(f"- (related) {text[:100]}")
                    if rel_lines:
                        parts.append("[Related facts]\n" + "\n".join(rel_lines))

            # --- 4. Procedures ---
            proc_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_procedures "
                "WHERE token_hash = $1 AND active = TRUE", th)
            if proc_count:
                procs = await conn.fetch("""
                    SELECT id, name, trigger_pattern,
                           1 - (embedding <=> $2::vector) AS similarity
                    FROM agent_procedures
                    WHERE token_hash = $1 AND active = TRUE
                      AND 1 - (embedding <=> $2::vector) > 0.35
                    ORDER BY embedding <=> $2::vector
                    LIMIT 2
                """, th, str(query_emb))

                if procs:
                    await conn.execute("""
                        UPDATE agent_procedures SET last_used = NOW(),
                               use_count = use_count + 1
                        WHERE id = ANY($1::bigint[])
                    """, [p["id"] for p in procs])

                    proc_lines = [f"- [{p['name']}] trigger: {p.get('trigger_pattern', '?')}"
                                  for p in procs]
                    if proc_lines:
                        parts.append("[Procedures — known workflows]\n" + "\n".join(proc_lines))

    except Exception as e:
        log.warning(f"MEMORY P2: hybrid_retrieve failed: {e}")
        # Fallback to basic RAG
        from ..memory import retrieve_context
        return await retrieve_context(store, api_token, query, limit)

    if not parts:
        return ""

    result = "\n\n".join(parts)
    # Cap total context to ~2000 chars (~500 tokens)
    if len(result) > 2000:
        result = result[:2000] + "..."
    return result


# ---------------------------------------------------------------------------
# Decay sweep — Ebbinghaus forgetting curve
# ---------------------------------------------------------------------------

async def apply_decay(store, token_hash_val: str,
                       half_life_hours: float = 168.0) -> int:
    """Apply Ebbinghaus decay to episodic and semantic memories.

    Half-life default = 1 week (168h).
    Memories accessed recently get boosted. Unaccessed ones decay.
    Returns number of memories affected.
    """
    affected = 0
    try:
        async with store.pool.acquire() as conn:
            # Wrap in explicit transaction so pg_advisory_xact_lock holds across
            # all UPDATEs. Without this, asyncpg autocommits each execute and
            # the lock is released immediately after the first statement.
            async with conn.transaction():
                # Advisory lock: serialize apply_decay runs for the same user so
                # concurrent invocations wait their turn instead of deadlocking on
                # overlapping rows of user_memories/agent_episodes.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", token_hash_val)
                # Lock ordering fix: always acquire user_memories lock BEFORE agent_episodes
                # (hybrid_retrieve does user_memories then agent_episodes — must match).
                    # Decay semantic facts
                affected += int((await conn.execute("""
                    UPDATE user_memories
                    SET decay_factor = GREATEST(0.01,
                        decay_factor * POWER(0.5,
                            EXTRACT(EPOCH FROM (NOW() - COALESCE(last_accessed, created)))
                            / ($2 * 3600.0)
                        )
                    )
                    WHERE token_hash = $1 AND decay_factor > 0.01
                """, token_hash_val, half_life_hours)).split()[-1])

                # Boost recently accessed semantic facts
                await conn.execute("""
                    UPDATE user_memories
                    SET decay_factor = LEAST(1.0, decay_factor * 1.5)
                    WHERE token_hash = $1
                      AND last_accessed > NOW() - INTERVAL '24 hours'
                      AND access_count > 5
                """, token_hash_val)

                # Decay episodes
                affected += int((await conn.execute("""
                    UPDATE agent_episodes
                    SET decay_factor = GREATEST(0.01,
                        decay_factor * POWER(0.5,
                            EXTRACT(EPOCH FROM (NOW() - COALESCE(last_accessed, created_at)))
                            / ($2 * 3600.0)
                        )
                    )
                    WHERE token_hash = $1 AND decay_factor > 0.01
                """, token_hash_val, half_life_hours)).split()[-1])

                # Boost recently accessed episodes
                await conn.execute("""
                    UPDATE agent_episodes
                    SET decay_factor = LEAST(1.0, decay_factor * 1.5)
                    WHERE token_hash = $1
                      AND last_accessed > NOW() - INTERVAL '24 hours'
                      AND access_count > 5
                """, token_hash_val)

    except Exception as e:
        log.warning(f"MEMORY P2: decay sweep failed: {e}")

    if affected:
        log.info(f"MEMORY P2: decay sweep affected {affected} memories "
                 f"for {token_hash_val[:8]}...")
    return affected


# ---------------------------------------------------------------------------
# Enhanced consolidation — Phase 2 additions
# ---------------------------------------------------------------------------

async def full_consolidation(pool, store, api_token: str, conv_id: str):
    """Full consolidation pipeline:
    1. Observations -> Episode (Phase 1)
    2. Episode -> Semantic facts (Phase 2)
    3. Episodes -> Procedures (Phase 2, if enough episodes)
    4. Decay sweep
    """
    from .agent_memory import consolidate_to_episode, ENABLED
    if not ENABLED:
        return

    th = token_hash(api_token)

    # Step 1: Create episode from observations
    episode_id = await consolidate_to_episode(pool, store, api_token, conv_id)

    if episode_id:
        # Step 2: Extract semantic facts from the new episode
        await extract_semantic_facts(pool, store, api_token, episode_id)

        # Step 3: Check if we should extract procedures (every 5 episodes)
        try:
            async with store.pool.acquire() as conn:
                ep_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_episodes WHERE token_hash = $1", th)
            if ep_count and ep_count % 5 == 0:
                await extract_procedures(pool, store, api_token)
        except Exception:
            pass

    # Step 4: Decay sweep
    await apply_decay(store, th)
