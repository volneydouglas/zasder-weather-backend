"""2.6 plan item 5 (R25-A06): an archived forecast run names the sky it
was fetched for, and the scorecard grades only runs made for the sky the
server has now. Rows filed before 2.6 carry no sky and count as this one's.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from tests.test_forecast_skill import MAC, TODAY, _days, _filed, _measured, box  # noqa: E402,F401

HERE = (33.3, -112.0)          # the station's sky
FAR = (60.0, -112.0)           # 3,000 km north


async def _station(db, sky):
    await db.upsert_device(MAC, {"name": "Yard", "info": {
        "coords": {"coords": {"lat": sky[0], "lon": sky[1]}}}})


def test_runs_for_another_sky_are_not_graded_and_unsigned_rows_still_are(box):  # noqa: F811
    db = box

    async def go():
        await _station(db, HERE)
        for d in _days(20):
            await _measured(db, d, 70.0, 100.0, 0.0)
            # This sky, filed at noon: +2 on the high.
            await _filed(db, d, 1, hi=102.0, lo=70.0, sky=HERE)
            # A far sky, filed LATER the same day (it would win the
            # newest-issue rule if it were counted): +50.
            await _filed(db, d, 1, hi=150.0, lo=70.0, issued_hour=13, sky=FAR)
            # A pre-2.6 row with no sky at lead 3: counts as this sky.
            await _filed(db, d, 3, hi=106.0, lo=70.0)
        from app import forecast_skill
        return await forecast_skill.scorecard(MAC)
    card = asyncio.run(go())
    leads = {l["lead_days"]: l for l in card["leads"]}
    assert leads[1]["high"]["bias_f"] == pytest.approx(2.0), leads[1]
    assert leads[3]["high"]["bias_f"] == pytest.approx(6.0), leads[3]


def test_a_moved_forecast_station_scores_only_its_new_sky(box):  # noqa: F811
    """The server's first station is given new coordinates: the runs
    fetched for the old place stop grading the new one."""
    db = box

    async def go():
        for d in _days(20):
            await _measured(db, d, 70.0, 100.0, 0.0)
            await _filed(db, d, 1, hi=120.0, lo=70.0, sky=FAR)       # the old place
        await _station(db, HERE)
        from app import forecast_skill
        return await forecast_skill.scorecard(MAC)
    card = asyncio.run(go())
    assert card["available"] is False or all(
        l["n"] == 0 for l in card["leads"] if l["lead_days"] == 1), card


def test_the_beat_files_the_sky_it_fetched_for(client, monkeypatch):
    from app import db, forecast_snapshots as fs

    async def om(lat, lon):
        return {"time": ["2026-08-25"], "temperature_2m_max": [108.1],
                "temperature_2m_min": [84.0], "precipitation_probability_max": [20],
                "precipitation_sum": [0.0]}
    monkeypatch.setattr(fs, "_fetch_daily", om)
    devices = [{"mac": "AA", "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]
    asyncio.run(fs.check(devices, int(time.time() * 1000)))

    async def rows():
        async with db.connect() as conn:
            return [tuple(r) for r in await (await conn.execute(
                "SELECT lat, lon FROM forecast_snapshots")).fetchall()]
    assert asyncio.run(rows()) == [(33.3, -111.9)]


def test_a_pre_26_database_gains_the_sky_columns(client):
    """init_db on a database whose table predates the columns adds them;
    the old rows read back as unsigned."""
    from app import db

    async def go():
        async with db.connect() as conn:
            await conn.execute("DROP TABLE forecast_snapshots")
            await conn.execute("""CREATE TABLE forecast_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                issued_ms INTEGER NOT NULL, valid_date TEXT NOT NULL,
                lead_days INTEGER NOT NULL, tmax_f REAL, tmin_f REAL, pop REAL, precip_in REAL)""")
            await conn.execute("INSERT INTO forecast_snapshots (provider, issued_ms, valid_date, "
                               "lead_days, tmax_f) VALUES ('open-meteo', 1, '2026-08-25', 1, 100.0)")
            await conn.commit()
        await db.init_db()
        calls = await __import__("app.forecast_snapshots", fromlist=["x"]).day_ahead_calls(
            "open-meteo", date(2026, 8, 1), near=(33.3, -112.0))
        return calls
    calls = asyncio.run(go())
    assert "2026-08-25" in calls and calls["2026-08-25"]["lat"] is None
