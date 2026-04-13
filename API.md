# IAMINE Pool API Reference

**Base URL:** `https://iamine.org`
**Interactive Docs:** [https://iamine.org/docs](https://iamine.org/docs) (Swagger UI) | [https://iamine.org/redoc](https://iamine.org/redoc) (ReDoc)

## Authentication

IAMINE uses Bearer tokens. Two token types exist:

| Type | Prefix | Obtained via | Usage |
|------|--------|-------------|-------|
| Account token | `acc_*` | `/v1/auth/register` or `/v1/auth/login` | Full API access, consolidated credits |
| Worker token | `iam_*` | Automatically assigned when a worker joins the pool | API access with worker-earned credits |

**Header:** `Authorization: Bearer <token>`

## Common Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad request (missing or invalid parameters) |
| 401 | Unauthorized (missing or invalid token) |
| 402 | Insufficient credits |
| 403 | Forbidden (valid token but no access to resource) |
| 404 | Resource not found |
| 409 | Conflict (e.g. email already registered) |
| 429 | Rate limited (30 req/min per token) |
| 503 | Service unavailable (pool overloaded, but pool tries to always respond) |
| 504 | Gateway timeout |

## Common Headers

| Header | Required | Description |
|--------|----------|-------------|
| `Authorization` | Yes (most endpoints) | `Bearer acc_*` or `Bearer iam_*` |
| `Content-Type` | Yes (POST/PUT/DELETE with body) | `application/json` |
| `X-Session-Id` | No | Session identifier for conversation continuity |

---

## Inference

### POST /v1/chat/completions
OpenAI-compatible chat completion endpoint. Supports streaming, tool calls, and conversation memory.

- **Auth:** Bearer token (`acc_*` or `iam_*`)
- **Rate limit:** 30 requests/minute per token
- **Body:**
```json
{
  "messages": [{"role": "user", "content": "Hello"}],
  "model": "iamine",
  "max_tokens": 512,
  "stream": false,
  "conv_id": "optional-conversation-id",
  "tools": [{"type": "function", "function": {"name": "...", "parameters": {}}}],
  "webhook_url": "https://example.com/callback"
}
```
- **Response (non-streaming):**
```json
{
  "id": "iamine-abc123",
  "object": "chat.completion",
  "model": "Qwen3.5-7B-Q4_K_M",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "...", "tool_calls": []},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 10, "completion_tokens": 50, "total_tokens": 60},
  "conv_id": "auto_abc123...",
  "iamine": {
    "worker_id": "Vertex-307b",
    "tokens_per_sec": 14.2,
    "duration_sec": 3.5,
    "compacted": false
  }
}
```
- **Response (streaming):** SSE stream with `data: {"id":...,"object":"chat.completion.chunk",...}` events, terminated by `data: [DONE]`
- **Queue fallback:** When pool is saturated, returns `iamine.pending=true` with a `job_id` to poll via `GET /v1/jobs/{job_id}`
- **Special commands:** Sending "enregistre" / "save" / "restaure" / "restore" as the last user message triggers conversation save/restore (requires `acc_*` token with memory enabled)
- **Notes:**
  - Model aliases `"auto"`, `"iamine"`, `"pool"` all route to smart routing
  - When `tools` are provided, routing automatically selects the largest capable model
  - `conv_id` is auto-derived from token + `X-Session-Id` header if not provided
- **Errors:** 400 (invalid messages/max_tokens), 401 (invalid token), 429 (rate limited)

---

### POST /v1/api/chat
Authenticated chat endpoint. Costs 1 $IAMINE credit per request.

- **Auth:** Bearer token (`acc_*` or `iam_*`)
- **Body:**
```json
{
  "messages": [{"role": "user", "content": "Hello"}],
  "max_tokens": 512,
  "conv_id": "optional",
  "model": "auto",
  "webhook_url": "https://..."
}
```
- **Response:** Same OpenAI-compatible format as `/v1/chat/completions`, plus:
```json
{
  "iamine": {
    "worker_id": "...",
    "tokens_per_sec": 14.2,
    "credits_remaining": 42.0
  }
}
```
- **Errors:** 401 (invalid token), 402 (insufficient credits), 429 (queue limit)

---

### POST /v1/messages
Anthropic API-compatible endpoint. Translates Anthropic message format to internal OpenAI format.

- **Auth:** `x-api-key` header or `Authorization: Bearer` token
- **Body (Anthropic format):**
```json
{
  "model": "iamine",
  "messages": [{"role": "user", "content": "Hello"}],
  "system": "You are helpful.",
  "max_tokens": 512,
  "stream": false
}
```
- **Response (non-streaming):**
```json
{
  "id": "msg_abc123...",
  "type": "message",
  "role": "assistant",
  "content": [{"type": "text", "text": "..."}],
  "model": "iamine",
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 10, "output_tokens": 50}
}
```
- **Response (streaming):** SSE events: `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`
- **Usage:** Drop-in replacement for Anthropic API (Claude Code, Anthropic SDK):
```bash
export ANTHROPIC_BASE_URL="https://iamine.org"
export ANTHROPIC_API_KEY="acc_xxx"
claude --model iamine
```
- **Errors:** 400 (missing messages), 401 (invalid/expired token), 529 (pool overloaded)

---

### POST /v1/generate-spec
Generate SPEC.md + OPENCODE.md project scaffolding using the pool.

- **Auth:** Optional (demo with rate limit: 2/min anonymous, 10/min authenticated)
- **Body:**
```json
{
  "project_name": "My App",
  "description": "A web app for...",
  "stack": "python",
  "objective": "Build a REST API..."
}
```
- **Response:**
```json
{
  "spec_md": "# My App\n## Objective\n...",
  "opencode_md": "# Instructions for My App\n...",
  "model": "Qwen3.5-7B",
  "tokens_per_sec": 12.5
}
```
- **Errors:** 400 (missing project_name), 429 (rate limited), 503 (pool busy)

---

### GET /v1/jobs/{job_id}
Poll the status of a queued job (returned when pool is saturated).

- **Auth:** Bearer token (optional, via header or `?token=` query param)
- **Response (pending):**
```json
{
  "status": "pending",
  "queue_depth": 3,
  "estimated_wait_sec": 15
}
```
- **Response (completed):**
```json
{
  "status": "completed",
  "response": {}
}
```
- **Other statuses:** `"processing"`, `"failed"`
- **Errors:** 404 (job not found)

---

## Auth & Account

### POST /v1/auth/register
Create a new user account.

- **Auth:** None
- **Body:**
```json
{
  "email": "user@example.com",
  "password": "min8chars",
  "pseudo": "MyPseudo",
  "display_name": "My Display Name"
}
```
- **Response:**
```json
{
  "account_id": "abc123...",
  "session_id": "sess_...",
  "api_token": "acc_...",
  "pseudo": "MyPseudo",
  "display_name": "My Display Name"
}
```
- **Errors:** 400 (missing fields, password < 8 chars, missing pseudo), 409 (email already registered)

---

### POST /v1/auth/login
Log in with email and password.

- **Auth:** None
- **Body:**
```json
{
  "email": "user@example.com",
  "password": "mypassword"
}
```
- **Response:**
```json
{
  "account_id": "abc123...",
  "session_id": "sess_...",
  "api_token": "acc_...",
  "display_name": "My Display Name"
}
```
- **Errors:** 401 (invalid email or password)

---

### POST /v1/auth/google
Sign in with Google (OAuth). Automatically creates an account if the email is new.

- **Auth:** None
- **Body:**
```json
{
  "credential": "<google-jwt-token>"
}
```
- **Response:**
```json
{
  "account_id": "...",
  "session_id": "...",
  "api_token": "acc_...",
  "display_name": "John Doe",
  "pseudo": "john",
  "email": "john@gmail.com",
  "picture": "https://...",
  "needs_pseudo": true
}
```
- **Errors:** 400 (missing credential), 401 (expired/invalid token, email not verified, wrong audience)

---

### POST /v1/account/link-worker
Link a worker to a user account (by its API token).

- **Auth:** Session-based (`session_id` in body)
- **Body:**
```json
{
  "session_id": "sess_...",
  "api_token": "iam_..."
}
```
- **Response:**
```json
{
  "status": "ok",
  "worker_id": "Vertex-307b",
  "account_workers": 3
}
```
- **Errors:** 401 (invalid session), 404 (invalid api_token or worker not linked)

---

### POST /v1/account/unlink-worker
Remove a worker from a user account.

- **Auth:** Session-based
- **Body:**
```json
{
  "session_id": "sess_...",
  "worker_id": "Vertex-307b"
}
```
- **Response:**
```json
{
  "status": "ok",
  "worker_id": "Vertex-307b",
  "remaining_workers": 2
}
```
- **Errors:** 401 (invalid session), 404 (worker not linked)

---

### GET /v1/account/my-workers
List workers linked to the account with consolidated balance.

- **Auth:** `?session_id=sess_...`
- **Response:**
```json
{
  "account_id": "...",
  "account_token": "acc_...",
  "display_name": "John",
  "email": "john@example.com",
  "eth_address": null,
  "worker_count": 2,
  "workers": [{
    "worker_id": "Vertex-307b",
    "is_online": true,
    "model": "Qwen3.5-7B-Q4_K_M",
    "assigned_model": "Qwen 3.5 7B",
    "model_status": "ok",
    "jobs_done": 142,
    "credits": 42.0,
    "total_earned": 142.0,
    "api_token": "iam_abc123..."
  }],
  "total_credits": 84.0,
  "total_earned": 284.0
}
```
- **Errors:** 401 (invalid session)

---

### POST /v1/account/set-pseudo
Set or change the account display name / pseudo.

- **Auth:** Session-based
- **Body:**
```json
{
  "session_id": "sess_...",
  "pseudo": "NewPseudo"
}
```
- **Response:** `{"pseudo": "NewPseudo", "display_name": "NewPseudo"}`
- **Errors:** 400 (pseudo < 2 chars), 401 (invalid session)

---

### POST /v1/account/set-eth
Link an Ethereum address to the account (for future Web3 integration).

- **Auth:** Session-based
- **Body:**
```json
{
  "session_id": "sess_...",
  "eth_address": "0x1234...abcd"
}
```
- **Response:** `{"status": "ok", "eth_address": "0x1234...abcd"}`
- **Errors:** 400 (invalid ETH address), 401 (invalid session)

---

## Memory & Conversations

### GET /v1/account/memory
Get memory (RAG) status for the account.

- **Auth:** `?session_id=sess_...`
- **Response:**
```json
{
  "memory_enabled": true,
  "facts_count": 15
}
```
- **Errors:** 401 (invalid session)

---

### POST /v1/account/memory
Enable or disable persistent memory (RAG) for the account.

- **Auth:** Session-based
- **Body:**
```json
{
  "session_id": "sess_...",
  "enabled": true
}
```
- **Response:**
```json
{
  "memory_enabled": true,
  "message": "Memoire persistante activee"
}
```
- **Errors:** 400 (missing enabled field), 401 (invalid session)

---

### GET /v1/account/conversations
List saved conversations for the authenticated user.

- **Auth:** Bearer token (`acc_*`)
- **Response:**
```json
{
  "conversations": [{
    "conv_id": "auto_abc123",
    "title": "Chat about Python",
    "message_count": 12,
    "last_activity": "2026-04-08T10:30:00"
  }]
}
```
- **Errors:** 401 (missing/invalid Bearer token)

---

### GET /v1/account/conversations/{conv_id}
Export a full conversation (messages + summary).

- **Auth:** Bearer token (`acc_*`)
- **Query params:** `?format=json` (default) or `?format=markdown`
- **Response (JSON):**
```json
{
  "conv_id": "auto_abc123",
  "title": "Chat about Python",
  "summary": "...",
  "message_count": 12,
  "total_tokens": 3400,
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```
- **Response (Markdown):** Plain text markdown export
- **Errors:** 401 (invalid token), 404 (conversation not found)

---

### DELETE /v1/account/conversations
Delete ALL conversations for the user (GDPR right to erasure).

- **Auth:** Bearer token (`acc_*`)
- **Response:**
```json
{
  "status": "deleted",
  "conversations_deleted_db": 5,
  "conversations_deleted_ram": 1
}
```
- **Errors:** 401 (invalid token)

---

### DELETE /v1/account/conversations/{conv_id}
Delete a specific conversation.

- **Auth:** Bearer token (`acc_*`)
- **Response:**
```json
{
  "status": "deleted",
  "conv_id": "auto_abc123",
  "deleted_db": true,
  "deleted_ram": true
}
```
- **Errors:** 401 (invalid token), 404 (not found or not owned)

---

### DELETE /v1/account/data
Complete data deletion (GDPR right to be forgotten). Removes all conversations and RAG memories.

- **Auth:** Bearer token
- **Response:**
```json
{
  "status": "deleted",
  "conversations_deleted": 6,
  "memories_deleted": 15
}
```
- **Errors:** 401 (invalid token)

---

### GET /v1/account/memories
List stored RAG facts (decrypted).

- **Auth:** Bearer token (`acc_*`)
- **Response:**
```json
{
  "memories": [
    {"id": 1, "fact": "User likes Python", "created": "2026-04-01T12:00:00"}
  ]
}
```
- **Errors:** 401 (invalid token)

---

### DELETE /v1/account/memories/{memory_id}
Delete a specific stored memory fact.

- **Auth:** Bearer token (`acc_*`)
- **Response:** `{"deleted": true, "memory_id": 1}`
- **Errors:** 401 (invalid token), 404 (not found or not owned)

---

### GET /v1/conversations
List persistent conversations (alternative endpoint).

- **Auth:** `?api_token=acc_...`
- **Response:** `{"conversations": [...], "count": 5}`
- **Errors:** 401 (account token required)

---

### GET /v1/conversations/{conv_id}
Load a full conversation.

- **Auth:** `?api_token=acc_...`
- **Response:** Full conversation object with messages and summary
- **Errors:** 401 (token required), 404 (not found)

---

### DELETE /v1/conversation/{conv_id}
Delete a conversation (all layers: L1 RAM + L2 summary + L3 DB).

- **Auth:** `?api_token=...`
- **Response:** `{"status": "deleted", "conv_id": "..."}`
- **Errors:** 403 (unauthorized, wrong token)

---

### DELETE /v1/memory
Delete all vectorized memories for a user (right to be forgotten).

- **Auth:** JSON body `{"api_token": "acc_..."}`
- **Response:** `{"status": "deleted", "facts_deleted": 15}`
- **Errors:** 401 (account token required)

---

### GET /v1/memory/stats
Get vectorized memory statistics.

- **Auth:** `?api_token=acc_...`
- **Response:**
```json
{
  "facts_count": 15,
  "first_memory": "2026-03-15T10:00:00",
  "last_memory": "2026-04-08T09:30:00"
}
```
- **Errors:** 401 (account token required)

---

## Status & Pool Info

### GET /v1/status
Pool status overview.

- **Auth:** None
- **Response:** Pool status object (workers online, version, uptime, etc.)

---

### GET /v1/models
List available models (OpenAI-compatible).

- **Auth:** None
- **Response:**
```json
{
  "object": "list",
  "data": [{
    "id": "iamine",
    "object": "model",
    "created": 1712534400,
    "owned_by": "iamine-pool",
    "workers": 5
  }]
}
```

---

### GET /v1/pool/power
Analyze pool computing power and recommend optimal models.

- **Auth:** None
- **Response:**
```json
{
  "pool_capacity_tps": 85.3,
  "recommended_model": "...",
  "workers": [],
  "model_catalog": [
    {"id": "qwen3.5-7b-q4", "name": "Qwen 3.5 7B", "params": "7B", "size_gb": 4.2, "ram_required_gb": 6, "quality_score": 70}
  ]
}
```

---

### GET /v1/admin/models
List all models with their unlock status based on pool capacity.

- **Auth:** None (public)
- **Response:**
```json
{
  "pool_tps": 85.3,
  "max_worker_ram_gb": 32,
  "models": [{"id": "...", "name": "...", "status": "active|unlocked|locked"}],
  "summary": {"active": 2, "unlocked": 3, "locked": 5, "total": 10}
}
```

---

### GET /v1/wallet/{api_token}
Check credit balance for an API token.

- **Auth:** None (token in URL path)
- **Response:**
```json
{
  "worker_id": "Vertex-307b",
  "api_token": "iam_abc123...",
  "credits": 42.0,
  "requests_used": 100,
  "is_online": true,
  "can_use_api": true
}
```
- **Errors:** 403 (invalid token)

---

### GET /v1/router/stats
Smart router statistics (active conversations, tokens in memory).

- **Auth:** None
- **Response:** Router stats object

---

### POST /v1/worker/bench
Submit benchmark results from a connected worker.

- **Auth:** None (worker must be connected)
- **Body:**
```json
{
  "worker_id": "Vertex-307b",
  "avg_tps": 14.2
}
```
- **Response:**
```json
{
  "status": "ok",
  "recommended_model": "qwen3.5-7b-q4",
  "model_name": "Qwen 3.5 7B",
  "model_repo": "...",
  "model_file": "...",
  "model_size_gb": 4.2,
  "recommended_ctx": 4096,
  "quality_score": 70
}
```
- **Errors:** 403 (worker not connected)

---

### GET /v1/models/available
List GGUF model files available for download on the server.

- **Auth:** None
- **Response:**
```json
{
  "models": [
    {"filename": "model.gguf", "size_mb": 4200, "download_url": "/v1/models/download/model.gguf"}
  ]
}
```

---

### GET /v1/models/download/{filename}
Download a GGUF model file.

- **Auth:** None
- **Response:** Binary file (application/octet-stream)
- **Errors:** 400 (invalid filename), 404 (model not found)

---

## Tools & Utilities

### POST /v1/contact
Submit a contact message.

- **Auth:** None
- **Body:**
```json
{
  "name": "John",
  "email": "john@example.com",
  "message": "Hello..."
}
```
- **Response:** `{"status": "ok"}`

---

### GET /install.sh
Linux installation script.

- **Auth:** None
- **Usage:** `curl -sL https://iamine.org/install.sh | bash`

---

### GET /install.ps1
Windows installation script.

- **Auth:** None
- **Usage:** `irm https://iamine.org/install.ps1 | iex`

---

## Admin

All admin endpoints require authentication via:
- Cookie `session_id` (Google OAuth admin) + DB `admin_users` check, or
- Cookie/query param `admin_token`

### POST /admin/login
Admin login with email/password.

- **Body:** `{"email": "...", "password": "..."}`
- **Response:** `{"ok": true, "email": "..."}` (sets `admin_token` cookie)
- **Errors:** 401 (invalid credentials)

### GET /admin
Admin dashboard page (HTML). Redirects to login if not authenticated.

### GET /admin/models
Admin models dashboard (HTML) with worker table, actions, and stats.

### GET /admin/api/stats
Pool statistics (JSON) for AJAX dashboard refresh.

- **Response:**
```json
{
  "workers_online": 5,
  "total_tps": 85.3,
  "pool_load": 0.4,
  "workers": [{
    "id": "Vertex-307b",
    "model": "Qwen3.5-7B-Q4_K_M.gguf",
    "real_tps": 14.2,
    "bench_tps": 15.0,
    "jobs_ok": 142,
    "jobs_failed": 2,
    "busy": false,
    "version": "0.2.47",
    "outdated": false,
    "unknown_model": false
  }]
}
```

### POST /admin/api/assign
Assign a specific model to a worker.

- **Body:** `{"worker_id": "...", "model_id": "qwen3.5-7b-q4"}`
- **Response:** `{"ok": true, "worker": "...", "model": "..."}`
- **Errors:** 404 (worker not connected or model not found)

### GET /admin/api/assignments
List all DB model assignments for connected workers.

### GET /admin/api/hardware-db
Hardware benchmark database (hashrate-style).

### POST /admin/api/set-ctx
Change a worker's context size (persisted in DB + sent to worker).

- **Body:** `{"worker_id": "...", "ctx_size": 8192}`
- **Errors:** 400 (ctx_size < 512)

### GET /admin/api/families
List available model families and the active one.

### POST /admin/api/set-family
Switch the active model family and migrate all workers.

- **Body:** `{"family": "qwen3.5"}`

### GET /v1/admin/tasks
Recent distributed task history (last 50).

### POST /admin/api/worker-cmd
Send a command to a worker via WebSocket.

- **Body:** `{"worker_id": "...", "cmd": "update_model|shutdown|restart|set_ctx", ...}`

### POST /admin/api/pool-managed
Enable/disable automatic pool management for a worker.

- **Body:** `{"worker_id": "...", "pool_managed": true}`

### POST /admin/api/migrate-all
Migrate all workers to the current active model family.

### POST /admin/api/commands
Create an admin command (tracked in DB).

- **Body:** `{"issued_by": "RED", "target_worker": "...", "command_type": "...", "payload": {}}`

### GET /admin/api/commands
List recent admin commands. Filters: `?status=...`, `?result_status=...`, `?limit=50`

### GET /admin/api/lessons
RED agent learned lessons (experience memory).

- **Query:** `?command_type=...&limit=20`

### POST /admin/api/commands/{command_id}/complete
Mark an admin command as completed (RED reports result).

- **Body:** `{"status": "success", "result": "...", "lesson_learned": "..."}`

### GET /admin/api/admins
List admin users.

### POST /admin/api/admins
Add an admin user.

- **Body:** `{"email": "admin@example.com"}`

### DELETE /admin/api/admins/{email}
Remove an admin user (cannot remove root admin).

- **Errors:** 403 (cannot remove root admin)

### GET /admin/api/config
Get pool configuration (SMTP, alerts, RED, checker settings).

### POST /admin/api/config
Update pool configuration in DB.

- **Body:** `{"system_prompt": "...", "smtp_host": "...", ...}`

### GET /admin/api/checker
LLM checker/ladder status: config + per-worker quality scores.

### POST /admin/api/checker
Update checker configuration.

- **Body:** `{"checker_enabled": true, "checker_tps_threshold": 8.0, ...}`

### POST /admin/api/alert
Send an alert (logged to DB, emailed if SMTP configured).

- **Body:** `{"subject": "...", "body": "...", "level": "info|warning|critical", "source": "RED"}`

### POST /admin/api/inference-report
Record an inference quality report (RED evaluates a worker).

### GET /admin/api/capabilities
List hardware capabilities of all known workers.

### POST /admin/api/red/memory-save
Save RED.md content to DB (versioned snapshots).

- **Body:** `{"content": "...", "reason": "auto-save"}`

### GET /admin/api/red/memory-history
List RED.md snapshots for rollback.

- **Query:** `?limit=10`

### GET /admin/api/red/memory-restore/{snapshot_id}
Retrieve a specific RED.md snapshot content.

- **Errors:** 404 (snapshot not found)

### DELETE /admin/api/worker/{worker_id}
Remove a worker from the pool and DB. Sends shutdown command if connected.

### GET /admin/api/queue
List pending jobs in the queue with stats.

### GET /admin/api/cleanup/preview
Preview workers eligible for cleanup (slow, offline, high failure rate).

- **Query:** `?min_tps=8.0&max_offline_hours=72`

### POST /admin/api/cleanup
Delete specified workers from DB.

- **Body:** `{"worker_ids": ["worker1", "worker2"]}`

### GET /admin/api/blacklist
Get the worker blacklist.

### POST /admin/api/blacklist/add
Ban a worker from the pool.

- **Body:** `{"worker_id": "..."}`

### POST /admin/api/blacklist/remove
Unban a worker.

- **Body:** `{"worker_id": "..."}`

### GET /admin/api/accounts
List all user accounts.

### POST /admin/api/accounts/credits
Modify account credits.

- **Body:** `{"account_id": "...", "credits": 100, "action": "set|add|subtract"}`

### DELETE /admin/api/accounts/{account_id}
Delete a user account and all associated data.

---

## Admin -- RED Agent Chat

### POST /admin/api/red/chat
Send a message to the RED autonomous agent via WebSocket.

- **Auth:** Admin required
- **Body:** `{"message": "pool status?", "max_tokens": 500}`
- **Response:**
```json
{
  "chat_id": "admin-red-abc123",
  "text": "Le pool a 5 workers...",
  "tokens_per_sec": 30.5,
  "duration_sec": 2.1,
  "error": null
}
```
- **Errors:** 401 (admin required), 400 (empty message), 503 (RED not connected), 504 (RED timeout 120s)

### GET /admin/api/red/status
RED agent connection status.

- **Response:** `{"status": "IDLE|BUSY|OFFLINE", "connected": true, "model": "...", ...}`

### GET /admin/api/red/chat-history
Get admin chat history with RED (last 50 exchanges).

### DELETE /admin/api/red/chat-history
Clear RED chat history.

---

## Dev / Debug

### GET /v1/dev/backup
Download a backup archive (if available).

### GET /v1/dev/signal
Signal endpoint for autonomous David/Claude upgrade loop.

- **Response:** `{"version": "0.2.47", "action": "upgrade", "message": "..."}`

### GET /v1/dev/inbox
Read agent reports (David, Regis, Wasa) with full content and Claude responses.

---

## Static & PyPI

### GET /
Homepage (serves index.html).

### GET /m
Mobile-optimized page.

### GET /pypi/
PyPI root index -- lists all locally hosted packages.

### GET /pypi/iamine-ai/
PyPI simple index for the iamine-ai package.

### GET /pypi/{package}/
PyPI simple index for any locally hosted package.

### GET /pypi/dist/{filename}
Download a wheel or sdist file.

---

## Federation (Planned)

Future endpoints for pool-to-pool federation:

- `POST /v1/federation/announce` -- Announce pool presence to federation
- `GET /v1/federation/peers` -- List known federated pools
- `POST /v1/federation/relay` -- Relay a job to a federated pool

*These endpoints are not yet implemented.*

---

## WebSocket

### WSS wss://iamine.org/ws
Worker WebSocket connection for joining the compute pool.

- **Protocol:** JSON messages over WebSocket
- **Handshake:** Worker sends identity (worker_id, hardware info, model info)
- **Messages from pool:** `{"type": "job", ...}`, `{"type": "command", "cmd": "update_model|shutdown|restart|set_ctx", ...}`
- **Messages from worker:** `{"type": "result", ...}`, `{"type": "bench", ...}`
- **Usage:** Workers connect via `python -m iamine worker --auto`
