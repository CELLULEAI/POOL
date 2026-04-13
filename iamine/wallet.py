"""Wallet local $IAMINE — stocke les credits API du worker."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("iamine.wallet")

WALLET_FILE = "wallet.json"


class Wallet:
    """Wallet local pour stocker les credits $IAMINE.

    1 requete servie = 1 credit gagne
    1 requete API utilisee = 1 credit depense
    """

    def __init__(self, path: str = WALLET_FILE):
        self.path = Path(path)
        self.data = {
            "worker_id": "",
            "api_token": "",
            "credits": 0.0,
            "total_earned": 0.0,
            "total_spent": 0.0,
            "jobs_served": 0,
            "requests_made": 0,
            "created": "",
            "last_sync": "",
        }
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    saved = json.load(f)
                self.data.update(saved)
            except Exception:
                pass

    def save(self):
        self.data["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def init(self, worker_id: str, api_token: str):
        """Initialise le wallet avec les infos du worker."""
        self.data["worker_id"] = worker_id
        self.data["api_token"] = api_token
        if not self.data["created"]:
            self.data["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        log.info(f"Wallet init — {worker_id} — token: {api_token[:16]}...")

    def earn(self, amount: float = 1.0):
        """Credite le wallet apres avoir servi une requete."""
        self.data["credits"] += amount
        self.data["total_earned"] += amount
        self.data["jobs_served"] += 1
        self.save()

    def spend(self, amount: float = 1.0) -> bool:
        """Depense des credits pour utiliser l'API. Retourne False si solde insuffisant."""
        if self.data["credits"] < amount:
            return False
        self.data["credits"] -= amount
        self.data["total_spent"] += amount
        self.data["requests_made"] += 1
        self.save()
        return True

    @property
    def credits(self) -> float:
        return self.data["credits"]

    @property
    def api_token(self) -> str:
        return self.data.get("api_token", "")

    @property
    def worker_id(self) -> str:
        return self.data.get("worker_id", "")

    def status(self) -> dict:
        return {
            "worker_id": self.data["worker_id"],
            "credits": round(self.data["credits"], 2),
            "total_earned": round(self.data["total_earned"], 2),
            "total_spent": round(self.data["total_spent"], 2),
            "jobs_served": self.data["jobs_served"],
            "requests_made": self.data["requests_made"],
            "api_token": self.data.get("api_token", "")[:16] + "...",
        }

    def print_status(self):
        s = self.data
        print(f" * WALLET      {s['credits']:.1f} $IAMINE (earned={s['total_earned']:.1f} spent={s['total_spent']:.1f})")
        print(f" * SERVED      {s['jobs_served']} jobs | USED {s['requests_made']} requests")
        if s.get("api_token"):
            print(f" * API TOKEN   {s['api_token'][:20]}...")
