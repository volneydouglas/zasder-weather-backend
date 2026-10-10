"""The hourly forecast behind the charts' dashed continuation (2.6, 12a)."""
import httpx

H = {"Authorization": "Bearer test-api-token"}
HOUR = 3_600_000
NOW = 1_791_300_000_000 - 1_791_300_000_000 % HOUR + 20 * 60_000   # 20 min past an hour


def _body(hours=4):
    t0 = (NOW - NOW % HOUR) // 1000 - 3600          # one past hour, then the rest
    return {"hourly": {
        "time": [t0 + i * 3600 for i in range(hours + 1)],
        "temperature_2m": [60.0, 61.5, 62.0, None, 64.0][: hours + 1],
        "pressure_msl": [1013.25] * (hours + 1),
        "precipitation": [0.0, 0.02, float("nan"), 0.1, 0.0][: hours + 1],
        "uv_index": [None] * (hours + 1),
        "shortwave_radiation_instant": [0.0, 120.0, 340.0, 410.0, 380.0][: hours + 1],
    }}


def test_transform_starts_at_this_hour_in_station_units():
    from app import forecast_hourly as fh
    out = fh.transform(_body(), hours=3, now_ms=NOW)
    assert out["times_ms"][0] == NOW - NOW % HOUR, "the past hour is dropped, this hour kept"
    assert len(out["times_ms"]) == 3
    assert out["series"]["tempf"] == [61.5, 62.0, None]          # absent stays absent
    assert out["series"]["baromrelin"] == [29.921] * 3            # hPa -> inHg
    assert out["series"]["hourlyrainin"] == [0.02, None, 0.1]     # NaN is not a value
    assert "uv" not in out["series"], "a field the model sent no values for is left out"
    # The instant value, not the preceding hour's mean (CodeRabbit, PR #85).
    assert out["series"]["solarradiation"] == [120.0, 340.0, 410.0]
    assert "shortwave_radiation_instant" in fh.FIELDS and "shortwave_radiation" not in fh.FIELDS


def test_the_route_answers_a_stations_hours_and_no_coordinates(client, monkeypatch):
    from app import forecast_hourly as fh
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:71", "name": "Yard",
                                 "coords": {"lat": 40.33, "lon": -79.70}},
                      "timestamp_utc": "2026-10-06T12:00:00Z", "outdoor": {"tempf": 60}})
    asked = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            asked.append(params)
            return httpx.Response(200, json=_body(),
                                  request=httpx.Request("GET", url))
    monkeypatch.setattr(fh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(fh.time, "time", lambda: NOW / 1000)
    r = client.get("/api/forecast/hourly", headers=H,
                   params={"mac": "AABBCCDDEE71", "hours": 2})
    assert r.status_code == 200, r.text
    j = r.json()
    assert len(j["times_ms"]) == 2 and j["series"]["tempf"] == [61.5, 62.0]
    assert "latitude" not in j and "longitude" not in j
    assert asked[0]["latitude"] == 40.33 and asked[0]["timeformat"] == "unixtime"
    # A second chart within the half hour does not ask the model again.
    client.get("/api/forecast/hourly", headers=H, params={"mac": "AA:BB:CC:DD:EE:71"})
    assert len(asked) == 1


def test_a_station_without_a_location_is_404(client):
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:72", "name": "Garage"},
                      "timestamp_utc": "2026-10-06T12:00:00Z", "outdoor": {"tempf": 60}})
    assert client.get("/api/forecast/hourly", headers=H,
                      params={"mac": "AA:BB:CC:DD:EE:72"}).status_code == 404
    assert client.get("/api/forecast/hourly", headers=H,
                      params={"hours": 99}).status_code == 422


def test_an_upstream_failure_is_502_not_500(client, monkeypatch):
    from app import forecast_hourly as fh
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:73", "name": "Yard",
                                 "coords": {"lat": 40.3, "lon": -79.7}},
                      "timestamp_utc": "2026-10-06T12:00:00Z", "outdoor": {"tempf": 60}})

    async def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(fh, "fetch", boom)
    assert client.get("/api/forecast/hourly", headers=H,
                      params={"mac": "AA:BB:CC:DD:EE:73"}).status_code == 502


def test_without_a_station_it_uses_the_first_located_station(client, monkeypatch):
    """CodeRabbit, PR #87: the no-mac fallback called fs._coords, renamed
    to fs.coords by the sky-identity change merged beside it, so a request
    without a station on a server with no forecast location was a 500."""
    from app import forecast_hourly as fh
    from app.config import settings
    monkeypatch.setattr(settings, "forecast_lat", None)
    monkeypatch.setattr(settings, "forecast_lon", None)
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:74", "name": "Yard",
                                 "coords": {"lat": 40.33, "lon": -79.70}},
                      "timestamp_utc": "2026-10-06T12:00:00Z", "outdoor": {"tempf": 60}})
    asked = []

    async def fake_fetch(lat, lon, hours=48, now_ms=None):
        asked.append((lat, lon))
        return {"times_ms": [], "series": {}, "source": "open-meteo"}
    monkeypatch.setattr(fh, "fetch", fake_fetch)
    r = client.get("/api/forecast/hourly", headers=H)
    assert r.status_code == 200, r.text
    assert asked == [(40.33, -79.70)]
