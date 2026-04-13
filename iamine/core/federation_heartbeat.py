"""M7b — Peer-to-peer heartbeat loop.

Chaque pool ping ses bonded peers toutes les HEARTBEAT_INTERVAL_SEC secondes
via GET /v1/federation/info. Met à jour `federation_peers.last_seen`.

Findings molecule-guardian appliqués :
- last_seen est `ephemeral-acceptable` (pas de RF>=2)
- missed_beats_count exposable via status() admin
- heartbeat signé pas nécessaire pour GET /info (endpoint public unsigned)
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from . import federation as fed

log = logging.getLogger("iamine.federation.heartbeat")


# In-memory metric (reset au restart — c'est OK, purement diag)
_missed_beats = defaultdict(int)
_last_success = {}

async def _sync_peer_capabilities(pool, atom_id: str, capabilities: list) -> None:
    """Update peer capabilities in DB from live heartbeat data."""
    import json as _json
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return
    async with pool.store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE federation_peers SET capabilities = $1 WHERE atom_id = $2",
            _json.dumps(capabilities), atom_id,
        )
    log.debug(f"heartbeat: synced {len(capabilities)} capabilities for {atom_id[:16]}...")



def get_heartbeat_metrics() -> dict:
    """Return admin-visible heartbeat metrics (in-memory)."""
    return {
        "missed_beats": dict(_missed_beats),
        "last_success_at": {k: v.isoformat() if v else None for k, v in _last_success.items()},
        "interval_sec": fed.HEARTBEAT_INTERVAL_SEC,
        "unreachable_after_sec": fed.PEER_UNREACHABLE_AFTER_SEC,
    }


async def _probe_peer(peer: dict) -> dict | None:
    """GET <peer.url>/v1/federation/info. Return info dict if reachable, None otherwise."""
    import aiohttp
    url = (peer["url"] or "").rstrip("/") + "/v1/federation/info"
    if not url.startswith(("http://", "https://")):
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "iamine-pool/heartbeat"}) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        log.debug(f"heartbeat probe failed for {peer['name']}: {e}")
        return None

    # Sanity: atom_id must match what we have in DB (detect identity swap)
    if data.get("atom_id") != peer["atom_id"]:
        log.warning(
            f"heartbeat: peer {peer['name']} returned different atom_id "
            f"({data.get('atom_id', 'none')[:16]}... vs expected {peer['atom_id'][:16]}...)"
        )
        return None
    return data


async def heartbeat_loop(pool) -> None:
    """Background task: probe all bonded peers at regular interval.

    Only probes peers with trust_level >= 2 (trusted or bonded), not revoked.
    Updates federation_peers.last_seen on success, increments missed_beats on failure.
    """
    import datetime as _dt

    if fed.get_mode() == fed.FED_MODE_OFF:
        log.info("heartbeat: federation off, loop disabled")
        return

    log.info(
        f"heartbeat: loop starting (interval={fed.HEARTBEAT_INTERVAL_SEC}s, "
        f"unreachable_after={fed.PEER_UNREACHABLE_AFTER_SEC}s)"
    )

    while True:
        try:
            # Fetch all non-revoked peers with trust >= 2 (regardless of last_seen)
            all_peers = await fed.list_peers(pool, include_revoked=False)
            bonded = [p for p in all_peers if p.get("trust_level", 0) >= 2]

            if not bonded:
                await asyncio.sleep(fed.HEARTBEAT_INTERVAL_SEC)
                continue

            # Probe concurrently, bounded
            results = await asyncio.gather(
                *[_probe_peer(p) for p in bonded],
                return_exceptions=True,
            )

            now = _dt.datetime.utcnow()
            for peer, ok in zip(bonded, results):
                aid = peer["atom_id"]
                if isinstance(ok, Exception):
                    ok = None
                if ok is not None:
                    await fed.mark_peer_seen(pool, aid)
                    caps = ok.get("capabilities") or []
                    if caps != (peer.get("capabilities") or []):
                        await _sync_peer_capabilities(pool, aid, caps)
                    _last_success[aid] = now
                    _missed_beats[aid] = 0
                    try:
                        from . import federation_metrics as _fm
                        _fm.heartbeat_tick_ok()
                    except Exception:
                        pass
                else:
                    _missed_beats[aid] += 1
                    try:
                        from . import federation_metrics as _fm
                        _fm.heartbeat_tick_fail()
                    except Exception:
                        pass
                    if _missed_beats[aid] % 4 == 1:  # log on 1st, 5th, 9th ... miss
                        log.warning(
                            f"heartbeat: peer {peer['name']} ({aid[:16]}...) "
                            f"missed_beats={_missed_beats[aid]}"
                        )
        except Exception as e:
            log.error(f"heartbeat loop error: {e}", exc_info=True)

        await asyncio.sleep(fed.HEARTBEAT_INTERVAL_SEC)
