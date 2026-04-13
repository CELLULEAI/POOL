"""Endpoint pour poll les pending jobs (tampon DB anti-saturation)."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("iamine.routes.jobs")


def _pool():
    from iamine.pool import pool
    return pool


@router.get("/v1/jobs/{job_id}")
async def get_job_status(job_id: str, request: Request):
    """Poll le statut d'un job en file d'attente.

    Le client recoit un job_id quand le pool est sature.
    Il peut poll cet endpoint pour obtenir la reponse quand elle est prete.
    """
    p = _pool()

    # Extraire le token pour l'isolation
    api_token = request.query_params.get("token", "")
    if not api_token:
        # Chercher dans le header Authorization
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            api_token = auth[7:]

    job = await p.store.get_pending_job(job_id, api_token)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    if job["status"] == "completed":
        response = job.get("response", {})
        return {
            "status": "completed",
            "response": response,
        }

    if job["status"] == "failed":
        return JSONResponse({
            "status": "failed",
            "error": job.get("error", "Unknown error"),
        }, status_code=500)

    if job["status"] == "processing":
        return {
            "status": "processing",
            "worker_id": job.get("worker_id", ""),
        }

    # pending — calculer la position dans la queue
    stats = await p.store.get_queue_stats()
    return {
        "status": "pending",
        "queue_depth": stats["pending"],
        "estimated_wait_sec": max(5, stats["avg_wait_sec"]) if stats["avg_wait_sec"] else 15,
    }
