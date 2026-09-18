"""The day's rain as the SUM OF RISES (2.3, `daily_rollups.yearly_rise`).

A lifetime counter's day is `last - first` — right until the counter
resets twice, or resets and then climbs past where it restarted. Both
read short. Adding up every positive step and ignoring every drop is
right whatever the counter did, and that is what this column holds.

The rules pinned here are the ones that make it safe to PREFER over the
span: a drop contributes nothing, a corrupt step is refused, a day folded
out of order abandons the column rather than reporting a short sum, and a
station with no gauge gets NULL and never 0.0.

`ref_rain_counters` is the standing account of why this family keeps
biting; nothing here should be relaxed without reading it.
"""
from __future__ import annotations

import asyncio

import pytest

from app.day_rain import (day_rain_in, day_rain_provenance, RISE_MAX_IN,
                          PROVENANCE_DAILY, PROVENANCE_RISE,
                          PROVENANCE_SPAN, PROVENANCE_SIGNATURE,
                          PROVENANCE_NONE)

MAC = "AA:BB:CC:00:00:R1"
NOON = 1_756_728_000_000                      # 2026-09-01 12:00 UTC


@pytest.fixture()
def rollups(client, monkeypatch):
    from app import config, db, insights
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)
    return db, insights


def fold(db, insights, readings, mac: str = MAC, daily=None):
    """Fold (offset_minutes, counter) readings IN THE ORDER GIVEN. `daily`
    sets dailyrainin on every row (a console that reports both)."""
    rows = [{"dateutc": NOON + int(m * 60_000), "yearlyrainin": v,
             **({"dailyrainin": daily} if daily is not None else {})}
            for m, v in readings]

    async def run():
        async with db.connect() as conn:
            await insights.update_rollups(conn, mac, rows)
            await conn.commit()
            return await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?", (mac,))).fetchone()
    return asyncio.run(run())


# ── the thing the span gets wrong ──────────────────────────────────────

def test_two_resets_in_a_day(rollups):
    """The case `last - first` cannot do. The gauge is swapped twice; the
    day really caught 0.20 + 0.30 + 0.15."""
    db, insights = rollups
    r = fold(db, insights, [
        (0, 10.00), (10, 10.20),        # +0.20, then the gauge is replaced
        (20, 0.00), (30, 0.30),         # +0.30, and replaced again
        (40, 0.00), (50, 0.15),         # +0.15
    ])
    assert r["yearly_rise"] == pytest.approx(0.65)
    assert day_rain_in(r) == pytest.approx(0.65)
    # The span would have said 0.15 — the restarted counter's own total.
    assert r["yearly_last"] - 0 == pytest.approx(0.15)


def test_a_reset_then_more_rain_than_the_old_total(rollups):
    """One reset, but the day carries on past where the counter restarted.
    The span gets this one right only by accident when the restart total
    happens to equal the day's rain; here it does not."""
    db, insights = rollups
    r = fold(db, insights, [(0, 5.00), (10, 5.40), (20, 0.00), (30, 0.10)])
    assert r["yearly_rise"] == pytest.approx(0.50)      # 0.40 + 0.10
    assert day_rain_in(r) == pytest.approx(0.50)


def test_an_ordinary_day_agrees_with_the_span(rollups):
    """No reset: the sum of the steps IS last - first. The two rules must
    not disagree on the ordinary case, or every day would change value the
    moment the column landed."""
    db, insights = rollups
    r = fold(db, insights, [(0, 10.00), (10, 10.05), (20, 10.20), (30, 10.25)])
    assert r["yearly_rise"] == pytest.approx(0.25)
    assert r["yearly_last"] - r["yearly_first"] == pytest.approx(0.25)
    assert day_rain_in(r) == pytest.approx(0.25)


def test_a_dry_day_is_zero_and_a_gaugeless_day_is_nothing(rollups):
    """Absent is not zero. A working gauge that caught nothing reads 0.00;
    a station with no gauge at all reads None, and the two must never be
    the same answer."""
    db, insights = rollups
    dry = fold(db, insights, [(0, 12.00), (10, 12.00), (20, 12.00)])
    assert dry["yearly_rise"] == 0.0
    assert day_rain_in(dry) == 0.0

    rows = [{"dateutc": NOON, "tempf": 70.0},
            {"dateutc": NOON + 60_000, "tempf": 71.0}]

    async def run():
        async with db.connect() as conn:
            await insights.update_rollups(conn, "AA:BB:CC:00:00:R2", rows)
            await conn.commit()
            return await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?",
                ("AA:BB:CC:00:00:R2",))).fetchone()
    bare = asyncio.run(run())
    assert bare["yearly_rise"] is None
    assert day_rain_in(bare) is None


def test_a_corrupt_step_is_refused_not_absorbed(rollups):
    """A counter that jumps a season between two readings is broken, not
    wet. The bad step is dropped; the good ones on either side survive."""
    db, insights = rollups
    r = fold(db, insights, [
        (0, 1.00), (10, 1.20),                    # +0.20, real
        (20, 1.20 + RISE_MAX_IN + 5), (30, 1.20 + RISE_MAX_IN + 5.10),
    ])
    # The jump contributes nothing; the 0.10 climb after it still counts.
    assert r["yearly_rise"] == pytest.approx(0.30)


def test_an_out_of_order_fold_abandons_the_column(rollups):
    """A history import or a resumed relay folds rows out of order, and a
    running sum cannot see the steps it missed. Rather than report a short
    total that looks authoritative, the column goes NULL and the day falls
    back to the span — which is ordered by reading time and correct."""
    db, insights = rollups
    r = fold(db, insights, [(0, 12.40), (-240, 12.10), (180, 12.65), (60, 12.50)])
    assert r["yearly_rise"] is None
    assert day_rain_in(r) == pytest.approx(0.55)      # the span answered
    assert day_rain_provenance(r) == PROVENANCE_SPAN


def test_a_late_row_cannot_restart_the_sum(rollups):
    """Once abandoned, a day stays abandoned for this fold. A later
    in-order row restarting the sum from zero would understate the day a
    second way, and look confident doing it."""
    db, insights = rollups
    r = fold(db, insights, [(0, 12.40), (-240, 12.10), (300, 13.00)])
    assert r["yearly_rise"] is None


def test_the_rebuild_scans_in_order_and_fills_it_in(rollups):
    """The repair for an abandoned day, through the real rebuild.

    Rows are STORED out of order, which is what a history import or a
    resumed relay does, so the live fold abandons the column. The rebuild
    reads `observations` in timestamp order per station, sees every step,
    and the day comes back with a number.
    """
    db, insights = rollups
    mac = "AA:BB:CC:00:00:R3"
    # Two deliveries: the relay posts +0 and +20 live, then resends the
    # +10 and +30 it had buffered. Within ONE delivery the fold sorts by
    # time (2.3, see insert_observations), so the disorder has to come
    # from the second batch reaching back behind the first.
    first = [(0, 10.00), (20, 0.00)]
    later = [(10, 10.20), (30, 0.30)]

    def rows(batch):
        return [{"dateutc": NOON + m * 60_000, "yearlyrainin": v, "tempf": 70.0}
                for m, v in batch]

    async def go():
        assert await db.insert_observations(mac, rows(first)) == 2
        assert await db.insert_observations(mac, rows(later)) == 2
        async with db.connect() as conn:
            before = await (await conn.execute(
                "SELECT yearly_rise FROM daily_rollups WHERE mac = ?",
                (mac,))).fetchone()
        # The 10.20 at +10 arrived after the 0.00 at +20 had been folded,
        # so the running sum gave up.
        assert before is not None and before["yearly_rise"] is None
        await insights.rebuild(mac)
        async with db.connect() as conn:
            return await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?", (mac,))).fetchone()
    r = asyncio.run(go())
    assert r["yearly_rise"] == pytest.approx(0.50)     # 0.20 + 0.30
    assert day_rain_in(r) == pytest.approx(0.50)
    assert day_rain_provenance(r) == PROVENANCE_RISE


def test_a_batch_delivered_newest_first_still_folds_in_time_order(rollups):
    """A poller catching up, a relay resending a backlog, an import: one
    insert carrying several new rows, in whatever order the source sent
    them. The fold sorts the batch by timestamp before folding, so a
    reset inside it is read as the rises either side of it and the day
    is not abandoned. Before 2.3 the batch folded in SET order, and a day
    delivered this way lost its rise on every such batch."""
    db, insights = rollups
    mac = "AA:BB:CC:00:00:R4"
    newest_first = [(30, 0.30), (20, 0.00), (10, 10.20), (0, 10.00)]
    rows = [{"dateutc": NOON + m * 60_000, "yearlyrainin": v, "tempf": 70.0}
            for m, v in newest_first]

    async def go():
        assert await db.insert_observations(mac, rows) == 4
        async with db.connect() as conn:
            return await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?", (mac,))).fetchone()
    r = asyncio.run(go())
    assert r["yearly_rise"] == pytest.approx(0.50)
    assert day_rain_provenance(r) == PROVENANCE_RISE


def test_a_database_from_the_previous_release_gains_the_column(tmp_path,
                                                               monkeypatch):
    """The migration, from the OLD schema.

    Every rollup test starts from an empty file, and an empty file is the
    one shape that cannot catch a migration bug — that is exactly how the
    map directory's index reached production broken (ref_map_directory_
    outage). So this builds a daily_rollups WITHOUT yearly_rise, the way a
    2.2 server has it, and checks that opening it adds the column and
    marks the rollups dirty so the rebuild fills it in.
    """
    import sqlite3
    from app import config, db as dbmod, insights

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    # The 2.2 shape: the late columns that existed then, and not the new one.
    con.execute("""CREATE TABLE daily_rollups (
        mac TEXT NOT NULL, day TEXT NOT NULL,
        tempf_min REAL, tempf_max REAL, tempf_sum REAL, tempf_n INTEGER,
        rain_total REAL, yearly_min REAL, yearly_max REAL,
        yearly_first REAL, yearly_first_ms INTEGER,
        yearly_last REAL, yearly_last_ms INTEGER,
        PRIMARY KEY (mac, day))""")
    con.execute("INSERT INTO daily_rollups (mac, day, yearly_first, yearly_last)"
                " VALUES (?, ?, ?, ?)", (MAC, "2026-09-01", 10.0, 10.25))
    con.commit()
    con.close()

    monkeypatch.setattr(config.settings, "database_path", str(path))
    monkeypatch.setattr(dbmod.settings, "database_path", str(path))
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)

    async def go():
        await dbmod.init_db()
        async with dbmod.connect() as conn:
            cols = {r[1] for r in await (await conn.execute(
                "PRAGMA table_info(daily_rollups)")).fetchall()}
            row = await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?", (MAC,))).fetchone()
            dirty = await (await conn.execute(
                "SELECT v FROM server_kv WHERE k = 'rollups_dirty'")).fetchone()
        return cols, row, dirty
    cols, row, dirty = asyncio.run(go())

    assert "yearly_rise" in cols, "the ALTER did not run on an existing database"
    assert {"obs_first_ms", "obs_last_ms"} <= cols, "the 2.3 span columns (F01)"
    assert row["obs_first_ms"] is None, "an old row's coverage is unknown, not zero"
    # The existing day reads NULL, not 0.0 — it was never measured this
    # way, and a zero would be a claim the row cannot support.
    assert row["yearly_rise"] is None
    # Which is why it must still answer, via the span it does have.
    assert day_rain_in(row) == pytest.approx(0.25)
    assert day_rain_provenance(row) == PROVENANCE_SPAN
    # And why the rollups are marked dirty: the rebuild is what fills the
    # column in for history.
    assert dirty is not None, "a new rollup column must mark the ledger dirty"


# ── the midnight step (F02) ────────────────────────────────────────────

MIDNIGHT = NOON + 12 * 60 * 60_000            # 2026-09-02 00:00 UTC (tz=UTC)


def _days(db, mac):
    async def run():
        async with db.connect() as conn:
            return [dict(r) for r in await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ? ORDER BY day",
                (mac,))).fetchall()]
    return asyncio.run(run())


def test_the_midnight_step_is_credited_to_the_day_it_ends_on(rollups):
    """F02: 23:59 at 10.00, 00:01 at 10.20. The new day's first reading
    used to start its sum at 0.0, so the 0.20 in that fell across
    midnight appeared in NEITHER day. It is the second day's rain."""
    db, insights = rollups
    mac = "AA:BB:CC:00:00:F2"
    fold(db, insights, [(719, 10.00), (721, 10.20), (722, 10.20)], mac=mac)
    d1, d2 = _days(db, mac)
    assert (d1["day"][5:], d2["day"][5:]) == ("09-01", "09-02")
    assert d1["yearly_rise"] == pytest.approx(0.0)
    assert d2["yearly_rise"] == pytest.approx(0.20)
    assert day_rain_in(d1) + day_rain_in(d2) == pytest.approx(0.20)
    assert day_rain_provenance(d2) == PROVENANCE_RISE


def test_the_rebuild_credits_the_midnight_step_the_same_way(rollups):
    """Live fold and rebuild must agree: the rebuild folds each station in
    time order into staging, and the previous day it looks back at is the
    staging row, not the live one it is replacing."""
    db, insights = rollups
    mac = "AA:BB:CC:00:00:F3"
    rows = [{"dateutc": MIDNIGHT + off, "yearlyrainin": v, "tempf": 70.0}
            for off, v in ((-60_000, 10.00), (60_000, 10.20), (120_000, 10.20))]

    async def go():
        assert await db.insert_observations(mac, rows) == 3
        async with db.connect() as conn:
            live = [dict(r) for r in await (await conn.execute(
                "SELECT day, yearly_rise FROM daily_rollups WHERE mac = ? ORDER BY day",
                (mac,))).fetchall()]
        await insights.rebuild(mac)
        async with db.connect() as conn:
            rebuilt = [dict(r) for r in await (await conn.execute(
                "SELECT day, yearly_rise FROM daily_rollups WHERE mac = ? ORDER BY day",
                (mac,))).fetchall()]
        return live, rebuilt
    live, rebuilt = asyncio.run(go())
    assert [r["yearly_rise"] for r in live] == pytest.approx([0.0, 0.20])
    assert rebuilt == live


def test_a_reset_across_midnight_credits_only_the_climb_after_it(rollups):
    """23:59 at 10.00, 00:01 at 0.05 (the counter was reset overnight),
    00:02 at 0.10. A drop is not a rise, so the new day starts at zero
    and only the 0.05 it climbed afterwards is rain."""
    db, insights = rollups
    mac = "AA:BB:CC:00:00:F4"
    fold(db, insights, [(719, 10.00), (721, 0.05), (722, 0.10)], mac=mac)
    d1, d2 = _days(db, mac)
    assert d1["yearly_rise"] == pytest.approx(0.0)
    assert d2["yearly_rise"] == pytest.approx(0.05)


def test_a_gap_of_days_credits_nothing(rollups):
    """Only the CALENDAR day before counts as the previous reading. A
    station silent for three days may have seen rain, a reset, or a
    replaced gauge; the step across the gap is not a measurement."""
    db, insights = rollups
    mac = "AA:BB:CC:00:00:F5"
    fold(db, insights, [(0, 10.00), (3 * 24 * 60, 10.20), (3 * 24 * 60 + 1, 10.20)], mac=mac)
    d1, d4 = _days(db, mac)
    assert (d1["day"][5:], d4["day"][5:]) == ("09-01", "09-04")
    assert d1["yearly_rise"] == pytest.approx(0.0)
    assert d4["yearly_rise"] == pytest.approx(0.0)


def test_the_midnight_step_obeys_the_rate_gate(rollups):
    """A console set at 00:01 (10.00 -> 11.00 in two minutes) is not rain
    across midnight any more than it is inside a day."""
    db, insights = rollups
    mac = "AA:BB:CC:00:00:F6"
    fold(db, insights, [(719, 10.00), (721, 11.00), (722, 11.00)], mac=mac)
    d1, d2 = _days(db, mac)
    assert d2["yearly_rise"] == pytest.approx(0.0)


def test_the_observation_span_is_recorded(rollups):
    """F01: the day's first and last observation, any field, so the
    forecast scorecard can tell a covered day from a glimpse."""
    db, insights = rollups
    mac = "AA:BB:CC:00:00:F7"
    fold(db, insights, [(30, 1.0), (0, 1.0), (600, 1.0)], mac=mac)
    (d,) = _days(db, mac)
    assert d["obs_first_ms"] == NOON and d["obs_last_ms"] == NOON + 600 * 60_000


# ── precedence and provenance ──────────────────────────────────────────

def test_the_stations_own_daily_counter_still_wins():
    """A station that reports dailyrainin needs no arithmetic at all, and
    its own number outranks anything derived from the lifetime counter."""
    row = {"rain_total": 0.42, "yearly_rise": 0.10,
           "yearly_first": 1.0, "yearly_last": 3.0,
           "yearly_min": 1.0, "yearly_max": 3.0}
    assert day_rain_in(row) == 0.42
    assert day_rain_provenance(row) == PROVENANCE_DAILY


def test_the_rules_rank_in_order_of_how_much_they_know():
    rise = {"rain_total": None, "yearly_rise": 0.65,
            "yearly_first": 10.0, "yearly_last": 0.15,
            "yearly_min": 0.0, "yearly_max": 10.2}
    assert day_rain_in(rise) == pytest.approx(0.65)
    assert day_rain_provenance(rise) == PROVENANCE_RISE

    span = {"rain_total": None, "yearly_first": 10.00, "yearly_last": 10.25}
    assert day_rain_in(span) == pytest.approx(0.25)
    assert day_rain_provenance(span) == PROVENANCE_SPAN

    # Pre-2.2 rows have only the signature, and it still refuses a reset.
    sig = {"rain_total": None, "yearly_min": 0.05, "yearly_max": 42.10}
    assert day_rain_in(sig) is None
    assert day_rain_provenance(sig) == PROVENANCE_SIGNATURE

    assert day_rain_provenance({}) == PROVENANCE_NONE
    assert day_rain_provenance(None) == PROVENANCE_NONE


def test_an_absurd_day_is_unknown_rather_than_reported():
    """Whatever the rule, a day bigger than anywhere on earth has ever
    recorded is a broken counter and reads as unknown."""
    assert day_rain_in({"yearly_rise": 500.0}) is None
    assert day_rain_in({"yearly_rise": 0.0}) == 0.0


# ── the rate gate: a manual counter set is not rain (R23) ──────────────

def test_the_davis_console_set_of_2026_08_10_is_not_rain(rollups):
    """The real shape: the yearly counter went 0 -> 0.34 in ONE one-minute
    step at 21:07 local with the daily counter at 0 all day. A manual
    console set; ingest accepts it as a level shift once the next reading
    confirms, and the fold used to credit it as 0.34 in of rain."""
    db, insights = rollups
    r = fold(db, insights, [(0, 0.0), (1, 0.34), (2, 0.34), (3, 0.34)], daily=0.0)
    assert r["yearly_rise"] == pytest.approx(0.0)
    assert r["yearly_last"] == pytest.approx(0.34), "the level still moves"
    # The daily counter wins anyway on this station; the point is the
    # counter-only station, where nothing hides it.
    assert day_rain_in(r) == pytest.approx(0.0)


def test_the_davis_console_set_of_2026_05_24_is_not_rain(rollups):
    """14.6 -> 0 (01:18) -> 0.73 (01:26): the drop adds nothing and the
    0.73 in eight minutes does not fit the time it took."""
    db, insights = rollups
    r = fold(db, insights, [(0, 14.6), (18 - 0, 0.0), (26, 0.73), (30, 0.73)])
    assert r["yearly_rise"] == pytest.approx(0.0)
    assert day_rain_in(r) == pytest.approx(0.0)


def test_a_cloudburst_is_credited(rollups):
    """0.30 in over five minutes is 3.6 in/hr — rare, real, and inside
    the allowance (2.0 in/hr x 5 min + 0.25 in slack = 0.42 in)."""
    db, insights = rollups
    r = fold(db, insights, [(0, 3.10), (5, 3.40), (10, 3.45)])
    assert r["yearly_rise"] == pytest.approx(0.35)


def test_a_wh24_posting_every_16_seconds_through_a_3_in_hr_storm_is_credited_in_full(rollups):
    """A counter-only station at its real cadence: 0.01 in steps every
    16 s (with a 0.02 now and then, the counter's resolution) through a
    3 in/hr storm. Every step is far inside the allowance, so the day
    reads the storm's full inch."""
    db, insights = rollups
    readings = []
    for i in range(0, 76):                          # 75 x 16 s = 20 min
        t_min = i * 16 / 60
        readings.append((t_min, round(20.00 + 3.0 * t_min / 60, 2)))
    r = fold(db, insights, readings)
    assert r["yearly_rise"] == pytest.approx(1.0, abs=0.011)
    assert day_rain_in(r) == pytest.approx(r["yearly_last"] - 20.0)


def test_the_fold_gate_and_ingests_level_shift_gate_share_their_constants():
    from app import ingest, insights, day_rain, config
    assert day_rain.RATE_SLACK_IN == 0.25
    assert config.settings.ingest_max_rain_rate_in_per_hr == day_rain.RATE_MAX_IN_PER_HR_DEFAULT
    # What ingest calls a glitch (then a level shift once confirmed) is
    # exactly what the fold refuses to credit: the boundary agrees.
    rate = insights.rain_rate_max_in_per_hr()
    assert rate == 2.0
    step_ok, step_bad = 0.28, 0.29                  # one minute apart
    assert not ingest._is_rain_glitch(step_ok, 1 / 60, rate)
    assert ingest._is_rain_glitch(step_bad, 1 / 60, rate)
    assert step_ok <= rate * (1 / 60) + day_rain.RATE_SLACK_IN < step_bad


def test_the_fold_version_bump_marks_the_rollups_dirty_exactly_once(tmp_path, monkeypatch):
    """R23: the gate changes what history folds to, so a ledger folded
    under the old rule is rebuilt ONCE — the next boot, with the version
    recorded, leaves a clean ledger alone."""
    import sqlite3
    from app import config, db as dbmod, insights

    path = tmp_path / "v1.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE daily_rollups (
        mac TEXT NOT NULL, day TEXT NOT NULL,
        tempf_min REAL, tempf_max REAL, tempf_sum REAL, tempf_n INTEGER,
        rain_total REAL, yearly_min REAL, yearly_max REAL,
        yearly_first REAL, yearly_first_ms INTEGER,
        yearly_last REAL, yearly_last_ms INTEGER, yearly_rise REAL,
        PRIMARY KEY (mac, day))""")
    con.execute("INSERT INTO daily_rollups (mac, day, yearly_first, yearly_last, yearly_rise)"
                " VALUES (?, ?, ?, ?, ?)", (MAC, "2026-08-10", 0.0, 0.34, 0.34))
    con.commit()
    con.close()
    monkeypatch.setattr(config.settings, "database_path", str(path))
    monkeypatch.setattr(dbmod.settings, "database_path", str(path))
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)

    async def boot():
        await dbmod.init_db()
        return (await dbmod.get_kv("rollups_dirty"),
                await dbmod.get_kv(insights.ROLLUP_FOLD_VERSION_KEY))
    dirty, ver = asyncio.run(boot())
    assert dirty is not None and ver == str(insights.ROLLUP_FOLD_VERSION)
    # The rebuild (or the operator) clears the marker; the next boot
    # must not put it back.
    asyncio.run(dbmod.set_kv("rollups_dirty", None))
    dirty2, ver2 = asyncio.run(boot())
    assert dirty2 is None and ver2 == ver
    # A fresh database records the version and is never marked dirty.
    fresh = tmp_path / "fresh.db"
    monkeypatch.setattr(config.settings, "database_path", str(fresh))
    monkeypatch.setattr(dbmod.settings, "database_path", str(fresh))
    dirty3, ver3 = asyncio.run(boot())
    assert dirty3 is None and ver3 == ver
