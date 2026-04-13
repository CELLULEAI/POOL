"""WebSocket endpoint for worker connections — /ws"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
log = logging.getLogger("iamine.pool")


def _pool():
    from iamine.pool import pool
    return pool


def _parse_version(v: str) -> tuple[int, ...]:
    from iamine.pool import _parse_version
    return _parse_version(v)


def _version():
    from iamine import __version__
    return __version__


async def _check_assignment(pool_inst, worker, info):
    from iamine.pool import _check_model_assignment
    await _check_model_assignment(pool_inst, worker, info)


async def _self_heal(pool_inst, worker):
    from iamine.pool import _self_heal_downgrade
    await _self_heal_downgrade(pool_inst, worker)


# --- Auto-bench --- moved to core/assignment.py
async def _auto_bench(pool_inst, worker):
    from iamine.pool import _auto_bench as _ab
    await _ab(pool_inst, worker)


# --- WebSocket endpoint pour les workers ---
@router.websocket("/ws")
async def worker_ws(ws: WebSocket):
    await ws.accept()
    worker_id = None
    pool = _pool()
    __version__ = _version()

    try:
        async for raw in ws.iter_text():
            # Verifier si cette connexion a ete remplacee par une nouvelle
            if worker_id:
                current = pool.workers.get(worker_id)
                if current and current.ws is not ws:
                    # Cette connexion est obsolete — un nouveau WS l'a remplacee
                    log.info(f"Ancienne connexion de {worker_id} fermee (remplacee)")
                    break

            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "register":
                info = msg.get("worker", {})
                worker_id = info.get("worker_id", "unknown")
                pool.add_worker(worker_id, ws, info)
                # Envoyer le token API au worker
                w = pool.workers.get(worker_id)
                if w and w.ws is ws:
                    # Signal upgrade si version obsolete
                    worker_version = info.get("version", "0.0.0")
                    upgrade_signal = None
                    if _parse_version(worker_version) < _parse_version(__version__):
                        upgrade_signal = {
                            "current": worker_version,
                            "latest": __version__,
                            "url": "https://iamine.org/pypi",
                            "action": "pip install --upgrade iamine-ai -i https://iamine.org/pypi --extra-index-url https://pypi.org/simple",
                        }
                        log.info(f"Worker {worker_id} outdated: v{worker_version} → v{__version__}")

                    # Verifier si le modele est dans le registre
                    unknown_model = pool._is_unknown_model(w)
                    model_warning = None
                    if unknown_model:
                        model_file = info.get("model_path", "").split("/")[-1]
                        model_warning = {
                            "status": "excluded",
                            "reason": f"Model '{model_file}' is not in the pool registry. You will not receive any traffic.",
                            "action": "A model migration command will be sent shortly.",
                        }
                        log.warning(f"Worker {worker_id} has unknown model: {model_file} — excluded from routing")

                    await ws.send_json({
                        "type": "welcome",
                        "api_token": w.info.get("api_token", ""),
                        "message": f"Welcome {worker_id}! Your API token is ready.",
                        **({"upgrade": upgrade_signal} if upgrade_signal else {}),
                        **({"model_warning": model_warning} if model_warning else {}),
                    })
                    # M12: Push self_update command to outdated workers (>1 patch behind)
                    # Cooldown: only push once per worker per 10 minutes to avoid loops
                    if upgrade_signal:
                        import time as _time
                        wv = _parse_version(worker_version)
                        pv = _parse_version(__version__)
                        behind = (pv[2] - wv[2]) if len(wv) >= 3 and len(pv) >= 3 and wv[:2] == pv[:2] else 1
                        if not hasattr(pool, "_update_push_cooldown"):
                            pool._update_push_cooldown = {}
                        last_push = pool._update_push_cooldown.get(worker_id, 0)
                        if behind >= 1 and not w.busy and (_time.time() - last_push) > 600:
                            log.info(f"Worker {worker_id}: pushing self_update (v{worker_version} is {behind} patches behind)")
                            await ws.send_json({"type": "command", "cmd": "self_update"})
                            pool._update_push_cooldown[worker_id] = _time.time()
                        elif behind > 1 and (_time.time() - last_push) <= 600:
                            log.debug(f"Worker {worker_id}: update cooldown active, skipping push")

                    # Bench obligatoire : si pas de bench_tps, lancer un mini bench
                    if not info.get("bench_tps") and w and not w.busy and not unknown_model:
                        asyncio.create_task(_auto_bench(pool, w))

                    # Attribution auto : verifier si le worker a le bon modele
                    asyncio.create_task(_check_assignment(pool, w, info))

            elif msg_type == "result":
                pool.handle_result(msg)

            elif msg_type == "error":
                pool.handle_error(msg)

            elif msg_type == "pong":
                w = pool.workers.get(worker_id)
                if w and w.ws is ws:
                    w.last_seen = time.time()

            elif msg_type == "admin_chat_response":
                # Reponse du chat admin RED — resoudre le future
                chat_id = msg.get("chat_id", "")
                if chat_id and hasattr(pool, '_admin_chat_futures'):
                    future = pool._admin_chat_futures.pop(chat_id, None)
                    if future and not future.done():
                        future.set_result(msg)
                log.info(f"Admin chat response from {worker_id}: {chat_id}")

            elif msg_type == "command_ack":
                cmd = msg.get("cmd", "?")
                status = msg.get("status", "?")
                log.info(f"Command ACK from {worker_id}: {cmd} → {status}")

    except WebSocketDisconnect:
        pass
    finally:
        # Ne retirer le worker que si c'est encore CETTE connexion
        # (pas si un nouveau WS l'a deja remplace)
        if worker_id:
            current = pool.workers.get(worker_id)
            if current and current.ws is ws:
                pool.remove_worker(worker_id)
