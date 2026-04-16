"""core/assignment.py -- Attribution de modeles et self-healing des workers.

Extrait de pool.py (etape 8 du refactoring).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from ..core.types import PendingJob

log = logging.getLogger("iamine.pool")

# ---------------------------------------------------------------------------
# Helpers locaux
# ---------------------------------------------------------------------------

_MODEL_SIZE_RE = re.compile(r'[\-_](\d+(?:\.\d+)?)[Bb][\-_\.]')

# Anti-oscillation : cooldown par worker sur les pushs update_model
UPDATE_MODEL_COOLDOWN_SEC = 1800  # 30 min
_last_update_model_at: dict[str, float] = {}


def _can_push_update_model(worker_id: str) -> bool:
    last = _last_update_model_at.get(worker_id, 0)
    return (time.time() - last) >= UPDATE_MODEL_COOLDOWN_SEC


def _mark_update_model_pushed(worker_id: str) -> None:
    _last_update_model_at[worker_id] = time.time()


def _parse_model_size(model_path: str) -> float:
    """Extrait la taille en milliards depuis un path GGUF."""
    m = _MODEL_SIZE_RE.search(model_path)
    return float(m.group(1)) if m else 0


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse version string en tuple pour comparaison semantique."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


# ---------------------------------------------------------------------------
# _auto_bench
# ---------------------------------------------------------------------------

async def _auto_bench(pool_inst, worker):
    """Bench automatique d'un worker sans bench_tps -- envoie un petit job de test."""
    job_id = None
    try:
        await asyncio.sleep(3)  # laisser le worker s'initialiser
        if worker.busy or worker.worker_id not in pool_inst.workers:
            return
        worker.busy = True
        job_id = f"bench_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        pool_inst.pending_jobs[job_id] = PendingJob(
            job_id=job_id, messages=[{"role": "user", "content": "Hello"}],
            max_tokens=32, future=future
        )
        await worker.ws.send_json({
            "type": "job", "job_id": job_id,
            "messages": [{"role": "user", "content": "Say hello in one word."}],
            "max_tokens": 32,
        })
        result = await asyncio.wait_for(future, timeout=60)
        tps = result.get("tokens_per_sec", 0)
        if tps > 0:
            worker.info["bench_tps"] = round(tps, 1)
            log.info(f"Auto-bench {worker.worker_id}: {tps:.1f} tok/s")
            if hasattr(pool_inst, '_save_benchmark'):
                await pool_inst._save_benchmark(worker.worker_id, worker.info)
            # Post-upgrade check : si le bench reel est trop lent, downgrade
            model_path = worker.info.get("model_path", "")
            from ..models import MODEL_REGISTRY, REGISTRY_BY_ID
            current_tier = None
            for m in MODEL_REGISTRY:
                if m.hf_file in model_path:
                    current_tier = m
                    break
            if current_tier and tps < current_tier.min_tps_useful and current_tier != MODEL_REGISTRY[0]:
                # Trouver le tier inferieur
                idx = MODEL_REGISTRY.index(current_tier)
                downgrade = MODEL_REGISTRY[idx - 1] if idx > 0 else MODEL_REGISTRY[0]
                if not _can_push_update_model(worker.worker_id):
                    log.info(f"Post-upgrade downgrade SKIPPED (cooldown): {worker.worker_id}")
                    return
                log.info(f"Post-upgrade downgrade: {worker.worker_id} {current_tier.name} at {tps:.1f} t/s < {current_tier.min_tps_useful} -> {downgrade.name}")
                try:
                    await pool_inst.store.update_worker_assignment(
                        worker.worker_id, downgrade.id, f"models/{downgrade.hf_file}",
                        downgrade.ctx_default, -1 if worker.info.get("has_gpu") else 0)
                    await worker.ws.send_json({
                        "type": "command", "cmd": "update_model",
                        "model_url": f"http://dl.cellule.ai/v1/models/download/{downgrade.hf_file}",
                        "model_path": f"models/{downgrade.hf_file}",
                        "ctx_size": downgrade.ctx_default,
                        "gpu_layers": -1 if worker.info.get("has_gpu") else 0,
                    })
                    _mark_update_model_pushed(worker.worker_id)
                except Exception as e:
                    log.debug(f"Downgrade send failed: {e}")
            else:
                # Bench termine -> relancer l'attribution pour upgrader le modele
                asyncio.create_task(_check_model_assignment(pool_inst, worker, worker.info))
    except Exception as e:
        log.debug(f"Auto-bench {worker.worker_id} failed: {e}")
    finally:
        worker.busy = False
        if job_id:
            pool_inst.pending_jobs.pop(job_id, None)
        pool_inst._worker_freed.set()


# ---------------------------------------------------------------------------
# _self_heal_downgrade
# ---------------------------------------------------------------------------

async def _self_heal_downgrade(pool_inst, worker):
    """Rebalance un worker via best_model_from_bench (up ou down).

    Utilise le bench_tps original (sur 0.8B) pour recalculer le bon modele.
    Si le bench n'est pas dispo, extrapole depuis les t/s actuels.
    """
    try:
        # Workers non geres par le pool -- ne pas toucher
        try:
            if not await pool_inst.store.is_pool_managed(worker.worker_id):
                return
        except Exception:
            pass
        from ..models import best_model_from_bench, MODEL_REGISTRY
        current_model = worker.info.get("model_path", "")
        current_size = _parse_model_size(current_model)
        ram_gb = worker.info.get("ram_total_gb", 4)
        has_gpu = worker.info.get("has_gpu", False)
        gpu_vram = worker.info.get("gpu_vram_gb", 0)

        # Retrouver le bench 0.8B (original ou extrapole)
        bench_08b = worker.info.get("bench_tps") or 0
        real_tps = worker.info.get("real_tps") or 0
        if not bench_08b and real_tps > 0 and current_size > 0:
            bench_08b = real_tps * (current_size / 0.5)

        if not bench_08b:
            return

        version = worker.info.get("version", "0.0.0")
        if _parse_version(version) < _parse_version("0.2.4"):
            return

        if worker.info.get("_healing"):
            return

        best_target, ctx = best_model_from_bench(bench_08b, ram_gb, has_gpu=has_gpu, gpu_vram_gb=gpu_vram)

        # Deja le bon modele -> rien a faire
        if best_target.hf_file in current_model:
            return

        worker.info["_healing"] = True
        direction = "upgraded" if best_target.size_gb > current_size else "downgraded"

        if not _can_push_update_model(worker.worker_id):
            log.info(f"SELF-HEAL SKIPPED (cooldown): {worker.worker_id}")
            worker.info["_healing"] = False
            return

        payload = {
            "type": "command",
            "cmd": "update_model",
            "model_url": f"http://dl.cellule.ai/v1/models/download/{best_target.hf_file}",
            "model_path": f"models/{best_target.hf_file}",
            "ctx_size": ctx,
            "gpu_layers": -1 if has_gpu else 0,
            "threads": min(worker.info.get("cpu_threads", 4), 16),
        }
        await worker.ws.send_json(payload)
        _mark_update_model_pushed(worker.worker_id)
        worker.busy = True
        log.warning(
            f"SELF-HEAL: {worker.worker_id} {direction} {current_model.split('/')[-1]} -> {best_target.name} "
            f"(bench_0.8B={bench_08b:.1f} t/s)"
        )

        try:
            await pool_inst.store.update_worker_assignment(
                worker.worker_id, best_target.id, f"models/{best_target.hf_file}",
                ctx, -1 if has_gpu else 0)
        except Exception:
            pass
    except Exception as e:
        log.debug(f"Self-heal failed for {worker.worker_id}: {e}")


# ---------------------------------------------------------------------------
# _check_model_assignment
# ---------------------------------------------------------------------------

async def _check_model_assignment(pool_inst, worker, info: dict):
    """Verifie si le worker a le bon modele et envoie assign_model si besoin.
    Priorite : assignation persistee en DB > bench-first (inference reelle)."""
    try:
        from ..models import recommend_model_for_worker, REGISTRY_BY_ID, MODEL_REGISTRY
        current_model = info.get("model_path", "")
        hostname = info.get("hostname", "")

        # Ne pas auto-assigner le worker local VPS (fallback uniquement)
        if hostname == pool_inst._pool_hostname:
            return

        # Ne pas auto-assigner les workers proxy (Z2: RED, Eclipse, Scout)
        PROXY_WORKER_IDS = {"RED-z2", "Coder-z2", "Scout-z2", "Tank-z2"}
        ROUTING_EXCLUDED = set()
        if info.get("proxy_mode") or worker.worker_id in PROXY_WORKER_IDS:
            log.debug(f"Skip model assignment for {worker.worker_id} (proxy)")
            return

        # Workers non geres par le pool -- ne pas toucher
        try:
            if not await pool_inst.store.is_pool_managed(worker.worker_id):
                log.debug(f"Skip model assignment for {worker.worker_id} (pool_managed=false)")
                return
        except Exception:
            pass

        ram = info.get("ram_total_gb", 4)
        threads = info.get("cpu_threads", 4)
        has_gpu = info.get("has_gpu", False)
        gpu_vram = info.get("gpu_vram_gb", 0)

        # 1) Priorite : assignation persistee en DB
        target_file = None
        target_id = None
        target_ctx = None
        target_gpu_layers = None
        bench_tps = info.get("bench_tps") or 0
        real_tps = info.get("real_tps") or 0
        try:
            db_assign = await pool_inst.store.get_worker_assignment(worker.worker_id)
            if db_assign:
                target_id = db_assign["model_id"]
                tier = REGISTRY_BY_ID.get(target_id)
                if tier:
                    effective_bench = bench_tps
                    if real_tps > bench_tps and real_tps > 0:
                        effective_bench = real_tps
                    if effective_bench > 0 and tier.id.endswith("0.8b-q4"):
                        from ..models import best_model_from_bench
                        better, better_ctx = best_model_from_bench(effective_bench, ram, has_gpu=has_gpu, gpu_vram_gb=gpu_vram)
                        if better.id != tier.id:
                            log.info(f"Bench upgrade: {worker.worker_id} bench={bench_tps:.1f} -> {better.name} (was {tier.name})")
                            target_id = better.id
                            target_file = better.hf_file
                            target_ctx = better_ctx
                            target_gpu_layers = -1 if has_gpu else 0
                            await pool_inst.store.update_worker_assignment(
                                worker.worker_id, better.id, f"models/{better.hf_file}",
                                target_ctx, target_gpu_layers)
                    if not target_file:
                        target_file = tier.hf_file
                        target_ctx = db_assign.get("ctx_size") or tier.ctx_default
                        target_gpu_layers = db_assign.get("gpu_layers", -1 if has_gpu else 0)
                    log.debug(f"DB assignment for {worker.worker_id}: {target_id}")
        except Exception:
            pass

        # 2) Attribution par bench
        if not target_file:
            bench_tps = info.get("bench_tps") or 0
            if bench_tps > 0:
                from ..models import best_model_from_bench
                rec, ctx = best_model_from_bench(bench_tps, ram, has_gpu=has_gpu, gpu_vram_gb=gpu_vram)
                target_id = rec.id
                target_file = rec.hf_file
                target_ctx = ctx
                target_gpu_layers = -1 if has_gpu else 0
                log.info(f"Bench attribution: {worker.worker_id} bench={bench_tps:.1f} t/s -> {rec.name}")
                try:
                    await pool_inst.store.update_worker_assignment(
                        worker.worker_id, rec.id, f"models/{rec.hf_file}",
                        target_ctx, target_gpu_layers)
                except Exception:
                    pass

        # 3) Fallback : pas de bench -> 0.8B
        if not target_file:
            fallback = MODEL_REGISTRY[0]
            target_id = fallback.id
            target_file = fallback.hf_file
            target_ctx = fallback.ctx_default
            target_gpu_layers = -1 if has_gpu else 0

        # Promotion basee sur real_tps
        if target_file in current_model and real_tps > 0:
            from ..models import promote_from_real_tps, REGISTRY_BY_ID as _rid2
            current_tier = _rid2.get(target_id)
            if current_tier:
                promo = promote_from_real_tps(real_tps, current_tier.size_gb, ram, has_gpu, gpu_vram)
                if promo:
                    better_model, better_ctx = promo
                    if has_gpu and gpu_vram > 2 and better_model.size_gb + 0.3 > gpu_vram:
                        log.debug(f"Skip promotion {worker.worker_id}: {better_model.name} ({better_model.size_gb}G) > VRAM ({gpu_vram}G)")
                    elif better_model.id != target_id:
                        log.info(f"Real-TPS promotion: {worker.worker_id} does {real_tps:.1f} t/s on {current_tier.name} "
                                 f"-> promoting to {better_model.name}")
                        target_id = better_model.id
                        target_file = better_model.hf_file
                        target_ctx = better_ctx
                        target_gpu_layers = -1 if has_gpu else 0
                        try:
                            await pool_inst.store.update_worker_assignment(
                                worker.worker_id, better_model.id, f"models/{better_model.hf_file}",
                                target_ctx, target_gpu_layers)
                        except Exception:
                            pass

        # Le worker a-t-il deja le bon modele ?
        if target_file in current_model:
            current_bench = info.get("bench_tps") or 0
            current_tier = REGISTRY_BY_ID.get(target_id)
            if current_tier and current_bench > 0 and current_bench < current_tier.min_tps_useful:
                idx = MODEL_REGISTRY.index(current_tier) if current_tier in MODEL_REGISTRY else -1
                if idx > 0:
                    downgrade = MODEL_REGISTRY[idx - 1]
                    log.info(f"Speed check: {worker.worker_id} {current_tier.name} at {current_bench:.1f} t/s < {current_tier.min_tps_useful} -> {downgrade.name}")
                    target_id = downgrade.id
                    target_file = downgrade.hf_file
                    target_ctx = downgrade.ctx_default
                    try:
                        await pool_inst.store.update_worker_assignment(
                            worker.worker_id, downgrade.id, f"models/{downgrade.hf_file}",
                            target_ctx, target_gpu_layers)
                    except Exception:
                        pass
                else:
                    return
            else:
                return

        # Envoyer la commande seulement si le worker supporte les commandes (v0.2.4+)
        version = info.get("version", "0.0.0")
        if _parse_version(version) < _parse_version("0.2.4"):
            tier_name = REGISTRY_BY_ID.get(target_id, None)
            label = tier_name.name if tier_name else target_id
            log.info(f"Worker {worker.worker_id} needs {label} but version {version} too old for remote update")
            return

        if not _can_push_update_model(worker.worker_id):
            log.info(f"Placement SKIPPED (cooldown): {worker.worker_id} -> {target_id}")
            return

        payload = {
            "type": "command",
            "cmd": "update_model",
            "model_id": target_id,
            "model_url": f"http://dl.cellule.ai/v1/models/download/{target_file}",
            "model_path": f"models/{target_file}",
            "ctx_size": target_ctx,
            "gpu_layers": target_gpu_layers,
            "threads": min(threads, 16),
        }
        # Cooldown adaptatif par taille de modele
        last_assign = worker.info.get("_last_assign_time", 0)
        from ..models import REGISTRY_BY_ID as _rid
        _tier = _rid.get(target_id)
        _cooldown = 180
        if _tier:
            if _tier.size_gb >= 16:
                _cooldown = 1200
            elif _tier.size_gb >= 5:
                _cooldown = 600
            elif _tier.size_gb >= 2:
                _cooldown = 300
        if time.time() - last_assign < _cooldown:
            log.info(f"Skip auto-assign {worker.worker_id}: cooldown {_cooldown}s (assigned {int(time.time()-last_assign)}s ago)")
            return
        await worker.ws.send_json(payload)
        _mark_update_model_pushed(worker.worker_id)
        worker.busy = True
        worker.info["_last_assign_time"] = time.time()
        tier_name = REGISTRY_BY_ID.get(target_id, None)
        label = tier_name.name if tier_name else target_id
        log.info(f"Auto-assign: {worker.worker_id} -> {label} (was {current_model.split('/')[-1]}) -- worker busy until restart")
    except Exception as e:
        log.debug(f"Model assignment check failed for {worker.worker_id}: {e}")
