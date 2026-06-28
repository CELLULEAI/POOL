"""core/compaction.py — Compaction distribuée extraite de pool.py.

Fonctions standalone qui opèrent sur le pool via l'interface publique.
"""

import asyncio
import logging
import os
import time

log = logging.getLogger("iamine.pool")


def _compact_cooldown_active(pool, key: str) -> bool:
    """True si la conv a deja ete compactee il y a moins de IAMINE_COMPACT_COOLDOWN_SEC.

    Garde anti-emballement : un client qui martele UNE conversation declenchait
    auparavant une compaction a chaque message (~20/h), monopolisant les helpers
    (souvent le worker le plus fort). Le cooldown plafonne a 1 compaction par
    conv et par fenetre. Reglable ; 0 desactive le garde.
    """
    cooldown = float(os.environ.get("IAMINE_COMPACT_COOLDOWN_SEC", "30"))
    if cooldown <= 0:
        return False
    last = pool._last_compaction.get(key, 0.0)
    return (time.time() - last) < cooldown


def _inference_reserve() -> int:
    """Nombre de workers idle a toujours garder pour l'inference utilisateur."""
    try:
        return int(os.environ.get("IAMINE_INFERENCE_RESERVE", "1"))
    except ValueError:
        return 1


# ---------------------------------------------------------------------------
# async_compact  (fire-and-forget helper)
# ---------------------------------------------------------------------------

async def async_compact(pool, helper, prompt, conv_id, conv, source_worker):
    """Compactage fire-and-forget avec timeout — ne bloque jamais un worker."""
    try:
        helper.busy = True
        summary = await asyncio.wait_for(
            pool.delegate_task(
                helper=helper, task_type="compact",
                prompt=prompt, conv_id=conv_id, source_worker=source_worker,
            ),
            timeout=60  # max 60s pour une compaction async
        )
        if summary:
            archived = conv.compact(summary)
            if archived and conv.api_token:
                await pool.store.archive_messages(conv_id, archived, summary, conv.api_token)
            # RAG: vectorise facts in background (parity with handle_compaction immediate path)
            if conv.api_token and conv.api_token.startswith("acc_") and pool._is_memory_enabled(conv.api_token):
                asyncio.create_task(pool._embed_facts(conv.api_token, summary, conv_id))
            log.info(f"Async compaction done for {conv_id}")
        else:
            conv.compact("Previous conversation context.")
    except asyncio.TimeoutError:
        log.warning(f"Async compaction timeout for {conv_id} — releasing {helper.worker_id}")
    except Exception as e:
        log.warning(f"Async compaction failed for {conv_id}: {e}")
        conv.compact("Previous conversation context.")
    finally:
        helper.busy = False
        pool._worker_freed.set()


# ---------------------------------------------------------------------------
# handle_compaction  (remplace le bloc === COMPACTAGE DISTRIBUÉ === )
# ---------------------------------------------------------------------------

async def handle_compaction(pool, conv, worker, conv_id, budget, tools):
    """Vérifie et lance la compaction (immediate ou deferred).

    Appelé depuis submit_job après l'acquisition du worker.
    """
    if tools:
        return

    worker_ctx = worker.info.get("ctx_size", 2048)

    if not conv.needs_compaction(worker_ctx):
        return

    # Garde anti-emballement : 1 compaction max par conv et par fenetre de cooldown.
    if _compact_cooldown_active(pool, conv_id):
        log.debug(f"Compaction cooldown actif pour {conv_id} — saute")
        return

    compact_prompt = pool.router.check_and_compact(conv, worker_ctx)
    if not compact_prompt:
        return

    if budget == "suspended":
        log.info(f"Compaction deferred for {conv_id}: pool load {pool.pool_load}% — suspended")
        return

    # On s'engage a compacter : armer le cooldown des maintenant (avant dispatch)
    # pour qu'un client qui martele ne reessaie pas a chaque message.
    pool._last_compaction[conv_id] = time.time()

    # Chercher un worker plus fort, sinon même tier, sinon self-compact.
    # reserve : garder au moins N workers idle pour l'inference utilisateur.
    reserve = _inference_reserve()
    helper = pool.get_idle_worker(exclude=worker.worker_id, prefer_stronger=True, reserve=reserve)
    if not helper:
        # Fallback : worker de même tier ou n'importe quel idle
        helper = pool.get_idle_worker(exclude=worker.worker_id, prefer_stronger=False, reserve=reserve)

    if not helper:
        # Self-compaction : le worker fait son propre résumé
        helper = worker
        log.info(f"Self-compaction for {conv_id}: {worker.worker_id} (no external helper)")
    elif budget == "deferred":
        # Fire-and-forget — ne bloque pas le job en cours
        log.info(f"Compaction deferred (async) for {conv_id}: pool load {pool.pool_load}%")
        asyncio.create_task(async_compact(
            pool, helper, compact_prompt, conv_id, conv, worker.worker_id))
        return
    else:
        pass  # immediate — synchrone ci-dessous

    # Immediate — synchrone avec timeout
    try:
        helper.busy = True
        summary_result = await asyncio.wait_for(
            pool.delegate_task(
                helper=helper, task_type="compact",
                prompt=compact_prompt, conv_id=conv_id,
                source_worker=worker.worker_id,
            ), timeout=90
        )
        if summary_result:
            archived = conv.compact(summary_result)
            if archived and conv.api_token:
                await pool.store.archive_messages(conv_id, archived, summary_result, conv.api_token)
            # RAG : vectoriser les faits en background
            if conv.api_token and conv.api_token.startswith("acc_") and pool._is_memory_enabled(conv.api_token):
                asyncio.create_task(pool._embed_facts(conv.api_token, summary_result, conv_id))
        else:
            conv.compact("Previous conversation covered multiple topics.")
    except asyncio.TimeoutError:
        log.warning(f"Compaction timeout for {conv_id} — releasing {helper.worker_id}")
    except Exception as e:
        log.warning(f"Compaction failed for {conv_id}: {e}")
        conv.compact("Previous conversation context.")
    finally:
        helper.busy = False
        pool._worker_freed.set()


# ---------------------------------------------------------------------------
# handle_meta_compaction  (remplace le bloc === META-COMPACTION === )
# ---------------------------------------------------------------------------

async def handle_meta_compaction(pool, conv, worker, conv_id, budget, tools):
    """Vérifie et lance la meta-compaction (fusion de summaries).

    Appelé depuis submit_job après handle_compaction.
    """
    if tools:
        return

    if not conv.needs_meta_compaction() or budget == "suspended":
        return

    # Meme garde anti-emballement que la compaction (cle distincte meta:).
    if _compact_cooldown_active(pool, f"meta:{conv_id}"):
        log.debug(f"Meta-compaction cooldown actif pour {conv_id} — saute")
        return

    meta_prompt = pool.router.check_and_meta_compact(conv)
    if not meta_prompt:
        return

    reserve = _inference_reserve()
    helper = pool.get_idle_worker(exclude=worker.worker_id, prefer_stronger=True, reserve=reserve)
    if not helper:
        log.info(f"Meta-compaction skipped for {conv_id}: no stronger worker available (or reserve)")
        return

    pool._last_compaction[f"meta:{conv_id}"] = time.time()

    try:
        helper.busy = True
        condensed = await asyncio.wait_for(
            pool.delegate_task(
                helper=helper, task_type="meta_compact",
                prompt=meta_prompt, conv_id=conv_id,
                source_worker=worker.worker_id,
            ), timeout=90
        )
        if condensed:
            old_summary = conv.meta_compact(condensed)
            if conv.api_token and old_summary:
                await pool.store.archive_messages(
                    conv_id,
                    [{"role": "system", "content": f"[Archived summary]\n{old_summary}"}],
                    api_token=conv.api_token,
                )
    except asyncio.TimeoutError:
        log.warning(f"Meta-compaction timeout for {conv_id}")
    except Exception as e:
        log.warning(f"Meta-compaction failed for {conv_id}: {e}")
    finally:
        helper.busy = False
        pool._worker_freed.set()
