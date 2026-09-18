"""The 2026-09-16 deep review's restore findings (REC-01, REC-02),
converted from its defect probes into pins of the DESIRED behaviour:
every step after the first rename is inside the rollback boundary, the
database does not reopen over an empty live name, and a cancelled
restore holds the maintenance lease until its swap thread has finished
and undone its work.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.test_restore import _seed, _snapshot_of, _rows, H


@pytest.fixture(autouse=True)
def _fresh_restore_state(temp_env):
    from app import restore
    restore.JOB.clear()
    restore.JOB["state"] = "idle"
    restore._CHALLENGE = None
    restore._TASK = None
    yield
    restore.JOB.clear()
    restore.JOB["state"] = "idle"
    restore._CHALLENGE = None
    restore._TASK = None


def _pre_copies(live: Path) -> list[Path]:
    from app import restore
    return sorted(live.parent.glob(f"{live.name}{restore.PRE_RESTORE_SUFFIX}-*.db"))


# ───────────────────────── REC-01: the rollback boundary ─────────────────────

def test_a_sidecar_failure_after_the_first_rename_keeps_the_old_database_live(client, temp_env, monkeypatch):
    """The live file had been renamed away and the WAL/SHM unlink sat
    OUTSIDE the try that rolls the second rename back: an error there
    reached the job's handler with the live name empty and the old rows
    only under the pre-restore name, and the gate reopened over nothing."""
    from app import db, restore
    _seed(client, 3)
    live = Path(temp_env)
    upload = live.parent / "candidate.db"
    _snapshot_of(temp_env, upload)
    real = restore._sidecars

    def fail_while_absent(path):
        # The same fault the review injected: every sidecar unlink beside
        # an empty live name fails, the rollback's included.
        if path == live and not live.exists():
            raise PermissionError("injected sidecar unlink failure after the first rename")
        real(path)
    monkeypatch.setattr(restore, "_sidecars", fail_while_absent)
    asyncio.run(restore._validate_and_swap(upload, live))
    st = restore.status()
    assert st["state"] == "error" and "sidecar" in st["error"], st
    assert db.is_open()
    assert live.exists(), "the old database is back under its own name"
    assert _rows(temp_env) == 3
    assert not _pre_copies(live) and not restore._marker(live).exists()
    assert client.get("/api/devices", headers=H).status_code == 200


def test_a_first_rename_failure_leaves_no_marker_behind(tmp_path, monkeypatch):
    """The marker is written before the first rename; when that rename
    fails nothing moved, and a marker beside the untouched database would
    only make the next boot report an interruption that never happened."""
    from app import restore
    live, upload = tmp_path / "weather.db", tmp_path / "upload.db"
    for p in (live, upload):
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE t (x)")
        con.commit()
        con.close()
    real = restore.os.replace

    def fail_first(src, dst):
        if Path(src) == live:
            raise PermissionError("injected first-rename failure")
        return real(src, dst)
    monkeypatch.setattr(restore.os, "replace", fail_first)
    with pytest.raises(PermissionError):
        restore.swap_in(upload, live, stamp="t")
    assert live.exists() and upload.exists()
    assert not restore._marker(live).exists()


def test_the_database_stays_closed_when_the_rollback_itself_fails(client, temp_env, monkeypatch):
    """Fail-closed: the second rename fails AND the rollback cannot put
    the old file back. Reopening the gate would let the next connection
    create an empty database under the live name. It stays closed with
    the reason in every refusal, and boot recovery is what reinstalls
    the pre-restore copy."""
    from app import db, restore
    _seed(client, 3)
    live = Path(temp_env)
    upload = live.parent / "candidate.db"
    _snapshot_of(temp_env, upload)
    real_replace = restore.os.replace

    def fail_second(src, dst):
        if Path(src) == upload:
            raise OSError("injected second-rename failure")
        return real_replace(src, dst)
    monkeypatch.setattr(restore.os, "replace", fail_second)

    def rollback_fails(live_, pre):
        raise OSError("injected rollback failure")
    monkeypatch.setattr(restore, "roll_back", rollback_fails)
    asyncio.run(restore._validate_and_swap(upload, live))
    st = restore.status()
    assert st["state"] == "error" and "closed until the server restarts" in st["error"], st
    assert not live.exists() and len(_pre_copies(live)) == 1
    assert not db.is_open(), "the gate must not reopen over an empty live name"
    assert db.held_reason() == restore.HELD_REASON

    async def refused():
        with pytest.raises(db.DatabaseUnavailable, match="closed until the server restarts"):
            async with db.connect():
                pass
    asyncio.run(refused())
    # The route answers 503, never an empty database.
    assert client.get("/api/devices", headers=H).status_code == 503
    # What the next boot does: the marker names the pre-restore copy.
    note = restore.recover_at_boot(live)
    assert note and "back in place" in note
    db.release_hold()
    assert db.is_open() and _rows(temp_env) == 3
    assert client.get("/api/devices", headers=H).status_code == 200


# ───────────────────────── REC-02: cancellation and the lease ────────────────

def test_a_cancelled_restore_holds_the_lease_until_the_swap_thread_is_done(client, temp_env, monkeypatch):
    """Cancelling the task that awaits the swap thread never stopped the
    thread; it only stopped the wait, so the maintenance lease exited and
    the database reopened while the renames were still happening. The
    cancelled task now waits the thread out, rolls a completed swap back,
    and only then lets the lease go."""
    from app import db, restore
    _seed(client, 3)
    live = Path(temp_env)
    upload = live.parent / "candidate.db"
    _snapshot_of(temp_env, upload)
    with sqlite3.connect(upload) as con:
        con.execute("DELETE FROM observations")
        con.commit()
    entered, finish, exited = threading.Event(), threading.Event(), threading.Event()
    real = restore.swap_in

    def held_swap(*args):
        entered.set()
        try:
            if not finish.wait(5):
                raise RuntimeError("probe timed out")
            return real(*args)
        finally:
            exited.set()
    monkeypatch.setattr(restore, "swap_in", held_swap)

    async def scenario():
        job = asyncio.create_task(restore._validate_and_swap(upload, live))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not db.is_open()
            job.cancel()
            await asyncio.sleep(0.1)
            assert not job.done(), "the cancelled task is waiting for its thread"
            assert not exited.is_set()
            assert not db.is_open(), "the lease is held while the swap thread owns the files"
            # A second cancel while it waits changes nothing.
            job.cancel()
            await asyncio.sleep(0.05)
            assert not db.is_open()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert exited.is_set()
        assert db.is_open()
    asyncio.run(scenario())
    # The swap completed on the thread and was rolled back: the OLD rows
    # are live, the candidate is gone, nothing half-done is left behind.
    assert live.exists() and _rows(temp_env) == 3
    assert not _pre_copies(live) and not restore._marker(live).exists()
    assert not db._CHART_INDEX_BUILDING
    assert client.get("/api/devices", headers=H).status_code == 200
