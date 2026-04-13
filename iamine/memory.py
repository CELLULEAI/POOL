"""RAG memory system — memoire vectorisee long-terme pour les utilisateurs IAMINE.

Utilise sentence-transformers pour les embeddings et pgvector pour le stockage.
Chaque fait est chiffre avec le token utilisateur (zero-knowledge).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

log = logging.getLogger("iamine.memory")

# Lazy-loaded embedding model (~80MB RAM, charge au premier appel)
_model = None


def _get_model():
    """Charge le modele d'embedding (all-MiniLM-L6-v2, 384-dim)."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            log.info("Embedding model loaded: all-MiniLM-L6-v2 (384-dim)")
        except ImportError:
            log.warning("sentence-transformers not installed — RAG disabled")
            return None
    return _model


def embed_text(text: str) -> list[float] | None:
    """Encode un texte en vecteur 384-dim. Retourne None si modele indisponible."""
    model = _get_model()
    if model is None:
        log.warning("RAG embedding failed: model not loaded")
        return None
    return model.encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]] | None:
    """Encode un batch de textes. Retourne None si modele indisponible."""
    model = _get_model()
    if model is None:
        log.warning("RAG embedding failed: model not loaded")
        return None
    return model.encode(texts, normalize_embeddings=True).tolist()


def token_hash(api_token: str) -> str:
    """Hash one-way du token pour isolation utilisateur en DB."""
    return hashlib.sha256(api_token.encode()).hexdigest()


def parse_facts(summary: str) -> list[str]:
    """Extrait les faits individuels d'un resume de compaction.

    Formats supportes :
    - '1. Name: Elena'
    - '- Age: 41'
    - '* Favorite color: blue'
    """
    facts = []
    for line in summary.strip().split("\n"):
        line = line.strip()
        # Ignorer les lignes trop courtes ou les headers
        if len(line) < 5:
            continue
        # Matcher les listes numerotees ou a puces
        m = re.match(r'^(?:\d+[\.\)]\s*|[-*]\s*)(.*)', line)
        if m and len(m.group(1).strip()) > 3:
            facts.append(m.group(1).strip())
        elif not line.startswith("#") and not line.startswith("[") and len(line) > 10:
            # Ligne de texte libre assez longue
            facts.append(line)
    return facts


async def store_facts(store, api_token: str, summary: str, conv_id: str = "") -> int:
    """Parse, embed et stocke les faits d'un resume de compaction.

    Retourne le nombre de faits stockes.
    """
    facts = parse_facts(summary)
    if not facts:
        return 0

    embeddings = embed_batch(facts)
    if embeddings is None:
        return 0

    th = token_hash(api_token)
    stored = 0
    for fact, emb in zip(facts, embeddings):
        try:
            await store.store_memory(th, emb, fact, api_token, conv_id)
            stored += 1
        except Exception as e:
            log.warning(f"Failed to store memory: {e}")

    if stored:
        log.info(f"RAG: {stored}/{len(facts)} facts stored for user {th[:8]}...")
    else:
        log.warning(f"RAG: 0 facts stored (embedding or DB failure) for user {th[:8]}...")
    return stored


async def retrieve_context(store, api_token: str, query: str,
                           limit: int = 5, min_similarity: float = 0.35,
                           conv_id: str = "") -> str:
    """Recherche les memoires pertinentes pour une requete.

    Retourne un bloc de texte formate pour injection dans le system prompt,
    ou une chaine vide si rien de pertinent.
    """
    from .db import _decrypt_fact

    query_emb = embed_text(query)
    if query_emb is None:
        log.warning("RAG retrieve: embedding failed, skipping context retrieval")
        return ""

    th = token_hash(api_token)
    results = await store.search_memories(th, query_emb, limit, min_similarity, conv_id=conv_id)
    if not results:
        log.warning(f"RAG retrieve: no matching memories for user {th[:8]}...")
        return ""

    # Dechiffrer et formater
    facts = []
    for r in results:
        text = _decrypt_fact(r["fact_text_enc"], r["salt"], api_token)
        if text:
            facts.append(f"- {text}")

    if not facts:
        return ""

    # Limiter a ~1600 chars (~400 tokens) pour ne pas saturer le contexte
    result = "[Long-term memory — relevant facts from past conversations]\n" + "\n".join(facts)
    if len(result) > 1600:
        result = result[:1600] + "..."
    return result
