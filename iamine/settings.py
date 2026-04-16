"""Configuration du pool — bascule entre MemoryStore et PostgresStore.

Pour migrer vers PostgreSQL :
1. pip install asyncpg
2. Definir DATABASE_URL (env ou ici)
3. Changer USE_POSTGRES = True
"""

import os

# --- Bascule MVP / Production ---
USE_POSTGRES = os.environ.get("IAMINE_DB") == "postgres"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:pass@localhost:5432/dbname")

# --- Pool settings ---
WELCOME_BONUS = float(os.environ.get("IAMINE_BONUS", "10.0"))
CREDIT_PER_JOB = float(os.environ.get("IAMINE_CREDIT", "1.0"))
API_COST_PER_REQUEST = float(os.environ.get("IAMINE_API_COST", "1.0"))
HEARTBEAT_INTERVAL = int(os.environ.get("IAMINE_HB_INTERVAL", "30"))
HEARTBEAT_TIMEOUT = int(os.environ.get("IAMINE_HB_TIMEOUT", "90"))


async def create_store():
    """Factory — retourne le bon Store selon la config."""
    if USE_POSTGRES:
        from .db import PostgresStore
        store = PostgresStore(DATABASE_URL)
        await store.connect()
        return store
    else:
        from .db import MemoryStore
        return MemoryStore()
