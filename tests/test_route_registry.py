"""Harnais route-level — snapshot de la surface API (audit 2026-06-22).

Filet pour les refactors par RELOCATION (deplacer des handlers entre fichiers sans
changer le comportement) : on fige l'ensemble des routes HTTP enregistrees
(methode + path + dependances declaratives, dont require_admin) ET le nombre total
d'APIRoutes. Un decoupage de god file qui preserve la surface DOIT laisser ces deux
invariants intacts ; toute route perdue/ajoutee/dupliquee ou tout require_admin
retire est attrape ici.

Golden : tests/route_registry_snapshot.txt. Ne le regenerer QUE pour un changement
de surface volontaire (jamais lors d'un simple refactor).
"""
import os
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

SNAPSHOT = Path(__file__).parent / "route_registry_snapshot.txt"
EXPECTED_APIROUTE_COUNT = 188  # surface non-dev (sans IAMINE_DEV) — +1 : route /demo (atout communautaire)


def _collect_api_routes(routes, seen=None):
    """Aplatit recursivement les APIRoute, quelle que soit la version FastAPI.

    Jusqu'a starlette 0.52, include_router aplatissait les routes directement
    dans app.routes. Depuis starlette 1.x, il insere un wrapper _IncludedRouter
    qui garde les routes sous .original_router (et les Mount les gardent sous
    .routes). On descend dans les deux pour rester agnostique a la version.
    """
    if seen is None:
        seen = set()
    out = []
    for r in routes:
        if isinstance(r, APIRoute) and id(r) not in seen:
            seen.add(id(r))
            out.append(r)
        sub = getattr(r, "routes", None)
        if sub:
            out.extend(_collect_api_routes(sub, seen))
        original = getattr(r, "original_router", None)  # starlette 1.x _IncludedRouter
        if original is not None and getattr(original, "routes", None):
            out.extend(_collect_api_routes(original.routes, seen))
    return out


def _live():
    from iamine.pool import app
    sigs, count = [], 0
    for r in _collect_api_routes(app.routes):
        count += 1
        methods = "|".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
        deps = ",".join(sorted(
            d.call.__name__ for d in r.dependant.dependencies if getattr(d, "call", None)
        ))
        sigs.append(f"{methods} {r.path} [{deps}]")
    return sigs, count


def _skip_if_dev():
    if os.environ.get("IAMINE_DEV") == "1":
        pytest.skip("le snapshot reflete la surface non-dev (IAMINE_DEV non defini)")


def test_route_surface_matches_snapshot():
    _skip_if_dev()
    live = set(_live()[0])
    expected = set(SNAPSHOT.read_text(encoding="utf-8").splitlines())
    added = sorted(live - expected)
    removed = sorted(expected - live)
    assert not added and not removed, (
        f"\nRoutes AJOUTEES ({len(added)}): {added}"
        f"\nRoutes RETIREES ({len(removed)}): {removed}"
        "\n-> Si le changement de surface est VOULU, regenerer route_registry_snapshot.txt."
    )


def test_no_accidental_duplicate_or_lost_registration():
    _skip_if_dev()
    count = _live()[1]
    assert count == EXPECTED_APIROUTE_COUNT, (
        f"{count} APIRoutes enregistrees (attendu {EXPECTED_APIROUTE_COUNT}). "
        "Un ecart sans changement de surface = double-registration d'un router ou route perdue."
    )
