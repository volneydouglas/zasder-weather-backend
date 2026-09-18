"""The aiosqlite worker thread and a loop that is already gone (2.3,
2026-09-14). A job queued on a loop that closes before the thread hands
its result back used to kill the thread with "Event loop is closed": the
CI-only PytestUnhandledThreadExceptionWarning sites, attributed to
whatever test happened to be running when the thread got there. pytest
now promotes that warning to an error, so this test fails the old way
if the tolerant thread regresses."""
import asyncio
import sqlite3
import threading
import time


def test_worker_thread_ends_quietly_when_its_loop_is_gone(temp_env, tmp_path):
    from app import db  # after the env fixture: Settings reads at import
    path = str(tmp_path / "t.sqlite")
    holder = {}

    async def open_and_abandon():
        conn = db.open_connection(path)
        conn.start()
        loop = asyncio.get_running_loop()
        # A slow job whose result lands after asyncio.run has closed the
        # loop: the exact shape of a connect cancelled at shutdown.
        fut = loop.create_future()
        conn._tx.put_nowait((fut, lambda: time.sleep(0.3) or sqlite3.connect(path)))
        holder["conn"] = conn
        # Return without awaiting it. asyncio.run closes the loop now.

    asyncio.run(open_and_abandon())
    thread: threading.Thread = holder["conn"]
    thread.join(timeout=5)
    assert not thread.is_alive(), "the thread must end, not wait forever"
    # Any exception in the thread would have reached pytest's thread
    # hook and, promoted to an error, failed this test.


def test_open_connection_is_the_only_opener(temp_env):
    """Every raw connection goes through db.open_connection, so the
    tolerant thread covers the backups and the self-updater too."""
    import pathlib
    from app import db
    app_dir = pathlib.Path(db.__file__).parent
    hits = [p.name for p in app_dir.glob("*.py")
            if "aiosqlite.connect(" in p.read_text()]
    assert hits == [], hits


def test_a_normal_connection_still_round_trips(temp_env, tmp_path):
    from app import db
    async def run():
        async with db.open_connection(str(tmp_path / "n.sqlite")) as conn:
            await conn.execute("CREATE TABLE t (v INTEGER)")
            await conn.execute("INSERT INTO t VALUES (7)")
            row = await (await conn.execute("SELECT v FROM t")).fetchone()
            return row[0]
    assert asyncio.run(run()) == 7


def test_an_established_connection_is_closed_when_its_loop_dies_mid_query(temp_env, tmp_path):
    """R23: the first fix closed a connection the thread had just OPENED
    for a loop that was gone; an already-established one running a query
    for that loop was left open until the collector found it."""
    from app import db
    path = str(tmp_path / "e.sqlite")
    holder = {}

    async def open_then_abandon():
        conn = db.open_connection(path)
        await conn                      # established: _connection is set
        assert conn._connection is not None
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        conn._tx.put_nowait((fut, lambda: time.sleep(0.3) or 1))
        holder["conn"] = conn

    asyncio.run(open_then_abandon())
    conn = holder["conn"]
    conn.join(timeout=5)
    assert not conn.is_alive()
    assert conn._connection is None, "the established handle must be closed and nulled"
