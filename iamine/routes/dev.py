"""Dev/debug endpoints — /v1/dev/backup, /v1/dev/signal, /v1/dev/inbox."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("iamine.pool")

REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


def _pool():
    from iamine.pool import pool
    return pool


# ─── GET /v1/dev/backup ─────────────────────────────────────────────────────

@router.get("/v1/dev/backup")
async def dev_backup():
    """Sert le backup tar.gz pour download (temporaire)."""
    from fastapi.responses import FileResponse as FR
    backup = Path("/tmp/iamine-backup-2026-03-31.tar.gz")
    if backup.exists():
        return FR(str(backup), media_type="application/gzip", filename=backup.name)
    return JSONResponse({"error": "no backup found"}, status_code=404)


# ─── GET /v1/dev/signal ─────────────────────────────────────────────────────

@router.get("/v1/dev/signal")
async def dev_signal():
    """Signal de mise à jour pour la boucle autonome David/Claude."""
    signal_file = REPORTS_DIR / "claude" / "signal-upgrade.json"
    if signal_file.exists():
        import json as _json
        return _json.loads(signal_file.read_text(encoding="utf-8"))
    return {"version": None, "action": None, "message": "no pending upgrade"}


# ─── GET /v1/dev/inbox ──────────────────────────────────────────────────────

@router.get("/v1/dev/inbox")
async def dev_inbox():
    """Lit les rapports avec leur contenu complet."""
    reports = []
    for author in ["david", "regis", "wasa"]:
        author_dir = REPORTS_DIR / author
        if not author_dir.exists():
            continue
        for f in sorted(author_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            # Vérifier s'il y a une réponse claude
            response_file = REPORTS_DIR / "claude" / f"re-{author}-{f.name}"
            has_response = response_file.exists()
            response_content = response_file.read_text(encoding="utf-8") if has_response else ""
            reports.append({
                "author": author,
                "filename": f.name,
                "content": f.read_text(encoding="utf-8"),
                "status": "traite" if has_response else "en_attente",
                "response": response_content,
            })
    return {"reports": reports}
