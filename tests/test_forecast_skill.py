"""The forecast scorecard (2.3): how wrong the forecast tends to be here.

`forecast_snapshots` has archived every forecast AS ISSUED since 1.8, and
until now only one slice of it was ever read. These tests build a small
archive by hand and pin what the scoring claims: bias signed in the
direction the docstring promises, each lead scored on its own calls, only
the model's LAST word before the day counted, and the two ways a day can
be unscorable — the station did not measure it, the model did not call it
— kept out of the denominator instead of dressed up as skill.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

AUTH = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:F0"
TODAY = date(2026, 8, 30)


def _ms(on: date, hour: int = 12) -> int:
    return int(datetime.combine(on, time(hour), tzinfo=timezone.utc)
               .timestamp() * 1000)


def _days(n: int, *, ending: date = TODAY) -> list[date]:
    """The n days before `ending`, oldest first. Never `ending` itself:
    today's high has not happened."""
    return [ending - timedelta(days=i) for i in range(n, 0, -1)]


@pytest.fixture()
def box(client, monkeypatch):
    """A server whose clock is pinned and whose insights are on."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


async def _measured(db, on: date, lo: float | None, hi: float | None,
                    rain: float | None, mac: str = MAC,
                    span_h: float | None = 23.9) -> None:
    """A measured day. `span_h` is how long the station was up that day
    (its first to last observation); the default is a whole day, None
    is a row without a span at all (folded before 2.3)."""
    first = last = None
    if span_h is not None:
        first = _ms(on, 0)
        last = first + int(span_h * 3_600_000)
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO daily_rollups "
            "(mac, day, tempf_min, tempf_max, rain_total, obs_first_ms, obs_last_ms) "
            "VALUES (?,?,?,?,?,?,?)",
            (mac, on.isoformat(), lo, hi, rain, first, last))
        await conn.commit()


async def _filed(db, valid: date, lead: int, *, hi: float, lo: float,
                 pop: float | None = 10.0, issued_hour: int = 12) -> None:
    await db.insert_forecast_snapshots(
        "open-meteo", _ms(valid - timedelta(days=lead), issued_hour),
        [{"valid_date": valid.isoformat(), "lead_days": lead,
          "tmax_f": hi, "tmin_f": lo, "pop": pop, "precip_in": 0.0}])


@pytest.fixture()
def archive(box):
    """Twenty days measured 70/100, called +2°F at lead 1 and +6°F at
    lead 3. That gap between the leads is the whole reason the scorecard
    has a row per lead, so the fixture has to carry it."""
    db = box

    async def go():
        for d in _days(20):
            await _measured(db, d, 70.0, 100.0, 0.0)
            await _filed(db, d, 1, hi=102.0, lo=70.0)
            await _filed(db, d, 3, hi=106.0, lo=70.0)
    asyncio.run(go())
    return db


def _by_lead(card):
    return {row["lead_days"]: row for row in card["leads"]}


def test_bias_is_forecast_minus_measured_and_each_lead_scores_its_own_calls(archive):
    from app import forecast_skill as fsk
    card = asyncio.run(fsk.scorecard(MAC, days=60, today=TODAY))
    assert card["available"] is True
    leads = _by_lead(card)
    one, three = leads[1], leads[3]

    assert one["n"] == 20 and three["n"] == 20
    assert one["enough"] and three["enough"]
    # Forecast minus measured. The model promised more heat than the
    # backyard delivered, so the bias is POSITIVE — a sign flip here is
    # the difference between "runs warm" and "runs cold" on the page.
    assert one["high"]["bias_f"] == pytest.approx(2.0)
    assert three["high"]["bias_f"] == pytest.approx(6.0)
    # A constant error makes bias and mean absolute error agree; they are
    # both reported because a model that is 8° high half the time and 8°
    # low the rest has no bias and is still badly wrong.
    assert one["high"]["mae_f"] == pytest.approx(2.0)
    # A perfect low stays perfect and is not smeared by the high's miss.
    assert one["low"]["bias_f"] == pytest.approx(0.0)
    assert one["low"]["mae_f"] == pytest.approx(0.0)
    # 2°F is inside the three-degree line; 6°F is outside it.
    assert one["high"]["within_3f"] == pytest.approx(1.0)
    assert three["high"]["within_3f"] == pytest.approx(0.0)
    # A lead nobody filed reads as empty, never as a perfect score.
    assert leads[5]["n"] == 0 and leads[5]["enough"] is False
    assert "high" not in leads[5]


def test_today_and_the_days_ahead_are_never_graded(archive):
    """Today's high has not happened yet, and tomorrow's forecast is
    waiting to be graded, not wrong."""
    from app import forecast_skill as fsk
    db = archive

    async def go():
        for d in (TODAY, TODAY + timedelta(days=1)):
            # A 40°F miss: if either day were counted, the bias would move
            # by a mile and this test would see it.
            await _measured(db, d, 70.0, 60.0, 0.0)
            await _filed(db, d, 1, hi=100.0, lo=70.0)
    asyncio.run(go())
    one = _by_lead(asyncio.run(fsk.scorecard(MAC, days=60, today=TODAY)))[1]
    assert one["n"] == 20
    assert one["high"]["bias_f"] == pytest.approx(2.0)


def test_the_models_last_word_before_the_day_is_the_one_scored(archive):
    """Four runs a day file the same lead. Scoring a forecast means
    scoring what the model said last, not what it said first."""
    from app import forecast_skill as fsk
    db = archive
    target = _days(20)[-1]
    asyncio.run(_filed(db, target, 1, hi=120.0, lo=70.0, issued_hour=23))

    one = _by_lead(asyncio.run(fsk.scorecard(MAC, days=60, today=TODAY)))[1]
    assert one["n"] == 20, "the later run replaces the earlier, it does not add a day"
    # Nineteen days at +2 and one at +20.
    assert one["high"]["bias_f"] == pytest.approx((19 * 2.0 + 20.0) / 20, abs=0.01)
    assert one["high"]["worst_day"] == target.isoformat()
    assert one["high"]["worst_error_f"] == pytest.approx(20.0)


def test_a_gaugeless_station_has_no_rain_record_rather_than_a_perfect_one(box):
    """Absent is not zero. A station with no rain gauge must not be
    reported as a long run of dry days the forecast called correctly."""
    from app import forecast_skill as fsk
    db = box

    async def go():
        for d in _days(15):
            await _measured(db, d, 60.0, 80.0, None)   # temps yes, gauge never
            await _filed(db, d, 1, hi=80.0, lo=60.0, pop=5.0)
    asyncio.run(go())
    one = _by_lead(asyncio.run(fsk.scorecard(MAC, days=60, today=TODAY)))[1]
    assert one["n"] == 15        # the temperatures still score
    assert one["rain"] is None   # the rain calls have nothing to grade


def test_rain_calls_tally_four_ways_and_count_only_days_both_sides_spoke(box):
    from app import forecast_skill as fsk
    db = box
    days = _days(12)

    async def go():
        # hit, false alarm, miss, then nine quiet days — and one of those
        # nine has no probability filed, so it must drop out entirely.
        plan = [(0.40, 80.0), (0.00, 80.0), (0.30, 5.0)] + [(0.0, 5.0)] * 9
        for d, (rain, pop) in zip(days, plan):
            await _measured(db, d, 60.0, 80.0, rain)
            await _filed(db, d, 1, hi=80.0, lo=60.0,
                         pop=None if d == days[-1] else pop)
    asyncio.run(go())
    rain = _by_lead(asyncio.run(fsk.scorecard(MAC, days=60, today=TODAY)))[1]["rain"]
    assert rain["hits"] == 1 and rain["false_alarms"] == 1 and rain["misses"] == 1
    assert rain["quiet"] == 8
    assert rain["n"] == 11, "the day with no probability is not a day the model got right"
    assert rain["agreed"] == pytest.approx(9 / 11, abs=0.001)


def test_an_empty_archive_says_so_instead_of_claiming_a_perfect_forecast(box):
    from app import forecast_skill as fsk
    db = box

    async def go():
        for d in _days(5):
            await _measured(db, d, 50.0, 70.0, 0.0)
    asyncio.run(go())
    card = asyncio.run(fsk.scorecard(MAC, days=60, today=TODAY))
    assert card["available"] is False and "nothing to score" in card["reason"]
    # A station with no measured days at all is its own honest answer.
    blank = asyncio.run(fsk.scorecard("ZZ:ZZ:ZZ:ZZ:ZZ:ZZ", days=60, today=TODAY))
    assert blank["available"] is False and blank["leads"] == []


def test_the_route_answers_the_scorecard_and_is_token_gated(client, archive):
    r = client.get(f"/api/devices/{MAC}/forecast-accuracy?days=60", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["provider"] == "open-meteo"
    assert body["window_days"] == 60 and body["close_f"] == 3.0
    assert _by_lead(body)[1]["high"]["bias_f"] == pytest.approx(2.0)
    # Gated like every other /api read (tests/test_security_invariants.py
    # is the enforced copy of the open-route list; this is not on it).
    assert client.get(f"/api/devices/{MAC}/forecast-accuracy").status_code in (401, 403)


def test_the_worst_day_tie_goes_to_the_earlier_day():
    """R23: the comment promised the earlier day and the key picked the
    later one. `errors` is in day order and max() keeps the first maximum."""
    from app.forecast_skill import _side
    side = _side([("2026-08-20", 5.0), ("2026-08-21", -5.0), ("2026-08-22", 2.0)])
    assert side["worst_day"] == "2026-08-20" and side["worst_error_f"] == 5.0
    side = _side([("2026-08-20", 1.0), ("2026-08-21", -6.0), ("2026-08-22", 6.0)])
    assert side["worst_day"] == "2026-08-21" and side["worst_error_f"] == -6.0


def test_a_day_the_station_glimpsed_is_not_a_day_it_measured(box):
    """F01: one noon reading has a minimum and a maximum, and ten of them
    satisfied MIN_DAYS and graded the model on highs, lows and dry days
    nobody observed. A day is scored only when the station's observations
    span MIN_COVER_HOURS; the rest are counted out, by reason."""
    from app import forecast_skill as fs
    db = box

    async def go():
        for d in _days(10):
            await _measured(db, d, 75.0, 75.0, 0.0, span_h=0.0)   # noon only
            await _filed(db, d, 1, hi=95.0, lo=60.0, pop=10.0)
        return await fs.scorecard(MAC, today=TODAY)
    card = asyncio.run(go())
    lead = _by_lead(card)[1]
    assert lead["n"] == 0 and lead["enough"] is False
    assert lead["scored"] == 0 and lead["excluded"] == 10
    assert lead["excluded_reasons"] == [{"reason": fs.EXCL_PARTIAL, "n": 10}]
    assert "high" not in lead and "rain" not in lead, "nothing was graded"
    assert card["available"] is False, "ten glimpses are not a scorecard"


def test_full_days_are_scored_and_short_ones_counted_out_beside_them(box):
    from app import forecast_skill as fs
    db = box
    days = _days(12)

    async def go():
        for i, d in enumerate(days):
            # Ten whole days, one that ran 6 h, one folded before the span
            # columns existed.
            span = 23.5 if i < 10 else (6.0 if i == 10 else None)
            await _measured(db, d, 70.0, 100.0, 0.0, span_h=span)
            await _filed(db, d, 1, hi=102.0, lo=70.0)
        return await fs.scorecard(MAC, today=TODAY)
    card = asyncio.run(go())
    assert card["available"] is True
    assert card["min_cover_hours"] == fs.MIN_COVER_HOURS
    lead = _by_lead(card)[1]
    assert lead["n"] == lead["scored"] == 10 and lead["enough"] is True
    assert lead["excluded"] == 2
    assert {r["reason"]: r["n"] for r in lead["excluded_reasons"]} == {
        fs.EXCL_PARTIAL: 1, fs.EXCL_UNKNOWN: 1}
    assert lead["high"]["bias_f"] == pytest.approx(2.0)
    assert lead["rain"]["n"] == 10, "rain is graded on the covered days only"
    # The keys the app decodes are all still there.
    for k in ("lead_days", "n", "enough", "first_day", "last_day", "high", "low", "rain"):
        assert k in lead


def test_a_full_day_from_the_ledger_itself_covers_the_scorecard(box):
    """Through the real fold: a station posting through the day gets a
    span from the rollup and is scored; the review's noon-only shape is
    not. Neither row was written by hand."""
    from app import forecast_skill as fs, insights
    db = box
    full, glimpse = _days(2)

    async def go():
        rows = [{"dateutc": _ms(full, h), "tempf": 70.0 + h, "dailyrainin": 0.0}
                for h in range(0, 24)]
        rows += [{"dateutc": _ms(glimpse, 12), "tempf": 75.0, "dailyrainin": 0.0}]
        await db.insert_observations(MAC, rows)
        for d in (full, glimpse):
            await _filed(db, d, 1, hi=95.0, lo=70.0)
        return await fs.scorecard(MAC, today=TODAY)
    card = asyncio.run(go())
    lead = _by_lead(card)[1]
    assert lead["scored"] == 1 and lead["first_day"] == full.isoformat()
    assert lead["excluded_reasons"] == [{"reason": fs.EXCL_PARTIAL, "n": 1}]


# ───────────────────────── the 2026-09-16 mutation witnesses ────────────────
# Four one-expression mutations of forecast_skill survived this file: MAE
# without the absolute value, an exclusive close threshold, a strict rain
# threshold, a strict day floor. Each boundary is pinned here so they die.

def test_mae_cannot_cancel_opposite_signed_errors():
    """Errors of -3 and +3 have no bias and a typical miss of 3, and only
    reporting both says so (the docstring's own example)."""
    from app.forecast_skill import _side
    side = _side([("2026-09-01", -3.0), ("2026-09-02", 3.0)])
    assert side["bias_f"] == 0.0
    assert side["mae_f"] == 3.0


def test_an_error_of_exactly_three_degrees_is_within_three():
    from app.forecast_skill import _side, CLOSE_F
    assert _side([("2026-09-01", CLOSE_F)])["within_3f"] == 1.0
    assert _side([("2026-09-01", -CLOSE_F)])["within_3f"] == 1.0
    assert _side([("2026-09-01", CLOSE_F + 0.01)])["within_3f"] == 0.0


def test_a_fifty_percent_call_with_rain_is_a_hit():
    """The model called rain AT the threshold and it rained: a hit, not a
    miss. RAIN_POP is inclusive on both the scorecard and the story."""
    from app.forecast_skill import _rain, RAIN_POP
    tally = _rain(["2026-09-01"], {"2026-09-01": {"pop": RAIN_POP}},
                  {"2026-09-01": {"rain_total": 0.1}})
    assert tally["hits"] == 1 and tally["misses"] == 0
    dry = _rain(["2026-09-01"], {"2026-09-01": {"pop": RAIN_POP - 0.5}},
                {"2026-09-01": {"rain_total": 0.1}})
    assert dry["misses"] == 1 and dry["hits"] == 0


def test_exactly_min_days_of_covered_days_reach_the_threshold(box):
    """Ten fully covered, matched days are enough; nine are not."""
    from app import forecast_skill as fs
    db = box
    days = _days(fs.MIN_DAYS)

    async def go(n):
        for d in days[:n]:
            await _measured(db, d, 70.0, 100.0, 0.0)
            await _filed(db, d, 1, hi=102.0, lo=70.0)
        return await fs.scorecard(MAC, today=TODAY)
    short = _by_lead(asyncio.run(go(fs.MIN_DAYS - 1)))[1]
    assert short["n"] == fs.MIN_DAYS - 1 and short["enough"] is False
    full = _by_lead(asyncio.run(go(fs.MIN_DAYS)))[1]
    assert full["n"] == full["scored"] == fs.MIN_DAYS and full["enough"] is True
