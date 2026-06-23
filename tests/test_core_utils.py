"""Tests de caracterisation — utilitaires partages (audit 2026-06-22).

iamine.core.utils est importe par pool/router/worker. On verrouille le parsing de
taille de modele (regex a separateurs stricts), le parsing de version semantique,
et le nettoyage du thinking-mode. DB-free, fonctions pures.
"""
from iamine.core.utils import parse_model_size, parse_version, strip_thinking


# --- parse_model_size : exige des separateurs autour de la taille -----------

def test_parse_model_size_typical():
    assert parse_model_size("Qwen3.5-9B.gguf") == 9.0
    assert parse_model_size("Qwen3-Coder-30B-A3B") == 30.0
    assert parse_model_size("tinyllama-1.1B-chat.gguf") == 1.1


def test_parse_model_size_requires_trailing_separator():
    # Sans separateur apres le 'B', la regex ne matche pas -> 0 (comportement
    # non-evident a geler : un model_path mal forme ne donne PAS de taille).
    assert parse_model_size("Qwen3.5-9B") == 0


def test_parse_model_size_no_size_or_empty():
    assert parse_model_size("no-size-model.gguf") == 0
    assert parse_model_size("") == 0


# --- parse_version : tuple semantique, fallback (0,0,0) ---------------------

def test_parse_version_typical():
    assert parse_version("1.0.3") == (1, 0, 3)
    assert parse_version("0.2.10") == (0, 2, 10)
    assert parse_version("1.2") == (1, 2)


def test_parse_version_invalid_falls_back():
    assert parse_version("abc") == (0, 0, 0)
    assert parse_version(None) == (0, 0, 0)
    assert parse_version("") == (0, 0, 0)


# --- strip_thinking ----------------------------------------------------------

def test_strip_thinking_removes_closed_tags():
    assert strip_thinking("<think>reasoning here</think>answer") == "answer"


def test_strip_thinking_passthrough_and_empty():
    assert strip_thinking("just an answer") == "just an answer"
    assert strip_thinking("") == ""


def test_strip_thinking_thinking_process_keeps_text_before():
    assert strip_thinking("answer before\nThinking Process: blah") == "answer before"
