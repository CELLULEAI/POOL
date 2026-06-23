"""Tests RGPD — purge complete des couches memoire a la suppression de compte.

Verrouille le helper PostgresStore.delete_user_memory_tiers (audit 2026-06-22,
items #2/#3) : il doit purger les 6 couches memoire et ignorer proprement une
table absente (pool en retard de migration) sans la supprimer ni planter.

DB-free : on injecte une fausse connexion ; aucun PostgreSQL requis.
"""
import pytest

from iamine.db import PostgresStore

ALL_TIERS = {
    "agent_observations",
    "agent_episodes",
    "agent_procedures",
    "memory_relationships",
    "memory_consolidation_log",
    "user_memories",
}


class FakeConn:
    """Connexion asyncpg simulee : to_regclass + DELETE comptabilises."""

    def __init__(self, existing):
        self.existing = set(existing)
        self.deletes = []

    async def fetchval(self, sql, table):
        # SELECT to_regclass($1) -> nom si la table existe, sinon None
        assert "to_regclass" in sql
        return table if table in self.existing else None

    async def execute(self, sql, *args):
        if sql.strip().upper().startswith("DELETE"):
            self.deletes.append(sql)
            return "DELETE 3"
        return "OK"


@pytest.mark.asyncio
async def test_purge_covers_all_six_tiers():
    store = PostgresStore.__new__(PostgresStore)  # pas de __init__ (pas de pool reel)
    conn = FakeConn(existing=ALL_TIERS)

    deleted = await store.delete_user_memory_tiers("acc_x", conn=conn)

    # Les 6 couches sont rapportees...
    assert set(deleted) == ALL_TIERS
    # ...et chacune a bien recu un DELETE filtre par token_hash.
    assert len(conn.deletes) == 6
    for table in ALL_TIERS:
        assert any(f"FROM {table} WHERE token_hash" in d for d in conn.deletes), table
    assert all(deleted[t] == 3 for t in ALL_TIERS)


@pytest.mark.asyncio
async def test_purge_skips_missing_table_without_delete():
    # Pool en retard de migration : seule user_memories existe.
    store = PostgresStore.__new__(PostgresStore)
    conn = FakeConn(existing={"user_memories"})

    deleted = await store.delete_user_memory_tiers("acc_x", conn=conn)

    assert deleted["user_memories"] == 3
    # Les tables absentes sont rapportees a 0 et JAMAIS supprimees (pas de DELETE).
    for absent in ALL_TIERS - {"user_memories"}:
        assert deleted[absent] == 0
    assert len(conn.deletes) == 1
    assert "FROM user_memories WHERE token_hash" in conn.deletes[0]
