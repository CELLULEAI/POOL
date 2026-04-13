"""Auth & account management endpoints — /v1/auth/*, /v1/account/*."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("iamine.pool")

GOOGLE_CLIENT_ID = "106098942094-u10np9r0n03pg0g0370m0su0tgcjede0.apps.googleusercontent.com"


# --- Lazy imports pour eviter les imports circulaires ---

def _pool():
    from iamine.pool import pool
    return pool

def _accounts():
    from iamine.pool import _accounts
    return _accounts

def _sessions():
    from iamine.pool import _sessions
    return _sessions

def _create_session(account_id: str) -> str:
    from iamine.pool import _create_session
    return _create_session(account_id)

def _get_session_account(session_id: str) -> str | None:
    from iamine.pool import _get_session_account
    return _get_session_account(session_id)

def _derive_account_token(email: str) -> str:
    from iamine.pool import _derive_account_token
    return _derive_account_token(email)

def _derive_api_token(worker_id: str) -> str:
    from iamine.pool import _derive_api_token
    return _derive_api_token(worker_id)

def _save_accounts():
    from iamine.pool import _save_accounts
    return _save_accounts()

def _save_account_to_db(acc_id: str):
    from iamine.pool import _save_account_to_db
    asyncio.create_task(_save_account_to_db(acc_id))

def _seed_user_memory(api_token: str, pseudo: str):
    from iamine.pool import _seed_user_memory
    return _seed_user_memory(api_token, pseudo)


# --- Email verification helpers (migration 016) ---
import smtplib as _smtplib
from email.mime.text import MIMEText as _MIMEText

_VERIFICATION_TTL_SEC = 15 * 60  # 15 minutes
_RESEND_COOLDOWN_SEC = 60


def _generate_verification_code() -> str:
    """6-digit code, always 6 chars (100000-999999)."""
    return str(secrets.randbelow(900000) + 100000)


async def _get_smtp_config(pool):
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM pool_config WHERE key LIKE 'smtp_%' OR key = 'alert_email'"
            )
            return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


async def _get_resend_config(pool):
    """Load Resend config from pool_config DB table."""
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM pool_config WHERE key IN ('resend_api_key', 'resend_from')"
            )
            return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


async def _send_via_resend(api_key: str, from_email: str, to_email: str, code: str, pseudo: str) -> tuple[bool, str]:
    """POST to https://api.resend.com/emails. Returns (sent, status_msg)."""
    import httpx as _httpx
    text_body = (
        f"Bonjour {pseudo},\n\n"
        f"Voici votre code d activation pour cellule.ai :\n\n"
        f"    {code}\n\n"
        f"Il expire dans 15 minutes.\n\n"
        f"Si vous n etes pas a l origine de cette inscription, ignorez ce message.\n\n"
        f"-- Cellule.ai"
    )
    html_body = (
        f"<div style=\"font-family:ui-sans-serif,system-ui,sans-serif;max-width:520px;margin:0 auto;padding:2rem;\">"
        f"<h2 style=\"color:#00d4ff;margin:0 0 1rem;\">Cellule.ai</h2>"
        f"<p>Bonjour <strong>{pseudo}</strong>,</p>"
        f"<p>Voici votre code d activation :</p>"
        f"<div style=\"font-size:2rem;letter-spacing:0.6rem;text-align:center;padding:1.2rem;background:#f5f5f7;border-radius:10px;font-family:ui-monospace,monospace;font-weight:600;color:#111;\">{code}</div>"
        f"<p style=\"color:#888;font-size:0.85rem;\">Il expire dans 15 minutes. Si vous n etes pas a l origine de cette inscription, ignorez ce message.</p>"
        f"<p style=\"color:#bbb;font-size:0.75rem;margin-top:2rem;border-top:1px solid #eee;padding-top:1rem;\">Cellule.ai &mdash; Decentralized AI network</p>"
        f"</div>"
    )
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": f"Cellule.ai - Code d activation : {code}",
        "text": text_body,
        "html": html_body,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
            if r.status_code >= 400:
                return False, f"resend {r.status_code}: {r.text[:200]}"
            return True, "sent via resend"
    except Exception as e:
        return False, f"resend exception: {str(e)[:200]}"


async def _send_verification_email(pool, to_email: str, code: str, pseudo: str) -> tuple[bool, str]:
    """Send 6-digit verification code. Priority: Resend API > SMTP fallback."""
    # Try Resend first
    resend_cfg = await _get_resend_config(pool)
    api_key = resend_cfg.get("resend_api_key") or ""
    if api_key:
        from_email = resend_cfg.get("resend_from") or "Cellule.ai <noreply@cellule.ai>"
        sent, status = await _send_via_resend(api_key, from_email, to_email, code, pseudo)
        if sent:
            log.info(f"Verification email sent to {to_email} via Resend")
            return True, "sent via resend"
        log.warning(f"Resend failed ({status}), falling back to SMTP")

    # Fallback: SMTP (legacy, kept for emergency only — set smtp_host in pool_config)
    cfg = await _get_smtp_config(pool)
    import os as _os
    smtp_host = cfg.get("smtp_host") or _os.environ.get("SMTP_HOST", "")
    if not smtp_host:
        return False, "email transport unavailable (Resend not responding and no fallback configured)"
    smtp_port = int(cfg.get("smtp_port") or _os.environ.get("SMTP_PORT", "587"))
    smtp_user = cfg.get("smtp_user") or _os.environ.get("SMTP_USER", "")
    smtp_pass = cfg.get("smtp_pass") or _os.environ.get("SMTP_PASS", "")
    smtp_from = cfg.get("smtp_from") or _os.environ.get("SMTP_FROM", "contact@cellule.ai")

    body = (
        f"Bonjour {pseudo},\n\n"
        f"Voici votre code d activation pour cellule.ai :\n\n"
        f"    {code}\n\n"
        f"Il expire dans 15 minutes.\n\n"
        f"Si vous n etes pas a l origine de cette inscription, ignorez ce message.\n\n"
        f"-- Cellule.ai"
    )
    msg = _MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Cellule.ai] Code d activation : {code}"
    msg["From"] = smtp_from
    msg["To"] = to_email
    try:
        with _smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            if smtp_user:
                s.starttls()
                s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_from, [to_email], msg.as_string())
        log.info(f"Verification email sent to {to_email} via SMTP fallback")
        return True, "sent via smtp"
    except Exception as e:
        log.warning(f"SMTP send to {to_email} failed: {e}")
        return False, str(e)[:200]


# ─── POST /v1/auth/register ─────────────────────────────────────────────────

@router.post("/v1/auth/register")
async def auth_register(data: dict):
    """Creer un compte pour lier plusieurs workers."""
    from passlib.hash import argon2 as _argon2
    pool = _pool()
    accounts = _accounts()

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    pseudo = data.get("pseudo", "").strip()
    display_name = data.get("display_name", "")

    # M11.1 — quorum precheck (scaffold phase 1 never blocks; full enforcement
    # when ACCOUNT_CREATION_QUORUM_ENABLED=true AND N_total >= 3).
    if not email or not password:
        return JSONResponse({"error": "email and password required"}, status_code=400)

    # M11.1 precheck (may block if flag enabled + partition detected)
    try:
        from ..core import federation_replication as _repl
        if _repl.is_account_creation_quorum_enabled():
            precheck = await _repl.account_creation_quorum_precheck(pool)
            if precheck.get("would_block_in_phase_2"):
                return JSONResponse({
                    "error": "quorum unavailable, molecule partition detected, retry later",
                    "precheck": precheck,
                }, status_code=503)
    except Exception as _e:
        # Never fail-closed on precheck bugs. Log and continue.
        pass

    if len(password) < 8:
        return JSONResponse({"error": "password must be at least 8 characters"}, status_code=400)
    if not pseudo:
        return JSONResponse({"error": "pseudo is required"}, status_code=400)

    # Validation display_name (longueur + anti-XSS)
    if display_name:
        display_name = display_name.replace("<", "").replace(">", "").replace("&", "")
        if len(display_name) < 2 or len(display_name) > 50:
            return JSONResponse({"error": "display_name must be 2-50 characters"}, status_code=400)

    # Verifier si l'email existe deja
    for acc in accounts.values():
        if acc["email"] == email:
            return JSONResponse({"error": "email already registered"}, status_code=409)

    account_id = secrets.token_hex(16)
    password_hash = _argon2.hash(password)

    account_token = _derive_account_token(email)
    accounts[account_id] = {
        "account_id": account_id,
        "account_token": account_token,
        "email": email,
        "password_hash": password_hash,
        "pseudo": pseudo,
        "display_name": display_name or pseudo,
        "eth_address": None,
        "worker_ids": [],
        "created": time.time(),
    }
    _save_accounts()
    # DB INSERT: await directly so verification UPDATE later in this function
    # sees an existing row (avoid race with the fire-and-forget wrapper).
    try:
        from iamine.pool import _save_account_to_db as _save_db_async
        await _save_db_async(account_id)
    except Exception as _e:
        log.warning(f"register DB insert failed: {_e}")

    # M11.1 — fire-and-forget replication push to bonded peers (identity-only).
    # Guardian hard rec #3: MUST NOT be awaited in handler to preserve register
    # latency under degraded network conditions. asyncio.create_task makes it
    # truly background.
    try:
        from ..core import federation_replication as _repl
        if _repl.is_account_creation_quorum_enabled():
            account_row_for_push = {
                "account_id": account_id,
                "email": email,
                "password_hash": password_hash,
                "display_name": display_name or pseudo,
                "pseudo": pseudo,
                "eth_address": None,
                "account_token": account_token,
                "memory_enabled": False,
                "created": accounts[account_id].get("created"),
            }
            asyncio.create_task(_repl.replicate_account_to_peers(pool, account_row_for_push, min_trust=2))
    except Exception as _e:
        # Never fail register on replication push errors
        pass


    session_id = _create_session(account_id)
    acc = accounts[account_id]
    api_token = acc.get("account_token", _derive_account_token(email))
    if api_token not in pool.api_tokens:
        pool.api_tokens[api_token] = {
            "worker_id": f"account-{account_id[:8]}",
            "account_id": account_id,
            "created": time.time(),
            "requests_used": 0,
            "credits": 0,
        }
    # === Email verification (migration 016) ===
    verification_code = _generate_verification_code()
    verification_expires = int(time.time()) + _VERIFICATION_TTL_SEC
    accounts[account_id]["email_verified"] = False
    accounts[account_id]["verification_code"] = verification_code
    accounts[account_id]["verification_expires"] = verification_expires
    _save_accounts()

    # Persist verification fields in DB (local columns, NEVER replicated)
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE accounts
                   SET email_verified = FALSE,
                       verification_code = $1,
                       verification_expires = $2
                 WHERE account_id = $3
                """,
                verification_code, verification_expires, account_id,
            )
    except Exception as _e:
        log.warning(f"verification DB persist failed: {_e}")

    sent, send_status = await _send_verification_email(pool, email, verification_code, pseudo)
    if not sent:
        # SMTP broken: surface the error but keep the pending account so user can retry resend.
        log.error(f"Register {email}: SMTP send failed ({send_status})")
        return JSONResponse({
            "error": "Could not send verification email. Please try again or contact support.",
            "pending_verification": True,
            "email": email,
            "smtp_error": send_status,
        }, status_code=502)

    # RAG : on seed uniquement apres activation (handled in /activate)
    return {
        "pending_verification": True,
        "email": email,
        "pseudo": pseudo,
        "message": "Verification code sent to your email. Enter it to activate your account.",
    }


# ─── POST /v1/auth/login ────────────────────────────────────────────────────

@router.post("/v1/auth/login")
async def auth_login(data: dict):
    """Connexion avec email/password."""
    from passlib.hash import argon2 as _argon2
    pool = _pool()
    accounts = _accounts()

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    for acc in accounts.values():
        if acc["email"] == email:
            # Support ancien hash SHA256 + nouveau argon2
            if acc["password_hash"].startswith("$argon2"):
                if not _argon2.verify(password, acc["password_hash"]):
                    break
            else:
                # Migration : vérifier ancien SHA256, puis upgrader vers argon2
                old_hash = hashlib.sha256(password.encode()).hexdigest()
                if acc["password_hash"] != old_hash:
                    break
                acc["password_hash"] = _argon2.hash(password)
                _save_accounts()
# Persister en DB aussi (DB-first)    from iamine.pool import _save_account_to_db    asyncio.create_task(_save_account_to_db(account_id))
            # === Email verification gate (migration 016) ===
            if not acc.get("email_verified", True):
                return JSONResponse({
                    "error": "email not verified",
                    "needs_verification": True,
                    "email": acc["email"],
                }, status_code=403)
            session_id = _create_session(acc["account_id"])
            api_token = acc.get("account_token", _derive_account_token(email))
            if api_token not in pool.api_tokens:
                pool.api_tokens[api_token] = {
                    "worker_id": f"account-{acc['account_id'][:8]}",
                    "account_id": acc["account_id"],
                    "created": time.time(),
                    "requests_used": 0,
                    "credits": acc.get("total_credits", 0),
                }
            return {
                "account_id": acc["account_id"],
                "session_id": session_id,
                "api_token": api_token,
                "display_name": acc["display_name"],
            }

    return JSONResponse({"error": "invalid email or password"}, status_code=401)


# ─── POST /v1/auth/google ───────────────────────────────────────────────────

@router.post("/v1/auth/google")
async def auth_google(data: dict):
    """Connexion via Google Sign-In. Cree le compte automatiquement si nouveau."""
    import base64
    pool = _pool()
    accounts = _accounts()

    credential = data.get("credential", "")
    if not credential:
        return JSONResponse({"error": "missing credential"}, status_code=400)

    # Decoder le JWT Google (sans lib externe, on decode le payload)
    try:
        # Le JWT a 3 parties: header.payload.signature
        parts = credential.split(".")
        # Decoder le payload (2e partie)
        payload = parts[1]
        # Ajouter le padding base64
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))

        email = decoded.get("email", "").strip().lower()
        name = decoded.get("name", "")
        picture = decoded.get("picture", "")
        email_verified = decoded.get("email_verified", False)

        # Verifier expiration du token Google
        exp = decoded.get("exp", 0)
        iat = decoded.get("iat", 0)
        now = time.time()
        if exp and now > exp + 300:  # 5 min de grace
            return JSONResponse({"error": "Google token expired"}, status_code=401)
        if iat and now < iat - 60:  # token du futur (1 min de grace)
            return JSONResponse({"error": "Google token not yet valid"}, status_code=401)
        aud = decoded.get("aud", "")

        # Verifier que le token est pour notre app
        if aud != GOOGLE_CLIENT_ID:
            return JSONResponse({"error": "invalid token audience"}, status_code=401)
        if not email or not email_verified:
            return JSONResponse({"error": "email not verified"}, status_code=401)

    except Exception as e:
        return JSONResponse({"error": f"invalid Google token: {e}"}, status_code=401)

    # Chercher si le compte existe deja
    existing_account = None
    for acc in accounts.values():
        if acc["email"] == email:
            existing_account = acc
            break

    if existing_account:
        # Login
        account_id = existing_account["account_id"]
    else:
        # Register automatique
        account_id = secrets.token_hex(16)
        account_token = _derive_account_token(email)
        pseudo = name or email.split("@")[0]
        accounts[account_id] = {
            "account_id": account_id,
            "account_token": account_token,
            "email": email,
            "password_hash": "google_oauth",
            "pseudo": pseudo,
            "display_name": pseudo,
            "email_verified": True,
            "eth_address": None,
            "worker_ids": [],
            "created": time.time(),
            "google_picture": picture,
        }
        _save_accounts()
# Persister en DB aussi (DB-first)    from iamine.pool import _save_account_to_db    asyncio.create_task(_save_account_to_db(account_id))
        log.info(f"New Google account: {email} ({pseudo})")
        asyncio.create_task(_seed_user_memory(account_token, pseudo))

    session_id = _create_session(account_id)
    acc = accounts[account_id]
    api_token = acc.get("account_token", _derive_account_token(email))
    # S'assurer que le token est dans le pool pour le routing
    if api_token not in pool.api_tokens:
        pool.api_tokens[api_token] = {
            "worker_id": f"account-{account_id[:8]}",
            "account_id": account_id,
            "created": time.time(),
            "requests_used": 0,
            "credits": acc.get("credits", 0),
        }
    # Si pseudo == email prefix, l utilisateur n a pas choisi de pseudo personnalise
    pseudo = acc.get("pseudo", "")
    needs_pseudo = (not pseudo) or (pseudo == email.split("@")[0])

    return {
        "account_id": account_id,
        "session_id": session_id,
        "api_token": api_token,
        "display_name": acc["display_name"],
        "pseudo": pseudo,
        "email": email,
        "picture": acc.get("google_picture", ""),
        "needs_pseudo": needs_pseudo,
    }


# ─── POST /v1/account/link-worker ───────────────────────────────────────────

@router.post("/v1/account/link-worker")
async def link_worker(data: dict):
    """Lie un worker (via son api_token) a un compte utilisateur."""
    pool = _pool()
    accounts = _accounts()

    session_id = data.get("session_id", "")
    api_token = data.get("api_token", "")

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)

    token_data = pool.api_tokens.get(api_token)
    if not token_data:
        return JSONResponse({"error": "invalid api_token"}, status_code=404)

    worker_id = token_data["worker_id"]
    acc = accounts[account_id]

    # Verifier en memoire OU en DB
    in_memory = worker_id in acc["worker_ids"]
    in_db = False
    try:
        db_workers = await pool.store.get_workers_by_account(account_id)
        in_db = any(w["worker_id"] == worker_id for w in db_workers)
    except Exception:
        pass
    if not in_memory and not in_db:
        return JSONResponse({"error": "worker not linked to this account"}, status_code=404)

        _save_accounts()
# Persister en DB aussi (DB-first)    from iamine.pool import _save_account_to_db    asyncio.create_task(_save_account_to_db(account_id))

    # Marquer le token comme lie au compte
    token_data["account_id"] = account_id

    return {
        "status": "ok",
        "worker_id": worker_id,
        "account_workers": len(acc["worker_ids"]),
    }


# ─── GET /v1/account/my-workers ─────────────────────────────────────────────

@router.get("/v1/account/my-workers")
async def my_workers(session_id: str = ""):
    """Retourne les workers lies a un compte + solde consolide."""
    pool = _pool()
    accounts = _accounts()

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)

    acc = accounts[account_id]
    workers_list = []
    total_credits = 0.0
    total_earned = 0.0

    # Fusionner workers en memoire + workers en DB pour ce compte
    db_worker_ids = set()
    try:
        db_workers = await pool.store.get_workers_by_account(account_id)
        db_worker_ids = {w["worker_id"] for w in db_workers}
    except Exception:
        pass
    all_worker_ids = list(set(acc["worker_ids"]) | db_worker_ids)

    for wid in all_worker_ids:
        w = pool.workers.get(wid)
        # Trouver le token de ce worker
        tk = None
        for t, td in pool.api_tokens.items():
            if td["worker_id"] == wid:
                tk = td
                break
        expected_token = _derive_api_token(wid)

        # Modele : in-memory (actuel) + assignation DB (cible)
        current_model = w.info.get("model_path", "?").split("/")[-1] if w else "offline"
        assigned_model = None
        try:
            db_assign = await pool.store.get_worker_assignment(wid)
            if db_assign:
                from iamine.models import REGISTRY_BY_ID
                tier = REGISTRY_BY_ID.get(db_assign["model_id"])
                assigned_model = tier.name if tier else db_assign["model_id"]
                # Si offline, montrer le modele assigne au lieu de "offline"
                if not w and assigned_model:
                    current_model = assigned_model + " (offline)"
        except Exception:
            pass

        # Determiner le statut migration
        is_unknown = pool._is_unknown_model(w) if w else False
        model_status = "ok"
        if not w:
            model_status = "offline"
        elif is_unknown:
            model_status = "migrating"
        elif pool._is_outdated(w):
            model_status = "outdated"

        workers_list.append({
            "worker_id": wid,
            "is_online": w is not None,
            "model": current_model,
            "assigned_model": assigned_model,
            "model_status": model_status,
            "jobs_done": w.jobs_done if w else (tk.get("total_earned", 0) if tk else 0),
            "credits": round(tk["credits"], 2) if tk else 0,
            "total_earned": round(tk.get("total_earned", 0), 2) if tk else 0,
            "api_token": expected_token[:20] + "...",
        })
        if tk:
            total_credits += tk["credits"]
            total_earned += tk.get("total_earned", 0)

    return {
        "account_id": account_id,
        "account_token": acc.get("account_token", ""),
        "display_name": acc["display_name"],
        "email": acc["email"],
        "eth_address": acc.get("eth_address"),
        "worker_count": len(all_worker_ids),
        "workers": workers_list,
        "total_credits": round(total_credits, 2),
        "total_earned": round(total_earned, 2),
    }


# ─── POST /v1/account/set-pseudo ────────────────────────────────────────────

@router.post("/v1/account/set-pseudo")
async def set_pseudo(data: dict):
    """Definir ou changer le pseudo du compte. Necessaire pour la vectorisation RAG."""
    pool = _pool()
    accounts = _accounts()
    session_id = data.get("session_id", "")
    pseudo = data.get("pseudo", "").strip()

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)
    if not pseudo or len(pseudo) < 2:
        return JSONResponse({"error": "pseudo must be at least 2 characters"}, status_code=400)

    acc = accounts[account_id]
    old_pseudo = acc.get("pseudo", "")
    acc["pseudo"] = pseudo
    acc["display_name"] = pseudo
    _save_accounts()
# Persister en DB aussi (DB-first)    from iamine.pool import _save_account_to_db    asyncio.create_task(_save_account_to_db(account_id))

    # Re-seed la memoire RAG avec le nouveau pseudo
    api_token = acc.get("account_token", "")
    if api_token:
        asyncio.create_task(_seed_user_memory(api_token, pseudo))

    log.info(f"Pseudo updated: {acc.get("email","?")} {old_pseudo} -> {pseudo}")
    return {"pseudo": pseudo, "display_name": pseudo}



# ─── GET /v1/account/memory ────────────────────────────────────────────────

@router.get("/v1/account/memory")
async def get_memory_status(session_id: str = ""):
    """Retourne l etat actuel de la memoire persistante du compte."""
    pool = _pool()
    accounts = _accounts()

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)

    acc = accounts[account_id]
    memory_enabled = acc.get("memory_enabled", True)

    # Compter les faits RAG stockes
    facts_count = 0
    try:
        api_token = acc.get("account_token", "")
        if api_token and hasattr(pool, "store") and hasattr(pool.store, "count_user_memories"):
            facts_count = await pool.store.count_user_memories(api_token)
    except Exception:
        pass

    return {"memory_enabled": memory_enabled, "facts_count": facts_count}


# ─── POST /v1/account/memory ───────────────────────────────────────────────

@router.post("/v1/account/memory")
async def set_memory(data: dict):
    """Active ou desactive la memoire persistante (RAG) du compte."""
    accounts = _accounts()

    session_id = data.get("session_id", "")
    enabled = data.get("enabled")

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)

    if enabled is None:
        return JSONResponse({"error": "enabled field required (true/false)"}, status_code=400)

    acc = accounts[account_id]
    acc["memory_enabled"] = bool(enabled)
    _save_accounts()
    # Persister en DB aussi (DB-first)
    from iamine.pool import _save_account_to_db
    asyncio.create_task(_save_account_to_db(account_id))

    state = "activee" if acc["memory_enabled"] else "desactivee"
    log.info(f"Memory {state} for {acc.get('email', account_id)}")
    return {
        "memory_enabled": acc["memory_enabled"],
        "message": f"Memoire persistante {state}",
    }



# ─── GET /v1/account/conversations — liste les conversations ────────────────

@router.get("/v1/account/conversations")
async def list_conversations(request: Request):
    """Liste les conversations d'un utilisateur. Auth par Bearer token."""
    pool = _pool()
    accounts = _accounts()

    auth_header = request.headers.get("authorization", "")
    api_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not api_token:
        return JSONResponse({"error": "missing Bearer token"}, status_code=401)

    acc = next((a for a in accounts.values() if a.get("account_token") == api_token), None)
    if not acc:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    try:
        conversations = await pool.store.list_conversations(api_token)
    except Exception as e:
        log.warning(f"list_conversations error: {e}")
        return JSONResponse({"error": "internal error"}, status_code=500)

    return {"conversations": conversations}


# ─── GET /v1/account/conversations/{conv_id} — export une conversation ──────

@router.get("/v1/account/conversations/{conv_id}")
async def get_conversation(conv_id: str, request: Request, format: str = "json"):
    """Export une conversation complete. Auth par Bearer token.
    Query param ?format=markdown pour export MD (defaut: JSON).
    """
    pool = _pool()
    accounts = _accounts()

    auth_header = request.headers.get("authorization", "")
    api_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not api_token:
        return JSONResponse({"error": "missing Bearer token"}, status_code=401)

    acc = next((a for a in accounts.values() if a.get("account_token") == api_token), None)
    if not acc:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    try:
        conv = await pool.store.load_conversation(conv_id, api_token)
    except Exception as e:
        log.warning(f"get_conversation error: {e}")
        return JSONResponse({"error": "internal error"}, status_code=500)

    if not conv:
        return JSONResponse({"error": "conversation not found"}, status_code=404)

    if format == "markdown":
        md_lines = []
        md_lines.append("# " + (conv.get("title") or conv["conv_id"]))
        md_lines.append("")
        if conv.get("summary"):
            md_lines.append("**Resume:** " + conv["summary"])
            md_lines.append("")
        md_lines.append(f"_Messages: {conv.get('message_count', 0)} | Tokens: {conv.get('total_tokens', 0)}_")
        md_lines.append("")
        for msg in conv.get("messages", []):
            role = msg.get("role", "unknown").capitalize()
            ctn = msg.get("content", "")
            md_lines.append(f"### {role}")
            md_lines.append(ctn)
            md_lines.append("")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(chr(10).join(md_lines), media_type="text/markdown")

    return conv


# ─── DELETE /v1/account/conversations ────────────────────────────────────────

@router.delete("/v1/account/conversations")
async def delete_account_conversations(request: Request):
    """Supprime toutes les conversations d un utilisateur (RGPD)."""
    pool = _pool()
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "Bearer token required"}, status_code=401)
    api_token = auth.removeprefix("Bearer ").strip()
    if api_token not in pool.api_tokens:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    # Supprimer en DB
    count = await pool.store.delete_user_conversations(api_token)

    # Nettoyer les conversations en RAM
    ram_deleted = 0
    to_delete = [
        cid for cid, conv in pool.router._conversations.items()
        if conv.api_token == api_token
    ]
    for cid in to_delete:
        del pool.router._conversations[cid]
        ram_deleted += 1

    log.info(f"RGPD: deleted {count} DB + {ram_deleted} RAM conversations for token {api_token[:12]}...")
    return {"status": "deleted", "conversations_deleted_db": count, "conversations_deleted_ram": ram_deleted}


# ─── DELETE /v1/account/conversations/{conv_id} ─────────────────────────────

@router.delete("/v1/account/conversations/{conv_id}")
async def delete_account_conversation(conv_id: str, request: Request):
    """Supprime une conversation specifique d un utilisateur."""
    pool = _pool()
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "Bearer token required"}, status_code=401)
    api_token = auth.removeprefix("Bearer ").strip()
    if api_token not in pool.api_tokens:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    # Supprimer en DB (verifie le proprietaire)
    deleted = await pool.store.delete_conversation_by_user(conv_id, api_token)

    # Supprimer de la RAM si presente
    conv = pool.router._conversations.get(conv_id)
    ram_deleted = False
    if conv and conv.api_token == api_token:
        del pool.router._conversations[conv_id]
        ram_deleted = True

    if not deleted and not ram_deleted:
        return JSONResponse({"error": "conversation not found or not owned"}, status_code=404)

    log.info(f"RGPD: deleted conversation {conv_id} for token {api_token[:12]}...")
    return {"status": "deleted", "conv_id": conv_id, "deleted_db": deleted, "deleted_ram": ram_deleted}


# ─── DELETE /v1/account/data ─────────────────────────────────────────────────

@router.delete("/v1/account/data")
async def delete_account_data(request: Request):
    """Suppression totale des donnees utilisateur — droit a l oubli (RGPD)."""
    pool = _pool()
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "Bearer token required"}, status_code=401)
    api_token = auth.removeprefix("Bearer ").strip()
    if api_token not in pool.api_tokens:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    # 1. Supprimer les conversations (DB + RAM)
    conv_count = await pool.store.delete_user_conversations(api_token)
    ram_deleted = 0
    to_delete = [
        cid for cid, conv in pool.router._conversations.items()
        if conv.api_token == api_token
    ]
    for cid in to_delete:
        del pool.router._conversations[cid]
        ram_deleted += 1

    # 2. Supprimer les memoires RAG
    from iamine.memory import token_hash
    th = token_hash(api_token)
    mem_count = await pool.store.delete_user_memories(th)

    log.info(f"RGPD droit a l oubli: token {api_token[:12]}... -> {conv_count} convs DB, {ram_deleted} convs RAM, {mem_count} memories")
    return {
        "status": "deleted",
        "conversations_deleted": conv_count + ram_deleted,
        "memories_deleted": mem_count,
    }


# ─── GET /v1/account/memories — liste les faits memorises ────────────────────

@router.get("/v1/account/memories")
async def list_memories(request: Request):
    """Liste les faits memorises (RAG) dechiffres. Auth par Bearer token."""
    pool = _pool()
    accounts = _accounts()

    auth_header = request.headers.get("authorization", "")
    api_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not api_token:
        return JSONResponse({"error": "missing Bearer token"}, status_code=401)

    acc = next((a for a in accounts.values() if a.get("account_token") == api_token), None)
    if not acc:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    try:
        memories = await pool.store.list_user_memories(api_token)
    except Exception as e:
        log.warning(f"list_memories error: {e}")
        return JSONResponse({"error": "internal error"}, status_code=500)

    return {"memories": memories}


# ─── DELETE /v1/account/memories/{memory_id} — supprime un fait ──────────────

@router.delete("/v1/account/memories/{memory_id}")
async def delete_memory(memory_id: int, request: Request):
    """Supprime un fait memorise specifique. Auth par Bearer token."""
    pool = _pool()
    accounts = _accounts()

    auth_header = request.headers.get("authorization", "")
    api_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not api_token:
        return JSONResponse({"error": "missing Bearer token"}, status_code=401)

    acc = next((a for a in accounts.values() if a.get("account_token") == api_token), None)
    if not acc:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    try:
        deleted = await pool.store.delete_user_memory(memory_id, api_token)
    except Exception as e:
        log.warning(f"delete_memory error: {e}")
        return JSONResponse({"error": "internal error"}, status_code=500)

    if not deleted:
        return JSONResponse({"error": "memory not found or not owned"}, status_code=404)

    return {"deleted": True, "memory_id": memory_id}


# ─── POST /v1/account/unlink-worker ─────────────────────────────────────────

@router.post("/v1/account/unlink-worker")
async def unlink_worker(data: dict):
    """Supprime un worker du compte utilisateur."""
    pool = _pool()
    accounts = _accounts()

    session_id = data.get("session_id", "")
    worker_id = data.get("worker_id", "")

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)

    acc = accounts[account_id]
    # Verifier en memoire OU en DB
    in_memory = worker_id in acc["worker_ids"]
    in_db = False
    try:
        db_workers = await pool.store.get_workers_by_account(account_id)
        in_db = any(w["worker_id"] == worker_id for w in db_workers)
    except Exception:
        pass
    if not in_memory and not in_db:
        return JSONResponse({"error": "worker not linked to this account"}, status_code=404)


    if worker_id in acc["worker_ids"]:
        acc["worker_ids"].remove(worker_id)
    _save_accounts()
# Persister en DB aussi (DB-first)    from iamine.pool import _save_account_to_db    asyncio.create_task(_save_account_to_db(account_id))

    # Supprimer de la DB (DB-first)
    try:
        await pool.store.delete_worker(worker_id)
    except Exception as e:
        log.warning(f"DB delete worker {worker_id} failed: {e}")

    # Retirer le lien account_id du token API si existant
    api_token = _derive_api_token(worker_id)
    token_data = pool.api_tokens.get(api_token)
    if token_data:
        token_data.pop("account_id", None)

    log.info(f"Worker {worker_id} unlinked from account {acc['email']}")
    return {
        "status": "ok",
        "worker_id": worker_id,
        "remaining_workers": len(acc["worker_ids"]),
    }


# ─── POST /v1/account/set-eth ───────────────────────────────────────────────

@router.post("/v1/account/set-eth")
async def set_eth_address(data: dict):
    """Lie une adresse ETH au compte (pour l'export Web3 futur)."""
    accounts = _accounts()

    session_id = data.get("session_id", "")
    eth_address = data.get("eth_address", "")

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)

    if not eth_address.startswith("0x") or len(eth_address) != 42:
        return JSONResponse({"error": "invalid ETH address"}, status_code=400)

    accounts[account_id]["eth_address"] = eth_address
    return {"status": "ok", "eth_address": eth_address}



# ─── POST /v1/auth/activate ────────────────────────────────

@router.post("/v1/auth/activate")
async def auth_activate(data: dict):
    """Verifie le code 6-digits envoye par email et active le compte."""
    pool = _pool()
    accounts = _accounts()
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    if not email or not code:
        return JSONResponse({"error": "email and code required"}, status_code=400)

    acc = None
    account_id = None
    for aid, a in accounts.items():
        if a.get("email") == email:
            acc = a
            account_id = aid
            break
    if not acc:
        return JSONResponse({"error": "account not found"}, status_code=404)
    if acc.get("email_verified"):
        return JSONResponse({"error": "already verified", "already_verified": True}, status_code=400)
    stored = acc.get("verification_code")
    expires = int(acc.get("verification_expires") or 0)
    if not stored or not expires:
        return JSONResponse({"error": "no pending verification, resend code"}, status_code=400)
    if time.time() > expires:
        return JSONResponse({"error": "code expired, resend a new one", "expired": True}, status_code=400)
    if code != stored:
        return JSONResponse({"error": "invalid code"}, status_code=400)

    acc["email_verified"] = True
    acc["verification_code"] = None
    acc["verification_expires"] = None
    _save_accounts()

    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE accounts
                   SET email_verified = TRUE,
                       verification_code = NULL,
                       verification_expires = NULL
                 WHERE account_id = $1
                """,
                account_id,
            )
    except Exception as _e:
        log.warning(f"activate DB update failed: {_e}")

    # RAG: seed memory now that account is usable
    api_token = acc.get("account_token")
    if api_token:
        asyncio.create_task(_seed_user_memory(api_token, acc.get("pseudo", "")))

    # Optional: push to peers now that the account is verified
    try:
        from ..core import federation_replication as _repl
        if _repl.is_account_creation_quorum_enabled():
            row = {
                "account_id": account_id,
                "email": acc["email"],
                "password_hash": acc["password_hash"],
                "display_name": acc.get("display_name", acc.get("pseudo", "")),
                "pseudo": acc.get("pseudo", ""),
                "eth_address": acc.get("eth_address"),
                "account_token": acc.get("account_token"),
                "memory_enabled": acc.get("memory_enabled", False),
                "created": acc.get("created"),
            }
            asyncio.create_task(_repl.replicate_account_to_peers(pool, row, min_trust=2))
    except Exception:
        pass

    session_id = _create_session(account_id)
    log.info(f"Account {email} activated via email code")
    return {
        "account_id": account_id,
        "session_id": session_id,
        "api_token": api_token,
        "pseudo": acc.get("pseudo", ""),
        "display_name": acc.get("display_name", acc.get("pseudo", "")),
        "email_verified": True,
    }


# ─── POST /v1/auth/resend-code ──────────────────────────────

_LAST_RESEND = {}  # email -> ts

@router.post("/v1/auth/resend-code")
async def auth_resend_code(data: dict):
    pool = _pool()
    accounts = _accounts()
    email = (data.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)

    now = time.time()
    last = _LAST_RESEND.get(email, 0)
    if now - last < _RESEND_COOLDOWN_SEC:
        wait = int(_RESEND_COOLDOWN_SEC - (now - last))
        return JSONResponse({"error": f"please wait {wait}s before resending", "cooldown_sec": wait}, status_code=429)

    acc = None
    account_id = None
    for aid, a in accounts.items():
        if a.get("email") == email:
            acc = a
            account_id = aid
            break
    if not acc:
        return JSONResponse({"error": "account not found"}, status_code=404)
    if acc.get("email_verified"):
        return JSONResponse({"error": "already verified"}, status_code=400)

    code = _generate_verification_code()
    expires = int(now) + _VERIFICATION_TTL_SEC
    acc["verification_code"] = code
    acc["verification_expires"] = expires
    _save_accounts()
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE accounts
                   SET verification_code = $1, verification_expires = $2
                 WHERE account_id = $3
                """,
                code, expires, account_id,
            )
    except Exception as _e:
        log.warning(f"resend DB update failed: {_e}")

    sent, err = await _send_verification_email(pool, email, code, acc.get("pseudo", ""))
    _LAST_RESEND[email] = now
    if not sent:
        return JSONResponse({"error": f"smtp failed: {err}"}, status_code=502)
    return {"sent": True, "cooldown_sec": _RESEND_COOLDOWN_SEC}


# ─── GET /v1/account/me ───────────────────────────────────

@router.get("/v1/account/me")
async def account_me(request: Request):
    """Profil complet du compte connecte."""
    accounts = _accounts()
    session_id = request.headers.get("x-session") or request.query_params.get("session_id", "")
    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)
    acc = accounts[account_id]
    return {
        "account_id": account_id,
        "email": acc.get("email"),
        "pseudo": acc.get("pseudo"),
        "display_name": acc.get("display_name"),
        "eth_address": acc.get("eth_address"),
        "memory_enabled": acc.get("memory_enabled", False),
        "email_verified": acc.get("email_verified", True),
        "created": acc.get("created"),
        "worker_ids": acc.get("worker_ids", []),
        "account_token": acc.get("account_token"),
    }


# ─── POST /v1/account/change-password ─────────────────────────

@router.post("/v1/account/change-password")
async def account_change_password(data: dict):
    from passlib.hash import argon2 as _argon2
    pool = _pool()
    accounts = _accounts()
    session_id = data.get("session_id", "")
    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)
    acc = accounts[account_id]
    if acc.get("password_hash") == "google_oauth":
        return JSONResponse({"error": "Google accounts cannot change password here"}, status_code=400)
    if not old_password or not new_password:
        return JSONResponse({"error": "old_password and new_password required"}, status_code=400)
    if len(new_password) < 8:
        return JSONResponse({"error": "new password must be at least 8 characters"}, status_code=400)
    try:
        if not _argon2.verify(old_password, acc["password_hash"]):
            return JSONResponse({"error": "old password incorrect"}, status_code=401)
    except Exception:
        return JSONResponse({"error": "old password incorrect"}, status_code=401)

    new_hash = _argon2.hash(new_password)
    acc["password_hash"] = new_hash
    _save_accounts()
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                "UPDATE accounts SET password_hash = $1 WHERE account_id = $2",
                new_hash, account_id,
            )
    except Exception as _e:
        log.warning(f"change-password DB update failed: {_e}")

    log.info(f"Password changed for {acc.get('email')}")
    return {"ok": True}


# ─── DELETE /v1/account/delete ──────────────────────────────

@router.post("/v1/account/delete")
async def account_delete(data: dict):
    """Suppression TOTALE du compte. RGPD + zero trace.

    Requires: session_id + password + confirm=='DELETE' (English caps).
    """
    from passlib.hash import argon2 as _argon2
    pool = _pool()
    accounts = _accounts()
    session_id = data.get("session_id", "")
    password = data.get("password", "")
    confirm = data.get("confirm", "")

    account_id = _get_session_account(session_id)
    if not account_id or account_id not in accounts:
        return JSONResponse({"error": "invalid session"}, status_code=401)
    if confirm != "DELETE":
        return JSONResponse({"error": "confirm must equal DELETE"}, status_code=400)
    acc = accounts[account_id]
    if acc.get("password_hash") != "google_oauth":
        if not password:
            return JSONResponse({"error": "password required"}, status_code=400)
        try:
            if not _argon2.verify(password, acc["password_hash"]):
                return JSONResponse({"error": "password incorrect"}, status_code=401)
        except Exception:
            return JSONResponse({"error": "password incorrect"}, status_code=401)

    email = acc.get("email", "")
    api_token = acc.get("account_token", "")

    # 1. Wipe conversations + RAG
    try:
        await pool.store.delete_user_conversations(api_token)
    except Exception:
        pass
    try:
        from iamine.memory import token_hash
        await pool.store.delete_user_memories(token_hash(api_token))
    except Exception:
        pass

    # 2. Drop in-RAM conversations for this token
    to_del = [cid for cid, conv in pool.router._conversations.items() if conv.api_token == api_token]
    for cid in to_del:
        del pool.router._conversations[cid]

    # 3. Delete account row in DB
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute("DELETE FROM accounts WHERE account_id = $1", account_id)
    except Exception as _e:
        log.error(f"account delete DB failed: {_e}")
        return JSONResponse({"error": "db delete failed"}, status_code=500)

    # 4. Remove from in-memory dict + JSON file
    if account_id in accounts:
        del accounts[account_id]
    _save_accounts()

    # 5. Invalidate session
    try:
        _invalidate_session(session_id)
    except Exception:
        pass

    log.info(f"Account DELETED: {email}")
    return {"deleted": True}
