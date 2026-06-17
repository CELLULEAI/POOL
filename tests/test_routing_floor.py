"""Tests du plancher de qualité du routage (curseur 9B / quality_score>=75).

Vérifie :
- un prompt NON-trivial n'est jamais routé vers un modèle SOUS le plancher quand
  un worker adéquat (>=q75) est idle ;
- GARDE-FOU #1 : JAMAIS de 503 — dégradation gracieuse vers le plus fort dispo
  quand seuls des workers sous-plancher sont idle ;
- l'arithmétique courte n'est plus classée 'small' ;
- le prédicat model_below_floor est basé sur quality_score (MoE-safe, garde-fou #2).

Runnable direct (`python tests/test_routing_floor.py`) ou via pytest.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from iamine.router import SmartRouter
from iamine.core.routing_heuristic import classify_prompt
from iamine import models as M

M1B = "google_gemma-3-1b-it-Q4_K_M.gguf"     # q30 -> sous plancher
M4B = "Qwen_Qwen3.5-4B-Q4_K_M.gguf"          # q55 -> sous plancher
M9B = "Qwen_Qwen3.5-9B-Q4_K_M.gguf"          # q75 -> adequat
M27B = "Qwen_Qwen3.5-27B-Q4_K_M.gguf"        # q92 -> adequat


class _W:
    """Worker mock minimal (select_worker n'accede qu'a .busy/.worker_id/.info)."""

    def __init__(self, wid, model_path, bench=25.0, busy=False):
        self.worker_id = wid
        self.busy = busy
        self.info = {
            "model_path": model_path, "bench_tps": bench, "real_tps": 0.0,
            "ctx_size": 8192, "total_jobs": 0, "jobs_failed": 0,
            "has_gpu": True, "proxy_mode": False, "hostname": "test",
        }


def _route(workers, tier="medium", conf=0.8, requested_model=None, conv_id="c"):
    r = SmartRouter()
    conv = r.get_or_create_conversation(conv_id)
    return r.select_worker(
        conv, {w.worker_id: w for w in workers},
        requested_model=requested_model, pool_version=None, approved_files=None,
        preferred_tier=tier, preferred_confidence=conf,
    )


def test_floor_excludes_below_when_adequate_idle():
    # 1B + 9B idle, prompt non-trivial -> doit TOUJOURS choisir le 9B
    for _ in range(20):  # robuste au round-robin
        wid = _route([_W("w1b", M1B), _W("w9b", M9B)], tier="medium")
        assert wid == "w9b", f"plancher non applique: {wid}"


def test_never_503_graceful_degrade():
    # GARDE-FOU #1 : seul un 1B idle -> repond quand meme (jamais None / 503)
    wid = _route([_W("only1b", M1B)], tier="large")
    assert wid == "only1b", f"503 au lieu de degradation gracieuse: {wid}"


def test_degrade_picks_strongest_below_floor():
    # seuls des sous-plancher idle (1B + 4B) -> le PLUS FORT (4B), jamais le 1B
    for _ in range(20):
        wid = _route([_W("w1b", M1B), _W("w4b", M4B)], tier="medium")
        assert wid == "w4b", f"degrade n'a pas pris le plus fort: {wid}"


def test_trivial_allows_small():
    # prompt trivial (tier small) -> pas de plancher, le 1B reste eligible
    wid = _route([_W("w1b", M1B)], tier="small", conf=0.95)
    assert wid == "w1b", f"prompt trivial bloque a tort: {wid}"


def test_explicit_model_not_floored():
    # requested_model explicite -> pas de plancher (on honore le choix utilisateur)
    wid = _route([_W("w1b", M1B), _W("w9b", M9B)], tier="medium", requested_model="gemma")
    assert wid == "w1b", f"requested_model explicite non honore: {wid}"


def test_floor_with_three_tiers():
    # 1B + 9B + 27B, large -> jamais le 1B (un des deux adequats)
    for _ in range(20):
        wid = _route([_W("w1b", M1B), _W("w9b", M9B), _W("w27b", M27B)], tier="large")
        assert wid in ("w9b", "w27b"), f"plancher viole: {wid}"


def test_arithmetic_not_small():
    for p in ["17 x 23", "Calcule 17 x 23.", "2+2", "combien font 144 / 12 ?", "3^4 = ?"]:
        t, _ = classify_prompt(p)
        assert t != "small", f"{p!r} classe {t} (devrait etre non-small)"


def test_dates_phones_stay_trivial():
    # non-regression : pas de faux positif arithmetique
    for p in ["on se voit le 17/06/2026", "appelle le 06 12 34 56 78", "version 3.5"]:
        t, _ = classify_prompt(p)
        assert t == "small", f"faux positif arithmetique sur {p!r} -> {t}"


def test_floor_predicate_quality_based():
    assert M.model_below_floor(M1B) is True
    assert M.model_below_floor(M4B) is True
    assert M.model_below_floor(M9B) is False
    assert M.model_below_floor(M27B) is False
    # GARDE-FOU #2 : MoE 35B-A3B = q95 malgre 3B actifs -> adequat (pas sous plancher)
    assert M.model_below_floor("Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf") is False
    # proxy hors-registry fort -> eligible ; inconnu non parsable -> eligible
    assert M.model_below_floor("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf") is False
    assert M.model_below_floor("some-unknown-model.gguf") is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"=> {passed}/{len(tests)} tests OK")
    sys.exit(0 if passed == len(tests) else 1)
