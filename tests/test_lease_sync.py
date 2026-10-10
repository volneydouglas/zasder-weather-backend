"""B-02 (the 2.6 architecture review): the maintenance lease sees the
retention thread's connection, and a restore does not start under it.

Before: `maintenance._connect` opened the file on its own, invisible to
the lease's drain, so a restore could rename the database under a
writing thread (its remaining writes went into an unlinked WAL) and
`busy_reason` did not know the pass was running.
"""
from __future__ import annotations

import asyncio
import gc
import os
import threading

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")


def test_a_sync_connection_from_a_thread_holds_the_lease(client):
    from app import db, maintenance
    from app.config import settings
    opened = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def worker():
        conn = maintenance._connect(settings.database_path)   # the counted path
        conn.execute("SELECT 1")
        opened.set()
        release.wait(5)
        conn.close()
        closed.set()

    async def scenario():
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert opened.wait(5)
        assert db.active_connections() >= 1
        with pytest.raises(db.LeaseBusy):
            async with db.maintenance_lease(drain_timeout_s=0.3):
                pass
        assert db.is_open(), "a refused lease reopens the gate"
        release.set()
        assert closed.wait(5)
        t.join(5)
        async with db.maintenance_lease(drain_timeout_s=2.0):
            pass          # drains at once now
        return True
    assert asyncio.run(scenario())


def test_a_closed_gate_refuses_a_new_sync_connection(client):
    from app import db
    from app.config import settings
    base = db.active_connections()      # the running app may hold one
    db._GATE_CLOSED = True
    try:
        with pytest.raises(db.DatabaseUnavailable):
            db.sync_connection(settings.database_path)
        assert db.active_connections() == base
    finally:
        db._GATE_CLOSED = False


def test_a_counted_connection_dropped_without_close_still_releases(client, monkeypatch):
    """Counted on open, released once on close, and released by the
    collector for a path that forgot to close. Measured through the
    counter's own moves, not the shared total: the running app opens and
    closes connections of its own underneath a test (CI, PR #64)."""
    from app import db
    from app.config import settings
    moves = []
    real = db._count_connection
    me = threading.get_ident()      # the app's own connections move the counter too

    def recording(delta):
        if threading.get_ident() == me:
            moves.append(delta)
        real(delta)
    monkeypatch.setattr(db, "_count_connection", recording)
    conn = db.sync_connection(settings.database_path)
    # Admission counts under the lock, not through _count_connection, so
    # the open shows as the state, and the releases as moves.
    assert db.active_connections() >= 1 and moves == []
    del conn
    gc.collect()
    assert moves == [-1], "the collector released it"
    conn = db.sync_connection(settings.database_path)
    conn.close(); conn.close()
    assert moves == [-1, -1], "closing twice releases once"


def test_the_retention_pass_in_flight_is_a_reason_not_to_restore(client, monkeypatch):
    from app import maintenance, restore
    assert restore.busy_reason() is None
    monkeypatch.setattr(maintenance, "_IN_FLIGHT", "the nightly retention pass")
    assert "retention pass" in (restore.busy_reason() or "")


def test_run_thin_night_announces_itself_and_clears(client, monkeypatch):
    from app import maintenance
    seen = []

    def fake_pass(eff, db_path=None):
        seen.append(maintenance.in_flight())
        return {"ok": True}
    monkeypatch.setattr(maintenance, "_run_thin_night_pass", fake_pass)
    assert maintenance.run_thin_night({}) == {"ok": True}
    assert seen == ["the nightly retention pass"]
    assert maintenance.in_flight() is None


def _own_counter(monkeypatch):
    """The connection count is process-wide and the app's own background
    work opens and closes connections on the TestClient's thread while a
    test runs, so a before/after difference of the global flakes. Count
    only the moves made from this test's own thread."""
    import threading
    from app import db
    me, moves = threading.get_ident(), []
    real = db._count_connection

    def counting(delta):
        if threading.get_ident() == me:
            moves.append(delta)
        real(delta)
    monkeypatch.setattr(db, "_count_connection", counting)
    return lambda: sum(moves)


def test_a_cancel_while_a_counted_connection_opens_keeps_it_counted_until_closed(client, monkeypatch, tmp_path):
    """R26-05 (the 2.6 detailed review): cancelling the task while the
    worker was still opening SQLite released the count at once, the
    lease could then enter, and the worker opened its handle inside the
    exclusive window and stayed alive. The count now holds until the
    acquisition has finished AND its handle is closed, and the worker
    exits; a second cancel does not cut that short."""
    import sqlite3
    import threading
    from app import db
    gate, opened = threading.Event(), threading.Event()
    path = str(tmp_path / "copy.db")

    def slow_connector():
        gate.wait(5)
        conn = sqlite3.connect(path, check_same_thread=False)
        opened.set()
        return conn
    monkeypatch.setattr(db, "open_connection", lambda p, **k: db._Connection(slow_connector, 64))
    mine = _own_counter(monkeypatch)

    async def go():
        workers = []

        async def use():
            async with db.counted_open_connection(path) as c:
                await c.execute("select 1")
        t = asyncio.ensure_future(use())
        await asyncio.sleep(0.05)
        workers = [th for th in threading.enumerate() if isinstance(th, db._Connection)]
        t.cancel()
        await asyncio.sleep(0.05)
        t.cancel()                                   # twice
        await asyncio.sleep(0.05)
        held = mine()
        lease_entered = False
        try:
            async with db.maintenance_lease(drain_timeout_s=0.2):
                lease_entered = True
        except db.LeaseBusy:
            pass
        gate.set()                                   # the connector finishes
        try:
            await t
        except asyncio.CancelledError:
            pass
        for th in workers:
            th.join(2)
        return held, lease_entered, mine(), [th.is_alive() for th in workers]
    held, lease_entered, after, alive = asyncio.run(go())
    assert held == 1, "the count was released while SQLite was still opening"
    assert not lease_entered, "the lease entered while a worker could still open SQLite"
    assert opened.is_set() and after == 0
    assert not any(alive), "the worker thread outlived the cancelled acquisition"


def test_a_cancel_while_connect_opens_keeps_it_counted_until_closed(client, monkeypatch):
    """R26-05's twin: db.connect() counted, then awaited the worker, with
    the same hole. Same invariant: counted until the handle is closed."""
    import sqlite3
    import threading
    from app import db
    gate = threading.Event()
    real = db.open_connection

    def slow(path, **k):
        inner = real(path, **k)

        def connector():
            gate.wait(5)
            return sqlite3.connect(str(path), check_same_thread=False)
        return db._Connection(connector, 64) if inner else inner
    monkeypatch.setattr(db, "open_connection", slow)
    mine = _own_counter(monkeypatch)

    async def go():

        async def use():
            async with db.connect() as c:
                await c.execute("select 1")
        t = asyncio.ensure_future(use())
        await asyncio.sleep(0.05)
        workers = [th for th in threading.enumerate() if isinstance(th, db._Connection)]
        t.cancel()
        await asyncio.sleep(0.05)
        held = mine()
        gate.set()
        try:
            await t
        except asyncio.CancelledError:
            pass
        for th in workers:
            th.join(2)
        return held, mine(), [th.is_alive() for th in workers]
    held, after, alive = asyncio.run(go())
    assert held == 1 and after == 0
    assert not any(alive)
