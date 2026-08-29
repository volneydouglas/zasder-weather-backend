"""Climate presentation set (1.9): degree-day math, water year, the
year summary, the NOAA report shapes, and the year-span series — all
rollup-fed."""
from __future__ import annotations

import asyncio
import os
from datetime import date

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app.climate import degree_days, water_year_start  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:88"


def test_degree_days_noaa_convention():
    # Mean (30+50)/2 = 40 → 25 heating, 0 cooling.
    hdd, cdd, gdd = degree_days(30.0, 50.0)
    assert (hdd, cdd) == (25.0, 0.0) and gdd == 0.0
    # Phoenix July: mean (85+105)/2 = 95 → 30 cooling; GDD capped at 86:
    # (86 + 85)/2 - 50 = 35.5.
    hdd, cdd, gdd = degree_days(85.0, 105.0)
    assert (hdd, cdd) == (0.0, 30.0)
    assert gdd == 35.5
    # A mean exactly on base: neither heats nor cools.
    assert degree_days(60.0, 70.0)[:2] == (0.0, 0.0)
    assert degree_days(None, 70.0) is None


def test_water_year_start_wraps():
    assert water_year_start(date(2026, 8, 27), 10) == date(2025, 10, 1)
    assert water_year_start(date(2026, 11, 2), 10) == date(2026, 10, 1)
    assert water_year_start(date(2026, 10, 1), 10) == date(2026, 10, 1)
    assert water_year_start(date(2026, 8, 27), 1) == date(2026, 1, 1)


def _seed_rollups(db, days: list[tuple[str, float, float, float, float]]):
    async def run():
        async with db.connect() as conn:
            for day, tmin, tmax, rain, gust in days:
                await conn.execute(
                    "INSERT INTO daily_rollups (mac, day, tempf_min, "
                    "tempf_max, rain_total, windgustmph_max) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (MAC, day, tmin, tmax, rain, gust))
            await conn.commit()
    asyncio.run(run())


def test_year_summary_months_and_water_year(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    today = date.today().isoformat()
    _seed_rollups(db, [
        ("2025-01-10", 30.0, 50.0, 0.25, 22.0),
        ("2025-01-11", 32.0, 48.0, 0.00, 18.0),
        ("2025-07-04", 85.0, 105.0, 0.00, 35.0),
        # A rollup day inside the current water year (guaranteed: today).
        (today, 70.0, 90.0, 0.40, 20.0),
    ])
    r = client.get(f"/api/devices/{MAC}/climate?year=2025", headers=H)
    assert r.status_code == 200
    body = r.json()
    jan = body["months"][0]
    assert jan["days"] == 2 and jan["mean"] == 40.0
    assert jan["min"] == 30.0 and jan["min_day"] == "2025-01-10"
    assert jan["rain"] == 0.25 and jan["hdd"] == 50.0 and jan["cdd"] == 0
    jul = body["months"][6]
    assert jul["cdd"] == 30.0 and jul["gdd"] == 35.5
    feb = body["months"][1]
    assert feb["days"] == 0 and feb["mean"] is None and feb["rain"] is None
    assert body["totals"]["rain"] == 0.25
    assert body["totals"]["hdd"] == 50.0 and body["totals"]["cdd"] == 30.0
    # Day-weighted, unrounded: means 40, 40, 95 -> 58.3 — NOT the 67.5 an
    # average of monthly means gives (CodeRabbit, PR #33).
    assert body["totals"]["mean"] == 58.3
    # Water year runs against TODAY and catches the seeded today row.
    assert body["water_year"]["rain"] == 0.40


def test_noaa_reports_render(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed_rollups(db, [
        ("2025-01-10", 30.0, 50.0, 0.25, 22.0),
        ("2025-01-11", 32.0, 48.0, 0.00, 18.0),
    ])
    r = client.get(f"/api/devices/{MAC}/reports/noaa?year=2025&month=1",
                   headers=H)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "MONTHLY CLIMATOLOGICAL SUMMARY for January 2025" in text
    assert "    10   40.0   50.0   30.0" in text     # day row, fixed-width
    assert "Days with data: 2" in text

    r = client.get(f"/api/devices/{MAC}/reports/noaa?year=2025", headers=H)
    assert r.status_code == 200
    assert "YEARLY CLIMATOLOGICAL SUMMARY for 2025" in r.text
    assert "Water year" in r.text


def test_daily_series_year_span(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed_rollups(db, [
        ("2025-01-10", 30.0, 50.0, 0.25, 22.0),
        ("2025-07-04", 85.0, 105.0, 0.00, 35.0),
    ])
    r = client.get(f"/api/devices/{MAC}/daily-series"
                   "?days=365&end_day=2025-12-31", headers=H)
    assert r.status_code == 200
    series = r.json()["series"]
    assert series == [
        ["2025-01-10", 30.0, 50.0, 40.0, 0.25, 22.0],
        ["2025-07-04", 85.0, 105.0, 95.0, 0.0, 35.0],
    ]


def test_climate_routes_404_without_insights(client):
    for path in (f"/api/devices/{MAC}/climate?year=2025",
                 f"/api/devices/{MAC}/reports/noaa?year=2025",
                 f"/api/devices/{MAC}/daily-series"):
        r = client.get(path, headers=H)
        assert r.status_code == 404, path
    # And auth-gated like every read route.
    assert client.get(f"/api/devices/{MAC}/climate?year=2025").status_code == 401
