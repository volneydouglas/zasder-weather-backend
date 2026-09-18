"""Database restore from an uploaded snapshot (2.1, app/restore.py).

The other half of the snapshot download: a self-hoster puts a saved
database back on a fresh box from the app. Each test pins one promise:
the two-step gate (write token, one-shot challenge, upload digest), the
refusals (corrupt file, newer schema, busy server), the swap (rows come
from the upload, the old file survives as the pre-restore copy), and the
boot sweep of an abandoned upload.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

import pytest

H = {"Authorization": "Bearer test-api-token"}
RO = {"Authorization": "Bearer test-reviewer-token"}
MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def _fresh_restore_state(temp_env):
    """app.restore is not in conftest's per-test reload list; its job and
    challenge are module state, so reset them here. Depends on temp_env so
    the settings import below sees the test environment."""
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


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _snapshot_of(db_path: str, dest: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()


def _seed(client, n: int, start_ms: int = 1_700_000_000_000) -> None:
    from app import db
    import asyncio
    rows = [{"dateutc": start_ms + i * 60_000, "tempf": 70.0 + i}
            for i in range(n)]
    asyncio.run(db.insert_observations(MAC, rows))


def _rows(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    finally:
        con.close()


def _wait_done(client, timeout_s: float = 20.0) -> dict:
    deadline = time.time() + timeout_s
    st: dict = {"state": "never polled"}
    while time.time() < deadline:
        st = client.get("/api/backup/database/restore/status", headers=H).json()
        if st["state"] in ("done", "error", "idle"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"restore never finished: {st}")


def _upload(client, body: bytes, challenge: str | None, sha: str | None = None,
            headers: dict | None = None):
    h = dict(headers or H)
    if challenge is not None:
        h["X-Restore-Challenge"] = challenge
    h["X-Restore-SHA256"] = sha if sha is not None else _sha(body)
    h["Content-Type"] = "application/vnd.sqlite3"
    return client.post("/api/backup/database/restore", content=body, headers=h)


# ───────────────────────── the gate ─────────────────────────

def test_challenge_and_upload_need_the_write_token(client):
    assert client.post("/api/backup/database/restore/challenge").status_code == 401
    assert client.post("/api/backup/database/restore/challenge",
                       headers=RO).status_code == 403
    r = _upload(client, b"x" * 100, "whatever", headers=RO)
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]


def test_challenge_is_one_shot_and_expires():
    from app import restore
    c = restore.issue_challenge(now_ms=1000)
    assert c["expires_ms"] == 1000 + restore.CHALLENGE_TTL_MS
    assert restore.consume_challenge("nope", now_ms=1500) is False
    # A wrong guess burned it: the right answer no longer works.
    assert restore.consume_challenge(c["challenge"], now_ms=1500) is False
    c = restore.issue_challenge(now_ms=1000)
    assert restore.consume_challenge(c["challenge"],
                                     now_ms=1000 + restore.CHALLENGE_TTL_MS + 1) is False
    c = restore.issue_challenge(now_ms=1000)
    assert restore.consume_challenge(c["challenge"], now_ms=2000) is True
    assert restore.consume_challenge(c["challenge"], now_ms=2000) is False


def test_upload_without_a_fresh_challenge_is_refused_before_reading(client, temp_env):
    _seed(client, 3)
    body = b"SQLite format 3\x00" + b"\0" * 100
    r = _upload(client, body, None)
    assert r.status_code == 403 and "challenge" in r.json()["detail"]
    r = _upload(client, body, "stale-or-guessed")
    assert r.status_code == 403
    assert _rows(temp_env) == 3
    assert not list(Path(temp_env).parent.glob(".dbrestore-*"))


def test_challenge_reports_what_is_about_to_be_replaced(client):
    _seed(client, 7)
    r = client.post("/api/backup/database/restore/challenge", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["current_rows"] == 7
    assert len(body["challenge"]) >= 32


def test_digest_mismatch_is_refused_and_nothing_is_kept(client, temp_env):
    _seed(client, 2)
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    r = _upload(client, b"SQLite format 3\x00" + b"\0" * 4096, c["challenge"],
                sha="0" * 64)
    assert r.status_code == 400 and "SHA-256" in r.json()["detail"]
    assert not list(Path(temp_env).parent.glob(".dbrestore-*"))
    assert client.get("/api/backup/database/restore/status",
                      headers=H).json()["state"] == "error"


# ───────────────────────── refusals ─────────────────────────

def test_a_file_that_is_not_a_database_is_refused(client, temp_env):
    _seed(client, 2)
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    r = _upload(client, b"{\"not\": \"sqlite\"}", c["challenge"])
    assert r.status_code == 200 and r.json()["state"] == "validating"
    st = _wait_done(client)
    assert st["state"] == "error" and "not a SQLite database" in st["error"]
    assert _rows(temp_env) == 2
    assert not list(Path(temp_env).parent.glob(".dbrestore-*"))


def test_a_damaged_database_is_refused(client, temp_env, tmp_path):
    _seed(client, 2)
    snap = tmp_path / "snap.db"
    _snapshot_of(temp_env, snap)
    data = bytearray(snap.read_bytes())
    # Scribble over the middle of the file, past the header. Whole PAGES,
    # not 512 bytes: a fixed little window lands wherever the schema
    # happens to put it, and adding one column to daily_rollups (2.3,
    # yearly_rise) moved it onto bytes SQLite did not mind — so the test
    # went green while checking nothing. Four pages from a page boundary
    # is certain to hit a b-tree.
    page = int.from_bytes(data[16:18], "big") or 4096
    start = ((len(data) // 2) // page) * page
    for i in range(start, min(len(data), start + page * 4)):
        data[i] = 0xFF
    # And PROVE the file is damaged before asking the server to notice.
    # Without this the assertions below pass for a corrupted file and for
    # a perfectly good one that was never really corrupted.
    probe = tmp_path / "probe.db"
    probe.write_bytes(bytes(data))
    con = sqlite3.connect(probe)
    try:
        # A badly damaged file makes the PRAGMA itself raise rather than
        # return a verdict; either way it is not "ok".
        try:
            verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError:
            verdict = "raised"
        assert verdict != "ok", \
            "the scribble missed: this test would pass without checking anything"
    finally:
        con.close()
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert _upload(client, bytes(data), c["challenge"]).status_code == 200
    st = _wait_done(client)
    assert st["state"] == "error"
    assert "integrity" in st["error"] or "damaged" in st["error"]
    assert _rows(temp_env) == 2


def test_a_backup_from_a_newer_release_is_refused(client, temp_env, tmp_path):
    _seed(client, 2)
    snap = tmp_path / "snap.db"
    _snapshot_of(temp_env, snap)
    con = sqlite3.connect(snap)
    con.execute("ALTER TABLE observations ADD COLUMN from_the_future REAL")
    con.commit()
    con.close()
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert _upload(client, snap.read_bytes(), c["challenge"]).status_code == 200
    st = _wait_done(client)
    assert st["state"] == "error" and "NEWER release" in st["error"]
    assert "from_the_future" in st["error"]
    assert _rows(temp_env) == 2


def test_a_snapshot_missing_the_core_tables_is_refused(client, temp_env, tmp_path):
    _seed(client, 1)
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE notes (x TEXT)")
    con.commit()
    con.close()
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert _upload(client, other.read_bytes(), c["challenge"]).status_code == 200
    st = _wait_done(client)
    assert st["state"] == "error" and "observations" in st["error"]
    assert _rows(temp_env) == 1


def test_refused_while_a_rebuild_is_in_flight(client, monkeypatch):
    from app import insights
    _seed(client, 1)
    monkeypatch.setattr(insights, "_PROGRESS", {
        "phase": "fold", "mac": MAC, "rows": 1, "cursor_ms": 1,
        "batch_started": 0.0})
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    r = _upload(client, b"x", c["challenge"])
    assert r.status_code == 409 and "rebuild" in r.json()["detail"]
    # A busy server must not burn the challenge: it is still good.
    monkeypatch.setattr(insights, "_PROGRESS", None)
    from app import restore
    assert restore.consume_challenge(c["challenge"]) is True


def test_refused_while_a_backup_job_is_running(client):
    from app import main as M
    _seed(client, 1)
    import asyncio

    async def never():
        await asyncio.sleep(3600)
    # Simulate a live backup job by hand: the state plus an unfinished task.
    loop = asyncio.new_event_loop()
    task = loop.create_task(never())
    M._DB_BACKUP_JOB = {"state": "running", "path": "/nope"}
    M._DB_BACKUP_TASK = task
    try:
        c = client.post("/api/backup/database/restore/challenge", headers=H).json()
        r = _upload(client, b"x", c["challenge"])
        assert r.status_code == 409 and "backup" in r.json()["detail"]
    finally:
        task.cancel()
        try:
            loop.run_until_complete(task)
        except BaseException:
            pass
        loop.close()
        M._DB_BACKUP_JOB = {"state": "idle"}
        M._DB_BACKUP_TASK = None


def test_no_room_on_the_volume_is_a_507_up_front(client, monkeypatch, temp_env):
    import shutil
    from app import restore
    _seed(client, 1)
    live = Path(temp_env)
    real = shutil.disk_usage
    monkeypatch.setattr(restore.shutil, "disk_usage",
                        lambda p: real(p)._replace(free=1024))
    with pytest.raises(Exception) as ei:
        restore.upload_dest(live, 10 * 2**20)
    assert getattr(ei.value, "status_code", None) == 507


# ───────────────────────── the swap ─────────────────────────

def test_restore_swaps_in_the_upload_and_keeps_the_old_database(client, temp_env):
    from app import restore
    _seed(client, 5)
    snap = Path(temp_env).parent / "saved.db"
    _snapshot_of(temp_env, snap)
    # Diverge: the live database grows after the backup was taken.
    _seed(client, 4, start_ms=1_800_000_000_000)
    assert _rows(temp_env) == 9
    body = snap.read_bytes()
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert c["current_rows"] == 9
    r = _upload(client, body, c["challenge"])
    assert r.status_code == 200, r.text
    assert r.json() == {"state": "validating", "received_bytes": len(body)}
    st = _wait_done(client)
    assert st["state"] == "done", st
    assert st["rows"] == 5
    assert _rows(temp_env) == 5
    pre = Path(temp_env).parent / st["snapshot"]
    assert pre.exists() and st["snapshot"].startswith(
        Path(temp_env).name + restore.PRE_RESTORE_SUFFIX)
    assert _rows(str(pre)) == 9, "the old database must survive as the pre-restore copy"
    assert not list(Path(temp_env).parent.glob(".dbrestore-*"))
    # The server keeps serving, from the restored file: the newest reading
    # is the fifth seeded row, not the ninth.
    cur = client.get(f"/api/devices/{MAC}/current", headers=H)
    assert cur.status_code == 200, cur.text
    assert cur.json()["tempf"] == 74.0


def test_only_the_newest_pre_restore_copy_is_kept(client, temp_env, monkeypatch):
    from app import restore
    _seed(client, 3)
    snap = Path(temp_env).parent / "saved.db"
    _snapshot_of(temp_env, snap)
    body = snap.read_bytes()
    # The pre-restore stamp has one-second resolution; hand each swap its
    # own stamp instead of sleeping through the second.
    real_swap, n = restore.swap_in, {"i": 0}

    def stamped_swap(upload, live, stamp=None):
        n["i"] += 1
        return real_swap(upload, live, stamp=f"20260905-00000{n['i']}")
    monkeypatch.setattr(restore, "swap_in", stamped_swap)
    for i in range(2):
        c = client.post("/api/backup/database/restore/challenge", headers=H).json()
        assert _upload(client, body, c["challenge"]).status_code == 200
        st = _wait_done(client)
        assert st["state"] == "done", st
    pres = list(Path(temp_env).parent.glob(Path(temp_env).name + ".pre-restore-*.db"))
    assert len(pres) == 1


def test_the_body_limit_middleware_exempts_exactly_the_restore_path(client, temp_env):
    """A years-deep archive is hundreds of MB; the 1 MiB global cap must
    not truncate the restore upload, and must still bound everything
    else."""
    from app import limits
    assert limits.EXEMPT_PATHS == frozenset({"/api/backup/database/restore"})
    _seed(client, 2)
    snap = Path(temp_env).parent / "saved.db"
    _snapshot_of(temp_env, snap)
    big = snap.read_bytes()
    # Pad the file past the cap with a big freelist-free blob: append rows
    # instead, so the file stays a valid database.
    con = sqlite3.connect(snap)
    con.execute("CREATE TABLE padding (blob BLOB)")
    con.executemany("INSERT INTO padding VALUES (?)",
                    [(os.urandom(64 * 1024),) for _ in range(24)])
    con.commit()
    con.close()
    big = snap.read_bytes()
    assert len(big) > limits._DEFAULT_MAX
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    r = _upload(client, big, c["challenge"])
    assert r.status_code == 200, r.text
    assert r.json()["received_bytes"] == len(big)
    # 'padding' is a table this server does not know: refused as newer,
    # which proves the whole body arrived and was read.
    st = _wait_done(client)
    assert st["state"] == "error" and "padding" in st["error"]
    # And a big body elsewhere is still capped.
    r = client.post("/api/config/restore", headers=H,
                    content=b"{" + b" " * (limits._DEFAULT_MAX + 10) + b"}")
    assert r.status_code in (413, 422, 400)


def test_boot_sweeps_an_abandoned_upload(client, temp_env):
    from app import restore
    junk = Path(temp_env).parent / ".dbrestore-deadbeef.db"
    junk.write_bytes(b"half an upload")
    assert restore.sweep_leftovers(Path(temp_env)) == 1
    assert not junk.exists()


# ───────────── 2.1 pre-release review, §7 test gaps ─────────────

def test_swap_refuses_when_the_checkpoint_is_busy(tmp_path, monkeypatch):
    """wal_checkpoint(TRUNCATE) answers busy=1 instead of raising when a
    reader blocks it; renaming the live file then would strand committed
    WAL frames outside the pre-restore copy. The swap must refuse and leave
    both files where they were — after a few tries a second apart, since a
    widget fetch is a reader for milliseconds (round-three review BE-F10)."""
    from app import restore
    live, upload = tmp_path / "weather.db", tmp_path / "upload.db"
    live.write_bytes(b"live"); upload.write_bytes(b"upload")
    monkeypatch.setattr(restore, "CHECKPOINT_RETRY_S", 0.0)
    tries = []
    monkeypatch.setattr(restore, "_checkpoint", lambda _p: (tries.append(1) or (1, 7, 3)))
    with pytest.raises(restore.CheckpointBusy, match="checkpoint"):
        restore.swap_in(upload, live, stamp="t")
    assert len(tries) == restore.CHECKPOINT_ATTEMPTS
    assert live.read_bytes() == b"live" and upload.read_bytes() == b"upload"
    assert not list(tmp_path.glob("*pre-restore*"))
    # A reader that lets go on the second try: the swap goes ahead.
    answers = iter([(1, 7, 3), (0, 0, 0)])
    monkeypatch.setattr(restore, "_checkpoint", lambda _p: next(answers))
    pre = restore.swap_in(upload, live, stamp="t2")
    assert live.read_bytes() == b"upload" and pre.read_bytes() == b"live"


def test_a_checkpoint_refusal_keeps_the_upload(tmp_path, monkeypatch):
    """BE-F10: one transient reader used to cost the whole uploaded
    database. The refused upload stays beside the database (the boot
    sweep clears it if nobody retries) and the job says why."""
    import asyncio
    from app import restore
    live, upload = tmp_path / "weather.db", tmp_path / "upload.db"
    live.write_bytes(b"live"); upload.write_bytes(b"upload")
    monkeypatch.setattr(restore, "validate_snapshot", lambda u, l: {"rows": 1, "tables": 1})

    async def prepared(_u):
        return None
    monkeypatch.setattr(restore, "prepare_candidate", prepared)
    monkeypatch.setattr(restore, "CHECKPOINT_RETRY_S", 0.0)
    monkeypatch.setattr(restore, "_checkpoint", lambda _p: (1, 7, 3))
    restore.JOB.clear()
    asyncio.run(restore._validate_and_swap(upload, live))
    assert restore.JOB["state"] == "error" and "kept" in restore.JOB["error"]
    assert upload.exists() and live.read_bytes() == b"live"


def test_a_failed_swap_leaves_the_old_database_serving_and_nothing_behind(client, temp_env, monkeypatch):
    """T3: the swap raises after validation; the live file is untouched,
    the server still answers from it, the job says error, and no
    .dbrestore-* upload is left on the volume."""
    from app import restore
    _seed(client, 5)
    snap = Path(temp_env).parent / "saved.db"
    _snapshot_of(temp_env, snap)
    body = snap.read_bytes()

    def explode(upload, live, stamp=None):
        raise OSError("volume went read-only")
    monkeypatch.setattr(restore, "swap_in", explode)
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert _upload(client, body, c["challenge"]).status_code == 200
    st = _wait_done(client)
    assert st["state"] == "error" and "read-only" in st["error"]
    assert _rows(temp_env) == 5
    assert not list(Path(temp_env).parent.glob(".dbrestore-*"))
    assert not list(Path(temp_env).parent.glob(Path(temp_env).name + ".pre-restore-*"))
    assert client.get(f"/api/devices/{MAC}/current", headers=H).status_code == 200


def test_an_aborted_upload_leaves_nothing_on_the_volume(client, temp_env):
    """T4: the body stops short of its declared length (a dropped
    connection); the partial file is deleted, the job says error."""
    import asyncio
    from app import restore
    _seed(client, 2)
    live = Path(temp_env)
    dest = restore.upload_dest(live, 1000)

    async def dropped():
        yield b"SQLite format 3\x00" + b"\0" * 100
        raise ConnectionResetError("client went away")

    async def run():
        try:
            await restore.receive_upload(dropped(), dest, expected_sha256="0" * 64,
                                         expected_bytes=1000, free_at_start=10**9)
        except ConnectionResetError:
            return "dropped"
        return "finished"
    assert asyncio.run(run()) == "dropped"
    assert not dest.exists()
    assert not list(live.parent.glob(".dbrestore-*"))
    assert _rows(temp_env) == 2


def test_an_oversize_declaration_is_refused_before_a_byte_is_written(client, temp_env, monkeypatch):
    """T4, the other half: a Content-Length the volume cannot hold is a
    507 up front and nothing lands beside the database."""
    import shutil
    from app import restore
    _seed(client, 2)
    monkeypatch.setattr(restore.shutil, "disk_usage",
                        lambda p: shutil._ntuple_diskusage(10**9, 10**9 - 10**6, 10**6))
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    body = b"SQLite format 3\x00" + b"\0" * 100
    r = _upload(client, body, c["challenge"],
                headers={**H, "Content-Length": str(10**8)})
    assert r.status_code == 507, r.text
    assert not list(Path(temp_env).parent.glob(".dbrestore-*"))


def test_a_second_restore_is_refused_and_the_challenge_is_not_burned(client, temp_env, monkeypatch):
    """T8: while one restore validates, another upload is 409 and the
    challenge it carried is still good afterwards."""
    import threading
    from app import restore
    _seed(client, 3)
    snap = Path(temp_env).parent / "saved.db"
    _snapshot_of(temp_env, snap)
    body = snap.read_bytes()
    release = threading.Event()
    real_validate = restore.validate_snapshot

    def slow_validate(path, live=None):
        release.wait(5.0)
        return real_validate(path, live)
    monkeypatch.setattr(restore, "validate_snapshot", slow_validate)
    c1 = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert _upload(client, body, c1["challenge"]).status_code == 200
    try:
        c2 = client.post("/api/backup/database/restore/challenge", headers=H).json()
        r = _upload(client, body, c2["challenge"])
        assert r.status_code == 409 and "in progress" in r.json()["detail"]
        assert restore.consume_challenge(c2["challenge"]) is True, \
            "a busy refusal must not spend the challenge"
    finally:
        release.set()
    st = _wait_done(client)
    assert st["state"] == "done", st
