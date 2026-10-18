"""The backyard correction (2.5, C1): the scorecard's bias, turned around."""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import forecast_skill as fs  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}


def _lead(lead, hi, lo, enough=True, n=20):
    return {"lead_days": lead, "n": n, "enough": enough,
            "high": {"bias_f": hi}, "low": {"bias_f": lo}}


def test_the_offset_is_the_bias_turned_around():
    card = {"leads": [_lead(1, -3.2, -0.7), _lead(3, -3.7, 6.6)]}
    got = fs.corrections_from(card)
    # Lead 1: the model ran 3.2 cold on highs → add 3.2; its low bias is
    # under a degree, which is noise.
    assert got[0] == {"lead_days": 1, "n": 20, "high_offset_f": 3.2,
                      "low_offset_f": None}
    # Lead 3: Chandler's desert-low finding, +6.6 warm → subtract 6.6.
    assert got[1]["low_offset_f"] == -6.6


def test_a_lead_without_enough_days_learns_nothing():
    assert fs.corrections_from({"leads": [_lead(2, -5, 5, enough=False)]}) == []
    assert fs.corrections_from({"leads": [_lead(2, 0.4, -0.3)]}) == []


def test_the_route_is_empty_on_a_new_server(client):
    r = client.get("/api/devices/AA:00:00:00:00:01/forecast-correction", headers=H)
    assert r.status_code == 200 and r.json()["leads"] == []
    assert client.get("/api/devices/AA:00:00:00:00:01/forecast-correction").status_code == 401


def test_a_station_far_from_the_forecast_station_gets_no_correction(client, monkeypatch):
    """R25-A06 (the 2.5 additional review): the archive holds the sky at
    the server's first station, and a station 3,000 km away was handed
    that station's errors as its own correction."""
    from app import db
    monkeypatch.setattr(db.settings, "insights", True)
    near, far = "AA:BB:CC:25:00:11", "AA:BB:CC:25:00:12"

    async def seed():
        await db.upsert_device(near, {"name": "A hot station", "info": {
            "coords": {"coords": {"lat": 33.3, "lon": -112.0}}}})
        await db.upsert_device(far, {"name": "Z cool station", "info": {
            "coords": {"coords": {"lat": 60.0, "lon": -112.0}}}})
    asyncio.run(seed())
    body = client.get(f"/api/devices/{far}/forecast-correction", headers=H).json()
    assert body["leads"] == [] and body["supported"] is True
    assert body["coords_station"] == "A hot station"
    assert "too far" in body["reason"]
    here = client.get(f"/api/devices/{near}/forecast-correction", headers=H).json()
    assert here["reason"] is None and here["coords_station"] == "A hot station"


def test_an_unarchived_provider_says_it_cannot_be_corrected(client, monkeypatch):
    """R25-A07: a provider the archive never writes gets a reason, not an
    unexplained empty list."""
    from app import db
    monkeypatch.setattr(db.settings, "insights", True)
    body = client.get("/api/devices/AA:BB:CC:25:00:11/forecast-correction?provider=gfs",
                      headers=H).json()
    assert body["supported"] is False and body["leads"] == []
    assert "not archived" in body["reason"]


def test_twc_without_a_key_says_why(client, monkeypatch):
    """2.6: TWC is an archived provider now, but only a server with a WU
    key can fetch it; the reason names the key, not the archive."""
    from app import db
    monkeypatch.setattr(db.settings, "insights", True)
    monkeypatch.setattr(db.settings, "wu_api_key", None)
    body = client.get("/api/devices/AA:BB:CC:25:00:11/forecast-correction?provider=twc",
                      headers=H).json()
    assert body["supported"] is False and body["leads"] == []
    assert "Weather Underground key" in body["reason"]


def test_twc_with_a_key_is_a_supported_provider_that_is_still_learning(client, monkeypatch):
    from app import db
    monkeypatch.setattr(db.settings, "insights", True)
    asyncio.run(db.set_kv("wu_api_key", "k" * 32))
    body = client.get("/api/devices/AA:BB:CC:25:00:11/forecast-correction?provider=twc",
                      headers=H).json()
    assert body["supported"] is True and body["reason"] is None
    assert body["leads"] == []          # nothing archived yet, honestly empty


def _twc_daily():
    return {"time": ["2026-08-25", "2026-08-26"],
            "temperature_2m_max": [110.0, 107.0],
            "temperature_2m_min": [86.0, 84.0],
            "precipitation_probability_max": [10, 60],
            "precipitation_sum": [None, 0.25]}


def _om_daily():
    return {"time": ["2026-08-25", "2026-08-26"],
            "temperature_2m_max": [108.1, 106.0],
            "temperature_2m_min": [84.0, 82.5],
            "precipitation_probability_max": [20, 55],
            "precipitation_sum": [0.0, 0.12]}


async def _providers(db):
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT provider, valid_date, tmax_f, precip_in FROM forecast_snapshots "
            "ORDER BY provider, valid_date")).fetchall()
    return [tuple(r) for r in rows]


def test_the_snapshot_beat_archives_twc_beside_open_meteo_when_a_key_is_set(client, monkeypatch):
    """R25-A07's fix: the same beat, the same days, both providers, so a
    TWC user's scorecard and correction can fill like an Open-Meteo
    user's."""
    import time
    from app import db, forecast_snapshots as fs

    async def om(lat, lon):
        return _om_daily()

    async def twc(lat, lon):
        return _twc_daily()
    monkeypatch.setattr(fs, "_fetch_daily", om)
    monkeypatch.setattr(fs, "_fetch_twc", twc)
    devices = [{"mac": "AA", "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]
    asyncio.run(fs.check(devices, int(time.time() * 1000)))
    rows = asyncio.run(_providers(db))
    # 2.7 archives the Open-Meteo models too (stubbed out here by conftest).
    assert {r[0] for r in rows} == {"open-meteo", "twc"} < set(fs.ARCHIVED_PROVIDERS)
    twc_rows = [r for r in rows if r[0] == "twc"]
    assert twc_rows == [("twc", "2026-08-25", 110.0, None), ("twc", "2026-08-26", 107.0, 0.25)]


def test_without_a_key_the_beat_never_calls_twc(client, monkeypatch):
    """The real `_fetch_twc` gate: no key, no request (a 401 per beat
    would be an inline 15 s HTTP attempt on the alert tick)."""
    import time
    from app import db, forecast_snapshots as fs, forecast_twc
    monkeypatch.setattr(db.settings, "wu_api_key", None)
    calls = []

    async def om(lat, lon):
        return _om_daily()

    async def fetch(lat, lon, api_key):
        calls.append(api_key)
        return {"daily": _twc_daily()}
    monkeypatch.setattr(fs, "_fetch_daily", om)
    monkeypatch.setattr(fs, "_fetch_twc", fs._fetch_twc_live)   # the real gate
    monkeypatch.setattr(forecast_twc, "fetch", fetch)
    devices = [{"mac": "AA", "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]
    asyncio.run(fs.check(devices, int(time.time() * 1000)))
    assert calls == []
    assert {r[0] for r in asyncio.run(_providers(db))} == {"open-meteo"}
    # And with a key the same gate calls through, once, with that key.
    asyncio.run(db.set_kv("wu_api_key", "k" * 32))
    asyncio.run(db.set_kv("forecast_snapshots.last_ms", "0"))
    asyncio.run(fs.check(devices, int(time.time() * 1000)))
    assert calls == ["k" * 32]
    assert {r[0] for r in asyncio.run(_providers(db))} == {"open-meteo", "twc"}


def test_coordinates_stored_as_strings_are_read_like_the_forecast_station(client, monkeypatch):
    """CodeRabbit, PR #52: coords_device accepted numeric strings and
    _device_coords did not, so a nearby station was refused the correction."""
    from app import db
    monkeypatch.setattr(db.settings, "insights", True)
    a, b = "AA:BB:CC:25:00:21", "AA:BB:CC:25:00:22"

    async def seed():
        await db.upsert_device(a, {"name": "A", "info": {"coords": {"coords": {"lat": "33.30", "lon": "-112.00"}}}})
        await db.upsert_device(b, {"name": "B", "info": {"coords": {"coords": {"lat": "33.31", "lon": "-112.01"}}}})
    asyncio.run(seed())
    body = client.get(f"/api/devices/{b}/forecast-correction", headers=H).json()
    assert body["reason"] is None and body["coords_station"] == "A"


def test_boolean_coordinates_are_not_a_place(client, monkeypatch):
    """CodeRabbit, PR #52: float(True) is 1.0, so a boolean stored as a
    coordinate read as a real location."""
    from app import db, main
    mac = "AA:BB:CC:25:00:31"
    asyncio.run(db.upsert_device(mac, {"name": "Odd", "info": {"coords": {"coords": {"lat": True, "lon": False}}}}))
    assert asyncio.run(main._device_coords(mac)) is None
