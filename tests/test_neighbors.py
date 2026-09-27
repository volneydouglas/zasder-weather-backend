"""Neighbour stations (2.5, Doren's 2a).

The design rules are the tests: never a devices row, fetched only when
asked, qcStatus honoured, WU rows stored as the averages they are, the
owner's own WU station never offered back, the key never in an error,
and the routes never a general WU proxy.
"""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

import pytest  # noqa: E402

from app import db, neighbors  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:E1"
KEY = "wu-secret-key-0123"


def _near(ids, qcs=None):
    n = len(ids)
    return {"location": {
        "stationId": ids,
        "stationName": [f"Station {i}" for i in ids],
        "latitude": [33.30 + 0.01 * k for k in range(n)],
        "longitude": [-111.80] * n,
        "qcStatus": qcs or [1] * n,
    }}


def _obs(epoch_s, temp, qc=1):
    return {"epoch": epoch_s, "qcStatus": qc, "humidityAvg": 30,
            "winddirAvg": 200,
            "imperial": {"tempAvg": temp, "dewptAvg": 40, "windspeedAvg": 5,
                         "windgustHigh": 12, "pressureMax": 29.9,
                         "pressureMin": 29.8, "precipRate": 0.5,
                         "precipTotal": 0.1, "heatindexAvg": temp,
                         "windchillAvg": temp}}


@pytest.fixture
def wu(monkeypatch):
    """A fake WU. Records every call; answers from `answers` by URL."""
    calls = []
    answers = {}

    async def fake(url, params):
        calls.append((url, dict(params)))
        a = answers.get(url)
        if isinstance(a, Exception):
            raise a
        return a(params) if callable(a) else a
    monkeypatch.setattr(neighbors, "_get_json", fake)
    return calls, answers


@pytest.fixture
def station(client):
    async def seed():
        await db.upsert_device(MAC, {"name": "Backyard", "info": {
            "coords": {"coords": {"lat": 33.30, "lon": -111.80}}}})
        await db.set_kv("wu_api_key", KEY)
    asyncio.run(seed())


def test_without_a_key_the_route_says_so_and_calls_nobody(client, wu, monkeypatch):
    calls, _ = wu
    from app.config import settings
    monkeypatch.setattr(settings, "wu_api_key", None)

    async def seed():
        await db.upsert_device(MAC, {"name": "Backyard", "info": {
            "coords": {"coords": {"lat": 33.30, "lon": -111.80}}}})
    asyncio.run(seed())
    r = client.get(f"/api/neighbors?mac={MAC}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False and body["stations"] == []
    assert "WU API key" in body["error"]
    assert calls == []


def test_nearby_stations_are_found_once_and_never_become_devices(client, wu, station):
    calls, answers = wu
    answers[neighbors.NEAR_URL] = _near(["KAZNEAR1", "KAZNEAR2"])
    r = client.get(f"/api/neighbors?mac={MAC}", headers=H)
    assert r.status_code == 200, r.text
    got = r.json()["stations"]
    assert [s["id"] for s in got] == ["KAZNEAR1", "KAZNEAR2"]
    assert got[0]["distance_km"] < got[1]["distance_km"]
    # Second open inside the week: no second search.
    client.get(f"/api/neighbors?mac={MAC}", headers=H)
    assert sum(1 for u, _ in calls if u == neighbors.NEAR_URL) == 1
    macs = {d["mac"] for d in client.get("/api/devices", headers=H).json()}
    assert macs == {MAC}


def test_the_owners_own_wu_station_is_not_a_neighbour(client, wu, station):
    _, answers = wu
    answers[neighbors.NEAR_URL] = _near(["KAZMINE1", "KAZNEAR2"])

    async def mine():
        async with db.connect() as conn:
            await conn.execute("INSERT INTO wu_station_map (mac, wu_station_id) "
                               "VALUES (?, ?)", (MAC, "KAZMINE1"))
            await conn.commit()
    asyncio.run(mine())
    got = client.get(f"/api/neighbors?mac={MAC}", headers=H).json()["stations"]
    assert [s["id"] for s in got] == ["KAZNEAR2"]


def test_a_station_that_failed_qc_is_listed_but_never_drawn(client, wu, station):
    calls, answers = wu
    answers[neighbors.NEAR_URL] = _near(["KAZBAD1"], qcs=[0])
    got = client.get(f"/api/neighbors?mac={MAC}", headers=H).json()["stations"]
    assert got[0]["qc"] == "failed"
    r = client.get("/api/neighbors/KAZBAD1/history",
                   params={"start_ms": int(time.time() * 1000) - 3_600_000},
                   headers=H)
    assert r.json()["rows"] == [] and "quality" in r.json()["error"]
    assert not any(u == neighbors.RECENT_URL for u, _ in calls)


def test_history_is_stored_as_averages_and_failed_rows_are_dropped(client, wu, station):
    calls, answers = wu
    answers[neighbors.NEAR_URL] = _near(["KAZNEAR1"])
    now = int(time.time())
    answers[neighbors.RECENT_URL] = {"observations": [
        _obs(now - 600, 90.0), _obs(now - 300, 91.0, qc=0), _obs(now - 60, 92.0)]}
    client.get(f"/api/neighbors?mac={MAC}", headers=H)
    r = client.get("/api/neighbors/KAZNEAR1/history",
                   params={"start_ms": (now - 3600) * 1000}, headers=H)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [x["tempf"] for x in rows] == [90.0, 92.0]
    row = rows[0]
    # The importer's mapping: gust is the interval HIGH, pressure the
    # midpoint of max/min, precipRate is NOT hourly rain.
    assert row["windgustmph"] == 12 and abs(row["baromrelin"] - 29.85) < 1e-9
    assert "hourlyrainin" not in row and row["dailyrainin"] == 0.1
    # Fresh inside ten minutes: a second open costs nothing.
    client.get("/api/neighbors/KAZNEAR1/history",
               params={"start_ms": (now - 3600) * 1000}, headers=H)
    assert sum(1 for u, _ in calls if u == neighbors.RECENT_URL) == 1


def test_older_days_are_fetched_once_and_capped_at_a_week(client, wu, station):
    calls, answers = wu
    answers[neighbors.NEAR_URL] = _near(["KAZNEAR1"])
    answers[neighbors.RECENT_URL] = {"observations": []}
    answers[neighbors.HISTORY_URL] = None      # WU's 204
    client.get(f"/api/neighbors?mac={MAC}", headers=H)
    now = int(time.time() * 1000)
    client.get("/api/neighbors/KAZNEAR1/history",
               params={"start_ms": now - 30 * 86_400_000}, headers=H)
    days = [p["date"] for u, p in calls if u == neighbors.HISTORY_URL]
    assert 1 <= len(days) <= neighbors.MAX_DAYS
    client.get("/api/neighbors/KAZNEAR1/history",
               params={"start_ms": now - 30 * 86_400_000}, headers=H)
    again = [p["date"] for u, p in calls if u == neighbors.HISTORY_URL]
    assert again == days


def test_the_route_is_not_a_general_wu_proxy(client, wu, station):
    calls, _ = wu
    start = int(time.time() * 1000) - 3_600_000
    assert client.get("/api/neighbors/KNOTNEAR9/history",
                      params={"start_ms": start}, headers=H).status_code == 404
    assert client.get("/api/neighbors/bad%20id!/history",
                      params={"start_ms": start}, headers=H).status_code == 400
    assert calls == []


def test_an_upstream_error_never_carries_the_key(client, wu, station):
    _, answers = wu
    answers[neighbors.NEAR_URL] = neighbors.UpstreamError(
        "Weather Underground answered HTTP 500")
    body = client.get(f"/api/neighbors?mac={MAC}", headers=H).json()
    assert body["error"] and KEY not in body["error"]


def test_the_real_transport_redacts_the_key(monkeypatch):
    import httpx

    class Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            raise httpx.ConnectError(f"boom {url}?apiKey={params['apiKey']}")
    monkeypatch.setattr(neighbors.httpx, "AsyncClient", Boom)
    with pytest.raises(neighbors.UpstreamError) as e:
        asyncio.run(neighbors._get_json(neighbors.NEAR_URL, {"apiKey": KEY}))
    assert KEY not in str(e.value)


def test_neighbours_need_the_api_token(client):
    assert client.get(f"/api/neighbors?mac={MAC}").status_code == 401
    assert client.get("/api/neighbors/KAZNEAR1/history?start_ms=1").status_code == 401


# ── neighbour QC (C3) ────────────────────────────────────────────────────

def test_a_steady_gap_one_way_is_drift_and_a_shower_is_not():
    hot = neighbors.drift_verdict([5.0] * 10 + [3.0] * 4, 4.0)
    assert hot["drifting"] and hot["median_gap"] == 5.0
    mixed = neighbors.drift_verdict([6.0, -6.0] * 7, 4.0)
    assert not mixed["drifting"]
    assert neighbors.drift_verdict([9.0] * 3, 4.0) is None, "too few hours"


def test_the_check_compares_against_the_neighbour_median(client, station):
    now = int(time.time() * 1000)
    hour = 3_600_000

    async def seed():
        async with db.connect() as conn:
            for sid, qc in (("KAZN1", 1), ("KAZN2", 1), ("KAZBAD", 0)):
                await conn.execute(
                    "INSERT INTO neighbor_stations (station_id, name, lat, lon, "
                    "qc_status, seen_ms) VALUES (?, ?, 33.3, -111.8, ?, ?)",
                    (sid, sid, qc, now))
            for k in range(1, 13):
                t = now - k * hour
                for sid, temp in (("KAZN1", 90.0), ("KAZN2", 91.0), ("KAZBAD", 150.0)):
                    await conn.execute(
                        "INSERT INTO neighbor_observations (station_id, ts_ms, tempf, "
                        "humidity, baromrelin) VALUES (?, ?, ?, 30, 29.85)", (sid, t, temp))
            await conn.commit()
        # Our shield reads 6 hot; humidity and pressure agree.
        await db.insert_observations(MAC, [
            {"dateutc": now - k * hour, "tempf": 96.5, "humidity": 31, "baromrelin": 29.86}
            for k in range(1, 13)])
    asyncio.run(seed())
    body = client.get(f"/api/neighbors/check?mac={MAC}", headers=H).json()
    assert body["neighbours"] == 2, "a station that failed QC is not a witness"
    assert body["fields"]["tempf"]["drifting"]
    assert body["fields"]["tempf"]["median_gap"] == 6.0
    assert not body["fields"]["humidity"]["drifting"]
    assert not body["fields"]["baromrelin"]["drifting"]


def test_only_stations_near_this_one_are_witnesses(client, station):
    """Greptile, PR #48: the neighbour tables hold every station any of
    this server's stations was compared with. Two stations near Seattle
    reading 90° are no evidence that a Chandler shield reads hot."""
    now = int(time.time() * 1000)
    hour = 3_600_000

    async def seed():
        async with db.connect() as conn:
            for sid in ("KWAFAR1", "KWAFAR2"):
                await conn.execute(
                    "INSERT INTO neighbor_stations (station_id, name, lat, lon, "
                    "qc_status, seen_ms) VALUES (?, ?, 47.6, -122.3, 1, ?)",
                    (sid, sid, now))
            for k in range(1, 13):
                for sid in ("KWAFAR1", "KWAFAR2"):
                    await conn.execute(
                        "INSERT INTO neighbor_observations (station_id, ts_ms, tempf, "
                        "humidity, baromrelin) VALUES (?, ?, 90.0, 30, 29.85)",
                        (sid, now - k * hour))
            await conn.commit()
        await db.insert_observations(MAC, [
            {"dateutc": now - k * hour, "tempf": 96.5, "humidity": 31, "baromrelin": 29.86}
            for k in range(1, 13)])
    asyncio.run(seed())
    body = client.get(f"/api/neighbors/check?mac={MAC}", headers=H).json()
    assert body["neighbours"] == 0 and body["fields"] == {}
