"""Story engine (2.0): "How Hard Did the AC Work?" — degree days, translated.

Almost nobody knows what a degree day is, which is the design constraint:
the card has to TEACH the unit in the same breath it quotes one, and the
lesson and the number have to be in the same scale. "One degree for every
degree above 65°F" printed over a Celsius total is two units in one
sentence, so the base converts as a READING and the total converts as the
sum of DIFFERENCES it is.

The producer leads with whichever side actually worked. Chandler leads with
cooling by a factor of fifty; Duluth leads with heating. A card that always
led with cooling would be an Arizona card pretending to be a feature, so
both directions are asserted below.

ONE definition: climate.degree_days, the same function the Science surface
and the insights rollups use. Nothing here re-implements max(0, mean - 65).
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:DD"
TODAY = date(2026, 8, 30)


def _seed(db, rows, mac: str = MAC) -> None:
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


def _span(first: date, last: date, hi: float, lo: float):
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


def _dd(mac: str = MAC, **kw) -> dict | None:
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_SCIENCE], limit=12, **kw))
    return next((s for s in out["stories"]
                 if s["story_type"] == "degree_days"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


@pytest.fixture()
def chandler(engine):
    """A desert year: a mild January, then a long brutal summer. Averages
    of 55° in winter (10 heating degree days a day) and 95° in summer
    (30 cooling degree days a day)."""
    _seed(engine, _span(date(2026, 1, 1), date(2026, 2, 28), 65.0, 45.0))
    _seed(engine, _span(date(2026, 3, 1), date(2026, 8, 30), 105.0, 85.0))
    return engine


# ───────────────────────── the happy path ─────────────────────────

def test_it_leads_with_the_side_that_actually_worked(chandler):
    s = _dd()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("science", "degree_days")
    assert s["title"] == "How Hard Did the AC Work?"
    assert "DEGREE DAYS OF COOLING" in s["hero_line"]
    # 183 summer days averaging 95° = 30 a day.
    assert s["hero"]["value"] == 183 * 30
    assert s["hero"]["unit"] == "degree days"
    assert _stat(s, "heating_degree_days")["value"] == 59 * 10


def test_a_cold_station_leads_with_heating(engine):
    """The same producer, the other climate. A card that always led with
    cooling would be an Arizona card pretending to be a feature."""
    _seed(engine, _span(date(2026, 1, 1), date(2026, 8, 30), 45.0, 25.0))
    s = _dd()
    assert s is not None
    assert "DEGREE DAYS OF HEATING" in s["hero_line"]
    assert _stat(s, "heating_degree_days") is not None
    # Never a ratio against zero: this station booked no cooling at all,
    # and "outworked cooling infinity to 1" is not a sentence.
    assert "There was no cooling demand at all." in s["context"]
    assert "outworked" not in s["context"]
    assert _stat(s, "cooling_degree_days")["value"] == 0


def test_the_ratio_is_only_drawn_between_two_real_numbers(chandler):
    """Both sides worked here, so the card can say how lopsided it was."""
    s = _dd()
    assert "Cooling outworked heating 9 to 1." in s["context"]


def test_it_teaches_the_unit_before_it_quotes_one(chandler):
    """The card's whole reason to exist. The worked example has to be an
    example, not a restatement: a temperature, a night, an average, and the
    number that falls out."""
    s = _dd()
    assert "counts one degree of demand for every degree" in s["context"]
    assert "65°F" in s["context"]
    assert "95°F" in s["context"] and "75°F" in s["context"]
    assert "85°F" in s["context"]
    assert "20°F·days of cooling" in s["context"]
    assert "the same definition a utility uses" in s["viz"]["footnote"]


def test_the_busiest_month_is_named_and_shared(chandler):
    """The spec's "August created a third of the year's cooling demand"."""
    s = _dd()
    busiest = _stat(s, "busiest_month")
    assert "%" in busiest["label"] and "cooling" in busiest["label"]
    hero_bar = next(b for b in s["viz"]["series"] if b["hero"])
    assert hero_bar["note"] is not None and "%" in hero_bar["note"]
    # Every month carries both sides; a month with no heating says 0
    # because it MEASURED no demand, not because a sensor was missing.
    for bar in s["viz"]["series"]:
        assert isinstance(bar["cooling"], int)
        assert isinstance(bar["heating"], int)


# ─────────────── the unit lesson and the unit must agree ───────────────

def test_the_lesson_and_the_number_stay_in_one_scale(chandler):
    """A degree day is a SUM OF DIFFERENCES, so it converts by scale; the
    base it is measured from is a READING and carries the offset."""
    c = _dd(units=stories.Units(temperature="celsius"))
    f = _dd()
    # The base becomes 18.3°C, and no Fahrenheit survives anywhere.
    assert "18.3°C" in c["context"]
    assert "°F" not in c["context"]
    assert "18.3°C" in c["viz"]["footnote"]
    # The total converts by scale — 5/9 — never by the reading conversion.
    assert c["hero"]["value"] == pytest.approx(f["hero"]["value"] * 5 / 9, abs=1)
    # The worked example stays self-consistent in the new scale: 35°C over
    # 23.9°C averages 29.4°C, which is 11.1°C above the 18.3°C base.
    assert "35°C" in c["context"] and "23.9°C" in c["context"]
    assert "29.4°C" in c["context"]
    assert "11.1°C·days" in c["context"]
    # The unit name itself is scale-free; only the magnitude moved.
    assert c["hero"]["unit"] == f["hero"]["unit"] == "degree days"


# ───────────────────────── decline paths ─────────────────────────

def test_a_station_missing_one_end_declines(engine):
    """degree_days() needs both extremes; one end is absent, not zero."""
    _seed(engine, [(d, hi, None) for d, hi, _ in
                   _span(date(2026, 1, 1), date(2026, 8, 30), 95.0, 75.0)])
    assert _dd() is None


def test_too_few_measured_days_declines(engine):
    _seed(engine, _span(date(2026, 8, 1), date(2026, 8, 30), 105.0, 85.0))
    assert _dd() is None


def test_a_year_that_never_left_the_base_declines(engine):
    """Every day averaging exactly 65° books no demand of either kind."""
    _seed(engine, _span(date(2026, 1, 1), date(2026, 8, 30), 70.0, 60.0))
    assert _dd() is None


# ───────────────────────── calibration ─────────────────────────

def test_a_lopsided_climate_outscores_a_mild_one(chandler):
    lopsided = _dd()
    assert 0.0 <= lopsided["interestingness"] <= 1.0
    assert lopsided["score_parts"]["lopsidedness"] > 0.9

    # A mild maritime year: both sides work, neither dominates.
    _seed(chandler, _span(date(2026, 1, 1), date(2026, 4, 30), 68.0, 52.0))
    _seed(chandler, _span(date(2026, 5, 1), date(2026, 8, 30), 78.0, 62.0))
    mild = _dd()
    assert 0.0 <= mild["interestingness"] <= 1.0
    assert mild["score_parts"]["lopsidedness"] < lopsided["score_parts"]["lopsidedness"]
    assert mild["interestingness"] < lopsided["interestingness"]
