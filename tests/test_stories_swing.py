"""Story engine (2.0): the "Biggest Swing" producer.

The card is two facts, and the second is the one people do not expect: the
month's WIDEST high-to-low day, and its NARROWEST. Wide is a clear, dry,
still night radiating heat away; narrow is cloud, humidity or a storm
holding the air in place. Both come from daily_rollups, which already
stores tempf_min and tempf_max per day, so nothing here touches an
observation.

THE TRAP THIS SUITE EXISTS FOR: a swing is a DIFFERENCE and converts by
scale alone, while the high and low it was measured between are READINGS
and carry the offset. A 30°F swing is 16.7°C. Run it through the reading
conversion and it becomes −1.1°C — a number that is not wrong by a little,
it is a different kind of quantity. Every unit assertion below checks both
kinds in the same story.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:57"
TODAY = date(2026, 8, 30)


def _seed(db, rows, mac: str = MAC) -> None:
    """(day, high, low). A None low is a day that measured only one end."""
    async def run():
        async with db.connect() as conn:
            for day, hi, lo in rows:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mac, day, lo, hi, hi, 1))
            await conn.commit()
    asyncio.run(run())


def _history(first: date, last: date, hi: float = 90.0,
             lo: float = 70.0) -> list[tuple[str, float, float]]:
    """A flat 20°F-swing background, so anything the tests add stands out."""
    out, d = [], first
    while d <= last:
        out.append((d.isoformat(), hi, lo))
        d += timedelta(days=1)
    return out


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _swing(mac: str = MAC, **kw) -> dict | None:
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_RECORDS], limit=12, **kw))
    return next((s for s in out["stories"]
                 if s["story_type"] == "biggest_swing"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


@pytest.fixture()
def august(engine):
    """A year of flat 20° days, then an August holding a 45° day and a 6°
    one — the two the card is about."""
    _seed(engine, _history(date(2025, 9, 1), date(2026, 7, 31)))
    _seed(engine, _history(date(2026, 8, 1), date(2026, 8, 30)))
    _seed(engine, [("2026-08-12", 100.0, 55.0),    # the 45° day
                   ("2026-08-19", 84.0, 78.0)])    # the 6° day
    return engine


# ───────────────────────── the happy path ─────────────────────────

def test_it_leads_with_the_widest_day_of_the_month(august):
    s = _swing()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("records", "biggest_swing")
    assert s["title"] == "Biggest Swing"
    assert s["id"] == "records.biggest_swing.2026-08"
    assert s["hero_line"] == "45°F IN ONE DAY"
    assert s["hero"]["value"] == 45.0
    assert _stat(s, "high")["value"] == 100.0
    assert _stat(s, "low")["value"] == 55.0
    assert s["period"] == {"kind": "month", "label": "August 2026 so far",
                           "start": "2026-08-01", "end": "2026-08-30",
                           "partial": True}


def test_the_narrow_day_is_the_half_that_tells_you_something(august):
    """The spec's point: the small swing reveals the humid/cloudy/stormy
    days, so it is a supporting stat and a marked bar, not a footnote."""
    s = _swing()
    narrow = _stat(s, "narrowest_swing")
    assert narrow["value"] == 6.0
    assert "August 19" in narrow["label"]
    marked = {b["note"]: b["key"] for b in s["viz"]["series"] if b["note"]}
    assert marked == {"widest": "2026-08-12", "narrowest": "2026-08-19"}
    assert "narrowest day" in s["context"]
    assert "cloud, damp or a storm" in s["context"]


def test_the_median_is_the_baseline_not_the_mean(august):
    """A mean is dragged upward by exactly the outlier this card is about,
    so the comparison would flatter every hero it ever picked."""
    s = _swing()
    c = s["comparison"]
    assert c["kind"] == "vs_station_typical"
    assert c["baseline"] == 20.0            # the flat background, unmoved
    assert c["value"] == 45.0
    assert c["delta"] == 25.0
    assert c["direction"] == "above"
    assert (c["rank"], c["of"]) == (1, 364)
    assert c["rank_line"] == "the widest of 364 days on record"
    assert _stat(s, "typical_swing")["value"] == 20.0


# ───────────────── the units trap, both kinds in one story ─────────────────

def test_a_swing_converts_by_scale_and_a_reading_by_offset(august):
    """The whole reason this producer keeps two conversions."""
    c = _swing(units=stories.Units(temperature="celsius"))
    # 45°F of SWING is 25°C of swing — scale only.
    assert c["hero"]["value"] == pytest.approx(25.0, abs=0.05)
    assert c["hero_line"] == "25°C IN ONE DAY"
    # …and emphatically NOT the reading conversion, which would say 7.2.
    assert c["hero"]["value"] != pytest.approx((45.0 - 32) * 5 / 9, abs=0.05)
    # The high and low ARE readings and carry the offset.
    assert _stat(c, "high")["value"] == pytest.approx(37.8, abs=0.05)
    assert _stat(c, "low")["value"] == pytest.approx(12.8, abs=0.05)
    # The bar the card draws must agree with the stats it prints.
    hero_bar = next(b for b in c["viz"]["series"] if b["hero"])
    assert hero_bar["swing"] == pytest.approx(25.0, abs=0.05)
    assert hero_bar["high"] == pytest.approx(37.8, abs=0.05)
    assert hero_bar["low"] == pytest.approx(12.8, abs=0.05)
    # A reader subtracting the drawn ends gets the drawn length.
    assert hero_bar["high"] - hero_bar["low"] == pytest.approx(
        hero_bar["swing"], abs=0.05)
    # Both the median and the delta are differences too.
    assert c["comparison"]["baseline"] == pytest.approx(11.1, abs=0.05)
    assert c["comparison"]["delta"] == pytest.approx(13.9, abs=0.05)


# ───────────────────────── decline paths ─────────────────────────

def test_a_station_with_only_one_end_declines(engine):
    """Highs but no lows is not a station whose days never swung."""
    _seed(engine, [(d, hi, None) for d, hi, _ in
                   _history(date(2025, 9, 1), date(2026, 8, 30))])
    assert _swing() is None


def test_too_little_history_to_know_a_typical_day_declines(engine):
    """"Bigger than typical" needs a typical, and a fortnight is not one."""
    _seed(engine, _history(date(2026, 8, 1), date(2026, 8, 30)))
    assert _swing() is None


def test_a_thin_newest_month_declines(engine):
    _seed(engine, _history(date(2025, 9, 1), date(2026, 7, 31)))
    _seed(engine, _history(date(2026, 8, 1), date(2026, 8, 4)))
    assert _swing() is None


def test_a_perfectly_flat_station_has_a_maximum_but_no_story(engine):
    """Some day has to be the widest. On a record where every day swung the
    same amount, that day beat nothing, and "20°F in one day" over a year of
    20°F days is a calendar entry — the same judgment the wildest-day
    producer makes when nothing clears its floor."""
    _seed(engine, _history(date(2025, 9, 1), date(2026, 8, 30)))
    assert _swing() is None
    # One day that actually stands out is all it takes to have something
    # to say — the guard is "beat nothing", not "beat the median".
    _seed(engine, [("2026-08-14", 92.0, 70.0)])
    s = _swing()
    assert s is not None and s["hero"]["value"] == 22.0
    assert s["score_parts"]["standout"] > 0.0


def test_the_month_is_the_newest_with_data_not_the_calendar_one(engine):
    """A station that stopped reporting in June still has a real June
    story; inventing an empty August one is the zero bug in a costume."""
    _seed(engine, _history(date(2025, 7, 1), date(2026, 6, 20)))
    _seed(engine, [("2026-06-11", 105.0, 60.0)])
    s = _swing()
    assert s is not None
    assert s["id"] == "records.biggest_swing.2026-06"
    assert s["period"]["partial"] is False, "June is over"
    assert s["period"]["label"] == "June 2026"


# ───────────────────────── calibration ─────────────────────────

def test_a_dramatic_month_outscores_a_flat_one_on_the_same_scale(august):
    dramatic = _swing()
    assert 0.0 <= dramatic["interestingness"] <= 1.0
    assert set(dramatic["score_parts"]) == {"magnitude", "contrast", "standout"}
    assert dramatic["score_parts"]["standout"] == 1.0      # widest on record

    # A flat month on the same record: no day stands out from the 20° norm.
    _seed(august, _history(date(2026, 8, 1), date(2026, 8, 30), 92.0, 70.0))
    flat = _swing()
    assert 0.0 <= flat["interestingness"] <= 1.0
    assert flat["interestingness"] < dramatic["interestingness"]
    assert flat["score_parts"]["contrast"] < dramatic["score_parts"]["contrast"]
