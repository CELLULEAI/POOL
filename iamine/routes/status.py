"""Status & info endpoints — /v1/status, /v1/models, /v1/pool/power, etc."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("iamine.pool")


def _pool():
    from iamine.pool import pool
    return pool


# ─── GET /v1/status ──────────────────────────────────────────────────────────

@router.get("/v1/status")
async def status():
    return _pool().status()


# ─── GET /v1/wallet/{api_token} ──────────────────────────────────────────────

@router.get("/v1/wallet/{api_token}")
async def get_wallet(api_token: str):
    """Consulte le solde d'un token API."""
    pool = _pool()
    token_data = pool.api_tokens.get(api_token)
    if not token_data:
        return JSONResponse({"error": "Accès refusé"}, status_code=403)

    worker_id = token_data["worker_id"]
    is_online = worker_id in pool.workers

    return {
        "worker_id": worker_id,
        "api_token": api_token[:16] + "...",
        "credits": round(token_data["credits"], 2),
        "requests_used": token_data["requests_used"],
        "is_online": is_online,
        "can_use_api": token_data["credits"] >= 1.0 or is_online,
    }


# ─── GET /v1/router/stats ────────────────────────────────────────────────────

@router.get("/v1/router/stats")
async def router_stats():
    """Stats du smart router — conversations actives, tokens en memoire."""
    return _pool().router.get_stats()


# ─── GET /v1/models ──────────────────────────────────────────────────────────

@router.get("/v1/models")
async def models():
    """Liste les modeles disponibles - seul iamine (smart routing) est expose."""
    pool = _pool()
    n_workers = len([w for w in pool.workers.values() if not w.busy])
    return {
        "object": "list",
        "data": [
            {
                "id": "iamine",
                "object": "model",
                "created": 1712534400,
                "owned_by": "iamine-pool",
                "workers": n_workers,
            }
        ],
    }


# ─── GET /v1/pool/power ──────────────────────────────────────────────────────

@router.get("/v1/pool/power")
async def pool_power():
    """Analyse la puissance du pool et recommande les modèles optimaux.

    Analyzes each worker's capacity, computes total network power,
    and recommends the optimal model per worker and for the pool.
    """
    from ..models import recommend_pool_model, MODEL_REGISTRY

    pool = _pool()
    workers_data = []
    for w in pool.workers.values():
        workers_data.append({
            "worker_id": w.worker_id,
            "ram_gb": w.info.get("ram_available_gb", w.info.get("ram_total_gb", 4)),
            "cpu_threads": w.info.get("cpu_threads", 4),
            "bench_tps": w.info.get("bench_tps"),
        })

    analysis = recommend_pool_model(workers_data)

    # Ajouter le catalogue complet des modèles disponibles
    analysis["model_catalog"] = [
        {
            "id": m.id,
            "name": m.name,
            "params": m.params,
            "size_gb": m.size_gb,
            "ram_required_gb": m.ram_required_gb,
            "quality_score": m.quality_score,
        }
        for m in MODEL_REGISTRY
    ]

    return analysis


# ─── POST /v1/worker/bench ───────────────────────────────────────────────────

@router.post("/v1/worker/bench")
async def receive_bench(data: dict):
    """Reçoit les résultats de benchmark d'un worker connecté."""
    pool = _pool()
    worker_id = data.get("worker_id")
    bench_tps = data.get("avg_tps")

    w = pool.workers.get(worker_id)
    if not w:
        return JSONResponse({"error": "worker not connected"}, status_code=403)
    if w and bench_tps:
        w.info["bench_tps"] = bench_tps
        w.info["bench_results"] = data
        log.info(f"Benchmark reçu de {worker_id}: {bench_tps} t/s")

        # Recommander un modèle pour ce worker
        from ..models import recommend_model_for_worker
        rec, rec_ctx = recommend_model_for_worker(
            ram_available_gb=w.info.get("ram_available_gb", w.info.get("ram_total_gb", 4)),
            cpu_threads=w.info.get("cpu_threads", 4),
            bench_tps=bench_tps,
        )
        return {
            "status": "ok",
            "recommended_model": rec.id,
            "model_name": rec.name,
            "model_repo": rec.hf_repo,
            "model_file": rec.hf_file,
            "model_size_gb": rec.size_gb,
            "recommended_ctx": rec_ctx,
            "quality_score": rec.quality_score,
        }

    return {"status": "error", "message": "worker_id ou avg_tps manquant"}


# ─── GET /v1/admin/models (public) ───────────────────────────────────────────

@router.get("/v1/admin/models")
async def admin_models():
    """Liste tous les modeles avec leur statut de deblocage."""
    from ..models import get_unlocked_models, recommend_pool_model

    pool = _pool()

    # Calculer la puissance du pool
    workers_data = []
    max_ram = 0
    for w in pool.workers.values():
        ram = w.info.get("ram_available_gb", w.info.get("ram_total_gb", 4))
        max_ram = max(max_ram, ram)
        workers_data.append({
            "worker_id": w.worker_id,
            "ram_gb": ram,
            "cpu_threads": w.info.get("cpu_threads", 4),
            "bench_tps": w.info.get("bench_tps"),
        })

    analysis = recommend_pool_model(workers_data)
    total_tps = analysis.get("pool_capacity_tps", 0)

    models = get_unlocked_models(total_tps, max_ram)

    active = sum(1 for m in models if m["status"] == "active")
    unlocked = sum(1 for m in models if m["status"] == "unlocked")
    locked = sum(1 for m in models if m["status"] == "locked")

    return {
        "pool_tps": total_tps,
        "max_worker_ram_gb": max_ram,
        "models": models,
        "summary": {
            "active": active,
            "unlocked": unlocked,
            "locked": locked,
            "total": len(models),
        },
    }
