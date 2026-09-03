"""2026-09-01 production incident: the deferred chart-index rebuild held
SQLite's write lock for ~5 minutes; every ingest POST waited out
busy_timeout and 500'd "database is locked", and the push-relay challenge
500'd beside it. Four fixes, each pinned here:

  1. build-then-swap index rebuild + ingest write-behind + healer chaining
     + shutdown reaping + lock-aware retry;
  2. SQLite lock errors answer 503 + Retry-After instead of 500;
  3. a non-ASCII bearer/ingest/admin token is 401, never 500;
  4. migration completion keys for the lightning and storm-capture
     backfills (a kill mid-UPDATE no longer skips the fill forever).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
import time

import pytest

H_API = {"Authorization": "Bearer test-api-token"}
H_INGEST = {"X-Ingest-Token": "test-ingest-token"}


def _payload(ts: str = "2026-08-09T12:00:00Z", tempf: float = 70.0,
             mac: str = "AABBCCDDEE02") -> dict:
    return {"device": {"id": mac}, "timestamp_utc": ts,
            "outdoor": {"tempf": tempf, "humidity": 50},
            "wind": {}, "rain": {}, "pressure": {}, "source": "t"}


def _index_names(path: str) -> dict[str, str]:
    con = sqlite3.connect(path)
    try:
        return dict(con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='observations' AND name LIKE 'idx_obs_chart%'"))
    finally:
        con.close()


# ───────────────────────── 1a. build-then-swap ─────────────────────────

def test_chart_index_rebuild_is_build_then_swap(client, monkeypatch):
    """The old index must exist at EVERY point of the rebuild — before the
    CREATE, after the CREATE (both present), and only after the swap is
    it gone. The in-progress flag brackets the CREATE alone."""
    from app import db
    from app.config import settings
    path = settings.database_path
    new_name = db.chart_index_name()
    seen: dict[str, object] = {}
    real_create = db._create_chart_index

    async def observed_create(conn, name):
        seen["before"] = _index_names(path)
        seen["flag_during"] = db.chart_index_rebuild_in_progress()
        await real_create(conn, name)
        await conn.commit()
        seen["after_create"] = _index_names(path)
    monkeypatch.setattr(db, "_create_chart_index", observed_create)

    async def run():
        # An upgrading database: the pre-2.0 bare name, stale columns.
        async with db.connect() as conn:
            await conn.execute(f"DROP INDEX {new_name}")
            await conn.execute(
                "CREATE INDEX idx_obs_chart ON observations (mac, dateutc_ms)")
            await conn.commit()
        db._CHART_INDEX_REBUILD_NEEDED = True
        await db.rebuild_chart_index()
    asyncio.run(run())

    assert "idx_obs_chart" in seen["before"]
    assert new_name not in seen["before"]
    assert seen["flag_during"] is True
    # Post-CREATE, pre-DROP: both present — a kill here leaves the old one.
    assert "idx_obs_chart" in seen["after_create"]
    assert new_name in seen["after_create"]
    final = _index_names(path)
    assert set(final) == {new_name}, final
    assert set(db._CHART_INDEX_COLS) <= db._index_columns(final[new_name])
    assert db.chart_index_rebuild_in_progress() is False
    assert db.chart_index_rebuild_needed() is False


def test_chart_index_flag_clears_when_create_fails(client, monkeypatch):
    from app import db

    async def boom(conn, name):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "_create_chart_index", boom)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(db.rebuild_chart_index())
    assert db.chart_index_rebuild_in_progress() is False


def test_boot_probe_accepts_versioned_name_and_sweeps_leftover(client):
    """A run killed AFTER its CREATE leaves both names. The probe must
    recognise the versioned index as covering (no rebuild) and drop the
    leftover bare one; and a fresh DB carries only the versioned name."""
    from app import db
    from app.config import settings
    path = settings.database_path
    name = db.chart_index_name()
    assert set(_index_names(path)) == {name}

    async def run():
        async with db.connect() as conn:
            await conn.execute(
                "CREATE INDEX idx_obs_chart ON observations (mac, dateutc_ms)")
            await conn.commit()
        db._CHART_INDEX_REBUILD_NEEDED = False
        await db.init_db()
        return db.chart_index_rebuild_needed()
    needed = asyncio.run(run())
    assert needed is False, "a covering versioned index is not stale"
    assert set(_index_names(path)) == {name}


def test_boot_probe_inline_path_uses_versioned_name(client, monkeypatch):
    """Small archive, stale bare-name index: the inline rebuild at boot
    must produce the same versioned name the deferred path does, so the
    two never disagree about what 'current' is."""
    from app import db
    from app.config import settings
    path = settings.database_path
    name = db.chart_index_name()

    async def run():
        async with db.connect() as conn:
            await conn.execute(f"DROP INDEX {name}")
            await conn.execute(
                "CREATE INDEX idx_obs_chart ON observations (mac, dateutc_ms)")
            await conn.commit()
        await db.init_db()
    asyncio.run(run())
    final = _index_names(path)
    assert set(final) == {name}
    assert set(db._CHART_INDEX_COLS) <= db._index_columns(final[name])


# ───────────────────────── 1b. ingest write-behind ─────────────────────

def test_ingest_queues_while_index_builds_and_drains_after(client, monkeypatch):
    from app import db, ingest
    mac = "AA:BB:CC:DD:EE:02"
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", True)
    r1 = client.post("/ingest/custom", headers=H_INGEST,
                     json=_payload("2026-08-09T12:00:00Z", 70.0))
    r2 = client.post("/ingest/custom", headers=H_INGEST,
                     json=_payload("2026-08-09T12:05:00Z", 71.0))
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["queued"] is True and r1.json()["inserted"] == 0
    assert ingest.write_behind_depth() == 2
    assert asyncio.run(db.observation_count(mac)) == 0
    # Still building: a drain must NOT flush (would block on the lock).
    assert asyncio.run(ingest.drain_write_behind()) == 0
    assert ingest.write_behind_depth() == 2

    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", False)
    assert asyncio.run(ingest.drain_write_behind()) == 2
    assert ingest.write_behind_depth() == 0
    assert asyncio.run(db.observation_count(mac)) == 2
    # And the normal path is back: a post now stores directly.
    r3 = client.post("/ingest/custom", headers=H_INGEST,
                     json=_payload("2026-08-09T12:10:00Z", 72.0))
    assert r3.json()["inserted"] == 1 and "queued" not in r3.json()
    assert asyncio.run(db.observation_count(mac)) == 3


def test_ingest_queue_skips_token_upgrade_while_queued(client, monkeypatch):
    """The upgrade mints a token row — a write. A queued post must not
    hand one out; the board re-asks on its next post."""
    from app import db
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", True)
    r = client.post("/ingest/custom",
                    headers={**H_INGEST, "X-Token-Upgrade": "request"},
                    json=_payload())
    assert r.status_code == 200
    assert "assign_ingest_token" not in r.json()


def test_write_behind_cap_drops_oldest(client, monkeypatch, caplog):
    from app import db, ingest
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", True)
    monkeypatch.setattr(ingest, "_WRITE_BEHIND_MAX", 2)
    for i, temp in enumerate((60.0, 61.0, 62.0)):
        r = client.post("/ingest/custom", headers=H_INGEST,
                        json=_payload(f"2026-08-09T12:0{i}:00Z", temp))
        assert r.status_code == 200 and r.json()["queued"] is True
    assert ingest.write_behind_depth() == 2
    temps = [p["outdoor"]["tempf"] for p in ingest._WRITE_BEHIND]
    assert temps == [61.0, 62.0], "oldest reading must be the one dropped"
    assert any("write-behind queue full" in rec.message
               for rec in caplog.records), "the drop must be logged"


def test_write_behind_byte_cap_drops_oldest(client, monkeypatch, caplog):
    """The entry cap alone admits 5000 x 64 KiB of parked payloads (~320 MB,
    more than a small Fly machine has). A byte budget sits beside it and
    evicts the oldest on either bound (CodeRabbit, PR #35)."""
    from app import db, ingest
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", True)
    one = ingest._write_behind_size(_payload("2026-08-09T12:00:00Z", 60.0))
    # Room for two payloads and change, never three.
    monkeypatch.setattr(ingest, "_WRITE_BEHIND_MAX_BYTES", 2 * one + one // 2)
    for i, temp in enumerate((60.0, 61.0, 62.0)):
        r = client.post("/ingest/custom", headers=H_INGEST,
                        json=_payload(f"2026-08-09T12:0{i}:00Z", temp))
        assert r.status_code == 200 and r.json()["queued"] is True
    assert ingest.write_behind_depth() == 2
    assert ingest.write_behind_bytes() == 2 * one
    temps = [p["outdoor"]["tempf"] for p in ingest._WRITE_BEHIND]
    assert temps == [61.0, 62.0], "oldest reading must be the one dropped"
    assert any("write-behind queue full" in rec.message
               for rec in caplog.records), "the drop must be logged"
    # Draining settles the charge: nothing parked, nothing counted.
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", False)
    assert asyncio.run(ingest.drain_write_behind()) == 2
    assert ingest.write_behind_depth() == 0
    assert ingest.write_behind_bytes() == 0


def test_drain_re_park_keeps_identical_payloads_in_arrival_order(
        client, monkeypatch):
    """A device retrying the same body parks two EQUAL payloads. When the
    rebuild flag flips on between the drain's check and _do_ingest's, the
    head re-parks itself at the back and must be moved back to the front
    by identity: deque.remove() matched by equality and pulled the second
    copy out of order instead (CodeRabbit, PR #35)."""
    from app import db, ingest
    a = _payload("2026-08-09T12:00:00Z", 60.0)
    a_again = _payload("2026-08-09T12:00:00Z", 60.0)      # == a, is not a
    b = _payload("2026-08-09T12:01:00Z", 61.0)
    for p in (a, a_again, b):
        ingest._enqueue_write_behind(p, "AA:BB:CC:DD:EE:02")
    before = ingest.write_behind_bytes()

    calls = {"n": 0}

    def flips_on(*_a):
        calls["n"] += 1
        return calls["n"] > 1        # False for the drain's check, then True
    monkeypatch.setattr(db, "chart_index_rebuild_in_progress", flips_on)

    assert asyncio.run(ingest.drain_write_behind()) == 0
    order = [p is a for p in ingest._WRITE_BEHIND]
    assert order == [True, False, False], "the head must return to the front"
    assert list(ingest._WRITE_BEHIND)[1] is a_again
    assert ingest.write_behind_depth() == 3
    assert ingest.write_behind_bytes() == before, "a re-park is not a charge"


def test_drain_drops_invalid_payload_and_continues(client, monkeypatch):
    from app import db, ingest
    mac = "AA:BB:CC:DD:EE:02"
    ingest._WRITE_BEHIND.append({"device": {}})          # no id → 400 live
    ingest._WRITE_BEHIND.append(_payload())
    assert asyncio.run(ingest.drain_write_behind()) == 1
    assert ingest.write_behind_depth() == 0
    assert asyncio.run(db.observation_count(mac)) == 1


# ───────────────── 1c/1d/1e. lifespan chaining, reaping, retry ─────────

@pytest.fixture
def booted(temp_env, monkeypatch):
    """A factory that reloads the app modules (the `client` fixture's
    dance), lets the test patch db/insights BEFORE boot, then boots a
    TestClient. Returns (client, main, db, insights)."""
    def _boot(prepare):
        for mod in ["app.config", "app.maintenance", "app.db", "app.insights",
                    "app.wu_upload", "app.capture", "app.ingest", "app.meter",
                    "app.discovery", "app.alerts", "app.apns", "app.relay",
                    "app.integrations", "app.main"]:
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
        from app import db, insights, main
        prepare(db, insights, main)
        from fastapi.testclient import TestClient
        return TestClient(main.app), main, db, insights
    return _boot


def _wait(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_rollup_healer_runs_after_chart_index_task(booted, monkeypatch):
    """When a rebuild is pending the healer must wait for it, not race it
    for the write lock."""
    monkeypatch.setenv("INSIGHTS", "1")
    order: list[str] = []

    def prepare(db, insights, main):
        asyncio.run(db.init_db())
        asyncio.run(db.set_kv("rollups_dirty", "1"))
        monkeypatch.setattr(db, "chart_index_rebuild_needed", lambda: True)

        async def slow_rebuild():
            await asyncio.sleep(0.2)
            order.append("index")
        monkeypatch.setattr(db, "rebuild_chart_index", slow_rebuild)

        async def fake_heal():
            order.append("heal")
            await db.set_kv("rollups_dirty", None)
            return {}
        monkeypatch.setattr(insights, "rebuild", fake_heal)
    tc, main, db, insights = booted(prepare)
    with tc:
        assert _wait(lambda: len(order) == 2), order
    assert order == ["index", "heal"]


def test_rollup_healer_unchanged_when_no_rebuild_pending(booted, monkeypatch):
    monkeypatch.setenv("INSIGHTS", "1")
    ran: list[str] = []

    def prepare(db, insights, main):
        asyncio.run(db.init_db())
        asyncio.run(db.set_kv("rollups_dirty", "1"))

        async def fake_heal():
            ran.append("heal")
            await db.set_kv("rollups_dirty", None)
            return {}
        monkeypatch.setattr(insights, "rebuild", fake_heal)
    tc, main, db, insights = booted(prepare)
    with tc:
        assert tc.app.state.chart_index_task is None
        assert _wait(lambda: ran == ["heal"])


def test_every_app_owned_task_is_cancelled_and_awaited_at_shutdown(booted, monkeypatch):
    """R18 finding 12: the heal task was cancelled and dropped, and the
    dashboard refresh, snapshot, records warmers and recounts were not
    reaped at all, so a task could finalize after the loop closed."""
    def prepare(db, insights, main):
        real_get = db.get_kv

        async def dirty(key):
            return "1" if key == "rollups_dirty" else await real_get(key)
        monkeypatch.setattr(db, "get_kv", dirty)

        async def forever(*a, **k):
            await asyncio.sleep(3600)
        monkeypatch.setattr(insights, "rebuild", forever)
        from app.config import settings
        monkeypatch.setattr(settings, "insights", True)
    tc, main, db, insights = booted(prepare)

    async def forever():
        await asyncio.sleep(3600)
    with tc:
        heal = tc.app.state.rollup_heal_task
        assert heal is not None and not heal.done()
        # Park one of each of the module-level families the way the app
        # would, from inside the running loop (the TestClient's portal).
        started: dict = {}

        async def park():
            started["dash"] = asyncio.create_task(forever())
            started["backup"] = asyncio.create_task(forever())
            started["warm"] = asyncio.create_task(forever())
            main._PUBLIC_DASH_REFRESH_TASK = started["dash"]
            main._DB_BACKUP_TASK = started["backup"]
            main._WARM_TASKS.add(started["warm"])
        tc.portal.call(park)
    for name, t in started.items():
        assert t.done() and t.cancelled(), name
    assert heal.done() and heal.cancelled()


def test_chart_index_task_is_reaped_at_shutdown(booted, monkeypatch):
    def prepare(db, insights, main):
        monkeypatch.setattr(db, "chart_index_rebuild_needed", lambda: True)

        async def forever():
            await asyncio.sleep(3600)
        monkeypatch.setattr(db, "rebuild_chart_index", forever)
    tc, main, db, insights = booted(prepare)
    with tc:
        task = tc.app.state.chart_index_task
        assert task is not None and not task.done()
    assert task.done() and task.cancelled()


def test_chart_index_job_does_not_retry_a_lock_timeout(client, monkeypatch):
    from app import db, main
    monkeypatch.setattr(main, "_CHART_INDEX_RETRY_DELAY_S", 0.0)
    calls = {"n": 0}

    async def locked():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "rebuild_chart_index", locked)
    asyncio.run(main._chart_index_job())
    assert calls["n"] == 1, "a lock timeout must not be retried"

    calls["n"] = 0

    async def other():
        calls["n"] += 1
        raise RuntimeError("disk hiccup")
    monkeypatch.setattr(db, "rebuild_chart_index", other)
    asyncio.run(main._chart_index_job())
    assert calls["n"] == 3, "other failures keep the three attempts"


def test_chart_index_job_drains_queue_after_each_attempt(client, monkeypatch):
    from app import db, ingest, main
    mac = "AA:BB:CC:DD:EE:02"
    monkeypatch.setattr(main, "_CHART_INDEX_RETRY_DELAY_S", 0.0)

    async def rebuild_with_post_mid_build():
        # A reading arrives while the CREATE holds the lock.
        db._CHART_INDEX_BUILDING = True
        try:
            res = await ingest._do_ingest(_payload())
            assert res["queued"] is True
        finally:
            db._CHART_INDEX_BUILDING = False
    monkeypatch.setattr(db, "rebuild_chart_index", rebuild_with_post_mid_build)
    asyncio.run(main._chart_index_job())
    assert ingest.write_behind_depth() == 0
    assert asyncio.run(db.observation_count(mac)) == 1


# ───────────────────────── 2. 503 + Retry-After ────────────────────────

def test_sqlite_lock_error_answers_503_with_retry_after(client, monkeypatch, caplog):
    from app import db

    async def locked():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "list_devices", locked)
    r = client.get("/api/devices", headers=H_API)
    assert r.status_code == 503, r.text
    assert r.headers.get("retry-after") == "5"
    assert r.json() == {"detail": "database busy, retry"}
    busy_lines = [rec for rec in caplog.records
                  if "database busy on /api/devices" in rec.message]
    assert len(busy_lines) == 1 and busy_lines[0].levelname == "WARNING"
    # Throttled: a second hit inside the minute logs nothing new.
    r = client.get("/api/devices", headers=H_API)
    assert r.status_code == 503
    assert len([rec for rec in caplog.records
                if "database busy on /api/devices" in rec.message]) == 1


def test_sqlite_busy_message_also_503s(client, monkeypatch):
    from app import db

    async def busy():
        raise sqlite3.OperationalError("database is busy")
    monkeypatch.setattr(db, "list_devices", busy)
    assert client.get("/api/devices", headers=H_API).status_code == 503


def test_other_sqlite_operational_errors_still_500(client, monkeypatch):
    """Only lock/busy is a 503. A real fault propagates exactly as before
    (TestClient re-raises what the server would have 500'd)."""
    from app import db

    async def broken():
        raise sqlite3.OperationalError("no such table: observations")
    monkeypatch.setattr(db, "list_devices", broken)
    with pytest.raises(sqlite3.OperationalError):
        client.get("/api/devices", headers=H_API)


# ───────────────────────── 3. non-ASCII token → 401 ────────────────────

_LATIN1_BEARER = "Bearer \xe9\xe9".encode("latin-1")
_LATIN1_TOKEN = "\xe9\xe9".encode("latin-1")


def test_non_ascii_bearer_is_401_not_500(client):
    r = client.get("/api/devices", headers={"Authorization": _LATIN1_BEARER})
    assert r.status_code == 401, r.text
    # Precondition: the ASCII wrong token takes the same door.
    assert client.get("/api/devices",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


def test_non_ascii_ingest_token_is_rejected_not_500(client):
    r = client.post("/ingest/custom", headers={"X-Ingest-Token": _LATIN1_TOKEN},
                    json=_payload())
    wrong = client.post("/ingest/custom", headers={"X-Ingest-Token": "nope"},
                        json=_payload())
    assert wrong.status_code in (401, 403)
    assert r.status_code == wrong.status_code, r.text


def test_tokens_match_never_raises_on_non_ascii():
    from app.config import tokens_match
    assert tokens_match("éé", "abc") is False
    assert tokens_match("abc", ("éé", "abc")) is True
    assert tokens_match("éé", "éé") is True
    assert tokens_match("\udcff", "x") is False        # lone surrogate


# ───────────────────────── 4. migration completion keys ────────────────

def test_lightning_backfill_reruns_when_key_set_and_columns_exist(client):
    """Columns present (the ALTERs autocommitted), key still set (the
    UPDATE was killed and rolled back): the next boot must run the fill."""
    from app import db
    from app.config import settings
    mac = "AA:11"
    stormy = {"dateutc": 1000, "tempf": 88.0, "lightningcount": 23,
              "lightning_last_1hr": 731, "lightning_distance_mi": 6.2}
    calm = {"dateutc": 2000, "tempf": 70.0}
    con = sqlite3.connect(settings.database_path)
    for row in (stormy, calm):
        con.execute(
            "INSERT INTO observations (mac, dateutc_ms, data_json, tempf) "
            "VALUES (?, ?, ?, ?)",
            (mac, row["dateutc"], json.dumps(row), row["tempf"]))
    con.commit(); con.close()

    # Boot with no key: columns exist, nothing to do — the fill must NOT
    # run on its own (that would be a full-table UPDATE every boot).
    asyncio.run(db.init_db())
    con = sqlite3.connect(settings.database_path)
    assert con.execute("SELECT lightningcount FROM observations "
                       "WHERE dateutc_ms=1000").fetchone()[0] is None
    con.close()

    asyncio.run(db.set_kv(db.LIGHTNING_BACKFILL_KEY, "1"))
    asyncio.run(db.init_db())
    con = sqlite3.connect(settings.database_path)
    rows = con.execute(
        "SELECT lightningcount, lightning_last_1hr, lightning_distance_mi "
        "FROM observations ORDER BY dateutc_ms").fetchall()
    con.close()
    assert rows[0] == (23, 731, 6.2)
    assert rows[1] == (None, None, None)              # absent is not zero
    assert asyncio.run(db.get_kv(db.LIGHTNING_BACKFILL_KEY)) is None
    assert asyncio.run(db.get_kv("rollups_dirty")) is not None


def test_lightning_migration_writes_key_before_fill(client, monkeypatch):
    """On the boot that ADDs the columns, the key must be durable before
    the UPDATE starts — that is the whole guarantee. Observe via the
    connection: when the UPDATE runs, a second connection sees the key."""
    from app import db
    from app.config import settings
    path = settings.database_path
    con = sqlite3.connect(path)
    con.execute(f"DROP INDEX {db.chart_index_name()}")   # references the cols
    for col in ("lightningcount", "lightning_last_1hr", "lightning_distance_mi"):
        con.execute(f"ALTER TABLE observations DROP COLUMN {col}")
    con.commit(); con.close()
    assert asyncio.run(db.get_kv(db.LIGHTNING_BACKFILL_KEY)) is None
    asyncio.run(db.init_db())
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(observations)")}
    con.close()
    assert {"lightningcount", "lightning_last_1hr",
            "lightning_distance_mi"} <= cols
    assert asyncio.run(db.get_kv(db.LIGHTNING_BACKFILL_KEY)) is None, \
        "a completed fill clears its key"


def test_storm_capture_backfill_reruns_when_key_set(client, monkeypatch):
    from app import db
    calls = {"n": 0}
    real = db._backfill_storm_capture

    async def counting(conn):
        calls["n"] += 1
        return await real(conn)
    monkeypatch.setattr(db, "_backfill_storm_capture", counting)

    asyncio.run(db.init_db())                 # columns exist, no key
    assert calls["n"] == 0
    asyncio.run(db.set_kv(db.STORM_CAPTURE_BACKFILL_KEY, "1"))
    asyncio.run(db.init_db())
    assert calls["n"] == 1
    assert asyncio.run(db.get_kv(db.STORM_CAPTURE_BACKFILL_KEY)) is None
    asyncio.run(db.init_db())
    assert calls["n"] == 1, "cleared key: the fill does not run again"


# ───────────── 5. replay failures keep the reading (R18 finding 4) ─────────────

def test_an_unexpected_replay_failure_keeps_the_reading_for_the_next_drain(
        client, monkeypatch, caplog):
    """The sender was told `queued: true` and not to retry, so the queue is
    the only copy. A non-lock error on replay re-parks the payload at the
    front and pauses the drain; the next drain stores it once."""
    from app import db, ingest
    mac = "AA:BB:CC:DD:EE:02"
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", True)
    r = client.post("/ingest/custom", headers=H_INGEST,
                    json=_payload("2026-08-09T12:00:00Z", 70.0))
    assert r.status_code == 200 and r.json()["queued"] is True
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", False)
    calls = {"n": 0}
    real = ingest._do_ingest

    async def flaky(p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk hiccup")
        return await real(p)
    monkeypatch.setattr(ingest, "_do_ingest", flaky)

    assert asyncio.run(ingest.drain_write_behind()) == 0
    assert ingest.write_behind_depth() == 1, "kept, not dropped"
    assert "reading kept" in caplog.text
    assert asyncio.run(ingest.drain_write_behind()) == 1
    assert ingest.write_behind_depth() == 0 and calls["n"] == 2
    assert asyncio.run(db.observation_count(mac)) == 1


def test_a_reading_that_keeps_failing_is_dropped_after_the_cap(client, monkeypatch, caplog):
    from app import db, ingest
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", True)
    client.post("/ingest/custom", headers=H_INGEST, json=_payload())
    assert ingest.write_behind_depth() == 1
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", False)

    async def broken(p):
        raise RuntimeError("always")
    monkeypatch.setattr(ingest, "_do_ingest", broken)
    for _ in range(ingest._WRITE_BEHIND_MAX_REPLAY_FAILURES - 1):
        asyncio.run(ingest.drain_write_behind())
        assert ingest.write_behind_depth() == 1
    asyncio.run(ingest.drain_write_behind())
    assert ingest.write_behind_depth() == 0
    assert "reading dropped" in caplog.text


# ───────────── 6. one recount per station (R18 finding 7) ─────────────

def test_an_evicted_reading_takes_its_failure_count_with_it(client, monkeypatch):
    """The count is keyed by id(payload), a name only while the object is
    in the deque; an eviction at the cap must settle it like the byte
    charge (CodeRabbit, PR #35)."""
    from app import db, ingest
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", True)
    client.post("/ingest/custom", headers=H_INGEST, json=_payload())
    monkeypatch.setattr(db, "_CHART_INDEX_BUILDING", False)

    async def broken(p):
        raise RuntimeError("once")
    monkeypatch.setattr(ingest, "_do_ingest", broken)
    asyncio.run(ingest.drain_write_behind())
    assert len(ingest._WRITE_BEHIND_FAILURES) == 1
    monkeypatch.setattr(ingest, "_WRITE_BEHIND_MAX", 1)
    ingest._enqueue_write_behind({"PASSKEY": "AA:BB:CC:DD:EE:33",
                                  "dateutc": 1_788_000_000_000, "tempf": 70.0},
                                 "AA:BB:CC:DD:EE:33")        # the cap evicts the first
    assert ingest._WRITE_BEHIND_FAILURES == {}
    assert ingest.write_behind_depth() == 1


def test_a_stale_count_spawns_one_recount_per_station(client, monkeypatch):
    import asyncio, time
    import app.main as m
    gate = asyncio.Event()
    calls: list[str] = []

    async def slow_count(mac):
        calls.append(mac)
        await gate.wait()
        return 42

    async def run():
        monkeypatch.setattr(m.db, "observation_count", slow_count)
        stale = time.time() - m._OBS_COUNT_TTL_S - 1
        m._OBS_COUNT_CACHE["A"] = (stale, 7)
        m._OBS_COUNT_CACHE["B"] = (stale, 8)
        got = await asyncio.gather(*[m._cached_observation_count("A") for _ in range(25)],
                                   m._cached_observation_count("B"))
        assert got == [7] * 25 + [8], "stale values served at once"
        await asyncio.sleep(0)
        assert sorted(calls) == ["A", "B"], "exactly one recount per station"
        assert set(m._OBS_COUNT_TASKS) == {"A", "B"}
        gate.set()
        await asyncio.gather(*m._OBS_COUNT_TASKS.values())
        assert m._OBS_COUNT_CACHE["A"][1] == 42 and m._OBS_COUNT_TASKS == {}
    asyncio.run(run())
