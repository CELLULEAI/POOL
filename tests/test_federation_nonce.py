"""Anti-replay federation — semantique atomique du nonce (audit 2026-06-22, TOCTOU).

Verrouille _record_nonce_atomic : 1re vue -> accepte, rejeu -> rejete, et permissif
si pas de store DB. DB-free : on simule une connexion asyncpg dont le fetchrow
reproduit INSERT ... ON CONFLICT DO NOTHING RETURNING 1.
"""
import pytest

from iamine.core import federation


class FakeConn:
    def __init__(self):
        self.seen = set()

    async def fetchrow(self, sql, atom_id, nonce):
        assert "ON CONFLICT" in sql and "RETURNING" in sql
        key = (atom_id, nonce)
        if key in self.seen:
            return None              # conflit -> rien insere -> replay
        self.seen.add(key)
        return {"?column?": 1}       # ligne inseree -> nouveau


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _PoolDB:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class FakePool:
    def __init__(self, conn):
        self.store = type("Store", (), {"pool": _PoolDB(conn)})()


@pytest.mark.asyncio
async def test_nonce_accept_then_reject_replay():
    pool = FakePool(FakeConn())
    assert await federation._record_nonce_atomic(pool, "atomA", "n1") is True
    # meme (atom, nonce) -> replay
    assert await federation._record_nonce_atomic(pool, "atomA", "n1") is False
    # nonce different -> accepte
    assert await federation._record_nonce_atomic(pool, "atomA", "n2") is True
    # meme nonce mais autre atom -> accepte (cle composite)
    assert await federation._record_nonce_atomic(pool, "atomB", "n1") is True


@pytest.mark.asyncio
async def test_nonce_no_db_is_permissive():
    no_db = type("P", (), {"store": type("S", (), {"pool": None})()})()
    assert await federation._record_nonce_atomic(no_db, "a", "n") is True
