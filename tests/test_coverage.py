"""Archive coverage (2.5, C10): which years this station has."""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import db  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "5D:5D:05:00:00:01"


def _seed():
    async def go():
        async with db.connect() as conn:
            for d in ("2015-03-01", "2015-03-02", "2015-07-04", "2026-09-20", "2026-09-21"):
                await conn.execute("INSERT INTO daily_rollups (mac, day) VALUES (?, ?)", (MAC, d))
            for d in ("2015-03-01", "2015-03-02", "2015-07-04"):
                await conn.execute("INSERT INTO imported_days (mac, day, source, imported_ms) "
                                   "VALUES (?, ?, 'wu', 1)", (MAC, d))
            await conn.commit()
    asyncio.run(go())


def test_years_newest_first_with_months_and_imports(client):
    _seed()
    r = client.get(f"/api/devices/{MAC}/coverage", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_day"] == "2015-03-01" and body["last_day"] == "2026-09-21"
    assert [y["year"] for y in body["years"]] == [2026, 2015]
    y2015 = body["years"][1]
    assert y2015["days"] == 3 and y2015["months"][2] == 2 and y2015["months"][6] == 1
    assert y2015["imported"] == {"wu": 3} and y2015["days_in_year"] == 365
    assert body["years"][0]["imported"] == {}


def test_an_empty_station_has_no_years(client):
    body = client.get("/api/devices/AA:00:00:00:00:01/coverage", headers=H).json()
    assert body == {"first_day": None, "last_day": None, "years": []}


def test_coverage_needs_the_token(client):
    assert client.get(f"/api/devices/{MAC}/coverage").status_code == 401
