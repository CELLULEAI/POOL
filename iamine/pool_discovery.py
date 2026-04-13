"""M12 — Pool discovery and intelligent placement.

A worker discovers available pools, queries their recruitment needs,
and selects the pool where it will be most useful (gap-filling).

Flow:
  1. Seed list (hardcoded + config + env)
  2. For each pool: GET /v1/recruitment/needs + /v1/federation/pools
  3. Score: where does MY profile fill the biggest gap?
  4. Return best pool URL

On pool failure, the worker re-runs discovery (failover).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("iamine.discovery")

# Default seed pools — cellule.ai is the bootstrap entry point
DEFAULT_SEEDS = [
    "https://cellule.ai",
]

# Priority scores for gap severity
PRIORITY_SCORE = {
    "critical": 100,
    "high": 50,
    "medium": 20,
    "low": 5,
}


@dataclass
class PoolCandidate:
    url: str
    name: str = "unknown"
    worker_count: int = 0
    gaps: list = None
    score: float = 0.0
    reachable: bool = False
    ws_url: str = ""
    latency_ms: float = 9999.0

    def __post_init__(self):
        if self.gaps is None:
            self.gaps = []


def get_seed_pools() -> list[str]:
    """Return list of pool base URLs to probe."""
    seeds = list(DEFAULT_SEEDS)

    # From env var (comma-separated)
    env_seeds = os.environ.get("IAMINE_POOL_SEEDS", "")
    if env_seeds:
        seeds.extend(s.strip() for s in env_seeds.split(",") if s.strip())

    # Deduplicate preserving order
    seen = set()
    result = []
    for s in seeds:
        s = s.rstrip("/")
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _probe_pool(base_url: str, timeout: int = 10) -> PoolCandidate:
    """Probe a single pool for recruitment needs."""
    candidate = PoolCandidate(url=base_url)

    # 0. Measure latency
    import time as _time
    t0 = _time.monotonic()

    # 1. Get recruitment needs
    try:
        req = urllib.request.Request(
            f"{base_url}/v1/recruitment/needs",
            headers={"User-Agent": "iamine-worker-discovery/1.0"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode())
        candidate.latency_ms = (_time.monotonic() - t0) * 1000
        candidate.reachable = True
        candidate.name = data.get("pool_name", "unknown")
        candidate.worker_count = data.get("worker_count", 0)
        candidate.gaps = data.get("gaps", [])
    except Exception as e:
        log.debug(f"discovery: {base_url} unreachable: {e}")
        return candidate

    # 2. Try to discover more pools via federation
    try:
        req2 = urllib.request.Request(
            f"{base_url}/v1/federation/pools",
            headers={"User-Agent": "iamine-worker-discovery/1.0"},
        )
        resp2 = urllib.request.urlopen(req2, timeout=timeout)
        fed_data = json.loads(resp2.read().decode())
        # Peer URLs are hidden in public endpoint, but we get peer names
        # Future: peers could expose their public URL for discovery
    except Exception:
        pass

    # 3. Determine WebSocket URL
    if base_url.startswith("https://"):
        candidate.ws_url = base_url.replace("https://", "wss://") + "/ws"
    else:
        candidate.ws_url = base_url.replace("http://", "ws://") + "/ws"

    return candidate


def _score_placement(candidate: PoolCandidate, worker_model: str, worker_tps: float) -> float:
    """Score how useful this worker would be on a given pool.

    Higher score = worker fills bigger gaps here.
    """
    if not candidate.reachable:
        return -1.0

    score = 0.0
    model_lower = worker_model.lower()

    for gap in candidate.gaps:
        gap_kind = gap.get("kind", "")
        gap_class = gap.get("model_class", "")
        priority = gap.get("priority", "low")
        base_score = PRIORITY_SCORE.get(priority, 0)

        # Match worker capabilities to gap
        match = False

        if gap_kind == "llm.tool-call" and gap_class == "proxy-agent":
            # Worker can fill this if it runs a coder/instruct model
            if any(kw in model_lower for kw in ("coder", "instruct", "tool")):
                match = True

        elif gap_kind == "llm.reasoning" and "30b" in gap_class:
            # Worker can fill this if running 30B+ model
            import re
            m = re.search(r'(\d+\.?\d*)b', model_lower)
            if m and float(m.group(1)) >= 25:
                match = True

        elif gap_kind == "llm.chat" and gap_class == "fast-responder":
            # Worker can fill this if bench_tps >= 30
            if worker_tps >= 30:
                match = True

        if match:
            score += base_score

    # Bonus: prefer pools with fewer workers (distribute the load)
    if candidate.worker_count > 0:
        score += max(0, 10 - candidate.worker_count)  # small bonus for small pools
    else:
        score += 15  # empty pool gets extra bonus

    # Latency penalty: prefer closer pools (lower latency = higher score)
    # Every 50ms of latency costs 5 points
    if candidate.latency_ms < 9999:
        latency_penalty = (candidate.latency_ms / 50) * 5
        score = max(score - latency_penalty, 0.1)

    # Local pool affinity: strongly prefer the pool running on the same machine
    # A worker should stay on its local pool (zero network latency, same machine)
    ws = candidate.ws_url or ''
    if '127.0.0.1' in ws or 'localhost' in ws or '://[::1]' in ws:
        score += 50  # massive bonus — local pool always wins unless truly broken

    # Baseline: even if no gaps, pool is reachable = small score
    if score < 1.0 and candidate.reachable:
        score = max(score, 1.0)

    return score


def discover_best_pool(worker_model: str = "", worker_tps: float = 0.0) -> str:
    """Discover pools and return the WebSocket URL of the best placement.

    Returns the WS URL of the pool where this worker fills the biggest gap.
    Falls back to cellule.ai if no pools are reachable or no gaps found.
    """
    seeds = get_seed_pools()
    log.info(f"discovery: probing {len(seeds)} seed pool(s)...")

    # Expand seeds with federated peers from /v1/federation/discover
    expanded_seeds = list(seeds)
    for seed in seeds:
        try:
            req = urllib.request.Request(
                f"{seed.rstrip('/')}/v1/federation/discover",
                headers={"User-Agent": "iamine-worker-discovery/1.0"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            for p in data.get("pools", []):
                peer_url = p.get("url", "")
                if peer_url and peer_url not in expanded_seeds:
                    expanded_seeds.append(peer_url)
                    log.info(f"discovery: found federated peer {p.get('name','')} at {peer_url}")
        except Exception as e:
            log.debug(f"discovery: /v1/federation/discover failed on {seed}: {e}")

    candidates = []
    for seed in expanded_seeds:
        candidate = _probe_pool(seed)
        if candidate.reachable:
            candidate.score = _score_placement(candidate, worker_model, worker_tps)
            candidates.append(candidate)
            log.info(
                f"discovery: {candidate.name} ({candidate.url}) — "
                f"{candidate.worker_count} workers, {len(candidate.gaps)} gaps, "
                f"score={candidate.score:.1f} latency={candidate.latency_ms:.0f}ms"
            )

    if not candidates:
        log.warning("discovery: no reachable pools, falling back to cellule.ai")
        return "wss://cellule.ai/ws"

    # Sort by score descending, then by worker_count ascending (prefer smaller pools on tie)
    candidates.sort(key=lambda c: (-c.score, c.worker_count))
    best = candidates[0]

    log.info(f"discovery: selected {best.name} ({best.ws_url}) — score={best.score:.1f}")
    return best.ws_url
