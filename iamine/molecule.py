"""IAMINE Molecule — connexion pool-to-pool.

Pool B se connecte a Pool A via WebSocket en tant que super-worker.
Quand Pool A recoit un job pour un modele que seul Pool B possede,
il forward le job a Pool B qui le traite avec ses propres workers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger("iamine.molecule")


class MoleculeLink:
    """Liaison entre deux pools IAMINE.

    Pool B (local) se connecte a Pool A (remote) et annonce ses modeles.
    Quand Pool A forward un job, Pool B le traite localement et renvoie le resultat.
    """

    def __init__(self, local_pool, remote_url: str, pool_id: str = "molecule"):
        self.pool = local_pool  # reference au Pool local
        self.remote_url = remote_url
        self.pool_id = pool_id
        self._ws = None
        self._running = False

    def get_local_models(self) -> list[str]:
        """Retourne les modeles disponibles sur le pool local."""
        models = set()
        for w in self.pool.workers.values():
            model = w.info.get("model_path", "")
            if model:
                models.add(model.split("/")[-1].replace(".gguf", ""))
        return list(models)

    def get_local_capacity(self) -> dict:
        """Capacite du pool local."""
        total_tps = sum(w.info.get("bench_tps", 0) or 0 for w in self.pool.workers.values())
        return {
            "pool_id": self.pool_id,
            "workers_online": len(self.pool.workers),
            "workers_busy": sum(1 for w in self.pool.workers.values() if w.busy),
            "pool_load": self.pool.pool_load,
            "models": self.get_local_models(),
            "capacity_tps": round(total_tps, 1),
        }

    async def start(self):
        """Demarre la liaison molecule avec reconnect auto."""
        self._running = True
        backoff = 5
        while self._running:
            try:
                await self._connect()
                backoff = 5
            except Exception as e:
                log.warning(f"Molecule link lost: {e}")
            if self._running:
                log.info(f"Molecule reconnect in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect(self):
        """Se connecte au pool distant et ecoute les jobs."""
        log.info(f"Molecule: connecting to {self.remote_url}")

        async with websockets.connect(self.remote_url) as ws:
            self._ws = ws

            # S'enregistrer comme pool (pas comme worker)
            capacity = self.get_local_capacity()
            await ws.send(json.dumps({
                "type": "register",
                "worker": {
                    "worker_id": f"Pool-{self.pool_id}",
                    "cpu": f"pool-{capacity['workers_online']}w",
                    "ram_total_gb": 0,
                    "model_path": capacity["models"][0] if capacity["models"] else "multi",
                    "ctx_size": 8192,
                    "platform": "molecule",
                    "bench_tps": capacity["capacity_tps"],
                    "version": "0.2.4",
                    "pool_id": self.pool_id,
                    "models": capacity["models"],
                },
            }))

            log.info(f"Molecule: registered as Pool-{self.pool_id} with {capacity['workers_online']}w")

            # Ecouter les messages du pool distant
            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "welcome":
                    log.info(f"Molecule: welcome from remote pool")

                elif msg_type == "ping":
                    # Repondre au ping avec le status du pool local
                    await ws.send(json.dumps({
                        "type": "pong",
                        "worker_id": f"Pool-{self.pool_id}",
                        **self.get_local_capacity(),
                    }))

                elif msg_type in ("job", "delegate"):
                    # Job recu du pool distant → traiter localement
                    asyncio.create_task(self._handle_remote_job(ws, msg))

                elif msg_type == "reward":
                    amount = msg.get("amount", 0)
                    log.info(f"Molecule: reward +{amount} from remote pool")

    async def _handle_remote_job(self, ws, msg: dict):
        """Traite un job recu du pool distant en le soumettant au pool local."""
        job_id = msg.get("job_id", "")
        messages = msg.get("messages", [])
        max_tokens = msg.get("max_tokens", 256)

        log.info(f"Molecule: job {job_id} received from remote → submitting locally")

        try:
            # Soumettre au pool local
            result = await self.pool.submit_job(
                messages=messages,
                max_tokens=max_tokens,
            )

            # Renvoyer le resultat au pool distant
            await ws.send(json.dumps({
                "type": "result",
                "job_id": job_id,
                "worker_id": f"Pool-{self.pool_id}:{result.get('worker_id', '?')}",
                "text": result.get("text", ""),
                "tokens_generated": result.get("tokens_generated", 0),
                "tokens_per_sec": result.get("tokens_per_sec", 0),
                "duration_sec": result.get("duration_sec", 0),
                "model": result.get("model", ""),
            }))

            log.info(f"Molecule: job {job_id} done — {result.get('tokens_generated', 0)} tokens")

        except Exception as e:
            log.error(f"Molecule: job {job_id} failed: {e}")
            await ws.send(json.dumps({
                "type": "error",
                "job_id": job_id,
                "error": f"Molecule pool error: {e}",
            }))
