"""Conversation and memory endpoints — extracted from pool.py."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _pool():
    from iamine.pool import pool
    return pool


@router.delete("/v1/conversation/{conv_id}")
async def delete_conversation(conv_id: str, api_token: str = ""):
    """Supprime une conversation — zero trace (L1+L2+L3). Vérifie le propriétaire."""
    p = _pool()
    conv = p.router._conversations.get(conv_id)
    if conv and conv.api_token and conv.api_token != api_token:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    p.router.delete_conversation(conv_id)
    await p.store.delete_conversation(conv_id)
    return {"status": "deleted", "conv_id": conv_id}


@router.get("/v1/conversations")
async def list_conversations(api_token: str = ""):
    """Liste les conversations persistantes d'un utilisateur authentifie."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required (acc_*)"}, status_code=401)
    try:
        p = _pool()
        convs = await p.store.list_conversations(api_token, limit=50)
        return {"conversations": convs, "count": len(convs)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/v1/conversations/{conv_id}")
async def get_conversation(conv_id: str, api_token: str = ""):
    """Charge une conversation complete (messages + summary)."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)
    try:
        p = _pool()
        data = await p.store.load_conversation(conv_id, api_token)
        if not data:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/v1/memory")
async def delete_memory(request: Request):
    """Supprime toutes les memoires vectorisees d'un utilisateur (droit a l'oubli)."""
    data = await request.json()
    api_token = data.get("api_token", "")
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)
    try:
        from ..memory import token_hash
        p = _pool()
        th = token_hash(api_token)
        count = await p.store.delete_user_memories(th)
        return {"status": "deleted", "facts_deleted": count}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/v1/memory/stats")
async def memory_stats(api_token: str = ""):
    """Statistiques memoire vectorisee d'un utilisateur."""
    if not api_token or not api_token.startswith("acc_"):
        return JSONResponse({"error": "Account token required"}, status_code=401)
    try:
        from ..memory import token_hash
        p = _pool()
        th = token_hash(api_token)
        async with p.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) as count, MIN(created) as first, MAX(created) as last FROM user_memories WHERE token_hash=$1",
                th)
            return {
                "facts_count": row["count"] if row else 0,
                "first_memory": row["first"].isoformat() if row and row["first"] else None,
                "last_memory": row["last"].isoformat() if row and row["last"] else None,
            }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
