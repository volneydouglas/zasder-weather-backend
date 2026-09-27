"""Storm compare (2.5, Doren's 2b): one storm window, every station.

Doren runs a Davis and a Tempest a few feet apart and wanted to see one
storm at both. Each station is measured by the storm summary's own rules,
and a station that was not reporting says so instead of reading as dry.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import db  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
DAVIS = "5D:5D:05:00:00:01"
TEMPEST = "5D:5D:06:02:A4:F7"
QUIET = "AA:BB:CC:00:00:D9"
AIR = "5D:5D:08:82:4D:6E"
T0 = 1_780_000_000_000
MIN = 60_000


def _seed():
    async def go():
        for mac, name in ((DAVIS, "Chaucer Drive"), (TEMPEST, "Tempest"),
                          (QUIET, "Garage"), (AIR, "Govee CO2")):
            await db.upsert_device(mac, {"name": name})
        # Davis: 0.40 in on the yearly counter over an hour, gust 30.
        await db.insert_observations(DAVIS, [
            {"dateutc": T0 + k * 5 * MIN, "yearlyrainin": 10.0 + 0.4 * k / 12,
             "tempf": 100 - k, "windgustmph": 20 + k, "hourlyrainin": 0.4}
            for k in range(13)])
        # Tempest: daily counter only, 0.30 in.
        await db.insert_observations(TEMPEST, [
            {"dateutc": T0 + k * 5 * MIN, "dailyrainin": 0.3 * k / 12,
             "tempf": 99 - k, "windgustmph": 25}
            for k in range(13)])
        # Garage: reporting temperature, no rain counter at all.
        await db.insert_observations(QUIET, [
            {"dateutc": T0 + k * 5 * MIN, "tempf": 80} for k in range(13)])
        await db.insert_observations(AIR, [
            {"dateutc": T0 + k * 5 * MIN, "co2": 600} for k in range(13)])
    asyncio.run(go())


def test_every_station_is_measured_by_its_own_counter(client):
    _seed()
    r = client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 60 * MIN})
    assert r.status_code == 200, r.text
    rows = {s["mac"]: s for s in r.json()["stations"]}
    assert set(rows) == {DAVIS, TEMPEST, QUIET}, "an air monitor is not a station"
    assert rows[DAVIS]["is_home"] and r.json()["stations"][0]["mac"] == DAVIS
    assert abs(rows[DAVIS]["total_in"] - 0.40) < 0.005
    assert abs(rows[TEMPEST]["total_in"] - 0.30) < 0.005
    assert rows[DAVIS]["max_gust_mph"] == 32 and rows[TEMPEST]["max_gust_mph"] == 25


def test_a_station_without_a_rain_counter_is_not_dry(client):
    _seed()
    rows = {s["mac"]: s for s in client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 60 * MIN}).json()["stations"]}
    assert rows[QUIET]["readings"] == 13
    assert rows[QUIET]["total_in"] is None


def test_a_station_that_was_silent_says_so(client):
    _seed()
    later = T0 + 10 * 86_400_000
    rows = client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": later, "ended_ms": later + 60 * MIN}).json()["stations"]
    assert all(s["readings"] == 0 and s["total_in"] is None for s in rows)


def test_the_window_is_bounded(client):
    r = client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 4 * 86_400_000})
    assert r.status_code == 400
    r = client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 - 1})
    assert r.status_code == 400
    assert client.get("/api/storms/compare", params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0}).status_code == 401
