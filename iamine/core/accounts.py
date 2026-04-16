"""Account & session management - extracted from pool.py (refactoring step 3)."""

from __future__ import annotations

import json as _j
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import Request

from .utils import _derive_api_token, _derive_account_token

log = logging.getLogger("iamine.accounts")

# ---------------------------------------------------------------------------
# Shared mutable state  (imported by reference everywhere)
# ---------------------------------------------------------------------------
_ACCOUNTS_FILE = Path(__file__).parent.parent.parent / "accounts.json"
_accounts: dict[str, dict] = {}
_sessions: dict[str, dict] = {}   # session_id -> {"account_id": str, "created": float}
_SESSION_TTL = 86400 * 30          # 30 jours

GOOGLE_CLIENT_ID = "106098942094-u10np9r0n03pg0g0370m0su0tgcjede0.apps.googleusercontent.com"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _create_session(account_id: str) -> str:
    """Cree une session avec TTL."""
    session_id = "ses_" + secrets.token_hex(24)
    _sessions[session_id] = {"account_id": account_id, "created": time.time()}
    return session_id


def _get_session_account(session_id: str) -> str | None:
    """Retourne l'account_id si la session est valide, None sinon."""
    s = _sessions.get(session_id)
    if not s:
        return None
    if isinstance(s, str):
        # Migration ancien format (str directe)
        return s
    if time.time() - s.get("created", 0) > _SESSION_TTL:
        _sessions.pop(session_id, None)
        return None
    return s["account_id"]


# ---------------------------------------------------------------------------
# Persistence  - JSON (fallback) + PostgreSQL (source of truth)
# ---------------------------------------------------------------------------
def _load_accounts():
    """Load accounts from JSON file (fallback). DB loading happens async at startup."""
    global _accounts
    if _ACCOUNTS_FILE.exists():
        try:
            with open(_ACCOUNTS_FILE) as f:
                _accounts.update(_j.load(f))
            log.info(f"Loaded {len(_accounts)} accounts from {_ACCOUNTS_FILE}")
        except Exception:
            pass


async def _load_accounts_from_db(pool_instance=None):
    """Load accounts from PostgreSQL (source of truth)."""
    global _accounts
    if pool_instance is None:
        from iamine.pool import pool as pool_instance
    if not hasattr(pool_instance, "store") or not hasattr(pool_instance.store, "pool") or not pool_instance.store.pool:
        return
    try:
        async with pool_instance.store.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM accounts")
            for r in rows:
                acc_id = r["account_id"]
                _accounts[acc_id] = {
                    "account_id": acc_id,
                    "email": r["email"],
                    "password_hash": r["password_hash"],
                    "display_name": r["display_name"] or "",
                    "pseudo": r.get("pseudo") or r["display_name"] or "",
                    "account_token": r.get("account_token") or _derive_account_token(r["email"]),
                    "eth_address": r.get("eth_address"),
                    "total_credits": float(r.get("total_credits") or 0),
                    "total_earned": float(r.get("total_earned") or 0),
                    "total_spent": float(r.get("total_spent") or 0),
                    "worker_ids": _j.loads(r.get("worker_ids") or "[]") if isinstance(r.get("worker_ids"), str) else [],
                    "created": r["created"].timestamp() if r.get("created") else 0,
                    "memory_enabled": bool(r.get("memory_enabled", False)),
                }
            if rows:
                log.info(f"Loaded {len(rows)} accounts from PostgreSQL")
    except Exception as e:
        log.warning(f"Failed to load accounts from DB: {e}")


def _sync_account_tokens(pool_instance=None):
    """Apres connexion d'un worker, verifie si son token doit etre lie a un compte."""
    if pool_instance is None:
        from iamine.pool import pool as pool_instance
    for acc in _accounts.values():
        for wid in acc.get("worker_ids", []):
            api_token = _derive_api_token(wid)
            if api_token in pool_instance.api_tokens:
                pool_instance.api_tokens[api_token]["account_id"] = acc["account_id"]


def _save_accounts():
    """Save to JSON file (sync fallback)."""
    with open(_ACCOUNTS_FILE, "w") as f:
        _j.dump(_accounts, f, indent=2)


async def _save_account_to_db(acc_id: str, pool_instance=None):
    """Persist a single account to PostgreSQL."""
    if pool_instance is None:
        from iamine.pool import pool as pool_instance
    if not hasattr(pool_instance, "store") or not hasattr(pool_instance.store, "pool") or not pool_instance.store.pool:
        return
    acc = _accounts.get(acc_id)
    if not acc:
        return
    try:
        async with pool_instance.store.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO accounts (account_id, email, password_hash, display_name, pseudo,
                                      account_token, eth_address, total_credits, total_earned,
                                      total_spent, worker_ids, created, memory_enabled)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, to_timestamp($12), $13)
                ON CONFLICT (account_id) DO UPDATE SET
                    email=EXCLUDED.email, password_hash=EXCLUDED.password_hash,
                    display_name=EXCLUDED.display_name, pseudo=EXCLUDED.pseudo,
                    account_token=EXCLUDED.account_token, eth_address=EXCLUDED.eth_address,
                    total_credits=EXCLUDED.total_credits, total_earned=EXCLUDED.total_earned,
                    total_spent=EXCLUDED.total_spent, worker_ids=EXCLUDED.worker_ids,
                    memory_enabled=EXCLUDED.memory_enabled
            """,
                acc_id, acc.get("email", ""), acc.get("password_hash", ""),
                acc.get("display_name", ""), acc.get("pseudo", ""),
                acc.get("account_token", ""), acc.get("eth_address"),
                float(acc.get("total_credits", 0) or 0),
                float(acc.get("total_earned", 0) or 0),
                float(acc.get("total_spent", 0) or 0),
                _j.dumps(acc.get("worker_ids", [])),
                float(acc.get("created", 0)),
                bool(acc.get("memory_enabled", False)),
            )
    except Exception as e:
        log.warning(f"Failed to save account {acc_id} to DB: {e}")


async def _seed_user_memory(api_token: str, pseudo: str, pool_instance=None):
    """Stocke le pseudo comme premier fait RAG pour que le LLM connaisse l'utilisateur."""
    if pool_instance is None:
        from iamine.pool import pool as pool_instance
    try:
        from iamine.memory import store_facts
        facts = f"1. L'utilisateur s'appelle {pseudo}\n2. Compte cree sur IAMINE le {time.strftime('%Y-%m-%d')}"
        await store_facts(pool_instance.store, api_token, facts, conv_id="account_init")
        log.info(f"RAG seed: pseudo '{pseudo}' stored for {api_token[:12]}...")
    except Exception as e:
        log.debug(f"RAG seed failed: {e}")


async def _check_admin(request: Request, pool_instance=None) -> str | None:
    """Verifie si l'utilisateur est admin. Retourne l'email ou None."""
    if pool_instance is None:
        from iamine.pool import pool as pool_instance
    # 1) Cookie session_id -> email -> check admin_users
    session_id = request.cookies.get("session_id", "")
    if session_id:
        account_id = _get_session_account(session_id)
        if account_id and account_id in _accounts:
            email = _accounts[account_id].get("email", "")
            if email:
                try:
                    async with pool_instance.store.pool.acquire() as conn:
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


# ---------------------------------------------------------------------------
# Module-level init  - load JSON accounts + migrate pseudos
# ---------------------------------------------------------------------------
_load_accounts()

# Mapping email -> pseudo par defaut, charge depuis env IAMINE_PSEUDO_DEFAULTS
# (JSON string). Utilise uniquement pour migration initiale. Le prod definit
# les pseudos en DB, ce dict est un fallback vide si la variable n est pas set.
import json as _json
_PSEUDO_DEFAULTS = _json.loads(os.environ.get("IAMINE_PSEUDO_DEFAULTS", "{}"))
_migrated = False
for _acc in _accounts.values():
    if "pseudo" not in _acc:
        _email = _acc.get("email", "")
        _acc["pseudo"] = _PSEUDO_DEFAULTS.get(_email, _acc.get("display_name", _email.split("@")[0]))
        _acc["display_name"] = _acc["pseudo"]
        _migrated = True
if _migrated:
    _save_accounts()
