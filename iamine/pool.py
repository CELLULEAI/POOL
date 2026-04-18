"""Orchestrateur / Pool IAMINE — reçoit les requêtes et les dispatch aux workers."""

from __future__ import annotations

import asyncio
import aiohttp
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
import uuid

from pathlib import Path

# Token derivation + server secret — moved to core/utils.py
from .core.utils import _derive_api_token, _derive_account_token, _SERVER_SECRET
# Assist / Think Tool / Boost — moved to core/assist.py
from .core.assist import (
    handle_think, handle_pool_assist, handle_boost, inject_think_tool,
    handle_auto_review,
    handle_sub_agent_pipeline,
    get_assist_worker as _get_assist_worker,
    boost_eligible as _boost_eligible,
    _tool_only_workers, _parse_model_size_from_path,
    BOOST_LOAD_THRESHOLD, BOOST_MAX_USERS, BOOST_ACTIVITY_WINDOW,
    BOOST_REVIEW_TIMEOUT, BOOST_REVIEW_MAX_TOKENS,
)

from .core.compaction import handle_compaction, handle_meta_compaction, async_compact

# Credits, rate limiting, loyalty — moved to core/credits.py
from .core.credits import (
    check_rate_limit as _check_rate_limit,
    update_worker_db as _update_worker_db_fn,
    save_benchmark as _save_benchmark_fn,
    is_memory_enabled as _is_memory_enabled_fn,
    embed_facts as _embed_facts_fn,
    save_conv_background as _save_conv_background_fn,
    credit_worker_for_job,
    loyalty_rewards,
    credit_sync_loop,
)


from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from iamine import __version__
from .core.utils import parse_model_size, parse_version, strip_thinking, should_disable_thinking

# Backward-compat aliases (underscore prefix) for legacy call sites in pool.py.
# Chunk 1 refactor : canonical definitions live in core/utils.py.
_parse_model_size = parse_model_size
_parse_version = parse_version
_strip_thinking = strip_thinking

log = logging.getLogger("iamine.pool")

app = FastAPI(title="IAMINE Pool", version=__version__)



# Servir les fichiers statiques (page web)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    # Auto-recover: si index.html manque dans le source, copier depuis le wheel installe
    _wheel_static = Path(__file__).resolve().parent / "static"
    import importlib.util as _ilu
    _spec = _ilu.find_spec("iamine")
    if _spec and _spec.origin:
        _wheel_static = Path(_spec.origin).parent / "static"
    if not (static_dir / "index.html").exists() and (_wheel_static / "index.html").exists():
        import shutil
        log.warning("static/index.html missing from source — recovering from installed wheel")
        for _f in _wheel_static.iterdir():
            if _f.is_file() and not (static_dir / _f.name).exists():
                shutil.copy2(str(_f), str(static_dir / _f.name))
                log.info(f"  recovered: {_f.name}")
    app.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")


from .core.types import ConnectedWorker, PendingJob
from .core.agent_memory import (capture_observation, trigger_consolidation,
                                get_episode_context, ENABLED as AGENT_MEMORY_ENABLED)
from .core.agent_memory_phase2 import hybrid_retrieve as _hybrid_retrieve, full_consolidation


class Pool:
    """Gestionnaire central des workers et des jobs."""

    # Fallback si DB indisponible — ecrase par pool_config au startup
    SYSTEM_PROMPT = (
        "You are a helpful AI assistant. "
        "Your name is IAMINE (only mention if asked). "
        "RULES: "
        "- Be concise and direct. Short questions = 1-3 sentences max. "
        "- Never repeat the user question. No preamble. "
        "- For code: complete code, minimal comments. "
        "- For lists: max 5 items unless asked for more. "
        "- Answer in the same language as the user. "
        "- NEVER output JSON, metadata, follow_ups or suggestions. Plain text only. "
        "MEMORY: "
        "- You have access to long-term memory. If memory context is provided above, USE IT to personalize your responses. "
        "- When the user shares personal info, ask: Voulez-vous que je m en souvienne ? "
        "- If they say yes, confirm naturally. The system handles storage."
    )

    HEARTBEAT_INTERVAL = 30   # ping toutes les 30s
    HEARTBEAT_TIMEOUT = 60    # considere mort apres 60s sans reponse

    WELCOME_BONUS = 50.0  # $IAMINE offerts a la premiere connexion (anti-farming: reduit de 500)
    PREPROD_MODE = True  # Désactive les crédits — API ouverte pour les tests

    QUEUE_TIMEOUT = 30   # secondes max à attendre qu'un worker se libère
    QUEUE_MAX_SIZE = 50  # max de jobs en attente simultanément
    RATE_LIMIT_PER_MIN = 300  # max requêtes par minute par source (relevé pour preprod)
    MAX_PENDING_PER_TOKEN = 10   # max pending jobs par token en mode QUEUE

    # Boost mode constants — moved to core/assist.py
    BOOST_LOAD_THRESHOLD = BOOST_LOAD_THRESHOLD
    BOOST_MAX_USERS = BOOST_MAX_USERS
    BOOST_ACTIVITY_WINDOW = BOOST_ACTIVITY_WINDOW
    BOOST_REVIEW_TIMEOUT = BOOST_REVIEW_TIMEOUT
    BOOST_REVIEW_MAX_TOKENS = BOOST_REVIEW_MAX_TOKENS

    # Checker ladder — validation synchrone par un LLM plus gros
    # Fallback si DB indisponible — ecrase par pool_config au startup
    CHECKER_ENABLED = True             # activer/desactiver le checker
    CHECKER_TPS_THRESHOLD = 15.0       # workers sous ce bench_tps sont verifies
    CHECKER_TIMEOUT = 60               # timeout du check en secondes
    CHECKER_MAX_TOKENS = 300           # tokens max pour le verdict
    CHECKER_FAIL_MAX = 3               # echecs consecutifs avant retrogradation
    CHECKER_SCORE_DECAY = 0.1          # penalite par echec (sur score 0-1)
    CHECKER_SCORE_RECOVERY = 0.05      # bonus par succes (remontee lente)
    CHECKER_MIN_SCORE = 0.3            # score sous lequel le worker est retrograde
    CHECKER_SAMPLE_RATE = 100          # % des reponses verifiees (100 = toutes)

    def __init__(self, store=None):
        from .router import SmartRouter
        self.workers: dict[str, ConnectedWorker] = {}
        self.pending_jobs: dict[str, PendingJob] = {}
        self.api_tokens: dict[str, dict] = {}  # token -> {worker_id, ...}
        self._known_machines: set[str] = set()  # machine IDs deja vues (pour le bonus)
        self._start_time = time.time()
        self.router = SmartRouter()
        self._pool_hostname = socket.gethostname()
        self._worker_freed = asyncio.Event()  # signal quand un worker se libère
        self._queue_size = 0  # nombre de jobs en attente dans la queue
        self._rate_counters: dict[str, list[float]] = {}  # source -> [timestamps]
        # L3 — tampon de conversation (RAM par défaut, PostgreSQL en prod)
        from .db import MemoryStore
        self.store = store or MemoryStore()
        self._task_log: list[dict] = []  # historique des tâches distribuées
        self._active_tasks: dict[str, dict] = {}  # tâches en cours (task_id -> info)
        self._admin_chat_futures: dict[str, asyncio.Future] = {}  # chat_id -> future (admin chat RED)
        self._blacklist: set[str] = set()  # worker IDs bannis (charge depuis DB)
        self.tool_routing_model: str = ""  # empty = normal deficit routing for tool-calls  # modele pour tool-calls (configurable via dashboard)

    def _is_outdated(self, w: ConnectedWorker) -> bool:
        """Worker obsolete = version trop ancienne (tolere n-1 patch)."""
        wv = _parse_version(w.info.get("version", "0.0.0"))
        pv = _parse_version(__version__)
        # Meme major.minor : tolerer patch n-1 (ex: 0.2.9 OK si pool 0.2.10)
        if len(wv) >= 3 and len(pv) >= 3 and wv[:2] == pv[:2]:
            return wv[2] < pv[2] - 1
        return wv < pv

    def _is_unknown_model(self, w: ConnectedWorker) -> bool:
        """Worker avec un modele hors registre = ne recoit pas de trafic.
        Proxy/pool_managed : toujours accepte (modele gere manuellement)."""
        from .models import MODEL_REGISTRY
        # Workers proxy ou pool_managed : toujours acceptes
        if w.info.get("proxy_mode"):
            return False
        model_path = w.info.get("model_path", "")
        if not model_path:
            return True
        if any(m.hf_file in model_path for m in MODEL_REGISTRY):
            return False
        return True

    async def add_worker(self, worker_id: str, ws: WebSocket, info: dict) -> None:
        import hashlib

        # Rejeter les workers trop anciens
        MIN_VERSION = "0.2.40"
        worker_version = info.get("version", "0.0.0")
        if _parse_version(worker_version) < _parse_version(MIN_VERSION):
            log.warning(f"Worker {worker_id} rejete: v{worker_version} < v{MIN_VERSION} (mise a jour requise)")
            return
        # Blacklist — workers bannis (configurable via dashboard admin)
        if worker_id in self._blacklist:
            log.warning(f"Worker {worker_id} rejete: blackliste")
            return

        # Si le worker existait deja (reconnexion), fermer l'ancien WebSocket et nettoyer
        if worker_id in self.workers:
            old = self.workers[worker_id]
            log.warning(f"Worker {worker_id} reconnecte — fermeture ancienne connexion")
            self._cleanup_worker_jobs(worker_id)
            # Marquer l'ancien WS comme remplace (sera ferme par le handler)
            old.info["_replaced"] = True

        # Le pool impose le ctx optimal par modele (sauf proxies qui gerent leur propre ctx)
        if not info.get("proxy_mode"):
            from .models import MODEL_REGISTRY
            CTX_BY_MODEL = {"0.5B": 1024, "1.5B": 4096, "3B": 8192, "4B": 16384, "7B": 16384, "9B": 32768, "14B": 32768, "32B": 32768, "72B": 32768}
            model_path = info.get("model_path", "")
            for m in MODEL_REGISTRY:
                if m.hf_file in model_path or m.id.replace("-q4", "") in model_path:
                    info["ctx_size"] = CTX_BY_MODEL.get(m.params, 4096)
                    info["model_tier"] = m.id
                    log.info(f"Worker {worker_id}: model={m.name} -> ctx={info['ctx_size']} (pool override)")
                    break

        self.workers[worker_id] = ConnectedWorker(
            worker_id=worker_id, ws=ws, info=info
        )

        # Token API dérivé du worker_id + secret serveur (non devinable)
        api_token = _derive_api_token(worker_id)

        # Si ce token existe deja, on garde les credits accumules
        if api_token not in self.api_tokens:
            # Premiere connexion de ce worker — bonus de bienvenue
            # L'ID machine est base sur le worker_id (deterministe par machine)
            machine_id = worker_id  # ex: Pulse-8457
            is_new_local = machine_id not in self._known_machines
            self._known_machines.add(machine_id)

            # Anti-migration-farming: check federation peers.
            # If ANY peer has seen this worker_id before, skip the bonus.
            # Fail-open on timeout/error: grant bonus rather than block onboarding.
            is_new_fed = True
            if is_new_local:
                try:
                    is_new_fed = not await self._worker_known_in_federation(machine_id)
                except Exception as _fe:
                    log.debug(f"Federation anti-farm check failed (fail-open): {_fe}")

            is_new = is_new_local and is_new_fed
            bonus = self.WELCOME_BONUS if is_new else 0.0
            self.api_tokens[api_token] = {
                "worker_id": worker_id,
                "created": time.time(),
                "requests_used": 0,
                "credits": bonus,
            }
            if is_new:
                log.info(f"NEW MACHINE {worker_id} — bonus +{bonus} $IAMINE! (local+fed clean)")
            elif is_new_local and not is_new_fed:
                log.info(f"REJOIN {worker_id} — bonus skipped (known in federation)")

        self.workers[worker_id].info["api_token"] = api_token
        gpu_tag = f" — GPU: {info['gpu']} ({info['gpu_vram_gb']} GB)" if info.get("has_gpu") else ""
        bench_tag = f" — bench: {info.get('bench_tps', '?')} tok/s" if info.get("bench_tps") else ""
        log.info(f"Worker connecte: {worker_id} — {info.get('cpu', '?')} / {info.get('ram_total_gb', '?')} GB{gpu_tag}{bench_tag} — v{info.get('version', '?')}")

        # Sauvegarder le benchmark en PostgreSQL si disponible
        if hasattr(self.store, 'pool') and info.get("bench_tps"):
            asyncio.create_task(self._save_benchmark(worker_id, info))

        # Mettre a jour version + status en DB (pour la page Mes Workers)
        if hasattr(self.store, 'pool'):
            asyncio.create_task(self._update_worker_db(worker_id, info))
        self._print_status()

    def remove_worker(self, worker_id: str) -> None:
        self._cleanup_worker_jobs(worker_id)
        self.workers.pop(worker_id, None)
        log.info(f"Worker deconnecte: {worker_id}")
        self._print_status()

    def _cleanup_worker_jobs(self, worker_id: str) -> None:
        """Annule les jobs en cours assignes a un worker qui se deconnecte."""
        cancelled = 0
        for job_id, job in list(self.pending_jobs.items()):
            if not job.future.done():
                job.future.set_exception(
                    RuntimeError(f"Worker {worker_id} disconnected during job {job_id}")
                )
                cancelled += 1
        # Liberer le flag busy au cas ou
        w = self.workers.get(worker_id)
        if w:
            w.busy = False
            self._worker_freed.set()
        if cancelled:
            log.warning(f"Cancelled {cancelled} pending job(s) for worker {worker_id}")

    def get_stale_workers(self) -> list[str]:
        """Retourne les worker_ids qui n'ont pas repondu au heartbeat."""
        now = time.time()
        return [
            w.worker_id for w in self.workers.values()
            if (now - w.last_seen) > self.HEARTBEAT_TIMEOUT
        ]

    def get_assist_worker(self, exclude_worker_id: str):
        """Trouve le meilleur worker pour assister Scout — delegue a core/assist.py."""
        return _get_assist_worker(self, exclude_worker_id)

    @staticmethod
    def _parse_model_size_from_path(model_path: str) -> float:
        """Extrait la taille du modele — delegue a core/assist.py."""
        return _parse_model_size_from_path(model_path)

    def _tool_only_workers(self) -> set:
        """Retourne les worker_ids TOOL_ONLY — delegue a core/assist.py."""
        return _tool_only_workers()

    def get_idle_worker(self, exclude: str = "", prefer_stronger: bool = False) -> ConnectedWorker | None:
        """Trouve un worker idle pour les tâches de fond (compactage, etc).

        Le pool est le broker de confiance : il connaît tous les workers,
        leurs capacités et leur état. Il peut déléguer du travail de fond
        à un worker idle pendant que le worker principal sert l'utilisateur.

        prefer_stronger=True : pour le compactage, préférer un modèle plus gros
        que le worker source (meilleur résumé).
        """
        source = self.workers.get(exclude)
        source_size = 0
        if source and prefer_stronger:
            source_size = _parse_model_size(source.info.get("model_path", ""))

        candidates = [
            w for w in self.workers.values()
            if not w.busy and w.worker_id != exclude
            and not self._is_outdated(w) and not self._is_unknown_model(w)
        ]
        if not candidates:
            return None

        def _model_size(w):
            return _parse_model_size(w.info.get("model_path", ""))

        if prefer_stronger:
            # REGLE ABSOLUE: seul un LLM plus gros peut compacter
            stronger = [w for w in candidates if _model_size(w) > source_size]
            if stronger:
                stronger.sort(key=_model_size, reverse=True)
                return stronger[0]
            # Pas de modèle plus gros → ne PAS compacter
            return None
        else:
            candidates.sort(key=lambda w: w.info.get("bench_tps") or 10.0, reverse=True)

        return candidates[0]

    @property
    def pool_load(self) -> float:
        """Charge du pool en % (0-100)."""
        total = len(self.workers)
        if total == 0:
            return 100.0
        busy = sum(1 for w in self.workers.values() if w.busy)
        return round(busy / total * 100, 1)

    @property
    def compaction_budget(self) -> str:
        """Stratégie de compactage selon la charge du pool.

        < 50%  → immediate  (workers idle dispo, compacter maintenant)
        50-80% → deferred   (fire-and-forget, ne pas bloquer le job)
        > 80%  → suspended  (toute la puissance aux jobs, compacter plus tard)
        """
        load = self.pool_load
        if load < 0:  # toujours deferred — ne jamais bloquer la reponse
            return "immediate"
        elif load < 80:
            return "deferred"
        else:
            return "suspended"

    def _boost_eligible(self) -> bool:
        """Verifie si le boost est activable — delegue a core/assist.py."""
        return _boost_eligible(self)

    async def _boost_review(self, draft_text, messages, primary_worker, conv):
        """Boost review — delegue a core/assist.py."""
        from .core.assist import _boost_review
        return await _boost_review(self, draft_text, messages, primary_worker, conv)

    def check_rate_limit(self, source: str) -> bool:
        """Vérifie le rate limit — délégué à core/credits.py."""
        return _check_rate_limit(self, source)

    async def _update_worker_db(self, worker_id: str, info: dict):
        """Met a jour version et status en DB — délégué à core/credits.py."""
        await _update_worker_db_fn(self, worker_id, info)


    async def _worker_known_in_federation(self, worker_id: str) -> bool:
        """Query federated peers to check if this worker_id has been seen.

        Returns True if at least 1 peer confirms. Fail-open (returns False)
        on any error or timeout — we prefer false-grant of a bonus to
        false-rejection of a legitimate new worker.

        Cache 1h per worker_id to avoid hammering peers on reconnection.
        """
        import aiohttp
        import time as _t

        cache = getattr(self, "_fed_known_cache", None)
        if cache is None:
            cache = {}
            self._fed_known_cache = cache

        cached = cache.get(worker_id)
        if cached and (_t.time() - cached[1]) < 3600:
            return cached[0]

        if not hasattr(self.store, "pool"):
            return False

        try:
            from .core.federation import list_peers as _list_peers
            peers = await _list_peers(self)
        except Exception:
            return False

        if not peers:
            return False

        async def _check_peer(peer):
            url = (peer.get("url") or "").rstrip("/") + f"/v1/federation/worker/known/{worker_id}"
            try:
                timeout = aiohttp.ClientTimeout(total=1.5)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(url) as resp:
                        if resp.status != 200:
                            return False
                        data = await resp.json()
                        return bool(data.get("known"))
            except Exception:
                return False

        # Overall timeout 2s across all peers in parallel
        import asyncio as _aio
        try:
            results = await _aio.wait_for(
                _aio.gather(*[_check_peer(p) for p in peers], return_exceptions=True),
                timeout=2.0,
            )
        except _aio.TimeoutError:
            log.debug("anti-farm fed check: overall timeout, fail-open")
            return False

        known = any(r is True for r in results)
        cache[worker_id] = (known, _t.time())
        return known

    async def _save_benchmark(self, worker_id: str, info: dict):
        """Sauvegarde le benchmark — délégué à core/credits.py."""
        await _save_benchmark_fn(self, worker_id, info)

    async def _async_compact(self, helper, prompt, conv_id, conv, source_worker):
        """Compactage fire-and-forget — délégué à core/compaction.py."""
        await async_compact(self, helper, prompt, conv_id, conv, source_worker)

    def _is_memory_enabled(self, api_token: str) -> bool:
        """Memoire activee — délégué à core/credits.py."""
        return _is_memory_enabled_fn(self, api_token)

    async def _embed_facts(self, api_token: str, summary: str, conv_id: str):
        """Vectorise les faits RAG — délégué à core/credits.py."""
        await _embed_facts_fn(self, api_token, summary, conv_id)

    async def _save_conv_background(self, conv):
        """Sauvegarde conversation — délégué à core/credits.py."""
        await _save_conv_background_fn(self, conv)

    def _is_local_worker(self, w: ConnectedWorker) -> bool:
        """Vérifie si le worker tourne sur la même machine que le pool."""
        return w.info.get("hostname", "") == self._pool_hostname

    def _job_timeout(self, w: ConnectedWorker) -> int:
        """Timeout adaptatif par taille de modèle + charge pool (en secondes)."""
        mp = w.info.get("model_path", "")
        base = 30  # 0.5B, 1.5B ou inconnu
        for part in mp.replace("-", ".").split("."):
            if part.lower().endswith("b") and part[:-1].replace(".", "").isdigit():
                try:
                    size = float(part[:-1])
                    if size >= 14:
                        base = 600
                    elif size >= 7:
                        base = 600
                    elif size >= 3:
                        base = 60
                except ValueError:
                    pass
        # Sous charge : minimum 90s pour laisser le temps au drain
        if self.pool_load >= 80:
            base = max(base, 90)
        return base

    def _has_external_3b_worker(self, exclude_id: str = "") -> bool:
        """Vérifie s'il y a au moins un worker externe 3B+ disponible."""
        for w in self.workers.values():
            if w.worker_id == exclude_id or w.busy:
                continue
            if self._is_local_worker(w):
                continue
            if self._is_outdated(w) or self._is_unknown_model(w):
                continue
            # Vérifier la taille du modèle >= 3B
            mp = w.info.get("model_path", "")
            for part in mp.replace("-", ".").split("."):
                if part.endswith("b") and part[:-1].replace(".", "").isdigit():
                    try:
                        if float(part[:-1]) >= 3:
                            return True
                    except ValueError:
                        pass
        return False

    def get_available_worker(self, conv_id: str | None = None, requested_model: str | None = None, preferred_tier: str | None = None, preferred_confidence: float | None = None) -> ConnectedWorker | None:
        """Smart routing — selectionne le meilleur worker selon le contexte.

        Le worker local (VPS) est exclu si un autre worker 3B+ est disponible,
        afin qu'il se concentre sur l'orchestration et la base de données.

        preferred_tier/confidence (Phase 2) : hint issu de classify_prompt sur
        le dernier message user. Non-bloquant — bonus de fit scale par confidence.
        """
        # Déterminer si le worker local doit être exclu
        exclude_local = self._has_external_3b_worker()

        if conv_id:
            conv = self.router.get_or_create_conversation(conv_id)
            from .models import MODEL_REGISTRY
            _approved = {m.hf_file for m in MODEL_REGISTRY}
            best_id = self.router.select_worker(conv, self.workers, requested_model, exclude_local_hostname=self._pool_hostname if exclude_local else None, pool_version=__version__, approved_files=_approved, preferred_tier=preferred_tier, preferred_confidence=preferred_confidence)
            if best_id:
                return self.workers.get(best_id)

        # Fallback : n'importe quel worker idle (meme CPU lent, mieux qu'un 503)
        for w in self.workers.values():
            if not w.busy:
                if exclude_local and self._is_local_worker(w):
                    continue
                if self._is_outdated(w) or self._is_unknown_model(w):
                    continue
                return w

        # Dernier recours : ignorer l'exclusion locale (VPS worker)
        for w in self.workers.values():
            if not w.busy and not self._is_outdated(w) and not self._is_unknown_model(w):
                return w
        return None

    async def submit_job(
        self, messages: list[dict], max_tokens: int = 512,
        conv_id: str | None = None, requested_model: str | None = None,
        api_token: str = "", tools: list | None = None,
    ) -> dict:
        """Soumet un job avec smart routing."""
        # Conversation tracking
        conv = self.router.get_or_create_conversation(conv_id, api_token)

        # === SMART ROUTING Phase 5 — detection re-prompt rapide ===
        # Si l'utilisateur relance une requete <30s apres la reponse precedente,
        # c'est un signal fort que le routing etait mal adapte (LLM trop petit,
        # reponse insatisfaisante, erreur de tier). On flag le job precedent
        # via routing_feedback — ces jobs seront exclus du vote KNN futur.
        if not tools and conv.last_job_id and conv.last_response_ts > 0:
            elapsed = time.time() - conv.last_response_ts
            if 0 < elapsed < 30.0:
                asyncio.create_task(self.store.log_routing_feedback(
                    conv.last_job_id, "reprompt_fast",
                    {"elapsed_sec": round(elapsed, 1), "conv_id": conv.conv_id},
                ))
                log.info(f"Phase 5 flag: job {conv.last_job_id} reprompt_fast in {elapsed:.1f}s (conv={conv.conv_id})")

        # === SMART ROUTING Phase 2+3+4 — classification du prompt ===
        # Phase 2 : heuristique lexicale (rapide, locale, ~1ms).
        # Phase 3 : KNN pgvector sur jobs.prompt_embedding (~10-50ms).
        # Phase 4 : si encore ambigu (< 0.7), LLM idle classifier (~1-3s).
        # Priorite de la confidence la plus haute. Doctrine "pool travaille".
        # Skip pour les tool-calls (le client gere son propre contexte/prompt).
        classified_tier = ""
        classified_conf: float | None = None
        classified_method = ""
        last_user_for_classify = ""
        prompt_embedding_vec: list[float] | None = None
        if not tools:
            # --- Phase 2 : heuristique ---
            try:
                from .core.routing_heuristic import classify_prompt
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_for_classify = m.get("content", "") or ""
                        break
                if last_user_for_classify:
                    classified_tier, classified_conf = classify_prompt(last_user_for_classify)
                    classified_method = "heuristic"
            except Exception as e:
                log.warning(f"classify_prompt failed (non-blocking): {e}")

            # --- Phase 3 : embedding + KNN pgvector vote ---
            if last_user_for_classify:
                try:
                    from .core.routing_embeddings import embed_prompt
                    loop_embed = asyncio.get_event_loop()
                    prompt_embedding_vec = await loop_embed.run_in_executor(
                        None, embed_prompt, last_user_for_classify,
                    )
                    if prompt_embedding_vec:
                        knn_res = await self.store.knn_tier_vote(prompt_embedding_vec, k=10)
                        if knn_res:
                            knn_tier, knn_conf, n_found = knn_res
                            # KNN requiert >=5 voisins pour etre fiable (cold start)
                            if n_found >= 5 and knn_conf > (classified_conf or 0):
                                log.info(f"KNN override: {classified_tier}({classified_conf:.2f}) -> {knn_tier}({knn_conf:.2f}) n={n_found}")
                                classified_tier = knn_tier
                                classified_conf = knn_conf
                                classified_method = "knn"
                except Exception as e:
                    log.warning(f"KNN classify failed (non-blocking): {e}")

            # --- Phase 4 : fallback LLM idle classifier si toujours ambigu ---
            if last_user_for_classify and classified_conf is not None and classified_conf < 0.7:
                try:
                    from .core.routing_llm_classify import classify_via_idle_worker
                    llm_result = await classify_via_idle_worker(self, last_user_for_classify)
                    if llm_result:
                        llm_tier, llm_conf, clf_wid = llm_result
                        if llm_conf > (classified_conf or 0):
                            log.info(f"LLM classify override: {classified_tier}({classified_conf:.2f}) -> {llm_tier}({llm_conf:.2f}) via {clf_wid}")
                            classified_tier = llm_tier
                            classified_conf = llm_conf
                            classified_method = "llm_idle"
                except Exception as e:
                    log.warning(f"classify_via_idle failed (non-blocking): {e}")

        # --- M13: Trigger consolidation if pending observations ---
        if AGENT_MEMORY_ENABLED and api_token:
            asyncio.create_task(full_consolidation(
                self, self.store, api_token, conv.conv_id))

        # L3 : charger le contexte depuis PostgreSQL si la conversation est vide en RAM
        if conv_id and conv.api_token and len(conv.messages) <= 1 and not conv._summary and not conv._l3_summary and self._is_memory_enabled(api_token) and not tools:
            try:
                stored = await self.store.load_conversation(conv.conv_id, conv.api_token)
                if stored and (stored.get("messages") or stored.get("summary")):
                    # Restaurer le summary (resume des compactions precedentes)
                    if stored.get("summary"):
                        conv._summary = stored["summary"]
                        log.info(f"L3 summary restored for {conv.conv_id}: {len(conv._summary)} chars")
                    # Restaurer les derniers messages (contexte recent)
                    if stored.get("messages"):
                        # Ne restaurer que les 6 derniers messages (pas tout l'historique)
                        recent = stored["messages"][-6:]
                        for msg in recent:
                            if msg.get("role") and msg.get("content"):
                                conv.messages.insert(-1, msg)  # avant le dernier message (l'actuel)
                        conv.total_tokens = stored.get("total_tokens", 0)
                        log.info(f"L3 messages restored for {conv.conv_id}: {len(recent)} msgs, {conv.total_tokens} tokens")
            except Exception as e:
                log.warning(f"L3 restore failed for {conv.conv_id}: {e}")

        # === RAG : memoire long-terme vectorisee ===
        rag_context = ""
        if api_token and api_token.startswith("acc_") and self._is_memory_enabled(api_token) and not tools:
            try:
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break
                if last_user_msg:
                    # --- M13 P2: hybrid retrieval replaces basic RAG ---
                    if AGENT_MEMORY_ENABLED:
                        from .core.agent_memory_phase2 import hybrid_retrieve as _hybrid_retrieve
                        rag_context = await _hybrid_retrieve(
                            self.store, api_token, last_user_msg, limit=5)
                    else:
                        from .memory import retrieve_context
                        rag_context = await retrieve_context(
                            self.store, api_token, last_user_msg, limit=5,
                            conv_id=conv.conv_id)
                    if rag_context:
                        log.info(f"RAG: {len(rag_context)} chars for conv={conv.conv_id}")
            except Exception as e:
                log.warning(f"RAG retrieval failed (non-blocking): {e}")
                rag_context = ""  # ne jamais bloquer un job a cause du RAG

                # --- M13: Episodic memory ---
                if AGENT_MEMORY_ENABLED:
                    try:
                        episode_ctx = await get_episode_context(
                            self.store, api_token, last_user_msg[:200])
                        if episode_ctx:
                            rag_context = (rag_context + "\n" + episode_ctx) if rag_context else episode_ctx
                    except Exception:
                        pass

        # Injecter le system prompt si absent
        # Skip pour les requetes tool-call : le client (OpenCode/Cursor/aider)
        # gere son propre prompt systeme et notre branding pollue les coding agents
        has_system = any(m.get("role") == "system" for m in messages)
        if not has_system and not tools:
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT}] + messages

        # Ne pas accumuler le contexte pour les requetes tool-call
        # (le client gere son propre historique)
        if not tools:
            for msg in messages:
                if msg.get("content") and msg.get("role") in ("user", "system"):
                    conv.add_message(msg["role"], msg["content"])

        worker = self.get_available_worker(conv.conv_id, requested_model, preferred_tier=classified_tier, preferred_confidence=classified_conf)

        # QoS : si tous les workers sont busy, attendre qu'un se libère
        if worker is None:
            if self._queue_size >= self.QUEUE_MAX_SIZE:
                raise RuntimeError("Pool saturated — too many queued requests")
            self._queue_size += 1
            log.info(f"Queue: waiting for worker (queue={self._queue_size}, conv={conv.conv_id})")
            try:
                deadline = time.time() + self.QUEUE_TIMEOUT
                waited = 0
                while worker is None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    self._worker_freed.clear()
                    try:
                        await asyncio.wait_for(self._worker_freed.wait(), timeout=min(remaining, 2.0))
                    except asyncio.TimeoutError:
                        pass
                    waited += 2
                    # D'abord essayer le smart routing
                    worker = self.get_available_worker(conv.conv_id, requested_model, preferred_tier=classified_tier, preferred_confidence=classified_conf)
                    # Apres 3s, tenter le forwarding cross-pool
                    if worker is None and waited >= 3:
                        # --- M7a: forward from queue when all local workers busy ---
                        try:
                            from .core.forwarding import should_forward, forward_job, is_forwarding_enabled, get_forwarding_mode
                            if is_forwarding_enabled() and get_forwarding_mode() == "active":
                                peer = await should_forward(self, requested_model, self._queue_size)
                                if peer:
                                    log.info(f"Queue->Forward: {peer['name']!r} after {waited}s wait (queue={self._queue_size})")
                                    try:
                                        fwd = await forward_job(self, peer, requested_model, messages, max_tokens, conv_id=conv.conv_id, api_token=api_token)
                                        if fwd and fwd.get("response"):
                                            # Build result as if local worker handled it
                                            result = {
                                                "text": fwd["response"],
                                                "model": requested_model or "forwarded",
                                                "usage": {"total_tokens": fwd.get("tokens_out", 0)},
                                                "job_id": job_id if 'job_id' in dir() else "fwd",
                                                "forwarded_to": peer.get("name"),
                                                "exec_pool_id": fwd.get("exec_pool_id"),
                                                "worker_id": f"fwd:{peer['name']}",
                                                "conv_id": conv.conv_id,
                                            }
                                            self._queue_size -= 1
                                            return result
                                    except Exception as e:
                                        log.warning(f"Queue->Forward failed: {e}, continuing local wait")
                        except Exception:
                            pass

                    # Apres 5s, accepter N'IMPORTE quel worker idle (meme CPU lent)
                    if worker is None and waited >= 5:
                        for w in self.workers.values():
                            if not w.busy and not self._is_outdated(w) and not self._is_unknown_model(w):
                                worker = w
                                log.info(f"Queue fallback: {w.worker_id} (any idle after {waited}s)")
                                break
            finally:
                self._queue_size -= 1

            if worker is None:
                raise RuntimeError(f"No worker available after {self.QUEUE_TIMEOUT}s queue")

        worker_ctx = worker.info.get("ctx_size", 2048)

        # === COMPACTAGE DISTRIBUE ADAPTATIF (delegue a core/compaction.py) ===
        budget = self.compaction_budget
        await handle_compaction(self, conv, worker, conv_id, budget, tools)

        # === META-COMPACTION DISTRIBUEE (delegue a core/compaction.py) ===
        await handle_meta_compaction(self, conv, worker, conv_id, budget, tools)

        job_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        self.pending_jobs[job_id] = PendingJob(
            job_id=job_id, messages=messages, max_tokens=max_tokens, future=future
        )

        worker.busy = True
        worker.info["_last_job_time"] = time.time()

        # Envoyer le contexte adapte au worker
        # Pour les tool-calls, utiliser les messages originaux du client (pas le contexte pool)
        if tools:
            worker_messages = messages
        else:
            worker_messages = conv.get_context_for_worker(worker_ctx)
        # Injecter la memoire RAG dans le system prompt
        if rag_context and worker_messages:
            if worker_messages[0].get("role") == "system":
                worker_messages[0]["content"] = rag_context + "\n\n" + worker_messages[0]["content"]
            else:
                worker_messages.insert(0, {"role": "system", "content": rag_context})
        # === Option B: adaptive /no_think injection (speed up simple chat) ===
        # 4B Qwen3.5 ignores /no_think when placed in the system message (chat
        # template behavior). Prepend it to the LAST user message instead —
        # works across all Qwen3 model sizes. Diagnosed 2026-04-18 during load
        # audit : 4B kept generating "Analyze the Request:..." despite the flag
        # being in system. Tested live : /no_think in user -> 9 tokens reply in
        # 4.2s instead of 500 tokens of thinking in 28s.
        if should_disable_thinking(worker.info.get("model_path", ""), worker_messages, bool(tools)):
            last_user_idx = -1
            for i in range(len(worker_messages) - 1, -1, -1):
                if worker_messages[i].get("role") == "user":
                    last_user_idx = i
                    break
            if last_user_idx >= 0:
                current = worker_messages[last_user_idx].get("content", "") or ""
                if "/no_think" not in current:
                    worker_messages[last_user_idx]["content"] = "/no_think " + current
                log.debug(f"Option B: /no_think prepended to user msg for {worker.worker_id}")

        job_payload = {
            "type": "job",
            "job_id": job_id,
            "messages": worker_messages,
            "max_tokens": max_tokens,
        }
        if tools:
            tools = inject_think_tool(tools)
            job_payload["tools"] = tools
        await worker.ws.send_json(job_payload)

        # Timeout adaptatif par taille de modèle
        job_timeout = self._job_timeout(worker)

        log.info(
            f"Job {job_id} → {worker.worker_id} "
            f"(conv={conv.conv_id}, {conv.total_tokens} tok, ctx={worker.info.get('ctx_size', '?')}, timeout={job_timeout}s)"
        )

        try:
            result = await asyncio.wait_for(future, timeout=job_timeout)
        except asyncio.TimeoutError:
            self.pending_jobs.pop(job_id, None)
            worker.busy = False
            self._worker_freed.set()  # signaler la queue
            raise RuntimeError(f"Job {job_id} timeout après {job_timeout}s")

        worker.busy = False
        self._worker_freed.set()  # signaler la queue
        worker.jobs_done += 1
        self.pending_jobs.pop(job_id, None)

        # === BOOST MODE (core/assist.py) ===
        result = await handle_boost(self, result, messages, worker, conv, tools)

        # Nettoyer le thinking mode des reponses (Qwen 3.5 thinking actif par defaut)
        raw_text = result.get("text", "")
        result["text"] = _strip_thinking(raw_text)

        # === MEMORIZE TAG : intercepter [MEMORIZE: ...] dans la reponse ===
        import re as _re
        memorize_match = _re.search(r"\[MEMORIZE:\s*(.+?)\]", result["text"])
        if memorize_match and api_token and api_token.startswith("acc_") and self._is_memory_enabled(api_token) and not tools:
            fact = memorize_match.group(1).strip()
            asyncio.create_task(self._embed_facts(api_token, fact, conv_id))
            result["text"] = result["text"].replace(memorize_match.group(0), "").strip()
            log.info(f"MEMORIZE: stored fact for {api_token[:12]}... — {fact[:80]}")

        # === THINK TOOL (core/assist.py) ===
        result = await handle_think(self, result, messages, worker, tools, conv_id, max_tokens)

        # === POOL ASSIST (core/assist.py) ===
        result = await handle_pool_assist(self, result, messages, worker, tools, conv_id, max_tokens)
        # === AUTO REVIEW (Phase 1 sub-agents) ===
        result = await handle_auto_review(self, result, messages, worker, conv_id, max_tokens)
        # === SUB-AGENT PIPELINE (Phase 3) ===
        result = await handle_sub_agent_pipeline(self, result, messages, worker, conv_id, max_tokens)

        # === CHECKER LADDER (core/checker.py) ===
        from .core.checker import handle_checker
        result = await handle_checker(self, result, messages, worker, conv)


        # Ajouter la reponse au contexte de la conversation
        if conv_id:
            response_text = result.get("text", "")
            conv.add_message("assistant", response_text)

            # Le compactage est fait AVANT l'envoi (proactif)

        # Crediter le worker : 100 tokens generes = 1 $IAMINE (core/credits.py)
        credit_worker_for_job(self, worker, result)

        # Mettre a jour la perf reelle en RAM + DB (moyenne glissante)
        job_tps = result.get("tokens_per_sec", 0)
        tokens_gen = result.get("tokens_generated", 0)
        if job_tps > 0 and tokens_gen > 0:
            old_tps = worker.info.get("real_tps", 0)
            worker.info["real_tps"] = round(old_tps * 0.8 + job_tps * 0.2, 2) if old_tps > 0 else round(job_tps, 2)
            worker.info["total_jobs"] = (worker.info.get("total_jobs") or 0) + 1
            try:
                await self.store.update_worker_real_tps(worker.worker_id, job_tps, tokens_gen)
            except Exception:
                pass
            # Enrichir hardware_benchmarks avec le real_tps
            cpu = worker.info.get("cpu", "")
            model_path = worker.info.get("model_path", "")
            if cpu and model_path:
                from .models import MODEL_REGISTRY
                for m in MODEL_REGISTRY:
                    if m.hf_file in model_path:
                        gpu = worker.info.get("gpu", "") if worker.info.get("has_gpu") else ""
                        try:
                            await self.store.upsert_hardware_benchmark(
                                cpu, gpu, worker.info.get("ram_total_gb", 0), m.id, job_tps)
                        except Exception:
                            pass
                        break

        # Smart routing Phase 5 : tracker le dernier job pour detecter re-prompts
        if conv_id:
            conv.last_job_id = job_id
            conv.last_response_ts = time.time()

        # === SAUVEGARDE CONVERSATION PERSISTANTE (acc_* uniquement, memory_enabled) ===
        if conv_id and conv.api_token and conv.api_token.startswith("acc_") and self._is_memory_enabled(conv.api_token):
            asyncio.create_task(self._save_conv_background(conv))

        # === SMART ROUTING — Phase 1 : instrumentation passive ===
        # Log chaque job :
        # - Phase 2 actif (tools=False) : routed_tier = tier classifie par l'heuristique,
        #   route_method=heuristic, confidence enregistree.
        # - Fallback (tools=True ou classification vide) : tier derive du worker servi,
        #   route_method=passive (comme Phase 1).
        # Voir project_todo_smart_routing.md
        try:
            from .db import JobRecord
            if classified_tier:
                logged_tier = classified_tier
                logged_method = classified_method or "heuristic"
                logged_conf = classified_conf
            else:
                model_path_log = (worker.info.get("model_path") or "").lower()
                if "coder" in model_path_log:
                    logged_tier = "code"
                elif "35b" in model_path_log or "27b" in model_path_log or "72b" in model_path_log:
                    logged_tier = "large"
                elif "9b" in model_path_log:
                    logged_tier = "medium"
                elif "2b" in model_path_log or "4b" in model_path_log:
                    logged_tier = "small"
                else:
                    logged_tier = ""
                logged_method = "passive"
                logged_conf = None
            asyncio.create_task(self.store.log_job(JobRecord(
                job_id=job_id,
                worker_id=worker.worker_id,
                tokens_generated=tokens_gen,
                tokens_per_sec=float(job_tps or 0),
                duration_sec=float(result.get("duration_sec", 0) or 0),
                model=result.get("model", "") or worker.info.get("model_path", ""),
                credits_earned=float(result.get("credits_earned", 0) or 0),
                routed_tier=logged_tier,
                route_confidence=logged_conf,
                route_method=logged_method,
                prompt_embedding=prompt_embedding_vec,
            )))
        except Exception as e:
            log.warning(f"log_job (routing instrumentation) failed: {e}")

        result["conv_id"] = conv.conv_id
        result["worker_id"] = worker.worker_id

        # --- M13: Capture inference observation ---
        if AGENT_MEMORY_ENABLED:
            _obs_text = result.get("text", "")[:500]
            if _obs_text and len(_obs_text) > 20:
                asyncio.create_task(capture_observation(
                    self.store, api_token, "inference", _obs_text,
                    conv_id=conv.conv_id, job_id=result.get("job_id", ""),
                    source_id=worker.worker_id,
                    metadata={"tokens": result.get("usage", {}).get("total_tokens", 0),
                              "model": result.get("model", "")}))
        result["prompt_tokens"] = conv.total_tokens
        result["compacted"] = conv._compacted if hasattr(conv, '_compacted') else False
        result["conv_messages_total"] = len(conv.messages)
        return result

    async def delegate_task(
        self,
        helper: ConnectedWorker,
        task_type: str,
        prompt: str = "",
        conv_id: str = "",
        source_worker: str = "",
        messages: list = None,
        tools: list = None,
        max_tokens: int = 400,
        depth: int = 0,
    ) -> str | None:
        """Délègue une tâche de fond à un worker helper (compactage distribué).

        Protocole worker-to-worker via le pool :
        1. Pool envoie un message 'delegate' au helper avec le contexte de la tâche
        2. Helper traite et renvoie un 'result'
        3. Pool applique le résultat et crédite le helper
        4. La tâche est tracée en BDD (worker_tasks)

        Le pool est le broker de confiance — les workers se font confiance
        parce qu'ils sont tous authentifiés auprès du pool.
        """
        if depth > 3:
            log.warning(f"delegate_task depth limit reached ({depth}) — aborting")
            return None
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        t0 = time.time()

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pending_jobs[task_id] = PendingJob(
            job_id=task_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            future=future,
        )

        # Protocole delegate : le helper sait qu'il aide un autre worker
        delegate_msg = {
            "type": "delegate",
            "job_id": task_id,
            "task_type": task_type,
            "source_worker": source_worker,
            "conv_id": conv_id,
            "messages": messages if messages else [{"role": "user", "content": prompt[:3000]}],
            "max_tokens": max_tokens,
        }
        if tools:
            delegate_msg["tools"] = tools
        await helper.ws.send_json(delegate_msg)

        # Marquer la tâche comme active (visible en temps réel sur le canvas)
        self._active_tasks[task_id] = {
            "task_id": task_id, "task_type": task_type,
            "source": source_worker, "helper": helper.worker_id,
            "conv_id": conv_id, "status": "running",
            "started": time.time(),
        }

        log.info(
            f"DELEGATE {task_type} [{task_id}] "
            f"{source_worker} → {helper.worker_id} "
            f"(conv={conv_id})"
        )

        try:
            result = await asyncio.wait_for(future, timeout=90)
            self.pending_jobs.pop(task_id, None)
            duration = time.time() - t0
            text = result.get("text", "")

            # Créditer le helper pour son travail de fond
            tokens_gen = result.get("tokens_generated", 0)
            credit = tokens_gen / 100.0
            api_token = helper.info.get("api_token")
            if api_token and api_token in self.api_tokens and credit > 0:
                self.api_tokens[api_token]["credits"] += credit
                self.api_tokens[api_token]["total_earned"] = self.api_tokens[api_token].get("total_earned", 0) + credit

            # Tracer la tâche en BDD
            try:
                await self.store.log_task(
                    task_id=task_id, task_type=task_type, conv_id=conv_id,
                    assigned_worker=helper.worker_id, source_worker=source_worker,
                    status="done", duration_sec=round(duration, 2),
                )
            except Exception:
                pass  # store peut ne pas supporter log_task

            log.info(
                f"DELEGATE DONE [{task_id}] "
                f"{helper.worker_id} \u2192 {source_worker} "
                f"({task_type}, {duration:.1f}s, {tokens_gen} tok)"
            )

            self._active_tasks.pop(task_id, None)
            self._task_log.append({
                "task_id": task_id, "task_type": task_type,
                "source": source_worker, "helper": helper.worker_id,
                "conv_id": conv_id, "status": "done",
                "duration": round(duration, 2), "tokens": tokens_gen,
                "timestamp": time.time(),
            })
            if len(self._task_log) > 200:
                self._task_log = self._task_log[-100:]

            # Pour pool_assist, retourner le resultat complet (avec tool_calls)
            if task_type in ("pool_assist", "think"):
                return result
            return text
        except asyncio.TimeoutError:
            self.pending_jobs.pop(task_id, None)
            try:
                await self.store.log_task(
                    task_id=task_id, task_type=task_type, conv_id=conv_id,
                    assigned_worker=helper.worker_id, source_worker=source_worker,
                    status="timeout", duration_sec=90.0,
                )
            except Exception:
                pass
            self._active_tasks.pop(task_id, None)
            log.warning(f"DELEGATE TIMEOUT [{task_id}] {helper.worker_id}")
            self._task_log.append({
                "task_id": task_id, "task_type": task_type,
                "source": source_worker, "helper": helper.worker_id,
                "conv_id": conv_id, "status": "timeout",
                "duration": 90.0, "tokens": 0,
                "timestamp": time.time(),
            })
            return None

    def handle_result(self, msg: dict) -> None:
        """Traite un résultat reçu d'un worker."""
        job_id = msg.get("job_id")
        job = self.pending_jobs.get(job_id)
        if job and not job.future.done():
            job.future.set_result(msg)

    def handle_error(self, msg: dict) -> None:
        """Traite une erreur reçue d'un worker."""
        job_id = msg.get("job_id")
        worker_id = msg.get("worker_id", "")
        job = self.pending_jobs.get(job_id)
        if job and not job.future.done():
            job.future.set_exception(RuntimeError(msg.get("error", "Unknown error")))
        # Tracker les echecs en RAM + DB
        if worker_id:
            w = self.workers.get(worker_id)
            if w:
                w.info["jobs_failed"] = (w.info.get("jobs_failed") or 0) + 1
            asyncio.create_task(self._track_failure(worker_id))

    async def _track_failure(self, worker_id: str) -> None:
        try:
            await self.store.increment_worker_failures(worker_id)
        except Exception:
            pass

    async def heartbeat_loop(self) -> None:
        """Boucle de heartbeat — delegue a core/heartbeat.py."""
        from .core.heartbeat import heartbeat_loop as _heartbeat_loop
        await _heartbeat_loop(self)

    def status(self) -> dict:
        """Snapshot JSON du pool — delegue a core/pool_status.py."""
        from .core.pool_status import build_pool_status
        return build_pool_status(self)

    async def _credit_sync_loop(self):
        """Sync credits RAM -> DB — delegue a core/credits.py."""
        await credit_sync_loop(self)

    def _print_status(self) -> None:
        n = len(self.workers)
        busy = sum(1 for w in self.workers.values() if w.busy)
        local_status = "exclu (externe 3B+ dispo)" if self._has_external_3b_worker() else "actif (seul 3B+)"
        print(f" * POOL STATUS  {n} workers ({busy} busy) — worker local: {local_status}")


# --- Instance globale ---
pool = Pool()


# --- Drain pending jobs loop (tampon DB anti-saturation) ---

# _fire_webhook — moved to core/heartbeat.py
from .core.heartbeat import fire_webhook as _fire_webhook


# _drain_pending_jobs_loop — moved to core/heartbeat.py
async def _drain_pending_jobs_loop():
    """Drain pending jobs — delegue a core/heartbeat.py."""
    from .core.heartbeat import drain_pending_jobs_loop
    await drain_pending_jobs_loop(pool)
# --- Heartbeat au demarrage ---
@app.on_event("startup")
async def start_heartbeat():
    from .core.startup import initialize_pool
    await initialize_pool(pool)


# --- CORS (pour accès depuis iamine.org ou autre domaine) ---
from fastapi.middleware.cors import CORSMiddleware

ALLOWED_ORIGINS = os.environ.get("IAMINE_CORS_ORIGINS", "https://cellule.ai,https://iamine.org,http://127.0.0.1:8081").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# --- Enregistrer les routes depuis les modules ---
from .routes import register_routes
register_routes(app)


# --- Enterprise plugins (opt-in via CELLULE_ENTERPRISE=1) ---
# Public build: no-op. Enterprise build: loads iamine_enterprise.plugins
# which can monkey-patch pool internals and add FastAPI routes. See
# iamine/plugins/__init__.py for the gated loader logic.
@app.on_event("startup")
async def _load_enterprise_plugins_on_startup():
    from .plugins import load_enterprise_plugins
    await load_enterprise_plugins(pool, app)


# --- WebSocket endpoint pour les workers ---
# _auto_bench moved to core/assignment.py -- re-exported below


# --- API REST compatible OpenAI ---
# _check_admin moved to core/accounts.py (imported below with other account symbols)


# --- Pipeline (utilitaire interne, utilise par routes) ---
# Moved to core/startup.py
from .core.startup import get_pipeline as _get_pipeline_impl

def _get_pipeline():
    return _get_pipeline_impl(pool)


# --- Dev Chat API ---
CHAT_FILE = Path(__file__).parent.parent / "reports" / "chat" / "conversation.md"
REPORTS_DIR = Path(__file__).parent.parent / "reports"



# --- Auth Web2 + Mes Workers ---
# Moved to core/accounts.py -- re-export for backward compatibility
from .core.accounts import (  # noqa: E402
    _accounts, _sessions, _SESSION_TTL, _ACCOUNTS_FILE,
    _create_session, _get_session_account,
    _load_accounts, _load_accounts_from_db,
    _sync_account_tokens,
    _save_accounts, _save_account_to_db,
    _seed_user_memory, _check_admin,
    GOOGLE_CLIENT_ID,
)

# Assignment / self-heal -- moved to core/assignment.py
from .core.assignment import (  # noqa: E402
    _auto_bench, _self_heal_downgrade, _check_model_assignment,
)

# === SELF-HEALING — moved to core/assignment.py ===


# === ATTRIBUTION AUTO DES MODELES — moved to core/assignment.py ===


# === CANAL DE COMMANDES ADMIN → WORKERS ===

# === API RED : commandes admin pour l'agent LLM ===

# === RED MEMORY : sauvegarde et rollback de RED.md ===

# print_banner — moved to core/startup.py
from .core.startup import print_banner  # noqa: E402,F811
