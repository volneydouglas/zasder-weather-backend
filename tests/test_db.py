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


@pytest.mark.asyncio
async def test_init_db_adds_lightning_columns_and_backfills(db_module):
    """A database created before 1.6 has no lightning columns, but its
    data_json may already CARRY lightning — the poller deliberately captured
    it into the blob before the schema existed, because the counters are
    interval-scoped and a storm that passes unrecorded is gone. init_db must
    both ALTER the columns in and fold that captured data into them; without
    the backfill, every strike stored before the upgrade is invisible to
    records and charts even though we hold the data."""
    import aiosqlite
    import json
    from app.config import settings
    db = db_module
    # Recreate the pre-1.6 table: every column except the lightning three.
    old_cols = [c for c in db._COLUMNS if not c.startswith("lightning")]
    async with aiosqlite.connect(settings.database_path) as conn:
        await conn.execute(
            "CREATE TABLE observations (mac TEXT NOT NULL, "
            "dateutc_ms INTEGER NOT NULL, data_json TEXT NOT NULL, "
            + ", ".join(f"{c} REAL" for c in old_cols)
            + ", PRIMARY KEY (mac, dateutc_ms))")
        stormy = {"dateutc": 1000, "tempf": 88.0, "lightningcount": 23,
                  "lightning_last_1hr": 731, "lightning_distance_mi": 6.2}
        calm = {"dateutc": 2000, "tempf": 70.0}
        for row in (stormy, calm):
            await conn.execute(
                "INSERT INTO observations (mac, dateutc_ms, data_json, tempf) "
                "VALUES (?, ?, ?, ?)",
                ("AA:11", row["dateutc"], json.dumps(row), row["tempf"]))
        await conn.commit()
    await db.init_db()                                   # migration + backfill
    async with aiosqlite.connect(settings.database_path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT dateutc_ms, lightningcount, lightning_last_1hr, "
            "lightning_distance_mi FROM observations ORDER BY dateutc_ms"
        )).fetchall()
    assert rows[0]["lightningcount"] == 23
    assert rows[0]["lightning_last_1hr"] == 731
    assert rows[0]["lightning_distance_mi"] == 6.2
    # The row with no lightning stays NULL across the board — a backfill
    # that wrote 0 would render a tile confidently reporting "0 strikes"
    # for a station with no detector (absent is not zero).
    assert rows[1]["lightningcount"] is None
    assert rows[1]["lightning_last_1hr"] is None
    assert rows[1]["lightning_distance_mi"] is None


@pytest.mark.asyncio
async def test_bucketed_history_serves_lightning_peak_as_int(db_module):
    """Bucketed history must carry lightning_last_1hr (a) at the bucket's
    PEAK — averaging a storm's ramp against its tail hides the number that
    mattered — and (b) as a JSON integer: the app decodes strikes as Int?
    with synthesized Codable, so one 731.0 fails the whole row's decode."""
    db = db_module
    await db.init_db()
    hour = 3_600_000
    # 731.5 on purpose: our own poller posts ints, but /ingest/custom can
    # deliver a float, and SQLite's INTEGER affinity keeps a non-integral
    # REAL as REAL — the exact value that would poison the JSON without
    # the CAST. An integral test value would pass with the CAST removed.
    rows = [{"dateutc": 1 * hour, "tempf": 88.0,
             "lightning_last_1hr": 100},
            {"dateutc": 1 * hour + 60_000, "tempf": 88.0,
             "lightning_last_1hr": 731.5}]
    await db.insert_observations("AA:11", rows)
    # An 8h window forces the bucketed path (raw only up to 6h).
    out = await db.history("AA:11", 0, 8 * hour)
    got = [r["lightning_last_1hr"] for r in out
           if r.get("lightning_last_1hr") is not None]
    assert got, "bucketed history dropped lightning entirely"
    peak = max(got)
    assert peak == 731
    assert isinstance(peak, int) and not isinstance(peak, bool)


@pytest.mark.asyncio
async def test_records_report_lightning_only_when_the_station_has_it(db_module):
    """Most-strikes-in-an-hour is a record for a station with a detector, and
    ABSENT for one without — an empty field must be omitted (not zeroed) so
    the app shows no card at all."""
    db = db_module
    await db.init_db()
    await db.insert_observations("TE:MP", [
        {"dateutc": 1000, "tempf": 88.0, "lightning_last_1hr": 731},
        {"dateutc": 2000, "tempf": 87.0, "lightning_last_1hr": 12},
    ])
    await db.insert_observations("DA:VI", [
        {"dateutc": 1000, "tempf": 70.0},
    ])
    tempest = await db.records("TE:MP")
    davis = await db.records("DA:VI")
    rec = tempest["periods"]["all"]["fields"]["lightning_last_1hr"]
    assert rec["max"] == 731
    assert rec["maxAt"] == 1000
    assert "lightning_last_1hr" not in davis["periods"]["all"]["fields"]


@pytest.mark.asyncio
async def test_rain_rollups_derives_periods_from_daily_counters(db_module):
    """Tempest shape: the source posts hourly+daily rain and nothing longer
    (the WeatherFlow REST response has no weekly/monthly/yearly), so the
    longer periods must come from summing stored DAILY counters. A day's
    total is the day's MAX — WeatherFlow revises the accumulator downward
    mid-day and re-climbs, so last-value drops a low-revision day and
    increment-summing counts every re-climb twice."""
    import time
    from datetime import datetime, timezone
    db = db_module
    await db.init_db()
    day = 86_400_000
    now = datetime.now(timezone.utc)
    day0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0)
               .timestamp() * 1000)
    rows = [
        # One local (UTC in tests) day: climb, downward revision, re-climb.
        # The day's truth is the 0.123 high-water — not the 0.025 revision,
        # and not the 0.252 that summing increments would invent.
        {"dateutc": day0 + 1 * 3_600_000, "dailyrainin": 0.104},
        {"dateutc": day0 + 2 * 3_600_000, "dailyrainin": 0.025},
        {"dateutc": day0 + 3 * 3_600_000, "dailyrainin": 0.123},
        # ~370 days ago: a different calendar year, excluded from every
        # period however wet it was.
        {"dateutc": day0 - 370 * day, "dailyrainin": 5.0},
    ]
    await db.insert_observations("TE:MP", rows)
    out = await db.rain_rollups("TE:MP")
    assert out["weekly_in"] == 0.123
    assert out["monthly_in"] == 0.123
    assert out["yearly_in"] == 0.123
    # The source's own counters are authoritative for the short windows —
    # a MAX here could contradict the (revised) number the station shows.
    assert out["daily_in"] is None
    assert out["hourly_in"] is None


async def _seed_three_days(db, mac):
    """Three local (UTC in tests) days of readings with known extremes:
    day-2 holds the all-time high 111.0 at a known instant; today holds the
    period-today high 99.5. Plus lightning on day-1 only.

    The today-row's timestamp must be BOTH within today and in the PAST:
    a fixed day0+2h landed in the future for any test run between 00:00
    and 02:00 UTC, records() excluded it (end_ms = now), and the whole
    records family failed for two hours a day (found live 2026-08-20 at
    00:35 UTC)."""
    import time
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    day0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0)
               .timestamp() * 1000)
    day = 86_400_000
    today_ts = max(day0 + 1000, now_ms - 60_000)
    rows = [
        {"dateutc": day0 - 2 * day + 14 * 3_600_000, "tempf": 111.0,
         "humidity": 20.0, "dailyrainin": 0.0},
        {"dateutc": day0 - 2 * day + 20 * 3_600_000, "tempf": 90.0,
         "humidity": 55.0, "dailyrainin": 0.4},
        {"dateutc": day0 - 1 * day + 6 * 3_600_000, "tempf": 76.0,
         "humidity": 80.0, "dailyrainin": 0.0, "lightning_last_1hr": 731},
        {"dateutc": today_ts, "tempf": 99.5,
         "humidity": 40.0, "dailyrainin": 0.1},
    ]
    await db.insert_observations(mac, rows)
    return day0, rows


@pytest.mark.asyncio
async def test_records_long_periods_come_from_rollups(db_module, monkeypatch):
    """Month/year/all-time records are answered from daily_rollups (a few
    thousand pre-folded rows) instead of scanning the whole archive — the
    110s cold call on a 1.09M-row archive. Values must MATCH the raw truth,
    the record's day must survive (date-only is all the UI shows there),
    and Today must keep its exact raw timestamp."""
    from datetime import datetime, timezone
    from app import insights
    db = db_module
    await db.init_db()
    mac = "TE:MP"
    day0, rows = await _seed_three_days(db, mac)
    await insights.rebuild(mac)

    out = await db.records(mac)
    allp = out["periods"]["all"]["fields"]
    assert allp["tempf"]["max"] == 111.0
    assert allp["tempf"]["min"] == 76.0
    assert allp["humidity"]["max"] == 80.0
    # The record's DAY is preserved (noon of the day that held 111.0).
    at = datetime.fromtimestamp(allp["tempf"]["maxAt"] / 1000, timezone.utc)
    expect = datetime.fromtimestamp((day0 - 2 * 86_400_000) / 1000,
                                    timezone.utc)
    assert at.date() == expect.date()
    # Lightning records ride the rollups too — and only for stations that
    # ever reported it.
    assert allp["lightning_last_1hr"]["max"] == 731
    # Today keeps the raw path: the exact reading timestamp, not noon.
    today = out["periods"]["today"]["fields"]["tempf"]
    assert today["max"] == 99.5
    assert today["maxAt"] == rows[-1]["dateutc"]


@pytest.mark.asyncio
async def test_records_week_period_is_trailing_seven_days(db_module):
    """1.9: the Week period. Trailing 7 local days including today — the
    Charts "7d" grammar, not a calendar week (which shows one day's records
    every Sunday morning, and whose Sunday depends on locale)."""
    db = db_module
    await db.init_db()
    mac = "TE:MP"
    day0, rows = await _seed_three_days(db, mac)

    out = await db.records(mac)
    week = out["periods"]["week"]
    # Boundary: local (UTC here) midnight six days back, so the window spans
    # exactly 7 day-buckets ending today.
    assert week["start_ms"] == day0 - 6 * 86_400_000
    # The 3-day seed sits entirely inside the trailing week, so the week
    # record equals the all-time record — including day-2's 111.0, which
    # "today" must NOT see.
    assert week["fields"]["tempf"]["max"] == 111.0
    assert week["fields"]["tempf"]["min"] == 76.0
    assert out["periods"]["today"]["fields"]["tempf"]["max"] == 99.5


@pytest.mark.asyncio
async def test_records_fall_back_when_rollups_do_not_cover(db_module):
    """INSIGHTS enabled late (or never rebuilt) leaves rollups starting
    after the archive does. Answering all-time from those would silently
    erase the oldest records — the period must fall back to the raw scan."""
    from app import insights
    db = db_module
    await db.init_db()
    mac = "TE:MP"
    day0, rows = await _seed_three_days(db, mac)
    await insights.rebuild(mac)
    # Simulate late-enabled insights: drop the oldest rollup day (the one
    # holding the 111.0 record).
    async with db.connect() as conn:
        await conn.execute(
            "DELETE FROM daily_rollups WHERE mac = ? AND day = "
            "(SELECT MIN(day) FROM daily_rollups WHERE mac = ?)", (mac, mac))
        await conn.commit()
    out = await db.records(mac)
    allp = out["periods"]["all"]["fields"]
    assert allp["tempf"]["max"] == 111.0, \
        "partial rollups served for all-time — oldest record erased"
    # And the raw fallback keeps exact times.
    assert allp["tempf"]["maxAt"] == day0 - 2 * 86_400_000 + 14 * 3_600_000


@pytest.mark.asyncio
async def test_records_rollup_path_keeps_cumulative_rain_guard(db_module):
    """The wettest-day suppression for lifetime cumulative counters must
    hold on the rollup path too — rain_total folded from a non-resetting
    dailyrainin is a lifetime total, not a single-day record."""
    import time
    from app import insights
    db = db_module
    await db.init_db()
    mac = "SD:R1"
    now = int(time.time() * 1000)
    day = 86_400_000
    await db.insert_observations(mac, [
        {"dateutc": now - 2 * day, "tempf": 70.0, "dailyrainin": 17.10},
        {"dateutc": now - 1 * day, "tempf": 71.0, "dailyrainin": 17.12},
        {"dateutc": now, "tempf": 72.0, "dailyrainin": 17.16},
    ])
    await insights.rebuild(mac)
    out = await db.records(mac)
    dr = out["periods"]["all"]["fields"]["dailyrainin"]
    assert dr["max"] is None and dr["maxAt"] is None


@pytest.mark.asyncio
async def test_daily_rain_tier_week_boundary_in_a_dst_zone(db_module):
    """Review 2026-08-20: the daily-rain tier floored period boundaries
    against a Jan-1-midnight anchor, and in a DST zone the two midnights sit
    an hour apart during summer — flooring N days minus one hour gave N-1,
    so weekly rain absorbed the entire day BEFORE the week started. A
    Saturday cloudburst must not appear in the following week's total.
    (Discriminates only during the DST half of the year — EST winter aligns
    the midnights — but the rounding it pins is provably right year-round.)"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    db = db_module
    tz = ZoneInfo("America/New_York")
    await db.init_db()
    mac = "TE:NY"
    now = datetime.now(tz)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_today - timedelta(days=(now.weekday() + 1) % 7)
    day_before = start_of_week - timedelta(hours=12)   # prior Saturday noon
    in_week = start_of_week + timedelta(hours=6)
    await db.insert_observations(mac, [
        {"dateutc": int(day_before.timestamp() * 1000), "dailyrainin": 3.0},
        {"dateutc": int(in_week.timestamp() * 1000), "dailyrainin": 0.2},
    ])
    out = await db.rain_rollups(mac, "America/New_York")
    assert out["weekly_in"] == 0.2, \
        f"prior Saturday's 3.0in leaked into the week: {out}"


@pytest.mark.asyncio
async def test_daily_tier_refuses_cumulative_counters(db_module):
    """CodeRabbit 2026-08-20: a device whose ONLY rain field is a lifetime
    cumulative counter stored in dailyrainin enters tier 3, and summing its
    per-day maxima invents 17.10 + 17.16 = 34.26in across two days for a
    0.06in increment. Same judgment records() makes: a counter that never
    touches ~0 is not computable — all-None, never a fabricated total."""
    import time
    db = db_module
    await db.init_db()
    mac = "SD:R2"
    day = 86_400_000
    now = int(time.time() * 1000)
    await db.insert_observations(mac, [
        {"dateutc": now - 2 * day, "dailyrainin": 17.10},
        {"dateutc": now - 1 * day, "dailyrainin": 17.12},
        {"dateutc": now, "dailyrainin": 17.16},
    ])
    out = await db.rain_rollups(mac)
    assert out["weekly_in"] is None
    assert out["monthly_in"] is None
    assert out["yearly_in"] is None


@pytest.mark.asyncio
async def test_records_fall_back_when_rollups_freeze(db_module):
    """CodeRabbit 2026-08-20: coverage was judged only at the START day, so
    rollups frozen by INSIGHTS being disabled (observations keep landing,
    daily_rollups stops advancing) still answered the long periods — and a
    record set after the freeze never appeared. Staleness at the NEW end
    must force the raw fallback too."""
    import time
    from app import insights
    db = db_module
    await db.init_db()
    mac = "TE:FR"
    day = 86_400_000
    now = int(time.time() * 1000)
    await db.insert_observations(mac, [
        {"dateutc": now - 2 * day, "tempf": 111.0},
        {"dateutc": now - 1 * day, "tempf": 90.0},
    ])
    await insights.rebuild(mac)
    # The freeze: a NEW all-time record lands after rollups stop advancing.
    await db.insert_observations(mac, [{"dateutc": now, "tempf": 115.0}])
    async with db.connect() as conn:
        await conn.execute(
            "DELETE FROM daily_rollups WHERE mac = ? AND day = "
            "(SELECT MAX(day) FROM daily_rollups WHERE mac = ?)", (mac, mac))
        await conn.commit()
    out = await db.records(mac)
    assert out["periods"]["all"]["fields"]["tempf"]["max"] == 115.0, \
        "frozen rollups served for all-time — the new record vanished"


@pytest.mark.asyncio
async def test_records_take_the_raw_path_while_rollups_are_dirty(db_module):
    """CODE_REVIEW_R5 R5-15: a repair nulls a bad spike in observations,
    but fold-forward rollups can't go down — and records() serves long
    periods from rollups, so the repaired spike stayed on the Records
    screen indefinitely. The rollups_dirty flag (set by maintenance
    repairs and the lightning backfill) must force the raw path; a FULL
    rebuild clears it; a single-mac rebuild must NOT (it heals one
    station's ledger, the flag is global)."""
    from app import insights
    db = db_module
    await db.init_db()
    mac = "TE:MP"
    day0, rows = await _seed_three_days(db, mac)
    await insights.rebuild(mac)

    # The "repair": condemn the 111.0 spike in raw. Rollups still hold it.
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE observations SET tempf = NULL WHERE tempf = 111.0")
        await conn.commit()
    out = await db.records(mac)
    assert out["periods"]["all"]["fields"]["tempf"]["max"] == 111.0, \
        "precondition: covering rollups still serve the repaired spike"

    await db.set_kv("rollups_dirty", "1")
    out = await db.records(mac)
    assert out["periods"]["all"]["fields"]["tempf"]["max"] == 99.5, \
        "dirty flag ignored — the repaired spike is still a displayed record"

    # Single-mac rebuild: flag survives (and so does the raw path).
    await insights.rebuild(mac)
    assert await db.get_kv("rollups_dirty") == "1"

    # Full rebuild: flag clears and the healed rollups serve again.
    await insights.rebuild()
    assert await db.get_kv("rollups_dirty") is None
    out = await db.records(mac)
    assert out["periods"]["all"]["fields"]["tempf"]["max"] == 99.5


@pytest.mark.asyncio
async def test_raw_reads_normalize_lightning_to_int(db_module):
    """CODE_REVIEW_R5 R5-09: the app decodes strike counts as Int?, and the
    raw read paths (/current merge, un-bucketed history) echoed data_json
    verbatim — a REAL 731.0 or a numeric-string "7" from a custom poster
    failed the WHOLE row's decode client-side. Raw reads must serve ints;
    junk degrades to None (absent), never 0."""
    import time
    db = db_module
    await db.init_db()
    mac = "LI:NT"
    now = int(time.time() * 1000)
    await db.insert_observations(mac, [
        {"dateutc": now, "tempf": 90.0,
         "lightning_last_1hr": 731.0, "lightningcount": "7"},
        {"dateutc": now + 1000, "tempf": 90.5,
         "lightning_last_1hr": "junk"},
    ])
    cur = await db.latest_observation(mac)
    assert cur["lightning_last_1hr"] is None      # freshest row had junk
    assert type(cur["lightningcount"]) is int and cur["lightningcount"] == 7
    rows = await db.history(mac, now - 1000, now + 2000)
    first = rows[0]
    assert type(first["lightning_last_1hr"]) is int
    assert first["lightning_last_1hr"] == 731
    assert type(first["lightningcount"]) is int


@pytest.mark.asyncio
async def test_full_rebuild_preserves_a_mid_rebuild_dirty_marker(db_module, monkeypatch):
    """CodeRabbit 2026-08-20: a repair landing WHILE a rebuild scans (its
    rows already behind the cursor) must survive the rebuild's clear —
    otherwise stale maxima serve behind a clean flag. The marker is a
    nonce and the clear is conditional on the scan-start value."""
    from app import insights
    db = db_module
    await db.init_db()
    mac = "TE:MP"
    await _seed_three_days(db, mac)
    await db.set_kv("rollups_dirty", "nonce-A")

    real_scan = insights._rebuild_scan

    async def scan_with_midflight_repair(dbmod, mac_):
        out = await real_scan(dbmod, mac_)
        await dbmod.set_kv("rollups_dirty", "nonce-B")   # repair mid-scan
        return out
    monkeypatch.setattr(insights, "_rebuild_scan", scan_with_midflight_repair)
    await insights.rebuild()
    assert await db.get_kv("rollups_dirty") == "nonce-B", \
        "the rebuild cleared a marker written during its own scan"

    # An undisturbed follow-up rebuild clears it normally.
    monkeypatch.setattr(insights, "_rebuild_scan", real_scan)
    await insights.rebuild()
    assert await db.get_kv("rollups_dirty") is None


@pytest.mark.asyncio
async def test_rebuild_guards_against_concurrent_thinning(db_module, monkeypatch):
    """R11 V6: thin_history refuses while rollups_dirty is set — and that
    refusal is the only thing stopping the daily retention pass from
    advancing the thin watermark under a running rebuild (the scan
    snapshots the watermark once; concurrent thinning silently flattens
    rollup extremes). rebuild() must therefore hold a dirty nonce for the
    scan's duration, and clear its OWN guard afterwards — even a
    single-mac rebuild, whose guard claimed nothing about staleness."""
    from app import insights, maintenance
    # Patch the settings object MAINTENANCE holds — conftest's per-test
    # module reloading can leave app.config.settings a newer object than
    # the one maintenance bound at its import, and patching the newer one
    # leaves the INSIGHTS gate closed (flaked in full-suite runs only).
    monkeypatch.setattr(maintenance.settings, "insights", True)
    db = db_module
    await db.init_db()
    mac = "TE:MP"
    await _seed_three_days(db, mac)
    assert await db.get_kv("rollups_dirty") is None

    real_scan = insights._rebuild_scan
    seen: dict = {}

    from app.config import settings as cur_settings

    async def scan_probing_guard(dbmod, mac_):
        seen["dirty_during_scan"] = await dbmod.get_kv("rollups_dirty")
        # The actual victim: the retention pass firing mid-scan must refuse.
        # db_path passed explicitly — maintenance's own settings binding can
        # be a stale object after conftest's reloads (see the patch above).
        with pytest.raises(RuntimeError, match="dirty"):
            maintenance.thin_history(apply=True, detail_days=365,
                                     keep_minutes=5,
                                     db_path=cur_settings.database_path)
        return await real_scan(dbmod, mac_)

    monkeypatch.setattr(insights, "_rebuild_scan", scan_probing_guard)
    await insights.rebuild(mac)                     # single-mac on purpose
    assert seen["dirty_during_scan"], "no guard nonce held during the scan"
    assert await db.get_kv("rollups_dirty") is None, \
        "the rebuild's own guard must not outlive it"


@pytest.mark.asyncio
async def test_records_week_boundary_is_local_midnight_across_dst(db_module):
    """R12 sub-ledger: week0 is rebuilt from date components, not
    day0 - 6 days, so a DST jump inside the trailing week can't shift the
    boundary an hour off local midnight. Pin the component construction in
    a DST zone and that the boundary actually filters. (Discriminates only
    when a transition sits inside the window — the construction it pins is
    right year-round, same caveat as the rain-tier DST test.)"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    db = db_module
    tz = ZoneInfo("America/New_York")
    await db.init_db()
    mac = "TE:DS"
    now = datetime.now(tz)
    week_date = now.date() - timedelta(days=6)
    week0 = datetime(week_date.year, week_date.month, week_date.day,
                     tzinfo=tz)
    inside = week0 + timedelta(hours=2)
    outside = week0 - timedelta(hours=2)
    await db.insert_observations(mac, [
        {"dateutc": int(outside.timestamp() * 1000), "tempf": 120.0},
        {"dateutc": int(inside.timestamp() * 1000), "tempf": 95.0},
        {"dateutc": int(now.timestamp() * 1000) - 60_000, "tempf": 80.0},
    ])
    out = await db.records(mac, tz_name="America/New_York")
    week = out["periods"]["week"]
    assert week["start_ms"] == int(week0.timestamp() * 1000)
    # The pre-boundary 120.0 belongs to all-time, never the week.
    assert week["fields"]["tempf"]["max"] == 95.0
    assert out["periods"]["all"]["fields"]["tempf"]["max"] == 120.0
