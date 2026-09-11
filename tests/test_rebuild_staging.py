"""The 2.1 rollup rebuild: fold into staging, swap at the end.

Before this the rebuild's first statement deleted the live rollups, so
every Insights card vanished for the whole run, a crashed rebuild left
an empty ledger, and a fixed half-second pause was an 80% duty cycle on
a slow shared CPU. Each test here pins one of those.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

MAC = "AA:BB:CC:00:00:5A"
OTHER = "AA:BB:CC:00:00:5B"


def _ms(year, month, day, hour_local, tz="America/Phoenix") -> int:
    return int(datetime(year, month, day, hour_local,
                        tzinfo=ZoneInfo(tz)).timestamp() * 1000)


@pytest.fixture
def engine(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    return db


async def _count(db, table: str, mac: str | None = None) -> int:
    async with db.connect() as conn:
        if mac:
            cur = await conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE mac = ?", (mac,))
        else:
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
        return (await cur.fetchone())[0]


async def _seed(db, mac=MAC, days=3, per_day=4):
    rows = []
    for d in range(days):
        for h in range(per_day):
            rows.append({"dateutc": _ms(2025, 10, 5 + d, 7 + 3 * h),
                         "tempf": 70.0 + d + h, "feelsLike": 70.0 + d,
                         "humidity": 40.0})
    await db.insert_observations(mac, rows)
    return len(rows)


def test_the_live_ledger_never_empties_during_a_rebuild(engine, monkeypatch):
    """The whole point: while the scan runs, daily_rollups still holds
    every day it held before. Batches of two rows, and the pause between
    batches (where the old code had already deleted everything) is where
    the check runs."""
    from app import insights
    monkeypatch.setattr(insights, "REBUILD_BATCH_ROWS", 2)
    seen: list[int] = []
    armed = {"on": False, "loop": None}

    async def spy_sleep(_s):
        # The patch is global: the app's own background loops (lifespan
        # tasks on the TestClient's loop) sleep through it too. Only the
        # rebuild's own loop counts.
        if armed["on"] and asyncio.get_running_loop() is armed["loop"]:
            seen.append(await _count(engine, "daily_rollups", MAC))
    monkeypatch.setattr(insights.asyncio, "sleep", spy_sleep)

    async def run():
        armed["loop"] = asyncio.get_running_loop()
        n = await _seed(engine)
        before = await _count(engine, "daily_rollups", MAC)
        armed["on"] = True
        stats = await insights.rebuild()
        armed["on"] = False
        return n, before, stats, await _count(engine, "daily_rollups", MAC)
    n, before, stats, after = asyncio.run(run())
    assert before == 3 and after == 3 and stats["rows"] == n
    assert len(seen) >= 3, "the batch pause never ran"
    assert all(c == 3 for c in seen), f"live rows dipped mid-rebuild: {seen}"


def test_a_failed_rebuild_leaves_the_live_ledger_and_no_staging(engine, monkeypatch):
    from app import insights
    monkeypatch.setattr(insights, "REBUILD_BATCH_ROWS", 2)
    calls = {"n": 0, "armed": False}
    real = insights.rollup_params

    def explode(row, tz):
        if calls["armed"]:
            calls["n"] += 1
            if calls["n"] == 5:
                raise RuntimeError("disk on fire")
        return real(row, tz)
    monkeypatch.setattr(insights, "rollup_params", explode)

    async def run():
        await _seed(engine)
        before = await _count(engine, "daily_rollups", MAC)
        calls["armed"] = True
        with pytest.raises(RuntimeError, match="disk on fire"):
            await insights.rebuild()
        after = await _count(engine, "daily_rollups", MAC)
        async with engine.connect() as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_staging'")
            leftovers = [r[0] for r in await cur.fetchall()]
        return before, after, leftovers
    before, after, leftovers = asyncio.run(run())
    assert before == 3 and after == 3, "a failed rebuild emptied the ledger"
    assert leftovers == []
    assert insights.in_flight() is None


def test_rows_that_arrive_during_the_scan_are_folded_exactly_once(engine, monkeypatch):
    """Live ingest keeps folding into the live tables during a rebuild;
    those folds are replaced by the swap, which catches up on everything
    behind each station's cursor. A reading for station A that lands
    while station B is being scanned is behind A's cursor and would have
    been LOST by the old code (deleted up front, never re-scanned); it
    ends up counted once, not zero times and not twice."""
    from app import insights
    monkeypatch.setattr(insights, "REBUILD_BATCH_ROWS", 2)
    late_ts = _ms(2025, 10, 9, 12)
    state = {"first": None, "target": None, "inserted": None, "loop": None}

    async def inject(_s):
        # Only from the rebuild's own loop (the patch is global and the
        # app's background tasks sleep through it too).
        if asyncio.get_running_loop() is not state["loop"]:
            return
        p = insights._PROGRESS or {}
        if p.get("phase") != "fold":
            return
        if state["first"] is None:
            state["first"] = p["mac"]
        elif p["mac"] != state["first"] and state["target"] is None:
            state["target"] = state["first"]          # its scan is over
            state["inserted"] = await engine.insert_observations(
                state["target"],
                [{"dateutc": late_ts, "tempf": 99.0, "feelsLike": 99.0}])
    monkeypatch.setattr(insights.asyncio, "sleep", inject)

    async def run():
        state["loop"] = asyncio.get_running_loop()
        n = await _seed(engine, MAC) + await _seed(engine, OTHER)
        stats = await insights.rebuild()
        async with engine.connect() as conn:
            cur = await conn.execute(
                "SELECT tempf_n, tempf_max FROM daily_rollups "
                "WHERE mac = ? AND day = '2025-10-09'", (state["target"],))
            row = await cur.fetchone()
            cur = await conn.execute(
                "SELECT SUM(tempf_n) FROM hour_rollups WHERE mac = ?",
                (state["target"],))
            hours_n = (await cur.fetchone())[0]
        return n, stats, tuple(row) if row else None, hours_n
    n, stats, day, hours_n = asyncio.run(run())
    assert state["target"] in (MAC, OTHER) and state["inserted"] == 1
    assert stats["rows"] == n + 1
    assert day == (1, 99.0)
    assert hours_n == 12 + 1


def test_thinned_days_ride_through_the_swap_untouched(engine):
    """Days behind the thin watermark keep only sampled raw; their daily
    rows are the surviving record and must survive a rebuild byte for
    byte, while days on or after the watermark are recomputed."""
    from app import insights

    async def run():
        await _seed(engine)                       # 10-05, 10-06, 10-07
        wm = _ms(2025, 10, 6, 0)
        await engine.set_kv("history_thin_before_ms", str(wm))
        async with engine.connect() as conn:
            # A value no raw row could produce, on a preserved day and on
            # a recomputed one.
            await conn.execute(
                "UPDATE daily_rollups SET tempf_max = 999 WHERE mac = ? "
                "AND day IN ('2025-10-05', '2025-10-07')", (MAC,))
            await conn.commit()
        await insights.rebuild()
        async with engine.connect() as conn:
            cur = await conn.execute(
                "SELECT day, tempf_max FROM daily_rollups WHERE mac = ? "
                "ORDER BY day", (MAC,))
            return [tuple(r) for r in await cur.fetchall()]
    rows = asyncio.run(run())
    assert rows[0] == ("2025-10-05", 999.0), "preserved day was recomputed"
    assert rows[1][0] == "2025-10-06" and rows[1][1] < 999
    assert rows[2] == ("2025-10-07", 75.0), "day after the watermark kept the bad value"


def test_a_single_station_rebuild_leaves_the_other_station_alone(engine):
    from app import insights

    async def run():
        await _seed(engine, MAC)
        await _seed(engine, OTHER, days=2)
        async with engine.connect() as conn:
            await conn.execute(
                "UPDATE daily_rollups SET tempf_max = 999 WHERE mac = ?",
                (OTHER,))
            await conn.commit()
        await insights.rebuild(MAC)
        async with engine.connect() as conn:
            cur = await conn.execute(
                "SELECT MIN(tempf_max), MAX(tempf_max) FROM daily_rollups "
                "WHERE mac = ?", (OTHER,))
            other = tuple(await cur.fetchone())
            cur = await conn.execute(
                "SELECT MAX(tempf_max) FROM daily_rollups WHERE mac = ?", (MAC,))
            mine = (await cur.fetchone())[0]
            cur = await conn.execute(
                "SELECT COUNT(*) FROM hour_rollups WHERE mac = ?", (OTHER,))
            other_hours = (await cur.fetchone())[0]
        return other, mine, other_hours
    other, mine, other_hours = asyncio.run(run())
    assert other == (999.0, 999.0)
    assert mine < 999
    assert other_hours > 0


def test_catch_up_overflow_marks_the_ledger_dirty(engine, monkeypatch):
    """More rows behind a cursor than one short transaction may fold:
    the rest is missing from the ledger, so say so (records read raw and
    the rebuild hint fires) instead of serving it as complete."""
    from app import insights
    monkeypatch.setattr(insights, "REBUILD_BATCH_ROWS", 2)
    monkeypatch.setattr(insights, "REBUILD_CATCHUP_MAX", 1)
    state = {"first": None, "done": False, "loop": None}

    async def inject(_s):
        if asyncio.get_running_loop() is not state["loop"]:
            return
        p = insights._PROGRESS or {}
        if p.get("phase") != "fold":
            return
        if state["first"] is None:
            state["first"] = p["mac"]
        elif p["mac"] != state["first"] and not state["done"]:
            state["done"] = True
            await engine.insert_observations(state["first"], [
                {"dateutc": _ms(2025, 10, 9, h), "tempf": 80.0}
                for h in (9, 10, 11)])
    monkeypatch.setattr(insights.asyncio, "sleep", inject)

    async def run():
        state["loop"] = asyncio.get_running_loop()
        await _seed(engine, MAC)
        await _seed(engine, OTHER)
        await insights.rebuild()
        return await engine.get_kv("rollups_dirty")
    dirty = asyncio.run(run())
    assert state["done"]
    assert dirty and dirty.startswith("rebuild-catchup-overflow-")


def test_the_pause_scales_with_the_batch():
    from app import insights
    assert insights.rebuild_pause_s(0.0) == insights.REBUILD_BATCH_PAUSE_S
    assert insights.rebuild_pause_s(0.1) == insights.REBUILD_BATCH_PAUSE_S
    # An 8 s batch on a stolen CPU earns a 16 s yield: the writer is held
    # for a third of wall time at most, however slow the box.
    assert insights.rebuild_pause_s(8.0) == pytest.approx(16.0)
    assert insights.REBUILD_IDLE_RATIO >= 2.0


def test_staging_twins_carry_the_primary_key_and_altered_columns(engine):
    from app import insights

    async def run():
        async with engine.connect() as conn:
            await insights._create_staging(conn)
            cur = await conn.execute("PRAGMA table_info(daily_rollups_staging)")
            cols = {r[1]: r[5] for r in await cur.fetchall()}   # name -> pk
            # The upsert's ON CONFLICT(mac, day) needs the key. Params come
            # from rollup_params so this test cannot drift from the column
            # list (2.2 widened it).
            from zoneinfo import ZoneInfo
            def params(tempf: float) -> dict:
                p = insights.rollup_params(
                    {"dateutc": 1_735_776_000_000, "tempf": tempf}, ZoneInfo("UTC"))
                for k in ("_year", "_month", "_hour"):
                    p.pop(k)
                p["mac"] = MAC
                return p
            await conn.execute(insights._UPSERT_DAILY_STAGING, params(70.0))
            await conn.execute(insights._UPSERT_DAILY_STAGING, params(75.0))
            cur = await conn.execute(
                "SELECT tempf_n, tempf_max FROM daily_rollups_staging")
            merged = tuple(await cur.fetchone())
            await insights.drop_staging(conn)
            await conn.commit()
        return cols, merged
    cols, merged = asyncio.run(run())
    assert cols["mac"] == 1 and cols["day"] == 2
    assert "lightning_max" in cols
    assert "co2_n" in cols and "humidity_sum" in cols     # 2.2 columns ride along
    assert merged == (2, 75.0)
    with pytest.raises(ValueError):
        insights.staging_table("observations")


def test_boot_sweeps_a_staging_twin_left_by_a_killed_rebuild(engine):
    async def run():
        async with engine.connect() as conn:
            await conn.execute("CREATE TABLE hour_rollups_staging (x INTEGER)")
            await conn.commit()
        await engine.init_db()
        async with engine.connect() as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_staging'")
            return [r[0] for r in await cur.fetchall()]
    assert asyncio.run(run()) == []


def test_open_transactions_name_the_statement_and_clear_on_commit(engine):
    """The watchdog's new line: which SQL is holding the writer, and for
    how long. SELECTs under autocommit are not transactions."""
    async def run():
        async with engine.connect() as conn:
            await conn.execute("SELECT 1")
            idle = engine.open_transactions()
            await conn.execute(
                "INSERT INTO server_kv (k, v) VALUES ('probe', '1') "
                "ON CONFLICT(k) DO UPDATE SET v = '1'")
            held = engine.open_transactions()
            await conn.commit()
            done = engine.open_transactions()
        return idle, held, done
    idle, held, done = asyncio.run(run())
    assert idle == []
    assert len(held) == 1 and held[0]["sql"].startswith("INSERT INTO server_kv")
    assert held[0]["open_s"] >= 0
    assert done == []


def test_the_thread_dump_names_open_sql_and_the_rebuild(engine, monkeypatch, caplog):
    import faulthandler
    from app import insights, main as M
    # The stack dump itself needs a real stderr fd, which pytest's capture
    # does not provide; the lines under test go through the logger.
    monkeypatch.setattr(faulthandler, "dump_traceback", lambda **kw: None)
    monkeypatch.setattr(insights, "_PROGRESS", {
        "phase": "fold", "mac": MAC, "rows": 1234, "cursor_ms": 5,
        "batch_started": 0.0})
    monkeypatch.setattr(engine, "open_transactions", lambda: [
        {"open_s": 41.0, "sql": "INSERT INTO daily_rollups_staging (mac, day"}])
    with caplog.at_level("ERROR"):
        M.dump_all_threads("test")
    err = caplog.text
    assert "open transaction 41.0s: INSERT INTO daily_rollups_staging" in err
    assert "insights rebuild in flight: phase=fold mac=" + MAC in err
    assert "rows=1234" in err
