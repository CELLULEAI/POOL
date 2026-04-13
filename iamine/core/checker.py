"""Checker ladder -- validation synchrone par un LLM plus gros.

Extrait de pool.py (refactoring etape 4).
Toutes les fonctions prennent `pool` en premier parametre.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re

log = logging.getLogger("iamine.checker")

_MODEL_SIZE_RE = re.compile(r'[\-_](\d+(?:\.\d+)?)[Bb][\-_\.]')


def _parse_model_size(model_path: str) -> float:
    """Extrait la taille en milliards depuis un path GGUF."""
    m = _MODEL_SIZE_RE.search(model_path)
    return float(m.group(1)) if m else 0


# -- Defaults (ecrasables par Pool.CHECKER_*) --------------------------
CHECKER_ENABLED = True
CHECKER_TPS_THRESHOLD = 15.0
CHECKER_TIMEOUT = 60
CHECKER_MAX_TOKENS = 300
CHECKER_FAIL_MAX = 3
CHECKER_SCORE_DECAY = 0.1
CHECKER_SCORE_RECOVERY = 0.05
CHECKER_MIN_SCORE = 0.3
CHECKER_SAMPLE_RATE = 100


def _cfg(pool, name: str, default):
    """Lit CHECKER_<name> depuis pool (attribut de classe ou instance), fallback default."""
    return getattr(pool, name, default)


# -- Public API --------------------------------------------------------

def checker_should_check(pool, worker) -> bool:
    """Determine si ce worker doit etre verifie par le checker ladder."""
    if not _cfg(pool, "CHECKER_ENABLED", CHECKER_ENABLED):
        return False
    bench = worker.info.get("bench_tps") or worker.info.get("real_tps") or 0
    if bench >= _cfg(pool, "CHECKER_TPS_THRESHOLD", CHECKER_TPS_THRESHOLD):
        return False
    sample_rate = _cfg(pool, "CHECKER_SAMPLE_RATE", CHECKER_SAMPLE_RATE)
    if sample_rate < 100:
        if random.randint(1, 100) > sample_rate:
            return False
    return True


async def checker_review(pool, draft_text: str, messages: list[dict],
                         primary_worker, conv) -> dict | None:
    """Checker ladder : un LLM plus gros valide la reponse (synchrone).

    Retourne {"verdict": "OK"|"FAIL", "corrected": str|None, ...} ou None.
    Regle absolue : le checker doit etre un modele PLUS GROS que le worker verifie.
    """
    try:
        from ..router import SmartRouter
        excluded_roles = (
            getattr(SmartRouter, '_TOOL_ONLY', set())
            | getattr(SmartRouter, '_ASSIST_ONLY', set())
        )
    except Exception:
        excluded_roles = set()

    primary_size = _parse_model_size(primary_worker.info.get("model_path", ""))
    checker = None
    best_size = 0
    for w in pool.workers.values():
        if w.busy or w.worker_id == primary_worker.worker_id:
            continue
        if w.worker_id in excluded_roles:
            continue
        if pool._is_outdated(w) or pool._is_unknown_model(w):
            continue
        w_size = _parse_model_size(w.info.get("model_path", ""))
        if w_size > primary_size and w_size > best_size:
            w_tps = w.info.get("bench_tps") or w.info.get("real_tps") or 0
            if w_tps >= 5.0:
                checker = w
                best_size = w_size
    if not checker:
        log.debug(
            "Checker: no stronger worker available for "
            "%s (%sB)", primary_worker.worker_id, primary_size
        )
        return None

    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_msg = msg.get("content", "")[:2000]
            break

    prompt = (
        "Tu es un verificateur qualite. Analyse cette reponse et donne ton verdict.\n\n"
        "Question utilisateur :\n" + last_user_msg + "\n\n"
        "Reponse du worker :\n" + draft_text + "\n\n"
        "Instructions :\n"
        "1. La reponse est-elle correcte et pertinente ?\n"
        "2. Y a-t-il des erreurs factuelles, des hallucinations, ou du hors-sujet ?\n"
        "3. Si la reponse est mauvaise, fournis une version corrigee.\n\n"
        "Reponds EXACTEMENT dans ce format (une seule ligne pour le verdict) :\n"
        "VERDICT: OK\n"
        "ou\n"
        "VERDICT: FAIL\n"
        "CORRECTION: <ta version corrigee ici>"
    )

    timeout = _cfg(pool, "CHECKER_TIMEOUT", CHECKER_TIMEOUT)

    log.info(
        "Checker: %s (%sB) -> %s (%sB) (conv=%s)",
        primary_worker.worker_id, primary_size,
        checker.worker_id, best_size, conv.conv_id
    )

    try:
        checker.busy = True
        result_text = await asyncio.wait_for(
            pool.delegate_task(
                helper=checker,
                task_type="checker_review",
                prompt=prompt,
                conv_id=conv.conv_id,
                source_worker=primary_worker.worker_id,
            ),
            timeout=timeout,
        )
        if not result_text:
            return None

        result_text = result_text.strip()
        verdict = "OK"
        corrected = None
        for line in result_text.split("\n"):
            line_s = line.strip().upper()
            if line_s.startswith("VERDICT:"):
                v = line_s.replace("VERDICT:", "").strip()
                if "FAIL" in v:
                    verdict = "FAIL"
                else:
                    verdict = "OK"
        if verdict == "FAIL":
            if "CORRECTION:" in result_text:
                corrected = result_text.split("CORRECTION:", 1)[1].strip()
            elif "correction:" in result_text:
                corrected = result_text.split("correction:", 1)[1].strip()

        return {
            "verdict": verdict,
            "corrected": corrected if corrected and len(corrected) > 20 else None,
            "checker_id": checker.worker_id,
            "checker_size": best_size,
            "worker_size": primary_size,
        }
    except asyncio.TimeoutError:
        log.warning("Checker timeout (%ss) for %s", timeout, primary_worker.worker_id)
        return None
    except Exception as e:
        log.warning("Checker review failed: %s", e)
        return None
    finally:
        checker.busy = False
        pool._worker_freed.set()


async def checker_update_score(pool, worker, passed: bool) -> None:
    """Met a jour le score checker du worker et retrograde si necessaire."""
    old_score = worker.info.get("checker_score", 1.0)
    if passed:
        new_score = min(1.0, old_score + _cfg(pool, "CHECKER_SCORE_RECOVERY", CHECKER_SCORE_RECOVERY))
        worker.info["checker_fails"] = 0
    else:
        new_score = max(0.0, old_score - _cfg(pool, "CHECKER_SCORE_DECAY", CHECKER_SCORE_DECAY))
        worker.info["checker_fails"] = (worker.info.get("checker_fails") or 0) + 1
    worker.info["checker_score"] = round(new_score, 3)
    worker.info["checker_total"] = (worker.info.get("checker_total") or 0) + 1
    if passed:
        worker.info["checker_passed"] = (worker.info.get("checker_passed") or 0) + 1

    try:
        await pool.store.update_checker_score(worker.worker_id, passed, new_score)
    except Exception as e:
        log.debug("Checker score DB update failed: %s", e)

    fails = worker.info.get("checker_fails", 0)
    fail_max = _cfg(pool, "CHECKER_FAIL_MAX", CHECKER_FAIL_MAX)
    min_score = _cfg(pool, "CHECKER_MIN_SCORE", CHECKER_MIN_SCORE)
    if fails >= fail_max or new_score < min_score:
        await checker_demote_worker(pool, worker, new_score, fails)


async def checker_demote_worker(pool, worker, score: float, fails: int) -> None:
    """Retrograde un worker vers un modele plus petit."""
    from ..models import MODEL_REGISTRY
    current_path = worker.info.get("model_path", "")
    current_size = _parse_model_size(current_path)

    smaller = None
    smaller_size = 0
    for m in sorted(MODEL_REGISTRY, key=lambda x: _parse_model_size(x.hf_file), reverse=True):
        m_size = _parse_model_size(m.hf_file)
        if m_size < current_size and m_size > smaller_size:
            smaller = m
            smaller_size = m_size

    fail_max = _cfg(pool, "CHECKER_FAIL_MAX", CHECKER_FAIL_MAX)

    if not smaller:
        log.warning(
            "Checker demote: %s score=%.2f fails=%d "
            "-- already on smallest model (%sB), cannot demote further",
            worker.worker_id, score, fails, current_size
        )
        return

    log.warning(
        "CHECKER DEMOTE: %s %sB -> %sB (score=%.2f, fails=%d/%d)",
        worker.worker_id, current_size, smaller_size,
        score, fails, fail_max
    )

    try:
        gpu_layers = worker.info.get("gpu_layers", 0)
        await pool.store.update_worker_assignment(
            worker.worker_id, smaller.id, smaller.hf_file,
            worker.info.get("ctx_size", 2048), gpu_layers,
        )
        try:
            await worker.ws.send_json({
                "type": "reassign",
                "model_id": smaller.id,
                "model_path": smaller.hf_file,
                "reason": "checker_demote (score=%.2f, fails=%d)" % (score, fails),
            })
        except Exception:
            pass
    except Exception as e:
        log.warning("Checker demote DB update failed for %s: %s", worker.worker_id, e)


async def handle_checker(pool, result: dict, messages: list[dict],
                         worker, conv) -> dict:
    """Point d'entree unique pour le checker ladder dans submit_job.

    Modifie result in-place (ajoute result["checker"], remplace result["text"] si FAIL).
    Retourne result.
    """
    has_tools = result.get("tool_calls") or any(
        m.get("role") == "tool" for m in messages
    )
    if has_tools or not checker_should_check(pool, worker):
        return result

    try:
        check = await checker_review(pool, result["text"], messages, worker, conv)
        if check:
            passed = check["verdict"] == "OK"
            await checker_update_score(pool, worker, passed)
            result["checker"] = {
                "verdict": check["verdict"],
                "checker_id": check["checker_id"],
                "checker_size": check["checker_size"],
                "worker_score": worker.info.get("checker_score", 1.0),
            }
            if not passed and check.get("corrected"):
                log.warning(
                    "Checker FAIL: %s -- response replaced by %s (%sB)",
                    worker.worker_id, check['checker_id'], check['checker_size']
                )
                result["text"] = check["corrected"]
                result["checker"]["replaced"] = True
            elif not passed:
                log.warning(
                    "Checker FAIL: %s -- no correction available, "
                    "keeping original (score=%.2f)",
                    worker.worker_id, worker.info.get('checker_score', 1.0)
                )
            else:
                log.info(
                    "Checker OK: %s validated by %s (score=%.2f)",
                    worker.worker_id, check['checker_id'],
                    worker.info.get('checker_score', 1.0)
                )
    except Exception as e:
        log.debug("Checker skipped for %s: %s", conv.conv_id, e)

    return result
