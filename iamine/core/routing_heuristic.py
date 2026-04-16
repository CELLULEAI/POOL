"""Heuristique lexicale pour classifier un prompt par complexité (Phase 2 smart routing).

Phase 2 = baseline avant Phase 3 (KNN pgvector) et Phase 4 (LLM idle classifier).
Voir project_todo_smart_routing.md et migration 025_smart_routing_instrumentation.sql.

Tiers retournes :
- small  : prompts triviaux, politesse, questions courtes non-reasoning  → 2B/4B
- medium : questions generales, explications, conversation classique     → 9B
- code   : code, debug, refactor, fonctions, APIs                         → Coder-30B
- large  : reasoning complexe, analyses longues                           → 27B/35B

Contrat : la confidence < 0.7 est traitee comme ambigue cote router
(hint faible, le deficit scoring et le round-robin restent libres).
"""
from __future__ import annotations

import re

_CODE_MARKERS = re.compile(
    r"```|`[^`\n]{3,}`|def\s+\w+\s*\(|class\s+\w+[:\(]|function\s+\w+\s*\("
    r"|import\s+\w+|from\s+\w+\s+import|#include\s*<|public\s+(?:static\s+)?\w+\s+\w+\s*\("
    r"|SELECT\s+.+\s+FROM|CREATE\s+TABLE|=>\s*{|console\.log",
    re.IGNORECASE,
)

_CODE_KEYWORDS = {
    "code", "coder", "function", "fonction", "debug", "refactor", "refactoriser",
    "implement", "implemente", "implementer", "bug", "erreur", "stacktrace",
    "traceback", "exception", "api", "endpoint", "json", "python", "javascript",
    "typescript", "rust", "golang", "java", "kotlin", "swift", "sql", "query",
    "docker", "kubernetes", "nginx", "script", "regex", "unit test", "unittest",
    "pytest", "jest",
}

_REASONING_KEYWORDS = {
    "explique", "explique-moi", "analyse", "compare", "pourquoi", "why",
    "how does", "difference", "différence", "avantage", "inconvenient",
    "architecture", "strategy", "stratégie", "plan", "raisonne", "détaille",
    "step by step", "etape par etape", "pros and cons",
}

_TRIVIAL_EXACT = {
    "salut", "bonjour", "bonsoir", "hello", "hi", "hey", "coucou", "yo",
    "merci", "ok", "okay", "oui", "non", "yes", "no", "cool", "super",
    "parfait", "yep", "nope", "lol", "mdr", "\U0001f642", "\U0001f44d",
    "au revoir", "bye",
}


def classify_prompt(text: str) -> tuple[str, float]:
    """Classifie un prompt utilisateur en tier avec confidence.

    Args:
        text: le dernier message utilisateur (str, peut etre vide).

    Returns:
        (tier, confidence) — tier in {small, medium, code, large}, confidence in [0, 1].
        Quand confidence < 0.7 → considere ambigu (le router reste libre).
    """
    if not text:
        return "small", 0.5

    stripped = text.strip()
    lowered = stripped.lower()
    # normaliser pour matcher exact sans ponctuation finale
    stripped_clean = lowered.rstrip("?.!,;:")

    words = stripped.split()
    n_words = len(words)
    n_chars = len(stripped)

    # 1. Code block explicite ou pattern code fort → code tier haute confidence
    if _CODE_MARKERS.search(stripped):
        return "code", 0.95

    # 2. Trivial — match exact politesse courte (prioritaire car tres haute confidence)
    if stripped_clean in _TRIVIAL_EXACT:
        return "small", 0.95

    reasoning_hits = sum(1 for k in _REASONING_KEYWORDS if k in lowered)
    code_hits = sum(1 for k in _CODE_KEYWORDS if k in lowered)

    # 3. Large : reasoning explicite ET prompt long (heuristique lache)
    if reasoning_hits >= 2 and (n_words >= 120 or n_chars >= 800):
        return "large", 0.8
    if n_words >= 300 or n_chars >= 2000:
        return "large", 0.75

    # 4. Reasoning l'emporte sur code keywords quand les deux sont presents
    #    (un "compare python et rust" est une demande d'analyse, pas du code brut)
    if reasoning_hits >= 1 and n_words >= 3:
        return "medium", 0.72 if reasoning_hits >= 2 else 0.68
    if reasoning_hits >= 1 and n_words >= 2:
        return "medium", 0.55  # courte intention reasoning, ambigu

    # 5. Code : mots-cles non ambigus (aucune reasoning intent) + longueur minimale
    if code_hits >= 2:
        return "code", 0.82
    if code_hits == 1 and n_words >= 5:
        return "code", 0.7

    # 6. Trivial partiel (3 mots max contenant un terme trivial)
    if n_words <= 3 and any(p in stripped_clean for p in _TRIVIAL_EXACT):
        return "small", 0.85

    # 7. Medium par longueur
    if n_words >= 80:
        return "medium", 0.6
    if n_words >= 30:
        return "medium", 0.55

    # 8. Court sans indice specifique → small
    if n_words <= 6:
        return "small", 0.7
    if n_words <= 15:
        return "small", 0.58

    # 9. Default : ambigu medium
    return "medium", 0.5


# === Matrice de fit tier → worker_tier ===
# Le tier classifie guide le routing, mais le router reste non-bloquant :
# si le tier exact est busy, un tier "proche" prend le job. David a valide
# explicitement que le 9B (medium) est un bon subsitut pour coder,
# et que les modeles superieurs peuvent toujours traiter des prompts
# inferieurs (doctrine : "tout le pool travaille, pas de worker dedie").
#
# Valeurs = bonus de score ajoute dans select_worker (scale par confidence).
_TIER_FIT_BONUS: dict[str, dict[str, float]] = {
    "small":  {"small": 500, "medium": 120, "code": 60,  "large": 40},
    "medium": {"medium": 500, "code": 220,  "large": 160, "small": 80},
    "code":   {"code": 500,   "medium": 320, "large": 220, "small": 40},
    "large":  {"large": 500,  "code": 280,  "medium": 140, "small": 20},
}


def fit_bonus(preferred_tier: str, worker_tier: str) -> float:
    """Retourne le bonus de score pour un worker_tier donne un preferred_tier.

    Utilise par router.select_worker. Scale par confidence cote router.
    """
    if not preferred_tier or not worker_tier:
        return 0.0
    return _TIER_FIT_BONUS.get(preferred_tier, {}).get(worker_tier, 0.0)


def tier_from_model_path(model_path: str) -> str:
    """Derive le tier d'un worker depuis son model_path (pour matching)."""
    mp = (model_path or "").lower()
    if "coder" in mp:
        return "code"
    # on cherche le nombre de B
    m = re.search(r"(\d+(?:\.\d+)?)b", mp)
    if m:
        try:
            b = float(m.group(1))
            if b <= 4:
                return "small"
            if b <= 9:
                return "medium"
            return "large"
        except ValueError:
            pass
    return ""
