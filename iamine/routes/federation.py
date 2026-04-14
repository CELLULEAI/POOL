"""Federation endpoints — /v1/federation/*.

## Endpoints MAP (M3 → M6)

**PUBLIC unsigned** (no signature, no admin token):
- GET  /v1/federation/info       — public identity card
- GET  /v1/federation/molecule   — trusted_peers list (M7b discovery)

**SIGNED** (envelope Ed25519, verified via enforce_fed_policy):
- POST /v1/federation/handshake  — peer submits signed self-declaration
- POST /v1/federation/verify     — signed challenge/response for liveness

**ADMIN** (admin_token via cookie or query param):
- GET  /v1/federation/peers              — list known peers
- GET  /v1/federation/peers/{atom_id}    — peer detail
- GET  /v1/federation/heartbeat          — heartbeat metrics
- POST /v1/federation/admin/register     — CLI register (Modèle B)
- POST /v1/federation/peers/{id}/promote — raise trust level
- POST /v1/federation/peers/{id}/demote  — lower trust level
- POST /v1/federation/peers/{id}/revoke  — revoke peer

## M6 enforcement

Per-route helper fed.enforce_fed_policy() = kill switch FS + mode + signature.
Appelé par /handshake et /verify. Les routes admin utilisent _check_admin
indépendamment. Les routes PUBLIC ne passent PAS par enforce (seulement guard off).

AVANT ajout d'un endpoint : décider explicitement PUBLIC / SIGNED / ADMIN.
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..core import federation as fed

router = APIRouter()
log = logging.getLogger("iamine.federation")


def _pool():
    from iamine.pool import pool
    return pool


def _self_info_card(pool) -> dict:
    ident = getattr(pool, "federation_self", None)
    if ident is None:
        return {"mode": "off"}
    # Live capabilities from currently connected workers. Fallback to the
    # static DB column federation_self.capabilities only if live is empty.
    live_caps = fed.compute_live_capabilities(pool)
    capabilities = live_caps if live_caps else (ident.capabilities or [])
    return {
        "mode": getattr(pool, "federation_mode", fed.FED_MODE_OFF),
        "atom_id": ident.atom_id,
        "name": ident.name,
        "pubkey_hex": ident.pubkey.hex(),
        "url": ident.url,
        "molecule_id": ident.molecule_id,
        "capabilities": capabilities,
        "hop_max": fed.HOP_MAX,
        "nonce_window_sec": fed.NONCE_WINDOW_SEC,
        "schema": 1,
    }


def _is_off(pool) -> bool:
    return getattr(pool, "federation_mode", fed.FED_MODE_OFF) == fed.FED_MODE_OFF


def _is_active(pool) -> bool:
    return getattr(pool, "federation_mode", fed.FED_MODE_OFF) == fed.FED_MODE_ACTIVE




async def _read_pool_config_bool(pool, key: str, default: bool = True) -> bool:
    """Read a boolean flag from pool_config with fallback on default.

    Used to honor pool-operator toggles (accept_forwarding, publish_capabilities)
    configured via admin_pool.html.
    """
    try:
        if not (hasattr(pool, "store") and hasattr(pool.store, "pool")):
            return default
        async with pool.store.pool.acquire() as conn:
            v = await conn.fetchval(
                "SELECT value FROM pool_config WHERE key=$1", key)
        if v is None:
            return default
        return str(v).strip().lower() == "true"
    except Exception:
        return default


# ─── GET /v1/federation/info (unsigned) ──────────────────────────────────────

@router.get("/v1/federation/info")
async def federation_info():
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"mode": "off"}, status_code=503)

    card = _self_info_card(pool)

    # Pool-operator toggle: hide capabilities if publish_capabilities=false
    if not await _read_pool_config_bool(pool, "publish_capabilities", default=True):
        # Keep the identity card (needed for handshake) but strip capabilities
        if isinstance(card, dict):
            card = dict(card)
            card["capabilities"] = []
            card["private"] = True
    return card


# ─── GET /v1/federation/pools (public, unsigned) ─────────────────────────────
# Read-only public view of the federated network for the iamine.org landing
# page. Returns self identity card + list of non-revoked peers with public
# fields only. No admin token required. Safe to expose : names, atom_ids,
# URLs, molecule_ids, trust levels, last_seen, live capabilities. Excludes
# added_at and revoked_at (internal bookkeeping).

async def _fetch_peer_live_info(peer_url: str, timeout_sec: float = 2.0):
    """Best-effort GET <peer_url>/v1/federation/info. Returns dict or None on failure."""
    import aiohttp as _aiohttp
    try:
        base = (peer_url or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            return None
        _timeout = _aiohttp.ClientTimeout(total=timeout_sec)
        async with _aiohttp.ClientSession(timeout=_timeout) as _s:
            async with _s.get(base + "/v1/federation/info") as _r:
                if _r.status != 200:
                    return None
                return await _r.json()
    except Exception:
        return None


@router.get("/v1/federation/pools")
async def federation_pools():
    pool = _pool()
    if _is_off(pool):
        return {"mode": "off", "self": None, "peers": [], "count": 0}

    peers_rows = await fed.list_peers(pool, include_revoked=False)
    import json as _json
    import asyncio as _asyncio

    # Stale caps from DB (handshake time snapshot)
    peers_draft = []
    for p in peers_rows:
        caps = p.get("capabilities") or []
        if isinstance(caps, str):
            try:
                caps = _json.loads(caps)
            except Exception:
                caps = []
        # Redact peer URL for public endpoint — hides operator IP/host.
        # Admin endpoint /v1/federation/peers still exposes the full URL.
        peer_url_raw = p.get("url") or ""
        if peer_url_raw.startswith("https://"):
            peer_url_public = "https://<hidden>"
        elif peer_url_raw.startswith("http://"):
            peer_url_public = "http://<hidden>"
        else:
            peer_url_public = "<hidden>"
        peers_draft.append({
            "atom_id": p["atom_id"],
            "name": p["name"],
            "url": peer_url_public,
            "molecule_id": p.get("molecule_id"),
            "capabilities": caps,
            "trust_level": p["trust_level"],
            "last_seen": p["last_seen"].isoformat() if p.get("last_seen") else None,
        })

    # Live sub-fetch with bounded parallelism. Uses per-peer 2s timeout so
    # a slow/down peer cannot stall the whole response.
    if peers_draft:
        live_results = await _asyncio.gather(
            *[_fetch_peer_live_info(p["url"]) for p in peers_draft],
            return_exceptions=True,
        )
        for i, live in enumerate(live_results):
            if isinstance(live, dict):
                live_caps = live.get("capabilities") or []
                if live_caps:
                    peers_draft[i]["capabilities"] = live_caps
                # Refresh name from live /info in case peer renamed post-handshake
                if live.get("name"):
                    peers_draft[i]["name"] = live["name"]

    return {
        "mode": getattr(pool, "federation_mode", "off"),
        "self": _self_info_card(pool),
        "peers": peers_draft,
        "count": len(peers_draft),
    }

@router.post("/v1/federation/handshake")
async def federation_handshake(request: Request):
    pool = _pool()

    raw_body = await request.body()
    try:
        import json as _json
        payload = _json.loads(raw_body.decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    required = ("atom_id", "name", "pubkey_hex", "url")
    for k in required:
        if k not in payload:
            return JSONResponse({"error": f"missing field: {k}"}, status_code=400)

    try:
        peer_pubkey = bytes.fromhex(payload["pubkey_hex"])
    except ValueError:
        return JSONResponse({"error": "invalid pubkey_hex"}, status_code=400)
    if len(peer_pubkey) != 32:
        return JSONResponse({"error": "pubkey must be 32 bytes"}, status_code=400)

    # atom_id must match sha256(pubkey) — prevents impersonation
    expected_id = fed._atom_id_from_pubkey(peer_pubkey)
    if expected_id != payload["atom_id"]:
        return JSONResponse(
            {"error": "atom_id does not match pubkey fingerprint"},
            status_code=400,
        )

    # M6 — unified enforcement : kill switch + mode + signature verify ONCE.
    # peer_pubkey vient du body (first-contact handshake).
    reject, sig_ok = await fed.enforce_fed_policy(
        pool, request,
        method="POST", path="/v1/federation/handshake",
        body=raw_body,
        peer_pubkey=peer_pubkey,
        require_signature=True,
    )
    if reject:
        return JSONResponse({"error": reject["error"]}, status_code=reject["status_code"])

    # observe mode: sig_ok=False mais request passée → trust_level=0
    trust_after = 1 if sig_ok else 0

    # Self cannot handshake with self
    self_id = pool.federation_self.atom_id if pool.federation_self else None
    if payload["atom_id"] == self_id:
        return JSONResponse({"error": "cannot handshake with self"}, status_code=400)

    # M5 — idempotence : check if peer already known L1+ (influences added_by + reciprocation)
    existing_peer = await fed.load_peer(pool, payload["atom_id"])
    already_known_l1plus = (
        existing_peer is not None
        and existing_peer.get("trust_level", 0) >= 1
        and existing_peer.get("revoked_at") is None
    )

    await fed.upsert_peer(
        pool,
        atom_id=payload["atom_id"],
        name=payload["name"],
        pubkey=peer_pubkey,
        url=payload["url"],
        molecule_id=payload.get("molecule_id"),
        capabilities=payload.get("capabilities") or [],
        trust_level=trust_after,
        added_by="handshake_reciprocal" if already_known_l1plus else "handshake_initial",
    )

    # M5 — honor request_reciprocation via background fire-and-forget.
    # Cap global by semaphore. Skip if peer already L1+ (idempotence : évite A→B→A→B infini).
    reciprocate_requested = bool(payload.get("request_reciprocation"))
    reciprocate_scheduled = False
    if reciprocate_requested and sig_ok and not already_known_l1plus:
        import asyncio as _asyncio

        peer_url = payload["url"]
        peer_name = payload["name"]
        peer_atom_short = payload["atom_id"][:16]

        async def _reverse_handshake():
            sem = fed._get_reciprocation_sem()
            async with sem:
                try:
                    result = await fed.outbound_handshake_to(
                        pool, peer_url, reciprocate=False, name_hint=peer_name,
                    )
                    log.info(
                        f"reciprocation back→{peer_atom_short}... ok={result['ok']} "
                        f"msg={result.get('message', '')[:120]}"
                    )
                except Exception as _e:
                    log.warning(f"reciprocation back-handshake crashed: {_e}")

        _asyncio.create_task(_reverse_handshake())
        reciprocate_scheduled = True
        log.info(f"reciprocation scheduled back to {peer_name!r}")

    log.info(
        f"handshake peer={payload['atom_id'][:16]}... name={payload['name']!r} "
        f"trust={trust_after} sig_ok={sig_ok} reciprocate={reciprocate_scheduled}"
    )
    return {
        "ok": True,
        "peer_trust_level": trust_after,
        "signature_verified": sig_ok,
        "reciprocation_scheduled": reciprocate_scheduled,
        "already_known": already_known_l1plus,
        "self": _self_info_card(pool),
    }


# ─── POST /v1/federation/verify (signed) ─────────────────────────────────────

@router.post("/v1/federation/verify")
async def federation_verify(request: Request):
    """Challenge-response liveness probe.

    Body: {atom_id, challenge_b64}
    Returns: {ok, echo, signed_response_b64}
    We sign `sha256(challenge || our_atom_id)` with our privkey.
    Caller verifies with the pubkey from our /info card.
    """
    import hashlib as _h
    pool = _pool()

    raw_body = await request.body()
    try:
        import json as _json
        payload = _json.loads(raw_body.decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    peer_id = payload.get("atom_id")
    challenge_b64 = payload.get("challenge_b64")
    if not (peer_id and challenge_b64):
        return JSONResponse({"error": "missing atom_id or challenge_b64"}, status_code=400)

    peer = await fed.load_peer(pool, peer_id)
    if not peer:
        return JSONResponse({"error": "unknown peer"}, status_code=404)
    if peer.get("revoked_at") is not None:
        return JSONResponse({"error": "peer revoked"}, status_code=403)

    # M6 — unified enforcement using the peer's pubkey from DB
    reject, sig_ok = await fed.enforce_fed_policy(
        pool, request,
        method="POST", path="/v1/federation/verify",
        body=raw_body,
        peer_pubkey=bytes(peer["pubkey"]),
        require_signature=True,
    )
    if reject:
        return JSONResponse({"error": reject["error"]}, status_code=reject["status_code"])

    try:
        challenge = base64.b64decode(challenge_b64)
    except Exception:
        return JSONResponse({"error": "invalid challenge_b64"}, status_code=400)

    self_ident = pool.federation_self
    if self_ident is None:
        return JSONResponse({"error": "self identity not initialized"}, status_code=503)

    priv_raw = fed._load_privkey_from_disk(
        __import__("pathlib").Path(self_ident.privkey_path)
    )
    if priv_raw is None:
        return JSONResponse({"error": "privkey unavailable"}, status_code=500)

    to_sign = _h.sha256(challenge + self_ident.atom_id.encode()).digest()
    signature = fed.sign(priv_raw, to_sign)
    return {
        "ok": True,
        "echo_challenge_b64": challenge_b64,
        "signed_response_b64": base64.b64encode(signature).decode(),
        "self_atom_id": self_ident.atom_id,
        "signature_verified_inbound": sig_ok,
    }


# ─── GET /v1/federation/molecule (public, unsigned) ─────────────────────────
# M7b — worker discovery. Exposes {self, trusted_peers} filtré trust_level>=2
# et reachable (last_seen < PEER_UNREACHABLE_AFTER_SEC). Ordre last_seen DESC,
# intentionally NOT hierarchical. `self` exposed identically to peers.

@router.get("/v1/federation/molecule")
async def federation_molecule():
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"mode": "off"}, status_code=503)

    import json as _json
    peers_rows = await fed.list_molecule_peers(pool, min_trust=2)
    trusted = []
    for p in peers_rows:
        caps = p.get("capabilities") or []
        if isinstance(caps, str):
            try:
                caps = _json.loads(caps)
            except Exception:
                caps = []
        trusted.append({
            "atom_id": p["atom_id"],
            "name": p["name"],
            "url": p["url"],
            "molecule_id": p.get("molecule_id"),
            "capabilities": caps,
            "last_seen": p["last_seen"].isoformat() if p.get("last_seen") else None,
        })

    # self exposed identique aux peers (pas de marker "principal").
    # Capabilities = live from current workers (fallback: static DB column).
    ident = pool.federation_self
    live_caps_self = fed.compute_live_capabilities(pool)
    self_caps = live_caps_self if live_caps_self else (ident.capabilities or [])
    self_entry = {
        "atom_id": ident.atom_id,
        "name": ident.name,
        "url": ident.url,
        "molecule_id": ident.molecule_id,
        "capabilities": self_caps,
        "last_seen": None,
    }
    return {
        "mode": getattr(pool, "federation_mode", "off"),
        "molecule_id": ident.molecule_id,
        "self": self_entry,
        "trusted_peers": trusted,
        "trusted_peers_count": len(trusted),
        "hop_max": fed.HOP_MAX,
    }


# ─── GET /v1/federation/heartbeat (admin diag) ───────────────────────────────

@router.get("/v1/federation/heartbeat")
async def federation_heartbeat_status(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return {"mode": "off"}
    from ..core.federation_heartbeat import get_heartbeat_metrics
    return {
        "mode": getattr(pool, "federation_mode", "off"),
        **get_heartbeat_metrics(),
    }


# ─── GET /v1/federation/peers (admin) ────────────────────────────────────────

@router.get("/v1/federation/peers")
async def federation_peers(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)

    pool = _pool()
    if _is_off(pool):
        return {"mode": "off", "peers": []}

    include_revoked = request.query_params.get("include_revoked", "").lower() in ("1", "true", "yes")
    peers = await fed.list_peers(pool, include_revoked=include_revoked)

    # Serialize safely (bytes, datetime)
    import json as _json
    out = []
    for p in peers:
        caps = p.get("capabilities") or []
        if isinstance(caps, str):
            try:
                caps = _json.loads(caps)
            except Exception:
                caps = []
        out.append({
            "atom_id": p["atom_id"],
            "name": p["name"],
            "url": p["url"],
            "molecule_id": p.get("molecule_id"),
            "capabilities": caps,
            "trust_level": p["trust_level"],
            "last_seen": p["last_seen"].isoformat() if p.get("last_seen") else None,
            "added_at": p["added_at"].isoformat() if p.get("added_at") else None,
            "revoked_at": p["revoked_at"].isoformat() if p.get("revoked_at") else None,
        })
    return {
        "mode": getattr(pool, "federation_mode", "off"),
        "self": _self_info_card(pool),
        "peers": out,
        "count": len(out),
    }


# ─── POST /v1/federation/job (M7a, SIGNED, trust>=2) ────────────────────────
# Reçoit un job forwardé d'un peer bonded. Exécute localement via submit_job.
# worker_sig=NULL dans le ledger (M7-worker backfill dormant jusqu'à M9b).

@router.post("/v1/federation/job")
async def federation_job(request: Request):
    pool = _pool()

    # Pool-operator toggle: respect accept_forwarding
    if not await _read_pool_config_bool(pool, "accept_forwarding", default=True):
        return JSONResponse(
            {"error": "this pool does not accept forwarded jobs",
             "reason": "operator disabled accept_forwarding"},
            status_code=503)

    raw_body = await request.body()
    try:
        import json as _json
        payload = _json.loads(raw_body.decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    required = ("origin_pool_id", "origin_request_id", "messages")
    for k in required:
        if k not in payload:
            return JSONResponse({"error": f"missing field: {k}"}, status_code=400)

    # Double-safety hop check (belt and suspenders over R1 envelope check)
    hop_hdr = request.headers.get("x-iamine-hop") or request.headers.get("X-IAMINE-Hop") or "0"
    try:
        hop_val = int(hop_hdr)
    except ValueError:
        return JSONResponse({"error": "invalid hop"}, status_code=400)
    if hop_val >= fed.HOP_MAX:
        return JSONResponse(
            {"error": f"hop {hop_val} at max {fed.HOP_MAX}, refuse forward"},
            status_code=409,
        )

    # Origin peer must exist + be trusted >= 2
    origin_pool_id = payload["origin_pool_id"]
    origin_peer = await fed.load_peer(pool, origin_pool_id)
    if not origin_peer:
        return JSONResponse({"error": "unknown origin peer"}, status_code=403)
    if origin_peer.get("trust_level", 0) < 2:
        return JSONResponse({"error": "origin peer not trusted >=2"}, status_code=403)
    if origin_peer.get("revoked_at") is not None:
        return JSONResponse({"error": "origin peer revoked"}, status_code=403)

    # M6 enforcement using origin peer's pubkey from DB
    reject, sig_ok = await fed.enforce_fed_policy(
        pool, request,
        method="POST", path="/v1/federation/job",
        body=raw_body,
        peer_pubkey=bytes(origin_peer["pubkey"]),
        require_signature=True,
    )
    if reject:
        return JSONResponse({"error": reject["error"]}, status_code=reject["status_code"])

    model = payload.get("model") or None
    messages = payload["messages"]
    max_tokens = int(payload.get("max_tokens", 512))
    conv_id = payload.get("conv_id") or f"fed_{origin_pool_id[:8]}_{payload['origin_request_id']}"

    log.info(
        f"federation/job received: origin={origin_pool_id[:16]}... "
        f"req={payload['origin_request_id']} model={model} hop={hop_val} sig_ok={sig_ok}"
    )

    # Execute locally
    try:
        result = await pool.submit_job(
            messages, max_tokens,
            conv_id=conv_id,
            requested_model=model,
            api_token=None,
            tools=None,
        )
    except Exception as e:
        log.warning(f"federation/job local exec failed: {e}")
        return JSONResponse(
            {"error": f"exec failed: {str(e)[:200]}"},
            status_code=502,
        )

    # Ledger row (we are exec pool, worker_sig=NULL pending M7-worker)
    try:
        from ..core import revenue as rev
        import uuid as _uuid
        job_id = f"fedjob_{_uuid.uuid4().hex[:12]}"
        tokens_out = int(result.get("tokens_generated", 0) or result.get("tokens", 0) or 0)
        tokens_in = 0
        credits_total = max(tokens_out, 1)
        await rev.write_forward_entry(
            pool,
            job_id=job_id,
            origin_pool_id=origin_pool_id,
            exec_pool_id=pool.federation_self.atom_id,
            worker_id=result.get("worker_id", "unknown"),
            model=model or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            credits_total=credits_total,
            forward_chain=[origin_pool_id],
        )
    except Exception as e:
        log.warning(f"federation/job ledger write failed (non-fatal): {e}")

    # Q4 observer hook (scaffold phase 1) : log sample-eligible jobs without
    # raising disputes. Phase 2 will replace with real record_dispute + HTTP
    # cross-pool verifier call. Gated on DISPUTE_SAMPLING_ENABLED inside the
    # helper — no-op when disabled.
    try:
        from ..core import federation_disputes as _disp
        _disp.sample_and_log_forwarded(
            job_id=job_id,
            origin_pool_id=origin_pool_id,
            exec_pool_id=pool.federation_self.atom_id,
        )
    except Exception as _obs_err:
        log.warning(f"federation/job dispute observer failed (non-fatal): {_obs_err}")

    return {
        "ok": True,
        "response": result.get("text") or result.get("response") or result.get("content") or "",
        "worker_id": result.get("worker_id"),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "exec_pool_id": pool.federation_self.atom_id,
        "origin_request_id": payload["origin_request_id"],
        "worker_sig": None,
    }


# ─── POST /v1/federation/admin/register (M5, admin, Modèle B) ────────────────
# CLI `iamine pool register <url>` passe par cet endpoint. Le pool drive le
# handshake sortant (no privkey duplication, no secret dans le CLI).

@router.post("/v1/federation/admin/register")
async def federation_admin_register(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)

    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    import json as _json
    raw = await request.body()
    try:
        payload = _json.loads(raw.decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    target_url = (payload.get("url") or "").strip()
    if not target_url:
        return JSONResponse({"error": "missing field: url"}, status_code=400)
    reciprocate = bool(payload.get("reciprocate", False))
    name_hint = payload.get("name") or None

    client_host = request.client.host if request.client else "unknown"
    log.info(
        f"admin register: url={target_url} reciprocate={reciprocate} "
        f"initiator_ip={client_host}"
    )

    result = await fed.outbound_handshake_to(
        pool, target_url, reciprocate=reciprocate, name_hint=name_hint,
    )
    status_code = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status_code)


# ─── POST /v1/federation/peers/{atom_id}/promote|demote|revoke (M5) ─────────

async def _parse_target_level(request: Request, default: int) -> int:
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.body()
            if body:
                import json as _json
                data = _json.loads(body.decode())
                return int(data.get("target_level", default))
    except Exception:
        pass
    return default


@router.post("/v1/federation/peers/{atom_id}/promote")
async def federation_peer_promote(atom_id: str, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    target_level = await _parse_target_level(request, default=2)
    ok, msg = await fed.promote_peer(pool, atom_id, target_level)
    status_code = 200 if ok else 400
    return JSONResponse({"ok": ok, "message": msg, "atom_id": atom_id}, status_code=status_code)


@router.post("/v1/federation/peers/{atom_id}/demote")
async def federation_peer_demote(atom_id: str, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    target_level = await _parse_target_level(request, default=1)
    ok, msg = await fed.demote_peer(pool, atom_id, target_level)
    status_code = 200 if ok else 400
    return JSONResponse({"ok": ok, "message": msg, "atom_id": atom_id}, status_code=status_code)


@router.post("/v1/federation/peers/{atom_id}/revoke")
async def federation_peer_revoke(atom_id: str, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    ok, msg = await fed.revoke_peer(pool, atom_id)
    status_code = 200 if ok else 400
    return JSONResponse({"ok": ok, "message": msg, "atom_id": atom_id}, status_code=status_code)


# ─── M7-server-partial : workers_certs endpoints (admin) ────────────────────
# Enroll / revoke / list worker certs. Scaffold pour M7-worker (wheel future).
# Append-only ledger invariant : revoke NE CASCADE PAS sur revenue_ledger.

@router.post("/v1/federation/workers/enroll")
async def federation_worker_enroll(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    worker_id = payload.get("worker_id")
    pubkey_hex = payload.get("pubkey_hex")
    if not worker_id or not pubkey_hex:
        return JSONResponse({"error": "missing worker_id or pubkey_hex"}, status_code=400)
    try:
        pubkey = bytes.fromhex(pubkey_hex)
    except ValueError:
        return JSONResponse({"error": "invalid pubkey_hex"}, status_code=400)
    if len(pubkey) != 32:
        return JSONResponse({"error": "pubkey must be 32 bytes"}, status_code=400)

    try:
        cert = await fed.enroll_worker_cert(pool, worker_id, pubkey)
    except Exception as e:
        return JSONResponse({"error": f"enroll failed: {e}"}, status_code=500)

    log.info(f"admin enroll worker_id={worker_id} cert_id={cert['cert_id']}")
    return cert


@router.post("/v1/federation/workers/certs/{worker_id}/revoke")
async def federation_worker_cert_revoke(worker_id: str, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    ok, msg = await fed.revoke_worker_cert(pool, worker_id)
    status_code = 200 if ok else 400
    return JSONResponse({"ok": ok, "message": msg, "worker_id": worker_id}, status_code=status_code)


@router.get("/v1/federation/workers/certs")
async def federation_worker_certs_list(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return {"mode": "off", "certs": []}
    include_revoked = request.query_params.get("include_revoked", "").lower() in ("1", "true", "yes")
    certs = await fed.list_worker_certs(pool, include_revoked=include_revoked)
    return {"count": len(certs), "certs": certs}


# ─── M10-active Q7 consumer : user-session JWT mint (public) ──────────────
# Bridge between the existing session-based auth (routes/auth.py) and the
# cross-pool JWT mint helper. A user authenticated on THIS pool can request
# a JWT that they can later present to ANOTHER bonded pool as proof of identity
# (the other pool will verify with our atom_id pubkey via federation_peers).
#
# This closes the Q7 loop :
# - Admin mint endpoint : /v1/federation/accounts/jwt/mint (existing, admin gate)
# - User mint endpoint  : /v1/federation/accounts/jwt/mint-self (THIS, session gate)
# - Public verify       : /v1/federation/accounts/jwt/verify (existing, no gate)
#
# Phase 1 scope : JWT carries identity only (account_id + email). No credits
# transport, no role claims. See project_decisions_a_tranchees.md Q7.

@router.post("/v1/federation/accounts/jwt/mint-self")
async def federation_account_jwt_mint_self(request: Request):
    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    # Accept session_id from body OR cookie (matches routes/auth.py pattern)
    session_id = payload.get("session_id") or request.cookies.get("session_id", "")
    if not session_id:
        return JSONResponse({"error": "missing session_id"}, status_code=401)

    # Resolve session -> account_id via existing pool helper
    from iamine.pool import _get_session_account, _accounts as _pool_accounts
    account_id = _get_session_account(session_id)
    if not account_id:
        return JSONResponse({"error": "invalid session"}, status_code=401)
    if account_id not in _pool_accounts:
        return JSONResponse({"error": "account not found"}, status_code=404)

    account = _pool_accounts[account_id]
    email = account.get("email", "")

    # TTL : default 1h, user can request shorter but not longer than 1 day
    ttl_sec = payload.get("ttl_sec", 3600)
    try:
        ttl_sec = int(ttl_sec)
    except (TypeError, ValueError):
        return JSONResponse({"error": "ttl_sec must be integer"}, status_code=400)
    if ttl_sec <= 0 or ttl_sec > 86400:
        return JSONResponse({"error": "ttl_sec must be in (0, 86400]"}, status_code=400)

    try:
        minted = fed.sign_account_jwt(pool, account_id, email=email, ttl_sec=ttl_sec)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.error(f"mint-self failed for account {account_id[:16]}...: {e}")
        return JSONResponse({"error": f"mint failed: {e}"}, status_code=500)

    log.info(f"jwt mint-self : account={account_id[:16]}... ttl={ttl_sec}s issued")
    return minted


# ─── M10-active Q7 consumer : cross-pool session from JWT (public) ──────
# Close the Q7 federation loop. A user holding a valid JWT (minted on any
# bonded pool, including self) exchanges it for a LOCAL session on THIS pool.
#
# Phase 1 matching : by EMAIL ONLY. The JWT payload carries email alongside
# account_id; account_ids are LOCAL to each pool, so cross-pool account_id
# matching is meaningless. Email is the stable identifier across pools.
#
# Phase 1 scope (explicit NOT) :
#   - NO shadow account auto-creation : if email is unknown locally, return 404
#     and let the user register first. Phase 2 may auto-create.
#   - NO credits transport : local account keeps its local credits, the JWT
#     grants only identity+session.
#   - NO admin claims : admin status is per-pool.
#
# Guardian invariant "no master pool" preserved : any bonded peer can issue
# JWTs, pool B trusts the signature via federation_peers.pubkey (no central
# registrar). Self-issued JWTs are accepted via the pubkey shortcut.

@router.post("/v1/federation/accounts/jwt/session")
async def federation_account_jwt_session(request: Request):
    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    token = payload.get("token")
    if not token:
        return JSONResponse({"error": "missing token"}, status_code=400)

    # Verify JWT via existing helper (honors expiry, alg/typ, signature)
    verdict = await fed.verify_account_jwt(pool, token)
    if not verdict.get("valid"):
        reason = verdict.get("reason", "invalid token")
        return JSONResponse({"error": reason}, status_code=401)

    jwt_payload = verdict.get("payload", {})
    jwt_email = jwt_payload.get("email", "").strip().lower()
    jwt_sub = jwt_payload.get("sub", "")
    jwt_iss = jwt_payload.get("iss", "")

    if not jwt_email:
        return JSONResponse(
            {"error": "JWT has no email claim, cannot match local account"},
            status_code=400,
        )

    # Lookup local account by email (phase 1 matching)
    from iamine.pool import _accounts as _pool_accounts, _create_session
    local_account_id = None
    for acc_id, acc in _pool_accounts.items():
        if acc.get("email", "").strip().lower() == jwt_email:
            local_account_id = acc_id
            break

    if not local_account_id:
        return JSONResponse(
            {
                "error": "no local account matches JWT email",
                "hint": "register first on this pool with the same email",
                "jwt_email": jwt_email,
                "jwt_issuer_atom_id": jwt_iss,
            },
            status_code=404,
        )

    # Create a local session bound to the local account
    local_session_id = _create_session(local_account_id)

    log.info(
        f"jwt/session : iss={jwt_iss[:16]}... email={jwt_email} "
        f"local_account={local_account_id[:16]}... session issued"
    )

    return {
        "session_id": local_session_id,
        "account_id": local_account_id,
        "email": jwt_email,
        "jwt_issuer_atom_id": jwt_iss,
        "jwt_sub": jwt_sub,
        "cross_pool": jwt_iss != (pool.federation_self.atom_id if getattr(pool, "federation_self", None) else ""),
        "note": "phase 1 identity-only exchange, credits remain per-pool",
    }


# ─── M11.1 admin : account replication log readonly ──────────────────
# Read-only view on the most recent account_replication_log rows for the
# admin federation dashboard. Requires admin token. Returns the N most
# recent entries ordered by created_at DESC. Used to show which accounts
# were pushed/received from which peers.

@router.get("/v1/federation/accounts/replication-log")
async def federation_account_replication_log(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)

    pool = _pool()
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"rows": [], "count": 0}

    limit = 50
    try:
        qp_limit = request.query_params.get("limit")
        if qp_limit:
            limit = max(1, min(500, int(qp_limit)))
    except (ValueError, TypeError):
        limit = 50

    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, account_id, peer_atom_id, direction, status,
                       error_message, created_at
                FROM account_replication_log
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
    except Exception as e:
        return JSONResponse({"error": f"db: {str(e)[:200]}"}, status_code=500)

    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "account_id": r["account_id"],
            "peer_atom_id": r["peer_atom_id"],
            "direction": r["direction"],
            "status": r["status"],
            "error_message": r["error_message"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return {"rows": out, "count": len(out)}


# ─── M11.1 : account ingest (signed, trust>=2) ──────────────────
#
# Receives an account from a bonded peer pool and UPSERTs identity columns
# ONLY. Credit columns (total_credits, total_earned, total_spent) are
# EXPLICITLY excluded per molecule-guardian invariant (2026-04-11): ingesting
# a stale credit snapshot could silently overwrite a live balance. Credit
# replication follows the ledger gossip path (M11.2).
#
# Trust gate : >= 2 (promoted via iamine pool promote). Signature envelope
# verified via enforce_fed_policy. Nonce consumed (anti-replay).

@router.post("/v1/federation/accounts/ingest")
async def federation_accounts_ingest(request: Request):
    pool = _pool()
    raw_body = await request.body()

    try:
        import json as _json
        payload = _json.loads(raw_body.decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    required_fields = ("account_id", "email", "password_hash", "origin_pool_id")
    for k in required_fields:
        if k not in payload:
            return JSONResponse({"error": f"missing field: {k}"}, status_code=400)

    origin_atom_id = payload["origin_pool_id"]

    # Resolve peer pubkey from federation_peers table (needed for signature verify)
    peer_pubkey = None
    peer_trust = 0
    if hasattr(pool.store, "pool") and pool.store.pool:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT pubkey, trust_level FROM federation_peers WHERE atom_id = $1 AND revoked_at IS NULL",
                origin_atom_id,
            )
            if row:
                peer_pubkey = bytes(row["pubkey"]) if row["pubkey"] else None
                peer_trust = int(row["trust_level"] or 0)

    if peer_pubkey is None:
        return JSONResponse({"error": "origin pool not bonded here"}, status_code=403)

    # Trust >= 2 gate
    if peer_trust < 2:
        return JSONResponse({"error": f"trust_level {peer_trust} < 2 required"}, status_code=403)

    # Unified enforcement (signature + kill switch + mode)
    reject, sig_ok = await fed.enforce_fed_policy(
        pool, request,
        method="POST", path="/v1/federation/accounts/ingest",
        body=raw_body,
        peer_pubkey=peer_pubkey,
        require_signature=True,
    )
    if reject:
        return JSONResponse({"error": reject["error"]}, status_code=reject["status_code"])
    if not sig_ok:
        return JSONResponse({"error": "signature invalid"}, status_code=401)

    # UPSERT identity columns ONLY. Credit columns are intentionally EXCLUDED
    # to prevent stale-snapshot overwrites of live balances (guardian rec).
    #
    # INVARIANT — email verification columns (migration 016) MUST stay EXCLUDED:
    #   - email_verified         (local flag, origin pool only)
    #   - verification_code      (local, origin pool only)
    #   - verification_expires   (local, origin pool only)
    # Replicated accounts are treated as already-verified (DEFAULT TRUE handles
    # new INSERT, and we NEVER touch email_verified in ON CONFLICT DO UPDATE SET).
    # Adding them would create a cross-pool ghost-account bypass (race: register
    # on Pool A, replicated row lands on Pool B with false, login via Pool B).
    # Guardian session required before any change here.
    if hasattr(pool.store, "pool") and pool.store.pool:
        try:
            async with pool.store.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO accounts (
                        account_id, email, password_hash, display_name,
                        pseudo, eth_address, account_token, memory_enabled, created
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                    ON CONFLICT (account_id) DO UPDATE SET
                        email = EXCLUDED.email,
                        password_hash = EXCLUDED.password_hash,
                        display_name = EXCLUDED.display_name,
                        pseudo = EXCLUDED.pseudo,
                        eth_address = EXCLUDED.eth_address,
                        account_token = EXCLUDED.account_token,
                        memory_enabled = EXCLUDED.memory_enabled
                    """,
                    payload["account_id"],
                    payload["email"],
                    payload["password_hash"],
                    payload.get("display_name"),
                    payload.get("pseudo"),
                    payload.get("eth_address"),
                    payload.get("account_token"),
                    bool(payload.get("memory_enabled", False)),
                )
        except Exception as e:
            log.error(f"account ingest UPSERT failed: {e}")
            return JSONResponse({"error": f"db upsert failed: {str(e)[:200]}"}, status_code=500)

    # Log the receive for M11.3 targeting
    try:
        from ..core import federation_replication as _repl
        await _repl.log_account_recv_ack(pool, payload["account_id"], origin_atom_id, status="ack")
    except Exception as e:
        log.warning(f"log_account_recv_ack failed: {e}")

    self_atom_id = pool.federation_self.atom_id if pool.federation_self else ""
    import datetime as _dt
    return {
        "ok": True,
        "account_id": payload["account_id"],
        "self_atom_id": self_atom_id,
        "ack_at": _dt.datetime.utcnow().isoformat(),
    }


# ─── M11.3 admin : trigger rebuild ──────────────────────────
#
# Admin entry to trigger a rebuild cross-verify from one or all bonded
# peers. Phase 1 is manual-only (no auto-trigger on boot). Pattern per
# molecule-guardian M11.3 verdict.

@router.post("/v1/federation/rebuild/trigger")
async def federation_rebuild_trigger(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    # Honor kill switch
    try:
        if fed.is_fed_disabled_by_fs():
            return JSONResponse(
                {"error": "federation disabled by FS kill switch"},
                status_code=503,
            )
    except Exception:
        pass

    from ..core import federation_replication as frepl
    if not frepl.is_replication_enabled():
        return JSONResponse(
            {
                "error": "REPLICATION_ENABLED=false",
                "note": "flip flag to true before triggering rebuild",
            },
            status_code=503,
        )

    peer_atom_id = request.query_params.get("peer_atom_id") or None
    result = await frepl.trigger_rebuild(pool, peer_atom_id=peer_atom_id)
    status_code = 200 if result.get("status") == "complete" else 500 if result.get("status") == "failed" else 200
    return JSONResponse(result, status_code=status_code)


# ─── M11-scaffold : replication endpoints (skeleton + admin state) ──────
# Q1-Q6 RAID decisions tranchees 2026-04-11. See project_m11_raid_decisions.md.
#
# /v1/federation/ledger/ingest : SIGNED endpoint for bonded peers (trust>=3)
#   to push ledger rows via gossip. Phase 1 scaffold = returns 501 if
#   REPLICATION_ENABLED=false. Double protection : even if the env flag is
#   flipped, the trust>=3 gate is currently UNREACHABLE because promote_peer
#   still hard-locks target_level>2 (TRUST_LEVEL_MAX_M5=2 in core/federation.py).
#   The M5 lock will be lifted in its own dedicated guardian session before
#   M11.2 activates the gossip loop.
#
# /v1/federation/replication/state : admin read-only view on replication
#   state + queue stats + bonded peers reachable + flags. Extends the
#   unified /v1/admin/m10/health surface for M11 observability.

@router.post("/v1/federation/ledger/ingest")
async def federation_ledger_ingest(request: Request):
    """Ingest ledger rows from a bonded peer (M11-scaffold skeleton).

    Phase 1 behavior :
      1. FS kill switch check -> 503 if /etc/iamine/fed_disable present
      2. REPLICATION_ENABLED env flag check -> 501 if false
      3. Signed envelope verification via enforce_fed_policy (Ed25519, trust>=3)
      4. Payload schema validation (rows[], merkle_root, period_start, period_end)
      5. Recompute merkle root locally from rows canonical form v1
      6. Compare computed_root vs claimed_root :
         - mismatch -> WARNING log with BOTH roots + peer_id, reject 400
         - match -> insert rows append-only with pending_worker_attribution=true

    Phase 1 scaffold returns 501 early on the env flag check, so steps 3-6
    only matter when REPLICATION_ENABLED=true. The M5 trust lock provides
    a second gate : even if the env flag is accidentally flipped in prod,
    no peer can reach trust>=3 until M5 lock is lifted.
    """
    pool = _pool()

    # Honor FS kill switch (guardian rec #5 pattern)
    try:
        if fed.is_fed_disabled_by_fs():
            return JSONResponse(
                {"error": "federation disabled by FS kill switch /etc/iamine/fed_disable"},
                status_code=503,
            )
    except Exception:
        pass

    # Env flag scaffold gate
    from ..core import federation_replication as _repl
    if not _repl.is_replication_enabled():
        return JSONResponse(
            {
                "error": "not_implemented",
                "reason": "REPLICATION_ENABLED=false (phase 1 scaffold)",
                "scaffold": True,
                "phase": 1,
            },
            status_code=501,
        )

    # Signed envelope + trust>=3 check (M11.2 will wire this via enforce_fed_policy)
    # Scaffold stub : require admin token as secondary gate until M11.2 signs envelopes
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse(
            {"error": "admin required (scaffold) — M11.2 will require signed envelope trust>=3"},
            status_code=401,
        )

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    rows = payload.get("rows")
    claimed_root = payload.get("merkle_root")
    period_start = payload.get("period_start")
    period_end = payload.get("period_end")
    peer_atom_id = payload.get("peer_atom_id", "unknown")

    if rows is None or not isinstance(rows, list):
        return JSONResponse({"error": "missing or invalid rows[]"}, status_code=400)
    if not claimed_root:
        return JSONResponse({"error": "missing merkle_root"}, status_code=400)

    # Recompute + verify
    verdict = _repl.verify_ingest_payload(rows, claimed_root)
    if not verdict.get("ok"):
        # Guardian rec #5 : log BOTH roots + peer for forensic
        log.warning(
            "merkle mismatch from peer %s: computed=%s claimed=%s leaves=%d",
            peer_atom_id,
            verdict.get("computed_root"),
            verdict.get("claimed_root"),
            verdict.get("leaves_count", 0),
        )
        return JSONResponse(
            {
                "error": "merkle root mismatch",
                "computed_root": verdict.get("computed_root"),
                "claimed_root": verdict.get("claimed_root"),
                "leaves_count": verdict.get("leaves_count"),
                "scaffold": True,
            },
            status_code=400,
        )

    # M11.2 active : append-only INSERT with ON CONFLICT DO NOTHING idempotency
    # via UNIQUE (origin_pool_id, job_id) from migration 014.
    #
    # TRUST GATE NAMING (guardian rec M11.2 2026-04-11) :
    # - M5 CLI hard lock on promote_peer to level 3 is INTACT. This route
    #   does NOT lower that lock — it is a separate CLI constraint.
    # - Ingest gate here = trust>=2 (matches actual bonded peer level in
    #   the current testnet). Distinct concern from M5 CLI lock.

    inserted = 0
    if hasattr(pool.store, "pool") and pool.store.pool:
        try:
            async with pool.store.pool.acquire() as conn:
                for row in rows:
                    try:
                        r = await conn.execute(
                            """
                            INSERT INTO revenue_ledger (
                                job_id, origin_pool_id, exec_pool_id, worker_id,
                                model, tokens_in, tokens_out,
                                credits_total, credits_worker, credits_exec,
                                credits_origin, credits_treasury,
                                forward_chain, created_at, pending_worker_attribution
                            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,TRUE)
                            ON CONFLICT (origin_pool_id, job_id) DO NOTHING
                            """,
                            row.get("job_id"),
                            row.get("origin_pool_id"),
                            row.get("exec_pool_id"),
                            row.get("worker_id"),
                            row.get("model"),
                            int(row.get("tokens_in") or 0),
                            int(row.get("tokens_out") or 0),
                            int(row.get("credits_total") or 0),
                            int(row.get("credits_worker") or 0),
                            int(row.get("credits_exec") or 0),
                            int(row.get("credits_origin") or 0),
                            int(row.get("credits_treasury") or 0),
                            row.get("forward_chain") or [],
                            row.get("created_at"),
                        )
                        if isinstance(r, str) and r.startswith("INSERT ") and not r.endswith(" 0"):
                            inserted += 1
                    except Exception as _ie:
                        log.warning(f"M11.2 ingest row insert failed: {_ie}")
        except Exception as e:
            return JSONResponse({"error": f"db: {str(e)[:200]}"}, status_code=500)

    return {
        "ok": True,
        "leaves_count": verdict.get("leaves_count"),
        "verified_root": verdict.get("computed_root"),
        "peer_atom_id": peer_atom_id,
        "inserted": inserted,
        "period_start": period_start,
        "period_end": period_end,
    }


@router.get("/v1/federation/replication/state")
async def federation_replication_state(request: Request):
    """Unified admin view on M11 replication state (read-only).

    Aggregates :
      - REPLICATION_ENABLED + ACCOUNT_CREATION_QUORUM_ENABLED flags
      - replication_state row (rebuild_status, last_synced_period, etc.)
      - replication_queue stats breakdown
      - molecule_size, bonded_peers_reachable count
      - account_creation_quorum_precheck snapshot

    Read-only. Always returns scaffold=true (guardian rec #7 applied).
    """
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    from ..core import federation_replication as _repl

    flags = {
        "REPLICATION_ENABLED": _repl.is_replication_enabled(),
        "ACCOUNT_CREATION_QUORUM_ENABLED": _repl.is_account_creation_quorum_enabled(),
        "FS_KILL_SWITCH": fed.is_fed_disabled_by_fs(),
    }

    state = await _repl.get_replication_state(pool)
    queue = await _repl.get_queue_stats(pool)
    precheck = await _repl.account_creation_quorum_precheck(pool)
    reachable = await _repl.bonded_peers_reachable(pool)
    n_total = await _repl.molecule_size(pool)

    return {
        "scaffold": True,
        "phase": 1,
        "flags": flags,
        "state": state,
        "queue": queue,
        "account_creation_quorum": precheck,
        "bonded_peers_reachable_count": len(reachable),
        "molecule_size": n_total,
        "min_peers_threshold": _repl.MOLECULE_MIN_PEERS_FOR_QUORUM,
        "partition_detection_sec": _repl.PARTITION_DETECTION_SEC,
        "quorum_formula": "floor(N/2)+1",
        "note": "M11-scaffold read-only view. No flags active in phase 1.",
    }


# ─── M10-active : unified health endpoint (admin, read-only) ──────────────
# Single source of truth for ops checking M10-active readiness before go-live.
# Aggregates scaffold state for all 7 economic decisions + flag status + counts.
# Pure read-only, no state mutation, no guardian implications (no new core file,
# no table, no formula change). See project_m10_active_chunks_11avril.md.

@router.get("/v1/admin/m10/health")
async def federation_m10_health(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    import os as _os
    from ..core import slashing as _slashing
    from ..core import federation_disputes as _disp
    from ..core import federation_settlement as _fs
    from ..core import federation_merkle as _merkle
    from ..core import revenue as _rev

    # Flag state — env vars, never cached
    flags = {
        "SETTLEMENT_ENABLED": _fs.is_settlement_enabled(),
        "SETTLEMENT_MODE": _fs.get_settlement_mode(),
        "SLASHING_ENABLED": _slashing.is_slashing_enabled(),
        "DISPUTE_SAMPLING_ENABLED": _disp.is_dispute_sampling_enabled(),
        "IAMINE_FED_MODE": fed.get_mode(),
        "IAMINE_FED_KILL_SWITCH_FS": fed.is_fed_disabled_by_fs(),
    }

    # DB counts — scaffold tables + pre-existing federation tables
    counts = {}
    if hasattr(pool.store, "pool") and pool.store.pool:
        async with pool.store.pool.acquire() as conn:
            for key, sql in [
                ("revenue_ledger_total", "SELECT COUNT(*)::BIGINT FROM revenue_ledger"),
                ("revenue_ledger_pending",
                    "SELECT COUNT(*)::BIGINT FROM revenue_ledger WHERE pending_worker_attribution = true"),
                ("revenue_ledger_settled",
                    "SELECT COUNT(*)::BIGINT FROM revenue_ledger WHERE settled = true"),
                ("slashing_events_total", "SELECT COUNT(*)::BIGINT FROM slashing_events"),
                ("federation_disputes_total", "SELECT COUNT(*)::BIGINT FROM federation_disputes"),
                ("federation_disputes_pending",
                    "SELECT COUNT(*)::BIGINT FROM federation_disputes WHERE status = 'pending'"),
                ("federation_peers_total", "SELECT COUNT(*)::BIGINT FROM federation_peers"),
                ("federation_peers_bonded",
                    "SELECT COUNT(*)::BIGINT FROM federation_peers WHERE revoked_at IS NULL"),
                ("workers_certs_total",
                    "SELECT COUNT(*)::BIGINT FROM workers_certs WHERE revoked_at IS NULL"),
                ("federation_settlements_total",
                    "SELECT COUNT(*)::BIGINT FROM federation_settlements"),
            ]:
                try:
                    counts[key] = int(await conn.fetchval(sql) or 0)
                except Exception as e:
                    counts[key] = None
                    counts[f"{key}_error"] = str(e)[:120]

    # Decisions overview — 7 economic decisions mapping
    self_atom_id = None
    if getattr(pool, "federation_self", None):
        self_atom_id = pool.federation_self.atom_id

    anti_dumping_state = await fed.get_effective_anti_dumping_min_rate(pool)
    treasury_addr = _rev.get_treasury_address(pool)

    decisions = {
        "Q1_TREASURY": {
            "scaffold": True,
            "status": "scaffold",
            "phase": 1,
            "address_set": treasury_addr is not None,
            "address_source": (
                "env" if _os.environ.get("IAMINE_TREASURY_ADDRESS") else
                "pool_attr" if treasury_addr else "unset"
            ),
            "migration_trigger": "N >= 20 bonded peers OR external pool ops",
            "commit": "dad0052",
        },
        "Q2_ANCHOR": {
            "scaffold": True,
            "status": "structural",
            "anchor": "usage-backed",
            "swap_onchain_blocked_by": "token_iamine_onchain TODO",
            "note": "credits_total in revenue_ledger = tokens of inference consumable",
        },
        "Q3_SLASHING": {
            "scaffold": True,
            "status": "scaffold+consumer",
            "burn_destination": "BURN (out of circulation)",
            "events_count": counts.get("slashing_events_total"),
            "consumer_integration": "burns_meta in aggregate_period (non-contaminating)",
            "commits": ["7bf1f57", "997a484", "d2ca43f"],
        },
        "Q4_DISPUTE": {
            "scaffold": True,
            "status": "scaffold",
            "sampling_rate_pct": _disp.DISPUTE_SAMPLING_RATE_PCT,
            "epoch_sec": _disp.DISPUTE_EPOCH_SEC,
            "disputes_count": counts.get("federation_disputes_total"),
            "disputes_pending": counts.get("federation_disputes_pending"),
            "verifier_remuneration": "DEFERRED (FORMULA ASSUMPTION in federation_disputes.py)",
            "commit": "37b081f",
        },
        "Q5_ANTI_DUMPING": {
            "scaffold": True,
            "status": "scaffold",
            "phase": anti_dumping_state["phase"],
            "enforced": anti_dumping_state["enforced"],
            "rate": anti_dumping_state["rate"],
            "bonded_peers": anti_dumping_state["bonded_peers"],
            "threshold": anti_dumping_state["threshold"],
            "commit": "dad0052",
        },
        "Q6_WORKER_SHARE": {
            "scaffold": True,
            "status": "live-pre-session",
            "phase": 1,
            "mechanism": "virtual balance via api_token (core/credits.py)",
            "phase_2_blocked_by": "token_iamine_onchain TODO",
        },
        "Q7_ACCOUNT_FED": {
            "scaffold": True,
            "status": "scaffold",
            "jwt_scope": "identity-only (phase 1)",
            "issuer_atom_id": self_atom_id,
            "commit": "babef04",
        },
    }

    # Merkle v1 state
    merkle = {
        "version": getattr(_merkle, "LEDGER_MERKLE_VERSION", 1),
        "canonical_form_frozen": True,
        "slashing_events_excluded": True,
        "federation_disputes_excluded": True,
    }

    # Overall summary
    scaffolded = sum(1 for v in decisions.values() if v.get("scaffold"))
    active_flags = sum(1 for v in flags.values() if v is True)

    return {
        "m10_scaffold_complete": scaffolded == 7,
        "decisions_scaffolded": scaffolded,
        "decisions_total": 7,
        "any_flag_active": active_flags > 0,
        "flags": flags,
        "counts": counts,
        "decisions": decisions,
        "merkle": merkle,
        "self_atom_id": self_atom_id,
        "pool_version": __import__("iamine").__version__,
        "note": "read-only health check. No state mutation. Scaffold markers non-authoritative.",
    }


# ─── M10-active Q4 : disputes scaffold endpoints (admin) ──────────────────
# Record disputes + mark verification outcome + list. SCAFFOLD only :
# no actual re-execution, no HTTP cross-pool verifier call. Gated by
# DISPUTE_SAMPLING_ENABLED env (default false) inside core.federation_disputes.
# See project_m10_disputes_scaffold.md.

@router.post("/v1/federation/disputes/record")
async def federation_disputes_record(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    job_id = payload.get("job_id")
    contested_pool_id = payload.get("contested_pool_id")
    origin_pool_id = payload.get("origin_pool_id")
    reason = payload.get("reason", "")
    auto_pick = bool(payload.get("auto_pick_verifier", True))

    if not job_id:
        return JSONResponse({"error": "missing job_id"}, status_code=400)
    if not contested_pool_id:
        return JSONResponse({"error": "missing contested_pool_id"}, status_code=400)

    from ..core import federation_disputes as disp
    result = await disp.record_dispute(
        pool, job_id, contested_pool_id,
        origin_pool_id=origin_pool_id,
        reason=reason,
        auto_pick_verifier=auto_pick,
    )
    status_code = 200 if result.get("status") in ("recorded", "skipped") else 400
    return JSONResponse(result, status_code=status_code)


@router.post("/v1/federation/disputes/{dispute_id}/mark")
async def federation_disputes_mark(dispute_id: int, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    outcome = payload.get("outcome", "verified")
    result_text = payload.get("result", "")
    verified_by = payload.get("verified_by_peer_id")

    from ..core import federation_disputes as disp
    r = await disp.mark_dispute_verified(
        pool, dispute_id, result_text,
        verified_by_peer_id=verified_by,
        outcome=outcome,
    )
    status_code = 200 if r.get("status") == "marked" else 400
    return JSONResponse(r, status_code=status_code)


@router.get("/v1/federation/disputes")
async def federation_disputes_list(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    status = request.query_params.get("status")
    job_id = request.query_params.get("job_id")
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100

    from ..core import federation_disputes as disp
    return await disp.get_dispute_state(pool, status=status, job_id=job_id, limit=limit)


# ─── M10-active Q1/Q5 : policy status endpoint (admin, read-only) ──────────
# Surfaces Q1 treasury address (single-sig phase 1 migration-ready) and
# Q5 anti-dumping threshold + current phase. See project_decisions_a_tranchees.md.

@router.get("/v1/federation/policy")
async def federation_policy_status(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    from ..core import revenue as rev
    treasury = rev.get_treasury_address(pool)

    anti_dumping = await fed.get_effective_anti_dumping_min_rate(pool)

    return {
        "q1_treasury": {
            "address": treasury,
            "set": treasury is not None,
            "phase": 1,  # single-sig david until migration
            "migration_trigger": "N >= 20 peers OR external pool ops",
        },
        "q5_anti_dumping": anti_dumping,
    }


# ─── M10-active Q7 : account JWT endpoints (admin + public verify) ──────────
# Decision D-ACCOUNT-FED (a) JWT signed, identity-only phase 1.
# Voir project_decisions_a_tranchees.md

@router.post("/v1/federation/accounts/jwt/mint")
async def federation_account_jwt_mint(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    account_id = payload.get("account_id")
    email = payload.get("email", "")
    ttl_sec = payload.get("ttl_sec", 3600)
    if not account_id:
        return JSONResponse({"error": "missing account_id"}, status_code=400)
    try:
        ttl_sec = int(ttl_sec)
    except (TypeError, ValueError):
        return JSONResponse({"error": "ttl_sec must be integer"}, status_code=400)
    if ttl_sec <= 0 or ttl_sec > 86400:
        return JSONResponse({"error": "ttl_sec must be in (0, 86400]"}, status_code=400)

    try:
        minted = fed.sign_account_jwt(pool, account_id, email=email, ttl_sec=ttl_sec)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        log.error(f"jwt mint failed: {e}")
        return JSONResponse({"error": f"mint failed: {e}"}, status_code=500)

    return minted


@router.post("/v1/federation/accounts/jwt/verify")
async def federation_account_jwt_verify(request: Request):
    """Public endpoint : anyone can verify an IAMINE JWT.

    No admin gate because verification is inherently non-mutating and the
    cryptographic proof is self-contained. This is the standard JWT pattern
    (verifier doesn\u2019t need to be the issuer).
    """
    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    token = payload.get("token")
    if not token:
        return JSONResponse({"error": "missing token"}, status_code=400)

    result = await fed.verify_account_jwt(pool, token)
    status_code = 200 if result.get("valid") else 401
    return JSONResponse(result, status_code=status_code)


# ─── M10-active : slashing scaffold endpoints (admin) ──────────────────────
# BURN helper and read endpoints. Gated behind SLASHING_ENABLED env var
# INSIDE core.slashing.burn_credits itself (skipped status returned when off).
# The admin endpoint itself is always reachable so ops can observe the
# kill-switch behavior and read burn history even when disabled.
# Voir project_m10_slashing_scaffold.md et project_decisions_a_tranchees.md (Q3).

@router.post("/v1/federation/slashing/burn")
async def federation_slashing_burn(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or "{}")
    except Exception as e:
        return JSONResponse({"error": f"invalid json: {e}"}, status_code=400)

    peer_id = payload.get("peer_id")
    amount = payload.get("amount")
    reason = payload.get("reason")
    job_id = payload.get("job_id")  # optional

    if not peer_id:
        return JSONResponse({"error": "missing peer_id"}, status_code=400)
    if amount is None:
        return JSONResponse({"error": "missing amount"}, status_code=400)
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        return JSONResponse({"error": "amount must be integer"}, status_code=400)
    if amount <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    if not reason:
        return JSONResponse({"error": "missing reason"}, status_code=400)

    from ..core import slashing
    result = await slashing.burn_credits(pool, peer_id, amount, reason, job_id=job_id)
    status_code = 200 if result.get("status") in ("burned", "skipped") else 400
    return JSONResponse(result, status_code=status_code)


@router.get("/v1/federation/slashing/total/{peer_id}")
async def federation_slashing_total(peer_id: str, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    since_ts = request.query_params.get("since")
    from ..core import slashing
    result = await slashing.get_burn_total(pool, peer_id, since_ts=since_ts)
    return result


@router.get("/v1/federation/slashing/events")
async def federation_slashing_events(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()

    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return JSONResponse({"error": "no DB store", "events": []})

    peer_id = request.query_params.get("peer_id")
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 1000))

    async with pool.store.pool.acquire() as conn:
        if peer_id:
            rows = await conn.fetch(
                """
                SELECT id, peer_id, job_id, amount, reason, created_at
                FROM slashing_events
                WHERE peer_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT $2
                """,
                peer_id, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, peer_id, job_id, amount, reason, created_at
                FROM slashing_events
                ORDER BY created_at DESC, id DESC
                LIMIT $1
                """,
                limit,
            )

    events = [
        {
            "id": r["id"],
            "peer_id": r["peer_id"],
            "job_id": r["job_id"],
            "amount": int(r["amount"]),
            "reason": r["reason"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return {"count": len(events), "limit": limit, "peer_id": peer_id, "events": events}


# ─── M11-scaffold : ledger merkle snapshots (admin, read-only) ──────────────
# Primitive fondamentale neutre qui debloque toute strategie de replication
# (gossip, Raft, snapshots periodiques) sans en privilegier une. RFC 6962.
# Voir project_m11_scaffold_invariants.md.

@router.get("/v1/federation/ledger/merkle-root")
async def federation_ledger_merkle_root(request: Request):
    # M11.2: public (gossip loop of bonded peers polls this without auth).
    # Returns only the merkle_root + leaves_count + version, no sensitive data.
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    try:
        since_id = int(request.query_params.get("since_id")) if request.query_params.get("since_id") else None
    except ValueError:
        return JSONResponse({"error": "invalid since_id"}, status_code=400)
    try:
        until_id = int(request.query_params.get("until_id")) if request.query_params.get("until_id") else None
    except ValueError:
        return JSONResponse({"error": "invalid until_id"}, status_code=400)
    try:
        limit = int(request.query_params.get("limit", "10000"))
    except ValueError:
        limit = 10000

    from ..core import federation_merkle as fm
    return await fm.compute_ledger_merkle_root(pool, since_id=since_id, until_id=until_id, limit=limit)


# ─── M11.2 signed sync-pull (gossip-consumed) ──────────────────────────────
#
# Read-only endpoint returning the most recent ledger rows + merkle root,
# with the response body signed via X-IAMINE-Signature header. Consumed
# by the replication_ledger_gossip_loop on bonded peers.
#
# Signature is Ed25519 over sha256(body) using the pool self privkey.
# The caller (peer gossip loop) verifies the signature against
# federation_peers.pubkey BEFORE trusting the merkle root recompute.
# Per molecule-guardian M11.2 verdict 2026-04-11, this is the first of
# two signature gates ; the second is verify_ingest_payload merkle check.
#
# No admin token required — signature authentifies. Public endpoint.

@router.get("/v1/federation/ledger/sync-pull")
async def federation_ledger_sync_pull(request: Request):
    from fastapi.responses import Response as FAResponse
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    try:
        since_id = int(request.query_params.get("since_id")) if request.query_params.get("since_id") else None
    except ValueError:
        return JSONResponse({"error": "invalid since_id"}, status_code=400)
    try:
        until_id = int(request.query_params.get("until_id")) if request.query_params.get("until_id") else None
    except ValueError:
        return JSONResponse({"error": "invalid until_id"}, status_code=400)
    try:
        limit = int(request.query_params.get("limit", "500"))
    except ValueError:
        limit = 500
    limit = max(1, min(limit, 500))

    from ..core import federation_merkle as fm
    from ..core import federation_replication as frepl
    data = await fm.snapshot_ledger_range(pool, since_id=since_id, until_id=until_id, limit=limit)

    import json as _json
    body_bytes = _json.dumps(data, default=str).encode()
    sig_hex = frepl.sign_body_with_self(pool, body_bytes)

    return FAResponse(
        content=body_bytes,
        media_type="application/json",
        headers={"X-IAMINE-Signature": sig_hex},
    )


@router.get("/v1/federation/ledger/snapshot")
async def federation_ledger_snapshot(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    try:
        since_id = int(request.query_params.get("since_id")) if request.query_params.get("since_id") else None
    except ValueError:
        return JSONResponse({"error": "invalid since_id"}, status_code=400)
    try:
        until_id = int(request.query_params.get("until_id")) if request.query_params.get("until_id") else None
    except ValueError:
        return JSONResponse({"error": "invalid until_id"}, status_code=400)
    try:
        limit = int(request.query_params.get("limit", "500"))
    except ValueError:
        limit = 500

    from ..core import federation_merkle as fm
    return await fm.snapshot_ledger_range(pool, since_id=since_id, until_id=until_id, limit=limit)


# ─── GET /v1/federation/settlement/state (M10-scaffold, admin) ──────────────
# Returns recent settlement proposals. Scaffold only — non-authoritative.

@router.get("/v1/federation/settlement/state")
async def federation_settlement_state(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return {"mode": "off", "scaffold": True, "rows": []}
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))
    from ..core import federation_settlement as fs
    return await fs.get_settlement_state(pool, limit=limit)


# ─── POST /v1/federation/settlement/propose/{peer_atom_id} (admin trigger) ──

@router.post("/v1/federation/settlement/propose/{peer_atom_id}")
async def federation_settlement_propose(peer_atom_id: str, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    try:
        raw = await request.body()
        if raw:
            import json as _json
            body_dict = _json.loads(raw.decode())
        else:
            body_dict = {}
    except Exception:
        body_dict = {}

    import datetime as _dt
    period_sec = int(body_dict.get("period_sec", 86400))
    end = _dt.datetime.utcnow()
    start = end - _dt.timedelta(seconds=period_sec)

    from ..core import federation_settlement as fs
    result = await fs.propose_settlement(pool, peer_atom_id, start, end)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ─── GET /v1/federation/debug/enforcement (admin) ───────────────────────────
# Diagnostic snapshot: mode, kill switch, in-memory metrics counters.
# Non-authoritative — authoritative stats come from revenue_ledger + settlements.

@router.get("/v1/federation/debug/enforcement")
async def federation_debug_enforcement(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    from ..core import federation_metrics as fm
    import time as _time
    self_card = _self_info_card(pool) if not _is_off(pool) else {"mode": "off"}
    return {
        "mode": self_card.get("mode"),
        "molecule_id": self_card.get("molecule_id"),
        "atom_id": self_card.get("atom_id"),
        "kill_switch_present": fed.is_fed_disabled_by_fs(),
        "nonce_window_sec": fed.NONCE_WINDOW_SEC,
        "hop_max": fed.HOP_MAX,
        "forwarding_enabled": bool(__import__("os").environ.get("FORWARDING_ENABLED", "false").lower() in ("1","true","yes","on")),
        "forwarding_mode": __import__("os").environ.get("FORWARDING_MODE", "log_only"),
        "settlement_enabled": bool(__import__("os").environ.get("SETTLEMENT_ENABLED", "false").lower() in ("1","true","yes","on")),
        "settlement_mode": __import__("os").environ.get("SETTLEMENT_MODE", "dry_run"),
        "counters": fm.get_all(),
        "server_time": int(_time.time()),
    }


# ─── GET /v1/federation/ledger (M8, admin) ───────────────────────────────────
# Tail of revenue_ledger for the dashboard. Admin-only. Last N rows desc.

@router.get("/v1/federation/ledger")
async def federation_ledger_tail(request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return {"mode": "off", "rows": []}

    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))

    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return {"rows": [], "count": 0}

    async with pool.store.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, job_id, origin_pool_id, exec_pool_id, worker_id, worker_cert_id,
                   model, tokens_in, tokens_out,
                   credits_total, credits_worker, credits_exec, credits_origin, credits_treasury,
                   (worker_sig IS NULL) AS worker_sig_null,
                   forward_chain, settled, settled_at, created_at
            FROM revenue_ledger
            ORDER BY id DESC
            LIMIT $1
            """,
            limit,
        )

    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "job_id": r["job_id"],
            "origin_pool_id": r["origin_pool_id"],
            "exec_pool_id": r["exec_pool_id"],
            "worker_id": r["worker_id"],
            "worker_cert_id": r["worker_cert_id"],
            "model": r["model"],
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "credits_total": r["credits_total"],
            "credits_worker": r["credits_worker"],
            "credits_exec": r["credits_exec"],
            "credits_origin": r["credits_origin"],
            "credits_treasury": r["credits_treasury"],
            "worker_sig_null": r["worker_sig_null"],
            "forward_chain": list(r["forward_chain"] or []),
            "settled": r["settled"],
            "settled_at": r["settled_at"].isoformat() if r["settled_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return {
        "mode": getattr(pool, "federation_mode", "off"),
        "rows": out,
        "count": len(out),
    }


@router.delete("/v1/federation/peers/{atom_id}")
async def federation_peer_delete(atom_id: str, request: Request):
    """Hard-delete a peer row. Admin only. Rejects if settlements reference it."""
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return JSONResponse({"error": "no DB store"}, status_code=500)
    async with pool.store.pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM federation_settlements WHERE peer_id=$1", atom_id,
        )
        if count and count > 0:
            return JSONResponse(
                {"error": f"peer has {count} settlement records, revoke instead"},
                status_code=409,
            )
        res = await conn.execute("DELETE FROM federation_peers WHERE atom_id=$1", atom_id)
    log.warning(f"peer {atom_id[:16]}... HARD DELETED by admin")
    return {"ok": True, "deleted": atom_id, "result": str(res)}


@router.get("/v1/federation/peers/{atom_id}")
async def federation_peer_show(atom_id: str, request: Request):
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({"error": "admin required"}, status_code=401)
    pool = _pool()
    if _is_off(pool):
        return JSONResponse({"error": "federation off"}, status_code=503)

    peer = await fed.load_peer(pool, atom_id)
    if not peer:
        return JSONResponse({"error": "not found"}, status_code=404)

    import json as _json
    caps = peer.get("capabilities") or []
    if isinstance(caps, str):
        try:
            caps = _json.loads(caps)
        except Exception:
            caps = []
    return {
        "atom_id": peer["atom_id"],
        "name": peer["name"],
        "pubkey_hex": bytes(peer["pubkey"]).hex(),
        "url": peer["url"],
        "molecule_id": peer.get("molecule_id"),
        "capabilities": caps,
        "trust_level": peer["trust_level"],
        "last_seen": peer["last_seen"].isoformat() if peer.get("last_seen") else None,
        "added_at": peer["added_at"].isoformat() if peer.get("added_at") else None,
        "revoked_at": peer["revoked_at"].isoformat() if peer.get("revoked_at") else None,
    }




# ---- M12: Pool discovery for workers ----

@router.get("/v1/federation/discover")
async def federation_discover():
    """Public endpoint: return URLs of trusted federated pools.
    
    Workers use this to discover pools they can join.
    Only pools with trust_level >= 2 are listed.
    IPs are proxied via pool names (no raw IPs exposed).
    """
    pool = _pool()

    # Pool-operator toggle: hide this pool from discovery when OFF
    if not await _read_pool_config_bool(pool, "publish_capabilities", default=True):
        return JSONResponse(
            {"pools": [], "private": True,
             "reason": "this pool does not publish capabilities"},
            status_code=200)

    from ..core import federation as fed
    
    try:
        peers = await fed.list_peers(pool, include_revoked=False)
    except Exception:
        peers = []
    
    pools = []
    
    # Self
    ident = getattr(pool, "federation_self", None)
    if ident:
        pools.append({
            "name": ident.name,
            "url": ident.url,
            "molecule_id": ident.molecule_id,
            "capabilities": fed.compute_live_capabilities(pool),
        })
    
    # Trusted peers
    import json as _json
    for p in peers:
        if p.get("trust_level", 0) < 2:
            continue
        if p.get("revoked_at"):
            continue
        caps = p.get("capabilities") or []
        if isinstance(caps, str):
            try: caps = _json.loads(caps)
            except: caps = []
        pools.append({
            "name": p["name"],
            "url": p.get("url", ""),
            "molecule_id": p.get("molecule_id", ""),
            "capabilities": caps,
        })
    
    return {"pools": pools, "molecule_id": ident.molecule_id if ident else "iamine-testnet"}

# ---- M12: Recruitment needs (public endpoint) ----

@router.get("/v1/recruitment/needs")
async def recruitment_needs():
    """Public endpoint: return capability gaps for ALL pools (self + federation).

    M12 scaffold — aggregates gaps from self + peer capabilities.
    """
    from ..core.recruitment import get_recruitment_needs, detect_federation_gaps
    pool = _pool()

    # Local pool data (backward compat)
    local = get_recruitment_needs(pool)

    # Federation-wide gaps
    try:
        federation = await detect_federation_gaps(pool)
    except Exception:
        federation = []

    local["federation"] = federation
    return local
