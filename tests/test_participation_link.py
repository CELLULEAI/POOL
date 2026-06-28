"""Tests du lien worker -> compte (Release A du gating participation).

Vérifie la brique de LECTURE sur laquelle la future porte d'accès (B) s'appuiera :
`Pool.account_contributing_workers(account_id)` ne retourne QUE les workers
réellement rattachés au compte, vivants (fenêtre de grâce) et au-dessus du
plancher de contribution (≥9B / q75 par défaut, réglable).

Doctrine (David 2026-06-28) : participation libre pour tout atome, mais le
SERVICE de qualité exige ≥9B (sous 9B = non viable par expérience).

Runnable direct (`python tests/test_participation_link.py`) ou via pytest.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from iamine import models as M
from iamine.pool import Pool

M1B = "google_gemma-3-1b-it-Q4_K_M.gguf"      # q30
M7B = "Qwen2.5-7B-Instruct-Q4_K_M.gguf"       # q65
M9B = "Qwen_Qwen3.5-9B-Q4_K_M.gguf"           # q75
M27B = "Qwen_Qwen3.5-27B-Q4_K_M.gguf"         # q92


class _W:
    """Worker mock minimal : account_contributing_workers ne lit que .info / .last_seen."""

    def __init__(self, owner, model_path, last_seen=None):
        self.worker_id = f"w-{owner}-{model_path[:6]}"
        self.last_seen = time.time() if last_seen is None else last_seen
        self.info = {"owner_account_id": owner, "model_path": model_path}


def _pool(workers):
    p = Pool()
    p.workers = {w.worker_id: w for w in workers}
    return p


def test_compte_avec_worker_9B_qualifie():
    p = _pool([_W("acc-A", M9B)])
    res = p.account_contributing_workers("acc-A")
    assert len(res) == 1, "un 9B rattaché et vivant doit qualifier le compte"
    print("OK test_compte_avec_worker_9B_qualifie")


def test_sous_plancher_ne_qualifie_pas():
    # 1B et 7B sont sous q75 -> ne qualifient pas au plancher par défaut
    p = _pool([_W("acc-A", M1B), _W("acc-A", M7B)])
    assert p.account_contributing_workers("acc-A") == [], "sous 9B ne doit pas qualifier"
    print("OK test_sous_plancher_ne_qualifie_pas")


def test_relache_7B_via_curseur():
    # Si David relâche le plancher à 65 (7B), le 7B qualifie
    os.environ["IAMINE_MIN_CONTRIB_QUALITY"] = "65"
    try:
        p = _pool([_W("acc-A", M7B)])
        assert len(p.account_contributing_workers("acc-A")) == 1
        # ...mais le 1B (q30) reste sous le plancher relâché
        p2 = _pool([_W("acc-A", M1B)])
        assert p2.account_contributing_workers("acc-A") == []
    finally:
        os.environ.pop("IAMINE_MIN_CONTRIB_QUALITY", None)
    print("OK test_relache_7B_via_curseur")


def test_worker_d_un_autre_compte_exclu():
    p = _pool([_W("acc-B", M27B)])
    assert p.account_contributing_workers("acc-A") == [], "worker d'un autre compte ne compte pas"
    print("OK test_worker_d_un_autre_compte_exclu")


def test_worker_perime_hors_grace_exclu():
    # last_seen il y a 2h, grâce par défaut 30 min -> exclu
    p = _pool([_W("acc-A", M9B, last_seen=time.time() - 7200)])
    assert p.account_contributing_workers("acc-A") == [], "worker hors fenêtre de grâce exclu"
    print("OK test_worker_perime_hors_grace_exclu")


def test_worker_non_rattache_ignore():
    # owner_account_id absent (participe mais non lié) -> n'ouvre l'accès d'aucun compte
    w = _W(None, M9B)
    w.info.pop("owner_account_id")
    p = _pool([w])
    assert p.account_contributing_workers("acc-A") == []
    assert p.account_contributing_workers("") == []
    print("OK test_worker_non_rattache_ignore")


if __name__ == "__main__":
    test_compte_avec_worker_9B_qualifie()
    test_sous_plancher_ne_qualifie_pas()
    test_relache_7B_via_curseur()
    test_worker_d_un_autre_compte_exclu()
    test_worker_perime_hors_grace_exclu()
    test_worker_non_rattache_ignore()
    print("\nTOUS LES TESTS PASSENT")
