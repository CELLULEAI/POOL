"""Tests P0 securite (audit 2026-06-15) — pool public iamine-work.

Couvre les 3 correctifs critiques :
  - sec-pub-02 : verification de signature de l'id_token Google (anti-forge)
  - sec-pub-03 : hash argon2 des mots de passe admin (fin du plaintext)
  - sec-pub-01/07 : dependance require_admin (401 si non authentifie)

Aucune DB ni reseau : le JWKS Google est mocke par une cle RSA locale.
Lancer :  cd iamine-work && python -m pytest tests/test_p0_security.py -v
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from iamine.routes import admin as admin_mod
from iamine.routes import auth as auth_mod


# ── Helpers JWT/RSA locaux ────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int_b64(n: int) -> str:
    return _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def _make_rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_from_key(key, kid: str) -> dict:
    pub = key.public_key().public_numbers()
    return {"kid": kid, "kty": "RSA", "alg": "RS256",
            "n": _int_b64(pub.n), "e": _int_b64(pub.e)}


def _sign_jwt(key, kid: str, payload: dict) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{_b64url(sig)}"


def _valid_payload(**over) -> dict:
    base = {
        "iss": "https://accounts.google.com",
        "aud": auth_mod.GOOGLE_CLIENT_ID,
        "email": "victime@gmail.com",
        "email_verified": True,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _reset_jwks_cache():
    """Evite la pollution inter-tests du cache JWKS module-level."""
    yield
    auth_mod._GOOGLE_JWKS_CACHE["keys"] = []
    auth_mod._GOOGLE_JWKS_CACHE["fetched_at"] = 0.0


@pytest.fixture
def google_key(monkeypatch):
    """Installe une cle RSA locale comme unique cle JWKS Google."""
    key = _make_rsa_key()
    kid = "test-kid-1"
    monkeypatch.setattr(auth_mod, "_google_jwks", lambda: [_jwk_from_key(key, kid)])
    return key, kid


# ── sec-pub-02 : verification signature OAuth ─────────────────────────────────

def test_oauth_valid_token_accepted(google_key):
    key, kid = google_key
    token = _sign_jwt(key, kid, _valid_payload())
    decoded = auth_mod._verify_google_id_token(token)
    assert decoded["email"] == "victime@gmail.com"


def test_oauth_forged_token_rejected(google_key):
    """LE scenario d'account takeover : token signe par une cle de l'attaquant
    (pas la cle Google), meme kid -> doit etre rejete."""
    _, kid = google_key
    attacker_key = _make_rsa_key()
    token = _sign_jwt(attacker_key, kid, _valid_payload(email="victime@gmail.com"))
    with pytest.raises(Exception):
        auth_mod._verify_google_id_token(token)


def test_oauth_tampered_payload_rejected(google_key):
    key, kid = google_key
    token = _sign_jwt(key, kid, _valid_payload(email="legit@gmail.com"))
    h, p, s = token.split(".")
    forged_payload = _b64url(json.dumps(_valid_payload(email="attacker@evil.com")).encode())
    tampered = f"{h}.{forged_payload}.{s}"
    with pytest.raises(Exception):
        auth_mod._verify_google_id_token(tampered)


def test_oauth_wrong_audience_rejected(google_key):
    key, kid = google_key
    token = _sign_jwt(key, kid, _valid_payload(aud="someone-else.apps.googleusercontent.com"))
    with pytest.raises(ValueError):
        auth_mod._verify_google_id_token(token)


def test_oauth_expired_rejected(google_key):
    key, kid = google_key
    token = _sign_jwt(key, kid, _valid_payload(exp=int(time.time()) - 3600))
    with pytest.raises(ValueError):
        auth_mod._verify_google_id_token(token)


def test_oauth_wrong_issuer_rejected(google_key):
    key, kid = google_key
    token = _sign_jwt(key, kid, _valid_payload(iss="https://evil.example.com"))
    with pytest.raises(ValueError):
        auth_mod._verify_google_id_token(token)


def test_oauth_unknown_kid_rejected(google_key):
    key, _ = google_key
    token = _sign_jwt(key, "unknown-kid", _valid_payload())
    with pytest.raises(ValueError):
        auth_mod._verify_google_id_token(token)


def test_oauth_garbage_token_rejected(google_key):
    with pytest.raises(Exception):
        auth_mod._verify_google_id_token("not-a-jwt")


def test_jwks_stale_while_error(monkeypatch):
    # Cache perime + rafraichissement KO -> on sert les dernieres cles connues
    # (login Google reste fonctionnel pendant une coupure reseau transitoire).
    auth_mod._GOOGLE_JWKS_CACHE["keys"] = [{"kid": "old"}]
    auth_mod._GOOGLE_JWKS_CACHE["fetched_at"] = 0.0
    def _boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(auth_mod.urllib.request, "urlopen", _boom)
    assert auth_mod._google_jwks() == [{"kid": "old"}]


def test_jwks_no_cache_and_fetch_fails_raises(monkeypatch):
    auth_mod._GOOGLE_JWKS_CACHE["keys"] = []
    auth_mod._GOOGLE_JWKS_CACHE["fetched_at"] = 0.0
    def _boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(auth_mod.urllib.request, "urlopen", _boom)
    with pytest.raises(Exception):
        auth_mod._google_jwks()


# ── sec-pub-03 : argon2 mots de passe admin ──────────────────────────────────

def test_admin_password_hash_is_argon2():
    h = admin_mod._hash_admin_password("s3cret-pw")
    assert h.startswith("$argon2")
    assert h != "s3cret-pw"


def test_admin_password_verify_roundtrip():
    h = admin_mod._hash_admin_password("s3cret-pw")
    assert admin_mod._verify_admin_password("s3cret-pw", h) is True
    assert admin_mod._verify_admin_password("wrong", h) is False


def test_admin_password_legacy_plaintext_supported():
    # Anciennes lignes stockees en clair : doivent encore valider (puis migration).
    assert admin_mod._verify_admin_password("plain-old", "plain-old") is True
    assert admin_mod._verify_admin_password("plain-old", "different") is False


def test_admin_password_empty_stored_rejected():
    assert admin_mod._verify_admin_password("whatever", None) is False
    assert admin_mod._verify_admin_password("whatever", "") is False


# ── sec-pub-01/07 : dependance require_admin ─────────────────────────────────

@pytest.mark.asyncio
async def test_require_admin_rejects_anonymous(monkeypatch):
    from fastapi import HTTPException

    async def _no_admin(_req):
        return None
    monkeypatch.setattr(admin_mod, "_check_admin", _no_admin)
    with pytest.raises(HTTPException) as exc:
        await admin_mod.require_admin(object())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_require_admin_allows_admin(monkeypatch):
    async def _is_admin(_req):
        return "admin@cellule.ai"
    monkeypatch.setattr(admin_mod, "_check_admin", _is_admin)
    assert await admin_mod.require_admin(object()) == "admin@cellule.ai"


def test_sensitive_admin_routes_have_require_admin_guard():
    """Garde anti-regression : les routes sensibles /admin/api/* portent bien
    la dependance require_admin (empeche un futur oubli)."""
    guarded = {"/admin/api/worker-cmd", "/admin/api/assign", "/admin/api/set-ctx",
               "/admin/api/set-family", "/admin/api/pool-managed", "/admin/api/migrate-all",
               "/admin/api/capabilities", "/admin/api/alert", "/admin/api/inference-report"}
    seen = {}
    for route in admin_mod.router.routes:
        path = getattr(route, "path", None)
        if path in guarded:
            dep_calls = [d.call for d in route.dependant.dependencies]
            seen[path] = admin_mod.require_admin in dep_calls
    missing = guarded - set(seen)
    assert not missing, f"routes introuvables: {missing}"
    unguarded = [p for p, ok in seen.items() if not ok]
    assert not unguarded, f"routes sans require_admin: {unguarded}"


# ── sec-pub-04 : _check_admin n'accepte plus le token en query param ──────────

class _FakeReq:
    def __init__(self, cookies=None, headers=None, query=None):
        self.cookies = dict(cookies or {})
        self.headers = dict(headers or {})
        self.query_params = dict(query or {})


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cr3t-admin")
    monkeypatch.setattr(admin_mod, "_pool", lambda: object())
    monkeypatch.setattr(admin_mod, "_accounts", lambda: {})


@pytest.mark.asyncio
async def test_check_admin_accepts_bearer_header(admin_env):
    req = _FakeReq(headers={"authorization": "Bearer s3cr3t-admin"})
    assert await admin_mod._check_admin(req) == "admin"


@pytest.mark.asyncio
async def test_check_admin_accepts_cookie(admin_env):
    req = _FakeReq(cookies={"admin_token": "s3cr3t-admin"})
    assert await admin_mod._check_admin(req) == "admin"


@pytest.mark.asyncio
async def test_check_admin_rejects_query_param_token(admin_env):
    # ?token= n'est plus accepte (fuite via logs nginx / referer).
    req = _FakeReq(query={"token": "s3cr3t-admin"})
    assert await admin_mod._check_admin(req) is None


@pytest.mark.asyncio
async def test_check_admin_rejects_wrong_token(admin_env):
    req = _FakeReq(headers={"authorization": "Bearer wrong"})
    assert await admin_mod._check_admin(req) is None


# ── sec-pub-05 : join token opt-in ───────────────────────────────────────────

def test_join_token_open_when_unset(monkeypatch):
    from iamine.routes import websocket as ws_mod
    monkeypatch.delenv("IAMINE_POOL_JOIN_TOKEN", raising=False)
    assert ws_mod._join_token_ok({"worker_id": "x"}) is True


def test_join_token_required_and_matches(monkeypatch):
    from iamine.routes import websocket as ws_mod
    monkeypatch.setenv("IAMINE_POOL_JOIN_TOKEN", "pool-secret")
    assert ws_mod._join_token_ok({"join_token": "pool-secret"}) is True


def test_join_token_required_rejects_missing_or_wrong(monkeypatch):
    from iamine.routes import websocket as ws_mod
    monkeypatch.setenv("IAMINE_POOL_JOIN_TOKEN", "pool-secret")
    assert ws_mod._join_token_ok({"worker_id": "x"}) is False
    assert ws_mod._join_token_ok({"join_token": "wrong"}) is False


# ── sec-pub-06 : routes /v1/dev/* gardees par require_admin ───────────────────

def test_dev_routes_have_require_admin_guard():
    from iamine.routes import dev as dev_mod
    paths = {"/v1/dev/backup", "/v1/dev/signal", "/v1/dev/inbox"}
    seen = set()
    for route in dev_mod.router.routes:
        if getattr(route, "path", None) in paths:
            dep_calls = [d.call for d in route.dependant.dependencies]
            assert dev_mod.require_admin in dep_calls, f"{route.path} sans garde admin"
            seen.add(route.path)
    assert seen == paths, f"routes dev manquantes: {paths - seen}"


# ── sec-pub-09/10 : client_ip + throttle login ───────────────────────────────

class _Client:
    def __init__(self, host):
        self.host = host


def test_client_ip_prefers_cf_connecting_ip():
    from iamine.core import credits as credits_mod
    req = _FakeReq(headers={"cf-connecting-ip": "1.2.3.4", "x-real-ip": "9.9.9.9",
                            "x-forwarded-for": "8.8.8.8"})
    assert credits_mod.client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_in_order():
    from iamine.core import credits as credits_mod
    assert credits_mod.client_ip(_FakeReq(headers={"x-real-ip": "9.9.9.9"})) == "9.9.9.9"
    assert credits_mod.client_ip(_FakeReq(headers={"x-forwarded-for": "8.8.8.8, 7.7.7.7"})) == "8.8.8.8"
    req = _FakeReq()
    req.client = _Client("5.5.5.5")
    assert credits_mod.client_ip(req) == "5.5.5.5"
    assert credits_mod.client_ip(_FakeReq()) == "unknown"


def test_login_throttle_blocks_after_max():
    from iamine.core import credits as credits_mod
    key = "test-ip-throttle"
    credits_mod.clear_login_failures(key)
    try:
        for _ in range(credits_mod.LOGIN_MAX_FAILURES):
            assert credits_mod.is_login_blocked(key) is False
            credits_mod.register_login_failure(key)
        assert credits_mod.is_login_blocked(key) is True
        credits_mod.clear_login_failures(key)
        assert credits_mod.is_login_blocked(key) is False
    finally:
        credits_mod.clear_login_failures(key)


# ── sec-pub-09 : verrou anti brute-force du code d'activation ─────────────────

# ── sec-pub-13 : client MCP verifie le TLS par defaut ────────────────────────

def test_mcp_tls_verify_secure_by_default(monkeypatch):
    try:
        from iamine import mcp_server as mcp_mod
    except Exception:
        pytest.skip("mcp_server indisponible (dependance 'mcp' absente)")
    monkeypatch.delenv("IAMINE_MCP_CA", raising=False)
    monkeypatch.delenv("IAMINE_MCP_INSECURE", raising=False)
    assert mcp_mod._tls_verify() is True
    monkeypatch.setenv("IAMINE_MCP_CA", "/etc/ssl/ca.pem")
    assert mcp_mod._tls_verify() == "/etc/ssl/ca.pem"
    monkeypatch.delenv("IAMINE_MCP_CA", raising=False)
    monkeypatch.setenv("IAMINE_MCP_INSECURE", "1")
    assert mcp_mod._tls_verify() is False


# ── sec-pub-14 : /v1/contact borne + rate-limite ─────────────────────────────

@pytest.mark.asyncio
async def test_contact_rate_limited_and_requires_message():
    from iamine.routes import static as static_mod
    ip = "203.0.113.7"
    req = _FakeReq(headers={"cf-connecting-ip": ip})
    # quota atteint -> 429 (aucune ecriture fichier)
    static_mod._CONTACT_HITS[ip] = [time.time()] * static_mod._CONTACT_MAX
    r = await static_mod.contact({"message": "hello"}, req)
    assert r.status_code == 429
    static_mod._CONTACT_HITS.pop(ip, None)
    # message vide -> 400 (retour avant ecriture)
    r2 = await static_mod.contact({"message": "   "}, req)
    assert r2.status_code == 400
    static_mod._CONTACT_HITS.pop(ip, None)


# ── sec-pub-08 (Phase 1) : token de compte aleatoire ─────────────────────────

def test_new_account_token_is_random():
    t1 = auth_mod._new_account_token()
    t2 = auth_mod._new_account_token()
    assert t1.startswith("acc_") and len(t1) == 4 + 32
    assert t1 != t2  # aleatoire, plus derive de l'email (non forgeable)


@pytest.mark.asyncio
async def test_activate_code_lockout(monkeypatch):
    acc = {"email": "u@x.com", "email_verified": False,
           "verification_code": "123456",
           "verification_expires": int(time.time()) + 600}
    accounts = {"aid1": acc}
    monkeypatch.setattr(auth_mod, "_accounts", lambda: accounts)
    monkeypatch.setattr(auth_mod, "_save_accounts", lambda: None)
    monkeypatch.setattr(auth_mod, "_pool", lambda: object())
    # 4 essais errones -> 400, le code reste valide
    for _ in range(4):
        r = await auth_mod.auth_activate({"email": "u@x.com", "code": "000000"})
        assert r.status_code == 400
    assert acc["verification_code"] == "123456"
    # 5e essai -> 429 et code invalide (force un renvoi)
    r = await auth_mod.auth_activate({"email": "u@x.com", "code": "000000"})
    assert r.status_code == 429
    assert acc["verification_code"] is None
