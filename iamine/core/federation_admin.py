"""Federation admin actions — Phase 2 Console Molecule.

MVP cross-pool admin avec approbation manuelle du pool cible.
Actions whitelistees : circuit_reset, query_events.

Invariants load-bearing (guardians 2026-04-15) :
  1. Opt-in pool_config.federation_admin_actions_enabled (off par defaut)
  2. circuit_reset bloque si slashing_events pending/contested (override explicite trace)
  3. Cooldown configurable par pair/action_type
  4. query_events : whitelist event_types + filter target_atom_id != self + fenetre 7j
  5. X-IAMINE-Admin-Email = display-only, JAMAIS dans condition if-auth
  6. Signature Ed25519 du pool seule autorite d'identification
  7. Status executed_no_callback distinct pour split-brain

Pattern : miroir de core/federation.py (enforce_fed_policy, build_envelope_headers).
"""
from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from typing import Optional

log = logging.getLogger(__name__)

# Actions whitelistees — elargissement requiert session molecule-guardian
ALLOWED_ACTIONS = {"circuit_reset", "query_events"}

REQUEST_TTL_SEC = 24 * 3600

# Defaults si pool_config absent (aligne avec migration 023)
DEFAULT_COOLDOWN_CIRCUIT_RESET_SEC = 21600           # 6h
DEFAULT_MAX_PENDING_PER_PEER = 10
DEFAULT_QUERY_EVENTS_WHITELIST = [
    "circuit_opened", "circuit_closed",
    "worker_joined", "worker_left",
    "peer_handshake", "overview", "peer_status",
]
QUERY_EVENTS_WINDOW_DAYS = 7


# ---- pool_config helpers ----

async def _config_get(pool, key: str, default=None):
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return default
    try:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM pool_config WHERE key=$1", key
            )
        if not row:
            return default
        return row["value"]
    except Exception as e:
        log.warning(f"_config_get({key}) failed: {e}")
        return default


async def is_query_enabled(pool) -> bool:
    """Phase 2.1 : read-only actions (query_events). Default TRUE (token-guardian invariant 11).

    1.0.0 fix : default is genuinely TRUE now. Previously the fallback resolved to "false"
    when both config keys were absent, masking the intended reciprocity default. Existing
    pools that explicitly set the flag are respected; only fresh installs flip to True.
    Trust>=3 gate and field allowlist still enforced at the route layer.
    """
    val = await _config_get(pool, "federation_admin_query_enabled", None)
    if val is None:
        val = await _config_get(pool, "federation_admin_actions_enabled", None)
    if val is None:
        return True
    return str(val).lower() == "true"


async def is_writes_enabled(pool) -> bool:
    """Phase 2.1 : write actions (circuit_reset + future). Default FALSE (token-guardian invariant 11).

    Affects revenue_ledger/slashing/settlement indirectly -> opt-in OFF preserves wallet integrity.
    Invariant 12 (not yet enforced): federation_admin_identities must contain a distinct admin
    Ed25519 key before writes can be enabled. Gate planned for Phase 2.2.
    """
    val = await _config_get(pool, "federation_admin_writes_enabled", None)
    if val is None:
        val = await _config_get(pool, "federation_admin_actions_enabled", "false")
    return str(val).lower() == "true"


async def is_enabled(pool) -> bool:
    """Backward-compat alias. Returns True if EITHER query OR writes enabled.

    Do not use in new code. Routes must dispatch via action_type using is_action_enabled().
    """
    return (await is_query_enabled(pool)) or (await is_writes_enabled(pool))


READ_ACTIONS = {"query_events"}
WRITE_ACTIONS = {"circuit_reset"}


def action_is_write(action_type: str) -> bool:
    return action_type in WRITE_ACTIONS


async def is_action_enabled(pool, action_type: str) -> bool:
    """Gate check : is the given action_type currently accepted on this pool ?

    Token-guardian invariant 11 enforcement point.
    """
    if action_type in READ_ACTIONS:
        return await is_query_enabled(pool)
    if action_type in WRITE_ACTIONS:
        return await is_writes_enabled(pool)
    return False


async def get_cooldown(pool, action_type: str) -> int:
    key = f"federation_admin_cooldown_{action_type}_seconds"
    val = await _config_get(pool, key, None)
    if val is None:
        return DEFAULT_COOLDOWN_CIRCUIT_RESET_SEC if action_type == "circuit_reset" else 0
    try:
        return int(val)
    except Exception:
        return DEFAULT_COOLDOWN_CIRCUIT_RESET_SEC


async def get_max_pending(pool) -> int:
    val = await _config_get(pool, "federation_admin_max_pending_per_peer",
                            str(DEFAULT_MAX_PENDING_PER_PEER))
    try:
        return int(val)
    except Exception:
        return DEFAULT_MAX_PENDING_PER_PEER


async def get_query_events_whitelist(pool) -> list:
    raw = await _config_get(pool, "federation_admin_query_events_whitelist", None)
    if not raw:
        return list(DEFAULT_QUERY_EVENTS_WHITELIST)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return list(DEFAULT_QUERY_EVENTS_WHITELIST)


# ---- Audit log (append-only, local) ----

async def audit_log(
    pool,
    request_id: str,
    side: str,                       # 'emitter' | 'target'
    event_type: str,                 # 'created', 'approved', ...
    actor_email: Optional[str] = None,
    actor_atom_id: Optional[str] = None,
    action_type: Optional[str] = None,
    payload: Optional[dict] = None,
    notes: Optional[str] = None,
) -> None:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO federation_admin_actions_log
                (request_id, side, event_type, actor_email, actor_atom_id,
                 action_type, payload, notes)
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
                """,
                request_id, side, event_type,
                actor_email, actor_atom_id, action_type,
                json.dumps(payload or {}), notes,
            )
    except Exception as e:
        log.warning(f"audit_log failed: {e}")


# ---- Guard rails (load-bearing) ----

async def slashing_events_pending(pool, worker_id: Optional[str] = None) -> list:
    """Retourne les events slashing pending/contested.

    Si worker_id est None : tous les events du pool.
    Sinon : events pour ce worker specifiquement.
    """
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return []
    try:
        async with pool.store.pool.acquire() as conn:
            # Verifier existence table slashing_events (scaffold M10)
            exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name='slashing_events'"
            )
            if not exists:
                return []
            if worker_id:
                rows = await conn.fetch(
                    "SELECT id, worker_id, status, reason, created_at FROM slashing_events "
                    "WHERE worker_id=$1 AND status NOT IN ('settled','burned','dismissed') "
                    "ORDER BY created_at DESC LIMIT 50",
                    worker_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, worker_id, status, reason, created_at FROM slashing_events "
                    "WHERE status NOT IN ('settled','burned','dismissed') "
                    "ORDER BY created_at DESC LIMIT 50",
                )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"slashing_events_pending query failed: {e}")
        return []


async def cooldown_violation(pool, from_atom_id: str, action_type: str) -> Optional[int]:
    """Retourne le nombre de secondes restantes si cooldown actif, None sinon."""
    cooldown_sec = await get_cooldown(pool, action_type)
    if cooldown_sec <= 0:
        return None
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return None
    try:
        async with pool.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT EXTRACT(EPOCH FROM (now() - MAX(decided_at)))::int AS since_sec
                FROM federation_admin_requests
                WHERE direction='inbound'
                  AND from_atom_id=$1
                  AND action_type=$2
                  AND status IN ('approved','executed','executed_no_callback')
                """,
                from_atom_id, action_type,
            )
        if not row or row["since_sec"] is None:
            return None
        since = int(row["since_sec"])
        if since < cooldown_sec:
            return cooldown_sec - since
        return None
    except Exception as e:
        log.warning(f"cooldown_violation check failed: {e}")
        return None


async def pending_inbound_count(pool, from_atom_id: str) -> int:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return 0
    try:
        async with pool.store.pool.acquire() as conn:
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM federation_admin_requests "
                "WHERE direction='inbound' AND from_atom_id=$1 "
                "  AND status='pending' AND expires_at > now()",
                from_atom_id,
            ) or 0)
    except Exception:
        return 0


# ---- Request lifecycle ----

async def create_inbound_request(
    pool,
    request_id: str,
    from_atom_id: str,
    from_admin_email: Optional[str],
    to_atom_id: str,
    action_type: str,
    action_params: dict,
    envelope_sig: Optional[str],
    envelope_nonce: Optional[str],
) -> dict:
    """Crée une demande inbound pending. Retourne dict status ou erreur."""
    if action_type not in ALLOWED_ACTIONS:
        return {"ok": False, "error": f"action_type not whitelisted: {action_type}", "status_code": 400}

    max_pending = await get_max_pending(pool)
    current_pending = await pending_inbound_count(pool, from_atom_id)
    if current_pending >= max_pending:
        return {"ok": False, "error": f"max_pending_per_peer exceeded ({current_pending}/{max_pending})", "status_code": 429}

    cooldown_remaining = await cooldown_violation(pool, from_atom_id, action_type)
    if cooldown_remaining is not None:
        return {
            "ok": False,
            "error": f"cooldown active, retry in {cooldown_remaining}s",
            "status_code": 429,
            "cooldown_remaining_sec": cooldown_remaining,
        }

    expires_at_ts = int(time.time()) + REQUEST_TTL_SEC

    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"ok": False, "error": "store unavailable", "status_code": 503}

    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO federation_admin_requests
                (request_id, direction, from_atom_id, to_atom_id,
                 from_admin_email, action_type, action_params, status,
                 expires_at, envelope_sig, envelope_nonce)
                VALUES ($1,'inbound',$2,$3,$4,$5,$6::jsonb,'pending',
                        to_timestamp($7),$8,$9)
                """,
                request_id, from_atom_id, to_atom_id,
                from_admin_email, action_type,
                json.dumps(action_params or {}),
                expires_at_ts, envelope_sig, envelope_nonce,
            )
    except Exception as e:
        log.warning(f"create_inbound_request failed: {e}")
        return {"ok": False, "error": f"db error: {e}", "status_code": 500}

    await audit_log(
        pool, request_id, side="target", event_type="created",
        actor_email=from_admin_email, actor_atom_id=from_atom_id,
        action_type=action_type, payload={"params": action_params},
        notes="inbound request stored pending",
    )
    return {"ok": True, "request_id": request_id, "status": "pending",
            "expires_at": expires_at_ts}


async def record_outbound_request(
    pool,
    request_id: str,
    from_atom_id: str,
    to_atom_id: str,
    admin_email_label: Optional[str],
    action_type: str,
    action_params: dict,
) -> None:
    """Trace cote emetteur qu'on a envoye une request (pour suivi UI)."""
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return
    expires_at_ts = int(time.time()) + REQUEST_TTL_SEC
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO federation_admin_requests
                (request_id, direction, from_atom_id, to_atom_id,
                 from_admin_email, action_type, action_params, status, expires_at)
                VALUES ($1,'outbound',$2,$3,$4,$5,$6::jsonb,'pending',
                        to_timestamp($7))
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id, from_atom_id, to_atom_id,
                admin_email_label, action_type,
                json.dumps(action_params or {}), expires_at_ts,
            )
    except Exception as e:
        log.warning(f"record_outbound_request failed: {e}")
        return

    await audit_log(
        pool, request_id, side="emitter", event_type="created",
        actor_email=admin_email_label, actor_atom_id=from_atom_id,
        action_type=action_type, payload={"params": action_params},
        notes="outbound request sent",
    )


async def list_inbound_pending(pool) -> list:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return []
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT request_id, created_at, expires_at, from_atom_id,
                       from_admin_email, to_atom_id, action_type, action_params,
                       status
                FROM federation_admin_requests
                WHERE direction='inbound' AND status='pending' AND expires_at > now()
                ORDER BY created_at DESC
                LIMIT 200
                """
            )
        out = []
        for r in rows:
            d = dict(r)
            # params est jsonb -> renvoyer tel quel
            if isinstance(d.get("action_params"), str):
                try:
                    d["action_params"] = json.loads(d["action_params"])
                except Exception:
                    pass
            out.append(d)
        return out
    except Exception as e:
        log.warning(f"list_inbound_pending failed: {e}")
        return []


async def list_outbound_recent(pool, limit: int = 100) -> list:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return []
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT request_id, created_at, expires_at, decided_at, to_atom_id,
                       action_type, action_params, status, execution_result,
                       execution_error, slashing_block_override
                FROM federation_admin_requests
                WHERE direction='outbound'
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        out = []
        for r in rows:
            d = dict(r)
            for k in ("action_params", "execution_result"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            out.append(d)
        return out
    except Exception as e:
        log.warning(f"list_outbound_recent failed: {e}")
        return []


async def get_request(pool, request_id: str, direction: Optional[str] = None) -> Optional[dict]:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return None
    try:
        async with pool.store.pool.acquire() as conn:
            if direction:
                row = await conn.fetchrow(
                    "SELECT * FROM federation_admin_requests "
                    "WHERE request_id=$1 AND direction=$2",
                    request_id, direction,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM federation_admin_requests WHERE request_id=$1",
                    request_id,
                )
        if not row:
            return None
        d = dict(row)
        for k in ("action_params", "execution_result", "slashing_pending_at_decision"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        return d
    except Exception as e:
        log.warning(f"get_request failed: {e}")
        return None


# ---- Action executors (called AFTER approve on target side) ----

async def _execute_circuit_reset(pool, params: dict) -> dict:
    """Vide blacklist + reset checker_scores.

    Reuse les mecaniques existantes du pool si presentes, sinon no-op gracieux.
    """
    cleared_blacklist = 0
    reset_scores = 0
    try:
        if hasattr(pool.store, "pool") and pool.store.pool:
            async with pool.store.pool.acquire() as conn:
                # Blacklist workers (si table existe)
                try:
                    exists = await conn.fetchval(
                        "SELECT 1 FROM information_schema.tables WHERE table_name='worker_blacklist'"
                    )
                    if exists:
                        cleared_blacklist = int(await conn.fetchval(
                            "WITH d AS (DELETE FROM worker_blacklist RETURNING 1) "
                            "SELECT COUNT(*) FROM d"
                        ) or 0)
                except Exception as e:
                    log.warning(f"circuit_reset blacklist clear failed: {e}")

                # checker_scores reset a 1.0 (table name variable selon versions)
                for table in ("checker_scores", "llm_checker_scores", "worker_scores"):
                    try:
                        exists = await conn.fetchval(
                            "SELECT 1 FROM information_schema.tables WHERE table_name=$1",
                            table,
                        )
                        if exists:
                            reset_scores = int(await conn.fetchval(
                                f"WITH u AS (UPDATE {table} SET score=1.0 RETURNING 1) "
                                "SELECT COUNT(*) FROM u"
                            ) or 0)
                            break
                    except Exception:
                        continue
    except Exception as e:
        log.warning(f"_execute_circuit_reset failed: {e}")
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "cleared_blacklist": cleared_blacklist,
        "reset_scores": reset_scores,
    }


async def _execute_query_events(pool, params: dict, self_atom_id: str) -> dict:
    """Retourne molecule_events filtres selon whitelist + fenetre temporelle.

    Filtres (invariants token-guardian) :
      - event_type IN whitelist (pool_config)
      - target_atom_id IS NULL OR target_atom_id = self_atom_id  (pas d'events sur tiers)
      - ts > now() - 7 days
    """
    whitelist = await get_query_events_whitelist(pool)
    requested_types = params.get("event_types") or whitelist
    # Intersection stricte avec whitelist (token-guardian invariant)
    effective_types = [t for t in requested_types if t in whitelist]
    if not effective_types:
        return {"ok": True, "events": [], "note": "no whitelisted types matched"}

    limit = min(int(params.get("limit", 100)), 500)

    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return {"ok": True, "events": []}

    try:
        async with pool.store.pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.tables WHERE table_name='molecule_events'"
            )
            if not exists:
                return {"ok": True, "events": [], "note": "molecule_events not present"}

            rows = await conn.fetch(
                f"""
                SELECT ts, query_type, target_atom_id, result_status,
                       unreachable, summary, latency_ms
                FROM molecule_events
                WHERE ts > now() - INTERVAL '{QUERY_EVENTS_WINDOW_DAYS} days'
                  AND query_type = ANY($1::text[])
                  AND (target_atom_id IS NULL OR target_atom_id = $2)
                ORDER BY ts DESC
                LIMIT $3
                """,
                effective_types, self_atom_id, limit,
            )
        # Stripper admin_email (PII, non-pertinent cross-pool)
        events = []
        for r in rows:
            d = dict(r)
            d["ts"] = d["ts"].isoformat() if d.get("ts") else None
            events.append(d)
        return {"ok": True, "events": events, "count": len(events),
                "whitelist_applied": effective_types,
                "window_days": QUERY_EVENTS_WINDOW_DAYS}
    except Exception as e:
        log.warning(f"_execute_query_events failed: {e}")
        return {"ok": False, "error": str(e)}


async def execute_action(pool, req: dict) -> dict:
    """Execute l'action apres approve local. Retourne dict execution_result."""
    self_atom_id = pool.federation_self.atom_id if pool.federation_self else ""
    action_type = req["action_type"]
    params = req.get("action_params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}

    if action_type == "circuit_reset":
        return await _execute_circuit_reset(pool, params)
    if action_type == "query_events":
        return await _execute_query_events(pool, params, self_atom_id)
    return {"ok": False, "error": f"unknown action_type: {action_type}"}


async def mark_decided(
    pool,
    request_id: str,
    decision: str,                           # 'approved' | 'rejected'
    decided_by_email: Optional[str],
    decision_note: Optional[str],
    execution_result: Optional[dict] = None,
    execution_error: Optional[str] = None,
    slashing_block_override: bool = False,
    slashing_snapshot: Optional[list] = None,
) -> None:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return

    final_status = decision
    if decision == "approved":
        # Si execution reussie -> executed, sinon failed
        if execution_error:
            final_status = "failed"
        else:
            final_status = "executed"

    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE federation_admin_requests
                SET status=$2,
                    decided_at=now(),
                    decided_by_email=$3,
                    decision_note=$4,
                    execution_result=$5::jsonb,
                    execution_error=$6,
                    slashing_block_override=$7,
                    slashing_pending_at_decision=$8::jsonb
                WHERE request_id=$1 AND direction='inbound'
                """,
                request_id, final_status, decided_by_email, decision_note,
                json.dumps(execution_result) if execution_result else None,
                execution_error, bool(slashing_block_override),
                json.dumps(slashing_snapshot) if slashing_snapshot else None,
            )
    except Exception as e:
        log.warning(f"mark_decided failed: {e}")


async def mark_outbound_callback(
    pool,
    request_id: str,
    callback_status: str,                    # 'approved' | 'rejected' | 'executed' | 'failed'
    execution_result: Optional[dict] = None,
    execution_error: Optional[str] = None,
) -> None:
    if not (hasattr(pool.store, "pool") and pool.store.pool):
        return
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE federation_admin_requests
                SET status=$2,
                    decided_at=now(),
                    execution_result=$3::jsonb,
                    execution_error=$4
                WHERE request_id=$1 AND direction='outbound'
                """,
                request_id, callback_status,
                json.dumps(execution_result) if execution_result else None,
                execution_error,
            )
    except Exception as e:
        log.warning(f"mark_outbound_callback failed: {e}")


def new_request_id() -> str:
    return f"fadm_{uuid.uuid4().hex[:16]}"
