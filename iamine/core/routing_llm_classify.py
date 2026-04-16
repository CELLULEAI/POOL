"""Smart routing Phase 4 — classifier via un worker LLM idle.

Appele par submit_job quand l'heuristique lexicale est ambigue (confidence < 0.7).
Doctrine David : "tout le pool travaille, pas de worker dedie". Le pool cherche
le worker idle le plus petit dispo, lui envoie un `classify_task`, attend 3s
max. Si aucun idle → fallback sur le tier heuristique initial.

Voir project_todo_smart_routing.md Phase 4.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

log = logging.getLogger("iamine.routing_llm_classify")

CLASSIFY_TIMEOUT_SEC = 3.0
# Blacklist workers qui n'ont pas repondu a un classify_task (ancien code, bug,
# ou timeout reseau) pendant cette duree pour eviter de perdre 3s a chaque job.
_CLASSIFY_BLACKLIST_TTL = 300.0  # 5 min


def _parse_size_b(model_path: str) -> float:
    """Extract B-size from model_path for ordering. Unknown = +inf."""
    mp = (model_path or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)b", mp)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return float("inf")


def _pick_idle_classifier(pool) -> str | None:
    """Retourne le worker_id idle le plus petit (economie pool).

    - Evite Coder (repond en JSON meme pour taches libres)
    - Evite les workers blacklistes (timeout recent sur classify_task)
    """
    if not hasattr(pool, "_classify_blacklist"):
        pool._classify_blacklist = {}
    now = time.time()
    blacklist = pool._classify_blacklist
    # purge entries expirees
    for wid in list(blacklist.keys()):
        if now - blacklist[wid] > _CLASSIFY_BLACKLIST_TTL:
            blacklist.pop(wid, None)

    candidates: list[tuple[str, float]] = []
    for wid, w in pool.workers.items():
        if w.busy:
            continue
        if wid in blacklist:
            continue
        mp = (w.info.get("model_path") or "").lower()
        if "coder" in mp:
            continue
        if pool._is_outdated(w) or pool._is_unknown_model(w):
            continue
        candidates.append((wid, _parse_size_b(mp)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def _blacklist_worker(pool, wid: str) -> None:
    if not hasattr(pool, "_classify_blacklist"):
        pool._classify_blacklist = {}
    pool._classify_blacklist[wid] = time.time()


async def classify_via_idle_worker(pool, prompt: str, timeout_sec: float = CLASSIFY_TIMEOUT_SEC) -> tuple[str, float, str] | None:
    """Envoie un classify_task a un worker idle, attend la reponse.

    Args:
        pool: instance Pool (acces a workers + _classify_futures).
        prompt: texte utilisateur a classifier (tronque cote worker).
        timeout_sec: timeout soft (par defaut 3s).

    Returns:
        (tier, confidence, worker_id) si OK.
        None si aucun idle dispo, timeout, ou reponse invalide.
    """
    if not prompt:
        return None

    if not hasattr(pool, "_classify_futures"):
        pool._classify_futures = {}

    wid = _pick_idle_classifier(pool)
    if not wid:
        log.debug("classify_via_idle: no idle worker available — fallback to heuristic")
        return None

    worker = pool.workers.get(wid)
    if not worker or not worker.ws:
        return None

    task_id = f"clf_{uuid.uuid4().hex[:10]}"
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    pool._classify_futures[task_id] = future

    try:
        await worker.ws.send_json({
            "type": "classify_task",
            "task_id": task_id,
            "prompt": prompt[:500],
            "timeout_ms": int(timeout_sec * 1000),
        })
    except Exception as e:
        pool._classify_futures.pop(task_id, None)
        log.warning(f"classify_via_idle: send failed to {wid}: {e}")
        return None

    try:
        msg = await asyncio.wait_for(future, timeout=timeout_sec + 0.5)
    except asyncio.TimeoutError:
        pool._classify_futures.pop(task_id, None)
        log.info(f"classify_via_idle: {wid} timeout on {task_id} — blacklisting 5 min")
        _blacklist_worker(pool, wid)
        return None

    tier = msg.get("tier", "")
    conf = float(msg.get("confidence", 0.0) or 0.0)
    if tier not in ("small", "medium", "code", "large") or conf <= 0:
        log.debug(f"classify_via_idle: invalid response from {wid}: {msg!r}")
        _blacklist_worker(pool, wid)
        return None
    return tier, conf, wid
