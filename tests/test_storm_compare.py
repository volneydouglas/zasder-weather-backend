"""Storm compare (2.5, Doren's 2b): one storm window, every station.

Doren runs a Davis and a Tempest a few feet apart and wanted to see one
storm at both. Each station is measured by the storm summary's own rules,
and a station that was not reporting says so instead of reading as dry.
"""
from __future__ import annotations

import asyncio
import pytest
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


def test_the_counter_the_station_kept_wins_over_a_stray_row(client):
    """R25-11 (the 2.5 detailed review): a Davis that posts the daily
    counter every five minutes and a yearly value only at the window's
    two edges was measured by the yearly rows (first preference with any
    row), so the storm read as the edges' sparse difference. Coverage
    decides now: thirteen daily rows beat two yearly ones."""
    _seed()
    mixed = "AA:BB:CC:00:00:DA"

    async def go():
        await db.upsert_device(mixed, {"name": "Mixed"})
        rows = [{"dateutc": T0 + k * 5 * MIN, "dailyrainin": 0.3 * k / 12, "tempf": 90}
                for k in range(13)]
        rows[0]["yearlyrainin"] = 10.0          # the edges only ...
        rows[12]["yearlyrainin"] = 10.05        # ... and far less than the storm
        await db.insert_observations(mixed, rows)
    asyncio.run(go())
    rows = {s["mac"]: s for s in client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 60 * MIN}).json()["stations"]}
    assert rows[mixed]["total_in"] == 0.3, rows[mixed]
    # Equal coverage keeps the yearly preference (it survives midnight):
    # the seeded Davis, yearly only, still reads its 0.40.
    assert rows[DAVIS]["total_in"] == 0.4


def test_a_garbled_counter_does_not_hide_the_storm(client):
    """CodeRabbit, PR #70: insert_observations stores poller values as they
    came and SQLite keeps text in a REAL column, so a daily counter that
    arrived as "--" for the whole window once counted as thirteen covered
    rows, beat the seven numeric yearly rows that carried the storm, and
    the storm read as 0.00. No counter is picked any more: the stitch
    reads every finite value and skips the rest."""
    _seed()
    garbled = "AA:BB:CC:00:00:DB"

    async def go():
        await db.upsert_device(garbled, {"name": "Garbled"})
        rows = [{"dateutc": T0 + k * 5 * MIN, "dailyrainin": "--", "tempf": 90}
                for k in range(13)]
        for k in range(0, 13, 2):                # seven yearly rows, 0.30 in
            rows[k]["yearlyrainin"] = 10.0 + 0.3 * k / 12
        await db.insert_observations(garbled, rows)
    asyncio.run(go())
    rows = {s["mac"]: s for s in client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 60 * MIN}).json()["stations"]}
    assert rows[garbled]["total_in"] == 0.3, rows[garbled]


def test_a_window_of_only_garbled_counters_is_not_dry(client):
    """CodeRabbit, PR #81: the stitch's row count was the rows the SQL
    admitted, and "--" is not NULL, so a station whose every counter
    arrived garbled read as a dry 0.00. Absent is not zero: it reads as
    not measuring rain."""
    _seed()
    garbled = "AA:BB:CC:00:00:DC"

    async def go():
        await db.upsert_device(garbled, {"name": "All garbled"})
        await db.insert_observations(garbled, [
            {"dateutc": T0 + k * 5 * MIN, "dailyrainin": "--", "yearlyrainin": "n/a", "tempf": 90}
            for k in range(13)])
    asyncio.run(go())
    rows = {s["mac"]: s for s in client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 60 * MIN}).json()["stations"]}
    assert rows[garbled]["total_in"] is None, rows[garbled]
    assert rows[garbled]["readings"] == 13


def test_a_negative_counter_reading_is_not_a_baseline(client):
    """CodeRabbit, PR #81: a counter never runs below zero, but a garbled
    -1.0 followed by a real 0.1 was a 1.1 in climb to the stitch, beating
    the 0.1 the yearly counter measured. A negative reading is missing."""
    _seed()
    neg = "AA:BB:CC:00:00:DD"

    async def go():
        await db.upsert_device(neg, {"name": "Negative"})
        await db.insert_observations(neg, [
            {"dateutc": T0, "dailyrainin": -1.0, "yearlyrainin": 10.0, "tempf": 90},
            {"dateutc": T0 + 5 * MIN, "dailyrainin": 0.1, "yearlyrainin": 10.1, "tempf": 90},
        ])
    asyncio.run(go())
    rows = {s["mac"]: s for s in client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 60 * MIN}).json()["stations"]}
    assert rows[neg]["total_in"] == 0.1, rows[neg]


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


def test_a_source_that_switches_counters_midway_is_measured_whole(client):
    """R25-11's second half: yearly for the first half of the storm, daily
    for the second. One counter alone misses half the storm; stitched,
    both halves count (the one increment at the switch excepted)."""
    _seed()
    switch = "AA:BB:CC:00:00:DB"

    async def go():
        await db.upsert_device(switch, {"name": "Switcher"})
        rows = []
        for k in range(7):                       # yearly 10.00 -> 10.30
            rows.append({"dateutc": T0 + k * 5 * MIN, "yearlyrainin": 10.0 + 0.05 * k, "tempf": 90})
        for k in range(7, 13):                   # daily 0.00 -> 0.25
            rows.append({"dateutc": T0 + k * 5 * MIN, "dailyrainin": 0.05 * (k - 7), "tempf": 90})
        await db.insert_observations(switch, rows)
    asyncio.run(go())
    rows = {s["mac"]: s for s in client.get("/api/storms/compare", headers=H, params={
        "mac": DAVIS, "started_ms": T0, "ended_ms": T0 + 60 * MIN}).json()["stations"]}
    assert rows[switch]["total_in"] == pytest.approx(0.30 + 0.25, abs=0.011), rows[switch]
