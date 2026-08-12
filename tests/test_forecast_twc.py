"""TWC forecast source — transform mapping and endpoint fallback ladder."""
import pytest

_H = {"Authorization": "Bearer test-api-token"}

TWC_FIXTURE = {
    "validTimeLocal": ["2026-08-12T07:00:00-0700", "2026-08-13T07:00:00-0700"],
    "calendarDayTemperatureMax": [None, 108],      # day-0 null after 3pm
    "calendarDayTemperatureMin": [84, 86],
    "temperatureMax": [106, 108],
    "temperatureMin": [84, 86],
    "daypart": [{
        # Interleaved day/night halves; day-0's day half already null.
        "precipChance": [None, 10, 30, 20],
        "windSpeed": [None, 12, 18, 9],
        "windDirection": [None, 200, 240, 180],
        "iconCode": [None, 33, 38, 29],
    }],
}


def test_transform_maps_to_open_meteo_shape():
    from app import forecast_twc
    out = forecast_twc.transform(TWC_FIXTURE)
    d = out["daily"]
    assert out["source"] == "twc"
    assert d["time"] == ["2026-08-12", "2026-08-13"]
    # Day-0 calendar max is null → falls back to temperatureMax.
    assert d["temperature_2m_max"] == [106.0, 108.0]
    assert d["temperature_2m_min"] == [84.0, 86.0]
    # Worst precip chance of the surviving halves; peak wind.
    assert d["precipitation_probability_max"] == [10, 30]
    assert d["wind_speed_10m_max"] == [12.0, 18.0]
    # Day-0 daytime direction/icon null → night half used.
    assert d["wind_direction_10m_dominant"] == [200.0, 240.0]
    # Icon 33 (fair/night) → WMO 1; icon 38 (t-storm) → 95.
    assert d["weather_code"] == [1, 95]


def test_icon_36_hot_maps_to_clear_not_overcast():
    """R3-89: TWC iconCode 36 is "Hot" — a hot clear day must not fall
    through to the overcast default on exactly the hottest days."""
    from app import forecast_twc
    fx = dict(TWC_FIXTURE)
    fx["daypart"] = [{"precipChance": [0, 0, 0, 0], "windSpeed": [1, 1, 1, 1],
                      "windDirection": [0, 0, 0, 0], "iconCode": [36, 36, 36, 36]}]
    out = forecast_twc.transform(fx)
    assert out["daily"]["weather_code"] == [0, 0]


def test_transform_unknown_icon_defaults_overcast():
    from app import forecast_twc
    fx = dict(TWC_FIXTURE)
    fx["daypart"] = [{"precipChance": [0, 0, 0, 0], "windSpeed": [1, 1, 1, 1],
                      "windDirection": [0, 0, 0, 0], "iconCode": [99, 99, 99, 99]}]
    out = forecast_twc.transform(fx)
    assert out["daily"]["weather_code"] == [3, 3]


@pytest.fixture()
def om_stub(monkeypatch):
    """Stub the Open-Meteo HTTP call so endpoint tests never hit the net."""
    import httpx

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"daily": {"time": ["2026-08-12"], "weather_code": [0],
                              "temperature_2m_max": [100.0],
                              "temperature_2m_min": [80.0],
                              "precipitation_probability_max": [0],
                              "wind_speed_10m_max": [5.0],
                              "wind_direction_10m_dominant": [180.0]}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            assert "open-meteo" in url
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_default_source_is_open_meteo_marked(client, om_stub):
    body = client.get("/api/forecast?lat=33.3&lon=-111.9", headers=_H).json()
    assert body["source"] == "open-meteo"
    assert "fallback_from" not in body


def test_twc_without_key_falls_back_marked(client, om_stub):
    body = client.get("/api/forecast?lat=33.3&lon=-111.9&source=twc",
                      headers=_H).json()
    assert body["source"] == "open-meteo"
    assert body["fallback_from"] == "twc"


def test_twc_with_key_serves_twc(client, om_stub, monkeypatch):
    from app.config import settings
    from app import forecast_twc
    monkeypatch.setattr(settings, "wu_api_key", "k" * 32)
    async def fake_fetch(lat, lon, key):
        assert key == "k" * 32
        return forecast_twc.transform(TWC_FIXTURE)
    monkeypatch.setattr(forecast_twc, "fetch", fake_fetch)
    body = client.get("/api/forecast?lat=33.3&lon=-111.9&source=twc",
                      headers=_H).json()
    assert body["source"] == "twc"
    assert len(body["daily"]["time"]) == 2


def test_twc_failure_falls_back_to_open_meteo(client, om_stub, monkeypatch):
    """Dead key / WU outage → Open-Meteo for THIS response, marked, 200."""
    from app.config import settings
    from app import forecast_twc
    monkeypatch.setattr(settings, "wu_api_key", "k" * 32)
    async def dead(lat, lon, key):
        raise PermissionError("401")
    monkeypatch.setattr(forecast_twc, "fetch", dead)
    r = client.get("/api/forecast?lat=33.3&lon=-111.9&source=twc", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "open-meteo"
    assert body["fallback_from"] == "twc"


def test_bad_source_rejected(client):
    assert client.get("/api/forecast?lat=1&lon=1&source=noaa",
                      headers=_H).status_code == 422


def test_wu_key_app_managed_roundtrip(client):
    # Nothing configured.
    st = client.get("/api/config/wu-key", headers=_H).json()
    assert st == {"configured": False, "source": "none"}
    # Store (write-gated), status flips to app; value never returned.
    assert client.put("/api/config/wu-key",
                      json={"api_key": "k" * 32}).status_code == 401
    r = client.put("/api/config/wu-key", headers=_H, json={"api_key": "k" * 32})
    assert r.json()["source"] == "app"
    st = client.get("/api/config/wu-key", headers=_H).json()
    assert st["configured"] is True and "k" not in str(st.values())
    # Too-short rejected; "" clears.
    assert client.put("/api/config/wu-key", headers=_H,
                      json={"api_key": "short"}).status_code == 400
    r = client.put("/api/config/wu-key", headers=_H, json={"api_key": ""})
    assert r.json() == {"ok": True, "configured": False, "source": "none"}


def test_forecast_uses_app_stored_key(client, om_stub, monkeypatch):
    """The TWC fetch must receive the app-stored key, not just the env one."""
    from app import forecast_twc
    seen = {}
    async def spy(lat, lon, key):
        seen["key"] = key
        return forecast_twc.transform(TWC_FIXTURE)
    monkeypatch.setattr(forecast_twc, "fetch", spy)
    client.put("/api/config/wu-key", headers=_H, json={"api_key": "a" * 32})
    body = client.get("/api/forecast?lat=33.3&lon=-111.9&source=twc",
                      headers=_H).json()
    assert body["source"] == "twc"
    assert seen["key"] == "a" * 32


# ───────────────── R3-54: a 200 with an empty payload is NOT a forecast ─────────────────

def test_transform_empty_payload_raises():
    """TWC answering 200 with {} used to transform into valid-shaped all-empty
    arrays and be served as a successful "twc" response — the iOS strip went
    blank instead of falling back to Open-Meteo."""
    from app import forecast_twc
    with pytest.raises(ValueError):
        forecast_twc.transform({})
    with pytest.raises(ValueError):
        forecast_twc.transform({"validTimeLocal": []})


def test_twc_empty_200_falls_back_to_open_meteo(client, om_stub, monkeypatch):
    """Endpoint-level: the empty-payload ValueError must route through the
    blanket except into the MARKED Open-Meteo fallback."""
    from app.config import settings
    from app import forecast_twc
    monkeypatch.setattr(settings, "wu_api_key", "k" * 32)
    async def empty_200(lat, lon, key):
        return forecast_twc.transform({})       # raises like the real fetch
    monkeypatch.setattr(forecast_twc, "fetch", empty_200)
    r = client.get("/api/forecast?lat=33.3&lon=-111.9&source=twc", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "open-meteo"
    assert body["fallback_from"] == "twc"


# ───────────────── R3-144: transform edge robustness ─────────────────

def test_transform_tolerates_missing_and_short_daypart():
    """`daypart` absent entirely, or its arrays shorter than validTimeLocal —
    both must degrade to None/overcast fields, never raise."""
    from app import forecast_twc
    # Absent daypart.
    fx = {k: v for k, v in TWC_FIXTURE.items() if k != "daypart"}
    out = forecast_twc.transform(fx)
    d = out["daily"]
    assert d["time"] == ["2026-08-12", "2026-08-13"]
    assert d["precipitation_probability_max"] == [None, None]
    assert d["wind_speed_10m_max"] == [None, None]
    assert d["weather_code"] == [3, 3]
    # Daypart arrays shorter than 2×days (the at() bounds guard).
    fx = dict(TWC_FIXTURE)
    fx["daypart"] = [{"precipChance": [40], "windSpeed": [7],
                      "windDirection": [90], "iconCode": [32]}]
    out = forecast_twc.transform(fx)
    d = out["daily"]
    assert d["precipitation_probability_max"] == [40, None]
    assert d["wind_speed_10m_max"] == [7.0, None]
    assert d["weather_code"] == [0, 3]


def test_transform_accepts_float_icon_codes():
    """TWC emits numericPrecision=decimal — icon codes can arrive as 33.0."""
    from app import forecast_twc
    fx = dict(TWC_FIXTURE)
    fx["daypart"] = [{"precipChance": [0, 0, 0, 0], "windSpeed": [1, 1, 1, 1],
                      "windDirection": [0, 0, 0, 0],
                      "iconCode": [33.0, 33.0, 38.0, 38.0]}]
    out = forecast_twc.transform(fx)
    assert out["daily"]["weather_code"] == [1, 95]
