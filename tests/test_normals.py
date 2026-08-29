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


def test_ring_search_prefers_the_closest_hit(monkeypatch):
    """The Fountain Hills lesson (2026-08-27): one wide bbox handed
    Chandler a foothills station 28 miles out because NCEI returned it
    first. Rings must take a small-radius hit before ever widening —
    and still find a station that only the widest ring covers."""
    import asyncio

    import httpx

    from app import normals

    def station(name):
        return {"results": [{"stations": [{
            "id": f"GHCND:{name}", "name": name,
            "dataTypes": [{"id": "DLY-TMAX-NORMAL", "coverage": 1.0}]}]}]}

    calls: list[float] = []
    real = httpx.AsyncClient

    def make_handler(hits_by_ring):
        def handler(request: httpx.Request) -> httpx.Response:
            # bbox = "latN,lonW,latS,lonE" — recover the half-side.
            north = float(request.url.params["bbox"].split(",")[0])
            ring = round(north - 33.30, 2)
            calls.append(ring)
            body = hits_by_ring.get(ring) or {"results": []}
            return httpx.Response(200, json=body)
        return handler

    # Closest ring has a station: no widening happens.
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(
        transport=httpx.MockTransport(
            make_handler({0.08: station("CHANDLER HEIGHTS")})), **kw))
    got = asyncio.run(normals._find_station(33.30, -111.94))
    assert got == ("GHCND:CHANDLER HEIGHTS", "CHANDLER HEIGHTS")
    assert calls == [0.08]

    # Only the widest ring has one: still found, after walking out.
    calls.clear()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(
        transport=httpx.MockTransport(
            make_handler({0.35: station("FOUNTAIN HILLS")})), **kw))
    got = asyncio.run(normals._find_station(33.30, -111.94))
    assert got == ("GHCND:FOUNTAIN HILLS", "FOUNTAIN HILLS")
    assert calls == [0.08, 0.15, 0.35]


def test_malformed_ncei_responses_widen_the_ring_instead_of_raising(
        monkeypatch):
    """R14 carry: the whole search response is remote input. A valid-JSON
    scalar body, junk nested nodes, and a covered station with a garbage
    id must each keep the walk alive — the wider ring still answers, and
    nothing raises out of _find_station."""
    import asyncio

    import httpx

    from app import normals

    hostile_by_ring = {
        # Ring 1: valid JSON, wrong shape entirely.
        0.08: 42,
        # Ring 2: every node malformed a different way — non-dict result,
        # non-list stations, non-dict station, junk dataTypes, and a
        # covered station whose id is whitespace.
        0.15: {"results": [
            "not-a-dict",
            {"stations": "not-a-list"},
            {"stations": ["not-a-dict",
                          {"id": 7, "dataTypes": "junk"},
                          {"id": "   ",
                           "dataTypes": [{"id": "DLY-TMAX-NORMAL",
                                          "coverage": 1.0}]}]},
        ]},
        # Ring 3: a real station — with padding the guard must strip.
        0.35: {"results": [{"stations": [{
            "id": "  GHCND:USW00023183  ", "name": "  PHOENIX AIRPORT  ",
            "dataTypes": [{"id": "DLY-TMAX-NORMAL", "coverage": 1.0}]}]}]},
    }
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        north = float(request.url.params["bbox"].split(",")[0])
        ring = round(north - 33.30, 2)
        return httpx.Response(200, json=hostile_by_ring[ring])

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(
        transport=httpx.MockTransport(handler), **kw))
    got = asyncio.run(normals._find_station(33.30, -111.94))
    # Stripped id AND name (R14): a padded id must not reach the NCEI
    # data query verbatim.
    assert got == ("GHCND:USW00023183", "PHOENIX AIRPORT")
