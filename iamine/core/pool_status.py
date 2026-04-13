"""Pool status snapshot builder — extracted from pool.py chunk 2 refactor.

Read-only snapshot of the pool state for /status endpoint and dashboard.
Takes a Pool instance and returns a JSON-serializable dict.
"""
from __future__ import annotations

import time

from iamine import __version__


def build_pool_status(pool) -> dict:
    """Snapshot JSON du pool : workers, jobs, load, boost eligibility."""
    uptime = time.time() - pool._start_time
    now = time.time()
    total_jobs = sum((w.info.get("total_jobs") or 0) for w in pool.workers.values()) or 1
    return {
        "pool": "IAMINE",
        "version": __version__,
        "uptime_sec": round(uptime),
        "workers_online": len(pool.workers),
        "workers_busy": sum(1 for w in pool.workers.values() if w.busy),
        "total_jobs": sum(w.jobs_done for w in pool.workers.values()),
        "workers": [
            {
                "id": w.worker_id,
                "busy": w.busy,
                "jobs_done": w.jobs_done,
                "total_jobs": w.info.get("total_jobs") or 0,
                "real_tps": w.info.get("real_tps") or 0,
                "model": w.info.get("model_path", "?"),
                "cpu": w.info.get("cpu", "?"),
                "ram_gb": w.info.get("ram_total_gb", 0),
                "gpu": w.info.get("gpu", ""),
                "gpu_vram_gb": w.info.get("gpu_vram_gb", 0),
                "has_gpu": w.info.get("has_gpu", False),
                "bench_tps": w.info.get("bench_tps"),
                "credits_earned": round(pool.api_tokens.get(w.info.get("api_token", ""), {}).get("total_earned", 0), 2),
                "job_share": round((w.info.get("total_jobs") or 0) / max(total_jobs, 1) * 100, 1),
                "version": w.info.get("version", "?"),
                "outdated": pool._is_outdated(w),
                "unknown_model": pool._is_unknown_model(w),
                "last_seen_sec": round(now - w.last_seen),
            }
            for w in pool.workers.values()
        ],
        "queued_jobs": pool._queue_size,
        "pool_load": round(sum(1 for w in pool.workers.values() if w.busy) / max(len(pool.workers), 1) * 100),
        "compaction_budget": pool.compaction_budget,
        "boost_eligible": pool._boost_eligible(),
        "active_tasks": list(pool._active_tasks.values()),
    }
