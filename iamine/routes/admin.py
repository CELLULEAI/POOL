"""Admin endpoints — /admin/*, /v1/admin/*, /admin/api/*."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# ── Cellule.ai styled pages ──────────────────────────────────────────────────

_CELLULE_STYLE = "body{background:#0a0a0f;color:#e0e0e0;font-family:'Inter',system-ui,-apple-system,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.card{background:rgba(15,15,25,0.95);padding:2.5rem;border-radius:16px;border:1px solid rgba(0,212,255,0.2);width:420px;text-align:center;box-shadow:0 0 60px rgba(0,212,255,0.08),0 0 120px rgba(0,255,136,0.04);backdrop-filter:blur(20px)}h2{background:linear-gradient(135deg,#00d4ff,#00ff88);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:0.3rem;font-size:1.8rem;font-weight:800;letter-spacing:-0.5px}.sub{color:rgba(255,255,255,0.4);font-size:0.85rem;margin-bottom:2rem;letter-spacing:0.3px}input{width:100%;padding:0.8rem 1rem;margin:0.4rem 0;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);color:#e0e0e0;border-radius:10px;font-size:14px;box-sizing:border-box;transition:border-color 0.2s}input:focus{outline:none;border-color:rgba(0,212,255,0.4);box-shadow:0 0 20px rgba(0,212,255,0.08)}input::placeholder{color:rgba(255,255,255,0.2)}button{width:100%;padding:0.85rem;background:linear-gradient(135deg,#00d4ff,#00ff88);color:#0a0a0f;border:none;border-radius:10px;font-size:0.95rem;font-weight:700;cursor:pointer;text-transform:uppercase;letter-spacing:1px;margin-top:0.8rem;transition:all 0.2s}button:hover{opacity:0.9;transform:translateY(-1px);box-shadow:0 4px 20px rgba(0,212,255,0.3)}.err{color:#ff4466;font-size:0.85rem;margin-top:1rem;display:none;padding:0.5rem;border-radius:8px;background:rgba(255,68,102,0.08)}.ok{color:#00ff88;font-size:0.85rem;margin-top:1rem;display:none;padding:0.5rem;border-radius:8px;background:rgba(0,255,136,0.08)}.logo{font-size:0.75rem;color:rgba(255,255,255,0.15);margin-top:1.5rem;letter-spacing:2px;text-transform:uppercase}"

SETUP_PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Cellule.ai — Pool Setup</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>""" + _CELLULE_STYLE + """</style></head>
<body><div class="card"><h2>Pool Setup</h2>
<p class="sub">Create your administrator account</p>
<input type="email" id="em" placeholder="Email">
<input type="password" id="pw" placeholder="Password (min 6 characters)">
<input type="password" id="pw2" placeholder="Confirm password" onkeydown="if(event.key==='Enter')doSetup()">
<button onclick="doSetup()">Create Admin Account</button>
<div class="err" id="err"></div>
<div class="ok" id="ok"></div>
<div class="logo">Cellule.ai</div>
<script>
async function doSetup() {
    var em=document.getElementById('em').value;
    var pw=document.getElementById('pw').value;
    var pw2=document.getElementById('pw2').value;
    var err=document.getElementById('err');
    var ok=document.getElementById('ok');
    err.style.display='none'; ok.style.display='none';
    if(!em||!pw){err.textContent='Email and password required';err.style.display='block';return;}
    if(pw!==pw2){err.textContent='Passwords do not match';err.style.display='block';return;}
    if(pw.length<6){err.textContent='Password must be at least 6 characters';err.style.display='block';return;}
    var r=await fetch('/admin/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:em,password:pw})});
    var d=await r.json();
    if(d.ok){ok.textContent='Account created! Redirecting...';ok.style.display='block';setTimeout(function(){location.reload()},1500);}
    else{err.textContent=d.error||'Error';err.style.display='block';}
}
</script></div></body></html>"""

LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Cellule.ai — Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>""" + _CELLULE_STYLE + """.sep{margin:1.2rem 0;color:rgba(255,255,255,0.1);font-size:0.8rem;letter-spacing:2px}</style>
<script src="https://accounts.google.com/gsi/client" async></script></head>
<body><div class="card"><h2>Cellule.ai</h2>
<p class="sub">Pool Administration</p>
<div id="g_id_onload" data-client_id="106098942094-u10np9r0n03pg0g0370m0su0tgcjede0.apps.googleusercontent.com" data-callback="onGoogleLogin" data-auto_prompt="false"></div>
<div class="g_id_signin" data-type="standard" data-size="large" data-theme="filled_black" data-text="sign_in_with" data-shape="rectangular" data-logo_alignment="left" data-width="380"></div>
<div class="sep">— or —</div>
<input type="email" id="em" placeholder="Email">
<input type="password" id="pw" placeholder="Password" onkeydown="if(event.key==='Enter')loginPw()">
<button onclick="loginPw()">Sign In</button>
<div class="err" id="err"></div>
<div class="logo">Cellule.ai</div>
<script>
function onGoogleLogin(response) {
    fetch('/v1/auth/google', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({credential: response.credential})
    }).then(r=>r.json()).then(d=>{
        if(d.session_id) {
            document.cookie='session_id='+d.session_id+';path=/;max-age=86400;SameSite=Lax';
            location.reload();
        }
        else { document.getElementById('err').style.display='block'; document.getElementById('err').textContent=d.error||'Not authorized'; }
    });
}
async function loginPw() {
    const r = await fetch('/admin/login', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({email:document.getElementById('em').value, password:document.getElementById('pw').value})});
    if(r.ok) location.reload();
    else { document.getElementById('err').style.display='block'; document.getElementById('err').textContent='Invalid email or password'; }
}
</script></div></body></html>"""


log = logging.getLogger("iamine.pool")


# --- Lazy imports pour eviter les imports circulaires ---

def _pool():
    from iamine.pool import pool
    return pool


def _accounts():
    from iamine.pool import _accounts
    return _accounts


def _get_session_account(session_id: str):
    from iamine.pool import _get_session_account
    return _get_session_account(session_id)


def _static_dir() -> Path:
    return Path(__file__).parent.parent / "static"


# --- Helper admin auth ---

async def _check_admin(request: Request) -> str | None:
    """Verifie si l'utilisateur est admin. Retourne l'email ou None."""
    pool = _pool()
    accounts = _accounts()
    # 1) Cookie session_id -> email -> check admin_users
    session_id = request.cookies.get("session_id", "")
    if session_id:
        account_id = _get_session_account(session_id)
        if account_id and account_id in accounts:
            email = accounts[account_id].get("email", "")
            if email:
                try:
                    async with pool.store.pool.acquire() as conn:
                        row = await conn.fetchrow("SELECT email FROM admin_users WHERE email=$1", email)
                        if row:
                            return email
                except Exception:
                    pass
    # 2) Fallback : token admin (pour API/curl)
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    token = request.cookies.get("admin_token") or request.query_params.get("token", "")
    if token == admin_pass:
        return "admin"
    return None


# ─── GET /v1/admin/models ────────────────────────────────────────────────────

@router.get("/v1/admin/models")
async def admin_models():
    """Liste tous les modeles avec leur statut de deblocage."""
    from iamine.models import get_unlocked_models, recommend_pool_model

    pool = _pool()
    # Calculer la puissance du pool
    workers_data = []
    max_ram = 0
    for w in pool.workers.values():
        ram = w.info.get("ram_available_gb", w.info.get("ram_total_gb", 4))
        max_ram = max(max_ram, ram)
        workers_data.append({
            "worker_id": w.worker_id,
            "ram_gb": ram,
            "cpu_threads": w.info.get("cpu_threads", 4),
            "bench_tps": w.info.get("bench_tps"),
        })

    analysis = recommend_pool_model(workers_data)
    total_tps = analysis.get("pool_capacity_tps", 0)

    models = get_unlocked_models(total_tps, max_ram)

    active = sum(1 for m in models if m["status"] == "active")
    unlocked = sum(1 for m in models if m["status"] == "unlocked")
    locked = sum(1 for m in models if m["status"] == "locked")

    return {
        "pool_tps": total_tps,
        "max_worker_ram_gb": max_ram,
        "models": models,
        "summary": {
            "active": active,
            "unlocked": unlocked,
            "locked": locked,
            "total": len(models),
        },
    }


# ─── POST /admin/login ──────────────────────────────────────────────────────

@router.post("/admin/login")
async def admin_login(request: Request):
    """Login admin par email/password."""
    pool = _pool()
    data = await request.json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    try:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT email FROM admin_users WHERE email=$1 AND password_hash=$2",
                email, password)
            if row:
                from fastapi.responses import JSONResponse as JR
                resp = JR({"ok": True, "email": email})
                resp.set_cookie("admin_token", os.environ.get("ADMIN_PASSWORD"),
                                httponly=True, max_age=86400)
                return resp
    except Exception:
        pass
    return JSONResponse({"error": "Invalid credentials"}, status_code=401)


# --- POST /admin/setup --- first-time admin creation ---

@router.post("/admin/setup")
async def admin_setup(request: Request):
    """First-time setup: create admin account. Only works if no admin exists."""
    pool = _pool()
    try:
        async with pool.store.pool.acquire() as conn:
            count = await conn.fetchval("SELECT count(*) FROM admin_users")
            if count > 0:
                return JSONResponse({"error": "Admin already exists"}, status_code=403)
            data = await request.json()
            email = data.get("email", "").strip().lower()
            password = data.get("password", "")
            if not email or not password:
                return JSONResponse({"error": "Email and password required"}, status_code=400)
            if len(password) < 6:
                return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)
            await conn.execute(
                "INSERT INTO admin_users (email, password_hash) VALUES ($1, $2)",
                email, password)
            from fastapi.responses import JSONResponse as JR
            resp = JR({"ok": True, "email": email})
            resp.set_cookie("admin_token", os.environ.get("ADMIN_PASSWORD", password),
                            httponly=True, max_age=86400)
            return resp
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── GET /admin ──────────────────────────────────────────────────────────────

@router.get("/admin")
async def admin_page(request: Request):
    from fastapi.responses import FileResponse, HTMLResponse
    admin_email = await _check_admin(request)
    # Check if first-time setup needed (no admin exists)
    try:
        async with _pool().store.pool.acquire() as conn:
            admin_count = await conn.fetchval("SELECT count(*) FROM admin_users")
        if admin_count == 0:
            return HTMLResponse(SETUP_PAGE_HTML)
    except Exception:
        pass
    if not admin_email:
        return HTMLResponse(LOGIN_PAGE_HTML)
    static_dir = _static_dir()
    admin_file = static_dir / "admin.html"
    if admin_file.exists():
        from fastapi.responses import FileResponse as FR
        resp = FR(str(admin_file))
        resp.set_cookie("admin_token", os.environ.get("ADMIN_PASSWORD"),
                        httponly=False, max_age=86400, samesite="lax")
        return resp
    return {"error": "Admin page not found"}


# ─── GET /admin/models ───────────────────────────────────────────────────────

@router.get("/admin/models")
async def admin_models_dashboard():
    """Dashboard admin complet — tableau workers, actions, stats."""
    from fastapi.responses import HTMLResponse
    from iamine.admin import render_admin_dashboard
    from iamine.models import MODEL_REGISTRY, MODEL_FAMILIES, get_active_family
    html = render_admin_dashboard(_pool(), MODEL_REGISTRY,
        active_family=get_active_family(),
        available_families=list(MODEL_FAMILIES.keys()))
    return HTMLResponse(html)


# ─── GET /admin/api/stats ────────────────────────────────────────────────────

@router.get("/admin/api/stats")
async def admin_stats():
    """Stats JSON du pool pour refresh AJAX."""
    pool = _pool()
    total_tps = sum(
        (w.info.get("real_tps") or w.info.get("bench_tps") or 0)
        for w in pool.workers.values()
    )
    return {
        "workers_online": len(pool.workers),
        "total_tps": round(total_tps, 1),
        "pool_load": pool.pool_load,
        "compaction_budget": pool.compaction_budget,
        "workers": [
            {
                "id": w.worker_id,
                "model": w.info.get("model_path", "?").split("/")[-1],
                "real_tps": round(w.info.get("real_tps", 0) or 0, 1),
                "bench_tps": round(w.info.get("bench_tps", 0) or 0, 1),
                "jobs_ok": w.info.get("total_jobs", 0) or w.jobs_done,
                "jobs_failed": w.info.get("jobs_failed", 0) or 0,
                "busy": w.busy,
                "version": w.info.get("version", "?"),
                "outdated": pool._is_outdated(w),
                "unknown_model": pool._is_unknown_model(w),
            }
            for w in pool.workers.values()
        ],
    }


# ─── POST /admin/api/assign ─────────────────────────────────────────────────

@router.post("/admin/api/assign")
async def admin_assign(request: Request):
    """Assigne un modele specifique a un worker."""
    from iamine.models import REGISTRY_BY_ID
    pool = _pool()
    data = await request.json()
    worker_id = data.get("worker_id", "")
    model_id = data.get("model_id", "")

    worker = pool.workers.get(worker_id)
    if not worker:
        return JSONResponse({"error": f"Worker {worker_id} not connected"}, status_code=404)

    tier = REGISTRY_BY_ID.get(model_id)
    if not tier:
        return JSONResponse({"error": f"Model {model_id} not found"}, status_code=404)

    has_gpu = worker.info.get("has_gpu", False)
    ctx = tier.ctx_default
    gpu_layers = -1 if has_gpu else 0

    payload = {
        "type": "command",
        "cmd": "update_model",
        "model_url": f"http://dl.iamine.org/v1/models/download/{tier.hf_file}",
        "model_path": f"models/{tier.hf_file}",
        "ctx_size": ctx,
        "gpu_layers": gpu_layers,
        "threads": min(worker.info.get("cpu_threads", 4), 16),
    }
    await worker.ws.send_json(payload)

    try:
        await pool.store.update_worker_assignment(
            worker_id, tier.id, f"models/{tier.hf_file}", ctx, gpu_layers)
    except Exception:
        pass

    return {"ok": True, "worker": worker_id, "model": tier.name}


# ─── GET /admin/api/assignments ──────────────────────────────────────────────

@router.get("/admin/api/assignments")
async def admin_assignments():
    """JSON de toutes les assignations DB pour tous les workers connectes."""
    from iamine.models import REGISTRY_BY_ID
    pool = _pool()
    results = []
    for wid in pool.workers:
        try:
            assign = await pool.store.get_worker_assignment(wid)
            if assign:
                tier = REGISTRY_BY_ID.get(assign["model_id"])
                results.append({
                    "worker_id": wid,
                    "model_id": assign["model_id"],
                    "model_name": tier.name if tier else assign["model_id"],
                    "model_path": assign["model_path"],
                    "ctx_size": assign["ctx_size"],
                    "gpu_layers": assign["gpu_layers"],
                })
            else:
                results.append({"worker_id": wid, "model_id": None})
        except Exception:
            results.append({"worker_id": wid, "model_id": None, "error": "db_error"})
    return {"assignments": results}


# ─── GET /admin/api/hardware-db ──────────────────────────────────────────────

@router.get("/admin/api/hardware-db")
async def admin_hardware_db():
    """Base de benchmarks hardware (hashrate style XMRig)."""
    pool = _pool()
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT cpu_model, gpu_model, ram_gb, model_id, measured_tps, sample_count, last_updated
                FROM hardware_benchmarks ORDER BY cpu_model, measured_tps DESC
            """)
            return {"benchmarks": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        return {"error": str(e), "benchmarks": []}


# ─── POST /admin/api/set-ctx ────────────────────────────────────────────────

@router.post("/admin/api/set-ctx")
async def admin_set_ctx(request: Request):
    """Change le contexte d'un worker. Persiste en DB + envoie au worker."""
    pool = _pool()
    data = await request.json()
    worker_id = data.get("worker_id", "")
    ctx_size = data.get("ctx_size", 0)

    if not worker_id or ctx_size < 512:
        return JSONResponse({"error": "worker_id and ctx_size (>=512) required"}, status_code=400)

    worker = pool.workers.get(worker_id)

    # Persister en DB
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET assigned_ctx_size=$2 WHERE worker_id=$1",
                worker_id, ctx_size)
    except Exception as e:
        log.warning(f"Failed to persist ctx for {worker_id}: {e}")

    # Mettre a jour in-memory
    if worker:
        worker.info["ctx_size"] = ctx_size
        # Envoyer au worker pour restart avec le nouveau ctx
        try:
            await worker.ws.send_json({
                "type": "command", "cmd": "set_ctx", "ctx_size": ctx_size
            })
        except Exception:
            pass

    return {"ok": True, "worker": worker_id, "ctx_size": ctx_size}


# ─── GET /admin/api/families ─────────────────────────────────────────────────

@router.get("/admin/api/families")
async def admin_families():
    """Liste les familles disponibles et la famille active."""
    from iamine.models import MODEL_FAMILIES, get_active_family
    return {
        "active": get_active_family(),
        "available": {
            k: {"models": len(v), "sizes": [m.params for m in v]}
            for k, v in MODEL_FAMILIES.items()
        },
    }


# ─── POST /admin/api/set-family ─────────────────────────────────────────────

@router.post("/admin/api/set-family")
async def admin_set_family(request: Request):
    """Change la famille de modeles active et migre tous les workers."""
    from iamine.models import MODEL_FAMILIES, set_active_family, get_active_family, recommend_model_for_worker
    pool = _pool()
    data = await request.json()
    family = data.get("family", "")

    if family not in MODEL_FAMILIES:
        return JSONResponse({"error": f"Unknown family '{family}'. Available: {list(MODEL_FAMILIES.keys())}"}, status_code=400)

    old_family = get_active_family()
    if family == old_family:
        return {"ok": True, "message": f"Already on {family}", "migrated": 0}

    # Changer la famille active en memoire
    set_active_family(family)

    # Persister en DB
    try:
        await pool.store.set_config("active_family", family)
    except Exception as e:
        log.warning(f"Failed to persist active_family: {e}")

    # Migrer tous les workers vers la nouvelle famille
    results = []
    for wid, w in pool.workers.items():
        hostname = w.info.get("hostname", "")
        if hostname == pool._pool_hostname:
            continue

        ram = w.info.get("ram_total_gb", 4)
        threads = w.info.get("cpu_threads", 4)
        has_gpu = w.info.get("has_gpu", False)
        gpu_vram = w.info.get("gpu_vram_gb", 0)

        rec, ctx = recommend_model_for_worker(ram, threads, has_gpu=has_gpu, gpu_vram_gb=gpu_vram)
        gpu_layers = -1 if has_gpu else 0

        payload = {
            "type": "command",
            "cmd": "update_model",
            "model_url": f"http://dl.iamine.org/v1/models/download/{rec.hf_file}",
            "model_path": f"models/{rec.hf_file}",
            "ctx_size": ctx,
            "gpu_layers": gpu_layers,
            "threads": min(threads, 16),
        }
        try:
            await w.ws.send_json(payload)
            await pool.store.update_worker_assignment(
                wid, rec.id, f"models/{rec.hf_file}", ctx, gpu_layers)
            results.append({"worker": wid, "model": rec.name, "sent": True})
        except Exception as e:
            results.append({"worker": wid, "error": str(e), "sent": False})

    migrated = len([r for r in results if r.get("sent")])
    log.info(f"Family switch: {old_family} -> {family}, {migrated} workers migrated")
    return {"ok": True, "old_family": old_family, "new_family": family, "migrated": migrated, "details": results}


# ─── GET /v1/admin/tasks ─────────────────────────────────────────────────────

@router.get("/v1/admin/tasks")
async def admin_tasks():
    """Historique des taches distribuees entre workers."""
    pool = _pool()
    # En RAM, on maintient un log des dernieres taches
    return {"tasks": list(reversed(pool._task_log[-50:]))}


# ─── POST /admin/api/worker-cmd ─────────────────────────────────────────────

@router.post("/admin/api/worker-cmd")
async def admin_worker_cmd(request: Request):
    """Envoie une commande a un worker via WebSocket."""
    from iamine.models import REGISTRY_BY_ID
    pool = _pool()
    data = await request.json()
    worker_id = data.get("worker_id", "")
    cmd = data.get("cmd", "")

    worker = pool.workers.get(worker_id)
    if not worker:
        return JSONResponse({"error": f"Worker {worker_id} not connected"}, status_code=404)

    payload = {"type": "command", "cmd": cmd}
    if cmd == "update_model":
        model_url = data.get("model_url", "")
        model_path = data.get("model_path", "")
        ctx_size = data.get("ctx_size", 4096)
        gpu_layers = data.get("gpu_layers", 0)
        payload["model_url"] = model_url
        payload["model_path"] = model_path
        payload["ctx_size"] = ctx_size
        payload["gpu_layers"] = gpu_layers
        payload["threads"] = data.get("threads", 4)
        # Persister l'assignation en DB pour survie au restart
        model_id = data.get("model_id", "")
        if not model_id:
            # Deduire model_id depuis le fichier GGUF
            fname = model_path.split("/")[-1] if model_path else model_url.split("/")[-1]
            for mid, tier in REGISTRY_BY_ID.items():
                if tier.hf_file == fname:
                    model_id = mid
                    break
        if model_id:
            try:
                await pool.store.update_worker_assignment(
                    worker_id, model_id, model_path, ctx_size, gpu_layers)
            except Exception as e:
                log.debug(f"Failed to persist assignment for {worker_id}: {e}")

    await worker.ws.send_json(payload)
    log.info(f"Command '{cmd}' sent to {worker_id}")
    return {"ok": True, "sent_to": worker_id, "cmd": cmd}


# ─── POST /admin/api/pool-managed ───────────────────────────────────────────

@router.post("/admin/api/pool-managed")
async def admin_pool_managed(request: Request):
    """Active/desactive la gestion automatique du pool pour un worker.
    Si pool_managed=false, le pool n'enverra JAMAIS update_model a ce worker."""
    pool = _pool()
    data = await request.json()
    worker_id = data.get("worker_id", "")
    managed = data.get("pool_managed", True)
    if not worker_id:
        return JSONResponse({"error": "worker_id required"}, status_code=400)
    try:
        await pool.store.set_pool_managed(worker_id, managed)
        log.info(f"pool_managed={managed} for {worker_id}")
        return {"ok": True, "worker_id": worker_id, "pool_managed": managed}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── POST /admin/api/migrate-all ────────────────────────────────────────────

@router.post("/admin/api/migrate-all")
async def admin_migrate_all():
    """Migre tous les workers encore sur Qwen 2.5 vers Qwen 3.5."""
    from iamine.models import MODEL_REGISTRY, recommend_model_for_worker
    pool = _pool()
    results = []
    for wid, w in pool.workers.items():
        model_path = w.info.get("model_path", "")
        # Skip si deja sur Qwen 3.5
        if "Qwen3.5" in model_path or "Qwen_Qwen3.5" in model_path:
            continue
        # Skip molecule/pool workers
        if "molecule" in wid.lower() or "pool" in wid.lower():
            continue

        rec, ctx = recommend_model_for_worker(
            ram_available_gb=w.info.get("ram_total_gb", 4),
            cpu_threads=w.info.get("cpu_threads", 4),
            has_gpu=w.info.get("has_gpu", False),
            gpu_vram_gb=w.info.get("gpu_vram_gb", 0),
        )

        payload = {
            "type": "command",
            "cmd": "update_model",
            "model_url": f"http://dl.iamine.org/v1/models/download/{rec.hf_file}",
            "model_path": f"models/{rec.hf_file}",
            "ctx_size": ctx,
            "gpu_layers": -1 if w.info.get("has_gpu") else 0,
            "threads": min(w.info.get("cpu_threads", 4), 16),
        }
        try:
            await w.ws.send_json(payload)
            # Persister l'assignation en DB
            try:
                await pool.store.update_worker_assignment(
                    wid, rec.id, f"models/{rec.hf_file}", ctx,
                    -1 if w.info.get("has_gpu") else 0)
            except Exception:
                pass
            results.append({"worker": wid, "new_model": rec.name, "sent": True})
            log.info(f"Migration sent to {wid}: {rec.name}")
        except Exception as e:
            results.append({"worker": wid, "error": str(e), "sent": False})

    return {"migrated": len([r for r in results if r.get("sent")]), "details": results}


# === API RED : commandes admin pour l'agent LLM ===

# ─── POST /admin/api/commands ────────────────────────────────────────────────

@router.post("/admin/api/commands")
async def admin_create_command(request: Request):
    """Cree une commande admin (RED ou humain)."""
    pool = _pool()
    data = await request.json()
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO admin_commands (issued_by, target_worker, command_type, command_payload)
            VALUES ($1, $2, $3, $4::jsonb) RETURNING command_id, issued_at
        """, data.get("issued_by", "unknown"), data.get("target_worker", ""),
            data.get("command_type", ""), json.dumps(data.get("payload", data.get("command_payload", {}))))
        return {"command_id": row["command_id"], "issued_at": str(row["issued_at"])}


# ─── GET /admin/api/commands ─────────────────────────────────────────────────

@router.get("/admin/api/commands")
async def admin_list_commands(limit: int = 50, status: str = "", result_status: str = ""):
    """Liste les commandes admin recentes. Filtres : status, result_status."""
    pool = _pool()
    async with pool.store.pool.acquire() as conn:
        if result_status:
            rows = await conn.fetch(
                "SELECT * FROM admin_commands WHERE result_status=$1 ORDER BY issued_at DESC LIMIT $2",
                result_status, limit)
        elif status:
            rows = await conn.fetch(
                "SELECT * FROM admin_commands WHERE status=$1 ORDER BY issued_at DESC LIMIT $2",
                status, limit)
        else:
            rows = await conn.fetch(
                "SELECT * FROM admin_commands ORDER BY issued_at DESC LIMIT $1", limit)
        return {"commands": [dict(r) for r in rows]}


# ─── GET /admin/api/lessons ──────────────────────────────────────────────────

@router.get("/admin/api/lessons")
async def admin_lessons(command_type: str = "", limit: int = 20):
    """Retourne les lecons apprises par RED (memoire d'experience)."""
    pool = _pool()
    async with pool.store.pool.acquire() as conn:
        if command_type:
            rows = await conn.fetch("""
                SELECT command_type, target_worker, lesson_learned, error_message,
                       result_status, context, completed_at
                FROM admin_commands
                WHERE lesson_learned != '' AND command_type = $1
                ORDER BY completed_at DESC LIMIT $2
            """, command_type, limit)
        else:
            rows = await conn.fetch("""
                SELECT command_type, target_worker, lesson_learned, error_message,
                       result_status, context, completed_at
                FROM admin_commands
                WHERE lesson_learned != ''
                ORDER BY completed_at DESC LIMIT $1
            """, limit)
        return {"lessons": [dict(r) for r in rows]}


# ─── POST /admin/api/commands/{command_id}/complete ──────────────────────────

@router.post("/admin/api/commands/{command_id}/complete")
async def admin_complete_command(command_id: int, request: Request):
    """Marque une commande comme terminee (RED rapporte le resultat)."""
    pool = _pool()
    data = await request.json()
    async with pool.store.pool.acquire() as conn:
        await conn.execute("""
            UPDATE admin_commands SET status=$2, result_text=$3, error_text=$4,
                completed_at=NOW(), duration_ms=$5,
                result_status=COALESCE(NULLIF($6,''), result_status),
                error_message=COALESCE(NULLIF($7,''), error_message),
                lesson_learned=COALESCE(NULLIF($8,''), lesson_learned),
                context=COALESCE(NULLIF($9::text,'{}')::jsonb, context)
            WHERE command_id=$1
        """, command_id, data.get("status", "success"),
            data.get("result", ""), data.get("error", ""),
            data.get("duration_ms", 0),
            data.get("result_status", ""),
            data.get("error_message", ""),
            data.get("lesson_learned", ""),
            json.dumps(data.get("context", {})))
    return {"ok": True}


# ─── GET /admin/api/admins ───────────────────────────────────────────────────

@router.get("/admin/api/admins")
async def admin_list_admins(request: Request):
    """Liste les admins."""
    if not await _check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pool = _pool()
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch("SELECT email, added_at, added_by FROM admin_users ORDER BY added_at")
        return {"admins": [dict(r) for r in rows]}


# ─── POST /admin/api/admins ──────────────────────────────────────────────────

@router.post("/admin/api/admins")
async def admin_add_admin(request: Request):
    """Ajouter un admin."""
    admin_email = await _check_admin(request)
    if not admin_email:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pool = _pool()
    data = await request.json()
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "invalid email"}, status_code=400)
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO admin_users (email, added_by) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            email, admin_email)
    log.info(f"Admin added: {email} by {admin_email}")
    return {"ok": True, "email": email}


# ─── DELETE /admin/api/admins/{email} ────────────────────────────────────────

@router.delete("/admin/api/admins/{email}")
async def admin_remove_admin(email: str, request: Request):
    """Retirer un admin."""
    admin_email = await _check_admin(request)
    if not admin_email:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if email == "david.mourgues@gmail.com":
        return JSONResponse({"error": "Cannot remove root admin"}, status_code=403)
    pool = _pool()
    async with pool.store.pool.acquire() as conn:
        await conn.execute("DELETE FROM admin_users WHERE email=$1", email)
    return {"ok": True, "removed": email}


# ─── GET /admin/api/config ───────────────────────────────────────────────────

@router.get("/admin/api/config")
async def admin_get_config(request: Request):
    """Retourne la config admin (SMTP, etc) depuis pool_config DB."""
    if not await _check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pool = _pool()
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM pool_config WHERE key LIKE 'smtp_%' OR key LIKE 'alert_%' OR key LIKE 'red_%' OR key LIKE 'checker_%' OR key = 'system_prompt'")
            return {"config": {r["key"]: r["value"] for r in rows}}
    except Exception:
        return {"config": {}}


# ─── POST /admin/api/config ──────────────────────────────────────────────────

@router.post("/admin/api/config")
async def admin_set_config(request: Request):
    """Met a jour la config admin en DB."""
    if not await _check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pool = _pool()
    data = await request.json()
    try:
        async with pool.store.pool.acquire() as conn:
            for key, value in data.items():
                await conn.execute("""
                    INSERT INTO pool_config (key, value) VALUES ($1, $2)
                    ON CONFLICT (key) DO UPDATE SET value = $2
                """, key, str(value))
                # Appliquer en env pour usage immediat
                env_key = key.upper()
                os.environ[env_key] = str(value)
                # Appliquer system_prompt en RAM immediatement
                if key == "system_prompt":
                    pool.SYSTEM_PROMPT = str(value)
        return {"ok": True, "updated": list(data.keys())}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



# ─── GET /admin/api/checker ─────────────────────────────────────────────────────────────────

@router.get("/admin/api/checker")
async def admin_checker_status(request: Request):
    """Retourne le status du checker ladder : config + scores par worker."""
    if not await _check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pool = _pool()
    # Config checker (valeurs live en RAM)
    config = {
        "checker_enabled": pool.CHECKER_ENABLED,
        "checker_tps_threshold": pool.CHECKER_TPS_THRESHOLD,
        "checker_timeout": pool.CHECKER_TIMEOUT,
        "checker_max_tokens": pool.CHECKER_MAX_TOKENS,
        "checker_fail_max": pool.CHECKER_FAIL_MAX,
        "checker_score_decay": pool.CHECKER_SCORE_DECAY,
        "checker_score_recovery": pool.CHECKER_SCORE_RECOVERY,
        "checker_min_score": pool.CHECKER_MIN_SCORE,
        "checker_sample_rate": pool.CHECKER_SAMPLE_RATE,
    }
    # Scores par worker
    workers = []
    for w in pool.workers.values():
        bench = w.info.get("bench_tps") or w.info.get("real_tps") or 0
        workers.append({
            "worker_id": w.worker_id,
            "model_path": w.info.get("model_path", ""),
            "bench_tps": round(bench, 1),
            "checked": bench < pool.CHECKER_TPS_THRESHOLD,
            "checker_score": round(w.info.get("checker_score", 1.0), 3),
            "checker_fails": w.info.get("checker_fails", 0),
            "checker_total": w.info.get("checker_total", 0),
            "checker_passed": w.info.get("checker_passed", 0),
        })
    return {"config": config, "workers": workers}


@router.post("/admin/api/checker")
async def admin_set_checker(request: Request):
    """Met a jour la config checker en DB + RAM."""
    if not await _check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pool = _pool()
    data = await request.json()
    _keys = {
        "checker_enabled": ("CHECKER_ENABLED", lambda v: str(v).lower() == "true", "checker_enabled"),
        "checker_tps_threshold": ("CHECKER_TPS_THRESHOLD", float, "checker_tps_threshold"),
        "checker_timeout": ("CHECKER_TIMEOUT", int, "checker_timeout"),
        "checker_max_tokens": ("CHECKER_MAX_TOKENS", int, "checker_max_tokens"),
        "checker_fail_max": ("CHECKER_FAIL_MAX", int, "checker_fail_max"),
        "checker_score_decay": ("CHECKER_SCORE_DECAY", float, "checker_score_decay"),
        "checker_score_recovery": ("CHECKER_SCORE_RECOVERY", float, "checker_score_recovery"),
        "checker_min_score": ("CHECKER_MIN_SCORE", float, "checker_min_score"),
        "checker_sample_rate": ("CHECKER_SAMPLE_RATE", int, "checker_sample_rate"),
    }
    updated = []
    try:
        async with pool.store.pool.acquire() as conn:
            for key, value in data.items():
                if key in _keys:
                    attr, cast, db_key = _keys[key]
                    setattr(pool, attr, cast(value))
                    await conn.execute("""
                        INSERT INTO pool_config (key, value) VALUES ($1, $2)
                        ON CONFLICT (key) DO UPDATE SET value = $2
                    """, db_key, str(value))
                    updated.append(key)
        return {"ok": True, "updated": updated}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── POST /admin/api/alert ──────────────────────────────────────────────────

@router.post("/admin/api/alert")
async def admin_alert(request: Request):
    """RED envoie une alerte. Loggee en DB + email si SMTP configure."""
    from iamine import __version__
    pool = _pool()
    data = await request.json()
    subject = data.get("subject", "IAMINE Alert")
    body = data.get("body", "")
    level = data.get("level", "info")
    source = data.get("source", "unknown")

    # Toujours logger l'alerte en DB (admin_commands)
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO admin_commands (issued_by, command_type, command_payload, status, result_text)
                VALUES ($1, 'alert', $2, 'completed', $3)
            """, source, json.dumps({"subject": subject, "level": level}),
                f"[{level.upper()}] {body}")
    except Exception:
        pass
    log.info(f"Alert [{level}] from {source}: {subject}")

    # Email si SMTP configure (DB pool_config d'abord, puis env vars en fallback)
    smtp_cfg = {}
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM pool_config WHERE key LIKE 'smtp_%' OR key = 'alert_email'")
            smtp_cfg = {r["key"]: r["value"] for r in rows}
    except Exception:
        pass

    smtp_host = smtp_cfg.get("smtp_host") or os.environ.get("SMTP_HOST", "")
    if smtp_host:
        import smtplib
        from email.mime.text import MIMEText
        smtp_port = int(smtp_cfg.get("smtp_port") or os.environ.get("SMTP_PORT", "587"))
        smtp_user = smtp_cfg.get("smtp_user") or os.environ.get("SMTP_USER", "")
        smtp_pass = smtp_cfg.get("smtp_pass") or os.environ.get("SMTP_PASS", "")
        smtp_from = smtp_cfg.get("smtp_from") or os.environ.get("SMTP_FROM", "contact@iamine.org")
        to_email = smtp_cfg.get("alert_email") or os.environ.get("ALERT_EMAIL", "david.mourgues@gmail.com")

        msg = MIMEText(f"[{level.upper()}] {source}\n\n{body}\n\n— IAMINE Pool v{__version__}")
        msg["Subject"] = f"[IAMINE] {subject}"
        msg["From"] = smtp_from
        msg["To"] = to_email
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
                if smtp_user:
                    s.starttls()
                    s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_from, [to_email], msg.as_string())
            return {"sent": True, "to": to_email, "logged": True}
        except Exception as e:
            log.warning(f"Alert email failed: {e}")
            return {"sent": False, "error": str(e), "logged": True}

    return {"sent": False, "logged": True, "note": "No SMTP configured, alert logged in DB"}


# ─── POST /admin/api/inference-report ────────────────────────────────────────

@router.post("/admin/api/inference-report")
async def admin_inference_report(request: Request):
    """Enregistre un rapport d'inference (RED evalue un worker)."""
    pool = _pool()
    data = await request.json()
    async with pool.store.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO inference_reports (worker_id, model_id, model_path,
                prompt_tokens, completion_tokens, total_tokens,
                prompt_eval_ms, completion_ms, tokens_per_sec,
                prompt_text, response_text, response_quality,
                compared_to_worker, compared_quality_delta)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        """, data.get("worker_id"), data.get("model_id"), data.get("model_path"),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("total_tokens", 0), data.get("prompt_eval_ms", 0),
            data.get("completion_ms", 0), data.get("tokens_per_sec", 0),
            data.get("prompt_text", "")[:500], data.get("response_text", "")[:1000],
            data.get("response_quality", 0),
            data.get("compared_to_worker"), data.get("compared_quality_delta"))
    return {"ok": True}


# ─── GET /admin/api/capabilities ─────────────────────────────────────────────

@router.get("/admin/api/capabilities")
async def admin_capabilities():
    """Liste les capacites de toutes les machines connues."""
    pool = _pool()
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM worker_capabilities ORDER BY worker_id")
        return {"workers": [dict(r) for r in rows]}


# === RED MEMORY : sauvegarde et rollback de RED.md ===

# ─── POST /admin/api/red/memory-save ─────────────────────────────────────────

@router.post("/admin/api/red/memory-save")
async def red_memory_save(request: Request):
    """Sauvegarde le contenu de RED.md en DB pour versionning."""
    pool = _pool()
    data = await request.json()
    content = data.get("content", "")
    reason = data.get("reason", "auto-save")
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO admin_commands (issued_by, target_worker, command_type, command_payload, status)
            VALUES ('RED', 'RED', 'red.memory_snapshot', $1::jsonb, 'success')
            RETURNING command_id, issued_at
        """, json.dumps({"content": content, "reason": reason}))
        return {"snapshot_id": row["command_id"], "issued_at": str(row["issued_at"])}


# ─── GET /admin/api/red/memory-history ───────────────────────────────────────

@router.get("/admin/api/red/memory-history")
async def red_memory_history(limit: int = 10):
    """Liste les snapshots de RED.md pour rollback."""
    pool = _pool()
    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT command_id, issued_at, command_payload->>'reason' as reason,
                   length(command_payload->>'content') as size_chars
            FROM admin_commands
            WHERE command_type='red.memory_snapshot'
            ORDER BY issued_at DESC LIMIT $1
        """, limit)
        return {"snapshots": [dict(r) for r in rows]}


# ─── GET /admin/api/red/memory-restore/{snapshot_id} ─────────────────────────

@router.get("/admin/api/red/memory-restore/{snapshot_id}")
async def red_memory_restore(snapshot_id: int):
    """Recupere le contenu d'un snapshot pour rollback."""
    pool = _pool()
    async with pool.store.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT command_payload->>'content' as content, issued_at
            FROM admin_commands
            WHERE command_id=$1 AND command_type='red.memory_snapshot'
        """, snapshot_id)
        if row:
            return {"content": row["content"], "issued_at": str(row["issued_at"])}
        return JSONResponse({"error": "Snapshot not found"}, status_code=404)


# ─── DELETE /admin/api/worker/{worker_id} ─────────────────────────────────────

@router.delete("/admin/api/worker/{worker_id}")
async def delete_worker(worker_id: str, request: Request):
    """Supprime un worker du pool et de la DB."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    pool = _pool()

    # 1. Envoyer shutdown si connecte
    if worker_id in pool.workers:
        try:
            await pool.workers[worker_id].ws.send_json({"type": "command", "cmd": "shutdown"})
        except Exception:
            pass
        pool.remove_worker(worker_id)

    # 2. Supprimer de la DB
    deleted = False
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute("DELETE FROM jobs WHERE worker_id=$1", worker_id)
            result = await conn.execute("DELETE FROM workers WHERE worker_id=$1", worker_id)
            deleted = "DELETE 1" in result
    except Exception:
        pass

    if deleted:
        log.info(f"Worker {worker_id} supprime par {admin}")
        return {"ok": True, "deleted": worker_id}
    else:
        # Pas en DB mais peut-etre juste connecte
        return {"ok": True, "deleted": worker_id, "note": "pas en DB"}


# ─── GET /admin/api/queue ──────────────────────────────────────────────────

@router.get("/admin/api/queue")
async def admin_queue():
    """Liste les pending_jobs (file d attente)."""
    from iamine.pool import pool
    try:
        stats = await pool.store.get_queue_stats()
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT job_id, conv_id, api_token, status, requested_model,
                       max_tokens, created_at, started_at, completed_at,
                       worker_id, error
                FROM pending_jobs
                ORDER BY created_at DESC
                LIMIT 50
            """)
            jobs = []
            for r in rows:
                jobs.append({
                    "job_id": r["job_id"],
                    "conv_id": r["conv_id"] or "",
                    "token": (r["api_token"] or "")[:12] + "...",
                    "status": r["status"],
                    "model": r["requested_model"] or "auto",
                    "max_tokens": r["max_tokens"],
                    "created": r["created_at"].isoformat() if r["created_at"] else "",
                    "started": r["started_at"].isoformat() if r["started_at"] else "",
                    "completed": r["completed_at"].isoformat() if r["completed_at"] else "",
                    "worker_id": r["worker_id"] or "",
                    "error": (r["error"] or "")[:80],
                })
        return {"stats": stats, "jobs": jobs}
    except Exception as e:
        return {"stats": {}, "jobs": [], "error": str(e)}


# ─── GET /admin/api/cleanup/preview ────────────────────────────────────────

@router.get("/admin/api/cleanup/preview")
async def cleanup_preview(request: Request):
    """Liste les workers candidats au nettoyage (< seuil t/s, offline, etc)."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    pool = _pool()
    min_tps = float(request.query_params.get("min_tps", 8.0))
    max_offline_hours = int(request.query_params.get("max_offline_hours", 72))

    candidates = []
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT worker_id, hostname, cpu, real_tps, bench_tps,
                       has_gpu, is_online, last_seen, total_jobs, jobs_failed,
                       assigned_model_path, version, pool_managed
                FROM workers
                ORDER BY real_tps ASC NULLS FIRST
            """)
            now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            for r in rows:
                real_tps = float(r["real_tps"] or 0)
                bench_tps = float(r["bench_tps"] or 0)
                effective_tps = real_tps if real_tps > 0 else bench_tps
                has_gpu = r["has_gpu"] or False
                is_online = r["is_online"] or False
                last_seen = r["last_seen"]
                hours_offline = (now - last_seen).total_seconds() / 3600 if last_seen else 9999

                reasons = []
                if effective_tps > 0 and effective_tps < min_tps and not has_gpu:
                    reasons.append(f"trop lent: {effective_tps:.1f} t/s < {min_tps}")
                if not is_online and hours_offline > max_offline_hours:
                    reasons.append(f"offline depuis {hours_offline:.0f}h")
                if (r["jobs_failed"] or 0) > 0 and (r["total_jobs"] or 0) > 0:
                    fail_rate = (r["jobs_failed"] or 0) / (r["total_jobs"] or 1)
                    if fail_rate > 0.3:
                        reasons.append(f"echecs: {fail_rate:.0%} ({r['jobs_failed']}/{r['total_jobs']})")

                if reasons:
                    candidates.append({
                        "worker_id": r["worker_id"],
                        "hostname": r["hostname"] or "",
                        "cpu": (r["cpu"] or "")[:40],
                        "effective_tps": round(effective_tps, 1),
                        "has_gpu": has_gpu,
                        "is_online": is_online,
                        "last_seen": last_seen.isoformat() if last_seen else "",
                        "total_jobs": r["total_jobs"] or 0,
                        "jobs_failed": r["jobs_failed"] or 0,
                        "model": (r["assigned_model_path"] or "").split("/")[-1][:30],
                        "version": r["version"] or "",
                        "reasons": reasons,
                    })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return {"candidates": candidates, "min_tps": min_tps, "max_offline_hours": max_offline_hours}


@router.post("/admin/api/cleanup")
async def cleanup_workers(request: Request):
    """Supprime les workers specifies de la DB."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    data = await request.json()
    worker_ids = data.get("worker_ids", [])
    if not worker_ids:
        return {"ok": False, "error": "aucun worker specifie"}

    pool = _pool()
    deleted = []
    for wid in worker_ids:
        # Deconnecter si connecte
        if wid in pool.workers:
            try:
                await pool.workers[wid].ws.send_json({"type": "command", "cmd": "shutdown"})
            except Exception:
                pass
            pool.remove_worker(wid)

        # Supprimer de la DB
        try:
            async with pool.store.pool.acquire() as conn:
                await conn.execute("DELETE FROM jobs WHERE worker_id=$1", wid)
                result = await conn.execute("DELETE FROM workers WHERE worker_id=$1", wid)
                if "DELETE 1" in result:
                    deleted.append(wid)
        except Exception:
            pass

    log.info(f"Cleanup par {admin}: {len(deleted)} workers supprimes: {deleted}")
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


# ─── GET /admin/api/blacklist ──────────────────────────────────────────────

@router.get("/admin/api/blacklist")
async def get_blacklist(request: Request):
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    return {"blacklist": sorted(pool._blacklist)}


@router.post("/admin/api/blacklist/add")
async def blacklist_add(request: Request):
    """Bannir un worker du pool."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    data = await request.json()
    worker_id = data.get("worker_id", "").strip()
    if not worker_id:
        return {"ok": False, "error": "worker_id requis"}

    pool = _pool()
    pool._blacklist.add(worker_id)

    # Persister en DB
    import json
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pool_config (key, value) VALUES (blacklist, $1) ON CONFLICT (key) DO UPDATE SET value = $1",
            json.dumps(sorted(pool._blacklist))
        )

    # Deconnecter si connecte
    if worker_id in pool.workers:
        try:
            await pool.workers[worker_id].ws.send_json({"type": "command", "cmd": "shutdown"})
        except Exception:
            pass
        pool.remove_worker(worker_id)

    log.info(f"Worker {worker_id} blackliste par {admin}")
    return {"ok": True, "blacklist": sorted(pool._blacklist)}


@router.post("/admin/api/blacklist/remove")
async def blacklist_remove(request: Request):
    """Debannir un worker."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    data = await request.json()
    worker_id = data.get("worker_id", "").strip()
    if not worker_id:
        return {"ok": False, "error": "worker_id requis"}

    pool = _pool()
    pool._blacklist.discard(worker_id)

    import json
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pool_config (key, value) VALUES (blacklist, $1) ON CONFLICT (key) DO UPDATE SET value = $1",
            json.dumps(sorted(pool._blacklist))
        )

    log.info(f"Worker {worker_id} deblackliste par {admin}")
    return {"ok": True, "blacklist": sorted(pool._blacklist)}



# ─── GET /admin/api/accounts ───────────────────────────────────────────────

@router.get("/admin/api/accounts")
async def admin_accounts(request: Request):
    """Liste tous les comptes."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    from iamine.pool import _accounts
    accounts = []
    for acc_id, acc in _accounts.items():
        accounts.append({
            "account_id": acc_id,
            "email": acc.get("email", ""),
            "pseudo": acc.get("pseudo", ""),
            "display_name": acc.get("display_name", ""),
            "total_credits": round(float(acc.get("total_credits", 0) or 0), 2),
            "total_earned": round(float(acc.get("total_earned", 0) or 0), 2),
            "total_spent": round(float(acc.get("total_spent", 0) or 0), 2),
            "worker_ids": acc.get("worker_ids", []),
            "eth_address": acc.get("eth_address", ""),
            "account_token": acc.get("account_token", "")[:16] + "...",
            "created": acc.get("created", 0),
        })
    accounts.sort(key=lambda a: a["email"])
    return {"accounts": accounts, "total": len(accounts)}


@router.post("/admin/api/accounts/credits")
async def admin_set_credits(request: Request):
    """Modifier les credits d un compte."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    data = await request.json()
    account_id = data.get("account_id", "")
    credits = float(data.get("credits", 0))
    action = data.get("action", "set")  # set, add, subtract

    from iamine.pool import _accounts, _save_accounts, _save_account_to_db
    acc = _accounts.get(account_id)
    if not acc:
        return {"ok": False, "error": "compte introuvable"}

    if action == "add":
        acc["total_credits"] = float(acc.get("total_credits", 0) or 0) + credits
    elif action == "subtract":
        acc["total_credits"] = max(0, float(acc.get("total_credits", 0) or 0) - credits)
    else:
        acc["total_credits"] = credits

    _save_accounts()
    import asyncio
    asyncio.create_task(_save_account_to_db(account_id))

    # Sync credits in api_tokens RAM
    pool = _pool()
    token = acc.get("account_token", "")
    if token in pool.api_tokens:
        pool.api_tokens[token]["credits"] = acc["total_credits"]

    log.info(f"Credits {action}: {acc['email']} = {acc['total_credits']:.2f} (par {admin})")
    return {"ok": True, "credits": round(acc["total_credits"], 2)}


@router.delete("/admin/api/accounts/{account_id}")
async def admin_delete_account(account_id: str, request: Request):
    """Supprimer un compte."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "admin required"}, status_code=401)

    from iamine.pool import _accounts, _save_accounts
    acc = _accounts.pop(account_id, None)
    if not acc:
        return {"ok": False, "error": "compte introuvable"}

    _save_accounts()

    # Supprimer de la DB
    pool = _pool()
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute("DELETE FROM sessions WHERE account_id=$1", account_id)
            await conn.execute("DELETE FROM accounts WHERE account_id=$1", account_id)
    except Exception:
        pass

    # Retirer le token API du pool
    token = acc.get("account_token", "")
    pool.api_tokens.pop(token, None)

    log.info(f"Compte {acc['email']} supprime par {admin}")
    return {"ok": True, "deleted": acc["email"]}


@router.post("/admin/api/push-update")
async def admin_push_update(request: Request):
    """Force push self_update to all outdated workers."""
    admin = await _check_admin(request)
    if not admin:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    pool = _pool()
    from iamine import __version__

    updated = []
    skipped = []
    for w in list(pool.workers.values()):
        wv = w.info.get("version", "0.0.0")
        try:
            local = tuple(int(x) for x in __version__.split("."))
            remote = tuple(int(x) for x in wv.split("."))
            behind = (local[2] - remote[2]) if len(local) >= 3 and len(remote) >= 3 and local[:2] == remote[:2] else 0
        except Exception:
            behind = 0

        if behind >= 1:
            try:
                await w.ws.send_json({"type": "command", "cmd": "self_update"})
                updated.append({"worker_id": w.worker_id, "version": wv, "behind": behind})
                log.info(f"Admin push-update: {w.worker_id} v{wv} -> self_update")
            except Exception as e:
                skipped.append({"worker_id": w.worker_id, "error": str(e)[:100]})
        else:
            skipped.append({"worker_id": w.worker_id, "version": wv, "status": "up-to-date"})

    return {"ok": True, "pool_version": __version__, "pushed": updated, "skipped": skipped}
