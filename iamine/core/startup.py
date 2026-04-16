"""Bootstrap / startup sequence for the IAMINE pool."""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("iamine.pool")


async def initialize_pool(pool) -> None:
    """Bootstrap complet du pool — appelé par @app.on_event("startup")."""

    # 1. Init PostgresStore si configuré
    if os.environ.get("IAMINE_DB") == "postgres":
        from ..db import PostgresStore
        pg_store = PostgresStore(dsn="",
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "5432")),
            user=os.environ.get("DB_USER", "harpersatrage"),
            password=os.environ.get("DB_PASS", ""),
            database=os.environ.get("DB_NAME", "iamine"),
        )
        try:
            await pg_store.connect()
            pool.store = pg_store
            log.info("L3 PostgreSQL store active — production mode")
        except Exception as e:
            log.error(f"CRITICAL: PostgreSQL connection failed: {e}")
            log.error("Pool CANNOT run without PostgreSQL in production. Fix DB config.")
            pool._pg_failed = True

    # 1bis. Bootstrap federation identity (M3) — no-op si IAMINE_FED=off
    try:
        from ..core.federation import initialize_federation
        await initialize_federation(pool)
    except Exception as e:
        log.error(f"federation init failed (non-fatal): {e}")

    # 2. Charger la config depuis pool_config (system prompt, checker, etc.)
    await _load_pool_config(pool)

    # 3. Charger les machines connues depuis la DB (bonus anti-doublon)
    await _load_known_machines(pool)

    # 4. Charger les comptes depuis PostgreSQL (source de vérité)
    from ..core.accounts import _load_accounts_from_db
    await _load_accounts_from_db()

    # 5. Lancer les loops (heartbeat, credit_sync, drain)
    asyncio.create_task(pool.heartbeat_loop())
    asyncio.create_task(pool._credit_sync_loop())

    from ..core.heartbeat import drain_pending_jobs_loop
    asyncio.create_task(drain_pending_jobs_loop(pool))

    log.info("Heartbeat loop started (every 30s, timeout 90s)")
    log.info("Credit sync loop started (every 60s)")
    log.info("Drain pending jobs loop started (every 2s)")

    # 5bis. M11.2 — ledger gossip loop (anti-entropy). No-op unless
    # REPLICATION_ENABLED=true. The loop itself re-checks the flag at
    # each iteration so it can be toggled live without restart.
    try:
        from ..core.federation_replication import replication_ledger_gossip_loop
        asyncio.create_task(replication_ledger_gossip_loop(pool))
        log.info("M11.2 ledger gossip loop scheduled (flag-gated)")
    except Exception as _re:
        log.warning(f"M11.2 gossip loop schedule failed (non-fatal): {_re}")

    # 5ter. M11.5 Phase 2 — memory gossip loop (conversations + T3/T4/episodes).
    # No-op unless MEMORY_REPLICATION_ENABLED=true AND MEMORY_GOSSIP_LOOP_ENABLED=true.
    try:
        from ..core.memory_replication import memory_gossip_loop
        asyncio.create_task(memory_gossip_loop(pool))
        log.info("M11.5 memory gossip loop scheduled (flag-gated)")
    except Exception as _re:
        log.warning(f"M11.5 gossip loop schedule failed (non-fatal): {_re}")

    # 6. Sync les tokens avec les comptes au démarrage (différé 10s)
    from ..core.accounts import _sync_account_tokens
    asyncio.get_event_loop().call_later(10, _sync_account_tokens)


async def _load_pool_config(pool) -> None:
    """Charge pool_config depuis PostgreSQL (system_prompt, checker, etc.)."""
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM pool_config")
            config = {r["key"]: r["value"] for r in rows}
            if "system_prompt" in config:
                pool.SYSTEM_PROMPT = config["system_prompt"]
            if "welcome_bonus" in config:
                pool.WELCOME_BONUS = float(config["welcome_bonus"])
            if "preprod_mode" in config:
                pool.PREPROD_MODE = config["preprod_mode"].lower() == "true"
            if "active_family" in config:
                from ..models import set_active_family, get_active_family
                if set_active_family(config["active_family"]):
                    log.info(f"Model family loaded from DB: {config['active_family']}")
                else:
                    log.warning(f"Unknown family in DB: {config['active_family']}, keeping {get_active_family()}")
            # Checker ladder — toutes les clés checker_* depuis pool_config
            _checker_keys = {
                "checker_enabled": ("CHECKER_ENABLED", lambda v: v.lower() == "true"),
                "checker_tps_threshold": ("CHECKER_TPS_THRESHOLD", float),
                "checker_timeout": ("CHECKER_TIMEOUT", int),
                "checker_max_tokens": ("CHECKER_MAX_TOKENS", int),
                "checker_fail_max": ("CHECKER_FAIL_MAX", int),
                "checker_score_decay": ("CHECKER_SCORE_DECAY", float),
                "checker_score_recovery": ("CHECKER_SCORE_RECOVERY", float),
                "checker_min_score": ("CHECKER_MIN_SCORE", float),
                "checker_sample_rate": ("CHECKER_SAMPLE_RATE", int),
            }
            for db_key, (attr, cast) in _checker_keys.items():
                if db_key in config:
                    try:
                        setattr(pool, attr, cast(config[db_key]))
                    except (ValueError, TypeError):
                        pass
            log.info(f"Pool config loaded from DB: {len(config)} keys")
            if "tool_routing_model" in config:
                pool.tool_routing_model = config["tool_routing_model"]
                log.info(f"Tool routing model: {pool.tool_routing_model}")
            if "blacklist" in config:
                import json as _json
                try:
                    pool._blacklist = set(_json.loads(config["blacklist"]))
                    if pool._blacklist:
                        log.info(f"Blacklist: {pool._blacklist}")
                except Exception:
                    pool._blacklist = set()
    except Exception as e:
        log.warning(f"Failed to load pool_config: {e}")


async def _load_known_machines(pool) -> None:
    """Charge les machine IDs connus depuis la table workers (anti-doublon bonus).

    Source de verite : table `workers` (has first_seen, persisted on first
    join via _update_worker_db). Previous implementation read from
    `api_tokens` which is always empty because tokens are derived in RAM
    and never persisted -> every restart wrongly gave +500 IAMINE bonus
    to every reconnecting worker.
    """
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return
    try:
        async with pool.store.pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT worker_id FROM workers")
            for row in rows:
                pool._known_machines.add(row["worker_id"])
            log.info(f"Loaded {len(pool._known_machines)} known machines from DB")
    except Exception as e:
        log.warning(f"Failed to load known machines: {e}")


def print_banner(host: str, port: int) -> None:
    """Affiche le banner IAMINE au démarrage."""
    from iamine import __version__
    print()
    print(f" * CELLULE.AI POOL  v{__version__}")
    print(f" * LISTEN       {host}:{port}")
    print(f" * API          http://{host}:{port}/v1/chat/completions")
    print(f" * STATUS       http://{host}:{port}/v1/status")
    print(f" * WEBSOCKET    ws://{host}:{port}/ws")
    print()


def get_pipeline(pool_instance):
    """Lazy-init du Pipeline (utilitaire interne, utilisé par routes)."""
    from ..pipeline import Pipeline
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline(pool_instance)
    return _pipeline

_pipeline = None
