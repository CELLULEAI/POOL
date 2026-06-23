"""Federation admin-messaging — /v1/federation/admin/* (Phase 2 Console Molecule).

Sous-domaine extrait de routes/federation.py (audit 2026-06-22) : actions admin
cross-pool avec approbation manuelle cote cible. Invariants securite (guardians
2026-04-15) : trust>=3 STRICT, opt-in federation_admin_actions_enabled (off par
defaut), signature Ed25519 = seule autorite, circuit_reset bloque si slashing
pending, cooldown + max_pending anti-abus.

Sous-router monte sur le meme prefixe que federation.py -> zero changement de
surface (verifie par tests/test_route_registry.py).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..core import federation as fed
from ..core import federation_admin as fadm

router = APIRouter()
log = logging.getLogger("iamine.federation.admin")


def _pool():
    from iamine.pool import pool
    return pool


@router.post('/v1/federation/admin/request')
async def federation_admin_request(request: Request):
    '''Receive a cross-pool admin action request (signed envelope).

    The target pool stores the request as pending and surfaces it in its
    admin UI. No action is executed until the local admin approves manually.
    '''
    pool = _pool()
    raw_body = await request.body()

    try:
        import json as _json
        payload = _json.loads(raw_body.decode() or '{}')
    except Exception as e:
        return JSONResponse({'error': f'invalid json: {e}'}, status_code=400)

    origin_atom_id = payload.get('origin_pool_id') or payload.get('from_atom_id')
    action_type = payload.get('action_type')
    action_params = payload.get('action_params') or {}
    from_admin_email = payload.get('from_admin_email')  # DISPLAY ONLY
    req_id = payload.get('request_id') or fadm.new_request_id()

    if not origin_atom_id:
        return JSONResponse({'error': 'missing origin_pool_id'}, status_code=400)
    if action_type not in fadm.ALLOWED_ACTIONS:
        return JSONResponse(
            {'error': f'action_type not whitelisted: {action_type}'},
            status_code=400,
        )

    # Phase 2.1 : gate by action kind (read vs write), not a single global flag
    # Token-guardian invariant 11 : read-only accepted by default, writes opt-in OFF
    if not await fadm.is_action_enabled(pool, action_type):
        kind = 'write' if fadm.action_is_write(action_type) else 'query'
        return JSONResponse(
            {'error': f'federation_admin_{kind}_actions disabled on this pool',
             'action_type': action_type, 'kind': kind},
            status_code=403,
        )

    # Resolve peer pubkey + trust (authoritative identity source)
    peer_pubkey = None
    peer_trust = 0
    if hasattr(pool.store, 'pool') and pool.store.pool:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT pubkey, trust_level FROM federation_peers '
                'WHERE atom_id=$1 AND revoked_at IS NULL',
                origin_atom_id,
            )
            if row:
                peer_pubkey = bytes(row['pubkey']) if row['pubkey'] else None
                peer_trust = int(row['trust_level'] or 0)

    if peer_pubkey is None:
        return JSONResponse(
            {'error': 'origin pool not bonded here'}, status_code=403
        )
    # STRICT trust>=3
    if peer_trust < 3:
        return JSONResponse(
            {'error': f'trust_level {peer_trust} < 3 required (replication-bonded)'},
            status_code=403,
        )

    # Signature verification via enforce_fed_policy (consumes nonce)
    reject, sig_ok = await fed.enforce_fed_policy(
        pool, request,
        method='POST', path='/v1/federation/admin/request',
        body=raw_body, peer_pubkey=peer_pubkey, require_signature=True,
    )
    if reject:
        return JSONResponse({'error': reject['error']}, status_code=reject['status_code'])
    if not sig_ok:
        return JSONResponse({'error': 'signature invalid'}, status_code=401)

    # Capture signature headers for audit (display-only, not re-verified)
    env_sig = request.headers.get('X-IAMINE-Signature')
    env_nonce = request.headers.get('X-IAMINE-Nonce')

    self_atom_id = pool.federation_self.atom_id if pool.federation_self else ''
    result = await fadm.create_inbound_request(
        pool,
        request_id=req_id,
        from_atom_id=origin_atom_id,
        from_admin_email=from_admin_email,  # DISPLAY ONLY
        to_atom_id=self_atom_id,
        action_type=action_type,
        action_params=action_params,
        envelope_sig=env_sig,
        envelope_nonce=env_nonce,
    )
    if not result.get('ok'):
        return JSONResponse(
            {'error': result.get('error', 'rejected')},
            status_code=result.get('status_code', 400),
        )
    return {
        'ok': True,
        'request_id': result['request_id'],
        'status': 'pending',
        'expires_at': result['expires_at'],
    }


@router.post('/v1/federation/admin/decide')
async def federation_admin_decide(request: Request):
    '''Local admin approves/rejects a pending inbound request.

    ADMIN-LOCAL only : uses admin_token. Never a cross-pool call.
    If approved, executes the action locally then sends signed callback to emitter.
    '''
    from .admin import _check_admin
    admin_label = await _check_admin(request)
    if not admin_label:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    pool = _pool()

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or '{}')
    except Exception as e:
        return JSONResponse({'error': f'invalid json: {e}'}, status_code=400)

    request_id = payload.get('request_id')
    decision = payload.get('decision')  # 'approved' | 'rejected'
    note = payload.get('note')
    override_slashing = bool(payload.get('override_slashing_block', False))

    if not request_id or decision not in ('approved', 'rejected'):
        return JSONResponse(
            {'error': 'missing request_id or invalid decision'}, status_code=400,
        )

    req = await fadm.get_request(pool, request_id, direction='inbound')
    if not req:
        return JSONResponse({'error': 'request not found'}, status_code=404)
    if req['status'] != 'pending':
        return JSONResponse(
            {'error': f'request already decided: {req["status"]}'}, status_code=409,
        )

    # REJECTED path : no execution, just mark + callback
    if decision == 'rejected':
        await fadm.mark_decided(
            pool, request_id, 'rejected',
            decided_by_email=admin_label, decision_note=note,
        )
        await fadm.audit_log(
            pool, request_id, side='target', event_type='rejected',
            actor_email=admin_label, action_type=req['action_type'],
            notes=note,
        )
        # Send callback (best-effort)
        await _send_callback(pool, req, final_status='rejected',
                             execution_result=None, execution_error=None)
        return {'ok': True, 'request_id': request_id, 'status': 'rejected'}

    # APPROVED path : check slashing guard for circuit_reset
    slashing_snapshot = None
    if req['action_type'] == 'circuit_reset':
        params = req.get('action_params') or {}
        target_worker = params.get('worker_id') if isinstance(params, dict) else None
        pending = await fadm.slashing_events_pending(pool, worker_id=target_worker)
        if pending and not override_slashing:
            return JSONResponse({
                'error': 'slashing_events pending — use override_slashing_block=true to force',
                'pending_count': len(pending),
                'pending_sample': pending[:5],
            }, status_code=409)
        slashing_snapshot = pending if pending else None
        if pending and override_slashing:
            await fadm.audit_log(
                pool, request_id, side='target', event_type='override_applied',
                actor_email=admin_label, action_type='circuit_reset',
                payload={'pending_count': len(pending)},
                notes='admin forced circuit_reset despite pending slashing_events',
            )

    # Execute action
    exec_result = await fadm.execute_action(pool, req)
    exec_error = None if exec_result.get('ok') else exec_result.get('error', 'execution failed')

    await fadm.mark_decided(
        pool, request_id, 'approved',
        decided_by_email=admin_label, decision_note=note,
        execution_result=exec_result, execution_error=exec_error,
        slashing_block_override=bool(override_slashing and slashing_snapshot is not None),
        slashing_snapshot=slashing_snapshot,
    )
    await fadm.audit_log(
        pool, request_id, side='target',
        event_type='executed' if not exec_error else 'failed',
        actor_email=admin_label, action_type=req['action_type'],
        payload={'result_ok': exec_result.get('ok')},
        notes=note,
    )

    # Send signed callback to emitter
    await _send_callback(
        pool, req,
        final_status='executed' if not exec_error else 'failed',
        execution_result=exec_result, execution_error=exec_error,
    )

    return {
        'ok': True, 'request_id': request_id,
        'status': 'executed' if not exec_error else 'failed',
        'execution_result': exec_result,
    }


@router.post('/v1/federation/admin/callback')
async def federation_admin_callback(request: Request):
    '''Emitter receives target's signed decision callback.'''
    pool = _pool()
    raw_body = await request.body()

    try:
        import json as _json
        payload = _json.loads(raw_body.decode() or '{}')
    except Exception as e:
        return JSONResponse({'error': f'invalid json: {e}'}, status_code=400)

    origin_atom_id = payload.get('origin_pool_id') or payload.get('from_atom_id')
    request_id = payload.get('request_id')
    final_status = payload.get('final_status')  # 'approved'|'rejected'|'executed'|'failed'
    exec_result = payload.get('execution_result')
    exec_error = payload.get('execution_error')

    if not origin_atom_id or not request_id or final_status not in (
        'approved', 'rejected', 'executed', 'failed',
    ):
        return JSONResponse({'error': 'invalid callback payload'}, status_code=400)

    # Resolve peer pubkey + trust
    peer_pubkey = None
    peer_trust = 0
    if hasattr(pool.store, 'pool') and pool.store.pool:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT pubkey, trust_level FROM federation_peers '
                'WHERE atom_id=$1 AND revoked_at IS NULL',
                origin_atom_id,
            )
            if row:
                peer_pubkey = bytes(row['pubkey']) if row['pubkey'] else None
                peer_trust = int(row['trust_level'] or 0)
    if peer_pubkey is None or peer_trust < 3:
        return JSONResponse({'error': 'callback from non-bonded peer'}, status_code=403)

    reject, sig_ok = await fed.enforce_fed_policy(
        pool, request,
        method='POST', path='/v1/federation/admin/callback',
        body=raw_body, peer_pubkey=peer_pubkey, require_signature=True,
    )
    if reject:
        return JSONResponse({'error': reject['error']}, status_code=reject['status_code'])
    if not sig_ok:
        return JSONResponse({'error': 'signature invalid'}, status_code=401)

    # Verify the callback matches an outbound request we emitted
    req = await fadm.get_request(pool, request_id, direction='outbound')
    if not req:
        return JSONResponse({'error': 'no matching outbound request'}, status_code=404)
    if req['to_atom_id'] != origin_atom_id:
        return JSONResponse({'error': 'callback origin mismatch'}, status_code=403)

    await fadm.mark_outbound_callback(
        pool, request_id, final_status,
        execution_result=exec_result, execution_error=exec_error,
    )
    await fadm.audit_log(
        pool, request_id, side='emitter', event_type='callback_received',
        actor_atom_id=origin_atom_id, action_type=req['action_type'],
        payload={'final_status': final_status},
    )
    return {'ok': True}


# --- outbound helpers ---

@router.post('/v1/federation/admin/send')
async def federation_admin_send(request: Request):
    '''LOCAL admin initiates a cross-pool admin request (outbound).

    This endpoint is invoked from the Molecule UI. It crafts a signed envelope
    and calls the target peer's /v1/federation/admin/request.
    '''
    from .admin import _check_admin
    admin_label = await _check_admin(request)
    if not admin_label:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    pool = _pool()
    if pool.federation_self is None:
        return JSONResponse({'error': 'federation identity not initialized'}, status_code=503)

    try:
        import json as _json
        payload = _json.loads((await request.body()).decode() or '{}')
    except Exception as e:
        return JSONResponse({'error': f'invalid json: {e}'}, status_code=400)

    target_atom_id = payload.get('target_atom_id')
    action_type = payload.get('action_type')
    action_params = payload.get('action_params') or {}
    admin_email_label = payload.get('admin_email') or admin_label

    if not target_atom_id or action_type not in fadm.ALLOWED_ACTIONS:
        return JSONResponse(
            {'error': 'missing target_atom_id or invalid action_type'},
            status_code=400,
        )

    # Resolve target peer URL + trust (must be trust>=3)
    target_url = None
    target_trust = 0
    if hasattr(pool.store, 'pool') and pool.store.pool:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT url, trust_level FROM federation_peers '
                'WHERE atom_id=$1 AND revoked_at IS NULL',
                target_atom_id,
            )
            if row:
                target_url = row['url']
                target_trust = int(row['trust_level'] or 0)
    if not target_url:
        return JSONResponse({'error': 'target peer unknown'}, status_code=404)
    if target_trust < 3:
        return JSONResponse(
            {'error': f'target trust_level {target_trust} < 3'}, status_code=403,
        )

    # Build + sign envelope
    request_id = fadm.new_request_id()
    self_atom_id = pool.federation_self.atom_id
    body = {
        'request_id': request_id,
        'origin_pool_id': self_atom_id,
        'action_type': action_type,
        'action_params': action_params,
        'from_admin_email': admin_email_label,  # display-only on target side
    }
    import json as _json
    body_bytes = _json.dumps(body).encode()

    priv_raw = b''
    try:
        from pathlib import Path as _Path
        if pool.federation_self and getattr(pool.federation_self, 'privkey_path', None):
            p = _Path(pool.federation_self.privkey_path)
            if p.exists():
                priv_raw = p.read_bytes()
    except Exception:
        priv_raw = b''
    if not priv_raw:
        return JSONResponse({'error': 'signing key unavailable'}, status_code=503)

    headers = fed.build_envelope_headers(
        priv_raw, self_atom_id,
        method='POST', path='/v1/federation/admin/request',
        body=body_bytes,
    )
    headers['Content-Type'] = 'application/json'
    headers['X-IAMINE-Admin-Email'] = admin_email_label  # display-only, not verified

    # Record outbound locally BEFORE sending (so UI sees it immediately)
    await fadm.record_outbound_request(
        pool, request_id, self_atom_id, target_atom_id,
        admin_email_label, action_type, action_params,
    )

    # Fire the HTTP call
    import aiohttp, asyncio
    url = target_url.rstrip('/') + '/v1/federation/admin/request'
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body_bytes, headers=headers) as resp:
                txt = await resp.text()
                if resp.status == 200:
                    try:
                        data = _json.loads(txt)
                    except Exception:
                        data = {'raw': txt}
                    return {'ok': True, 'request_id': request_id, 'target_response': data}
                # 200 with ok:false — Cloudflare intercepts 5xx with HTML
                return {'ok': False, 'error': f'target returned {resp.status}',
                        'detail': txt, 'target_status': resp.status}
    except asyncio.TimeoutError:
        return {'ok': False, 'error': 'timeout reaching target', 'target_status': 0}
    except Exception as e:
        return {'ok': False, 'error': f'request failed: {e}', 'target_status': 0}


@router.get('/v1/federation/admin/inbox')
async def federation_admin_inbox(request: Request):
    '''List pending inbound requests (for admin UI).'''
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    pool = _pool()
    rows = await fadm.list_inbound_pending(pool)
    # Format timestamps for JSON
    out = []
    for r in rows:
        d = dict(r)
        for k in ('created_at', 'expires_at'):
            if d.get(k) is not None and hasattr(d[k], 'isoformat'):
                d[k] = d[k].isoformat()
        out.append(d)
    return {'ok': True, 'requests': out, 'count': len(out)}


@router.get('/v1/federation/admin/outbox')
async def federation_admin_outbox(request: Request):
    '''List outbound requests emitted by this pool (for admin UI).'''
    from .admin import _check_admin
    if not await _check_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    pool = _pool()
    rows = await fadm.list_outbound_recent(pool, limit=100)
    out = []
    for r in rows:
        d = dict(r)
        for k in ('created_at', 'expires_at', 'decided_at'):
            if d.get(k) is not None and hasattr(d[k], 'isoformat'):
                d[k] = d[k].isoformat()
        out.append(d)
    return {'ok': True, 'requests': out, 'count': len(out)}


async def _send_callback(pool, inbound_req: dict, final_status: str,
                         execution_result, execution_error):
    '''Send a signed callback to the emitter peer. Best-effort.'''
    if pool.federation_self is None:
        return
    emitter_atom = inbound_req['from_atom_id']
    request_id = inbound_req['request_id']

    target_url = None
    peer_pubkey = None
    if hasattr(pool.store, 'pool') and pool.store.pool:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT url, pubkey FROM federation_peers '
                'WHERE atom_id=$1 AND revoked_at IS NULL',
                emitter_atom,
            )
            if row:
                target_url = row['url']
                peer_pubkey = row['pubkey']
    if not target_url:
        log.warning(f'callback: emitter {emitter_atom} URL unknown, skip')
        return

    self_atom_id = pool.federation_self.atom_id
    body = {
        'origin_pool_id': self_atom_id,
        'request_id': request_id,
        'final_status': final_status,
        'execution_result': execution_result,
        'execution_error': execution_error,
    }
    import json as _json
    body_bytes = _json.dumps(body).encode()

    priv_raw = b''
    try:
        from pathlib import Path as _Path
        if getattr(pool.federation_self, 'privkey_path', None):
            p = _Path(pool.federation_self.privkey_path)
            if p.exists():
                priv_raw = p.read_bytes()
    except Exception:
        priv_raw = b''
    if not priv_raw:
        log.warning('callback: signing key unavailable, skip')
        return

    headers = fed.build_envelope_headers(
        priv_raw, self_atom_id,
        method='POST', path='/v1/federation/admin/callback',
        body=body_bytes,
    )
    headers['Content-Type'] = 'application/json'

    import aiohttp
    url = target_url.rstrip('/') + '/v1/federation/admin/callback'
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body_bytes, headers=headers) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    log.warning(f'callback to {emitter_atom} returned {resp.status}: {txt[:200]}')
                    await fadm.audit_log(
                        pool, request_id, side='target',
                        event_type='callback_failed',
                        payload={'status': resp.status},
                    )
                else:
                    await fadm.audit_log(
                        pool, request_id, side='target', event_type='callback_sent',
                        payload={'final_status': final_status},
                    )
    except Exception as e:
        log.warning(f'callback send failed: {e}')
        await fadm.audit_log(
            pool, request_id, side='target', event_type='callback_failed',
            payload={'error': str(e)},
        )
