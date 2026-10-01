"""The Postgres connection wrapper hands pooled connections back, exactly once.

No database needed: a fake pool records what happened. The point is the
contract api.py relies on — every route calls close() in a finally, some call
it twice — so a pooled connection must go back to the pool (not be closed, which
would drain the pool one request at a time) and a second close must be a no-op
(a double putconn raises in psycopg_pool).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402


class _FakeCon:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakePool:
    def __init__(self):
        self.out, self.returned = [], []

    def getconn(self):
        c = _FakeCon()
        self.out.append(c)
        return c

    def putconn(self, c):
        self.returned.append(c)


def _wrapper(pool, monkeypatch):
    # skip the catalog query a real first connection makes
    monkeypatch.setattr(db._PgConnection, "_id_tables", set())
    return db._PgConnection(pool=pool)


def test_close_returns_the_connection_to_the_pool(monkeypatch):
    pool = _FakePool()
    w = _wrapper(pool, monkeypatch)
    w.close()
    assert pool.returned == pool.out
    assert not pool.out[0].closed, "a pooled connection must be returned, not closed"


def test_double_close_returns_it_once(monkeypatch):
    pool = _FakePool()
    w = _wrapper(pool, monkeypatch)
    w.close()
    w.close()
    assert len(pool.returned) == 1


class _Cursor:
    description = None

    def execute(self, *a):
        pass


class _DeadCon(_FakeCon):
    """What a connection the network dropped while idle looks like."""

    def cursor(self):
        import psycopg
        raise psycopg.OperationalError("consuming input failed: SSL error: unexpected eof while reading")


class _LiveCon(_FakeCon):
    def cursor(self):
        return _Cursor()


class _StalePool(_FakePool):
    def __init__(self):
        super().__init__()
        self._queue = [_DeadCon(), _LiveCon()]

    def getconn(self):
        c = self._queue.pop(0)
        self.out.append(c)
        return c


def test_a_dropped_connection_on_the_first_statement_is_replaced(monkeypatch):
    pool = _StalePool()
    w = _wrapper(pool, monkeypatch)
    w.execute("SELECT 1")                      # must not raise
    assert isinstance(pool.returned[0], _DeadCon), "the dead connection goes back to be discarded"
    w.close()
    assert isinstance(pool.returned[-1], _LiveCon)


def test_a_failure_after_work_has_started_is_never_retried(monkeypatch):
    import psycopg
    pool = _FakePool()
    monkeypatch.setattr(db._PgConnection, "_id_tables", set())
    first = _LiveCon()
    pool.getconn = lambda: (pool.out.append(first), first)[1]
    w = db._PgConnection(pool=pool)
    w.execute("SELECT 1")
    first.cursor = _DeadCon().cursor           # dies mid-transaction
    try:
        w.execute("UPDATE leads SET stage='won'")
        raise AssertionError("a mid-transaction failure was silently retried")
    except psycopg.OperationalError:
        pass


def test_pool_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("EXCEEDBOX_DB_POOL", "0")
    assert db._pool_enabled() is False
    monkeypatch.setenv("EXCEEDBOX_DB_POOL", "1")
    assert db._pool_enabled() is True


def test_sqlite_runs_never_build_a_pool():
    assert db.DIALECT == "sqlite"
    con = db.connect()
    con.close()
    assert db._pool is None
