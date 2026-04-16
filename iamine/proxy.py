"""Proxy worker Z2 — connecte les llama-servers locaux au pool IAMINE.

Au lieu de charger un modele via llama-cpp-python, ce proxy forward
les jobs du pool vers les llama-servers deja actifs sur GPU ROCm.

Usage:
    python -m iamine proxy --config proxy.json

Config proxy.json:
{
    "pool_url": "wss://cellule.ai/ws",
    "backends": [
        {
            "name": "Coder",
            "url": "http://127.0.0.1:8081",
            "model": "Qwen3.5-27B",
            "model_path": "models/Qwen_Qwen3.5-27B-Q4_K_M.gguf",
            "worker_id": "Eclipse-a5a3"
        },
        {
            "name": "RED",
            "url": "http://127.0.0.1:8083",
            "model": "Qwen3-30B-A3B",
            "model_path": "models/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf",
            "worker_id": "RED-z2"
        }
    ],
    "red_wake_schedule": "0 3 * * *",
    "red_analysis_duration_min": 60
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("iamine.proxy")

from iamine.tool_parser import _parse_qwen_tool_calls


@dataclass
class Backend:
    """Un llama-server local a proxifier."""
    name: str
    url: str  # http://127.0.0.1:PORT
    model: str  # nom du modele (pour le pool)
    model_path: str  # chemin GGUF (pour le register)
    worker_id: str  # ID au pool
    bench_tps: float = 0  # bench reel mesure (0 = auto)
    pause_on_red: bool = False  # pause quand RED est actif (cycle/chat)


@dataclass
class ProxyConfig:
    pool_url: str
    backends: list[Backend]
    red_wake_cron: str = "0 3 * * *"  # quand RED se reveille (cron)
    red_analysis_duration_min: int = 60  # duree de l'analyse RED

    @classmethod
    def from_file(cls, path: str) -> ProxyConfig:
        with open(path) as f:
            raw = json.load(f)
        backends = []
        for b in raw.get("backends", []):
            backends.append(Backend(
                name=b["name"],
                url=b["url"],
                model=b.get("model", "unknown"),
                model_path=b.get("model_path", ""),
                worker_id=b["worker_id"],
                bench_tps=b.get("bench_tps", 0),
                pause_on_red=b.get("pause_on_red", False),
            ))
        return cls(
            pool_url=raw.get("pool_url", "wss://cellule.ai/ws"),
            backends=backends,
            red_wake_cron=raw.get("red_wake_schedule", "0 3 * * *"),
            red_analysis_duration_min=raw.get("red_analysis_duration_min", 60),
        )


class ProxyWorker:
    """Un proxy pour un backend llama-server unique."""

    def __init__(self, backend: Backend, pool_url: str):
        self.backend = backend
        self.pool_url = pool_url
        self._ws = None
        self._running = False
        self._jobs_done = 0
        self._connected = False
        self._paused = False  # mis en pause quand RED se reveille
        self._http_session = None  # reuse aiohttp session across jobs

    async def _get_session(self):
        import aiohttp
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _close_session(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    async def start(self):
        self._running = True
        backoff = 5
        while self._running:
            if self._paused:
                await asyncio.sleep(5)
                continue
            try:
                await self._connect_and_serve()
                backoff = 5
            except (ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.warning(f"[{self.backend.name}] Connexion perdue: {e}")
            except Exception as e:
                log.warning(f"[{self.backend.name}] Erreur: {type(e).__name__}: {e}")
            if self._running and not self._paused:
                log.info(f"[{self.backend.name}] Reconnexion dans {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect_and_serve(self):
        """Se connecte au pool et forward les jobs."""
        log.info(f"[{self.backend.name}] Connexion au pool {self.pool_url}...")

        async with websockets.connect(self.pool_url) as ws:
            self._ws = ws
            self._connected = True

            # Detecter les infos systeme du Z2
            sys_info = self._build_system_info()
            await ws.send(json.dumps({
                "type": "register",
                "worker": sys_info,
            }))
            log.info(f"[{self.backend.name}] Connecte au pool — worker={self.backend.worker_id}")

            async for raw in ws:
                if self._paused:
                    # RED se reveille — fermer proprement
                    log.info(f"[{self.backend.name}] Pause — deconnexion du pool")
                    break

                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type in ("job", "delegate"):
                    asyncio.create_task(self._handle_job(ws, msg))
                elif msg_type == "ping":
                    await ws.send(json.dumps({
                        "type": "pong",
                        "worker_id": self.backend.worker_id,
                    }))
                elif msg_type == "welcome":
                    log.info(f"[{self.backend.name}] Welcome recu du pool")
                elif msg_type == "admin_chat":
                    asyncio.create_task(self._handle_admin_chat(ws, msg))
                elif msg_type == "command":
                    cmd = msg.get("cmd", "")
                    # Ignorer update_model — on ne change pas de modele
                    if cmd == "update_model":
                        log.info(f"[{self.backend.name}] Ignore update_model (proxy mode)")
                        await ws.send(json.dumps({
                            "type": "command_ack",
                            "cmd": "update_model",
                            "status": "ignored_proxy",
                        }))
                    elif cmd == "self_update":
                        # Only one backend in the proxy should trigger the upgrade
                        # (pip install is global, not per-backend)
                        if self.backend.name == (list(self.proxy.backends.keys())[0]
                                                   if hasattr(self, "proxy") and hasattr(self.proxy, "backends")
                                                   else self.backend.name):
                            log.info(f"[{self.backend.name}] self_update: pip install --upgrade iamine-ai")
                            await ws.send(json.dumps({
                                "type": "command_ack",
                                "cmd": "self_update",
                                "status": "updating",
                            }))
                            await self._cmd_self_update()
                        else:
                            log.debug(f"[{self.backend.name}] self_update ignore (already handled by primary backend)")
                    else:
                        log.debug(f"[{self.backend.name}] Commande ignoree: {cmd}")

            self._connected = False

    async def _handle_job(self, ws, msg: dict):
        """Forward un job au llama-server local via /v1/chat/completions."""
        job_id = msg.get("job_id", "?")
        messages = msg.get("messages", [])
        max_tokens = msg.get("max_tokens", 512)
        tools = msg.get("tools")

        log.info(f"[{self.backend.name}] Job {job_id} — {len(messages)} messages")
        t0 = time.time()

        try:
            # Appel HTTP au llama-server local
            import aiohttp
            payload = {
                "model": self.backend.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            session = await self._get_session()
            async with session.post(
                f"{self.backend.url}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {error_text[:200]}")
                data = await resp.json()

            # Extraire la reponse du format OpenAI
            choice = data.get("choices", [{}])[0]
            msg_data = choice.get("message", {})
            text = msg_data.get("content", "") or ""
            # Qwen 3.5 thinking mode : fusionner reasoning + content
            # reasoning_content ignore — ne pas montrer le thinking a l utilisateur

            # Parse Qwen function calls
            parsed_text, parsed_tool_calls = _parse_qwen_tool_calls(text)
            native_tc = msg_data.get("tool_calls")
            if native_tc:
                parsed_tool_calls = native_tc
            elif parsed_tool_calls:
                text = parsed_text
            usage = data.get("usage", {})
            tokens_generated = usage.get("completion_tokens", len(text.split()))

            duration = time.time() - t0
            tps = tokens_generated / duration if duration > 0 else 0

            result_msg = {
                "type": "result",
                "job_id": job_id,
                "worker_id": self.backend.worker_id,
                "text": text,
                "tokens_generated": tokens_generated,
                "tokens_per_sec": round(tps, 1),
                "duration_sec": round(duration, 2),
                "model": self.backend.model_path,
            }
            # Forward tool_calls (parsed from Qwen format or native)
            if parsed_tool_calls:
                result_msg["tool_calls"] = parsed_tool_calls
            await ws.send(json.dumps(result_msg))

            self._jobs_done += 1
            log.info(f"[{self.backend.name}] Job {job_id} OK — {tokens_generated} tok @ {tps:.1f} t/s")

        except Exception as e:
            log.error(f"[{self.backend.name}] Job {job_id} echoue: {e}")
            await ws.send(json.dumps({
                "type": "error",
                "job_id": job_id,
                "worker_id": self.backend.worker_id,
                "error": str(e),
            }))

    def _parse_function_call(self, text: str) -> dict | None:
        """Extrait un appel de fonction JSON du texte LLM."""
        import re
        # Chercher un bloc JSON avec "function" ou "tool"
        patterns = [
            r'\{[^{}]*"function"\s*:\s*"[^"]+"\s*,\s*"args"\s*:\s*\{[^{}]*\}[^{}]*\}',
            r'\{[^{}]*"function"\s*:\s*"[^"]+"\s*[^{}]*\}',
            r'```json\s*(\{[^`]+\})\s*```',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.DOTALL)
            if m:
                try:
                    raw = m.group(1) if m.lastindex else m.group(0)
                    call = json.loads(raw)
                    if "function" in call:
                        return call
                except (json.JSONDecodeError, IndexError):
                    pass
        return None

    async def _execute_function(self, call: dict) -> str:
        """Execute une fonction demandee par RED (securise)."""
        import subprocess
        func = call.get("function", "")
        args = call.get("args", {})

        # Whitelist des fonctions autorisees
        safe_functions = {
            "pool_status": lambda: self._exec_cmd("curl -s http://localhost:8080/v1/status"),
            "pool_power": lambda: self._exec_cmd("curl -s http://localhost:8080/v1/pool/power"),
            "worker_list": lambda: self._exec_cmd("curl -s http://localhost:8080/v1/status | python3 -c 'import sys,json;[print(w[\"id\"],w.get(\"model\",\"\").split(\"/\")[-1]) for w in json.load(sys.stdin).get(\"workers\",[])]'"),
            "read_file": lambda: self._safe_read(args.get("path", "")),
        }

        if func in safe_functions:
            try:
                result = safe_functions[func]()
                return result[:2000]  # limiter la taille
            except Exception as e:
                return f"Erreur execution {func}: {e}"
        return f"Fonction '{func}' non autorisee (whitelist: {', '.join(safe_functions.keys())})"

    _ALLOWED_CMD_PREFIXES = ("curl", "python3", "cat")

    async def _cmd_self_update(self) -> None:
        """Upgrade the iamine-ai package via pip and re-exec the proxy."""
        import subprocess, sys, os
        try:
            in_venv = hasattr(sys, "real_prefix") or (
                hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
            cmd = [
                sys.executable, "-m", "pip", "install", "--upgrade",
                "iamine-ai", "-i", "https://cellule.ai/pypi",
                "--extra-index-url", "https://pypi.org/simple", "-q",
            ]
            if not in_venv:
                cmd.insert(4, "--user")
                cmd.insert(4, "--break-system-packages")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                log.info(f"self_update pip ok — re-exec proxy...")
                # Re-exec ourselves to pick up the new code
                os.execv(sys.executable,
                          [sys.executable, "-m", "iamine", "proxy",
                           "-c", getattr(self, "config_path", "proxy.json")])
            else:
                log.warning(f"self_update pip failed: {r.stderr[:300]}")
        except Exception as e:
            log.warning(f"self_update error: {e}")

    def _exec_cmd(self, cmd: str) -> str:
        import subprocess
        try:
            args = shlex.split(cmd)
            if not args or args[0] not in self._ALLOWED_CMD_PREFIXES:
                return f"Error: commande refusee (autorisees: {', '.join(self._ALLOWED_CMD_PREFIXES)})"
            r = subprocess.run(args, capture_output=True, text=True, timeout=10)
            return r.stdout[:2000] if r.returncode == 0 else f"Error: {r.stderr[:500]}"
        except Exception as e:
            return f"Error: {e}"

    def _safe_read(self, path: str) -> str:
        """Lecture securisee — uniquement dans ~/iamine/ ou ~/RED/"""
        from pathlib import Path
        p = Path(path).expanduser().resolve()
        allowed = [Path.home() / "iamine", Path.home() / "RED"]
        if not any(str(p).startswith(str(a)) for a in allowed):
            return f"Acces refuse: {path} (hors ~/iamine/ et ~/RED/)"
        if not p.exists():
            return f"Fichier non trouve: {path}"
        return p.read_text(encoding="utf-8", errors="replace")[:3000]

    async def _handle_admin_chat(self, ws, msg: dict):
        """Forward un message admin_chat vers le LLM RED avec boucle function-calling."""
        chat_id = msg.get("chat_id", "?")
        messages = list(msg.get("messages", []))
        max_tokens = msg.get("max_tokens", 500)

        log.info(f"[{self.backend.name}] Admin chat {chat_id}")
        t0 = time.time()
        actions = []
        total_tokens = 0
        MAX_TURNS = 5

        try:
            import aiohttp

            for turn in range(MAX_TURNS):
                payload = {
                    "model": self.backend.model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                    "chat_template_kwargs": {"enable_thinking": False},
                }

                session = await self._get_session()
                async with session.post(
                    f"{self.backend.url}/v1/chat/completions",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {error_text[:200]}")
                    data = await resp.json()

                choice = data.get("choices", [{}])[0]
                msg_data = choice.get("message", {})
                text = msg_data.get("content", "") or ""
                reasoning = msg_data.get("reasoning_content", "")
                if reasoning and not text:
                    text = reasoning
                usage = data.get("usage", {})
                total_tokens += usage.get("completion_tokens", len(text.split()))

                # Chercher un appel de fonction dans la reponse
                call = self._parse_function_call(text)
                if call:
                    func_name = call.get("function", "?")
                    log.info(f"[{self.backend.name}] Admin chat function call: {func_name}")
                    result = await self._execute_function(call)
                    actions.append({"function": func_name, "result": result[:500]})
                    # Ajouter le resultat et re-appeler le LLM
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": f"Resultat de {func_name}:\n{result}"})
                    continue
                else:
                    # Pas de function call — reponse finale
                    break

            duration = time.time() - t0
            tps = total_tokens / duration if duration > 0 else 0

            await ws.send(json.dumps({
                "type": "admin_chat_response",
                "chat_id": chat_id,
                "text": text,
                "tokens_generated": total_tokens,
                "tokens_per_sec": round(tps, 1),
                "duration_sec": round(duration, 2),
            }))
            log.info(f"[{self.backend.name}] Admin chat {chat_id} OK — {total_tokens} tok @ {tps:.1f} t/s")

        except Exception as e:
            log.error(f"[{self.backend.name}] Admin chat {chat_id} echoue: {e}")
            await ws.send(json.dumps({
                "type": "admin_chat_response",
                "chat_id": chat_id,
                "text": f"[Erreur RED] {e}",
                "error": str(e),
            }))

    def _build_system_info(self) -> dict:
        """Construit les infos systeme pour le register, sans psutil."""
        from . import __version__
        # Infos hardcodees pour Z2 (evite les dependances)
        return {
            "worker_id": self.backend.worker_id,
            "hostname": "Z2",
            "platform": "linux",
            "cpu": "AMD Ryzen AI MAX+ PRO 395",
            "cpu_count": 16,
            "cpu_threads": 32,
            "ram_total_gb": 94.0,
            "ram_available_gb": 60.0,
            "model_path": self.backend.model_path,
            "ctx_size": 131072,
            "gpu_layers": -1,
            "gpu": "AMD gfx1151 ROCm",
            "gpu_vram_gb": 88.5,
            "has_gpu": True,
            "max_concurrent": 2,
            "max_tokens": 2048,
            "version": __version__,
            "bench_tps": self.backend.bench_tps or 45.0,
            "proxy_mode": True,
        }

    def pause(self):
        """Deconnecte ce proxy du pool (RED se reveille)."""
        self._paused = True
        log.info(f"[{self.backend.name}] Mis en pause")

    def resume(self):
        """Reconnecte ce proxy au pool (RED a fini son analyse)."""
        self._paused = False
        log.info(f"[{self.backend.name}] Reprise")

    async def _cleanup(self):
        await self._close_session()

    def stop(self):
        self._running = False
        self._paused = False
        # Schedule session cleanup
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._cleanup())
        except RuntimeError:
            pass


class ProxyManager:
    """Gere N backends proxy avec le cycle veille/reveil de RED."""

    def __init__(self, config: ProxyConfig):
        self.config = config
        self.proxies: list[ProxyWorker] = []
        self._running = False

        for backend in config.backends:
            self.proxies.append(ProxyWorker(backend, config.pool_url))

    async def start(self):
        self._running = True

        print()
        print(" * IAMINE PROXY   Z2 mode")
        print(f" * POOL           {self.config.pool_url}")
        print(f" * BACKENDS       {len(self.proxies)}")
        for p in self.proxies:
            print(f"   - {p.backend.name}: {p.backend.url} ({p.backend.model})")
        print(f" * RED WAKE       {self.config.red_wake_cron}")
        print(f" * ANALYSIS       {self.config.red_analysis_duration_min} min")
        print()

        tasks = []
        for proxy in self.proxies:
            tasks.append(asyncio.create_task(proxy.start()))

        # Lancer le scheduler RED (cron) + flag watcher (dynamique)
        tasks.append(asyncio.create_task(self._red_scheduler()))
        tasks.append(asyncio.create_task(self._red_flag_watcher()))

        await asyncio.gather(*tasks)

    async def _red_scheduler(self):
        """Gere le cycle veille/reveil de RED toutes les 24h.

        Pendant l'analyse RED :
        - Seuls les backends marques pause_on_red se deconnectent
        - Scout reste routable en permanence
        Apres l'analyse :
        - Les proxies pauses se reconnectent
        """
        while self._running:
            next_wake = self._time_until_next_wake()
            log.info(f"RED prochain reveil dans {next_wake // 3600:.0f}h{(next_wake % 3600) // 60:.0f}m")
            await asyncio.sleep(next_wake)

            if not self._running:
                break

            # RED se reveille — pause des proxies concernes
            paused = [p for p in self.proxies if p.backend.pause_on_red]
            kept = [p for p in self.proxies if not p.backend.pause_on_red]
            log.warning(f"RED SE REVEILLE — pause {len(paused)} proxies, {len(kept)} restent routables")
            for proxy in paused:
                proxy.pause()

            # Attendre la duree de l'analyse
            await asyncio.sleep(self.config.red_analysis_duration_min * 60)

            # RED a fini — reprendre les proxies pauses
            log.warning("RED A FINI — reprise des proxies")
            for proxy in paused:
                proxy.resume()

    async def _red_flag_watcher(self):
        """Surveille /tmp/red-active pour pause/resume dynamique par le smolagent.

        Le smolagent ecrit /tmp/red-active au debut du cycle et le supprime a la fin.
        Ceci permet au proxy de reagir immediatement sans attendre le cron.
        """
        flag_path = Path("/tmp/red-active")
        was_active = False
        while self._running:
            is_active = flag_path.exists()
            if is_active and not was_active:
                # RED vient de s'activer
                log.warning("RED FLAG ACTIVE — pause dynamique des proxies concernes")
                for proxy in self.proxies:
                    if proxy.backend.pause_on_red:
                        proxy.pause()
            elif not is_active and was_active:
                # RED vient de finir
                log.warning("RED FLAG REMOVED — reprise des proxies")
                for proxy in self.proxies:
                    if proxy.backend.pause_on_red:
                        proxy.resume()
            was_active = is_active
            await asyncio.sleep(5)  # poll toutes les 5s

    def _time_until_next_wake(self) -> float:
        """Calcule le nombre de secondes jusqu'au prochain reveil RED (cron simplifie)."""
        # Parse cron simplifie : "H M * * *" → heure H, minute M chaque jour
        parts = self.config.red_wake_cron.split()
        if len(parts) >= 2:
            try:
                cron_min = int(parts[0])
                cron_hour = int(parts[1])
            except ValueError:
                cron_hour, cron_min = 3, 0  # fallback 3h00
        else:
            cron_hour, cron_min = 3, 0

        import datetime
        now = datetime.datetime.now()
        target = now.replace(hour=cron_hour, minute=cron_min, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds()

    def stop(self):
        self._running = False
        for proxy in self.proxies:
            proxy.stop()


async def run_proxy(config_path: str) -> None:
    """Point d'entree du proxy."""
    config = ProxyConfig.from_file(config_path)
    manager = ProxyManager(config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, manager.stop)
        except NotImplementedError:
            pass

    try:
        await manager.start()
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        pass
    finally:
        manager.stop()
        print()
        print(" * IAMINE PROXY   Stopped.")
        print()
