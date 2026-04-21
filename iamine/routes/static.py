"""Static/download endpoints — pages, modeles GGUF, PyPI, scripts d'installation."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("iamine.pool")
def _is_pool_operator_mode() -> bool:
    """True if running as a community Docker pool (restricted mode)."""
    import os
    return os.environ.get("POOL_OPERATOR_MODE", "").strip().lower() in (
        "1", "true", "yes", "on")




# --- Lazy imports pour eviter les imports circulaires ---

def _pool():
    from iamine.pool import pool
    return pool


def _static_dir():
    from iamine.pool import static_dir
    return static_dir


def _version():
    from iamine import __version__
    return __version__


# --- Page d'accueil ---
@router.get("/")
async def root():
    # Pool operator mode: redirect public traffic to cellule.ai — the Docker
    # image is for pool infrastructure, not for hosting the public site.
    if _is_pool_operator_mode():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="https://cellule.ai", status_code=302)
    from fastapi.responses import FileResponse
    index = _static_dir() / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": f"IAMINE Pool v{_version()}", "docs": "/docs", "status": "/v1/status"}


# --- Serveur de modeles GGUF ---
@router.get("/v1/models/download/{filename}")
async def download_model(filename: str):
    """Sert un modele GGUF depuis le dossier models/ du serveur."""
    from fastapi.responses import FileResponse as FR
    # Protection path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    models_dir = Path(__file__).parent.parent.parent / "models"
    fpath = (models_dir / filename).resolve()
    if not str(fpath).startswith(str(models_dir.resolve())):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    if fpath.exists() and fpath.is_file() and filename.endswith(".gguf"):
        return FR(str(fpath), media_type="application/octet-stream", filename=filename)
    return JSONResponse({"error": f"Model not found: {filename}"}, status_code=404)


@router.get("/v1/models/available")
async def available_models():
    """Liste les modeles GGUF disponibles sur le serveur."""
    models_dir = Path(__file__).parent.parent.parent / "models"
    files = []
    if models_dir.exists():
        for f in sorted(models_dir.glob("*.gguf")):
            files.append({
                "filename": f.name,
                "size_mb": round(f.stat().st_size / (1024**2)),
                "download_url": f"/v1/models/download/{f.name}",
            })
    return {"models": files}


# --- PyPI prive — sert le package iamine-ai depuis le VPS ---
@router.get("/pypi/iamine-ai/")
async def pypi_index():
    """Index PyPI simple pour pip install -i https://iamine.org/pypi."""
    dist_dir = Path(__file__).parent.parent.parent / "dist"
    if not dist_dir.exists():
        return JSONResponse({"error": "No dist/ directory. Run: python -m build"}, status_code=404)
    files = sorted(dist_dir.glob("iamine_ai-*.whl")) + sorted(dist_dir.glob("iamine-ai-*.tar.gz"))
    links = "".join(f'<a href="/pypi/dist/{f.name}">{f.name}</a><br>' for f in files)
    html = f"<html><body><h1>iamine-ai</h1>{links}</body></html>"
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@router.get("/pypi/dist/{filename}")
async def pypi_download(filename: str):
    """Sert un fichier wheel/sdist depuis dist/ ou dist/deps/."""
    from fastapi.responses import FileResponse as FR
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    dist_dir = Path(__file__).parent.parent.parent / "dist"
    # Chercher dans dist/ puis dist/deps/
    for search_dir in [dist_dir, dist_dir / "deps"]:
        fpath = (search_dir / filename).resolve()
        if not str(fpath).startswith(str(search_dir.resolve())):
            continue
        if fpath.exists() and fpath.is_file():
            return FR(str(fpath))
    return JSONResponse({"error": "File not found"}, status_code=404)


@router.get("/pypi/{package}/")
async def pypi_package_index(package: str):
    """Index PyPI simple pour n'importe quel package servi localement."""
    from fastapi.responses import HTMLResponse
    dist_dir = Path(__file__).parent.parent.parent / "dist"
    deps_dir = dist_dir / "deps"
    # Normaliser le nom (PEP 503: - et _ sont interchangeables)
    import re
    norm = re.sub(r"[-_]+", "[-_]", package) + "-"
    files = []
    for d in [dist_dir, deps_dir]:
        if d.exists():
            for f in d.glob("*.whl"):
                if re.match(norm, f.name, re.IGNORECASE):
                    files.append(f)
            for f in d.glob("*.tar.gz"):
                if re.match(norm, f.name, re.IGNORECASE):
                    files.append(f)
    files.sort(key=lambda f: f.name)
    links = "".join(f'<a href="/pypi/dist/{f.name}">{f.name}</a><br>' for f in files)
    html = f"<html><body><h1>{package}</h1>{links}</body></html>"
    return HTMLResponse(html)


@router.get("/pypi/")
async def pypi_root_index():
    """Index racine PyPI — liste tous les packages disponibles."""
    from fastapi.responses import HTMLResponse
    dist_dir = Path(__file__).parent.parent.parent / "dist"
    deps_dir = dist_dir / "deps"
    packages = set()
    for d in [dist_dir, deps_dir]:
        if d.exists():
            for f in d.glob("*.whl"):
                # Nom du package = premiere partie du nom du fichier
                packages.add(f.name.split("-")[0].replace("_", "-").lower())
    links = "".join(f'<a href="/pypi/{p}/">{p}</a><br>' for p in sorted(packages))
    html = f"<html><body><h1>IAMINE PyPI</h1>{links}</body></html>"
    return HTMLResponse(html)


# --- Pages speciales ---
@router.get("/m")
async def mobile_page():
    """Page mobile epuree (redirect to cellule.ai in operator mode)."""
    if _is_pool_operator_mode():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="https://cellule.ai/m", status_code=302)
    from fastapi.responses import FileResponse
    mobile = Path(__file__).parent.parent / "static" / "mobile-app.html"
    if mobile.exists():
        return FileResponse(str(mobile), media_type="text/html")
    return JSONResponse({"error": "mobile page not found"}, status_code=404)


# --- Scripts d'installation ---
@router.get("/install.sh")
async def install_script():
    """Script d'installation Linux — curl -sL https://iamine.org/install.sh | bash"""
    from fastapi.responses import FileResponse as FR
    script = Path(__file__).parent.parent / "install.sh"
    if script.exists():
        return FR(str(script), media_type="text/x-shellscript", filename="install.sh")
    return JSONResponse({"error": "install.sh not found"}, status_code=404)


@router.get("/install.ps1")
async def install_script_win():
    """Script d'installation Windows — irm https://iamine.org/install.ps1 | iex"""
    from fastapi.responses import FileResponse as FR
    script = Path(__file__).parent.parent / "install.ps1"
    if script.exists():
        return FR(str(script), media_type="text/plain", filename="install.ps1")
    return JSONResponse({"error": "install.ps1 not found"}, status_code=404)


@router.get("/install-worker.sh")
async def install_worker_script():
    """One-liner installer Linux+macOS workers.
    Usage : curl -sSL https://cellule.ai/install-worker.sh | bash
    Cf. memory project_doctrine_pools_sentraident_settled + doctrine UX
    compliqué-de-faire-simple : installation 1-ligne load-bearing pour
    l'onboarding communautaire.
    Le fichier vit à la racine du repo (iamine-work/install-worker.sh),
    d'où le .parent.parent.parent pour remonter depuis routes/static.py.
    """
    from fastapi.responses import FileResponse as FR
    script = Path(__file__).parent.parent.parent / "install-worker.sh"
    if script.exists():
        return FR(str(script), media_type="text/x-shellscript", filename="install-worker.sh")
    return JSONResponse({"error": "install-worker.sh not found"}, status_code=404)


# --- Contact ---
@router.post("/v1/contact")
async def contact(data: dict):
    """Stocke un message de contact."""
    import json as _json
    from pathlib import Path as _Path
    contact_file = _Path("contacts.jsonl")
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "name": data.get("name", ""),
        "email": data.get("email", ""),
        "message": data.get("message", ""),
    }
    with open(contact_file, "a") as f:
        f.write(_json.dumps(entry) + "\n")
    log.info(f"Contact from {entry['email']}: {entry['message'][:50]}...")
    return {"status": "ok"}


# --- Legacy /new route ---
# Historically served index_v2.html (early V1 landing design). The canonical
# frontend is now index.html (served at /) which is the current V2 with
# login modal, tools, API, token nav, etc. This route 301 redirects to /
# to eliminate V1 surface area. Keep for backward compat with old bookmarks.
@router.get("/new")
async def new_frontend_deprecated():
    from fastapi.responses import RedirectResponse
    if _is_pool_operator_mode():
        return RedirectResponse(url="https://cellule.ai", status_code=302)
    return RedirectResponse(url="/", status_code=301)


# --- Ancien frontend (chat, dashboard, login Google/email) ---
@router.get("/app")
async def app_frontend():
    """Redirect to cellule.ai/app in pool operator mode."""
    if _is_pool_operator_mode():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="https://cellule.ai/app", status_code=302)
    from fastapi.responses import FileResponse
    app_html = _static_dir() / "index_app.html"
    if app_html.exists():
        return FileResponse(str(app_html), media_type="text/html")
    return JSONResponse({"error": "index_app.html not found"}, status_code=404)


# --- Templates pour iamine init (bootstrap projet coding agents) ---
@router.get("/v1/opencode-md")
async def opencode_md_template():
    """Retourne le template OPENCODE.md pour le bootstrap d un projet via 'iamine init'."""
    from fastapi.responses import FileResponse
    tpl = Path(__file__).parent.parent / "templates" / "opencode_init.md"
    if tpl.exists():
        return FileResponse(str(tpl), media_type="text/markdown", filename="OPENCODE.md")
    return JSONResponse({"error": "opencode_init.md template not found"}, status_code=404)



# ── /docs/* — Serve static pool docs (landing pages, docker compose, etc.)
@router.get("/docs/{filename:path}")
async def docs_static(filename: str):
    """Serve files from iamine/static/docs/. Used for pool-docker.html
    and docker-compose.yml downloads from the pool bootstrap landing."""
    from fastapi.responses import FileResponse, JSONResponse
    from pathlib import Path
    if '/' in filename or '..' in filename:
        return JSONResponse({'error': 'invalid filename'}, status_code=400)
    fp = Path(__file__).parent.parent / 'static' / 'docs' / filename
    if not fp.exists() or not fp.is_file():
        return JSONResponse({'error': 'not found'}, status_code=404)
    media = 'text/html' if filename.endswith('.html') else 'text/plain'
    return FileResponse(str(fp), media_type=media)
