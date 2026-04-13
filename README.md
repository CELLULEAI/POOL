# Cellule.ai — Decentralized AI Inference Network

**AI belongs to those who run it.**

Cellule.ai is a community-powered distributed LLM inference network. Anyone can contribute computing power (CPU, GPU) to run AI models. No subscription, no payment — the network belongs to its contributors.

## Quick Start

```bash
pip install iamine-ai -i https://cellule.ai/pypi --extra-index-url https://pypi.org/simple
python -m iamine worker --auto
```

Your machine auto-detects hardware, discovers the best pool, downloads the right model, and starts contributing.

## How It Works

```
Workers (your PC)              Pool (cellule.ai)              Users / Agents
+--------------+              +-------------------+          +-------------+
| Auto worker  |<------------>|  Smart Router     |<-------->| API / MCP   |
| or Proxy     |  WebSocket   |  - load balancing |   HTTP   | OpenCode    |
| + GGUF model |              |  - gap detection  |          | ClawCode    |
+--------------+              |  - auto-migration |          | Cursor      |
                              +-------------------+          +-------------+
                                     ^    ^
                              +------+    +------+
                              |                  |
                     +--------+------+  +--------+------+
                     | Federated     |  | Federated     |
                     | Pool (Docker) |  | Pool (Docker) |
                     +---------------+  +---------------+
```

1. **You share your PC's power** — CPU or GPU runs AI models (GGUF format)
2. **Intelligent placement** — the network detects where you're most useful
3. **Pools federate** — multiple pools form a molecule (RAID-like resilience)
4. **Workers auto-migrate** — if a pool goes down, workers move to the best available
5. **Agents remember** — 4-tier memory persists across sessions and pools

## Features

### Compute
- **Multi-platform** — Linux, macOS, Windows (CPU, NVIDIA CUDA, AMD ROCm, Apple Metal)
- **Two modes** — Auto (plug & play) or Proxy (bring your own LLMs)
- **Sub-agents** — auto-review, security audit, test generation, documentation (parallel pipeline)

### Federation
- **Ed25519-signed protocol** — pools communicate via cryptographic envelopes
- **Auto-migration** — workers failover to the best pool in ~35 seconds
- **RAID-like resilience** — lose a pool, lose no data

### Agent Memory (M13)
- **4-tier memory system** — observations, episodes, semantic facts, procedural patterns
- **Hybrid retrieval** — vector similarity + relationship graph + procedures
- **Ebbinghaus decay** — stale memories fade, frequently accessed ones strengthen
- **MCP server** — any MCP-compatible agent (Claude Code, OpenCode, Cursor) can read/write collective pool memory
- **Zero-knowledge** — all content encrypted with user token (PBKDF2 + Fernet), pools cannot read your data
- **Federation sync** — semantic facts and procedures replicate across bonded pools

```bash
# Connect any MCP-compatible agent to pool memory
iamine mcp-server --pool-url https://cellule.ai --token acc_xxxxx
```

### API
- **OpenAI-compatible** — drop-in replacement for `/v1/chat/completions`
- **Persistent memory** — 3-level compaction + encrypted RAG (pgvector)
- **SSE streaming** — real-time token streaming with sub-agent review metadata

## Run Your Own Pool

```bash
docker compose up -d
```

Docker image: `celluleai/pool` — see [Docker Hub](https://hub.docker.com/r/celluleai/pool)

## API Usage

```bash
curl -X POST https://cellule.ai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello!"}],"max_tokens":200}'
```

### Memory API

```bash
# Check memory status
curl "https://cellule.ai/v1/memory/status?api_token=acc_xxxxx"

# Search across all memory tiers
curl "https://cellule.ai/v1/memory/search?q=routing&api_token=acc_xxxxx"

# Record an observation
curl -X POST "https://cellule.ai/v1/memory/observe?api_token=acc_xxxxx" \
  -H "Content-Type: application/json" \
  -d '{"content":"Found a bug in auth flow","source_type":"tool_call"}'
```

## MCP Server

Any MCP-compatible coding agent can connect to the pool's collective memory:

```json
{
  "mcpServers": {
    "cellule-memory": {
      "command": "python",
      "args": ["-m", "iamine", "mcp-server", "--pool-url", "https://cellule.ai", "--token", "YOUR_TOKEN"]
    }
  }
}
```

**8 MCP tools**: `memory_status`, `memory_search`, `memory_observe`, `memory_episodes`, `memory_procedures`, `memory_graph`, `memory_consolidate`, `memory_forget_all`

## Requirements

- Python 3.10+
- 4 GB RAM minimum (8 GB recommended)
- No GPU required (but CUDA/ROCm/Metal supported)

## Status

The project is in **alpha**. The network is live with federated pools and active workers.

- **$IAMINE token** — participation token for contributors (ALPHA, not yet deployed). Not a financial instrument.

## Links

- **Website**: [cellule.ai](https://cellule.ai)
- **Try the AI**: [cellule.ai](https://cellule.ai) (6 messages, no account needed)
- **Pool status**: [cellule.ai/v1/status](https://cellule.ai/v1/status)
- **Docker Hub**: [celluleai/pool](https://hub.docker.com/r/celluleai/pool)

## License

MIT
