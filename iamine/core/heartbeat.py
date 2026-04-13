"""Heartbeat, watchdog, drain-pending-jobs et webhook -- extrait de pool.py."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from ..pool import Pool

log = logging.getLogger("iamine.heartbeat")

# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------

async def heartbeat_loop(pool: "Pool") -> None:
    """Boucle de heartbeat : ping les workers et evicte les morts."""
    # Import tardif pour eviter les imports circulaires
    from .credits import loyalty_rewards
    from .assignment import _self_heal_downgrade
    _last_version_check = 0  # timestamp of last daily version push

    while True:
        await asyncio.sleep(pool.HEARTBEAT_INTERVAL)

        # Ping tous les workers
        for w in list(pool.workers.values()):
            try:
                await w.ws.send_json({"type": "ping"})
            except Exception:
                log.warning(f"Heartbeat: impossible de ping {w.worker_id}")

        # Eviction des workers morts (pas de pong depuis HEARTBEAT_TIMEOUT)
        stale = pool.get_stale_workers()
        for wid in stale:
            log.warning(f"Heartbeat: worker {wid} timeout ({pool.HEARTBEAT_TIMEOUT}s) — eviction")
            pool.remove_worker(wid)
            # Fermer la websocket si possible
            w = pool.workers.get(wid)
            if w:
                try:
                    await w.ws.close()
                except Exception:
                    pass

        # Watchdog busy flag :
        #   - Workers normaux  : reset si busy > 120s (jobs CPU/petits modeles sont courts)
        #   - Proxies (Z2 30B) : reset si busy > 600s (jobs longs legitimes, ne pas toucher)
        now = time.time()
        for w in pool.workers.values():
            if w.busy:
                busy_since = getattr(w, "_busy_since", 0)
                if not busy_since:
                    w._busy_since = now
                else:
                    is_proxy = bool(w.info.get("proxy_mode"))
                    threshold = 600 if is_proxy else 120
                    if now - busy_since > threshold:
                        log.warning(f"Watchdog: {w.worker_id} busy > {threshold}s — reset (proxy={is_proxy})")
                        w.busy = False
                        w._busy_since = 0
                        pool._worker_freed.set()
            else:
                w._busy_since = 0

        # Daily version check — push self_update to outdated workers (once per 24h)
        if time.time() - _last_version_check > 86400:
            _last_version_check = time.time()
            from .. import __version__
            for w in list(pool.workers.values()):
                wv = w.info.get("version", "0.0.0")
                try:
                    local = tuple(int(x) for x in __version__.split("."))
                    remote = tuple(int(x) for x in wv.split("."))
                    behind = (local[2] - remote[2]) if len(local) >= 3 and len(remote) >= 3 and local[:2] == remote[:2] else 0
                except Exception:
                    behind = 0
                if behind >= 1 and not w.busy:
                    try:
                        await w.ws.send_json({"type": "command", "cmd": "self_update"})
                        log.info(f"Daily version check: pushed self_update to {w.worker_id} (v{wv})")
                    except Exception:
                        pass

        # Loyalty rewards — delegue a core/credits.py
        await loyalty_rewards(pool)

        # Nettoyage L3 : conversations expirees en RAM et PostgreSQL
        expired_ids = pool.router.drain_expired()
        for cid in expired_ids:
            try:
                await pool.store.delete_conversation(cid)
            except Exception:
                pass  # store peut etre indisponible
        try:
            await pool.store.cleanup_expired_conversations()
        except Exception:
            pass

        # === SELF-HEALING : rebalance workers (cooldown 10 min) ===
        now = time.time()
        for w in list(pool.workers.values()):
            has_gpu = w.info.get("has_gpu", False)
            if has_gpu:
                continue
            # Cooldown : pas de rebalance si heal dans les 10 dernieres minutes
            last_heal = w.info.get("_heal_time", 0)
            if now - last_heal < 600:
                continue
            real_tps = w.info.get("real_tps", 0)
            bench = w.info.get("bench_tps") or 0
            total_jobs = w.info.get("total_jobs", 0) or 0
            effective = real_tps if real_tps > 0 else bench
            if effective > 0 and effective < 5.0:
                # Compteur de cycles lents consecutifs (stabilite avant action)
                slow_count = w.info.get("_slow_cycles", 0) + 1
                w.info["_slow_cycles"] = slow_count
                if slow_count >= 3 or (total_jobs == 0 and bench > 0 and bench < 5.0):
                    # 3 cycles lents (90s) OU idle avec bench lent -> rebalance
                    asyncio.create_task(_self_heal_downgrade(pool, w))
                    w.info["_heal_time"] = now
                    w.info["_slow_cycles"] = 0
            else:
                w.info["_slow_cycles"] = 0  # reset si performance OK


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

async def fire_webhook(job_id: str, webhook_url: str, result: dict) -> None:
    """POST webhook notification when a pending job completes."""
    try:
        payload = {
            "event": "job.completed",
            "job_id": job_id,
            "worker_id": result.get("worker_id", ""),
            "model": result.get("model", ""),
            "text": result.get("text", ""),
            "tokens_per_sec": result.get("tokens_per_sec", 0),
            "duration_sec": result.get("duration_sec", 0),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                log.info(f"Webhook {job_id} -> {webhook_url}: {resp.status}")
    except Exception as e:
        log.warning(f"Webhook {job_id} failed: {e}")


# ---------------------------------------------------------------------------
# Drain pending jobs loop
# ---------------------------------------------------------------------------

async def drain_pending_jobs_loop(pool: "Pool") -> None:
    """Traite les jobs en attente quand des workers se liberent.

    Poll toutes les 2s. Quand un worker idle est trouve ET qu'un job
    est en queue, le job est traite via submit_job avec timeout 300s
    et retry 3x (le drain n'est pas interactif, on peut attendre).
    Nettoyage des jobs expires toutes les 30 iterations (~60s).
    """
    _cleanup_counter = 0
    while True:
        await asyncio.sleep(2)
        try:
            # Verifier qu'il y a des workers idle
            idle_workers = [w for w in pool.workers.values() if not w.busy]
            if not idle_workers:
                _cleanup_counter += 1
                if _cleanup_counter >= 30:
                    _cleanup_counter = 0
                    await pool.store.cleanup_expired_jobs()
                continue

            # Prendre le plus ancien job pending
            job = await pool.store.get_next_pending_job()
            if not job:
                _cleanup_counter += 1
                if _cleanup_counter >= 30:
                    _cleanup_counter = 0
                    cleaned = await pool.store.cleanup_expired_jobs()
                    if cleaned:
                        log.info(f"Drain: cleaned {cleaned} expired pending jobs")
                continue

            # Traiter le job — timeout 300s + retry 3x
            job_id = job["job_id"]
            log.info(f"Drain: processing pending job {job_id}")
            success = False
            for attempt in range(3):
                try:
                    result = await asyncio.wait_for(
                        pool.submit_job(
                            messages=job["messages"],
                            max_tokens=job.get("max_tokens", 512),
                            conv_id=job.get("conv_id") or None,
                            requested_model=job.get("requested_model") or None,
                            api_token=job.get("api_token", ""),
                        ),
                        timeout=300,  # 5 min (pas interactif, le client poll)
                    )
                    await pool.store.complete_pending_job(
                        job_id, response=result, worker_id=result.get("worker_id", ""))
                    log.info(f"Drain: completed {job_id} via {result.get('worker_id', '?')}")
                    # Fire webhook notification if configured
                    try:
                        webhook_url = await pool.store.get_pending_job_webhook(job_id)
                        if webhook_url:
                            asyncio.create_task(fire_webhook(job_id, webhook_url, result))
                    except Exception:
                        pass
                    success = True
                    break
                except (asyncio.TimeoutError, RuntimeError) as e:
                    if attempt < 2:
                        log.warning(f"Drain: retry {attempt+1}/3 for {job_id}: {e}")
                        await asyncio.sleep(5)
                    else:
                        await pool.store.fail_pending_job(job_id, f"3 retries failed: {e}")
                        log.warning(f"Drain: failed {job_id} after 3 retries: {e}")
                except Exception as e:
                    await pool.store.fail_pending_job(job_id, str(e))
                    log.warning(f"Drain: failed {job_id}: {e}")
                    break

            # Nettoyage periodique
            _cleanup_counter += 1
            if _cleanup_counter >= 30:
                _cleanup_counter = 0
                await pool.store.cleanup_expired_jobs()

        except Exception as e:
            log.error(f"Drain loop error: {e}")
