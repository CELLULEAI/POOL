"""Tests de caracterisation — heuristiques de routing (audit 2026-06-22).

Verrouille les fonctions pures qui pilotent le choix concret du worker dans
router.select_worker : fit_bonus (table de substitution de tier) et
tier_from_model_path (derivation du tier depuis le model_path). DB-free.
"""
from iamine.core.routing_heuristic import fit_bonus, tier_from_model_path


# --- fit_bonus : bonus de score par (preferred_tier, worker_tier) -----------

def test_fit_bonus_exact_match_is_highest():
    assert fit_bonus("small", "small") == 500
    assert fit_bonus("medium", "medium") == 500
    assert fit_bonus("code", "code") == 500
    assert fit_bonus("large", "large") == 500


def test_fit_bonus_substitution_values():
    assert fit_bonus("medium", "code") == 220
    assert fit_bonus("code", "medium") == 320
    assert fit_bonus("small", "large") == 40
    assert fit_bonus("large", "small") == 20


def test_fit_bonus_unknown_pair_is_zero():
    assert fit_bonus("medium", "inconnu") == 0.0
    assert fit_bonus("inconnu", "medium") == 0.0


def test_fit_bonus_empty_args_is_zero():
    assert fit_bonus("", "medium") == 0.0
    assert fit_bonus("medium", "") == 0.0


# --- tier_from_model_path ----------------------------------------------------

def test_tier_coder_takes_precedence():
    # "coder" -> "code" meme si une taille est presente.
    assert tier_from_model_path("qwen3-coder-30b") == "code"
    assert tier_from_model_path("coder-2b") == "code"


def test_tier_from_size_boundaries():
    assert tier_from_model_path("tinyllama-1b") == "small"
    assert tier_from_model_path("model-4b") == "small"     # <= 4 -> small
    assert tier_from_model_path("model-5b") == "medium"    # 5..9 -> medium
    assert tier_from_model_path("qwen3-9b") == "medium"
    assert tier_from_model_path("model-10b") == "large"    # > 9 -> large
    assert tier_from_model_path("llama-70b") == "large"


def test_tier_unknown_or_empty_is_blank():
    assert tier_from_model_path("mystery-model") == ""
    assert tier_from_model_path("") == ""
    assert tier_from_model_path(None) == ""
