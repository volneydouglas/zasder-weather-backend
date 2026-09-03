"""Story engine (2.0): the diurnal pair — The Shape of a Year and The
Humidity Tax.

Both read the 12×24 month-by-hour grids `insights.assemble` publishes off
`hour_rollups`. The load-bearing rules pinned here:

- A cell the station never measured sends NO series row. Zero-filling a
  blank 3 am would draw the coldest hour of a year the station slept
  through, which is the `?? 0` bug family wearing a chart.
- The axis words are the producer's, riding the rows that carry them
  (month labels on each month's first cell, hour labels on the first
  month) — the client formats no clock and no month name.
- Units convert BOTH ways on one card: the shape's cells are READINGS
  (offset) while the tax's cells are DIFFERENCES (scale), and a producer
  that ran a delta through the reading conversion would owe somebody
  −16.7°C of humidity.
- The refund is only CLAIMED when the air measurably hands something
  back; the tax card declines entirely on a flat grid, because a station
  with no way to feel humidity has no tax to report.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:EE"
TODAY = date(2026, 8, 30)


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _story(mac: str = MAC, **kw):
    return asyncio.run(stories.top_stories(mac, limit=12, **kw))


def _temp(month: int, hour: int) -> float:
    """A synthetic but physical year: seasons peak in July, days peak at
    5 pm. Hottest cell July 17h, coldest January 5h — asserted below, so a
    fixture edit that moves them fails loudly."""
    season = {1: 0.0, 2: 2, 3: 8, 4: 14, 5: 22, 6: 30,
              7: 34, 8: 33, 9: 26, 10: 16, 11: 6, 12: 1}[month]
    diurnal = {h: v for h, v in enumerate(
        [2, 1, 0.5, 0.2, 0.1, 0.0, 1, 3, 5, 7, 9, 11,
         13, 14.5, 15.5, 16.5, 17.5, 18, 17, 14, 10, 7, 5, 3])}[hour]
    return 40.0 + season + diurnal


def _seed_hours(db, cells, mac: str = MAC, n: int = 30) -> None:
    """`cells` of (month, hour, tempf_avg, feels_avg | None)."""
    async def run():
        async with db.connect() as conn:
            for month, hour, t, f in cells:
                await conn.execute(
                    "INSERT OR REPLACE INTO hour_rollups "
                    "(mac, month, hour, tempf_sum, tempf_n, feels_sum, "
                    "feels_n) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (mac, month, hour, t * n, n,
                     (f if f is not None else 0.0) * n,
                     n if f is not None else 0))
            # A few daily rows so day_count and first/last day exist.
            d = date(2025, 9, 1)
            for _ in range(10):
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mac, d.isoformat(), 60.0, 100.0, 100.0, 1))
                d += timedelta(days=1)
            await conn.commit()
    asyncio.run(run())


def _full_year(tax=None):
    """Every cell of the year. `tax(month, hour)` adds a feels-like delta;
    None seeds feels equal to temp (a flat, tax-free grid)."""
    out = []
    for m in range(1, 13):
        for h in range(24):
            t = _temp(m, h)
            delta = tax(m, h) if tax else 0.0
            out.append((m, h, t, t + delta))
    return out


def _shape(out):
    return next((s for s in out["stories"]
                 if s["story_type"] == "shape_of_year"), None)


def _tax(out):
    return next((s for s in out["stories"]
                 if s["story_type"] == "humidity_tax"), None)


# ───────────────────────── The Shape of a Year ─────────────────────────

def test_shape_finds_the_lag_and_names_it(engine):
    _seed_hours(engine, _full_year())
    s = _shape(_story())
    assert s is not None
    assert (s["family"], s["title"]) == ("climate", "The Shape of a Year")
    # July 17h is the hottest cell by construction; the lag is the hero.
    assert s["hero_line"] == "THE YEAR PEAKS AT 5 PM IN JULY"
    assert s["viz"]["highlight_key"] == "m07h17"
    assert "5 pm in July" in s["context"]
    assert "coldest" in s["context"]
    # The lag sentence appears — 17h is 5 hours past noon.
    assert "another 5 hours" in s["context"]
    assert 0.0 <= s["interestingness"] <= 1.0


def test_shape_grid_rows_carry_the_producers_axis_words(engine):
    _seed_hours(engine, _full_year())
    s = _shape(_story())
    rows = s["viz"]["series"]
    assert s["viz"]["kind"] == "month_hour_grid"
    assert len(rows) == 288
    # Exactly one month label per month, on its first sent cell.
    labels = [r["month_label"] for r in rows if "month_label" in r]
    assert labels == ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    # The clock words are the producer's, on the first month only.
    hours = {r["hour"]: r["hour_label"] for r in rows if "hour_label" in r}
    assert hours == {0: "midnight", 6: "6 am", 12: "noon", 18: "6 pm"}
    assert all(r["month"] == 1 for r in rows if "hour_label" in r)


def test_a_blank_cell_sends_no_row_and_never_a_zero(engine):
    cells = [c for c in _full_year() if not (c[0] == 3 and c[1] == 3)]
    _seed_hours(engine, cells)
    s = _shape(_story())
    rows = s["viz"]["series"]
    assert len(rows) == 287
    assert not any(r["month"] == 3 and r["hour"] == 3 for r in rows)


def test_shape_declines_without_the_whole_year(engine):
    # Eleven months is not the shape of a year.
    _seed_hours(engine, [c for c in _full_year() if c[0] != 11])
    out = _story()
    assert _shape(out) is None and "shape_of_year" in out["declined"]


def test_shape_declines_on_a_threadbare_month(engine):
    # February present but with fewer hours than the floor.
    cells = [c for c in _full_year()
             if not (c[0] == 2 and c[1] >= stories.DIURNAL_MIN_HOURS_PER_MONTH - 1)]
    _seed_hours(engine, cells)
    out = _story()
    assert _shape(out) is None and "shape_of_year" in out["declined"]


def test_shape_cells_convert_as_readings(engine):
    _seed_hours(engine, _full_year())
    s = _shape(_story(units=stories.Units(temperature="celsius")))
    hot = next(r for r in s["viz"]["series"] if r["key"] == "m07h17")
    # 92.0°F reading → 33.3°C, offset and all.
    assert hot["value"] == round((92.0 - 32) * 5 / 9, 1)
    assert "33.3°C" in s["context"]


# ───────────────────────── The Humidity Tax ────────────────────────────

def _august_tax(month: int, hour: int) -> float:
    """+9.4°F at the average 7 am in August; −4.0°F at 3 pm in June (the
    refund); a mild charge elsewhere in the wet months."""
    if month == 8 and hour == 7:
        return 9.4
    if month == 6 and hour == 15:
        return -4.0
    if month in (7, 8, 9):
        return 3.0
    return 0.2


def test_tax_charges_and_refunds_in_the_producers_words(engine):
    _seed_hours(engine, _full_year(tax=_august_tax))
    s = _tax(_story())
    assert s is not None
    assert (s["family"], s["title"]) == ("science", "The Humidity Tax")
    assert s["hero_line"] == "A 9.4°F HUMIDITY TAX"
    assert s["viz"]["kind"] == "month_hour_delta_grid"
    assert s["viz"]["highlight_key"] == "m08h07"
    assert "7 am in August" in s["context"]
    # The refund is measured, so it is claimed — dry air, no geography.
    assert "BELOW the thermometer" in s["context"]
    keys = [t["key"] for t in s["supporting"]]
    assert keys == ["biggest_tax", "biggest_refund", "charged_share"]
    refund = next(t for t in s["supporting"] if t["key"] == "biggest_refund")
    assert refund["value"] == -4.0
    assert 0.0 <= s["interestingness"] <= 1.0


def test_refund_is_not_claimed_when_the_air_never_gave_back(engine):
    _seed_hours(engine, _full_year(
        tax=lambda m, h: 9.4 if (m, h) == (8, 7) else 0.4))
    s = _tax(_story())
    assert s is not None
    assert "BELOW the thermometer" not in s["context"]
    assert not any(t["key"] == "biggest_refund" for t in s["supporting"])


def test_tax_declines_on_a_flat_grid(engine):
    # feels == temp everywhere: no humidity sensor reaches the rollup.
    _seed_hours(engine, _full_year())
    out = _story()
    assert _tax(out) is None and "humidity_tax" in out["declined"]


def test_tax_cells_convert_as_differences(engine):
    _seed_hours(engine, _full_year(tax=_august_tax))
    s = _tax(_story(units=stories.Units(temperature="celsius")))
    # A +9.4°F tax is +5.2°C — SCALE only. The reading conversion would
    # print −12.6°C and charge the reader for weather that never happened.
    hot = next(r for r in s["viz"]["series"] if r["key"] == "m08h07")
    assert hot["value"] == round(9.4 * 5 / 9, 1)
    assert "5.2°C" in s["hero_line"]
