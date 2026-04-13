"""Assist, Think Tool et Boost -- delegation entre workers.

Extrait de pool.py (refactoring etape 5).
Toutes les fonctions prennent `pool` en premier parametre.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from .agent_memory import capture_observation, ENABLED as AGENT_MEMORY_ENABLED

log = logging.getLogger("iamine.assist")

# ---------------------------------------------------------------------------
# Constantes Boost (desactive par defaut, BOOST_LOAD_THRESHOLD=0)
# ---------------------------------------------------------------------------
BOOST_LOAD_THRESHOLD = 0       # max pool_load% pour activer le boost
BOOST_MAX_USERS = 1            # max utilisateurs actifs simultanes
BOOST_ACTIVITY_WINDOW = 120    # secondes pour considerer un user "actif"
BOOST_REVIEW_TIMEOUT = 45      # timeout review en secondes
BOOST_REVIEW_MAX_TOKENS = 800  # tokens max pour le reviewer
BOOST_MIN_TPS = 5.0            # bench_tps minimum pour un reviewer

# ---------------------------------------------------------------------------
# Think tool definition
# ---------------------------------------------------------------------------
THINK_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "think",
        "description": (
            "Ask a more powerful AI model to analyze a complex problem, "
            "review code, or help with a difficult task. Use this when "
            "the task requires deep reasoning."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or task to delegate to the more powerful model",
                }
            },
            "required": ["question"],
        },
    },
}


def inject_think_tool(tools: list[dict]) -> list[dict]:
    """Injecte le tool 'think' si le client envoie peu de tools (pas OpenCode 11+)."""
    if not tools:
        return tools
    if len(tools) <= 5 and not any(
        t.get("function", {}).get("name") == "think" for t in tools
    ):
        return tools + [THINK_TOOL]
    return tools


# ---------------------------------------------------------------------------
# Helpers (etaient des methodes de Pool)
# ---------------------------------------------------------------------------

_MODEL_SIZE_RE = re.compile(r"(\d+\.?\d*)B", re.IGNORECASE)


def _parse_model_size_from_path(model_path: str) -> float:
    """Extrait la taille du modele depuis le path (ex: 30B, 9B, 4B)."""
    m = _MODEL_SIZE_RE.search(model_path)
    return float(m.group(1)) if m else 0


def _tool_only_workers() -> set:
    """Retourne les worker_ids qui sont TOOL_ONLY (dans router.py)."""
    return {"Scout-z2"}  # TODO: charger depuis config DB


def get_assist_worker(pool, exclude_worker_id: str):
    """Trouve le meilleur worker pour assister Scout (plus gros modele idle)."""
    candidates = []
    for w in pool.workers.values():
        if w.busy or w.worker_id == exclude_worker_id:
            continue
        mp = w.info.get("model_path", "")
        model_size = _parse_model_size_from_path(mp)
        if model_size <= 9:
            continue
        candidates.append((w, model_size, w.info.get("bench_tps", 0)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return candidates[0][0]


# ---------------------------------------------------------------------------
# Boost eligibility + review
# ---------------------------------------------------------------------------

def boost_eligible(pool) -> bool:
    """Verifie si le boost (review par worker plus fort) est activable.

    Conditions : pool peu charge, 1 seul utilisateur actif, pas de queue,
    au moins 2 workers idle.
    """
    threshold = getattr(pool, "BOOST_LOAD_THRESHOLD", BOOST_LOAD_THRESHOLD)
    if pool.pool_load >= threshold:
        return False
    if pool._queue_size > 0:
        return False
    idle_count = sum(1 for w in pool.workers.values() if not w.busy)
    if idle_count < 2:
        return False
    # Compter les utilisateurs uniques actifs (activite recente)
    now = time.time()
    window = getattr(pool, "BOOST_ACTIVITY_WINDOW", BOOST_ACTIVITY_WINDOW)
    max_users = getattr(pool, "BOOST_MAX_USERS", BOOST_MAX_USERS)
    active_tokens: set = set()
    for conv in pool.router._conversations.values():
        if not conv.expired and (now - conv.last_activity) < window:
            if conv.api_token:
                active_tokens.add(conv.api_token)
    if len(active_tokens) > max_users:
        return False
    return True


async def _boost_review(
    pool,
    draft_text: str,
    messages: list[dict],
    primary_worker,
    conv,
) -> dict | None:
    """Review du draft par un worker plus fort (boost mode).

    Retourne le texte ameliore ou None si pas de reviewer dispo.
    """
    if not boost_eligible(pool):
        return None

    # Chercher un worker plus fort avec bench valide (>= 5 tok/s)
    reviewer = pool.get_idle_worker(
        exclude=primary_worker.worker_id, prefer_stronger=True
    )
    # Exclure les workers sans bench ou trop lents
    if reviewer and (reviewer.info.get("bench_tps") or 0) < BOOST_MIN_TPS:
        log.info(f"Boost: {reviewer.worker_id} exclu (bench_tps={reviewer.info.get('bench_tps')})")
        reviewer = None
    if not reviewer:
        # Fallback : worker avec meilleur bench_tps ou GPU
        reviewer = pool.get_idle_worker(
            exclude=primary_worker.worker_id, prefer_stronger=False
        )
        if reviewer:
            rev_tps = reviewer.info.get("bench_tps") or 0
            pri_tps = primary_worker.info.get("bench_tps") or 0
            rev_gpu = reviewer.info.get("has_gpu", False)
            # Exclure si pas de bench, trop lent, ou pas meilleur
            if rev_tps < BOOST_MIN_TPS or (not rev_gpu and rev_tps <= pri_tps):
                reviewer = None
    if not reviewer:
        return None

    # Extraire le dernier message user
    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_msg = msg.get("content", "")[:1500]
            break

    prompt = (
        "L'utilisateur a demande :\n" + last_user_msg + "\n\n"
        "Reponse initiale :\n" + draft_text + "\n\n"
        "Ameliore cette reponse : corrige les erreurs, enrichis si pertinent, "
        "ameliore la clarte. Renvoie UNIQUEMENT la reponse amelioree."
    )

    log.info(
        f"Boost review: {primary_worker.worker_id} -> {reviewer.worker_id} "
        f"(conv={conv.conv_id})"
    )

    try:
        reviewer.busy = True
        result_text = await pool.delegate_task(
            helper=reviewer,
            task_type="boost_review",
            prompt=prompt,
            conv_id=conv.conv_id,
            source_worker=primary_worker.worker_id,
        )
        if result_text and len(result_text.strip()) > 20:
            return {
                "reviewer_id": reviewer.worker_id,
                "text": result_text.strip(),
                "tokens": len(result_text) // 4,
            }
    except Exception as e:
        log.warning(f"Boost review failed: {e}")
    finally:
        reviewer.busy = False
        pool._worker_freed.set()
    return None


# ---------------------------------------------------------------------------
# Entry-point handlers (called from pool.py submit_job)
# ---------------------------------------------------------------------------

async def handle_boost(pool, result: dict, messages: list, worker, conv, tools) -> dict:
    """=== BOOST MODE : review par un worker plus fort si pool peu charge ===

    Skip boost sur conversations courtes : l'affinite worker n'est pas encore
    etablie, et marquer un reviewer busy peut casser le routing d'un 2eme message.
    """
    if conv.total_tokens < 200:
        return result
    if not boost_eligible(pool):
        return result
    if result.get("tool_calls") or tools:
        return result

    timeout = getattr(pool, "BOOST_REVIEW_TIMEOUT", BOOST_REVIEW_TIMEOUT)
    try:
        boost = await asyncio.wait_for(
            _boost_review(pool, result.get("text", ""), messages, worker, conv),
            timeout=timeout,
        )
        if boost:
            log.info(f"Boost applied: {boost['reviewer_id']} reviewed {worker.worker_id}'s draft")
            result["text"] = boost["text"]
            result["boost"] = {"reviewer_id": boost["reviewer_id"], "tokens": boost["tokens"]}
    except asyncio.TimeoutError:
        log.info(f"Boost timeout for {conv.conv_id} -- using draft")
    except Exception as e:
        log.info(f"Boost skipped for {conv.conv_id}: {e}")
    return result


async def handle_think(pool, result: dict, messages: list, worker, tools, conv_id: str, max_tokens: int) -> dict:
    """=== THINK TOOL : intercepter le tool_call 'think' de Scout ==="""
    if not result.get("tool_calls") or not tools:
        return result

    think_calls = [tc for tc in result["tool_calls"] if tc.get("function", {}).get("name") == "think"]
    if not think_calls:
        return result

    question = ""
    try:
        args = json.loads(think_calls[0]["function"]["arguments"])
        question = args.get("question", "")
    except Exception:
        question = str(think_calls[0]["function"].get("arguments", ""))

    assist = get_assist_worker(pool, worker.worker_id)
    if not assist or not question:
        return result

    log.info(f"THINK TOOL: {worker.worker_id} asks {assist.worker_id} -- {question[:80]}")
    try:
        assist.busy = True
        think_messages = messages + [{"role": "user", "content": f"Expert analysis requested: {question}"}]
        think_result = await asyncio.wait_for(
            pool.delegate_task(
                helper=assist, task_type="think",
                messages=think_messages,
                tools=[t for t in tools if t.get("function", {}).get("name") != "think"],
                max_tokens=max_tokens, conv_id=conv_id,
                source_worker=worker.worker_id,
            ), timeout=120
        )
        if think_result:
            # Remplacer le resultat par celui du gros LLM
            if isinstance(think_result, dict):
                result["text"] = think_result.get("text", "")
                if think_result.get("tool_calls"):
                    result["tool_calls"] = think_result["tool_calls"]
                else:
                    result.pop("tool_calls", None)
            else:
                result["text"] = str(think_result)
                result.pop("tool_calls", None)
            result["pool_assist"] = {"helper_id": assist.worker_id, "type": "think"}
            log.info(f"THINK OK: {assist.worker_id} responded")
    except Exception as e:
        log.warning(f"THINK failed: {e}")
    finally:
        assist.busy = False
    return result


async def handle_pool_assist(pool, result: dict, messages: list, worker, tools, conv_id: str, max_tokens: int) -> dict:
    """=== POOL ASSIST : si Scout repond en texte (skip si think vient d'etre traite)
    deleguer au meilleur LLM plus gros pour une meilleure reponse ==="""
    if not tools:
        return result
    if result.get("tool_calls"):
        return result
    if result.get("pool_assist"):
        return result
    if worker.worker_id not in _tool_only_workers():
        return result

    assist = get_assist_worker(pool, worker.worker_id)
    if not assist:
        return result

    log.info(f"POOL_ASSIST: {worker.worker_id} stuck (text instead of tool_call) -> delegating to {assist.worker_id}")
    try:
        assist.busy = True
        assist_result = await asyncio.wait_for(
            pool.delegate_task(
                helper=assist, task_type="pool_assist",
                prompt=None, conv_id=conv_id,
                source_worker=worker.worker_id,
                messages=messages, tools=tools, max_tokens=max_tokens,
            ), timeout=120
        )
        if assist_result:
            result["text"] = assist_result.get("text", result["text"])
            if assist_result.get("tool_calls"):
                result["tool_calls"] = assist_result["tool_calls"]
            result["pool_assist"] = {"helper_id": assist.worker_id}
            log.info(f"POOL_ASSIST OK: {assist.worker_id} -> {len(result.get('text', ''))} chars")
    except asyncio.TimeoutError:
        log.warning(f"POOL_ASSIST timeout for {assist.worker_id}")
    except Exception as e:
        log.warning(f"POOL_ASSIST failed: {e}")
    finally:
        assist.busy = False
    return result


async def handle_auto_review(pool, result: dict, messages: list, worker, conv_id: str, max_tokens: int) -> dict:
    """=== AUTO REVIEW : quand l'agent ecrit du code, un sous-agent le review ===
    Phase 1 : review sur le meme pool, worker different.
    Le review est ajoute en metadata, pas injecte dans la reponse principale.
    """
    # Only review if the response contains code (tool_calls with write or significant code blocks)
    text = result.get("text", "")
    tool_calls = result.get("tool_calls", [])

    has_code = False
    code_content = ""

    # Check tool_calls for write operations
    for tc in tool_calls:
        fn = tc.get("function", {})
        if fn.get("name") in ("write", "Write", "write_file", "create_file", "edit", "Edit"):
            has_code = True
            try:
                args = json.loads(fn.get("arguments", "{}"))
                code_content += args.get("content", args.get("new_string", "")) + "\n"
            except Exception:
                pass

    # Check text for code blocks or code patterns
    if not has_code and len(text) > 300:
        code_indicators = ["def ", "class ", "import ", "function ", "const ", "```",
                           "async def ", "return ", "if __name__", "module.exports",
                           "from ", "self.", "await "]
        if any(ind in text for ind in code_indicators):
            has_code = True
            code_content = text
            log.info(f"AUTO_REVIEW: detected code pattern in {len(text)} char response")

    if not has_code or not code_content.strip():
        return result

    # Find a different worker for review
    review_worker = None
    for w in pool.workers.values():
        if w.worker_id != worker.worker_id and not w.busy:
            review_worker = w
            break

    if not review_worker:
        # Phase 2: try cross-pool review via federation forwarding
        try:
            from .forwarding import should_forward, forward_job, pick_best_peer
            peers = pool.federation_peers if hasattr(pool, 'federation_peers') else {}
            bonded = [p for p in peers.values() if p.get("trust_level", 0) >= 3]
            if bonded:
                peer = bonded[0]  # Pick first bonded peer
                log.info(f"AUTO_REVIEW: no local worker, forwarding to peer {peer.get('name', '?')}")
                review_messages = [{"role": "user", "content": review_prompt}]
                fwd_result = await forward_job(
                    pool, peer, model=None,
                    messages=review_messages,
                    max_tokens=256, conv_id=conv_id,
                )
                if fwd_result and fwd_result.get("ok"):
                    resp = fwd_result.get("response", {})
                    review_text = ""
                    if isinstance(resp, dict):
                        choices = resp.get("choices", [])
                        if choices:
                            review_text = choices[0].get("message", {}).get("content", "")
                    elif isinstance(resp, str):
                        review_text = resp
                    if review_text.strip():
                        result["auto_review"] = {
                            "reviewer": f"peer:{peer.get('name', '?')}",
                            "review": review_text.strip(),
                        }
                        log.info(f"AUTO_REVIEW OK (cross-pool): {peer.get('name', '?')} -> {review_text.strip()[:80]}")
                        return result
        except Exception as e:
            log.debug(f"AUTO_REVIEW cross-pool failed: {e}")
        return result  # No local or remote reviewer available

    log.info(f"AUTO_REVIEW: {worker.worker_id} wrote code -> reviewing on {review_worker.worker_id}")

    review_prompt = f"""Review this code briefly. Focus on:
- Bugs or logic errors
- Security issues
- Missing error handling
Be concise (3-5 lines max). If the code looks good, say "LGTM".

Code:
{code_content[:3000]}"""

    try:
        review_worker.busy = True
        review_result = await asyncio.wait_for(
            pool.delegate_task(
                helper=review_worker, task_type="auto_review",
                messages=[{"role": "user", "content": review_prompt}],
                tools=[], max_tokens=256, conv_id=conv_id,
                source_worker=worker.worker_id,
            ), timeout=30
        )
        if review_result:
            review_text = (review_result.get("text", "") if isinstance(review_result, dict) else str(review_result)).strip()
            if review_text:
                result["auto_review"] = {
                    "reviewer": review_worker.worker_id,
                    "review": review_text,
                }
                log.info(f"AUTO_REVIEW OK: {review_worker.worker_id} -> {review_text[:80]}")
    except asyncio.TimeoutError:
        log.warning(f"AUTO_REVIEW timeout on {review_worker.worker_id}")
    except Exception as e:
        log.warning(f"AUTO_REVIEW error: {e}")
    finally:
        review_worker.busy = False


    # --- M13: Capture review observation ---
    if AGENT_MEMORY_ENABLED and "auto_review" in result:
        import asyncio as _aio
        _rev_data = result["auto_review"]
        _rev_text = _rev_data.get("review", "")[:500]
        if _rev_text:
            _aio.create_task(capture_observation(
                pool.store, "", "review", _rev_text,
                conv_id=conv_id, source_id=_rev_data.get("reviewer_id", "")))

    return result


# === Phase 3: Multi-role sub-agent pipeline ===

# Default pipeline config — can be overridden per-user or per-pool
DEFAULT_PIPELINE = {
    "review": True,      # Phase 1: auto-review
    "test": False,       # Phase 3: auto-generate tests
    "security": False,   # Phase 3: security audit
    "doc": False,        # Phase 3: documentation
}

PIPELINE_PROMPTS = {
    "test": """Generate unit tests for this code. Use the same language.
Output ONLY the test code, nothing else.

Code:
{code}""",

    "security": """Security audit this code. Check for:
- Injection vulnerabilities (SQL, command, XSS)
- Hardcoded credentials or secrets
- Missing input validation
- Unsafe file operations
Be concise (3-5 lines). If safe, say "No issues found."

Code:
{code}""",

    "doc": """Generate a brief docstring/documentation for this code.
Output ONLY the documentation, nothing else. Use the same language.

Code:
{code}""",
}


async def handle_sub_agent_pipeline(pool, result: dict, messages: list, worker, conv_id: str, max_tokens: int, pipeline_config: dict = None) -> dict:
    """Phase 3: run multiple sub-agents in parallel on code output.

    Each enabled role spawns a sub-agent on a different worker (or peer pool).
    Results are collected in result["sub_agents"] dict.
    """
    config = pipeline_config or DEFAULT_PIPELINE

    text = result.get("text", "")
    tool_calls = result.get("tool_calls", [])

    # Extract code from response
    code_content = ""
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        if fn.get("name") in ("write", "Write", "write_file", "create_file", "edit", "Edit"):
            try:
                args = json.loads(fn.get("arguments", "{}"))
                code_content += args.get("content", args.get("new_string", "")) + "\n"
            except Exception:
                pass

    if not code_content and len(text) > 300:
        code_indicators = ["def ", "class ", "import ", "function ", "const ",
                          "async def ", "return ", "from ", "self.", "await "]
        if any(ind in text for ind in code_indicators):
            code_content = text

    if not code_content.strip():
        return result

    # Collect available workers (exclude the one that wrote the code)
    available = [w for w in pool.workers.values()
                 if w.worker_id != worker.worker_id and not w.busy]

    # Also include federated peers as potential workers
    fed_peers = []
    if hasattr(pool, 'federation_peers'):
        fed_peers = [p for p in pool.federation_peers.values()
                     if p.get("trust_level", 0) >= 3]

    # Determine which roles to run
    roles_to_run = [role for role, enabled in config.items()
                    if enabled and role != "review" and role in PIPELINE_PROMPTS]

    if not roles_to_run:
        return result

    sub_agents = result.get("sub_agents", {})

    # Run sub-agents in parallel
    tasks = []
    for role in roles_to_run:
        prompt = PIPELINE_PROMPTS[role].format(code=code_content[:3000])

        if available:
            # Use local worker
            sub_worker = available.pop(0)
            log.info(f"PIPELINE [{role}]: {worker.worker_id} -> {sub_worker.worker_id}")

            async def run_local(sw, r, p):
                try:
                    sw.busy = True
                    res = await asyncio.wait_for(
                        pool.delegate_task(
                            helper=sw, task_type=f"pipeline_{r}",
                            messages=[{"role": "user", "content": p}],
                            tools=[], max_tokens=512, conv_id=conv_id,
                            source_worker=worker.worker_id,
                        ), timeout=30
                    )
                    text = (res.get("text", "") if isinstance(res, dict) else str(res)).strip()
                    return r, {"worker": sw.worker_id, "result": text}
                except Exception as e:
                    log.warning(f"PIPELINE [{r}] failed: {e}")
                    return r, None
                finally:
                    sw.busy = False

            tasks.append(run_local(sub_worker, role, prompt))

        elif fed_peers:
            # Use federated peer
            peer = fed_peers.pop(0)
            log.info(f"PIPELINE [{role}]: {worker.worker_id} -> peer:{peer.get('name', '?')}")

            async def run_remote(pr, r, p):
                try:
                    from .forwarding import forward_job
                    fwd = await forward_job(
                        pool, pr, model=None,
                        messages=[{"role": "user", "content": p}],
                        max_tokens=512, conv_id=conv_id,
                    )
                    if fwd and fwd.get("ok"):
                        resp = fwd.get("response", {})
                        text = ""
                        if isinstance(resp, dict):
                            choices = resp.get("choices", [])
                            if choices:
                                text = choices[0].get("message", {}).get("content", "")
                        elif isinstance(resp, str):
                            text = resp
                        return r, {"worker": f"peer:{pr.get('name', '?')}", "result": text.strip()}
                except Exception as e:
                    log.warning(f"PIPELINE [{r}] cross-pool failed: {e}")
                return r, None

            tasks.append(run_remote(peer, role, prompt))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            if isinstance(item, tuple) and item[1]:
                role, data = item
                sub_agents[role] = data
                log.info(f"PIPELINE [{role}] OK: {data.get('worker', '?')} -> {data.get('result', '')[:60]}")

    if sub_agents:
        result["sub_agents"] = sub_agents

    # --- M13: Capture pipeline observations ---
    if AGENT_MEMORY_ENABLED and result.get("sub_agents"):
        import asyncio as _aio
        for _role, _data in result["sub_agents"].items():
            _rtxt = _data.get("result_text", "")[:500]
            if _rtxt:
                _aio.create_task(capture_observation(
                    pool.store, "", "pipeline", _rtxt,
                    conv_id=conv_id, source_id=_data.get("worker_id", ""),
                    metadata={"role": _role}))

    return result
