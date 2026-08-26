"""Pillar B fan-out: the pure protocol builders (CWOP packet formatting
above all — fixed-width APRS is exactly the kind of code that fails
silently), the runner's gating, and the credentials-never-echo API."""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import time

# Set BEFORE importing app: config.py reads the environment at import time
# (the test_nowcast dance).
os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import share_targets as st  # noqa: E402

AUTH = {"Authorization": "Bearer test-api-token"}

OBS = {"tempf": 104.2, "humidity": 24.0, "dewPoint": 61.5,
       "winddir": 225.0, "windspeedmph": 6.0, "windgustmph": 14.0,
       "baromrelin": 29.88, "hourlyrainin": 0.05, "dailyrainin": 0.42,
       "solarradiation": 890.0, "uv": 9.0}
NOW = _dt.datetime(2026, 8, 25, 21, 30, 0, tzinfo=_dt.timezone.utc)


def test_pwsweather_params_imperial_passthrough():
    p = st.pwsweather_params({"station_id": "KAZCHAND1", "api_key": "sek"},
                             OBS, NOW)
    assert p["ID"] == "KAZCHAND1" and p["PASSWORD"] == "sek"
    assert p["dateutc"] == "2026-08-25 21:30:00"
    assert p["tempf"] == 104.2 and p["baromin"] == 29.88
    assert p["dewptf"] == 61.5 and p["UV"] == 9.0
    assert p["action"] == "updateraw"
    # Absent readings are omitted, never zeroed.
    p2 = st.pwsweather_params({}, {"tempf": 90.0}, NOW)
    assert "windspeedmph" not in p2 and "rainin" not in p2


def test_weathercloud_metric_times_ten():
    p = st.weathercloud_params({"wid": "w1", "key": "k1"}, OBS)
    assert p["temp"] == 401          # 104.2°F = 40.1°C ×10
    assert p["hum"] == 24
    assert p["bar"] == 10119         # 29.88 inHg = 1011.85 hPa ×10
    assert p["wspd"] == 27           # 6 mph = 2.68 m/s ×10
    assert p["rain"] == 107          # 0.42 in = 10.7 mm ×10
    assert p["uvi"] == 90


def test_cwop_packet_fixed_width():
    pkt = st.cwop_packet("CW1234", 33.3004, -111.9378, OBS, NOW)
    head, _, wx = pkt.partition("_")
    assert head.startswith("CW1234>APRS,TCPIP*:@252130z")
    assert "3318.02N/11156.27W" in head
    # dir/speed/gust fixed 3-digit, then temp/rain/humidity/pressure:
    assert wx.startswith("225/006g014t104")
    assert "r005" in wx              # 0.05 in → hundredths
    assert "P042" in wx              # since midnight
    assert "h24" in wx
    assert "b10119" in wx            # 1011.85 hPa in tenths of mb
    assert wx.endswith("ZasderWeather")


def test_cwop_packet_missing_and_edge_values():
    obs = {"tempf": -5.0, "humidity": 100.0}
    pkt = st.cwop_packet("CW1234", -33.5, 151.25, obs, NOW)
    assert "S/" in pkt and "E_" in pkt          # southern/eastern encoding
    assert "_.../...g..." in pkt                 # missing wind = dots
    assert "t-05" in pkt                         # negative temp form
    assert "h00" in pkt                          # 100% encodes as 00
    # No rain data → no rNNN group anywhere in the weather block (the
    # trailing "ZasderWeather" tag is why a bare "r" check can't work).
    import re as _re
    assert _re.search(r"r\d{3}", pkt.partition("_")[2]) is None
    # No rain fields at all when the gauge reported nothing:
    assert "P" not in pkt.split("g...")[1].replace("ZasderWeather", "")


def test_runner_gates_on_enabled_and_cadence(client, monkeypatch):
    calls = []

    async def fake_pws(cfg, obs, now_ms):
        calls.append(now_ms)
        return None

    monkeypatch.setattr(st, "_send_pwsweather", fake_pws)
    devices = [{"mac": "AA", "lastData": OBS,
                "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]
    now = int(time.time() * 1000)

    # Not configured → nothing.
    asyncio.run(st.check(devices, now))
    assert calls == []

    asyncio.run(st.set_config("pwsweather",
                              {"enabled": True, "station_id": "X",
                               "api_key": "Y"}))
    st._reset_for_tests()
    asyncio.run(st.check(devices, now))
    assert len(calls) == 1
    # Inside the 5-minute window → gated.
    asyncio.run(st.check(devices, now + 60_000))
    assert len(calls) == 1
    # Past it → sends again, and success stamped.
    asyncio.run(st.check(devices, now + 6 * 60_000))
    assert len(calls) == 2
    status = asyncio.run(st.get_status("pwsweather"))
    assert status["last_ok_ms"] == now + 6 * 60_000
    assert status["last_error"] is None


def test_sharing_api_never_echoes_credentials(client):
    r = client.put("/api/sharing/pwsweather", headers=AUTH,
                   json={"station_id": "KAZX", "api_key": "supersecret"})
    assert r.status_code == 200
    g = client.get("/api/sharing", headers=AUTH).json()
    assert g["pwsweather"]["fields"] == {"station_id": True, "api_key": True}
    assert "supersecret" not in str(g)
    # Enabling without credentials is refused loudly.
    r = client.put("/api/sharing/windy", headers=AUTH,
                   json={"enabled": True})
    assert r.status_code == 400
    r = client.put("/api/sharing/nope", headers=AUTH, json={"enabled": True})
    assert r.status_code == 404
    # With credentials, enable works and status reflects it.
    r = client.put("/api/sharing/windy", headers=AUTH,
                   json={"api_key": "wk", "enabled": True})
    assert r.status_code == 200
    g = client.get("/api/sharing", headers=AUTH).json()
    assert g["windy"]["enabled"] is True
