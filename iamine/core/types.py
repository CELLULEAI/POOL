"""Dataclasses partagées par le pool et les routes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass
class ConnectedWorker:
    worker_id: str
    ws: WebSocket
    info: dict = field(default_factory=dict)
    busy: bool = False
    jobs_done: int = 0
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


@dataclass
class PendingJob:
    job_id: str
    messages: list[dict]
    max_tokens: int
    future: asyncio.Future
