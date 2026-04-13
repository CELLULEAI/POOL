"""Utilitaires partages entre les modules IAMINE."""

import re

_MODEL_SIZE_RE = re.compile(r'[\-_](\d+(?:\.\d+)?)[Bb][\-_\.]')


def parse_model_size(model_path: str) -> float:
    """Extrait la taille en milliards depuis un path GGUF (ex: 'Qwen3.5-9B' -> 9.0)."""
    m = _MODEL_SIZE_RE.search(model_path)
    return float(m.group(1)) if m else 0


def parse_version(v: str) -> tuple[int, ...]:
    """Parse version string en tuple pour comparaison semantique.
    '0.2.10' -> (0, 2, 10)"""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def strip_thinking(text: str) -> str:
    """Retire le thinking mode des reponses Qwen 3.5 / Qwen3 / QwQ.

    Formes gerees :
    - <think>...</think> contenu           (balises fermees)
    - <think> contenu sans fermeture       (thinking tronque/malforme)
    - Thinking Process: ...                (variante Qwen 3.5)
    """
    if not text:
        return text
    # Etape 1 : retirer les balises <think>...</think> bien fermees
    text = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()
    # Etape 1b : si un <think> reste sans </think> (thinking non ferme),
    # couper tout ce qui suit <think> jusqu au prochain double saut de ligne
    if '<think>' in text:
        idx = text.index('<think>')
        before = text[:idx].strip()
        after = text[idx + len('<think>'):]
        if '\n\n' in after:
            real = after.split('\n\n', 1)[1].strip()
            text = (before + '\n\n' + real).strip() if before else real
        else:
            text = (before + ' ' + after).strip()
    # Etape 2 : retirer "Thinking Process:"
    if "Thinking Process:" in text:
        parts = text.split("Thinking Process:")
        before = parts[0].strip()
        if before:
            return before
        last = parts[-1].strip()
        if "\n\n" in last:
            return last.split("\n\n", 1)[-1].strip() or last
        return last
    return text


# --- Option B : controle adaptatif du thinking mode ---
THINKING_MODEL_KEYWORDS = ("qwen3", "qwq")
SIMPLE_CHAT_TOKENS_MAX = 2000  # seuil en tokens (~ 8000 chars)
COMPLEX_KEYWORDS = (
    "code", "python", "javascript", "typescript", "rust", "golang",
    "function", "algorithm", "debug", "error", "stack trace", "traceback",
    "refactor", "refactoring", "optimize", "optimise",
    "reflechis", "reflechir", "analyse", "analyser",
    "raisonne", "raisonner", "demontre", "prouve",
    "step by step", "etape par etape",
)


def should_disable_thinking(model_path: str, messages: list, has_tools: bool) -> bool:
    """Decide si on injecte /no_think pour accelerer la reponse.

    N injecte JAMAIS en presence de tools (tool-calling DOIT raisonner).
    Injecte uniquement pour les modeles thinking (qwen3/qwq) sur du chat court
    sans mots-cles complexes.
    """
    if has_tools:
        return False
    mp = (model_path or "").lower()
    if not any(kw in mp for kw in THINKING_MODEL_KEYWORDS):
        return False
    total_chars = sum(len(m.get("content", "") or "") for m in messages)
    if total_chars > SIMPLE_CHAT_TOKENS_MAX * 4:
        return False
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = (m.get("content") or "").lower()
            break
    if any(kw in last_user for kw in COMPLEX_KEYWORDS):
        return False
    return True


# --- Server secret & token derivation (moved from pool.py) ---
import hashlib
import hmac
import secrets
import logging
from pathlib import Path

# Le .server_secret est au niveau iamine/ (parent du package)
# Depuis core/utils.py: __file__ = iamine/core/utils.py → .parent.parent.parent = iamine/
_SERVER_SECRET_FILE = Path(__file__).parent.parent.parent / ".server_secret"
if _SERVER_SECRET_FILE.exists():
    _SERVER_SECRET = _SERVER_SECRET_FILE.read_text().strip()
else:
    _SERVER_SECRET = secrets.token_hex(32)
    _SERVER_SECRET_FILE.write_text(_SERVER_SECRET)
    logging.getLogger("iamine.pool").info("Generated new server secret")


def _derive_api_token(worker_id: str) -> str:
    """Dérive un token API à partir du worker_id + secret serveur (non devinable)."""
    raw = hmac.new(_SERVER_SECRET.encode(), worker_id.encode(), hashlib.sha256).hexdigest()
    return "iam_" + raw[:32]


def _derive_account_token(email: str) -> str:
    """Dérive un token de compte à partir de l'email + secret serveur."""
    raw = hmac.new(_SERVER_SECRET.encode(), email.encode(), hashlib.sha256).hexdigest()
    return "acc_" + raw[:32]
