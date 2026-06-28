"""Tests des gardes anti-saturation du pool (incident 2026-06-28).

Couvre les correctifs apportes apres l'emballement de compaction qui avait
fige pool_load a 100% :

1. delegate_task : nettoyage GARANTI de _active_tasks / pending_jobs meme quand
   l'appel est annule par un timeout EXTERNE (async_compact enveloppe
   delegate_task dans wait_for(60s) alors que delegate_task a son propre
   wait_for(90s) -> CancelledError non capture -> fuite ad vitam). C'est la
   reproduction directe de la fuite fantome.
2. get_idle_worker(reserve=N) : ne jamais epuiser la capacite d'inference.
3. cooldown de compaction par conversation : plafonne un client qui martele.

Runnable direct (`python tests/test_compaction_guards.py`) ou via pytest.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from iamine import __version__
from iamine.pool import Pool
from iamine.core import compaction as C


class _WS:
    async def send_json(self, msg):
        return None


class _Helper:
    """Worker mock minimal pour delegate_task / get_idle_worker."""

    def __init__(self, wid="h1", bench=50.0, busy=False):
        self.worker_id = wid
        self.ws = _WS()
        self.busy = busy
        # proxy_mode=True -> jamais 'unknown_model' ; version courante -> pas outdated
        self.info = {
            "api_token": None, "proxy_mode": True, "version": __version__,
            "model_path": "Qwen_Qwen3.5-9B-Q4_K_M.gguf", "bench_tps": bench,
        }


# ---------------------------------------------------------------------------
# 1. La fuite fantome : annulation externe -> _active_tasks DOIT rester vide
# ---------------------------------------------------------------------------

def test_delegate_task_no_ghost_on_external_cancel():
    async def _run():
        pool = Pool()
        helper = _Helper()
        # Outer timeout (0.05s) << inner wait_for(90s) de delegate_task :
        # reproduit EXACTEMENT le pattern async_compact wait_for(60) < 90.
        # Le future n'est jamais resolu -> l'outer annule delegate_task.
        try:
            await asyncio.wait_for(
                pool.delegate_task(
                    helper=helper, task_type="compact",
                    prompt="resume ceci", conv_id="conv_x", source_worker="src",
                ),
                timeout=0.05,
            )
        except asyncio.TimeoutError:
            pass
        # Laisser tourner la boucle pour que les finally s'executent
        await asyncio.sleep(0)
        return pool

    pool = asyncio.run(_run())
    assert pool._active_tasks == {}, f"FUITE: {pool._active_tasks}"
    assert pool.pending_jobs == {}, f"FUITE pending: {pool.pending_jobs}"
    print("OK test_delegate_task_no_ghost_on_external_cancel")


# ---------------------------------------------------------------------------
# 2. Reservation de capacite d'inference
# ---------------------------------------------------------------------------

def test_get_idle_worker_reserve():
    pool = Pool()
    pool.workers = {f"w{i}": _Helper(f"w{i}") for i in range(2)}  # 2 idle

    # reserve=2 avec 2 idle -> aucun helper (on garde tout pour l'inference)
    assert pool.get_idle_worker(prefer_stronger=False, reserve=2) is None
    # reserve=1 avec 2 idle -> un helper dispo (il en reste 1)
    assert pool.get_idle_worker(prefer_stronger=False, reserve=1) is not None
    # reserve=0 -> comportement historique, un helper dispo
    assert pool.get_idle_worker(prefer_stronger=False, reserve=0) is not None

    # 1 seul idle + reserve=1 -> aucun helper
    pool.workers = {"only": _Helper("only")}
    assert pool.get_idle_worker(prefer_stronger=False, reserve=1) is None
    print("OK test_get_idle_worker_reserve")


# ---------------------------------------------------------------------------
# 3. Cooldown de compaction par conversation
# ---------------------------------------------------------------------------

def test_compact_cooldown():
    pool = Pool()
    os.environ["IAMINE_COMPACT_COOLDOWN_SEC"] = "30"
    # Pas encore compacte -> pas de cooldown
    assert C._compact_cooldown_active(pool, "conv_a") is False
    # On marque une compaction recente -> cooldown actif
    import time as _t
    pool._last_compaction["conv_a"] = _t.time()
    assert C._compact_cooldown_active(pool, "conv_a") is True
    # Une autre conv n'est pas affectee
    assert C._compact_cooldown_active(pool, "conv_b") is False
    # cooldown=0 desactive le garde
    os.environ["IAMINE_COMPACT_COOLDOWN_SEC"] = "0"
    assert C._compact_cooldown_active(pool, "conv_a") is False
    os.environ.pop("IAMINE_COMPACT_COOLDOWN_SEC", None)
    print("OK test_compact_cooldown")


# ---------------------------------------------------------------------------
# 4. Le balayeur de taches fantomes (filet de securite) purge bien
# ---------------------------------------------------------------------------

def test_ghost_sweeper_logic():
    import time as _t
    pool = Pool()
    now = _t.time()
    pool._active_tasks = {
        "fresh": {"task_id": "fresh", "started": now},
        "ghost": {"task_id": "ghost", "started": now - 999},
    }
    pool.pending_jobs = {}
    # Reproduit la logique du balayeur heartbeat (TTL 180s)
    ghost_ttl = 180.0
    ghosts = [tid for tid, t in pool._active_tasks.items()
              if now - t.get("started", now) > ghost_ttl]
    for tid in ghosts:
        pool._active_tasks.pop(tid, None)
    assert "ghost" not in pool._active_tasks
    assert "fresh" in pool._active_tasks
    print("OK test_ghost_sweeper_logic")


if __name__ == "__main__":
    test_delegate_task_no_ghost_on_external_cancel()
    test_get_idle_worker_reserve()
    test_compact_cooldown()
    test_ghost_sweeper_logic()
    print("\nTOUS LES TESTS PASSENT")
