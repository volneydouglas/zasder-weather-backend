"""Two production bugs from Volney's Fly box, 2026-09-01.

1. GET /api/storage hung past Fly's 60 s proxy timeout on a 2.07 GB
   database: dbstat's default per-cell mode decodes every cell of every
   page. The breakdown now asks dbstat for its aggregated mode first and
   runs the whole scan under a wall-clock budget, answering `partial: true`
   instead of never. Second attempt, same night: aggregate mode walks a
   whole b-tree inside ONE xNext call, so the progress handler never fires
   during the observations walk and the route STILL exceeded 60 s live.
   The measurement is now a background job with its last report cached in
   server_kv; the route answers "ready" from the cache, "measuring" while
   the job runs, or "error" once.

2. Orphaned `.dbbackup-*.db` snapshots filled the volume (three of them,
   5.3 GB, beside a 2.07 GB database on 10.5 GB): the file was deleted
   only after a SUCCESSFUL download. A sweep now runs at boot, hourly,
   and before every new backup job.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

H = {"Authorization": "Bearer test-api-token"}


# ── fixtures ────────────────────────────────────────────────────────────

def _make_db(path: str, rows: int = 50) -> None:
    """A minimal database with the two tables storage_breakdown reads by
    name, plus an index so index_bytes has something to attribute."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE observations (mac TEXT, dateutc_ms INTEGER, "
                "data_json TEXT)")
    con.execute("CREATE INDEX obs_mac ON observations (mac)")
    con.execute("CREATE TABLE server_kv (k TEXT PRIMARY KEY, v TEXT)")
    now = int(time.time() * 1000)
    con.executemany(
        "INSERT INTO observations VALUES (?, ?, ?)",
        [("AA:BB", now - i * 60_000, '{"tempf": %d}' % i) for i in range(rows)])
    con.commit()
    con.close()


class _SpyConnection(sqlite3.Connection):
    """Records every SQL string executed; optionally refuses some."""
    executed: list[str] = []
    refuse_containing: tuple[str, ...] = ()
    on_execute = None                      # optional callback(sql)

    def execute(self, sql, *args, **kw):  # noqa: ANN001
        type(self).executed.append(sql)
        hook = type(self).__dict__.get("on_execute")   # unbound: no self
        if hook is not None:
            hook(sql)
        for needle in self.refuse_containing:
            if needle in sql:
                raise sqlite3.OperationalError(f"no such column: {needle}")
        return super().execute(sql, *args, **kw)


@pytest.fixture
def spy(monkeypatch):
    """Route maintenance's sqlite3.connect through the spy connection."""
    from app import maintenance
    real_connect = sqlite3.connect
    _SpyConnection.executed = []
    _SpyConnection.refuse_containing = ()
    _SpyConnection.on_execute = None

    def connect(*a, **kw):
        kw["factory"] = _SpyConnection
        return real_connect(*a, **kw)

    monkeypatch.setattr(maintenance.sqlite3, "connect", connect)
    return _SpyConnection


# ── Bug 1: /api/storage ─────────────────────────────────────────────────

def test_storage_breakdown_uses_aggregated_dbstat(temp_env, tmp_path, spy):
    """The fast path: dbstat's aggregated mode (page headers only) is the
    FIRST query, sizes come back, and the per-cell scan never runs."""
    from app import maintenance
    db = str(tmp_path / "w.db")
    _make_db(db)
    out = maintenance.storage_breakdown(db, detail_days=0)
    assert out["partial"] is False
    assert out["dbstat_mode"] == "aggregate"
    dbstat_sql = [s for s in spy.executed if "dbstat" in s]
    assert dbstat_sql == [maintenance._DBSTAT_AGGREGATE_SQL]
    assert "aggregate = TRUE" in dbstat_sql[0]
    tables = {t["table"]: t for t in out["tables"]}
    assert tables["observations"]["rows"] == 50
    assert tables["observations"]["bytes"] > 0
    assert tables["observations"]["index_bytes"] > 0      # obs_mac
    assert out["observations"]["rows"] == 50
    assert out["observations"]["per_station"] == [{"mac": "AA:BB", "rows": 50}]
    assert out["thinning"]["enabled"] is False


def test_storage_breakdown_falls_back_to_per_cell_dbstat(temp_env, tmp_path, spy):
    """SQLite < 3.32 has no `aggregate` column: the old per-cell query
    still answers, and the report says which mode measured."""
    from app import maintenance
    db = str(tmp_path / "w.db")
    _make_db(db)
    spy.refuse_containing = ("aggregate",)
    out = maintenance.storage_breakdown(db, detail_days=0)
    assert out["partial"] is False
    assert out["dbstat_mode"] == "cell"
    dbstat_sql = [s for s in spy.executed if "dbstat" in s]
    assert dbstat_sql == [maintenance._DBSTAT_AGGREGATE_SQL,
                          maintenance._DBSTAT_CELL_SQL]
    assert {t["table"]: t for t in out["tables"]}["observations"]["bytes"] > 0


def test_storage_breakdown_without_dbstat_has_null_sizes(temp_env, tmp_path, spy):
    """A build without the dbstat vtab: sizes null, counts still there."""
    from app import maintenance
    db = str(tmp_path / "w.db")
    _make_db(db)
    spy.refuse_containing = ("dbstat",)
    out = maintenance.storage_breakdown(db, detail_days=0)
    assert out["partial"] is False
    assert out["dbstat_mode"] is None
    t = {t["table"]: t for t in out["tables"]}["observations"]
    assert t["rows"] == 50 and t["bytes"] is None and t["index_bytes"] is None
    assert out["observations"]["rows"] == 50


def test_storage_breakdown_budget_abort_answers_partial(temp_env, tmp_path, monkeypatch):
    """The progress handler fires on the first VM step with a zero budget:
    every statement is interrupted, nothing raises, and the report is
    well-formed with `partial: true`, null sizes, and null counts."""
    from app import maintenance
    db = str(tmp_path / "w.db")
    _make_db(db)
    monkeypatch.setattr(maintenance, "_STORAGE_PROGRESS_EVERY", 1)
    out = maintenance.storage_breakdown(db, detail_days=0, budget_s=0)
    assert out["partial"] is True
    assert out["db_bytes"] > 0                    # stat() is not under sqlite
    for key in ("page_size", "freelist_bytes", "dbstat_mode", "tables",
                "observations", "thinning"):
        assert key in out
    assert out["observations"] is None
    assert out["dbstat_mode"] is None
    for t in out["tables"]:
        assert t["bytes"] is None and t["rows"] is None


def test_storage_breakdown_budget_abort_mid_scan_keeps_what_it_measured(
        temp_env, tmp_path, monkeypatch, spy):
    """Budget runs out AFTER the cheap stages: page size, the table list,
    thinning state, and dbstat sizes survive; the per-table COUNT loop
    (inside the budget too) and the observations split are cut."""
    from app import maintenance
    db = str(tmp_path / "w.db")
    _make_db(db, rows=2000)
    monkeypatch.setattr(maintenance, "_STORAGE_PROGRESS_EVERY", 1)
    start = time.monotonic()
    leap = {"on": False}

    def clock() -> float:
        # Time stands still through the pragmas, sqlite_master, the kv
        # read and aggregated dbstat; the first COUNT(*) statement is
        # where the wall clock leaps past any budget.
        return start + (10_000 if leap["on"] else 0)

    def on_execute(sql: str) -> None:
        if sql.startswith("SELECT COUNT(*) FROM"):
            leap["on"] = True
    spy.on_execute = on_execute
    monkeypatch.setattr(maintenance.time, "monotonic", clock)
    out = maintenance.storage_breakdown(db, detail_days=0, budget_s=25)
    assert out["partial"] is True
    assert out["page_size"] > 0
    assert out["thinning"]["enabled"] is False
    assert out["dbstat_mode"] == "aggregate"
    tables = {t["table"]: t for t in out["tables"]}
    assert set(tables) == {"observations", "server_kv"}
    assert tables["observations"]["bytes"] > 0
    assert tables["observations"]["rows"] is None
    assert out["observations"] is None


def test_storage_breakdown_interrupt_flag_is_the_only_error_swallowed(
        temp_env, tmp_path, spy):
    """A genuine sqlite error outside the dbstat probes still propagates:
    the budget path must not turn every failure into `partial`."""
    from app import maintenance
    db = str(tmp_path / "w.db")
    _make_db(db)
    spy.refuse_containing = ("FROM observations",)
    with pytest.raises(sqlite3.OperationalError):
        maintenance.storage_breakdown(db, detail_days=0)


def test_storage_cache_round_trips_and_rejects_garbage(temp_env, tmp_path):
    """put stamps measured_ms and get returns the same dict; a missing
    row, a hand-edited non-JSON row, a JSON row without measured_ms, and
    a database with no server_kv table at all read as "no cache"."""
    from app import maintenance
    db = str(tmp_path / "w.db")
    _make_db(db)
    assert maintenance.storage_cache_get(db) is None
    report = maintenance.storage_breakdown(db, detail_days=0)
    stamped = maintenance.storage_cache_put(report, db, now_ms=1234)
    assert stamped is report and report["measured_ms"] == 1234
    back = maintenance.storage_cache_get(db)
    assert back == report
    con = sqlite3.connect(db)
    for bad in ("not json", '{"db_bytes": 1}', '[1, 2]', ""):
        con.execute("UPDATE server_kv SET v = ? WHERE k = ?",
                    (bad, maintenance._STORAGE_CACHE_KEY))
        con.commit()
        assert maintenance.storage_cache_get(db) is None, bad
    con.close()
    bare = str(tmp_path / "bare.db")
    sqlite3.connect(bare).close()
    assert maintenance.storage_cache_get(bare) is None


# The app decodes these (WeatherAPI.swift StorageBreakdown); db_bytes is
# a REQUIRED Int64 there, so every state must carry it.
_APP_KEYS = ("db_bytes", "wal_bytes", "observations", "tables")


def _wait_for(client, state: str) -> dict:
    body = None
    for _ in range(100):
        body = client.get("/api/storage", headers=H).json()
        if body["state"] == state:
            return body
        time.sleep(0.05)
    raise AssertionError(f"never reached {state!r}; last {body}")


def _job_counter(monkeypatch, gate=None, boom: Exception | None = None):
    """Wrap the real storage_breakdown so tests can count starts, hold a
    job open (gate: a threading.Event the scan waits on) or fail it."""
    from app import maintenance
    real = maintenance.storage_breakdown
    calls: list[int] = []

    def counting(*a, **kw):
        calls.append(1)
        if gate is not None:
            gate.wait(10)
        if boom is not None:
            raise boom
        return real(*a, **kw)
    monkeypatch.setattr(maintenance, "storage_breakdown", counting)
    return calls


def _wait_calls(calls: list[int], n: int, timeout: float = 5.0) -> bool:
    """The job runs on a worker thread the route only *schedules*; on CI's
    slower runners the GET returns before the thread has started, so an
    immediate `calls == [1]` raced (2026-09-02, PR #35). Wait for it."""
    deadline = time.time() + timeout
    while len(calls) < n and time.time() < deadline:
        time.sleep(0.02)
    return len(calls) == n


def test_api_storage_no_cache_measures_then_serves_the_cached_report(
        client, monkeypatch):
    """First GET: 'measuring' at once (no wait for the scan) with the file
    sizes and every app key present; the job lands in the kv cache; the
    next GET is 'ready' with measured_ms and the full report — and does
    NOT start another job."""
    from app import main as M, maintenance
    calls = _job_counter(monkeypatch)
    r = client.get("/api/storage", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "measuring" and body["started_ms"] > 0
    for k in _APP_KEYS:
        assert k in body, k
    assert isinstance(body["db_bytes"], int) and body["observations"] is None
    assert "measured_ms" not in body            # nothing measured yet
    assert M._STORAGE_TASK is not None and M._STORAGE_JOB["state"] == "running"

    ready = _wait_for(client, "ready")
    assert ready["partial"] is False and ready["dbstat_mode"] == "aggregate"
    assert isinstance(ready["measured_ms"], int)
    assert ready["observations"]["rows"] >= 0 and ready["tables"]
    assert maintenance.storage_cache_get()["measured_ms"] == ready["measured_ms"]
    assert client.get("/api/storage", headers=H).json()["state"] == "ready"
    assert _wait_calls(calls, 1), calls


def test_api_storage_fresh_cache_answers_without_a_job(client, monkeypatch):
    """A cached report younger than the fresh window is the answer; the
    scan never runs."""
    from app import main as M, maintenance
    seed = maintenance.storage_breakdown(None, detail_days=0)
    calls = _job_counter(monkeypatch)
    now_ms = int(time.time() * 1000)
    maintenance.storage_cache_put(seed, now_ms=now_ms - 1000)
    body = client.get("/api/storage", headers=H).json()
    assert body["state"] == "ready" and body["measured_ms"] == now_ms - 1000
    assert body["db_bytes"] == seed["db_bytes"]
    assert calls == [] and M._STORAGE_TASK is None


def test_api_storage_stale_cache_serves_old_report_while_measuring(
        client, monkeypatch):
    """Past the fresh window: 'measuring' with the OLD report (its old
    measured_ms) so the app has numbers to show, and the job starts."""
    from app import main as M, maintenance
    seed = maintenance.storage_breakdown(None, detail_days=0)
    gate = threading.Event()
    calls = _job_counter(monkeypatch, gate=gate)
    old_ms = int(time.time() * 1000) - M._STORAGE_FRESH_MS - 1
    maintenance.storage_cache_put(seed, now_ms=old_ms)
    body = client.get("/api/storage", headers=H).json()
    assert body["state"] == "measuring"
    assert body["measured_ms"] == old_ms
    assert body["observations"] == seed["observations"]
    assert _wait_calls(calls, 1), calls
    gate.set()
    ready = _wait_for(client, "ready")
    assert ready["measured_ms"] > old_ms


def test_api_storage_second_get_while_running_starts_no_second_job(
        client, monkeypatch):
    """Polling (and ?refresh=1 mid-run) reports 'measuring' with the same
    started_ms; exactly one scan runs."""
    from app import main as M
    gate = threading.Event()
    calls = _job_counter(monkeypatch, gate=gate)
    first = client.get("/api/storage", headers=H).json()
    assert first["state"] == "measuring"
    task = M._STORAGE_TASK
    for url in ("/api/storage", "/api/storage?refresh=1", "/api/storage"):
        again = client.get(url, headers=H).json()
        assert again["state"] == "measuring"
        assert again["started_ms"] == first["started_ms"]
    assert M._STORAGE_TASK is task
    assert _wait_calls(calls, 1), calls
    gate.set()
    _wait_for(client, "ready")
    assert _wait_calls(calls, 1), calls


def test_api_storage_failed_job_reports_error_once_then_retries(
        client, monkeypatch):
    """The scan's exception lands in the job, is answered as 'error' once
    (with the file sizes, so the decoder still has db_bytes), and the
    GET after that starts a fresh job."""
    from app import main as M
    calls = _job_counter(monkeypatch, boom=RuntimeError("disk on fire"))
    assert client.get("/api/storage", headers=H).json()["state"] == "measuring"
    err = _wait_for(client, "error")
    assert err["error"] == "disk on fire" and isinstance(err["db_bytes"], int)
    assert M._STORAGE_JOB == {"state": "idle"} and M._STORAGE_TASK is None
    assert client.get("/api/storage", headers=H).json()["state"] == "measuring"
    assert _wait_calls(calls, 2), calls
    _wait_for(client, "error")                  # drain the second job


def test_api_storage_refresh_forces_a_new_measurement(client, monkeypatch):
    """?refresh=1 with a fresh cache: 'measuring' carrying the cached
    report, job started — and the poller (no flag) keeps seeing
    'measuring' until the NEW report lands, even though the old cache is
    still inside the fresh window. When it lands the cache is newer."""
    from app import maintenance
    seed = maintenance.storage_breakdown(None, detail_days=0)
    calls = _job_counter(monkeypatch)
    now_ms = int(time.time() * 1000)
    maintenance.storage_cache_put(seed, now_ms=now_ms - 5000)
    assert client.get("/api/storage", headers=H).json()["state"] == "ready"
    body = client.get("/api/storage?refresh=1", headers=H).json()
    assert body["state"] == "measuring" and body["measured_ms"] == now_ms - 5000
    assert _wait_calls(calls, 1), calls
    ready = _wait_for(client, "ready")
    assert ready["measured_ms"] >= now_ms
    assert _wait_calls(calls, 1), calls


def test_api_storage_dead_running_task_reads_as_error_once(client):
    """state='running' with a finished task (cancelled at shutdown) is
    reported as an interrupted measurement once, then the next GET
    starts over — the backup job's trust rule."""
    from app import main as M

    async def done() -> None:
        return None
    M._STORAGE_JOB = {"state": "running", "started_ms": 1}
    M._STORAGE_TASK = asyncio.run(_spawn_done(done))
    err = client.get("/api/storage", headers=H).json()
    assert err["state"] == "error"
    assert err["error"] == "previous measurement was interrupted"
    assert client.get("/api/storage", headers=H).json()["state"] == "measuring"
    _wait_for(client, "ready")


# ── Bug 2: orphaned snapshots ───────────────────────────────────────────

def _old(p: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(p, (t, t))


def test_sweep_deletes_old_snapshots_and_keeps_the_running_one(
        temp_env, tmp_path, monkeypatch):
    """Three orphans (Aug 23, Aug 24, Sep 1 on the real box) plus a stray
    journal go; the running job's file — and its journal — stay; the
    tempdir is swept as well; bytes freed are reported."""
    from app import maintenance
    dbdir = tmp_path / "data"
    dbdir.mkdir()
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(maintenance.tempfile, "gettempdir", lambda: str(tmpdir))
    db = dbdir / "weather.db"
    db.write_bytes(b"x")

    orphans = []
    for name, hours, size in ((".dbbackup-aug23.db", 9 * 24, 3000),
                              (".dbbackup-aug24.db", 8 * 24, 2000),
                              (".dbbackup-sep01.db", 2, 1000)):
        p = dbdir / name
        p.write_bytes(b"o" * size)
        _old(p, hours)
        orphans.append(p)
    journal = dbdir / ".dbbackup-aug23.db-journal"
    journal.write_bytes(b"j" * 100)
    _old(journal, 9 * 24)
    in_tmp = tmpdir / ".dbbackup-tmpone.db"
    in_tmp.write_bytes(b"t" * 500)
    _old(in_tmp, 3)
    running = dbdir / ".dbbackup-running.db"
    running.write_bytes(b"r" * 700)
    _old(running, 5)                       # older than fresh, still kept
    running_journal = dbdir / ".dbbackup-running.db-journal"
    running_journal.write_bytes(b"rj")
    _old(running_journal, 5)
    other = dbdir / "weather.db-wal"       # not a snapshot; untouched
    other.write_bytes(b"w")

    now_ms = int(time.time() * 1000)
    res = maintenance.sweep_stale_snapshots(
        now_ms, running, max_age_ms=10 * 60_000, db_path=str(db))

    for p in orphans + [journal, in_tmp]:
        assert not p.exists(), p
    assert running.exists() and running_journal.exists()
    assert other.exists() and db.exists()
    assert res["bytes_freed"] == 3000 + 2000 + 1000 + 100 + 500
    assert len(res["deleted"]) == 5
    assert str(in_tmp) in res["deleted"]


def test_sweep_age_guard_spares_fresh_untracked_files_until_boot(
        temp_env, tmp_path, monkeypatch):
    """The inline GET path VACUUMs into an untracked file whose mtime stays
    fresh while it is written: the hourly/start sweeps (fresh-window
    guard) leave it; the boot sweep (any age) takes it."""
    from app import maintenance
    monkeypatch.setattr(maintenance.tempfile, "gettempdir", lambda: str(tmp_path))
    db = tmp_path / "weather.db"
    db.write_bytes(b"x")
    fresh = tmp_path / ".dbbackup-fresh.db"
    fresh.write_bytes(b"f" * 10)
    now_ms = int(time.time() * 1000)
    res = maintenance.sweep_stale_snapshots(
        now_ms, None, max_age_ms=10 * 60_000, db_path=str(db))
    assert fresh.exists() and res == {"deleted": [], "bytes_freed": 0}
    res = maintenance.sweep_stale_snapshots(now_ms, None, db_path=str(db))
    assert not fresh.exists()
    assert res["bytes_freed"] == 10 and res["deleted"] == [str(fresh)]


def test_boot_sweep_runs_in_lifespan(temp_env, monkeypatch):
    """Boot calls the sweep with keep=None and no age limit, and a
    days-old orphan next to the database is gone once the app is up."""
    db_path = Path(temp_env)
    orphan = db_path.parent / ".dbbackup-boot.db"
    orphan.write_bytes(b"o" * 42)
    _old(orphan, 24)
    fresh_orphan = db_path.parent / ".dbbackup-boot-fresh.db"
    fresh_orphan.write_bytes(b"o" * 7)      # any age at boot: goes too

    # Same reload dance as the `client` fixture, then hook the sweep
    # AFTER the reload (the fixture's reload would drop an earlier patch).
    for mod in ["app.config", "app.maintenance", "app.db", "app.insights",
                "app.wu_upload", "app.capture", "app.ingest", "app.meter",
                "app.discovery", "app.alerts", "app.apns", "app.relay",
                "app.integrations", "app.main"]:
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import maintenance
    from app.main import app
    from fastapi.testclient import TestClient
    calls: list[tuple] = []
    real = maintenance.sweep_stale_snapshots

    def recorder(now_ms, keep=None, **kw):
        calls.append((keep, kw))
        return real(now_ms, keep, **kw)
    monkeypatch.setattr(maintenance, "sweep_stale_snapshots", recorder)

    with TestClient(app):
        assert calls and calls[0] == (None, {"max_age_ms": None})
        assert not orphan.exists()
        assert not fresh_orphan.exists()


def test_start_endpoint_sweeps_before_creating_a_new_job(client, monkeypatch):
    """POST /api/backup/database removes past-window orphans before the
    free-space check — the 5.3 GB of leftovers were exactly what made
    the next backup refuse with 507."""
    import time as _t
    from app import main as M
    dbdir = Path(M.settings.database_path).parent
    orphan = dbdir / ".dbbackup-stale.db"
    orphan.write_bytes(b"o" * 99)
    _old(orphan, 2)
    calls: list[str] = []
    real = M._sweep_orphan_snapshots

    def recorder(**kw):
        calls.append("sweep")
        return real(**kw)
    monkeypatch.setattr(M, "_sweep_orphan_snapshots", recorder)
    r = client.post("/api/backup/database", headers=H)
    assert r.status_code == 200 and r.json()["state"] in ("running", "ready")
    assert calls == ["sweep"]
    assert not orphan.exists()
    # The new job's own file is the one thing the sweep keeps.
    state = None
    for _ in range(50):
        state = client.get("/api/backup/database/status", headers=H).json()
        if state["state"] == "ready":
            break
        _t.sleep(0.1)
    assert state and state["state"] == "ready"
    job_file = Path(M._DB_BACKUP_JOB["path"])
    assert job_file.exists()
    # A ready-and-fresh job survives the periodic sweep too (the real
    # function: the recorder only counts the endpoint's call).
    swept = real()
    assert swept["deleted"] == [] and job_file.exists()
    # Second POST reuses it (no second sweep, no second VACUUM).
    assert client.post("/api/backup/database", headers=H).json()["state"] == "ready"
    assert calls == ["sweep"]
    client.get("/api/backup/database", headers=H)      # download clears it


def test_status_reports_expired_and_sweep_may_take_the_file(client):
    """A ready snapshot older than the fresh window is 'expired' on
    /status, is not the periodic sweep's keep path, and the next POST
    starts over rather than handing out the stale file."""
    from app import main as M
    dbdir = Path(M.settings.database_path).parent
    stale = dbdir / ".dbbackup-expired.db"
    stale.write_bytes(b"s" * 12)
    _old(stale, 1)
    now_ms = int(time.time() * 1000)
    M._DB_BACKUP_JOB = {"state": "ready", "path": str(stale), "size": 12,
                        "finished_ms": now_ms - M._DB_BACKUP_FRESH_MS - 1}
    assert client.get("/api/backup/database/status",
                      headers=H).json() == {"state": "expired"}
    assert M._db_backup_keep_path(now_ms) is None
    swept = M._sweep_orphan_snapshots()
    assert swept["deleted"] == [str(stale)] and not stale.exists()

    # Fresh ready job: still 'ready', still kept.
    fresh = dbdir / ".dbbackup-fresh.db"
    fresh.write_bytes(b"f" * 3)
    M._DB_BACKUP_JOB = {"state": "ready", "path": str(fresh), "size": 3,
                        "finished_ms": now_ms}
    assert client.get("/api/backup/database/status",
                      headers=H).json() == {"state": "ready", "size": 3}
    assert M._db_backup_keep_path(now_ms) == fresh
    assert M._sweep_orphan_snapshots()["deleted"] == [] and fresh.exists()
    fresh.unlink()


def test_keep_path_ignores_a_dead_running_task(client):
    """state='running' with a finished task (cancelled at shutdown) keeps
    nothing — the same trust rule the start endpoint applies."""
    from app import main as M

    async def done() -> None:
        return None
    task = asyncio.run(_spawn_done(done))
    M._DB_BACKUP_JOB = {"state": "running", "path": "/nowhere/.dbbackup-x.db"}
    M._DB_BACKUP_TASK = task
    assert M._db_backup_keep_path(int(time.time() * 1000)) is None


async def _spawn_done(coro_fn):
    t = asyncio.ensure_future(coro_fn())
    await t
    return t



def test_an_expired_snapshot_is_not_downloadable(client):
    """R18 finding 8: status said 'expired' and the next POST refused to
    reuse it, but a direct GET still served the stale file. Now it is
    removed and the caller is told to start a new one."""
    import time
    import app.main as M
    from app.config import settings
    dbdir = Path(settings.database_path).parent
    stale = dbdir / ".dbbackup-expired-get.db"
    stale.write_bytes(b"s" * 12)
    now_ms = int(time.time() * 1000)
    M._DB_BACKUP_JOB = {"state": "ready", "path": str(stale), "size": 12,
                        "finished_ms": now_ms - M._DB_BACKUP_FRESH_MS - 1}
    r = client.get("/api/backup/database", headers=H)
    assert r.status_code == 410, r.text
    assert not stale.exists()
    assert M._DB_BACKUP_JOB == {"state": "idle"}
