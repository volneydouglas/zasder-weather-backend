"""The /current rain periods read from the ledger (2.3, R22-11's second half).

`rain_rollups` used to answer today / this week / this month / this year
by differencing the lifetime counter against its value at each boundary,
reset-aware through the lowest reading since (`_yearly_rise_since`) and
memoised for six hours (`_YEAR_PRIOR_CACHE`). That rule is right for zero
or one reset and short for two, or for a reset the counter then climbed
past — the shapes `daily_rollups.yearly_rise` was added to get right.

Now the four periods are the sum of `day_rain_in` over the ledger's rows
(`_rain_ledger_periods`), one primary-key range read; the hour keeps the
raw rule; the raw rule stays as the fallback wherever the ledger cannot
answer. Every test here pins the ledger's answer AGAINST the raw one, so
the file says where the two agree and where the ledger corrects it.

The shapes are the ones in ref_rain_counters: a normal day, one reset,
two resets, a reset then more rain than the counter reached, a gauge-less
station (None, never 0.0), a dry day (0.0), a manual counter set (credited
nothing), and the year-to-date on January 2 after a January 1 reset.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

UTC = ZoneInfo("UTC")
# 2026-09-02 12:00 UTC, a Wednesday: the week began Sunday 08-30, the
# month on 09-01, so every period has finished days before today.
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TODAY = NOW.replace(hour=0, minute=0)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture()
def ledger(client, monkeypatch):
    """A server maintaining its ledger, clock pinned to NOW. The client
    fixture reloads app.db, so this patches the module the test sees."""
    from app import config, db, insights
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)
    monkeypatch.setattr(db, "_now_local", lambda tz: NOW.astimezone(tz))
    return db


def seed(db, mac, readings, **extra):
    """Store (datetime, counter) readings, folding them in time order the
    way a live station delivers them."""
    rows = [{"dateutc": _ms(t), "yearlyrainin": v, "tempf": 70.0, **extra}
            for t, v in sorted(readings, key=lambda r: r[0])]
    assert asyncio.run(db.insert_observations(mac, rows)) == len(rows)


def periods(db, mac):
    return asyncio.run(db.rain_rollups(mac, "UTC"))


def raw_since(db, mac, boundary: datetime):
    """The pre-2.3 rule for a period: the counter's rise since `boundary`,
    reset-aware, straight from the raw observations."""
    async def go():
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT yearlyrainin FROM observations WHERE mac = ? "
                "ORDER BY dateutc_ms DESC LIMIT 1", (mac,))).fetchone()
        return await db._yearly_rise_since(mac, row["yearlyrainin"], _ms(boundary))
    return asyncio.run(go())


def ledger_answered(db, mac):
    """Which periods the ledger itself answered for this station."""
    return asyncio.run(db._rain_ledger_periods(
        mac, UTC, TODAY, {
            "daily_in": TODAY,
            "weekly_in": TODAY - timedelta(days=3),
            "monthly_in": TODAY.replace(day=1),
            "yearly_in": TODAY.replace(month=1, day=1)}))


# ── where the two rules agree ──────────────────────────────────────────

def test_an_ordinary_day_reads_the_same_from_the_ledger_and_the_counter(ledger):
    """No reset: the sum of the steps IS the counter's rise since
    midnight. The ledger must not move a number the raw rule had right."""
    db = ledger
    mac = "AA:BB:CC:00:01:01"
    seed(db, mac, [(TODAY - timedelta(hours=8), 10.00),     # yesterday 16:00
                   (TODAY + timedelta(minutes=10), 10.00),
                   (TODAY + timedelta(hours=3), 10.05),
                   (TODAY + timedelta(hours=9), 10.20),
                   (NOW - timedelta(minutes=10), 10.25)])
    out = periods(db, mac)
    assert out["daily_in"] == pytest.approx(0.25)
    assert out["daily_in"] == pytest.approx(raw_since(db, mac, TODAY))
    assert set(ledger_answered(db, mac)) == {
        "daily_in", "weekly_in", "monthly_in", "yearly_in"}
    # And it was the ledger, not the memo: nothing anchored to a past
    # instant was cached on a healthy server.
    assert db._YEAR_PRIOR_CACHE == {}


def test_a_dry_day_is_zero_on_a_working_gauge(ledger):
    """The counter did not move today: 0.00, the same as the raw rule."""
    db = ledger
    mac = "AA:BB:CC:00:01:02"
    seed(db, mac, [(TODAY - timedelta(hours=8), 12.00),
                   (TODAY + timedelta(minutes=10), 12.00),
                   (NOW - timedelta(minutes=10), 12.00)])
    out = periods(db, mac)
    assert out["daily_in"] == 0.0 and out["weekly_in"] == 0.0
    assert out["daily_in"] == raw_since(db, mac, TODAY)


def test_a_gaugeless_station_is_none_never_zero(ledger):
    """No rain counter at all: every period is None, from either rule.
    The ledger has rows for the station (temperature folded every day)
    and must not read them as dry days."""
    db = ledger
    mac = "AA:BB:CC:00:01:03"
    rows = [{"dateutc": _ms(t), "tempf": 70.0}
            for t in (TODAY - timedelta(hours=8), TODAY + timedelta(hours=1),
                      NOW - timedelta(minutes=10))]
    assert asyncio.run(db.insert_observations(mac, rows)) == 3
    out = periods(db, mac)
    assert all(out[k] is None for k in
               ("hourly_in", "daily_in", "weekly_in", "monthly_in", "yearly_in")), out
    assert ledger_answered(db, mac) == {}


def test_a_day_the_gauge_was_silent_counts_nothing_and_breaks_no_period(ledger):
    """The counter dropped off the radio for a day while the thermometer
    kept posting: that day's row has no rain columns. It is not a hole in
    the ledger (the station is known to carry a counter, and a day it did
    not move is dry), so the week still answers from the ledger."""
    db = ledger
    mac = "AA:BB:CC:00:01:04"
    seed(db, mac, [(TODAY - timedelta(days=2, hours=8), 3.00),
                   (TODAY - timedelta(days=2, hours=2), 3.10)])
    quiet = [{"dateutc": _ms(TODAY - timedelta(days=1, hours=12)), "tempf": 70.0}]
    assert asyncio.run(db.insert_observations(mac, quiet)) == 1
    seed(db, mac, [(TODAY + timedelta(minutes=10), 3.10),
                   (NOW - timedelta(minutes=10), 3.30)])
    out = periods(db, mac)
    assert "weekly_in" in ledger_answered(db, mac)
    assert out["weekly_in"] == pytest.approx(0.30)
    assert out["daily_in"] == pytest.approx(0.20)


# ── where the ledger corrects the raw rule ─────────────────────────────

def test_one_reset_inside_the_day(ledger):
    """10.00 -> 10.20, the gauge is replaced, 0.00 -> 0.30. The day caught
    0.50. The raw rule re-differences from the lowest reading after the
    reset and reports 0.30; the ledger reports what fell."""
    db = ledger
    mac = "AA:BB:CC:00:01:11"
    seed(db, mac, [(TODAY - timedelta(hours=8), 10.00),
                   (TODAY + timedelta(minutes=10), 10.00),
                   (TODAY + timedelta(hours=2), 10.20),
                   (TODAY + timedelta(hours=4), 0.00),
                   (TODAY + timedelta(hours=6), 0.30),
                   (NOW - timedelta(minutes=10), 0.30)])
    out = periods(db, mac)
    assert raw_since(db, mac, TODAY) == pytest.approx(0.30)
    assert out["daily_in"] == pytest.approx(0.50)
    assert out["weekly_in"] == pytest.approx(0.50)


def test_two_resets_in_a_day(ledger):
    """The case last - first cannot do and the raw rule cannot either:
    0.20 + 0.30 + 0.15 fell; the raw rule sees the restarted counter's
    own 0.15."""
    db = ledger
    mac = "AA:BB:CC:00:01:12"
    seed(db, mac, [(TODAY - timedelta(hours=8), 10.00),
                   (TODAY + timedelta(minutes=10), 10.00),
                   (TODAY + timedelta(hours=1), 10.20),
                   (TODAY + timedelta(hours=2), 0.00),
                   (TODAY + timedelta(hours=3), 0.30),
                   (TODAY + timedelta(hours=4), 0.00),
                   (TODAY + timedelta(hours=5), 0.15),
                   (NOW - timedelta(minutes=10), 0.15)])
    out = periods(db, mac)
    assert raw_since(db, mac, TODAY) == pytest.approx(0.15)
    assert out["daily_in"] == pytest.approx(0.65)


def test_a_reset_then_more_rain_than_the_counter_reached(ledger):
    """5.00 -> 5.40, reset, 0.00 -> 0.10: 0.50 fell. The raw rule reports
    the 0.10 the restarted counter holds."""
    db = ledger
    mac = "AA:BB:CC:00:01:13"
    seed(db, mac, [(TODAY - timedelta(hours=8), 5.00),
                   (TODAY + timedelta(minutes=10), 5.00),
                   (TODAY + timedelta(hours=2), 5.40),
                   (TODAY + timedelta(hours=4), 0.00),
                   (TODAY + timedelta(hours=6), 0.10),
                   (NOW - timedelta(minutes=10), 0.10)])
    out = periods(db, mac)
    assert raw_since(db, mac, TODAY) == pytest.approx(0.10)
    assert out["daily_in"] == pytest.approx(0.50)


def test_a_manual_counter_set_is_credited_nothing(ledger):
    """The Davis console shape of 2026-08-10: 0 -> 0.34 in one minute,
    then flat. The raw rule reported it as today's rain on a counter-only
    station; the ledger's rate gate refuses the step and today reads dry,
    which is what the day's rollup already said."""
    db = ledger
    mac = "AA:BB:CC:00:01:14"
    seed(db, mac, [(TODAY - timedelta(hours=8), 0.00),
                   (TODAY + timedelta(minutes=10), 0.00),
                   (TODAY + timedelta(hours=2), 0.00),
                   (TODAY + timedelta(hours=2, minutes=1), 0.34),
                   (TODAY + timedelta(hours=2, minutes=2), 0.34),
                   (NOW - timedelta(minutes=10), 0.34)])
    out = periods(db, mac)
    assert raw_since(db, mac, TODAY) == pytest.approx(0.34)
    assert out["daily_in"] == 0.0


def test_year_to_date_on_january_2_after_a_january_1_reset(ledger, monkeypatch):
    """A lifetime counter at 16.00 going into the year. January 1: +0.20,
    then the gauge is replaced (16.20 -> 0.00), then +0.30. January 2 so
    far: +0.15. The year has seen 0.65. The raw rule anchors at the
    January 1 prior (16.00), sees the drop, re-differences from the
    lowest reading since and answers 0.45 — the pre-reset 0.20 is lost.
    The week (from Sunday 12-28) and the month agree with the year."""
    db = ledger
    jan2 = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_local", lambda tz: jan2.astimezone(tz))
    jan1 = datetime(2026, 1, 1, tzinfo=UTC)
    mac = "AA:BB:CC:00:01:15"
    seed(db, mac, [(jan1 - timedelta(hours=8), 16.00),          # Dec 31
                   (jan1 + timedelta(minutes=10), 16.00),
                   (jan1 + timedelta(hours=1), 16.20),
                   (jan1 + timedelta(hours=2), 0.00),
                   (jan1 + timedelta(hours=3), 0.30),
                   (jan1 + timedelta(hours=23, minutes=50), 0.30),
                   (jan1 + timedelta(days=1, minutes=10), 0.30),  # Jan 2
                   (jan2 - timedelta(hours=1), 0.45)])
    out = periods(db, mac)
    assert raw_since(db, mac, jan1) == pytest.approx(0.45)
    assert out["yearly_in"] == pytest.approx(0.65)
    assert out["monthly_in"] == pytest.approx(0.65)
    assert out["weekly_in"] == pytest.approx(0.65)
    assert out["daily_in"] == pytest.approx(0.15)


# ── the raw rule is still there for what the ledger cannot answer ──────

def _reset_shape(db, mac):
    """One reset today (true 0.50, raw 0.30): the number tells which rule
    answered."""
    seed(db, mac, [(TODAY - timedelta(hours=8), 10.00),
                   (TODAY + timedelta(minutes=10), 10.00),
                   (TODAY + timedelta(hours=2), 10.20),
                   (TODAY + timedelta(hours=4), 0.00),
                   (TODAY + timedelta(hours=6), 0.30),
                   (NOW - timedelta(minutes=10), 0.30)])


def test_a_dirty_ledger_steps_aside(ledger):
    """While a rebuild is filling the column in, the raw rule answers,
    memoised as before — that is the window the cache still exists for."""
    db = ledger
    mac = "AA:BB:CC:00:01:21"
    _reset_shape(db, mac)
    asyncio.run(db.set_kv("rollups_dirty", "1"))
    out = periods(db, mac)
    assert ledger_answered(db, mac) == {}
    assert out["daily_in"] == pytest.approx(0.30)
    assert any(k[2] == "prior" for k in db._YEAR_PRIOR_CACHE), \
        "the fallback's boundary lookups are memoised"


def test_a_frozen_ledger_steps_aside(ledger, monkeypatch):
    """INSIGHTS turned off after the ledger was built: observations keep
    landing, the table stops. It is not current, so it does not answer."""
    from app import config, insights
    db = ledger
    mac = "AA:BB:CC:00:01:22"
    seed(db, mac, [(TODAY - timedelta(hours=8), 10.00),
                   (TODAY + timedelta(minutes=10), 10.00)])
    monkeypatch.setattr(config.settings, "insights", False)
    monkeypatch.setattr(insights.settings, "insights", False)
    seed(db, mac, [(TODAY + timedelta(hours=2), 10.20),
                   (TODAY + timedelta(hours=4), 0.00),
                   (TODAY + timedelta(hours=6), 0.30)])
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)
    assert ledger_answered(db, mac) == {}
    assert periods(db, mac)["daily_in"] == pytest.approx(0.30)


def test_another_zone_steps_aside(ledger):
    """`rain_rollups` honours its caller's zone. The ledger is folded in
    the server's; a day string from another zone is a different day, so
    the raw rule answers such a call."""
    db = ledger
    mac = "AA:BB:CC:00:01:23"
    _reset_shape(db, mac)
    # The year, because in Phoenix (UTC-7) the reset happened yesterday
    # and today's raw and ledger figures legitimately agree on 0.0.
    assert periods(db, mac)["yearly_in"] == pytest.approx(0.50)
    phoenix = asyncio.run(db.rain_rollups(mac, "America/Phoenix"))
    assert phoenix["yearly_in"] == pytest.approx(0.30)


def test_a_row_the_ledger_cannot_price_drops_only_its_periods(ledger):
    """A pre-2.2 row inside the week (min/max only, the reset signature)
    answers "unknown". A sum with that hole is short and looks complete,
    so the week and everything longer fall back to the raw rule; today,
    which the row is not in, still comes from the ledger."""
    db = ledger
    mac = "AA:BB:CC:00:01:24"
    _reset_shape(db, mac)

    async def age_a_row():
        async with db.connect() as conn:
            await conn.execute(
                "UPDATE daily_rollups SET yearly_first = NULL, yearly_last = NULL, "
                "yearly_rise = NULL, yearly_min = 0.05, yearly_max = 42.0 "
                "WHERE mac = ? AND day = ?",
                (mac, (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")))
            await conn.commit()
    asyncio.run(age_a_row())
    answered = ledger_answered(db, mac)
    assert set(answered) == {"daily_in"}
    out = periods(db, mac)
    assert out["daily_in"] == pytest.approx(0.50)
    assert out["weekly_in"] is not None      # raw rule, not a silent None


def test_the_ledger_reads_use_the_primary_key_and_touch_no_observations(ledger):
    """The /current rule: bounded and indexed. The one range read over the
    ledger is a seek on (mac, day), its primary key. Plan shape, not
    speed: a five-row suite cannot tell."""
    db = ledger

    async def plan():
        async with db.connect() as conn:
            cur = await conn.execute("EXPLAIN QUERY PLAN " + db._RAIN_LEDGER_SQL,
                                     ("AA:BB", "2026-01-01", "2026-09-02"))
            return [r["detail"] for r in await cur.fetchall()]
    details = asyncio.run(plan())
    assert any("SEARCH daily_rollups USING" in d and "mac=? AND day>?" in d
               for d in details), details
    assert not any(d.startswith("SCAN") for d in details), details
    assert "observations" not in db._RAIN_LEDGER_SQL


# ── records: the floor stays, and the lifetime counter never reaches it ──

def test_records_never_read_a_reset_lifetime_counter_as_a_wettest_day(ledger):
    """Item 3 of the retirement, verified rather than assumed: records
    read the day's rain as `rain_total` / MAX(dailyrainin) and not through
    `day_rain_in`, so a station that posts only a lifetime counter has no
    wettest-day record (absent, not invented), and a reset in that
    counter — 10.0 -> 10.5 -> 0 -> 0.2, R22-11's own example — can never
    surface as 10.5. `_DAILY_RAIN_RESET_FLOOR_IN` guards a different
    thing (a lifetime counter posted under the DAILY name) and stays."""
    from app import insights
    db = ledger
    mac = "AA:BB:CC:00:01:31"
    seed(db, mac, [(TODAY - timedelta(hours=8), 10.0),
                   (TODAY + timedelta(minutes=10), 10.0),
                   (TODAY + timedelta(hours=2), 10.5),
                   (TODAY + timedelta(hours=4), 0.0),
                   (TODAY + timedelta(hours=6), 0.2),
                   (NOW - timedelta(minutes=10), 0.2)])
    asyncio.run(insights.rebuild(mac))
    recs = asyncio.run(db.records(mac, "UTC"))
    for pname, period in recs["periods"].items():
        dr = period["fields"].get("dailyrainin")
        assert dr is None or dr["max"] is None, (pname, dr)
    # The ledger itself knows the day was 0.70, for the screens that read it.
    assert periods(db, mac)["daily_in"] == pytest.approx(0.70)


def test_a_daily_counter_station_reads_the_ledger_too(client, monkeypatch):
    """2.4 item 12: tier 3 (a Tempest, whose only counter is the day's)
    used to re-scan roughly half a million index rows per /current. The
    ledger already has a per-day figure and tier 1 has read it since
    2.3, so this one does too — with the scan kept as the fallback for
    everything the ledger cannot answer."""
    import asyncio

    from app import db

    asked = {"ledger": 0}
    real = db._rain_ledger_periods

    async def counting(mac, tz, start_of_today, boundaries):
        asked["ledger"] += 1
        return await real(mac, tz, start_of_today, boundaries)

    monkeypatch.setattr(db, "_rain_ledger_periods", counting)

    async def run():
        db._DAILY_ROLLUP_CACHE.clear()
        await db._rollups_from_daily("AA:BB:CC:00:00:99",
                                     __import__("zoneinfo").ZoneInfo("UTC"))
    asyncio.run(run())
    # It ASKS, whatever the answer: a station with no ledger falls
    # through to the scan, which is the point of keeping both.
    assert asked["ledger"] == 1
