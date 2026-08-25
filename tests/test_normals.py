"""Today-vs-normal (1.7): cache behavior of normals.today() and the
/api/devices/{mac}/normals endpoint's honest-absence contract."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import normals  # noqa: E402

_H = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def fake_kv(monkeypatch):
    kv = {}

    async def fake_get(key):
        return kv.get(key)

    async def fake_set(key, value):
        kv[key] = value

    monkeypatch.setattr(normals.db, "get_kv", fake_get)
    monkeypatch.setattr(normals.db, "set_kv", fake_set)
    return kv


def test_today_fetches_once_then_serves_from_cache(fake_kv, monkeypatch):
    searches, fetches = [], []

    async def fake_find(lat, lon):
        searches.append((lat, lon))
        return ("USW00023183", "PHOENIX AIRPORT, AZ US")

    async def fake_year(station_id):
        fetches.append(station_id)
        # Cover every possible "today" so the test passes on any date.
        return {f"{m:02d}-{d:02d}": {"high": 104.3, "low": 82.4,
                                     "mtd_precip": 0.70}
                for m in range(1, 13) for d in range(1, 32)}

    monkeypatch.setattr(normals, "_find_station", fake_find)
    monkeypatch.setattr(normals, "_fetch_year", fake_year)

    async def run():
        first = await normals.today(33.3004, -111.9378)
        assert first is not None
        assert first["normal_high"] == 104.3 and first["normal_low"] == 82.4
        assert first["station"] == "PHOENIX AIRPORT, AZ US"
        second = await normals.today(33.3004, -111.9378)
        assert second == first

    asyncio.run(run())
    # The decade-cache promise: one station lookup, one data fetch, however
    # many times the app asks.
    assert len(searches) == 1 and len(fetches) == 1
    # Keys are namespaced per location/station now (CodeRabbit PR #27 /
    # R6 finding 12) — two stations in different cities used to evict
    # each other's slot on every alternating request.
    assert json.loads(fake_kv["normals.data.USW00023183"])["station"] == "USW00023183"


def test_today_returns_none_outside_coverage(fake_kv, monkeypatch):
    async def fake_find(lat, lon):
        return None                     # non-US location

    monkeypatch.setattr(normals, "_find_station", fake_find)

    async def run():
        assert await normals.today(48.85, 2.35) is None   # Paris

    asyncio.run(run())


def test_fetch_year_parses_and_drops_sentinels(monkeypatch):
    import httpx

    rows = [
        {"DATE": "08-21", "DLY-TMAX-NORMAL": "104.3",
         "DLY-TMIN-NORMAL": "82.4", "MTD-PRCP-NORMAL": "0.70"},
        # Missing-value sentinel must vanish, not become a reading.
        {"DATE": "08-22", "DLY-TMAX-NORMAL": "-9999",
         "DLY-TMIN-NORMAL": "82.3"},
        {"DATE": "08-23", "DLY-TMAX-NORMAL": "junk"},
    ]

    def handler(request):
        return httpx.Response(200, json=rows)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(transport=httpx.MockTransport(handler)))

    async def run():
        days = await normals._fetch_year("USW00023183")
        assert days["08-21"] == {"high": 104.3, "low": 82.4, "mtd_precip": 0.70}
        assert days["08-22"] == {"low": 82.3}       # sentinel high dropped
        assert "08-23" not in days                  # nothing parseable

    asyncio.run(run())


def test_normals_endpoint_absent_without_coords(client):
    """A station with no coordinates answers available:false — the app must
    get an honest nothing, never an invented comparison."""
    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF"}, "timestamp_utc": ts,
                      "outdoor": {"tempf": 90}, "source": "test"})
    r = client.get("/api/devices/AA:BB:CC:DD:EE:FF/normals", headers=_H)
    assert r.status_code == 200
    assert r.json() == {"available": False}
    # Unknown mac: same honest shape, not a 500.
    r = client.get("/api/devices/11:22:33:44:55:66/normals", headers=_H)
    assert r.json() == {"available": False}
