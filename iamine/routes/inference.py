"""Chat/inference endpoints — extracted from pool.py."""

import asyncio
import hashlib
import re
import ipaddress
import logging
import time
import uuid

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from iamine.core.utils import strip_thinking


router = APIRouter()
log = logging.getLogger("iamine.routes.inference")

# M12: Guest mode — 3 free requests per IP without account
_guest_usage = {}
GUEST_MAX_REQUESTS = 20

def _check_guest(ip):
    return _guest_usage.get(ip, 0) < GUEST_MAX_REQUESTS

def _increment_guest(ip):
    _guest_usage[ip] = _guest_usage.get(ip, 0) + 1



def _sanitize_webhook_url(url: str) -> str:
    """Validate webhook_url to prevent SSRF. Returns empty string if invalid."""
    if not url:
        return ""
    if not url.startswith("https://"):
        log.warning("webhook_url rejected: must start with https:// — got %s", url[:60])
        return ""
    # Extract hostname (between :// and next / or end)
    match = re.match(r"https://([^/:]+)", url)
    if not match:
        return ""
    hostname = match.group(1).lower()
    # Block localhost variants
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
        log.warning("webhook_url rejected: localhost not allowed — %s", hostname)
        return ""
    # Block private/reserved IP ranges
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_reserved or addr.is_loopback or addr.is_link_local:
            log.warning("webhook_url rejected: private/reserved IP — %s", hostname)
            return ""
    except ValueError:
        pass  # Not a bare IP, it is a hostname — check for sneaky names
    # Block hostnames that resolve to common private patterns
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        log.warning("webhook_url rejected: internal hostname — %s", hostname)
        return ""
    return url


def _derive_conv_id(api_token: str, session_id: str = "") -> str:
    """Derive a stable conv_id for clients that don't send one.
    Uses X-Session-Id header if present, else 'default' -> one conv per token.
    """
    key = api_token + ":" + (session_id or "default")
    return "auto_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _extract_last_user_message(messages: list, conv_has_context: bool) -> list:
    """When pool already has conversation context, only keep last user message.
    If pool conv is empty (new), pass all messages so system prompt is captured.
    EXCEPTION: if messages contain tool/tool_result roles, pass ALL messages
    (multi-step tool workflow — the LLM needs to see previous tool results).
    """
    if not conv_has_context or not messages:
        return messages
    # Don't trim if there's a tool workflow in progress
    has_tool_messages = any(msg.get("role") == "tool" or msg.get("role") == "function" or msg.get("tool_call_id") or msg.get("tool_calls") for msg in messages)
    if has_tool_messages:
        return messages
    last_user = None
    system_msgs = []
    for msg in messages:
        if msg.get("role") == "system":
            system_msgs.append(msg)
        if msg.get("role") == "user":
            last_user = msg
    if last_user:
        return system_msgs + [last_user]
    return messages




def _get_content_str(msg):
    """Extract text content from a message, handling both str and list formats."""
    c = msg.get("content", "")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        # Multi-part content: extract text parts
        parts = []
        for part in c:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return " ".join(parts).strip()
    return str(c).strip()

# --- i18n helper for conversation commands ---
def _detect_lang(messages):
    """Detect user language from last message. Returns 'fr' or 'en'."""
    if not messages:
        return 'en'
    last = _get_content_str(messages[-1]).lower()
    fr_indicators = ['bonjour', 'salut', 'merci', 'oui', 'non', 'je ', 'mon ', 'mes ',
                     'enregistre', 'restaure', 'supprime', 'efface', 'oublie',
                     'sauvegarde', 'souviens', 'retiens', 'rappelle']
    for w in fr_indicators:
        if w in last:
            return 'fr'
    return 'en'

def _msg(messages, fr, en):
    """Return fr or en message based on detected language."""
    return fr if _detect_lang(messages) == 'fr' else en


def _pool():
    from iamine.pool import pool
    return pool


def _accounts():
    from iamine.pool import _accounts
    return _accounts


@router.post("/v1/chat/completions")
async def chat_completions(http_request: Request):
    """Endpoint compatible OpenAI — reserve aux participants du pool."""
    p = _pool()
    accounts = _accounts()
    client_ip = http_request.headers.get("x-real-ip") or http_request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (http_request.client.host if http_request.client else "unknown")

    request = await http_request.json()

    api_token = request.get("api_token", "")
    # Support Authorization: Bearer header (OpenCode, Cursor, etc.)
    if not api_token:
        auth_header = http_request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            api_token = auth_header[7:]

    messages = request.get("messages", [])
    max_tokens = request.get("max_tokens", 512)
    conv_id = request.get("conv_id")
    requested_model = request.get("model")
    webhook_url = _sanitize_webhook_url(request.get("webhook_url", ""))
    stream = request.get("stream", False)
    tools = request.get("tools") or request.get("functions")

    # Normalize model name — "iamine", "pool", "auto" all mean smart-routing
    POOL_ALIASES = {"auto", "iamine", "cellule", "pool", "iamine/pool", "iamine/auto", "iamine/iamine", "cellule/auto", "cellule/pool"}
    if requested_model and requested_model.lower() in POOL_ALIASES:
        requested_model = None  # let the pool decide

    # Tool-call routing: force biggest model when client sends tools
    if tools and not requested_model and p.tool_routing_model:
        requested_model = p.tool_routing_model  # configurable via dashboard admin
        log.info(f"Tool-call detected ({len(tools)} tools) — routing to {requested_model}")

    # Trial chat cascade — cross-federation (doctrine : "pools s'entraident,
    # API = point d'entrée"). Le trial de cellule.ai envoie 'qwen3.5-2b'
    # mais la fédération peut n'avoir que du 4B (Gladiator) ou du 9B (Scout-z2).
    # Cascade stricte 2B → 4B → 9B, JAMAIS 30B-Coder ni 35B-A3B (réservés
    # au routage intent-based). Cf. memory project_trial_chat_model_design.
    # Résolution AVANT forwarding hook → le should_forward() voit le modèle
    # effectif et peut Case A vers Gladiator.
    TRIAL_MODEL_CASCADE = {
        "qwen3.5-2b": ["qwen3.5-2b", "qwen3.5-4b", "qwen3.5-9b"],
        "qwen3.5-4b": ["qwen3.5-4b", "qwen3.5-2b", "qwen3.5-9b"],
    }
    def _local_has_model(m: str) -> bool:
        ml = m.lower()
        return any(
            ml in (w.info.get("model_path", "") or "").lower()
            or ml in (w.worker_id or "").lower()
            for w in p.workers.values()
        )
    async def _resolve_trial_cascade(requested: str, include_peers: bool) -> str:
        """Return the best cascade candidate, or the original if none match.
        include_peers=False → local-only (fallback après forward échoué).
        """
        cascade = TRIAL_MODEL_CASCADE.get(requested.lower())
        if not cascade:
            return requested
        peer_models: set = set()
        if include_peers:
            try:
                from ..core import federation as _fed
                for peer in await _fed.list_molecule_peers(p, min_trust=2):
                    caps = peer.get("capabilities") or []
                    if isinstance(caps, str):
                        try:
                            caps = json.loads(caps)
                        except Exception:
                            caps = []
                    for cap in caps:
                        if isinstance(cap, dict) and str(cap.get("kind", "")).startswith("llm.chat"):
                            peer_models.add(str(cap.get("model", "")).lower())
            except Exception as e:
                log.debug(f"trial cascade: peer discovery failed (non-fatal): {e}")
        def _peer_has(m: str) -> bool:
            ml = m.lower()
            return any(ml in pm or pm in ml for pm in peer_models if pm)
        for candidate in cascade:
            if _local_has_model(candidate) or (include_peers and _peer_has(candidate)):
                if candidate.lower() != requested.lower():
                    scope = "local" if _local_has_model(candidate) else "peer"
                    log.info(f"trial cascade {scope}: {requested!r} → {candidate!r}")
                return candidate
        return requested  # pas de match → fallthrough au 400 downstream

    if requested_model and requested_model.lower() in TRIAL_MODEL_CASCADE:
        requested_model = await _resolve_trial_cascade(requested_model, include_peers=True)

    # Auto conv_id for OpenAI-compatible clients that don't send one
    session_id = http_request.headers.get("x-session-id", "")
    if not conv_id and api_token:
        conv_id = _derive_conv_id(api_token, session_id)
        log.info(f"Auto conv_id derived: {conv_id} (session={session_id or 'default'})")
        log.info("DEBUG msg roles: " + str([(m.get("role","?"), bool(m.get("tool_calls")), bool(m.get("tool_call_id"))) for m in messages]))

        # Check if pool already has context for this conversation
        existing_conv = p.router._conversations.get(conv_id)
        conv_has_context = bool(existing_conv and (
            len(existing_conv.messages) > 1 or
            existing_conv._summary or
            existing_conv._l3_summary
        ))
        if conv_has_context and not tools:
            original_count = len(messages)
            messages = _extract_last_user_message(messages, True)
            if len(messages) < original_count:
                log.info(f"Trimmed messages {original_count} -> {len(messages)} (pool has context)")

    # Verifier que l'utilisateur participe au pool (token valide)
    # Supporter les tokens worker (iam_) ET les tokens de compte (acc_)
    if api_token:
        token_data = p.api_tokens.get(api_token)
        if not token_data and api_token.startswith("acc_"):
            # Token de compte non encore dans le pool — l'enregistrer a la volee
            for acc in accounts.values():
                if acc.get("account_token") == api_token:
                    p.api_tokens[api_token] = {
                        "worker_id": f"account-{acc['account_id'][:8]}",
                        "account_id": acc["account_id"],
                        "created": time.time(),
                        "requests_used": 0,
                        "credits": acc.get("total_credits", 0),
                    }
                    token_data = p.api_tokens[api_token]
                    break
        if not token_data:
            return JSONResponse({"error": "Invalid token. Join the network to use the chat."}, status_code=401)
    else:
        # No token — guest mode (3 free requests per IP)
        if not _check_guest(client_ip):
            return JSONResponse({"error": "Session complete. Create an account to join the network.", "signup_url": "https://cellule.ai", "guest_limit": GUEST_MAX_REQUESTS}, status_code=429)
        _increment_guest(client_ip)
        api_token = f"guest_{client_ip}"
        token_data = {"worker_id": "guest", "requests_used": 0, "credits": 0}
        log.info(f"Guest request from {client_ip} ({_guest_usage.get(client_ip, 0)}/{GUEST_MAX_REQUESTS})")

    # --- Commande "enregistre" : sauvegarde conversation sans passer par le LLM ---
    if messages and api_token and api_token.startswith("acc_"):
        _save_msg = _get_content_str(messages[-1])
        _save_lower = _save_msg.lower()
        _save_keywords = ["enregistre", "save", "mémorise", "memorise", "sauvegarde",
                          "souviens-toi", "souviens toi", "retiens", "remember", "rappelle-toi", "rappelle toi"]
        _is_save_cmd = False
        for _kw in _save_keywords:
            if _save_lower == _kw:
                _is_save_cmd = True
                break
            if re.match(r"^" + re.escape(_kw) + r"[\s.,!:;]", _save_lower):
                _is_save_cmd = True
                break
        if _is_save_cmd:
            acc = next((a for a in accounts.values() if a.get("account_token") == api_token), None)
            if not acc or not acc.get("memory_enabled", False):
                return {"id": f"iamine-save-{conv_id}", "object": "chat.completion", "model": "system",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": _msg(messages, "Activez la mémoire persistante dans votre profil sur cellule.ai pour sauvegarder.", "Enable persistent memory in your profile on cellule.ai to save conversations.")}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
            else:
                conv = p.router._conversations.get(conv_id)
                if conv:
                    await p.store.save_conversation_state(conv_id, api_token, conv.messages, conv._summary)
                    # Stocker les derniers messages comme faits RAG
                    recent_facts = " ".join(m.get("content", "") for m in conv.messages[-6:] if m.get("role") == "user" and m.get("content"))
                    if recent_facts:
                        asyncio.create_task(p._embed_facts(api_token, recent_facts[:500], conv_id))
                return {"id": f"iamine-save-{conv_id}", "object": "chat.completion", "model": "system",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": _msg(messages, "Conversation enregistrée.", "Conversation saved.")}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


    # --- Commande "restaure" : lister/charger/supprimer une conversation sauvegardee ---
    if messages and api_token and api_token.startswith("acc_"):
        _rest_msg = _get_content_str(messages[-1])
        _rest_lower = _rest_msg.lower()
        _restore_keywords = ["restaure", "restore", "mes conversations", "my conversations", "historique", "history", "list"]
        _delete_keywords = ["supprime", "delete", "efface", "oublie", "forget", "remove", "erase"]
        _is_restore_cmd = False
        _is_delete_cmd = False
        _restore_num = None
        _restore_id = None  # support both number and conv_id prefix
        for _kw in _restore_keywords:
            if _rest_lower == _kw:
                _is_restore_cmd = True
                break
            # Accept "restaure <number>" or "restaure [<number>]"
            m_num = re.match(r"^" + re.escape(_kw) + r"\s+\[?(\d+)\]?$", _rest_lower)
            if m_num:
                _restore_num = int(m_num.group(1))
                _is_restore_cmd = True
                break
            # Accept "restaure <conv_id>" or "restaure [<conv_id>]" (alphanumeric + _)
            m_id = re.match(r"^" + re.escape(_kw) + r"\s+\[?([a-zA-Z0-9_]+)\]?$", _rest_lower)
            if m_id:
                _restore_id = m_id.group(1)
                _is_restore_cmd = True
                break
            if re.match(r"^" + re.escape(_kw) + r"[\s.,!:;]", _rest_lower) and not _is_restore_cmd:
                _is_restore_cmd = True
                break
        # supprime all / supprime <num_or_id>
        _is_delete_all = False
        for _kw in _delete_keywords:
            if re.match(r"^" + re.escape(_kw) + r"\s+(all|tout|toutes|tous)$", _rest_lower):
                _is_delete_cmd = True
                _is_delete_all = True
                break
            m_num = re.match(r"^" + re.escape(_kw) + r"\s+\[?(\d+)\]?$", _rest_lower)
            if m_num:
                _restore_num = int(m_num.group(1))
                _is_delete_cmd = True
                break
            m_id = re.match(r"^" + re.escape(_kw) + r"\s+\[?([a-zA-Z0-9_]+)\]?$", _rest_lower)
            if m_id:
                _restore_id = m_id.group(1)
                _is_delete_cmd = True
                break
        if _is_restore_cmd or _is_delete_cmd:
            acc = next((a for a in accounts.values() if a.get("account_token") == api_token), None)
            if not acc or not acc.get("memory_enabled", False):
                return {"id": f"iamine-restore-{conv_id}", "object": "chat.completion", "model": "system",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": _msg(messages, "Activez la mémoire persistante dans votre profil sur cellule.ai.", "Enable persistent memory in your profile on cellule.ai to use this command.")}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
            else:
                try:
                    conv_list = await p.store.list_conversations(api_token)
                except Exception as e:
                    log.warning(f"restore: list_conversations failed: {e}")
                    conv_list = []

                # Resolve target conv_id from number or id prefix
                target_conv_id = None
                if _restore_num is not None:
                    if not conv_list or _restore_num < 1 or _restore_num > len(conv_list):
                        return {"id": f"iamine-restore-{conv_id}", "object": "chat.completion", "model": "system",
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": _msg(messages, f"Numéro invalide. Tapez restaure/restore pour voir la liste ({len(conv_list)} disponibles).", f"Invalid number. Type restore to see the list ({len(conv_list)} available).")}, "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
                    target_conv_id = conv_list[_restore_num - 1]["conv_id"]
                elif _restore_id is not None:
                    matches = [c for c in conv_list if c["conv_id"].startswith(_restore_id) or c["conv_id"] == _restore_id]
                    if not matches:
                        return {"id": f"iamine-restore-{conv_id}", "object": "chat.completion", "model": "system",
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": _msg(messages, f"ID invalide: {_restore_id}. Tapez restaure pour voir la liste.", f"Invalid ID: {_restore_id}. Type restore to see the list.")}, "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
                    target_conv_id = matches[0]["conv_id"]

                # --- DELETE ALL branch ---
                if _is_delete_cmd and _is_delete_all:
                    deleted_count = 0
                    failed_count = 0
                    for _c in conv_list:
                        try:
                            if await p.store.delete_conversation_by_user(_c["conv_id"], api_token):
                                deleted_count += 1
                            else:
                                failed_count += 1
                        except Exception as _e:
                            log.warning(f"delete-all: failed {_c.get('conv_id')}: {_e}")
                            failed_count += 1
                    content = _msg(messages, f"🗑️ {deleted_count} conversation(s) supprimée(s). ", f"🗑️ {deleted_count} conversation(s) deleted. ")
                    if failed_count:
                        content += _msg(messages, f"{failed_count} échec(s).", f"{failed_count} failed.")
                    else:
                        content += _msg(messages, "Historique vidé.", "History cleared.")
                    return {"id": f"iamine-delete-all-{conv_id}", "object": "chat.completion", "model": "system",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

                # --- DELETE single branch ---
                if _is_delete_cmd and target_conv_id:
                    try:
                        ok = await p.store.delete_conversation_by_user(target_conv_id, api_token)
                        if ok:
                            content = _msg(messages, f"Conversation supprimée : [{target_conv_id[:12]}].", f"Conversation deleted: [{target_conv_id[:12]}].")
                        else:
                            content = _msg(messages, "Impossible de supprimer : introuvable ou non autorisé.", "Cannot delete: not found or unauthorized.")
                    except Exception as e:
                        log.warning(f"delete: failed: {e}")
                        content = _msg(messages, f"Erreur: {str(e)[:100]}", f"Error: {str(e)[:100]}")
                    return {"id": f"iamine-delete-{conv_id}", "object": "chat.completion", "model": "system",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

                if _is_delete_cmd and not target_conv_id:
                    # Delete without target — refuse, must specify
                    return {"id": f"iamine-delete-{conv_id}", "object": "chat.completion", "model": "system",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": _msg(messages, "Usage: supprime <numéro> ou supprime <id>. Tapez restaure pour la liste.", "Usage: delete <number> or delete <id>. Type restore to see the list.")}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

                if target_conv_id:
                    target = next((c for c in conv_list if c["conv_id"] == target_conv_id), None)
                    try:
                        restored = await p.store.load_conversation(target_conv_id, api_token)
                    except Exception as e:
                        log.warning(f"restore: load_conversation failed: {e}")
                        restored = None
                    # Defense-in-depth: verify token ownership even though store filters by api_token
                    if restored and restored.get("api_token") and restored["api_token"] != api_token:
                        log.warning(f"restore: token mismatch for conv {target_conv_id}")
                        restored = None
                    if restored and restored.get("messages"):
                        conv = p.router.get_or_create_conversation(conv_id)
                        conv.messages = restored["messages"]
                        conv._summary = restored.get("summary", "")
                        conv.total_tokens = sum(len(str(m.get("content", ""))) // 4 for m in conv.messages)
                        msg_count = len(restored["messages"])
                        title = restored.get("title", target_conv_id[:12])
                        content = f"Conversation restaurée : \"{title}\" ({msg_count} messages). Vous pouvez continuer."
                    else:
                        content = _msg(messages, "Conversation vide ou introuvable.", "Conversation empty or not found.")
                    return {"id": f"iamine-restore-{conv_id}", "object": "chat.completion", "model": "system",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
                else:
                    # Cas 1 : lister les conversations
                    if not conv_list:
                        content = _msg(messages, "Aucune conversation sauvegardée.", "No saved conversations.")
                    else:
                        lines = [_msg(messages, "Vos conversations sauvegardées :\n", "Your saved conversations:\n")]
                        for i, c in enumerate(conv_list, 1):
                            short_id = c["conv_id"][:12]
                            title = (c.get("title") or short_id).replace(chr(10), " ")[:50].strip()
                            msg_count = c.get("message_count", 0)
                            last_act = c.get("last_activity", "?")
                            lines.append(f"{i}. [{short_id}] \"{title}\" — {msg_count} msg, {last_act}")
                        lines.append(_msg(messages, "\nCommandes: `restaure <n>` / `supprime <n>` (ou restore/delete)", "\nCommands: `restore <n>` / `delete <n>` (or restaure/supprime)"))
                        content = "\n".join(lines)
                    return {"id": f"iamine-restore-{conv_id}", "object": "chat.completion", "model": "system",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    # Routing intelligent : detecter les demandes de contenu long → pipeline
    if messages:
        last_msg = _get_content_str(messages[-1]).lower() if messages else ""
        pipeline_keywords = ["ecris un chapitre", "write a chapter", "genere un livre",
            "redige un article", "ecris un rapport", "genere un texte long",
            "write a book", "generate a report"]
        if any(kw in last_msg for kw in pipeline_keywords) and max_tokens >= 256 and not tools:
            # Rediriger vers le pipeline
            topic = messages[-1].get("content", "")[:200]
            try:
                from iamine.pipeline import Pipeline
                pipe = Pipeline(p)
                result = await pipe.generate_chapter(
                    chapter_num=1, title=topic[:80], context="",
                    instructions=topic, style="factual", topic=topic[:80]
                )
                return {
                    "id": f"iamine-pipe-{result.get('pipeline_id','')}",
                    "object": "chat.completion",
                    "model": "pipeline",
                    "choices": [{"index": 0, "message": {"role": "assistant",
                        "content": result.get("text", "")}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": result.get("total_tokens", 0),
                              "total_tokens": result.get("total_tokens", 0)},
                    "conv_id": conv_id,
                    "iamine": {"pipeline": True, "steps": len(result.get("steps", []))},
                }
            except Exception as e:
                log.warning(f"Pipeline auto-route failed: {e} — falling back to chat")
    else:
        # Pas de token = demo limitee (désactivée en preprod)
        if not p.PREPROD_MODE and conv_id:
            conv = p.router.get_or_create_conversation(conv_id)
            if len(conv.messages) > 6:  # 3 echanges user+assistant
                return JSONResponse({
                    "error": "Free demo limit reached. Join the network to continue.",
                    "hint": "pip install iamine-ai && python -m iamine worker --auto",
                }, status_code=402)

    # Rate limiting (par token ou "anonymous")
    rate_source = api_token or "anonymous"
    if not p.check_rate_limit(rate_source):
        return JSONResponse({"error": "Rate limited — max 30 requests/minute"}, status_code=429)

    # Validation basique
    if not isinstance(messages, list) or len(messages) == 0:
        return JSONResponse({"error": "messages doit être une liste non vide"}, status_code=400)
    if not isinstance(max_tokens, int) or not (1 <= max_tokens <= 16384):
        return JSONResponse({"error": "max_tokens doit être entre 1 et 16384"}, status_code=400)
    for msg in messages:
        if not isinstance(msg, dict) or "content" not in msg:
            return JSONResponse({"error": "chaque message doit contenir une clé 'content'"}, status_code=400)

    # M7a — Forwarding hook (opt-in via FORWARDING_ENABLED). Placed AFTER auth
    # and BEFORE model validation so we can forward models we don't have locally.
    # Fallback doctrine: any exception → log + continue local routing.
    try:
        from ..core import forwarding as _fwd
        if _fwd.is_forwarding_enabled():
            queue_size = len(getattr(p, 'pending_jobs', {})) if hasattr(p, 'pending_jobs') else 0
            _peer = await _fwd.should_forward(p, requested_model, queue_size)
            if _peer is not None:
                if _fwd.get_forwarding_mode() == 'log_only':
                    log.info(f"M7a log_only: WOULD forward to {_peer['name']!r} (model={requested_model} queue={queue_size})")
                else:
                    try:
                        fwd_result = await _fwd.forward_job(p, _peer, requested_model, messages, max_tokens, conv_id=conv_id, api_token=api_token)
                        log.info(f"M7a forward ok: peer={_peer['name']!r} tokens_out={fwd_result.get('tokens_out')}")
                        return {
                            'choices': [{'message': {'role': 'assistant', 'content': fwd_result.get('response', '')}, 'finish_reason': 'stop'}],
                            'model': requested_model or 'iamine/auto',
                            'usage': {'prompt_tokens': fwd_result.get('tokens_in', 0), 'completion_tokens': fwd_result.get('tokens_out', 0), 'total_tokens': fwd_result.get('tokens_out', 0)},
                            'forwarded_to': _peer.get('name'),
                            'exec_pool_id': fwd_result.get('exec_pool_id'),
                        }
                    except Exception as _fwe:
                        log.warning(f"M7a forward failed, falling back to local: {_fwe}")
                        # Si le modèle résolu par cascade était peer-only,
                        # re-résoudre local-only pour tomber sur Scout-9B.
                        if requested_model and requested_model.lower() in TRIAL_MODEL_CASCADE:
                            requested_model = await _resolve_trial_cascade(requested_model, include_peers=False)
    except Exception as _fwh:
        log.warning(f"M7a forwarding hook exception (non-fatal): {_fwh}")

    # Validation modèle — rejeter les modèles inconnus (ex: gpt-4)
    # Skip validation for internal routing hints (set by tool-call routing)
    ROUTING_HINTS = {"Coder", "30B", "_largest"}
    if requested_model and requested_model != "auto" and requested_model not in ROUTING_HINTS:
        model_found = any(requested_model.lower() in w.info.get("model_path", "").lower() or requested_model.lower() in w.worker_id.lower() for w in p.workers.values())
        if not model_found:
            available = list(set(w.info.get("model_path", "").split("/")[-1].replace(".gguf", "") for w in p.workers.values()))
            return JSONResponse({"error": f"Model '{requested_model}' not available. Use 'auto' or one of: {', '.join(sorted(available))}"}, status_code=400)

    # Warning conv_id inconnu
    conv_warning = None
    if conv_id:
        conv = p.router._conversations.get(conv_id)
        if not conv or len(conv.messages) == 0:
            conv_warning = "Unknown conv_id — starting new conversation"

    try:
        result = await p.submit_job(messages, max_tokens, conv_id=conv_id, requested_model=requested_model, api_token=api_token, tools=tools)
    except RuntimeError as e:
        # Tampon DB anti-saturation : enqueue au lieu de 503
        error_msg = str(e)
        if "saturated" in error_msg or "No worker available" in error_msg:
            try:
                # Rate limit: max pending jobs per token (anti-flood)
                if api_token:
                    pending_count = await p.store.count_pending_by_token(api_token)
                    if pending_count >= p.MAX_PENDING_PER_TOKEN:
                        return JSONResponse({
                            "error": f"Queue limit reached ({p.MAX_PENDING_PER_TOKEN} pending jobs). Please wait for current jobs to complete.",
                            "pending_count": pending_count,
                        }, status_code=429)

                job_id = f"pj_{uuid.uuid4().hex[:12]}"
                await p.store.enqueue_pending_job(
                    job_id=job_id, conv_id=conv_id or "",
                    api_token=api_token, messages=messages,
                    max_tokens=max_tokens, requested_model=requested_model or "",
                    webhook_url=webhook_url,
                )
                # Reponse intermediaire : resume L3 si disponible
                fallback_content = "Votre requête est en file d'attente. Utilisez le job_id pour suivre le statut."
                if conv_id:
                    summary = None
                    # 1) Chercher en RAM (conversation active)
                    conv = p.router._conversations.get(conv_id)
                    if conv and conv._summary:
                        summary = conv._summary
                    elif conv and conv._l3_summary:
                        summary = conv._l3_summary
                    # 2) Fallback DB (conversation evincee de la RAM)
                    if not summary and api_token:
                        try:
                            summary = await p.store.get_conversation_summary(conv_id, api_token)
                        except Exception:
                            pass
                    if summary:
                        fallback_content = (
                            f"[En attente d'un worker — voici le contexte de votre conversation]\n"
                            f"{summary}"
                        )

                stats = await p.store.get_queue_stats()
                log.info(f"Job enqueued: {job_id} (queue={stats['pending']})")
                return {
                    "id": f"iamine-{job_id}",
                    "object": "chat.completion",
                    "model": "queue",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": fallback_content},
                        "finish_reason": "stop",
                    }],
                    "conv_id": conv_id,
                    "iamine": {
                        "pending": True,
                        "job_id": job_id,
                        "queue_depth": stats["pending"],
                        "estimated_wait_sec": max(5, stats["avg_wait_sec"]) if stats["avg_wait_sec"] else 15,
                        "webhook": bool(webhook_url),
                        **({"warning": conv_warning} if conv_warning else {}),
                    },
                }
            except Exception as enqueue_err:
                log.error(f"Failed to enqueue job: {enqueue_err}")
                # Doctrine : TOUJOURS repondre, meme en dernier recours
                # Essayer les cached_responses (FAQ pre-calculees)
                fallback_text = ("Le pool est actuellement très chargé. "
                                 "Votre message a été reçu et sera traité dès que possible. "
                                 "Réessayez dans quelques instants.")
                if messages:
                    last_msg = messages[-1].get("content", "")
                    try:
                        cached = await p.store.get_cached_response(last_msg)
                        if cached:
                            fallback_text = cached
                    except Exception:
                        pass
                return {
                    "id": "iamine-fallback",
                    "object": "chat.completion",
                    "model": "cached",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant",
                            "content": fallback_text},
                        "finish_reason": "stop",
                    }],
                    "conv_id": conv_id,
                    "iamine": {"overloaded": True, "cached": True},
                }
        # Doctrine : jamais de 503 vide — toujours une reponse utile
        return {
            "id": "iamine-fallback",
            "object": "chat.completion",
            "model": "fallback",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                    "content": "Le pool rencontre un problème temporaire. "
                               "Votre message a été reçu. Réessayez dans quelques instants."},
                "finish_reason": "stop",
            }],
            "conv_id": conv_id,
            "iamine": {"error": error_msg, "overloaded": True},
        }

    # Format de réponse compatible OpenAI
    resp_id = f"iamine-{result.get('job_id', '')}"
    model_name = result.get("model", "unknown")
    text = result.get("text", "")
    tool_calls = result.get("tool_calls")
    finish = "tool_calls" if tool_calls else "stop"
    usage = {
        "prompt_tokens": result.get("prompt_tokens", 0),
        "completion_tokens": result.get("tokens_generated", 0),
        "total_tokens": (result.get("prompt_tokens", 0) or 0) + (result.get("tokens_generated", 0) or 0),
    }

    if stream:
        # SSE streaming format (fake streaming — send complete response as chunks)
        created = int(time.time())
        def _chunk(delta, finish_reason=None, extra=None):
            d = {"id": resp_id, "object": "chat.completion.chunk", "created": created, "model": model_name,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            if extra:
                d.update(extra)
            return f"data: {json.dumps(d)}\n\n"

        async def sse_stream():
            yield _chunk({"role": "assistant", "content": ""})
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    yield _chunk({"tool_calls": [{"index": i, **tc}]})
            if text:
                yield _chunk({"content": text})
            # Build final chunk with usage + sub-agent results
            final_extra = {"usage": usage}
            if result.get("auto_review"):
                final_extra["auto_review"] = result["auto_review"]
            if result.get("sub_agents"):
                final_extra["sub_agents"] = result["sub_agents"]
            yield _chunk({}, finish_reason=finish, extra=final_extra)
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse_stream(), media_type="text/event-stream")

    return {
        "id": resp_id,
        "object": "chat.completion",
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
                "finish_reason": finish,
            }
        ],
        "usage": usage,
        "conv_id": result.get("conv_id") or conv_id,
        "iamine": {
            "worker_id": result.get("worker_id"),
            "tokens_per_sec": result.get("tokens_per_sec"),
            "duration_sec": result.get("duration_sec"),
            "compacted": result.get("compacted", False),
            "conv_messages_total": result.get("conv_messages_total", 0),
            **({"boost": result["boost"]} if result.get("boost") else {}),
            **({"warning": conv_warning} if conv_warning else {}),
        },
    }


@router.post("/v1/api/chat")
async def api_chat(request: dict):
    """Endpoint API authentifie — coute 1 credit $IAMINE par requete.

    Usage:
        curl -H "Authorization: Bearer iam_xxx" \\
             -d '{"messages":[...]}' \\
             https://iamine.org/v1/api/chat

    Fonctionne meme si le worker est offline tant qu'il a des credits.
    """
    p = _pool()
    accounts = _accounts()

    # Extraire le token du body OU du header
    api_token = request.get("api_token", "")
    if not api_token:
        # Chercher dans le header (pour OpenWebUI etc)
        # Note: FastAPI ne passe pas les headers ici car request est un dict
        # On gère ça côté client pour le moment
        pass

    # Supporter les tokens de compte (acc_xxx) ET les tokens worker (iam_xxx)
    token_data = None
    _is_account_token = False
    if api_token.startswith("acc_"):
        # Token de compte — consolider les credits de tous les workers
        for acc in accounts.values():
            if acc.get("account_token") == api_token:
                total_credits = 0.0
                first_worker_token = None
                for wid in acc.get("worker_ids", []):
                    for t, td in p.api_tokens.items():
                        if td["worker_id"] == wid:
                            total_credits += td["credits"]
                            if not first_worker_token:
                                first_worker_token = td
                if first_worker_token:
                    token_data = first_worker_token
                    token_data["_consolidated_credits"] = total_credits
                    _is_account_token = True
                break
    else:
        token_data = p.api_tokens.get(api_token)

    if not token_data:
        return JSONResponse({"error": "Invalid API token. Join the network to earn credits."}, status_code=401)

    effective_credits = token_data.get("_consolidated_credits", token_data["credits"]) if _is_account_token else token_data["credits"]
    if not p.PREPROD_MODE and effective_credits < 1.0:
        return JSONResponse({
            "error": "Insufficient credits",
            "credits": round(token_data["credits"], 2),
            "hint": "Run your worker to earn more credits (1 request served = 1 credit)",
        }, status_code=402)

    messages = request.get("messages", [])
    max_tokens = request.get("max_tokens", 512)
    conv_id = request.get("conv_id")
    requested_model = request.get("model")
    webhook_url = _sanitize_webhook_url(request.get("webhook_url", ""))
    if not messages:
        return JSONResponse({"error": "messages required"}, status_code=400)

    try:
        result = await p.submit_job(messages, max_tokens, conv_id=conv_id, requested_model=requested_model, api_token=api_token)
    except RuntimeError as e:
        error_msg = str(e)
        if "saturated" in error_msg or "No worker available" in error_msg:
            try:
                # Rate limit: max pending jobs per token
                if api_token:
                    pending_count = await p.store.count_pending_by_token(api_token)
                    if pending_count >= p.MAX_PENDING_PER_TOKEN:
                        return JSONResponse({
                            "error": f"Queue limit reached ({p.MAX_PENDING_PER_TOKEN} pending jobs). Please wait.",
                            "pending_count": pending_count,
                        }, status_code=429)

                job_id = f"pj_{uuid.uuid4().hex[:12]}"
                await p.store.enqueue_pending_job(
                    job_id=job_id, conv_id=conv_id or "",
                    api_token=api_token, messages=messages,
                    max_tokens=max_tokens, requested_model=requested_model or "",
                    webhook_url=webhook_url,
                )
                stats = await p.store.get_queue_stats()
                log.info(f"API job enqueued: {job_id}")
                return {
                    "id": f"iamine-{job_id}",
                    "object": "chat.completion",
                    "model": "queue",
                    "choices": [{"index": 0, "message": {"role": "assistant",
                        "content": "Requête en file d'attente. Pollez GET /v1/jobs/" + job_id},
                        "finish_reason": "stop"}],
                    "conv_id": conv_id,
                    "iamine": {"pending": True, "job_id": job_id,
                               "queue_depth": stats["pending"]},
                }
            except Exception as enqueue_err:
                log.error(f"Failed to enqueue API job: {enqueue_err}")
        # Doctrine : jamais de 503
        return {
            "id": "iamine-fallback",
            "object": "chat.completion",
            "model": "fallback",
            "choices": [{"index": 0, "message": {"role": "assistant",
                "content": "Le pool est temporairement chargé. Réessayez dans quelques instants."},
                "finish_reason": "stop"}],
            "conv_id": conv_id,
            "iamine": {"overloaded": True, "error": error_msg},
        }

    # Debiter 1 credit (désactivé en preprod)
    if not p.PREPROD_MODE:
        token_data["credits"] -= 1.0
    token_data["requests_used"] += 1

    return {
        "id": f"iamine-{result.get('job_id', '')}",
        "object": "chat.completion",
        "model": result.get("model", "unknown"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.get("text", "")},
            "finish_reason": "stop",
        }],
        "usage": {"completion_tokens": result.get("tokens_generated", 0)},
        "conv_id": result.get("conv_id") or conv_id,
        "iamine": {
            "worker_id": result.get("worker_id"),
            "tokens_per_sec": result.get("tokens_per_sec"),
            "credits_remaining": round(token_data["credits"], 2),
            **({"pool_assist": result["pool_assist"]} if result.get("pool_assist") else {}),
            **({"auto_review": result["auto_review"]} if result.get("auto_review") else {}),
            **({"sub_agents": result["sub_agents"]} if result.get("sub_agents") else {}),
        },
    }


# ── Generate SPEC.md + OPENCODE.md ──────────────────────────────────
_SPEC_RATE: dict[str, list[float]] = {}   # token -> [timestamps]
_SPEC_RATE_WINDOW = 60          # 1 minute
_SPEC_RATE_MAX_ANON = 2         # anonymous: 2/min
_SPEC_RATE_MAX_AUTH = 10        # authenticated: 10/min


def _spec_rate_ok(key: str, is_auth: bool) -> bool:
    """Simple sliding-window rate limiter for /v1/generate-spec."""
    now = time.time()
    window = _SPEC_RATE.setdefault(key, [])
    window[:] = [t for t in window if now - t < _SPEC_RATE_WINDOW]
    limit = _SPEC_RATE_MAX_AUTH if is_auth else _SPEC_RATE_MAX_ANON
    if len(window) >= limit:
        return False
    window.append(now)
    return True


@router.post("/v1/generate-spec")
async def generate_spec(http_request: Request):
    """Generate SPEC.md + OPENCODE.md for a project using the pool."""
    p = _pool()

    body = await http_request.json()
    project_name = (body.get("project_name") or "").strip()[:120]
    description  = (body.get("description") or "").strip()[:500]
    stack        = (body.get("stack") or "python").strip()[:30]
    objective    = (body.get("objective") or "").strip()[:300]

    if not project_name:
        return JSONResponse({"error": "project_name is required"}, status_code=400)

    # Auth (optional — demo allowed with rate limit)
    api_token = ""
    auth_header = http_request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        api_token = auth_header[7:]

    is_auth = bool(api_token and (api_token in p.api_tokens or api_token.startswith("acc_")))
    rate_key = api_token if is_auth else (http_request.client.host if http_request.client else "anon")
    if not _spec_rate_ok(rate_key, is_auth):
        return JSONResponse({"error": "Rate limited. Please wait before generating again."}, status_code=429)

    prompt = f"""You are a senior software architect. Generate two markdown files for the project described below.

PROJECT NAME: {project_name}
DESCRIPTION: {description}
TECH STACK: {stack}
MAIN OBJECTIVE: {objective}

---

Output exactly two sections separated by the line "---SPLIT---".

FIRST SECTION — SPEC.md:
# {project_name}

## Objective
(1-3 sentences describing the project goal)

## Required Features
(bulleted list of 5-10 features)

## Tech Stack
(bulleted list of technologies with brief justification)

## Suggested Architecture
(describe the architecture: modules, data flow, key design decisions)

## File Structure
(tree-like structure of the project files)

## Constraints
(list of non-functional requirements: performance, security, etc.)

SECOND SECTION — OPENCODE.md:
# Instructions for {project_name}

## Context
(describe the project context for the AI coding agent — what this project is, what it does)

## Rules
(bulleted list of 8-12 coding rules the AI must follow: style, patterns, testing, etc.)

## Workflow
(step-by-step workflow the AI should follow when implementing features)

## File Conventions
(naming conventions, directory structure, import patterns)

---

Be concise and practical. Output raw markdown only, no code fences around the markdown.
Stack-specific: tailor the rules, architecture, and conventions to {stack}.
"""

    messages = [
        {"role": "system", "content": "You are a precise software architect. Output only the requested markdown, nothing else."},
        {"role": "user", "content": prompt},
    ]

    try:
        result = await p.submit_job(messages, max_tokens=4096, api_token=api_token)
    except RuntimeError as e:
        log.warning(f"generate-spec pool error: {e}")
        return JSONResponse({"error": "Pool is busy. Please try again in a moment."}, status_code=503)

    raw_text = result.get("text", "")

    # Split into SPEC.md and OPENCODE.md
    if "---SPLIT---" in raw_text:
        parts = raw_text.split("---SPLIT---", 1)
        spec_md = parts[0].strip()
        opencode_md = parts[1].strip()
    else:
        # Fallback: try to split on "# Instructions" or "OPENCODE"
        for marker in ["# Instructions for", "# Instructions", "## OPENCODE"]:
            idx = raw_text.find(marker)
            if idx > 0:
                spec_md = raw_text[:idx].strip()
                opencode_md = raw_text[idx:].strip()
                break
        else:
            spec_md = raw_text.strip()
            opencode_md = f"# Instructions for {project_name}\n\n## Context\nSee SPEC.md\n\n## Rules\n- Follow {stack} best practices\n- Write clean, documented code\n\n## Workflow\n1. Read SPEC.md\n2. Implement features one by one\n3. Test each feature"

    return {
        "spec_md": spec_md,
        "opencode_md": opencode_md,
        "model": result.get("model", "unknown"),
        "tokens_per_sec": result.get("tokens_per_sec"),
    }
