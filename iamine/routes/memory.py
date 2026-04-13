"""M13 Agent Memory — REST API routes (FastAPI).

GET  /v1/memory/status    — memory stats per tier
GET  /v1/memory/search    — hybrid search (?q=...&limit=10)
GET  /v1/memory/episodes  — list episodes
POST /v1/memory/observe   — manual observation (for MCP bridge)
POST /v1/memory/consolidate — force consolidation
DELETE /v1/memory/forget-all — RGPD purge all tiers
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("iamine.routes.memory")

router = APIRouter()


def _pool():
    from iamine.pool import pool
    return pool


@router.get("/v1/memory/status")
async def memory_status(api_token: str = ""):
    """Memory statistics per tier."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)

    from ..core.agent_memory import get_stats
    p = _pool()
    stats = await get_stats(p.store, api_token)
    return stats


@router.get("/v1/memory/search")
async def memory_search(q: str = "", limit: int = 5, api_token: str = ""):
    """Hybrid search across memory tiers."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)
    if not q.strip():
        return JSONResponse({"error": "missing ?q= parameter"}, status_code=400)

    limit = min(limit, 20)

    from ..core.agent_memory import search_episodes
    from ..memory import retrieve_context
    p = _pool()

    episodes = await search_episodes(p.store, api_token, q, limit)
    rag = await retrieve_context(p.store, api_token, q, limit)

    return {
        "query": q,
        "episodes": [
            {"id": ep["id"], "title": ep.get("title", ""),
             "similarity": round(ep.get("similarity", 0), 3),
             "observation_count": ep.get("observation_count", 0),
             "created_at": str(ep.get("created_at", ""))}
            for ep in episodes
        ],
        "semantic_facts_count": rag.count("- ") if rag else 0,
    }


@router.get("/v1/memory/episodes")
async def memory_episodes(limit: int = 10, offset: int = 0, api_token: str = ""):
    """List episodes for a user."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)

    limit = min(limit, 50)
    from ..memory import token_hash
    p = _pool()
    th = token_hash(api_token)

    try:
        async with p.store.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, conv_id, title, outcome, observation_count,
                       access_count, decay_factor, created_at
                FROM agent_episodes
                WHERE token_hash = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """, th, limit, offset)
        episodes = []
        for r in rows:
            ep = dict(r)
            ep["created_at"] = str(ep["created_at"])
            episodes.append(ep)
        return {"episodes": episodes, "count": len(episodes)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/v1/memory/observe")
async def memory_observe(request: Request, api_token: str = ""):
    """Manual observation injection (for MCP bridge)."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)

    data = await request.json()
    content = data.get("content", "").strip()
    if not content:
        return JSONResponse({"error": "missing content"}, status_code=400)

    source_type = data.get("source_type", "tool_call")
    conv_id = data.get("conv_id", "")
    metadata = data.get("metadata", {})

    from ..core.agent_memory import capture_observation
    p = _pool()
    obs_id = await capture_observation(
        p.store, api_token, source_type, content,
        conv_id=conv_id, metadata=metadata)

    if obs_id:
        return {"observation_id": obs_id, "status": "captured"}
    return JSONResponse({"error": "capture failed or memory disabled"}, status_code=400)


@router.post("/v1/memory/consolidate")
async def memory_consolidate(request: Request, api_token: str = ""):
    """Force consolidation of pending observations."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)

    data = await request.json()
    conv_id = data.get("conv_id", "")

    from ..core.agent_memory import consolidate_to_episode
    p = _pool()
    episode_id = await consolidate_to_episode(p, p.store, api_token, conv_id)

    if episode_id:
        return {"episode_id": episode_id, "status": "consolidated"}
    return {"status": "nothing_to_consolidate"}


@router.delete("/v1/memory/forget-all")
async def memory_forget_all(api_token: str = ""):
    """RGPD purge — delete all memory tiers for a user."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)

    from ..memory import token_hash
    p = _pool()
    th = token_hash(api_token)

    deleted = {}
    try:
        async with p.store.pool.acquire() as conn:
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
                "DELETE FROM memory_consolidation_log WHERE token_hash = $1", th)
            deleted["consolidation_logs"] = int(r.split()[-1])

        # --- M13 P4: Propagate RGPD forget to federation ---
        try:
            from ..core.memory_replication import propagate_forget, MEMORY_REPLICATION_ENABLED
            if MEMORY_REPLICATION_ENABLED:
                import asyncio
                asyncio.create_task(propagate_forget(p, th))
        except Exception:
            pass

        return {"status": "purged", "deleted": deleted}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/v1/memory/procedures")
async def memory_procedures(limit: int = 10, api_token: str = ""):
    """List active procedures for a user."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)

    from ..memory import token_hash
    p = _pool()
    th = token_hash(api_token)

    try:
        async with p.store.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, name, trigger_pattern, success_rate,
                       use_count, last_used, created_at
                FROM agent_procedures
                WHERE token_hash = $1 AND active = TRUE
                ORDER BY use_count DESC
                LIMIT $2
            """, th, min(limit, 50))
        procs = []
        for r in rows:
            proc = dict(r)
            proc["last_used"] = str(proc["last_used"]) if proc["last_used"] else None
            proc["created_at"] = str(proc["created_at"])
            procs.append(proc)
        return {"procedures": procs, "count": len(procs)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/v1/memory/graph")
async def memory_graph(fact_id: int = 0, depth: int = 1, api_token: str = ""):
    """Get relationship graph around a memory fact."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)
    if not fact_id:
        return JSONResponse({"error": "missing ?fact_id= parameter"}, status_code=400)

    from ..memory import token_hash
    p = _pool()
    th = token_hash(api_token)

    try:
        async with p.store.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT r.id, r.source_id, r.target_id,
                       r.source_table, r.target_table,
                       r.relation_type, r.strength
                FROM memory_relationships r
                WHERE r.token_hash = $1
                  AND (r.source_id = $2 OR r.target_id = $2)
                LIMIT 20
            """, th, fact_id)
        edges = [dict(r) for r in rows]
        return {"fact_id": fact_id, "edges": edges, "count": len(edges)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/v1/memory/decay")
async def memory_decay(api_token: str = ""):
    """Trigger manual decay sweep."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)

    from ..memory import token_hash
    from ..core.agent_memory_phase2 import apply_decay
    p = _pool()
    th = token_hash(api_token)
    affected = await apply_decay(p.store, th)
    return {"affected": affected, "status": "decay_applied"}


# ---------------------------------------------------------------------------
# Federation memory ingest endpoints (signed, peer-to-peer)
# ---------------------------------------------------------------------------

@router.post("/v1/federation/memory/ingest")
async def federation_memory_ingest(request: Request):
    """Receive replicated memories from a bonded peer.
    Requires Ed25519 signed envelope (same as account ingest)."""
    # Verify federation signature
    p = _pool()
    from ..core.federation import verify_request_signature
    from_atom_id = request.headers.get("X-Federation-Atom-Id", "")
    if not from_atom_id:
        return JSONResponse({"error": "missing federation headers"}, status_code=403)

    # Check trust level
    peers = p.federation_peers if hasattr(p, 'federation_peers') else {}
    peer = peers.get(from_atom_id, {})
    if peer.get("trust_level", 0) < 3:
        return JSONResponse({"error": "insufficient trust"}, status_code=403)

    data = await request.json()
    from ..core.memory_replication import handle_memory_ingest
    result = await handle_memory_ingest(p, data, from_atom_id)
    return result


@router.post("/v1/federation/memory/forget")
async def federation_memory_forget(request: Request):
    """Receive RGPD forget tombstone from a bonded peer."""
    p = _pool()
    from_atom_id = request.headers.get("X-Federation-Atom-Id", "")
    if not from_atom_id:
        return JSONResponse({"error": "missing federation headers"}, status_code=403)

    peers = p.federation_peers if hasattr(p, 'federation_peers') else {}
    peer = peers.get(from_atom_id, {})
    if peer.get("trust_level", 0) < 3:
        return JSONResponse({"error": "insufficient trust"}, status_code=403)

    data = await request.json()
    from ..core.memory_replication import handle_memory_forget
    result = await handle_memory_forget(p, data, from_atom_id)
    return result
