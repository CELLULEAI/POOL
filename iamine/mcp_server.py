"""M13 Phase 3 — MCP Server for Cellule.ai Agent Memory.

Exposes pool memory as MCP tools for Claude Code, OpenCode, Cursor, etc.
Two modes:
  - Remote (default): calls pool REST API via HTTP
  - Direct: connects to PostgreSQL directly (co-located deployment)

Usage:
  python -m iamine.mcp_server --pool-url https://iamine.org --token acc_xxx
  # or via CLI:
  iamine mcp-server --pool-url https://iamine.org --token acc_xxx
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("iamine.mcp")

# --- Configuration ---
POOL_URL = os.environ.get("IAMINE_POOL_URL", "https://iamine.org")
API_TOKEN = os.environ.get("IAMINE_TOKEN", "")

mcp = FastMCP(
    "cellule-memory",
    instructions="Cellule.ai distributed agent memory — search, observe, and recall across federated pools"
)


# --- HTTP client for remote mode ---

def _client() -> httpx.Client:
    return httpx.Client(
        base_url=POOL_URL,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=30.0,
        verify=False,  # Self-signed certs on some pools
    )


def _get(path: str, params: dict | None = None) -> dict:
    """GET request to pool API."""
    with _client() as c:
        params = params or {}
        params["api_token"] = API_TOKEN
        r = c.get(path, params=params)
        r.raise_for_status()
        return r.json()


def _post(path: str, data: dict | None = None) -> dict:
    """POST request to pool API."""
    with _client() as c:
        r = c.post(f"{path}?api_token={API_TOKEN}", json=data or {})
        r.raise_for_status()
        return r.json()


def _delete(path: str) -> dict:
    """DELETE request to pool API."""
    with _client() as c:
        r = c.delete(f"{path}?api_token={API_TOKEN}")
        r.raise_for_status()
        return r.json()


# --- MCP Tools ---

@mcp.tool()
def memory_status() -> str:
    """Get memory statistics: observation count, episodes, semantic facts, procedures.
    Use this to understand how much memory the pool has accumulated for your account."""
    result = _get("/v1/memory/status")
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Search across all memory tiers (semantic facts, episodes, procedures).
    Returns matching memories ranked by relevance.
    Use this before starting work to recall relevant context from past sessions."""
    result = _get("/v1/memory/search", {"q": query, "limit": limit})
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_observe(content: str, source_type: str = "tool_call",
                    conv_id: str = "", metadata: str = "{}") -> str:
    """Record an observation in working memory.
    Use this to save important findings, decisions, or context that should persist.
    source_type: tool_call, inference, review, pipeline, federation
    metadata: JSON string with extra context (optional)."""
    try:
        meta = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError:
        meta = {}

    result = _post("/v1/memory/observe", {
        "content": content,
        "source_type": source_type,
        "conv_id": conv_id,
        "metadata": meta,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_episodes(limit: int = 10) -> str:
    """List recent session episodes (consolidated summaries of past work).
    Episodes are created automatically when enough observations accumulate.
    Use this to review what happened in previous sessions."""
    result = _get("/v1/memory/episodes", {"limit": limit})
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_procedures(limit: int = 10) -> str:
    """List active procedural memories (recurring workflow patterns).
    Procedures are detected automatically from episode patterns.
    Use this to find known-good approaches for similar tasks."""
    result = _get("/v1/memory/procedures", {"limit": limit})
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_graph(fact_id: int, depth: int = 1) -> str:
    """Get the relationship graph around a specific memory fact.
    Shows related facts connected by similarity or causation.
    Use this to explore how memories are linked."""
    result = _get("/v1/memory/graph", {"fact_id": fact_id, "depth": depth})
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_consolidate(conv_id: str = "") -> str:
    """Force consolidation of pending observations into an episode.
    Normally happens automatically after 5+ observations.
    Use this at the end of a session to ensure work is captured."""
    result = _post("/v1/memory/consolidate", {"conv_id": conv_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_forget_all() -> str:
    """RGPD: Permanently delete all memory tiers for your account.
    WARNING: This is irreversible. All observations, episodes, facts, and procedures will be deleted."""
    result = _delete("/v1/memory/forget-all")
    return json.dumps(result, indent=2)


# --- MCP Resources ---

@mcp.resource("memory://status")
def resource_status() -> str:
    """Current memory statistics."""
    return memory_status()


@mcp.resource("memory://episodes")
def resource_episodes() -> str:
    """Recent session episodes."""
    return memory_episodes()


@mcp.resource("memory://procedures")
def resource_procedures() -> str:
    """Active procedural memories."""
    return memory_procedures()


# --- Entry point ---

def main():
    global POOL_URL, API_TOKEN

    parser = argparse.ArgumentParser(
        description="Cellule.ai MCP Memory Server")
    parser.add_argument("--pool-url", default=POOL_URL,
                        help="Pool URL (default: $IAMINE_POOL_URL or https://iamine.org)")
    parser.add_argument("--token", default=API_TOKEN,
                        help="Account token (default: $IAMINE_TOKEN)")
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "streamable-http"],
                        help="MCP transport (default: stdio)")
    args = parser.parse_args()

    POOL_URL = args.pool_url
    API_TOKEN = args.token

    if not API_TOKEN:
        print("Error: --token or $IAMINE_TOKEN required", file=sys.stderr)
        sys.exit(1)

    log.info(f"Cellule.ai MCP server starting — pool={POOL_URL}, transport={args.transport}")
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
