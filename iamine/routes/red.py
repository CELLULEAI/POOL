"""Routes pour le chat admin avec RED (via WebSocket existant du proxy Z2)."""

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("iamine.routes.red")

RED_WORKER_ID = "RED-z2"
RED_SYSTEM_PROMPT = """Tu es RED, agent admin autonome du pool IAMINE.
David te parle via le dashboard. Reponds en francais, sois concis.

FONCTIONS DISPONIBLES (reponds avec UN SEUL JSON par message) :

- pool_status : retourne l'etat du pool (workers, jobs, uptime)
  Appel : {"function": "pool_status"}

- pool_power : capacite totale du pool en t/s
  Appel : {"function": "pool_power"}

- worker_list : liste les workers avec leurs modeles
  Appel : {"function": "worker_list"}

- run : execute une commande shell sur Z2
  Appel : {"function": "run", "args": {"cmd": "rocm-smi"}}

- read_file : lire un fichier sur Z2 (~/iamine/ ou ~/RED/)
  Appel : {"function": "read_file", "args": {"path": "~/RED/RED.md"}}

- run_remote : execute une commande sur une machine distante
  Appel : {"function": "run_remote", "args": {"host": "192.168.1.86", "cmd": "uptime"}}

- pool_assign : assigner un modele a un worker
  Appel : {"function": "pool_assign", "args": {"worker_id": "Thor-7c8a", "model_id": "qwen3.5-9b-q4"}}

- pool_inference : tester l'inference du pool
  Appel : {"function": "pool_inference", "args": {"prompt": "Bonjour"}}

REGLES :
- Pour pool_status, pool_power, worker_list : PAS d'args, juste le nom
- Reponds toujours avec le JSON de la fonction, puis interprete le resultat
- Si la demande est une question simple, reponds directement en texte (pas de JSON)
"""

# Historique du chat admin (en RAM, reset au restart)
_chat_history: list[dict] = []


def _pool():
    from iamine.pool import pool
    return pool


async def _check_admin(request: Request):
    from iamine.routes.admin import _check_admin as check
    return await check(request)


@router.post("/admin/api/red/chat")
async def red_chat(request: Request):
    """Envoie un message a RED via le WebSocket du proxy Z2."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)

    pool = _pool()

    # Trouver le worker RED-z2 connecte
    worker = pool.workers.get(RED_WORKER_ID)
    if not worker or not worker.ws:
        return JSONResponse({
            "error": "RED-z2 non connecte au pool",
            "hint": "Le proxy Z2 doit etre demarre sur la machine Z2",
        }, status_code=503)

    # Construire les messages avec historique
    messages = [{"role": "system", "content": RED_SYSTEM_PROMPT}]
    # Inclure les 10 derniers echanges pour le contexte
    for h in _chat_history[-10:]:
        messages.append({"role": "user", "content": h["user"]})
        if h.get("assistant"):
            messages.append({"role": "assistant", "content": h["assistant"]})
    messages.append({"role": "user", "content": message})

    # Creer un future pour attendre la reponse
    chat_id = f"admin-red-{uuid.uuid4().hex[:8]}"
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pool._admin_chat_futures[chat_id] = future

    # Envoyer via le WebSocket existant
    try:
        await worker.ws.send_json({
            "type": "admin_chat",
            "chat_id": chat_id,
            "messages": messages,
            "max_tokens": int(body.get("max_tokens", 500)),
        })
    except Exception as e:
        pool._admin_chat_futures.pop(chat_id, None)
        return JSONResponse({"error": f"WebSocket send failed: {e}"}, status_code=503)

    # Attendre la reponse (timeout 120s)
    try:
        result = await asyncio.wait_for(future, timeout=120)
    except asyncio.TimeoutError:
        pool._admin_chat_futures.pop(chat_id, None)
        return JSONResponse({"error": "RED n'a pas repondu dans les 120s"}, status_code=504)

    text = result.get("text", "")
    error = result.get("error")

    # Sauvegarder dans l'historique
    _chat_history.append({
        "user": message,
        "assistant": text,
        "timestamp": time.time(),
        "chat_id": chat_id,
        "tps": result.get("tokens_per_sec", 0),
        "admin": admin,
    })
    # Garder max 50 echanges
    if len(_chat_history) > 50:
        _chat_history[:] = _chat_history[-50:]

    return {
        "chat_id": chat_id,
        "text": text,
        "tokens_per_sec": result.get("tokens_per_sec", 0),
        "duration_sec": result.get("duration_sec", 0),
        "error": error,
    }


@router.get("/admin/api/red/status")
async def red_status(request: Request):
    """Retourne le statut de RED-z2."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    pool = _pool()
    worker = pool.workers.get(RED_WORKER_ID)

    if not worker:
        return {"status": "OFFLINE", "connected": False}

    return {
        "status": "BUSY" if worker.busy else "IDLE",
        "connected": True,
        "worker_id": RED_WORKER_ID,
        "model": worker.info.get("model_path", "").split("/")[-1],
        "bench_tps": worker.info.get("bench_tps"),
        "jobs_done": worker.jobs_done,
        "last_seen": worker.last_seen,
        "chat_history_count": len(_chat_history),
    }


@router.get("/admin/api/red/chat-history")
async def red_chat_history(request: Request):
    """Retourne l'historique du chat admin avec RED."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    return {"history": _chat_history[-50:]}


@router.delete("/admin/api/red/chat-history")
async def red_clear_history(request: Request):
    """Efface l'historique du chat admin."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    _chat_history.clear()
    return {"status": "cleared"}
