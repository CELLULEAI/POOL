"""Smart routing Phase 3 — embedder local pour KNN pgvector.

Modele : sentence-transformers/all-MiniLM-L6-v2 (384d, ~88MB, multilingue).
Precharge au demarrage du pool. ~50ms par prompt sur CPU, cache LRU sur 512
derniers prompts pour les re-prompts rapides.

Dep soft : si sentence-transformers absent, embed_prompt retourne None et le
routing fallback sur heuristique + LLM idle (Phase 2 + 4). Zero crash.

Voir project_todo_smart_routing.md Phase 3.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("iamine.routing_embeddings")

# Path absolu du modele preteleche — evite les bugs de cwd relatif de
# sentence-transformers (cherche d'abord un dir local du nom).
_EMBEDDER_PATH = os.environ.get(
    "IAMINE_EMBEDDER_PATH",
    str(Path.home() / ".cache" / "iamine-embedder"),
)

_model = None
_model_load_tried = False
EMBEDDING_DIM = 384


def _load_model():
    """Lazy-load du modele. Retourne None si indispo (dep ou fichiers manquants)."""
    global _model, _model_load_tried
    if _model is not None:
        return _model
    if _model_load_tried:
        return None
    _model_load_tried = True
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        log.info("sentence-transformers not installed — KNN routing disabled")
        return None
    if not Path(_EMBEDDER_PATH).exists():
        log.warning(f"Embedder path missing: {_EMBEDDER_PATH} — KNN routing disabled")
        return None
    try:
        _model = SentenceTransformer(_EMBEDDER_PATH)
        log.info(f"Embedder loaded: {_EMBEDDER_PATH} dim={_model.get_sentence_embedding_dimension()}")
        return _model
    except Exception as e:
        log.warning(f"Embedder load failed: {e} — KNN routing disabled")
        return None


@lru_cache(maxsize=512)
def _embed_cached(text: str) -> tuple[float, ...] | None:
    """Embed + normalize un prompt. Cache LRU pour re-prompts rapides.

    Retourne un tuple (hashable pour lru_cache). None si embedder indispo.
    """
    m = _load_model()
    if m is None:
        return None
    try:
        # normalize → vectors ont norme 1, cosine = dot product (rapide cote pgvector)
        vec = m.encode(text, normalize_embeddings=True)
        return tuple(float(x) for x in vec.tolist())
    except Exception as e:
        log.warning(f"encode failed: {e}")
        return None


def embed_prompt(text: str) -> list[float] | None:
    """API publique : embed un prompt, retourne list[float] de dim 384 ou None.

    Attention : blocking call (~50ms CPU). Wrap dans run_in_executor si appele
    depuis un handler async qui veut rester reactif.
    """
    if not text:
        return None
    text = text.strip()
    if len(text) > 1000:
        text = text[:1000]
    tup = _embed_cached(text)
    return list(tup) if tup is not None else None


def is_available() -> bool:
    """True si le pipeline embedder est fonctionnel (lazy-load tolere)."""
    return _load_model() is not None
