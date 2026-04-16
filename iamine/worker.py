"""Worker IAMINE — se connecte au pool et traite les requêtes d'inférence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from enum import Enum

import websockets
from websockets.exceptions import ConnectionClosed

from .config import WorkerConfig
from .engine import InferenceEngine
from .wallet import Wallet

log = logging.getLogger("iamine.worker")


class WorkerState(Enum):
    IDLE = "idle"
    BUSY = "busy"
    PAUSED = "paused"
    OFFLINE = "offline"


class Worker:
    """Worker principal — noeud d'inference distribue."""

    def __init__(self, config: WorkerConfig, config_path: str = "config.json"):
        self.config = config
        self.config_path = config_path  # chemin du fichier config (pour update_model)
        self.engine = InferenceEngine(config.model, config.limits)
        self.wallet = Wallet()
        self.state = WorkerState.OFFLINE
        self._ws = None
        self._running = False
        self._jobs_done = 0
        self._jobs_failed = 0
        self._start_time = 0.0

    async def start(self) -> None:
        """Démarre le worker : charge le modèle puis se connecte au pool."""
        self._running = True
        self._start_time = time.time()

        self._print_banner()

        # Charger le modèle
        log.info("Initialisation du moteur d'inférence...")
        self.engine.load()

        # Lancer le serveur local wallet (port 8081)
        asyncio.create_task(self._run_local_api())

        # Auto-update periodique (verifie la version du pool)
        if os.environ.get("IAMINE_AUTO_UPDATE", "1") != "0":
            asyncio.create_task(self._auto_update_loop())

        # M12: migrate legacy iamine.org URL to cellule.ai
        if "iamine.org" in self.config.pool.url:
            old_url = self.config.pool.url
            self.config.pool.url = old_url.replace("iamine.org", "cellule.ai")
            log.info(f"URL migrated: {old_url} -> {self.config.pool.url}")

        # Boucle de connexion au pool (reconnexion auto avec backoff)
        backoff = 5
        while self._running:
            try:
                await self._connect_and_serve()
                backoff = 5  # reset apres connexion reussie
            except (ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.warning(f"Connexion perdue : {e}")
            except Exception as e:
                # Catch-all : HTTP 502, InvalidStatusCode, etc.
                log.warning(f"Erreur connexion : {type(e).__name__}: {e}")
            if self._running:
                # M12: After 3 failures, try re-discovering a better pool
                if backoff >= 40:  # ~3rd retry (5, 10, 20, 40)
                    try:
                        from .pool_discovery import discover_best_pool
                        model_path = (self.config.model.path or "").split("/")[-1]
                        bench_tps = getattr(self.engine, "bench_tps", 0) or 0
                        new_url = discover_best_pool(worker_model=model_path, worker_tps=float(bench_tps))
                        if new_url != self.config.pool.url:
                            log.info(f"Failover: migration vers {new_url} (ancien: {self.config.pool.url})")
                            self.config.pool.url = new_url
                            # Persist to config.json so relaunch does not retry the dead pool
                            try:
                                import json as _json
                                from pathlib import Path as _Path
                                cp = _Path(self.config_path).resolve()
                                if cp.exists():
                                    with open(cp) as _f:
                                        _cfg = _json.load(_f)
                                    _cfg.setdefault("pool", {})["url"] = new_url
                                    with open(cp, "w") as _f:
                                        _json.dump(_cfg, _f, indent=4)
                                    log.info(f"Persisted pool.url to {cp}")
                            except Exception as _pe:
                                log.debug(f"Persist pool.url failed: {_pe}")
                            backoff = 5  # reset backoff for new pool
                    except Exception as e:
                        log.debug(f"Re-discovery failed: {e}")
                log.info(f"Reconnexion dans {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # 5s, 10s, 20s, 40s, 60s max

    async def _connect_and_serve(self) -> None:
        """Se connecte au pool et écoute les jobs."""
        url = self.config.pool.url
        log.info(f"Connexion au pool {url}...")

        async with websockets.connect(url) as ws:
            self._ws = ws
            self.state = WorkerState.IDLE

            # Envoyer les infos du worker au pool (handshake)
            info = self.config.system_info()
            info["bench_tps"] = getattr(self.engine, "bench_tps", None)
            await self._send(ws, {
                "type": "register",
                "worker": info,
            })

            log.info(f"Connecté au pool — worker={self.config.pool.worker_id}")
            self._print_status()

            # Écouter les messages du pool
            # Les jobs sont lances en tache de fond pour ne pas bloquer les pings
            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")
                if msg_type in ("job", "delegate"):
                    # Executer en parallele — ne bloque pas la boucle de messages
                    asyncio.create_task(self._handle_message(ws, msg))
                else:
                    await self._handle_message(ws, msg)

    async def _handle_message(self, ws, msg: dict) -> None:
        """Traite un message reçu du pool."""
        msg_type = msg.get("type")

        if msg_type == "job":
            await self._handle_job(ws, msg)
        elif msg_type == "delegate":
            # Tâche déléguée par le pool — un autre worker a besoin d'aide
            src = msg.get("source_worker", "?")
            task = msg.get("task_type", "?")
            log.info(f"DELEGATE from {src} ({task}) — helping...")
            await self._handle_job(ws, msg)
        elif msg_type == "reward":
            amount = msg.get("amount", 0)
            label = msg.get("label", "REWARD")
            credits = msg.get("credits", 0)
            log.info(f"LOYALTY {label}: +{amount} $IAMINE (total: {credits})")
            if self.wallet and credits > self.wallet.credits:
                self.wallet.data["credits"] = credits
                self.wallet.save()
        elif msg_type == "ping":
            await self._send(ws, {"type": "pong", "worker_id": self.config.pool.worker_id})
        elif msg_type == "welcome":
            # Le pool envoie le token API au worker apres le register
            api_token = msg.get("api_token", "")
            if api_token:
                self.wallet.init(self.config.pool.worker_id, api_token)
                # Sync credits depuis le pool
                try:
                    import urllib.request, json as _json
                    pool_url = self.config.pool.url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")
                    resp = urllib.request.urlopen(f"{pool_url}/v1/wallet/{api_token}", timeout=5)
                    data = _json.loads(resp.read())
                    pool_credits = data.get("credits", 0)
                    if pool_credits > self.wallet.credits:
                        self.wallet.data["credits"] = pool_credits
                        self.wallet.save()
                except Exception:
                    pass
                self.wallet.print_status()
        elif msg_type == "status":
            self._print_status()
        elif msg_type == "command":
            await self._handle_command(ws, msg)
        else:
            log.debug(f"Message inconnu : {msg_type}")

    async def _handle_job(self, ws, msg: dict) -> None:
        """Exécute un job d'inférence et renvoie le résultat au pool."""
        job_id = msg.get("job_id", "?")
        messages = msg.get("messages", [])
        max_tokens = msg.get("max_tokens")
        tools = msg.get("tools")

        self.state = WorkerState.BUSY
        log.info(f"Job {job_id} reçu — {len(messages)} messages")

        try:
            # Inférence dans un thread séparé pour ne pas bloquer l'event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, self.engine.generate, messages, max_tokens, tools
            )

            result_msg = {
                "type": "result",
                "job_id": job_id,
                "worker_id": self.config.pool.worker_id,
                "text": result.text,
                "tokens_generated": result.tokens_generated,
                "tokens_per_sec": result.tokens_per_sec,
                "duration_sec": result.duration_sec,
                "model": result.model,
            }
            if result.tool_calls:
                result_msg["tool_calls"] = result.tool_calls
            await self._send(ws, result_msg)

            self._jobs_done += 1
            # 100 tokens generes = 1 $IAMINE
            credit = result.tokens_generated / 100.0
            self.wallet.earn(credit)
            log.info(f"Job {job_id} OK — {result.tokens_generated} tok @ {result.tokens_per_sec} t/s — +{credit:.2f} $IAMINE (wallet: {self.wallet.credits:.2f})")

        except Exception as e:
            self._jobs_failed += 1
            log.error(f"Job {job_id} échoué : {e}")
            await self._send(ws, {
                "type": "error",
                "job_id": job_id,
                "worker_id": self.config.pool.worker_id,
                "error": str(e),
            })

        self.state = WorkerState.IDLE

    def _restart_self(self) -> None:
        """Relance le process worker (Linux/systemd, Windows frozen, Windows dev)."""
        log.info("Restarting worker process...")
        args = sys.argv[1:] if sys.argv else []
        # Frozen exe on Windows (PyInstaller --onefile) : parent+child extract/cleanup
        # of the _MEI runtime dir race if we exec/Popen directly (modules like
        # unicodedata become unloadable in the child). Solution : spawn a COMPLETELY
        # decoupled batch that sleeps 3s then relaunches the exe. Batch is a shell
        # process (cmd.exe), not a python child, so no _MEI shared.
        if getattr(sys, "frozen", False):
            import os as _os
            import tempfile
            import subprocess
            exe_path = sys.executable
            # Quote args with embedded spaces
            quoted_args = " ".join(f"\"{a}\"" if " " in a else a for a in args)
            batch_content = (
                "@echo off\r\n"
                "timeout /t 10 /nobreak > nul\r\n"
                f"start \"\" \"{exe_path}\" {quoted_args}\r\n"
            )
            batch_path = _os.path.join(tempfile.gettempdir(), "cellule-restart.bat")
            with open(batch_path, "w") as bf:
                bf.write(batch_content)
            log.info(f"Spawning decoupled relaunch via {batch_path} (10s delay)")
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP : completely detach from parent
            DETACHED = 0x00000008
            NEW_PGROUP = 0x00000200
            subprocess.Popen(
                ["cmd", "/c", batch_path],
                creationflags=DETACHED | NEW_PGROUP,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            sys.exit(0)
        # Non-frozen (Linux/dev) : os.execv is fine
        cmd = [sys.executable, "-m", "iamine"] + args
        try:
            os.execv(sys.executable, cmd)
        except Exception:
            import subprocess
            subprocess.Popen(cmd)
            sys.exit(0)

    async def _handle_command(self, ws, msg: dict) -> None:
        """Traite une commande administrative du pool."""
        cmd = msg.get("cmd", "")
        log.info(f"Command received: {cmd}")

        if cmd == "update_model":
            await self._cmd_update_model(ws, msg)
        elif cmd == "self_update":
            await self._cmd_self_update(ws)
        elif cmd == "set_ctx":
            new_ctx = msg.get("ctx_size", 4096)
            log.info(f"Setting ctx_size to {new_ctx}")
            # Mettre a jour config.json et redemarrer
            from pathlib import Path
            config_path = Path(self.config_path).resolve()
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
                cfg.setdefault("model", {})["ctx-size"] = new_ctx
                with open(config_path, "w") as f:
                    json.dump(cfg, f, indent=4)
            await self._send(ws, {"type": "command_ack", "cmd": "set_ctx", "status": "restarting", "ctx_size": new_ctx})
            self._restart_self()
        elif cmd == "restart":
            log.info("Pool requests restart")
            await self._send(ws, {"type": "command_ack", "cmd": cmd, "status": "restarting"})
            self._restart_self()
        else:
            log.warning(f"Unknown command: {cmd}")
            await self._send(ws, {"type": "command_ack", "cmd": cmd, "status": "unknown"})

    async def _cmd_update_model(self, ws, msg: dict) -> None:
        """Telecharge un nouveau modele et redemarre."""
        import urllib.request
        from pathlib import Path

        model_url = msg.get("model_url", "")
        model_path = msg.get("model_path", "")
        ctx_size = msg.get("ctx_size", 4096)
        gpu_layers = msg.get("gpu_layers", 0)
        threads = msg.get("threads", 4)

        # Securite : uniquement iamine.org ou huggingface
        if "iamine.org" not in model_url and "cellule.ai" not in model_url and "huggingface" not in model_url:
            log.warning(f"URL refused: {model_url}")
            await self._send(ws, {"type": "command_ack", "cmd": "update_model", "status": "refused"})
            return

        dest = Path(model_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        await self._send(ws, {"type": "command_ack", "cmd": "update_model", "status": "downloading"})

        if not dest.exists():
            log.info(f"Downloading {model_url}...")
            try:
                urllib.request.urlretrieve(model_url, str(dest))
                log.info(f"Download complete: {dest}")
            except Exception as e:
                log.error(f"Download failed: {e}")
                await self._send(ws, {"type": "command_ack", "cmd": "update_model", "status": "download_failed"})
                return

        # Mettre a jour le config (utilise le meme fichier que -c)
        config_path = Path(self.config_path).resolve()
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
        else:
            cfg = {"pool": {"url": "wss://cellule.ai/ws"}, "model": {}, "limits": {}}

        cfg["model"]["path"] = model_path
        cfg["model"]["ctx-size"] = ctx_size
        cfg["model"]["gpu-layers"] = gpu_layers
        cfg["model"]["threads"] = threads
        cfg["model"]["pool-assigned"] = True  # empeche --auto de re-overrider
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=4)

        # Sauvegarder l'assignement dans assigned_model.json (persistant, survive --auto)
        import time as _time
        assigned = {
            "model_id": msg.get("model_id", ""),
            "model_path": model_path,
            "ctx_size": ctx_size,
            "gpu_layers": gpu_layers,
            "assigned_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            assigned_path = config_path.parent / "assigned_model.json"
            with open(assigned_path, "w") as f:
                json.dump(assigned, f, indent=4)
            log.info(f"Saved {assigned_path}: {model_path}")
        except Exception as e:
            log.warning(f"Failed to save assigned_model.json: {e}")

        log.info(f"Config updated: {model_path} ctx={ctx_size} gpu={gpu_layers}")
        await self._send(ws, {"type": "command_ack", "cmd": "update_model", "status": "restarting"})
        self._restart_self()

    async def _auto_update_loop(self) -> None:
        """Verifie periodiquement la version du pool et se met a jour si necessaire."""
        import subprocess, urllib.request
        interval = int(os.environ.get("IAMINE_UPDATE_INTERVAL", "86400"))  # 24h default
        await asyncio.sleep(300)  # attendre 5 min avant le premier check (eviter boucle restart)

        while self._running:
            try:
                # Lire la version du pool
                pool_url = self.config.pool.url.replace("wss://", "https://").replace("ws://", "http://").replace("/ws", "")
                resp = urllib.request.urlopen(f"{pool_url}/v1/status", timeout=10)
                data = json.loads(resp.read())
                pool_version = data.get("version", "0.0.0")

                # Comparer : si plus de n-1 de retard, updater
                from . import __version__
                local = tuple(int(x) for x in __version__.split("."))
                remote = tuple(int(x) for x in pool_version.split("."))

                if len(local) >= 3 and len(remote) >= 3 and local[:2] == remote[:2]:
                    behind = remote[2] - local[2]
                else:
                    behind = 1 if remote > local else 0

                if behind > 1 and self.state != WorkerState.BUSY:
                    log.info(f"Auto-update: v{__version__} → v{pool_version} ({behind} patches behind)")
                    in_venv = hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
                    cmd = [
                        sys.executable, "-m", "pip", "install", "--upgrade",
                        "iamine-ai", "-i", "https://cellule.ai/pypi",
                        "--extra-index-url", "https://pypi.org/simple", "-q"
                    ]
                    if not in_venv:
                        cmd.insert(4, "--break-system-packages")
                    subprocess.run(cmd)
                    log.info("Auto-update done, restarting...")
                    self._restart_self()
                elif behind > 0:
                    log.debug(f"Auto-update: v{__version__} (pool v{pool_version}, within n-1 tolerance)")
            except Exception as e:
                log.debug(f"Auto-update check failed: {e}")

            await asyncio.sleep(interval)

    async def _cmd_self_update(self, ws) -> None:
        """Met a jour le package iamine-ai depuis le PyPI prive."""
        import subprocess
        # Frozen exe (PyInstaller) : pip n'existe pas dans le bundle.
        # L'utilisateur doit re-telecharger l'exe depuis cellule.ai.
        if getattr(sys, "frozen", False):
            log.info("Self-update skipped: running as bundled exe. "
                     "Re-download latest from https://cellule.ai/docs/install-worker.html")
            await self._send(ws, {"type": "command_ack", "cmd": "self_update", "status": "skipped_frozen"})
            return
        log.info("Self-update: pip install --upgrade iamine-ai...")
        await self._send(ws, {"type": "command_ack", "cmd": "self_update", "status": "updating"})
        # Detecter si on est dans un venv (pas besoin de --break-system-packages)
        in_venv = hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
        cmd = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            "iamine-ai", "-i", "https://cellule.ai/pypi",
            "--extra-index-url", "https://pypi.org/simple", "-q"
        ]
        if not in_venv:
            cmd.insert(4, "--break-system-packages")
        subprocess.run(cmd)
        log.info("Self-update done, running discovery before restart...")
        # M12: re-discover best pool after update
        try:
            from .pool_discovery import discover_best_pool
            model_path = (self.config.model.path or "").split("/")[-1]
            bench_tps = getattr(self.engine, "bench_tps", 0) or 0
            new_url = discover_best_pool(worker_model=model_path, worker_tps=float(bench_tps))
            if new_url and new_url != self.config.pool.url:
                log.info(f"Discovery: migrating {self.config.pool.url} -> {new_url}")
                self.config.pool.url = new_url
                # Update config.json
                import json as _json
                from pathlib import Path as _Path
                cp = _Path(self.config_path).resolve()
                if cp.exists():
                    with open(cp) as f: cfg = _json.load(f)
                    cfg["pool"]["url"] = new_url
                    with open(cp, "w") as f: _json.dump(cfg, f, indent=2)
        except Exception as e:
            log.debug(f"Post-update discovery failed: {e}")
        self._restart_self()

    async def _send(self, ws, data: dict) -> None:
        await ws.send(json.dumps(data))

    async def _run_local_api(self, port: int = 8081) -> None:
        """Mini serveur HTTP local — expose le wallet et l'etat du worker.

        La page web detecte automatiquement ce serveur pour afficher
        le token API et le solde sans que l'utilisateur ait a le saisir.
        """
        from aiohttp import web

        async def handle_wallet(request):
            data = {
                **self.wallet.status(),
                "state": self.state.value,
                "jobs_done": self._jobs_done,
                "api_token_full": self.wallet.api_token,
                "pool_url": self.config.pool.url,
                "model": self.engine.model_name if self.engine.loaded else "",
                "uptime_sec": round(time.time() - self._start_time),
            }
            return web.json_response(data, headers={"Access-Control-Allow-Origin": "*"})

        async def handle_cors(request):
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            })

        app = web.Application()
        app.router.add_get("/wallet", handle_wallet)
        app.router.add_route("OPTIONS", "/wallet", handle_cors)

        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            log.info(f"Local API on http://127.0.0.1:{port}/wallet")
        except OSError:
            log.warning(f"Port {port} busy — local API disabled")

    def stop(self) -> None:
        """Arrête le worker proprement."""
        log.info("Arrêt du worker...")
        self._running = False
        self.state = WorkerState.OFFLINE

    def _print_banner(self) -> None:
        """Affiche la banniere au demarrage."""
        cfg = self.config
        print()
        from . import __version__
        print(f" * IAMINE      v{__version__}")
        print(f" * MODEL       {cfg.model.path}")
        print(f" * THREADS     {cfg.model.threads}  ctx={cfg.model.ctx_size}")
        print(f" * POOL        {cfg.pool.url}")
        print(f" * WORKER      {cfg.pool.worker_id}")
        print(f" * LOCAL API   http://127.0.0.1:8081/wallet")
        print(f" * MAX TOKENS  {cfg.limits.max_tokens}")
        print(f" * CONCURRENT  {cfg.limits.max_concurrent}")
        print()

    def _print_status(self) -> None:
        uptime = time.time() - self._start_time if self._start_time else 0
        h, m = divmod(int(uptime), 3600)
        m, s = divmod(m, 60)
        print(f" * STATUS      {self.state.value} | jobs={self._jobs_done} err={self._jobs_failed} | uptime={h}h{m:02d}m{s:02d}s")


async def run_worker(config: WorkerConfig, config_path: str = "config.json") -> None:
    """Point d'entrée async du worker."""
    worker = Worker(config, config_path=config_path)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            pass

    try:
        await worker.start()
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        pass
    finally:
        worker.stop()
        wallet = worker.wallet
        if wallet.credits > 0:
            print()
            wallet.print_status()
        print()
        print(" * IAMINE      Stopped. See you soon!")
        print()
