"""The Zambretti daily ledger (2.0): one slide-rule call per station per
local day, filed at the first monitor tick at or after 09:00 station-local
and never revised.

The clock is passed in as `now_ms` and the zone as `tz`, so every test
here puts the tick exactly where it wants it (the `db._now_local` lesson:
a rollover is only testable when the test owns the clock). The zone is
America/Phoenix, seven hours behind UTC with no DST, so a wrong-zone bug
lands the row on the wrong day rather than passing by coincidence.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import zambretti_ledger as ledger  # noqa: E402

MAC = "AA:BB:CC:00:00:ZL"
TZ = ZoneInfo("America/Phoenix")
DAY = date(2026, 8, 30)


def _local_ms(on: date, hour: int, minute: int = 0) -> int:
    return int(datetime(on.year, on.month, on.day, hour, minute,
                        tzinfo=TZ).timestamp() * 1000)


def _seed(db, now_inhg: float, three_h_ago_inhg: float | None,
          at_ms: int, mac: str = MAC) -> None:
    """A reading at `at_ms` and, unless None, one three hours before it."""
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM observations WHERE mac = ?",
                               (mac,))
            await conn.commit()
        rows = [{"dateutc": at_ms, "tempf": 90.0, "baromrelin": now_inhg}]
        if three_h_ago_inhg is not None:
            rows.insert(0, {"dateutc": at_ms - ledger.TREND_MS,
                            "tempf": 80.0, "baromrelin": three_h_ago_inhg})
        await db.insert_observations(mac, rows)
    asyncio.run(run())


@pytest.fixture()
def db(client, monkeypatch):
    from app import db as dbmod
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    return dbmod


def _record(now_ms: int, mac: str = MAC):
    return asyncio.run(ledger.record_today(mac, now_ms, TZ))


def _calls(mac: str = MAC, days: int = 30):
    return asyncio.run(ledger.list_calls(mac, days))


# ───────────────────────── the first tick after nine ─────────────────────────

def test_the_first_tick_at_or_after_nine_local_files_one_row(db):
    from app import derived
    _seed(db, 29.62, 29.74, _local_ms(DAY, 8, 58))     # falling hard
    assert _record(_local_ms(DAY, 8, 59)) is None, "not yet nine"
    assert _calls() == []

    row = _record(_local_ms(DAY, 9, 0))
    assert row is not None
    assert row["day"] == DAY.isoformat()
    assert row["trend"] == "falling"
    _, word = derived.pressure_tendency_code(29.62 - 29.74)
    assert row["call"] == derived.zambretti(29.62 * 33.8639, word)
    assert row["slp_inhg"] == 29.62
    assert row["issued_ms"] == _local_ms(DAY, 8, 58)
    assert _calls() == [row]


def test_later_ticks_the_same_day_never_revise_it(db):
    _seed(db, 29.62, 29.74, _local_ms(DAY, 8, 58))
    first = _record(_local_ms(DAY, 9, 1))
    assert first["trend"] == "falling"
    # The barometer turned around by 09:30. The 09:00 call stands.
    _seed(db, 29.90, 29.70, _local_ms(DAY, 9, 29))
    assert _record(_local_ms(DAY, 9, 30)) is None
    assert _record(_local_ms(DAY, 11, 0)) is None
    assert _calls() == [first]


def test_the_next_day_gets_its_own_row(db):
    _seed(db, 29.62, 29.74, _local_ms(DAY, 8, 58))
    _record(_local_ms(DAY, 9, 0))
    nxt = DAY + timedelta(days=1)
    _seed(db, 29.90, 29.70, _local_ms(nxt, 8, 58))
    row = _record(_local_ms(nxt, 9, 0))
    assert row["day"] == nxt.isoformat() and row["trend"] == "rising"
    assert [r["day"] for r in _calls()] == [DAY.isoformat(), nxt.isoformat()]
    assert [r["day"] for r in _calls(days=1)] == [nxt.isoformat()]


# ───────────────────────── nothing is written when nothing is known ─────

def test_a_day_with_no_pressure_reading_writes_nothing(db):
    async def run():
        await db.insert_observations(MAC, [
            {"dateutc": _local_ms(DAY, 8, 58), "tempf": 90.0}])
    asyncio.run(run())
    assert _record(_local_ms(DAY, 9, 0)) is None
    assert _calls() == []


def test_no_reading_three_hours_back_writes_nothing(db):
    """Zambretti is a function OF the trend: no trend, no call, and never
    a "steady" invented to fill the row."""
    _seed(db, 29.62, None, _local_ms(DAY, 8, 58))
    assert _record(_local_ms(DAY, 9, 0)) is None
    assert _calls() == []


def test_a_station_that_never_reported_writes_nothing(db):
    assert db is not None
    assert _record(_local_ms(DAY, 9, 0)) is None
    assert _calls() == []


def test_a_stale_barometer_is_not_a_morning_call(db):
    """The station went quiet at 01:00. Its last reading is not what the
    barometer said at nine."""
    _seed(db, 29.62, 29.74, _local_ms(DAY, 1, 0))
    assert _record(_local_ms(DAY, 9, 0)) is None
    assert _calls() == []


def test_the_window_closes_at_noon(db):
    """A server dark all morning does not file a "09:00 call" from the
    afternoon barometer."""
    _seed(db, 29.62, 29.74, _local_ms(DAY, 13, 58))
    assert _record(_local_ms(DAY, 14, 0)) is None
    assert _calls() == []
    # …but a station still catching up at 10:30 gets a slightly late one.
    _seed(db, 29.62, 29.74, _local_ms(DAY, 10, 28))
    assert _record(_local_ms(DAY, 10, 30)) is not None


# ───────────────────────── the zone is the station's ─────────────────────────

def test_the_day_is_the_station_local_day_not_utc(db):
    """09:00 Phoenix is 16:00 UTC, and at 23:30 Phoenix it is already
    tomorrow in UTC. The row must carry the Phoenix date either way."""
    _seed(db, 29.62, 29.74, _local_ms(DAY, 8, 58))
    row = _record(_local_ms(DAY, 9, 0))
    assert row["day"] == "2026-08-30"
    utc_day = datetime.fromtimestamp(row["issued_ms"] / 1000,
                                     tz=ZoneInfo("UTC")).date()
    assert utc_day == DAY, "the fixture's 09:00 is the same UTC date"
    # And in the evening, when UTC has rolled over, the window is shut in
    # Phoenix: nothing files for a "tomorrow" the station has not reached.
    nxt = DAY + timedelta(days=1)
    _seed(db, 29.90, 29.70, _local_ms(DAY, 23, 28))
    assert _record(_local_ms(DAY, 23, 30)) is None
    assert [r["day"] for r in _calls()] == [DAY.isoformat()]
    assert nxt.isoformat() not in [r["day"] for r in _calls()]


# ───────────────────────── one helper, two callers ─────────────────────────

def test_the_ledger_and_the_card_read_the_same_barometer(db, monkeypatch):
    """`barometer_says` renders from `compute_call`; the ledger snapshots
    it. The story's headline and the row's call are the same sentence."""
    from app import climate, stories
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: DAY)
    _seed(db, 29.62, 29.74, _local_ms(DAY, 8, 58))
    row = _record(_local_ms(DAY, 9, 0))
    out = asyncio.run(stories.top_stories(
        MAC, families=[stories.FAMILY_SCIENCE], limit=8))
    card = next(s for s in out["stories"]
                if s["story_type"] == "barometer_says")
    assert card["hero_line"] == row["call"].upper()
    assert row["trend"] in card["hero"]["label"]


# ───────────────────────── the tick ─────────────────────────

def test_check_files_every_weather_station_and_survives_a_bad_one(db,
                                                                   monkeypatch):
    other = "AA:BB:CC:00:00:ZM"
    _seed(db, 29.62, 29.74, _local_ms(DAY, 8, 58))
    _seed(db, 29.90, 29.70, _local_ms(DAY, 8, 58), mac=other)
    devices = [{"mac": "AA:BB:CC:00:00:ZX"},         # nothing seeded
               {"mac": MAC},
               {"mac": other, "info": {}},
               {"mac": "AA:BB:CC:00:00:AQ", "info": {"kind": "air"}}]
    real = ledger.record_today
    boom = {"n": 0}

    async def flaky(mac, now_ms, tz):
        if mac == "AA:BB:CC:00:00:ZX":
            boom["n"] += 1
            raise RuntimeError("one bad station")
        return await real(mac, now_ms, tz)
    monkeypatch.setattr(ledger, "record_today", flaky)
    asyncio.run(ledger.check(devices, _local_ms(DAY, 9, 0)))
    assert boom["n"] == 1
    assert [r["trend"] for r in _calls()] == ["falling"]
    assert [r["trend"] for r in _calls(other)] == ["rising"]


def test_the_alert_tick_runs_the_ledger(db, monkeypatch):
    """Hooked beside the forecast snapshotter, bounded the same way."""
    from app import alerts
    seen = []

    async def spy(devices, now_ms):
        seen.append((len(devices), now_ms))
    monkeypatch.setattr(ledger, "check", spy)
    asyncio.run(alerts.AlertMonitor()._tick())
    assert len(seen) == 1
