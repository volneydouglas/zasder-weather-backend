"""SQLite roundtrip tests — insert / count / dedup.

Uses the same temp_env fixture as the API tests but skips the TestClient
since these go straight at the db module."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def db_module(temp_env: str):
    # Force re-import so config.settings picks up our temp DATABASE_PATH.
    for mod in ["app.config", "app.db"]:
        if mod in importlib.sys.modules: importlib.reload(importlib.sys.modules[mod])
    from app import db
    return db


@pytest.mark.asyncio
async def test_insert_dedup(db_module):
    db = db_module
    await db.init_db()
    rows = [{"dateutc": 1000, "tempf": 70.0}, {"dateutc": 2000, "tempf": 71.0}]
    n = await db.insert_observations("AA:BB:CC:DD:EE:FF", rows)
    assert n == 2
    # Same MAC + same dateutc_ms is a primary-key conflict → silently ignored.
    n2 = await db.insert_observations("AA:BB:CC:DD:EE:FF", rows)
    assert n2 == 0
    assert await db.observation_count("AA:BB:CC:DD:EE:FF") == 2


@pytest.mark.asyncio
async def test_observation_count_isolated_per_mac(db_module):
    db = db_module
    await db.init_db()
    await db.insert_observations("AA:11", [{"dateutc": 1, "tempf": 70}])
    await db.insert_observations("BB:22", [{"dateutc": 1, "tempf": 80},
                                            {"dateutc": 2, "tempf": 81}])
    assert await db.observation_count("AA:11") == 1
    assert await db.observation_count("BB:22") == 2
    assert await db.observation_count("CC:33") == 0


@pytest.mark.asyncio
async def test_history_returns_chronological(db_module):
    db = db_module
    await db.init_db()
    await db.insert_observations("AA:11", [
        {"dateutc": 3000, "tempf": 73},
        {"dateutc": 1000, "tempf": 71},
        {"dateutc": 2000, "tempf": 72},
    ])
    rows = await db.history("AA:11", 0, 10000)
    temps = [r.get("tempf") for r in rows]
    # history is ORDER BY dateutc_ms ASC
    assert temps == [71, 72, 73]


@pytest.mark.asyncio
async def test_rain_rollups_falls_back_to_monthly_when_yearly_broken(db_module):
    """Regression: a Davis WeatherLink whose yearly counter reset while a stale
    rain offset clamps it to ~0 (yearly < monthly) must still report weekly
    rain — derived from the reliable monthly counter, not the broken yearly."""
    from app import db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    await db.init_db()
    mac = "5D:5D:05:00:00:01"

    # Anchor to the REAL week boundary rather than assuming fixed day offsets
    # straddle it. Weeks start Sunday (db.rain_rollups), so on a Sunday
    # "5 days ago" and "1 day ago" both land in the *previous* week and weekly
    # rain is legitimately 0 — this test used to fail every Sunday.
    tz = ZoneInfo("America/Phoenix")
    now_local = datetime.now(tz)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_today - timedelta(days=(now_local.weekday() + 1) % 7)
    ms = lambda d: int(d.timestamp() * 1000)
    # Midpoint is always inside [start_of_week, now], even at 00:00 Sunday.
    midweek = start_of_week + (now_local - start_of_week) / 2

    # Baseline just before the week started with monthly=0.0, then rain brought
    # monthly to 0.14; yearly is stuck at 0.0 (broken) the whole time.
    await db.insert_observations(mac, [
        {"dateutc": ms(start_of_week - timedelta(hours=1)),
         "yearlyrainin": 0.0, "monthlyrainin": 0.0,  "dailyrainin": 0.0},
        {"dateutc": ms(midweek),
         "yearlyrainin": 0.0, "monthlyrainin": 0.14, "dailyrainin": 0.14},
        {"dateutc": ms(now_local),
         "yearlyrainin": 0.0, "monthlyrainin": 0.14, "dailyrainin": 0.0},
    ])
    r = await db.rain_rollups(mac, "America/Phoenix")
    assert r["monthly_in"] == 0.14           # direct from the monthly counter
    assert r["weekly_in"] == 0.14            # derived from monthly, NOT the 0 yearly


@pytest.mark.asyncio
async def test_rain_rollups_uses_yearly_for_lifetime_counter(db_module):
    """SDR/LilyGO sensors post only a monotonic lifetime yearly (no monthly);
    the trusted yearly path is unchanged."""
    from app import db
    import time
    await db.init_db()
    mac = "5D:5D:01:00:02:C7"
    HOUR = 3_600_000
    now = int(time.time() * 1000)
    await db.insert_observations(mac, [
        {"dateutc": now - 5 * 24 * HOUR, "yearlyrainin": 0.73},
        {"dateutc": now,                 "yearlyrainin": 0.74},
    ])
    r = await db.rain_rollups(mac, "America/Phoenix")
    assert r["weekly_in"] == 0.01            # 0.74 - 0.73 via the yearly counter


@pytest.mark.asyncio
async def test_upsert_device_out_of_order_does_not_regress(db_module):
    """The devices-row UPDATE arm is monotonic on purpose (backend CR-18): a
    backfilled / out-of-order post (SDR replay, a batch of history) must not
    drag last_seen_ms BACKWARDS or overwrite lastData with older values — a
    regressed last_seen once fired false device-down alerts and showed a stale
    "current" reading until the next fresh post."""
    db = db_module
    await db.init_db()
    mac = "AA:BB:CC:DD:EE:FF"

    def info(ts, tempf, location):
        return {"name": None, "auto_name": "Test Source",
                "info": {"name": "Test Source", "location": location},
                "lastData": ({"dateutc": ts, "tempf": tempf}
                             if ts is not None else {})}

    await db.upsert_device(mac, info(2000, 80.0, "newer"))
    # Out-of-order repost with an OLDER timestamp: everything retained.
    await db.upsert_device(mac, info(1000, 70.0, "older"))
    d = (await db.list_devices())[0]
    assert d["lastSeen"] == 2000, "last_seen_ms regressed on an older post"
    assert d["lastData"]["tempf"] == 80.0, "lastData overwritten by older data"
    assert d["location"] == "newer"


@pytest.mark.asyncio
async def test_upsert_device_same_timestamp_repost_merges(db_module):
    """`>=` not `>`: a same-timestamp repost (a second source contributing to
    the same composite reading) must still update the row."""
    db = db_module
    await db.init_db()
    mac = "AA:BB:CC:DD:EE:FF"
    base = {"name": None, "auto_name": "A", "info": {}}
    await db.upsert_device(mac, {**base, "lastData": {"dateutc": 2000, "tempf": 80.0}})
    await db.upsert_device(mac, {**base, "lastData": {"dateutc": 2000, "tempf": 81.5}})
    d = (await db.list_devices())[0]
    assert d["lastSeen"] == 2000
    assert d["lastData"]["tempf"] == 81.5      # same-ts repost took effect


@pytest.mark.asyncio
async def test_upsert_device_null_last_seen_refreshes_but_never_regresses(db_module):
    """A caller with no lastData (last_seen NULL) can't be ordered: it must
    refresh info_json but leave the timestamp alone (the MAX/COALESCE pair)."""
    db = db_module
    await db.init_db()
    mac = "AA:BB:CC:DD:EE:FF"
    await db.upsert_device(mac, {"name": None, "auto_name": "A", "info": {},
                                 "lastData": {"dateutc": 2000, "tempf": 80.0}})
    await db.upsert_device(mac, {"name": "Renamed", "auto_name": "A",
                                 "info": {"note": "fresh"}, "lastData": {}})
    d = (await db.list_devices())[0]
    assert d["lastSeen"] == 2000               # never regressed to NULL
    assert d["name"] == "Renamed"              # explicit rename applied
    assert d["info"].get("note") == "fresh"    # info_json refreshed


@pytest.mark.asyncio
async def test_init_db_rebuilds_stale_chart_index(db_module):
    """init_db must rebuild idx_obs_chart whenever ANY expected column is
    missing from the stored definition — not just one hard-coded name. The
    stale index here deliberately CONTAINS windgustmph (the column the old
    hard-coded check looked for) but misses others, so a regression back to
    the single-name test skips the rebuild and this fails."""
    import aiosqlite
    from app.config import settings
    db = db_module
    await db.init_db()
    async with aiosqlite.connect(settings.database_path) as conn:
        await conn.execute("DROP INDEX idx_obs_chart")
        await conn.execute(
            "CREATE INDEX idx_obs_chart ON observations "
            "(mac, dateutc_ms, tempf, windgustmph)")     # old/narrow definition
        await conn.commit()
    await db.init_db()                                   # migration runs here
    async with aiosqlite.connect(settings.database_path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_obs_chart'")).fetchone()
    assert row is not None and row["sql"]
    for col in db._CHART_INDEX_COLS:
        assert col in row["sql"], f"rebuilt index is missing {col}"
