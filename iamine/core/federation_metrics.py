"""M8+ — Federation in-memory metrics counters.

Lightweight observability layer for the federation backplane. Counters are
in-memory only (reset on restart) — that's fine for diagnostic, not for
authoritative accounting. Authoritative = revenue_ledger + federation_settlements.

Exposed via GET /v1/federation/debug/enforcement (admin only).
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Dict


_lock = Lock()
_counters: Dict[str, int] = {}
_started_at = time.time()


def inc(name: str, value: int = 1) -> None:
    """Increment a counter. Safe to call from any coroutine."""
    with _lock:
        _counters[name] = _counters.get(name, 0) + value


def get_all() -> dict:
    """Return a snapshot of all counters + uptime."""
    with _lock:
        snap = dict(_counters)
    snap["_metrics_uptime_sec"] = int(time.time() - _started_at)
    return snap


def reset() -> None:
    """Reset all counters (diagnostic only, admin-triggered)."""
    global _started_at
    with _lock:
        _counters.clear()
    _started_at = time.time()


# Convenience helpers with canonical names

def handshake_ok():
    inc("handshake_total{result=ok}")


def handshake_fail(reason: str = "unknown"):
    inc(f"handshake_total{{result=fail,reason={reason}}}")


def forward_attempt():
    inc("forward_jobs_total{phase=attempt}")


def forward_ok():
    inc("forward_jobs_total{phase=ok}")


def forward_fail(reason: str = "unknown"):
    inc(f"forward_jobs_total{{phase=fail,reason={reason}}}")


def signature_reject(path: str):
    # path without /v1/federation/ prefix for brevity
    short = path.replace("/v1/federation/", "") or "root"
    inc(f"signature_reject_total{{path={short}}}")


def killswitch_reject(path: str):
    short = path.replace("/v1/federation/", "") or "root"
    inc(f"killswitch_reject_total{{path={short}}}")


def heartbeat_tick_ok():
    inc("heartbeat_ticks_total{result=ok}")


def heartbeat_tick_fail():
    inc("heartbeat_ticks_total{result=fail}")


def settlement_proposed():
    inc("settlement_proposed_total")
