"""Ecowitt cloud client + poller transform (2.0).

The fixture is built from doc.ecowitt.net's own real_time example
("Getting Device Real-Time Data", trimmed to the groups the poller reads),
with the two rain families deliberately DIFFERENT so the tipping-wins rule
is observable, and the battery leaves set to exercise every convention the
doc names (volts, 1-is-low flag, 0-5 level).
"""
from __future__ import annotations

import asyncio
import importlib
import os

import httpx
import pytest

# Set BEFORE importing app: config.py reads the environment at import time and
# .env carries a placeholder that fails the length floor. The `test-` prefix is
# exempt, but only under pytest (see Settings' token validator).
os.environ.setdefault("API_TOKEN", "test-api-token")

from app.ecowitt_cloud_client import (EcowittCloudClient,  # noqa: E402
                                      EcowittCloudError, UNIT_PARAMS, native)
from app.ecowitt_cloud_poller import (EcowittCloudPoller,  # noqa: E402
                                      build_payload, history_payloads,
                                      parse_macs, reading_time, wire_form)

MAC = "88:F1:55:05:D1:63"
T = "1768980454"    # 2026-01-21T07:27:34Z


def leaf(value, unit="", time=T):
    return {"time": time, "unit": unit, "value": str(value)}


def real_time_data(**over):
    """A GW3000 with a WS90 (haptic rain, `rainfall_piezo`) AND a WH40
    tipping gauge (`rainfall`), an indoor sensor, and a WH57."""
    d = {
        "outdoor": {
            "temperature": leaf("104.5", "ºF"),
            "feels_like": leaf("108.2", "ºF"),
            "app_temp": leaf("106.1", "ºF"),
            "dew_point": leaf("55.4", "ºF"),
            "humidity": leaf("21", "%"),
        },
        "indoor": {
            "temperature": leaf("82.4", "ºF"),
            "humidity": leaf("38", "%"),
        },
        "solar_and_uvi": {
            "solar": leaf("847.95", "W/m²"),
            "uvi": leaf("8", ""),
        },
        "rainfall": {                       # WH40 tipping gauge
            "rain_rate": leaf("0.24", "in/hr"),
            "daily": leaf("0.12", "in"),
            "event": leaf("0.12", "in"),
            "hourly": leaf("0.10", "in"),
            "weekly": leaf("0.12", "in"),
            "monthly": leaf("1.31", "in"),
            "yearly": leaf("8.25", "in"),
        },
        "rainfall_piezo": {                 # WS90 haptic, phantom-prone
            "rain_rate": leaf("0.30", "in/hr"),
            "daily": leaf("0.19", "in"),
            "event": leaf("0.19", "in"),
            "hourly": leaf("0.15", "in"),
            "weekly": leaf("0.19", "in"),
            "monthly": leaf("1.40", "in"),
            "yearly": leaf("9.01", "in"),
        },
        "wind": {
            "wind_speed": leaf("4.7", "mph"),
            "wind_gust": leaf("9.2", "mph"),
            "wind_direction": leaf("193", "º"),
        },
        "pressure": {
            "relative": leaf("29.864", "inHg"),
            "absolute": leaf("28.598", "inHg"),
        },
        "lightning": {
            "distance": leaf("19", "mi", time="1768976854"),
            "count": leaf("3", ""),
        },
        "battery": {
            "haptic_array_battery": leaf("3.28", "V"),      # WS90
            "rainfall_sensor": leaf("1.6", "V"),            # WH40
            "lightning_sensor": leaf("4", ""),              # WH57 level
            "t_rh_p_sensor": leaf("0", ""),                 # WH25 flag, ok
        },
        "temp_and_humidity_ch1": {
            "temperature": leaf("70.0", "ºF"),
            "humidity": leaf("85", "%"),
        },
        "soil_ch1": {"soilmoisture": leaf("79", "%"), "ad": leaf("163", "")},
        "temp_ch1": {"temperature": leaf("64.3", "ºF")},
        "water_leak": {"leak_ch1": leaf("0", ""), "leak_ch2": leaf("2", "")},
    }
    d.update(over)
    return d


DEVICE = {"id": 1050, "name": "Backyard GW3000", "mac": MAC, "type": 1,
          "date_zone_id": "America/Phoenix", "createtime": 1642561960,
          "longitude": -111.8413, "latitude": 33.3062,
          "stationtype": "GW3000B_V1.2.0"}


# ───────────────────────────── the transform ─────────────────────────────

def test_build_payload_full_station():
    p = build_payload(MAC, real_time_data(), None, DEVICE)
    assert p is not None
    assert p["source"] == "ecowitt-cloud"
    assert p["timestamp_utc"] == "2026-01-21T07:27:34+00:00"
    d = p["device"]
    assert d["id"] == MAC, "the REAL MAC, not a synthetic one"
    assert d["name"] == "Backyard GW3000"
    assert d["model"] == "GW3000B_V1.2.0"
    assert d["coords"] == {"lat": 33.3062, "lon": -111.8413}
    assert d["battery_outdoor"] == "normal"        # WS90 3.28 V

    o = p["outdoor"]
    assert o["tempf"] == 104.5
    assert o["dew_point_f"] == 55.4                # from the API, not derived
    assert o["feels_like"] == 108.2
    assert o["humidity"] == 21
    assert o["uv"] == 8
    assert o["solar_wm2"] == 847.95
    assert p["indoor"] == {"tempf": 82.4, "humidity": 38}
    assert p["wind"] == {"speed_mph": 4.7, "gust_mph": 9.2, "direction": 193}
    assert p["pressure"] == {"relative_inhg": 29.864, "absolute_inhg": 28.598}
    # Lightning: nearest strike + when, in miles; today's total is NOT an
    # interval count and stays out of the accumulating column.
    assert p["lightning"] == {"last_distance_mi": 19.0,
                              "last_strike_ms": 1768976854000}
    # Channel sensors ride `extra` under AWN names (ecowitt._channels).
    assert p["extra"] == {"temp1f": 70.0, "humidity1": 85.0,
                          "soilhum1": 79.0, "soiltemp1f": 64.3, "leak1": 0.0}


def test_tipping_gauge_beats_piezo_per_field():
    """Both rain families present: the WH40 wins every field, and the rate
    key feeds hourly_in (the ecowitt.py rule, reused rather than copied)."""
    p = build_payload(MAC, real_time_data())
    assert p["rain"] == {"hourly_in": 0.24, "event_in": 0.12, "daily_in": 0.12,
                         "weekly_in": 0.12, "monthly_in": 1.31, "yearly_in": 8.25}


def test_piezo_fills_only_the_holes():
    d = real_time_data()
    del d["rainfall"]["daily"]
    del d["rainfall"]["rain_rate"]
    del d["rainfall"]["hourly"]
    p = build_payload(MAC, d)
    assert p["rain"]["daily_in"] == 0.19          # piezo fallback
    assert p["rain"]["hourly_in"] == 0.30         # piezo rate fallback
    assert p["rain"]["event_in"] == 0.12          # tipping still wins
    # A piezo-only station (WS90 without a WH40) simply uses piezo.
    d = real_time_data()
    del d["rainfall"]
    p = build_payload(MAC, d)
    assert p["rain"]["daily_in"] == 0.19


def test_battery_conventions_map_through_ecowitt_py():
    """Every convention the doc names, normalized to the stored 1 = ok /
    0 = low under the wire names health_watch already reads."""
    p = build_payload(MAC, real_time_data())
    assert p["batteries"] == {"wh90batt": 1.0, "wh40batt": 1.0,
                              "batt_lightning": 1.0, "wh25batt": 1.0}
    # WS90 below the 2.4 V array line → low, and it drives battery_outdoor.
    d = real_time_data()
    d["battery"]["haptic_array_battery"] = leaf("2.1", "V")
    d["battery"]["lightning_sensor"] = leaf("1", "")      # level ≤ 1 = low
    d["battery"]["t_rh_p_sensor"] = leaf("1", "")         # flag 1 = LOW
    p = build_payload(MAC, d)
    assert p["device"]["battery_outdoor"] == "low"
    assert p["batteries"]["wh90batt"] == 0.0
    assert p["batteries"]["batt_lightning"] == 0.0
    assert p["batteries"]["wh25batt"] == 0.0
    # A WS80 (sonic) or a classic WH65 array flag drives it too.
    d = real_time_data(battery={"sonic_array": leaf("2.3", "V")})
    assert build_payload(MAC, d)["device"]["battery_outdoor"] == "low"
    d = real_time_data(battery={"sensor_array": leaf("1", "")})
    assert build_payload(MAC, d)["device"]["battery_outdoor"] == "low"
    d = real_time_data(battery={"sensor_array": leaf("0", "")})
    assert build_payload(MAC, d)["device"]["battery_outdoor"] == "normal"
    # No array battery leaf at all: no claim.
    d = real_time_data(battery={})
    assert "battery_outdoor" not in build_payload(MAC, d)["device"]


def test_units_are_requested_native_and_guarded():
    """The store is API-native (°F, mph, inHg, inches, W/m²). The request
    pins those unit ids, and a leaf that still arrives in something else is
    converted on its own `unit` string rather than trusted."""
    assert UNIT_PARAMS == {"temp_unitid": 2, "pressure_unitid": 4,
                           "wind_speed_unitid": 9, "rainfall_unitid": 13,
                           "solar_irradiance_unitid": 16}
    d = real_time_data()
    d["outdoor"]["temperature"] = leaf("40.3", "ºC")
    d["pressure"]["relative"] = leaf("1011.4", "hPa")
    d["wind"]["wind_speed"] = leaf("2.1", "m/s")
    d["wind"]["wind_gust"] = leaf("14.8", "km/h")
    d["rainfall"]["daily"] = leaf("3.05", "mm")
    d["solar_and_uvi"]["solar"] = leaf("1200", "lux")   # not convertible
    p = build_payload(MAC, d)
    assert p["outdoor"]["tempf"] == pytest.approx(104.54, abs=0.01)
    assert p["pressure"]["relative_inhg"] == pytest.approx(29.867, abs=0.002)
    assert p["wind"]["speed_mph"] == pytest.approx(4.70, abs=0.01)
    assert p["wind"]["gust_mph"] == pytest.approx(9.20, abs=0.01)
    assert p["rain"]["daily_in"] == pytest.approx(0.12, abs=0.001)
    assert "solar_wm2" not in p["outdoor"], "lux must not be stored as W/m²"
    # Face value when the unit is the native one, blank, or unknown.
    assert native("temp", 70.0, "ºF") == 70.0
    assert native("temp", 70.0, "") == 70.0
    assert native("rain", 0.5, "in") == 0.5
    assert native("wind", 3.0, "mph") == 3.0


def test_timestamp_is_the_newest_core_group(): 
    """R18 finding 2: the reading is as fresh as its freshest sensor. The
    outdoor temperature used to stamp the whole reading, so an offline
    array froze the station while wind and rain kept arriving."""
    d = real_time_data()
    d["outdoor"]["temperature"]["time"] = "1768980500"
    d["wind"]["wind_speed"]["time"] = "1768980600"      # newer: it stamps the reading
    assert reading_time(d) == 1768980600
    p = build_payload(MAC, d)
    assert p["timestamp_utc"] == "2026-01-21T07:30:00+00:00"
    assert "tempf" in p["outdoor"], "100 s behind is the same reading"
    # Indoor-only console: the newest core-group leaf time stands in.
    d = {"indoor": {"temperature": leaf("70", "ºF", time="1768980300"),
                    "humidity": leaf("40", "%", time="1768980360")}}
    assert reading_time(d) == 1768980360
    p = build_payload(MAC, d)
    assert p["timestamp_utc"].startswith("2026-01-21T")
    assert "outdoor" not in p


def test_partial_and_empty_readings():
    """A station reporting only temperature must still ingest, without
    inventing a 0 mph wind or a 0.00 in rain total for hardware that said
    nothing; nothing at all is None, not a payload."""
    p = build_payload(MAC, {"outdoor": {"temperature": leaf("68.0", "ºF")}})
    assert p["outdoor"] == {"tempf": 68.0}
    for k in ("wind", "rain", "pressure", "indoor", "lightning", "extra"):
        assert k not in p
    assert build_payload(MAC, {}) is None
    assert build_payload(MAC, {"battery": {"sensor_array": leaf("0")}}) is None
    # Blank / dash / non-numeric values are silence, not zero.
    p = build_payload(MAC, {"outdoor": {"temperature": leaf("", "ºF"),
                                        "humidity": leaf("--", "%")},
                            "wind": {"wind_speed": leaf("4.0", "mph")}})
    assert "outdoor" not in p
    assert p["wind"] == {"speed_mph": 4.0}


def test_configured_name_and_offline_leak_channel():
    p = build_payload(MAC, real_time_data(), "Chandler", DEVICE)
    assert p["device"]["name"] == "Chandler"
    # leak_ch2 = 2 (offline) was dropped; leak_ch1 = 0 (normal) kept.
    assert p["extra"]["leak1"] == 0.0 and "leak2" not in p["extra"]
    # No list entry: still a payload, just without name/model/coords.
    p = build_payload(MAC, real_time_data())
    assert set(p["device"]) == {"id", "battery_outdoor"}
    # (0, 0) coordinates are the API's "unset", not a station in the Gulf
    # of Guinea.
    p = build_payload(MAC, real_time_data(), None,
                      {"latitude": 0, "longitude": 0})
    assert "coords" not in p["device"]


def test_wire_form_reuses_the_gateway_vocabulary():
    form = wire_form(real_time_data())
    assert form["dailyrainin"] == 0.12 and form["drain_piezo"] == 0.19
    assert form["rainratein"] == 0.24 and form["rrain_piezo"] == 0.30
    assert form["wh90batt"] == 3.28 and form["wh40batt"] == 1.6
    assert form["wh57batt"] == 4.0 and form["wh25batt"] == 0.0
    assert form["temp1f"] == 70.0 and form["soilmoisture1"] == 79.0
    assert form["tf_ch1"] == 64.3 and form["leak_ch1"] == 0.0


def test_parse_macs():
    assert parse_macs("88:f1:55:05:d1:63, 88F155050000 ,,") == [
        "88:F1:55:05:D1:63", "88:F1:55:05:00:00"]
    assert parse_macs("") == [] and parse_macs(None) == []
    assert parse_macs("not-a-mac") == []


def test_history_pivot_is_chronological_and_per_epoch():
    data = {
        "outdoor": {
            "temperature": {"unit": "°F", "list": {"1768363200": "58.0",
                                                    "1768348800": "44.3"}},
            "humidity": {"unit": "%", "list": {"1768348800": "47",
                                                "1768363200": "49"}},
        },
        "rainfall": {
            "daily": {"unit": "in", "list": {"1768348800": "0.00",
                                              "1768363200": "0.04"}},
        },
        "wind": {"wind_speed": {"unit": "mph", "list": {"1768363200": "3.1"}}},
    }
    rows = history_payloads(MAC, data, "Chandler", DEVICE)
    assert [r["timestamp_utc"] for r in rows] == ["2026-01-14T00:00:00+00:00",
                                                  "2026-01-14T04:00:00+00:00"]
    assert rows[0]["outdoor"] == {"tempf": 44.3, "humidity": 47}
    assert rows[0]["rain"] == {"daily_in": 0.0}
    assert "wind" not in rows[0]
    assert rows[1]["wind"] == {"speed_mph": 3.1}
    assert rows[1]["device"]["name"] == "Chandler"
    assert history_payloads(MAC, {}) == []
    assert history_payloads(MAC, {"outdoor": "junk"}) == []


# ───────────────────────────── the client ────────────────────────────────

def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return original(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)


def test_error_never_carries_the_keys():
    """Both keys ride as QUERY PARAMS and httpx embeds the full URL in
    HTTPStatusError; the log must not get either secret."""
    planted_app = "A1B2C3D4-0000-4444-8888-DEADBEEF0001"
    planted_api = "b2c3d4e5-1111-4444-8888-deadbeef0002"
    request = httpx.Request(
        "GET", "https://api.ecowitt.net/api/v3/device/list"
               f"?application_key={planted_app}&api_key={planted_api}")
    response = httpx.Response(401, request=request)
    err = EcowittCloudError.from_http(
        httpx.HTTPStatusError("401", request=request, response=response))
    assert planted_app not in str(err) and planted_api not in str(err)
    assert "401" in str(err) and "/api/v3/device/list" in str(err)


async def test_result_code_failure_raises_without_echoing_keys(monkeypatch):
    """A bad key is HTTP 200 + code 40010, so a 2xx alone is nothing — an
    unauthorized pair would otherwise look like a quiet station and the
    poller would report healthy forever. The message must survive scrubbing
    even when the API echoes a key back."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={
            "code": 40010, "msg": "Illegal application_key parameter APPKEY123",
            "time": "1578988481", "data": []})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(EcowittCloudError) as ei:
        await EcowittCloudClient("APPKEY123", "APIKEY456").list_devices()
    assert "40010" in str(ei.value) and "invalid application key" in str(ei.value)
    assert "APPKEY123" not in str(ei.value)
    assert seen["application_key"] == "APPKEY123" and seen["api_key"] == "APIKEY456"


async def test_system_busy_code_is_an_error(monkeypatch):
    def busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -1, "msg": "busy", "data": []})
    _patch_transport(monkeypatch, busy)
    with pytest.raises(EcowittCloudError) as ei:
        await EcowittCloudClient("a", "b").real_time(MAC)
    assert "busy" in str(ei.value)


async def test_real_time_requests_native_units_and_empty_data_is_quiet(monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        seen["path"] = request.url.path
        # A station silent for 2h+: code 0 with an EMPTY LIST for data.
        return httpx.Response(200, json={"code": 0, "msg": "success",
                                         "time": T, "data": []})

    _patch_transport(monkeypatch, handler)
    assert await EcowittCloudClient("a", "b").real_time(MAC) == {}
    assert seen["path"] == "/api/v3/device/real_time"
    assert seen["mac"] == MAC and seen["call_back"] == "all"
    for k, v in UNIT_PARAMS.items():
        assert seen[k] == str(v)


async def test_list_devices_pages_and_history_window(monkeypatch):
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        params["path"] = request.url.path
        calls.append(params)
        if request.url.path.endswith("/device/list"):
            page = int(params.get("page", "1"))
            return httpx.Response(200, json={
                "code": 0, "msg": "success", "time": T,
                "data": {"total": 2, "totalPage": 2, "pageNum": page,
                         "list": [dict(DEVICE, id=page, mac=f"88:F1:55:05:D1:6{page}")]}})
        return httpx.Response(200, json={"code": 0, "msg": "success", "time": T,
                                         "data": {"outdoor": {}}})

    _patch_transport(monkeypatch, handler)
    client = EcowittCloudClient("a", "b")
    devices = await client.list_devices()
    assert [d["mac"] for d in devices] == ["88:F1:55:05:D1:61", "88:F1:55:05:D1:62"]
    from datetime import datetime, timezone
    start = datetime(2026, 1, 20, 7, 27, 34, tzinfo=timezone.utc)
    end = datetime(2026, 1, 21, 7, 27, 34, tzinfo=timezone.utc)
    assert await client.history(MAC, start, end) == {"outdoor": {}}
    h = calls[-1]
    assert h["path"] == "/api/v3/device/history"
    assert h["start_date"] == "2026-01-20 07:27:34"
    assert h["end_date"] == "2026-01-21 07:27:34"
    assert h["cycle_type"] == "5min"
    assert "outdoor" in h["call_back"] and "rainfall_piezo" in h["call_back"]
    assert h["temp_unitid"] == "2"


def test_redact_scrubs_an_ecowitt_url():
    """Belt and braces behind EcowittCloudError: if a raw httpx error ever
    escapes an unwrapped path, source_status.redact must catch both keys
    (`application_key` and `api_key` both match its key-ish pattern)."""
    from app import source_status
    leaked = ("Ecowitt request failed: GET https://api.ecowitt.net/api/v3/"
              "device/real_time?application_key=APP-DEAD-BEEF&api_key="
              "API-DEAD-BEEF&mac=88:F1:55:05:D1:63")
    out = source_status.redact(leaked)
    assert "APP-DEAD-BEEF" not in out and "API-DEAD-BEEF" not in out
    assert "device/real_time" in out and "mac=88:F1:55:05:D1:63" in out


# ───────────────────────────── the poller ────────────────────────────────

class FakeClient:
    def __init__(self, data_by_mac, devices=None, history=None):
        self.data_by_mac = data_by_mac
        self.devices = devices if devices is not None else [DEVICE]
        self.history_data = history or {}
        self.calls: list[str] = []

    async def list_devices(self):
        self.calls.append("list")
        return self.devices

    async def real_time(self, mac):
        self.calls.append(f"real_time {mac}")
        return self.data_by_mac.get(mac, {})

    async def history(self, mac, start, end):
        self.calls.append(f"history {mac}")
        return self.history_data


def _fresh_db(temp_env):
    for mod in ("app.config", "app.db", "app.ingest", "app.ecowitt_cloud_poller"):
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import db
    asyncio.run(db.init_db())
    return db


def test_poller_ingests_once_per_reading_and_skips_repeats(temp_env, monkeypatch):
    """The cloud repeats the last reading until the station reports; the
    poller must post a timestamp once, and the device row must carry the
    list's name, model and coordinates."""
    db = _fresh_db(temp_env)
    from app import ecowitt_cloud_poller as ecp

    posted: list[dict] = []

    async def fake_ingest(payload):
        posted.append(payload)
        return {"ok": True, "inserted": 1}
    monkeypatch.setattr(ecp.ingest, "_do_ingest", fake_ingest)

    fake = FakeClient({MAC: real_time_data()})
    poller = ecp.EcowittCloudPoller(fake, 60, None, "Chandler")

    async def run():
        await poller._discover()
        await poller._tick()
        await poller._tick()                 # same timestamp → skipped
        fake.data_by_mac[MAC]["outdoor"]["temperature"]["time"] = "1768980514"
        await poller._tick()                 # new reading → posted
    asyncio.run(run())
    assert len(posted) == 2
    assert posted[0]["device"]["id"] == MAC
    assert posted[0]["device"]["name"] == "Chandler"      # single device
    assert posted[0]["device"]["model"] == "GW3000B_V1.2.0"
    assert posted[0]["device"]["coords"] == {"lat": 33.3062, "lon": -111.8413}
    assert posted[1]["timestamp_utc"] == "2026-01-21T07:28:34+00:00"
    assert fake.calls.count(f"real_time {MAC}") == 3
    del db


def test_poller_filters_by_configured_macs_and_skips_cameras(temp_env, monkeypatch):
    _fresh_db(temp_env)
    from app import ecowitt_cloud_poller as ecp
    posted: list[str] = []

    async def fake_ingest(payload):
        posted.append(payload["device"]["id"])
        return {"ok": True, "inserted": 1}
    monkeypatch.setattr(ecp.ingest, "_do_ingest", fake_ingest)

    other = "88:F1:55:05:D1:64"
    cam = dict(DEVICE, id=9, mac="AA:AA:AA:AA:AA:AA", type=2, name="Camera")
    fake = FakeClient({MAC: real_time_data(), other: real_time_data()},
                      devices=[DEVICE, dict(DEVICE, id=2, mac=other, name="Shed"), cam])
    # Unrestricted: both stations, never the camera; the configured name
    # does NOT label two devices.
    poller = ecp.EcowittCloudPoller(fake, 60, None, "Chandler")
    asyncio.run(poller._discover())
    assert set(poller._devices) == {MAC, other}
    assert poller._label(MAC) is None
    # Restricted, lowercase + compact: only the named one.
    poller = ecp.EcowittCloudPoller(fake, 60, "88f155 05 d164".replace(" ", ""))
    asyncio.run(poller._discover())
    asyncio.run(poller._tick())
    assert list(poller._devices) == [other]
    assert posted == [other]
    assert poller._devices[other]["name"] == "Shed"


def test_poller_records_failure_only_when_nothing_was_stored(temp_env, monkeypatch):
    _fresh_db(temp_env)
    from app import ecowitt_cloud_poller as ecp, source_status

    class Boom(FakeClient):
        async def real_time(self, mac):
            raise EcowittCloudError("Ecowitt code 40012: unknown device MAC")

    poller = ecp.EcowittCloudPoller(Boom({}), 60, MAC)
    source_status.reset()
    source_status.declare("ecowitt-cloud", True)
    asyncio.run(poller._discover())
    asyncio.run(poller._tick())
    st = next(s for s in source_status.snapshot() if s["name"] == "ecowitt-cloud")
    assert st["consecutive_failures"] == 1
    assert "40012" in st["last_error"]


def test_poller_interval_floor():
    p = EcowittCloudPoller(FakeClient({}), 5)
    assert p._interval_s == 30


def test_bootstrap_backfills_a_day_without_touching_the_ingest_path(temp_env, monkeypatch):
    """The AmbientWeather precedent: history rows go straight to storage —
    never through _do_ingest, where a day of stale readings could fire
    threshold alerts or be re-posted to Weather Underground."""
    db = _fresh_db(temp_env)
    from app import ecowitt_cloud_poller as ecp
    import time as _time
    now = int(_time.time()) // 300 * 300
    t0, t1 = str(now - 600), str(now - 300)
    history = {
        "outdoor": {"temperature": {"unit": "°F", "list": {t0: "60.0", t1: "61.0"}},
                    "humidity": {"unit": "%", "list": {t0: "40", t1: "41"}}},
        "rainfall": {"daily": {"unit": "in", "list": {t0: "0.00", t1: "0.01"}}},
    }

    async def never(payload):
        raise AssertionError("bootstrap must not post through _do_ingest")
    monkeypatch.setattr(ecp.ingest, "_do_ingest", never)

    fake = FakeClient({}, history=history)
    poller = ecp.EcowittCloudPoller(fake, 60, None, "Chandler")

    async def run():
        await poller._discover()
        await poller.bootstrap()
        await poller.bootstrap()             # idempotent on (mac, dateutc)
        return await db.history(MAC, (now - 3600) * 1000, (now + 60) * 1000)
    rows = asyncio.run(run())
    assert fake.calls.count(f"history {MAC}") == 2
    temps = sorted(r["tempf"] for r in rows if r.get("tempf") is not None)
    assert temps == [60.0, 61.0]
    assert asyncio.run(db.get_device_name(MAC)) == "Chandler"



def test_a_group_lagging_the_reading_is_left_out_not_restamped():
    from app.ecowitt_cloud_poller import STALE_GROUP_S, stale_groups
    d = real_time_data()
    live = 1768980600
    d["wind"]["wind_speed"]["time"] = str(live)
    d["wind"]["wind_gust"]["time"] = str(live)
    for k in d["outdoor"]:
        d["outdoor"][k]["time"] = str(live - STALE_GROUP_S - 1)
    assert stale_groups(d, live) == {"outdoor"}
    p = build_payload(MAC, d)
    assert p["timestamp_utc"] == "2026-01-21T07:30:00+00:00"
    assert "outdoor" not in p or "tempf" not in p["outdoor"]
    assert p["wind"]["speed_mph"] is not None


def test_poller_stores_a_live_wind_update_behind_a_stale_temperature(temp_env, monkeypatch):
    """Both polls carry the same outdoor time; the second has newer wind.
    It used to be skipped as 'already ingested'."""
    db = _fresh_db(temp_env)
    from app import ecowitt_cloud_poller as ecp
    posted: list[dict] = []

    async def fake_ingest(payload):
        posted.append(payload)
        return {"ok": True, "inserted": 1}
    monkeypatch.setattr(ecp.ingest, "_do_ingest", fake_ingest)
    data = real_time_data()
    fake = FakeClient({MAC: data})
    poller = ecp.EcowittCloudPoller(fake, 60, None, "Chandler")

    async def run():
        await poller._discover()
        await poller._tick()
        data["wind"]["wind_speed"] = leaf("5.4", "m/s", time="1768980514")
        data["wind"]["wind_gust"]["time"] = "1768980514"
        await poller._tick()
        await poller._tick()                 # nothing new → skipped
    asyncio.run(run())
    assert len(posted) == 2
    assert posted[1]["timestamp_utc"] == "2026-01-21T07:28:34+00:00"
    assert posted[1]["wind"]["speed_mph"] == pytest.approx(12.08, abs=0.01)
    assert posted[1]["outdoor"]["tempf"] == posted[0]["outdoor"]["tempf"]
    del db


def test_bootstrap_asks_for_the_window_in_the_devices_own_clock(temp_env, monkeypatch):
    """R18 finding 3: the history API reads the offset-free strings as the
    device's zone, so a Phoenix station's window goes over as Phoenix wall
    clock, seven hours behind UTC; an unknown zone falls back to UTC."""
    db = _fresh_db(temp_env)
    from app import ecowitt_cloud_poller as ecp
    from datetime import datetime, timezone
    seen: list[tuple[str, str]] = []

    class Recording(FakeClient):
        async def history(self, mac, start, end):
            seen.append((start.strftime("%Y-%m-%d %H:%M:%S"), start.utcoffset(),
                         end.strftime("%Y-%m-%d %H:%M:%S")))
            return {}
    fixed = datetime(2026, 9, 3, 5, 0, 0, tzinfo=timezone.utc)   # 22:00 Phoenix, the day before

    class FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(ecp, "datetime", FrozenDT)
    phoenix = dict(DEVICE, date_zone_id="America/Phoenix")
    odd = dict(DEVICE, mac="88:F1:55:05:D1:99", date_zone_id="Mars/Olympus")
    fake = Recording({MAC: real_time_data()}, devices=[phoenix, odd])
    poller = ecp.EcowittCloudPoller(fake, 60, None, "Chandler")

    async def run():
        await poller._discover()
        await poller.bootstrap()
    asyncio.run(run())
    assert seen[0][0] == "2026-09-01 22:00:00" and seen[0][2] == "2026-09-02 22:00:00"
    assert seen[1][0] == "2026-09-02 05:00:00" and seen[1][2] == "2026-09-03 05:00:00"
    del db
