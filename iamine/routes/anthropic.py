"""Endpoint /v1/messages — compatibilite API Anthropic.

Traduit le format Anthropic (Claude Code, SDK Anthropic) vers le format
OpenAI interne utilise par le pool IAMINE. Permet d'utiliser IAMINE
comme drop-in replacement pour l'API Anthropic.

Usage avec Claude Code:
    $env:ANTHROPIC_BASE_URL = "https://iamine.org"
    $env:ANTHROPIC_API_KEY = "acc_xxx"
    claude --model iamine
"""

import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()
log = logging.getLogger("iamine.routes.anthropic")


def _pool():
    from iamine.pool import pool
    return pool


def _accounts():
    from iamine.pool import _accounts
    return _accounts


@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Endpoint compatible API Anthropic /v1/messages.

    Traduit la requete Anthropic → format OpenAI interne → reponse Anthropic.
    """
    pool = _pool()

    # Auth : header x-api-key ou Authorization Bearer
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            api_key = auth[7:]

    body = await request.json()

    # Extraire les champs Anthropic
    model = body.get("model", "auto")
    messages_anthropic = body.get("messages", [])
    system_raw = body.get("system", "")
    max_tokens = body.get("max_tokens", 512)
    stream = body.get("stream", False)

    # System prompt peut etre string ou liste de blocks
    if isinstance(system_raw, list):
        system_prompt = "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in system_raw
        )
    else:
        system_prompt = str(system_raw) if system_raw else ""

    # Convertir messages Anthropic → OpenAI
    messages_openai = []
    if system_prompt:
        messages_openai.append({"role": "system", "content": system_prompt})

    for msg in messages_anthropic:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # Anthropic content peut etre une liste de blocks
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "\n".join(text_parts)
        messages_openai.append({"role": role, "content": content})

    if not messages_openai:
        return JSONResponse({"error": {"type": "invalid_request_error", "message": "messages required"}}, status_code=400)

    # Soumettre au pool via le meme chemin que /v1/chat/completions
    try:
        result = await pool.submit_job(
            messages_openai, max_tokens,
            api_token=api_key,
            requested_model=model if model != "iamine" else None,
        )
    except RuntimeError as e:
        return JSONResponse({
            "type": "error",
            "error": {"type": "overloaded_error", "message": str(e)},
        }, status_code=529)

    text = result.get("text", "")
    tokens_out = result.get("tokens_generated", 0)
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    # Si streaming demande
    if stream:
        async def stream_response():
            # message_start
            yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'id':msg_id,'type':'message','role':'assistant','content':[],'model':model,'stop_reason':None,'usage':{'input_tokens':0,'output_tokens':0}}})}\n\n"
            # content_block_start
            yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
            # content_block_delta (envoyer le texte complet d'un coup car on n'a pas de vrai streaming)
            yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n"
            # content_block_stop
            yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
            # message_delta
            yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':tokens_out}})}\n\n"
            # message_stop
            yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"

        return StreamingResponse(stream_response(), media_type="text/event-stream")

    # Reponse non-streaming (format Anthropic)
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": text}
        ],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": sum(len(m.get("content", "").split()) for m in messages_openai),
            "output_tokens": tokens_out,
        },
    }
