"""Credit management, rate limiting & loyalty rewards — extracted from pool.py (refactoring step 7)."""

from __future__ import annotations

import asyncio
import logging
import random
import time

log = logging.getLogger("iamine.credits")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def check_rate_limit(pool, source: str) -> bool:
    """Verifie le rate limit. Retourne True si la requete est autorisee."""
    now = time.time()
    window = 60.0
    if source not in pool._rate_counters:
        pool._rate_counters[source] = []
    # Nettoyer les vieux timestamps
    pool._rate_counters[source] = [t for t in pool._rate_counters[source] if now - t < window]
    if len(pool._rate_counters[source]) >= pool.RATE_LIMIT_PER_MIN:
        return False
    pool._rate_counters[source].append(now)
    return True


# ---------------------------------------------------------------------------
# Worker DB updates
# ---------------------------------------------------------------------------

async def update_worker_db(pool, worker_id: str, info: dict):
    """Met a jour version et status en DB quand un worker se connecte."""
    try:
        version = info.get("version", "")
        async with pool.store.pool.acquire() as conn:
            await conn.execute("""
                UPDATE workers SET version=$2, status='online', is_online=true, last_seen=NOW()
                WHERE worker_id=$1
            """, worker_id, version)
    except Exception as e:
        log.debug(f"Failed to update worker DB for {worker_id}: {e}")


async def save_benchmark(pool, worker_id: str, info: dict):
    """Sauvegarde le benchmark worker en PostgreSQL."""
    try:
        async with pool.store.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO worker_benchmarks (worker_id, model, bench_tps, cpu_info, ram_gb, has_gpu, gpu_info, gpu_vram_gb, version)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (worker_id) DO UPDATE SET
                    model=EXCLUDED.model, bench_tps=EXCLUDED.bench_tps,
                    cpu_info=EXCLUDED.cpu_info, ram_gb=EXCLUDED.ram_gb,
                    has_gpu=EXCLUDED.has_gpu, gpu_info=EXCLUDED.gpu_info,
                    gpu_vram_gb=EXCLUDED.gpu_vram_gb, version=EXCLUDED.version,
                    last_updated=NOW()
            """, worker_id, info.get("model_path", ""), info.get("bench_tps", 0),
                info.get("cpu", ""), info.get("ram_total_gb", 0),
                info.get("has_gpu", False), info.get("gpu", ""),
                info.get("gpu_vram_gb", 0), info.get("version", ""))
        # Enrichir hardware_benchmarks (base hashrate)
        cpu = info.get("cpu", "")
        tps = info.get("bench_tps", 0)
        model_path = info.get("model_path", "")
        if cpu and tps and tps > 0 and model_path:
            from ..models import MODEL_REGISTRY
            for m in MODEL_REGISTRY:
                if m.hf_file in model_path:
                    # Ne pas polluer la DB avec des bench sous le seuil
                    if tps < m.min_tps_useful:
                        log.debug(f"Hardware DB skip: {cpu} + {m.id} = {tps:.1f} t/s < {m.min_tps_useful}")
                        break
                    gpu = info.get("gpu", "") if info.get("has_gpu") else ""
                    await pool.store.upsert_hardware_benchmark(
                        cpu, gpu, info.get("ram_total_gb", 0), m.id, tps)
                    log.info(f"Hardware DB: {cpu} + {m.id} = {tps:.1f} t/s")
                    break
    except Exception as e:
        log.warning(f"Failed to save benchmark for {worker_id}: {e}")


# ---------------------------------------------------------------------------
# Memory & RAG helpers
# ---------------------------------------------------------------------------

def is_memory_enabled(pool, api_token: str) -> bool:
    """Verifie si la memoire persistante est activee pour ce token."""
    from .accounts import _accounts
    for acc in _accounts.values():
        if acc.get("account_token") == api_token:
            return acc.get("memory_enabled", False)
    return False


async def embed_facts(pool, api_token: str, summary: str, conv_id: str):
    """Background : vectorise les faits d une compaction pour le RAG."""
    try:
        from ..memory import store_facts
        count = await store_facts(pool.store, api_token, summary, conv_id)
        if count:
            log.info(f"RAG embed: {count} facts stored for conv={conv_id}")
    except Exception as e:
        log.debug(f"RAG embed failed for {conv_id}: {e}")


async def save_conv_background(pool, conv):
    """Background : sauvegarde la conversation en DB (persistante)."""
    try:
        # Generer un titre auto a partir du premier message user
        title = ""
        for m in conv.messages:
            if m.get("role") == "user":
                title = m.get("content", "")[:100]
                break
        await pool.store.save_conversation_state(
            conv.conv_id, conv.api_token, conv.messages,
            conv._summary, title, conv.total_tokens)
    except Exception as e:
        log.warning(f"Conv save failed for {conv.conv_id}: {e}")


# ---------------------------------------------------------------------------
# Credit worker after a job
# ---------------------------------------------------------------------------

def credit_worker_for_job(pool, worker, result: dict):
    """Credite le worker apres un job termine : 100 tokens = 1 $IAMINE.

    Retourne le montant credite.
    """
    tokens_gen = result.get("tokens_generated", 0)
    credit = tokens_gen / 100.0  # 100 tokens = 1 $IAMINE
    api_token = worker.info.get("api_token")
    if api_token and api_token in pool.api_tokens and credit > 0:
        pool.api_tokens[api_token]["credits"] += credit
        pool.api_tokens[api_token]["total_earned"] = pool.api_tokens[api_token].get("total_earned", 0) + credit
        log.info(f"Worker {worker.worker_id} +{credit:.2f} $IAMINE ({tokens_gen} tokens) — total: {pool.api_tokens[api_token]['credits']:.2f}")
    return credit


# ---------------------------------------------------------------------------
# Loyalty rewards (called from heartbeat_loop)
# ---------------------------------------------------------------------------

async def loyalty_rewards(pool):
    """Chance aleatoire de crediter un worker en ligne.

    Anti-farming: worker doit avoir fait >= 1 job ET etre connecte > 5 min.
    Appele depuis heartbeat_loop avec ~30% de chance par cycle.
    """
    if not pool.workers or random.random() >= 0.3:
        return

    eligible = [w for w in pool.workers.values()
                if w.jobs_done > 0
                and time.time() - w.connected_at > 300]
    if not eligible:
        return

    lucky = random.choice(eligible)
    token = lucky.info.get("api_token")
    if not token or token not in pool.api_tokens:
        return

    # Reward entre 0.5 et 5.0 $IAMINE, avec rare jackpot
    roll = random.random()
    now_ts = time.time()
    last_jackpot = pool.api_tokens[token].get("_last_jackpot", 0)
    if roll < 0.01 and (now_ts - last_jackpot) > 3600:  # 1% jackpot, max 1/heure
        reward = round(random.uniform(20.0, 50.0), 1)
        label = "JACKPOT"
        pool.api_tokens[token]["_last_jackpot"] = now_ts
    elif roll < 0.10:  # 9% gros bonus
        reward = round(random.uniform(5.0, 15.0), 1)
        label = "BONUS"
    else:  # 90% reward normal
        reward = round(random.uniform(0.5, 3.0), 1)
        label = "REWARD"
    pool.api_tokens[token]["credits"] += reward
    pool.api_tokens[token]["total_earned"] = pool.api_tokens[token].get("total_earned", 0) + reward
    log.info(f"LOYALTY {label}: {lucky.worker_id} +{reward} $IAMINE")
    # Notifier le worker
    try:
        await lucky.ws.send_json({
            "type": "reward",
            "amount": reward,
            "label": label,
            "credits": round(pool.api_tokens[token]["credits"], 2),
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Credit sync loop (RAM -> PostgreSQL)
# ---------------------------------------------------------------------------

async def credit_sync_loop(pool):
    """Sync les credits RAM -> PostgreSQL toutes les 60s."""
    while True:
        await asyncio.sleep(60)
        if not hasattr(pool.store, "pool") or not pool.store.pool:
            continue
        try:
            async with pool.store.pool.acquire() as conn:
                for token, data in list(pool.api_tokens.items()):
                    await conn.execute("""
                        UPDATE api_tokens SET credits=$2, total_earned=$3
                        WHERE token=$1
                    """, token, data.get("credits", 0), data.get("total_earned", 0))
            log.debug(f"Credit sync: {len(pool.api_tokens)} tokens synced to DB")
        except Exception as e:
            log.warning(f"Credit sync failed: {e}")
