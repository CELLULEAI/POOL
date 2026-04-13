"""M7a — Inter-atom job forwarding (server-side only).

Validated by molecule-guardian 2026-04-10. Key invariants :
- Opt-in via FORWARDING_ENABLED env var (default false)
- FORWARDING_MODE=log_only (default) or active
- Fire-and-forget safe : caller wraps in try/except → fallback local
- Hop counter + forward_chain (R1) applied via envelope_bump
- Semaphore cap 128 dedicated (NOT shared with reciprocation cap)
- Ledger writes: worker_sig=NULL (pending M7-worker backfill for anti-cheat)

Worker signing (which closes the anti-cheat loop) = M7-worker + M9b wheel.
Ledger replication RF>=2 = M11.2 (currently local writes only).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Optional

from . import federation as fed
from .. import __version__

log = logging.getLogger("iamine.forwarding")


# ---- config via env vars ----

def is_forwarding_enabled() -> bool:
    return os.environ.get("FORWARDING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def get_forwarding_mode() -> str:
    """Return 'log_only' or 'active'. Default log_only."""
    mode = os.environ.get("FORWARDING_MODE", "log_only").strip().lower()
    if mode not in ("log_only", "active"):
        return "log_only"
    return mode


def get_saturation_threshold() -> int:
    try:
        return int(os.environ.get("POOL_SATURATION_THRESHOLD", "5"))
    except ValueError:
        return 5


# ---- semaphore dedicated to forwarding (NOT shared with reciprocation) ----

_FORWARDING_SEM_SIZE = 128
_forwarding_sem = None


def _get_forwarding_sem():
    global _forwarding_sem
    if _forwarding_sem is None:
        _forwarding_sem = asyncio.Semaphore(_FORWARDING_SEM_SIZE)
    return _forwarding_sem


# ---- capability matching ----

def _peer_has_model(peer: dict, requested_model: str) -> bool:
    """Check if a peer advertises the requested model in its capabilities."""
    if not requested_model:
        return False
    caps = peer.get("capabilities") or []
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except Exception:
            caps = []
    model_lower = requested_model.lower()
    for cap in caps:
        if not isinstance(cap, dict):
            continue
        if cap.get("kind", "").startswith("llm.chat"):
            cap_model = str(cap.get("model", "")).lower()
            if model_lower in cap_model or cap_model in model_lower:
                return True
    return False


def _pool_has_model_locally(pool, requested_model: str) -> bool:
    if not requested_model:
        return True  # "auto" = any local worker will do
    model_lower = requested_model.lower()
    for w in pool.workers.values():
        mp = (w.info.get("model_path", "") or "").lower()
        wid = (w.worker_id or "").lower()
        if model_lower in mp or model_lower in wid:
            return True
    return False


async def should_forward(
    pool,
    requested_model: Optional[str],
    current_queue_size: int = 0,
) -> Optional[dict]:
    """Decide whether to forward this job and to whom.

    Returns a peer dict if forwarding is appropriate, else None.
    NEVER raises — caller can assume a None result means "route locally".

    Conditions :
    - Feature flag FORWARDING_ENABLED must be true
    - Federation mode must be active (not observe / off)
    - Case A : requested_model not local + some bonded peer has it
    - Case B : local queue saturated + some bonded peer not (approx — we don't
               know peer queues yet, so we just pick any bonded peer reachable)
    """
    try:
        if not is_forwarding_enabled():
            return None
        if fed.get_effective_mode(pool) != fed.FED_MODE_ACTIVE:
            return None

        peers = await fed.list_molecule_peers(pool, min_trust=2)
        if not peers:
            return None

        # Case A : local pool can't serve the requested model
        if requested_model and not _pool_has_model_locally(pool, requested_model):
            for peer in peers:
                if _peer_has_model(peer, requested_model):
                    log.info(
                        f"forward decision: local missing model={requested_model!r}, "
                        f"peer={peer['name']!r} matches"
                    )
                    return peer

        # Case B : local queue saturated OR all workers busy
        threshold = get_saturation_threshold()
        busy_count = sum(1 for w in (pool.workers or {}).values() if w.busy)
        total_workers = len(pool.workers or {})
        all_busy = total_workers > 0 and busy_count >= total_workers

        if current_queue_size > threshold or all_busy:
            peer = peers[0]
            reason = f"all_busy ({busy_count}/{total_workers})" if all_busy else f"queue {current_queue_size}>{threshold}"
            log.info(f"forward decision: {reason}, peer={peer['name']!r}")
            return peer

        return None
    except Exception as e:
        log.warning(f"should_forward error (non-fatal): {e}")
        return None


# ---- outbound forward ----

async def forward_job(
    pool,
    peer: dict,
    model: Optional[str],
    messages: list,
    max_tokens: int,
    conv_id: Optional[str] = None,
    api_token: Optional[str] = None,
) -> dict:
    """Forward a chat job to a bonded peer via /v1/federation/job.

    Returns the peer's response dict: {ok, response, worker_id, tokens_in, tokens_out}.
    Raises on network/protocol errors — caller MUST wrap in try/except and fallback
    to local routing (doctrine toujours répondre).
    """
    import aiohttp

    if pool.federation_self is None:
        raise RuntimeError("forward_job called with uninitialized federation_self")

    peer_url = peer.get("url", "").rstrip("/")
    if not peer_url.startswith(("http://", "https://")):
        raise ValueError(f"invalid peer URL: {peer_url}")

    # Build job payload
    import uuid as _uuid
    origin_request_id = f"fwd_{_uuid.uuid4().hex[:12]}"
    payload = {
        "origin_pool_id": pool.federation_self.atom_id,
        "origin_request_id": origin_request_id,
        "model": model or "",
        "messages": messages,
        "max_tokens": max_tokens,
        "conv_id": conv_id or "",
        # API token not forwarded — exec pool doesn't bill a cross-pool user yet (M10)
    }
    body = json.dumps(payload).encode()

    # Signed envelope — hop=1, chain=[self] (R1)
    priv_raw = fed._load_privkey_from_disk(
        __import__("pathlib").Path(pool.federation_self.privkey_path)
    )
    if priv_raw is None:
        raise RuntimeError("privkey unavailable for forward")

    self_atom = pool.federation_self.atom_id
    headers = fed.build_envelope_headers(
        priv_raw, self_atom,
        "POST", "/v1/federation/job", body,
        hop=1, chain=[self_atom],
    )
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = f"iamine-pool/{__version__}"

    sem = _get_forwarding_sem()
    async with sem:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{peer_url}/v1/federation/job",
                data=body, headers=headers,
            ) as resp:
                status = resp.status
                text = await resp.text()
                if status != 200:
                    try:
                        from . import federation_metrics as _fm
                        _fm.forward_fail(f"http_{status}")
                    except Exception:
                        pass
                    raise RuntimeError(f"peer /job returned {status}: {text[:200]}")
                try:
                    from . import federation_metrics as _fm
                    _fm.forward_ok()
                except Exception:
                    pass
                return json.loads(text)
