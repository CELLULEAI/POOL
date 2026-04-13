"""Moteur d'inférence — wrapper autour de llama-cpp-python."""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Semaphore

from llama_cpp import Llama

from .config import ModelConfig, LimitsConfig

log = logging.getLogger("iamine.engine")


@dataclass
class InferenceResult:
    text: str
    tokens_generated: int
    tokens_per_sec: float
    duration_sec: float
    model: str
    tool_calls: list | None = None


class InferenceEngine:
    """Charge un modèle GGUF et gère l'inférence."""

    def __init__(self, model_cfg: ModelConfig, limits_cfg: LimitsConfig):
        self.model_cfg = model_cfg
        self.limits_cfg = limits_cfg
        self.model: Llama | None = None
        self._semaphore = Semaphore(limits_cfg.max_concurrent)
        self._model_name = Path(model_cfg.path).stem

    def load(self) -> None:
        """Charge le modèle en mémoire."""
        path = self.model_cfg.path
        if not Path(path).exists():
            raise FileNotFoundError(f"Modèle introuvable : {path}")

        log.info(f"Chargement du modèle {path}...")
        t0 = time.perf_counter()

        self.model = Llama(
            model_path=path,
            n_ctx=self.model_cfg.ctx_size,
            n_threads=self.model_cfg.threads,
            n_gpu_layers=self.model_cfg.gpu_layers,
            verbose=False,
        )

        dt = time.perf_counter() - t0
        log.info(f"Modèle prêt en {dt:.1f}s — ctx={self.model_cfg.ctx_size} threads={self.model_cfg.threads}")

        # Bench rapide au chargement (50 tokens pour amortir le prompt processing)
        self.bench_tps = self._quick_bench()

    def _quick_bench(self, tokens: int = 50) -> float:
        """Bench rapide après chargement — retourne les tokens/sec."""
        try:
            t0 = time.perf_counter()
            bench_kwargs = dict(messages=[{"role": "user", "content": "Hi"}],
                                max_tokens=tokens, temperature=0.0)
            try:
                r = self.model.create_chat_completion(
                    **bench_kwargs, chat_template_kwargs={"enable_thinking": False})
            except TypeError:
                r = self.model.create_chat_completion(**bench_kwargs)
            dt = time.perf_counter() - t0
            n = r.get("usage", {}).get("completion_tokens", tokens)
            tps = n / dt if dt > 0 else 0
            log.info(f"Bench: {tps:.1f} tok/s ({n} tokens in {dt:.2f}s)")
            return round(tps, 1)
        except Exception as e:
            log.warning(f"Bench failed: {e}")
            return 0.0

    def generate(self, messages: list[dict], max_tokens: int | None = None, tools: list | None = None) -> InferenceResult:
        """Génère une réponse à partir d'une liste de messages (format OpenAI)."""
        if self.model is None:
            raise RuntimeError("Modèle non chargé — appeler load() d'abord")

        max_tok = min(max_tokens or self.limits_cfg.max_tokens, self.limits_cfg.max_tokens)

        # Sémaphore pour limiter les inférences concurrentes
        if not self._semaphore.acquire(timeout=30):
            raise TimeoutError("Trop de requêtes en cours")

        try:
            t0 = time.perf_counter()

            kwargs = dict(messages=messages, max_tokens=max_tok, temperature=0.7)
            if tools:
                # Convert OpenAI tools format to llama-cpp format
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            # Disable thinking mode si llama-cpp-python le supporte (< 0.3.20)
            try:
                response = self.model.create_chat_completion(
                    **kwargs, chat_template_kwargs={"enable_thinking": False})
            except TypeError:
                response = self.model.create_chat_completion(**kwargs)

            dt = time.perf_counter() - t0
            msg = response["choices"][0]["message"]
            text = msg.get("content") or ""
            raw_tool_calls = msg.get("tool_calls")
            # Format tool_calls for OpenAI compatibility
            parsed_tool_calls = None
            if raw_tool_calls:
                parsed_tool_calls = []
                for i, tc in enumerate(raw_tool_calls):
                    fn = tc.get("function", tc) if isinstance(tc, dict) else tc
                    parsed_tool_calls.append({
                        "id": f"call_{job_id}_{i}" if 'job_id' in dir() else f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "{}") if isinstance(fn.get("arguments"), str) else __import__("json").dumps(fn.get("arguments", {})),
                        }
                    })
            usage = response.get("usage", {})
            tokens_out = usage.get("completion_tokens", 0)
            tps = tokens_out / dt if dt > 0 else 0

            log.info(f"Inférence: {tokens_out} tokens en {dt:.2f}s ({tps:.1f} t/s)")

            return InferenceResult(
                text=text,
                tokens_generated=tokens_out,
                tokens_per_sec=round(tps, 2),
                duration_sec=round(dt, 3),
                model=self._model_name,
                tool_calls=parsed_tool_calls,
            )
        finally:
            self._semaphore.release()

    @property
    def loaded(self) -> bool:
        return self.model is not None

    @property
    def model_name(self) -> str:
        return self._model_name
