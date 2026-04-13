"""M12 — Agentic Recruitment: gap detection engine.

Each pool analyzes its connected workers vs observed job patterns to detect
capability gaps. Gaps are published via /v1/recruitment/needs so that new
workers/agents can self-select into the most valuable roles.

Invariants (token-guardian validated 2026-04-12):
- Routing is the SOLE incentive lever (no rate premium, no multiplier)
- Gap declaration requires corroboration from >= 1 bonded peer before
  influencing routing weights
- routing_reason must be recorded in revenue_ledger for auditability
- Feature-gated: M12_AGENTIC_ROUTING env var (default off)

Flag: M12_AGENTIC_ROUTING=off (scaffold, no activation)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger("iamine.recruitment")


def _get_pool_name(pool) -> str:
    """Get pool name from federation identity or fallback."""
    try:
        fed_self = getattr(pool, "federation_self", None)
        if fed_self and hasattr(fed_self, "name") and fed_self.name:
            return fed_self.name
    except Exception:
        pass
    return "cellule.ai"

# ---- Feature gate ----

def is_recruitment_enabled() -> bool:
    return os.environ.get("M12_AGENTIC_ROUTING", "off").lower() in ("on", "true", "1")


# ---- Gap types ----

@dataclass
class CapabilityGap:
    """A detected capability gap in the pool."""
    kind: str              # e.g. "llm.chat", "llm.tool-call", "llm.reasoning"
    model_class: str       # e.g. "proxy-9b", "reasoning-30b", "fast-3b"
    priority: str          # "critical", "high", "medium", "low"
    reason: str            # human-readable explanation
    detected_at: float = field(default_factory=time.time)
    min_tps: float = 0.0   # minimum throughput needed
    min_ctx: int = 0       # minimum context size needed
    corroborated_by: list[str] = field(default_factory=list)  # atom_ids that confirm


# ---- Gap detection ----

# Capability archetypes a pool should ideally have
IDEAL_CAPABILITIES = [
    {
        "kind": "llm.tool-call",
        "model_class": "proxy-agent",
        "description": "Agent proxy for tool-calling (code, search, file ops)",
        "min_workers": 1,
        "detect": lambda workers: _count_tool_capable(workers) == 0,
        "priority": "critical",
        "reason": "No tool-call capable agent — coding assistants cannot function",
    },
    {
        "kind": "llm.reasoning",
        "model_class": "reasoning-30b+",
        "description": "Large reasoning model (30B+ params) for complex tasks",
        "min_workers": 1,
        "detect": lambda workers: _count_by_size(workers, min_b=25) == 0,
        "priority": "high",
        "reason": "No large reasoning model — complex queries fall back to small models",
    },
    {
        "kind": "llm.chat",
        "model_class": "fast-responder",
        "description": "Fast chat model (>30 tok/s) for low-latency responses",
        "min_workers": 2,
        "detect": lambda workers: _count_fast(workers, min_tps=30) < 2,
        "priority": "medium",
        "reason": "Insufficient fast responders — user latency may be high",
    },
]


def _count_tool_capable(workers: dict) -> int:
    """Count workers that can handle tool-calls (proxy agents)."""
    count = 0
    for w in workers.values():
        info = getattr(w, "info", {}) or {}
        model = (info.get("model_path") or "").lower()
        # Tool-call capable: typically 9B+ instruct/coder models
        if any(kw in model for kw in ("coder", "instruct", "tool")):
            bench = info.get("bench_tps", 0) or 0
            if float(bench) >= 8:  # must be fast enough to be useful
                count += 1
    return count


def _count_by_size(workers: dict, min_b: float) -> int:
    """Count workers running models >= min_b billion params."""
    count = 0
    for w in workers.values():
        info = getattr(w, "info", {}) or {}
        model = (info.get("model_path") or "").lower()
        size = _extract_param_size(model)
        if size >= min_b:
            count += 1
    return count


def _count_fast(workers: dict, min_tps: float) -> int:
    """Count workers with bench_tps >= min_tps."""
    count = 0
    for w in workers.values():
        info = getattr(w, "info", {}) or {}
        try:
            if float(info.get("bench_tps", 0) or 0) >= min_tps:
                count += 1
        except (TypeError, ValueError):
            pass
    return count


def _extract_param_size(model_path: str) -> float:
    """Extract approximate param size in billions from model filename.
    E.g. 'qwen3-30b-a3b' -> 30.0, 'qwen3.5-2b' -> 2.0
    """
    import re
    m = re.search(r'(\d+\.?\d*)b', model_path.lower())
    if m:
        return float(m.group(1))
    return 0.0


def detect_gaps(pool) -> list[CapabilityGap]:
    """Analyze current pool state and return detected capability gaps.

    This runs on every call (lightweight). For persistence, see
    persist_gaps() which writes to DB with TTL.
    """
    workers = getattr(pool, "workers", {}) or {}

    gaps = []
    for archetype in IDEAL_CAPABILITIES:
        if archetype["detect"](workers):
            gaps.append(CapabilityGap(
                kind=archetype["kind"],
                model_class=archetype["model_class"],
                priority=archetype["priority"],
                reason=archetype["reason"],
            ))

    return gaps


def get_recruitment_needs(pool) -> dict:
    """Public API: return current recruitment needs for this pool.

    Returns a dict suitable for JSON serialization.
    """
    gaps = detect_gaps(pool)
    workers = getattr(pool, "workers", {}) or {}

    # Also include current workforce summary
    workforce = []
    for w in workers.values():
        info = getattr(w, "info", {}) or {}
        workforce.append({
            "model": (info.get("model_path") or "unknown").split("/")[-1],
            "tps": info.get("bench_tps", 0),
            "busy": getattr(w, "busy", False),
        })

    return {
        "pool_name": _get_pool_name(pool),
        "worker_count": len(workers),
        "gaps": [
            {
                "kind": g.kind,
                "model_class": g.model_class,
                "priority": g.priority,
                "reason": g.reason,
                "detected_at": g.detected_at,
                "corroborated": len(g.corroborated_by) > 0,
            }
            for g in gaps
        ],
        "workforce": workforce,
        "recruitment_active": is_recruitment_enabled(),
    }




# ---- Federation-wide gap analysis ----

async def detect_federation_gaps(pool) -> list[dict]:
    """Analyze ALL pools (self + federated peers) and return gaps per pool.
    
    Uses capabilities from federation_peers (synced by heartbeat).
    Returns a list of pool gap summaries for the UI.
    """
    from .federation import list_peers, compute_live_capabilities
    
    results = []
    
    # 1. Self gaps
    self_gaps = detect_gaps(pool)
    workers = getattr(pool, "workers", {}) or {}
    self_caps = compute_live_capabilities(pool)
    pool_name = _get_pool_name(pool)
    
    results.append({
        "pool_name": pool_name,
        "pool_type": "self",
        "worker_count": len(workers),
        "capabilities": self_caps,
        "gaps": [
            {
                "kind": g.kind,
                "model_class": g.model_class,
                "priority": g.priority,
                "reason": g.reason,
            }
            for g in self_gaps
        ],
    })
    
    # 2. Peer gaps (from heartbeat-synced capabilities)
    try:
        peers = await list_peers(pool, include_revoked=False)
    except Exception:
        peers = []
    
    for peer in peers:
        if peer.get("trust_level", 0) < 2:
            continue
        
        import json as _json
        caps = peer.get("capabilities") or []
        if isinstance(caps, str):
            try:
                caps = _json.loads(caps)
            except Exception:
                caps = []
        
        # Analyze peer capabilities to detect gaps
        peer_gaps = _analyze_peer_caps(caps)
        
        results.append({
            "pool_name": peer.get("name", "unknown"),
            "pool_type": "peer",
            "worker_count": sum(c.get("worker_count", 0) for c in caps),
            "capabilities": caps,
            "gaps": peer_gaps,
        })
    
    return results


def _analyze_peer_caps(caps: list) -> list[dict]:
    """Detect gaps from a peer pool's capability list."""
    gaps = []
    
    # Check for tool-call capable agent
    has_tool_agent = False
    has_reasoning = False
    fast_count = 0
    
    for cap in caps:
        model = (cap.get("model") or "").lower()
        tps = cap.get("max_tps", 0) or 0
        workers = cap.get("worker_count", 0) or 0
        
        if any(kw in model for kw in ("coder", "instruct", "tool")) and tps >= 8:
            has_tool_agent = True
        
        import re
        m = re.search(r"(\d+\.?\d*)b", model)
        if m and float(m.group(1)) >= 25:
            has_reasoning = True
        
        if tps >= 30 and workers > 0:
            fast_count += workers
    
    if not has_tool_agent:
        gaps.append({
            "kind": "llm.tool-call",
            "model_class": "proxy-agent",
            "priority": "critical",
            "reason": "No tool-call capable agent",
        })
    
    if not has_reasoning:
        gaps.append({
            "kind": "llm.reasoning",
            "model_class": "reasoning-30b+",
            "priority": "high",
            "reason": "No large reasoning model (30B+)",
        })
    
    if fast_count < 2:
        gaps.append({
            "kind": "llm.chat",
            "model_class": "fast-responder",
            "priority": "medium",
            "reason": "Insufficient fast responders (>30 tok/s)",
        })
    
    return gaps

# ---- Persistence (scaffold — activated when M12_AGENTIC_ROUTING=on) ----

async def persist_gaps(pool, gaps: list[CapabilityGap]) -> None:
    """Write detected gaps to DB with 24h TTL. Scaffold only."""
    if not is_recruitment_enabled():
        return
    if not (hasattr(pool.store, 'pool') and pool.store.pool):
        return
    import json as _json
    async with pool.store.pool.acquire() as conn:
        for g in gaps:
            await conn.execute(
                """INSERT INTO recruitment_needs
                   (capability_kind, model_class, priority, reason, detected_at, expires_at, corroborated_by, status)
                   VALUES (, , , , to_timestamp(), to_timestamp( + 86400), , 'open')
                   ON CONFLICT (capability_kind, model_class) WHERE status = 'open'
                   DO UPDATE SET priority = EXCLUDED.priority, detected_at = EXCLUDED.detected_at,
                                 expires_at = EXCLUDED.expires_at""",
                g.kind, g.model_class, g.priority, g.reason,
                g.detected_at, _json.dumps(g.corroborated_by),
            )
    log.info(f"recruitment: persisted {len(gaps)} gaps to DB")
