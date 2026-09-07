"""Pillar B fan-out: the pure protocol builders (CWOP packet formatting
above all — fixed-width APRS is exactly the kind of code that fails
silently), the runner's gating, and the credentials-never-echo API."""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import time

import pytest

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
    # `rainin` is the trailing hour's ACCUMULATION the runner attaches,
    # never the reading's `hourlyrainin` rate (round-three review BE-F6).
    assert "rainin" not in p
    assert st.pwsweather_params({}, dict(OBS, rain_last_hour_in=0.12), NOW)["rainin"] == 0.12
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
    pkt = st.cwop_packet("CW1234", 33.3004, -111.9378,
                         dict(OBS, rain_last_hour_in=0.05), NOW)
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


def test_interval_is_configurable_down_to_each_networks_floor(client, monkeypatch):
    """Doren, 2026-09-06: "is it possible to send more often?" PWSWeather
    may go to a minute; Windy's floor is 5, WeatherCloud's 10, CWOP's 5.
    The PUT clamps, the GET says what is in force and what the floor is,
    and the runner honours the shorter cadence."""
    r = client.put("/api/sharing/pwsweather", headers=AUTH,
                   json={"station_id": "X", "api_key": "Y", "enabled": True,
                         "interval_min": 1})
    assert r.status_code == 200
    r = client.put("/api/sharing/weathercloud", headers=AUTH,
                   json={"wid": "W", "key": "K", "interval_min": 1})
    assert r.status_code == 200
    g = client.get("/api/sharing", headers=AUTH).json()
    assert g["pwsweather"]["interval_min"] == 1
    assert g["pwsweather"]["min_interval_min"] == 1
    assert g["weathercloud"]["interval_min"] == 10, "clamped to the free-plan floor"
    assert g["cwop"]["interval_min"] == 10 and g["cwop"]["min_interval_min"] == 5
    assert client.put("/api/sharing/pwsweather", headers=AUTH,
                      json={"interval_min": 0}).status_code == 422
    assert "wu" in g and g["wu"]["enabled"] is False

    calls = []

    async def fake_pws(cfg, obs, now_ms):
        calls.append(now_ms)
        return None
    monkeypatch.setattr(st, "_send_pwsweather", fake_pws)
    devices = [{"mac": "AA", "lastData": dict(OBS),
                "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]
    now = int(time.time() * 1000)
    devices[0]["lastData"]["dateutc"] = now
    st._reset_for_tests()
    asyncio.run(st.check(devices, now))
    asyncio.run(st.check(devices, now + 30_000))       # inside the minute
    asyncio.run(st.check(devices, now + 61_000))       # past it
    assert len(calls) == 2, calls


def test_windy_error_carries_windys_message_without_the_key():
    assert st.windy_error(401, '{"message":"Token is invalid","statusCode":401}') \
        == "HTTP 401 — Token is invalid"
    assert st.windy_error(400, "<html>nope</html>") == "HTTP 400"
    assert st.windy_error(400, '{"message":"bad key abc123"}', key="abc123") \
        == "HTTP 400 — bad key ***"
    assert len(st.windy_error(400, '{"message":"' + "x" * 500 + '"}')) < 140


def _post_station(client, mac_id, tempf=70.0, air=False):
    body = {"device": {"id": mac_id, "name": mac_id},
            "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "test"}
    if air:
        body["air"] = {"co2": 700, "pm25": 3.0}
        body["indoor"] = {"tempf": tempf}
    else:
        body["outdoor"] = {"tempf": tempf, "humidity": 30}
    r = client.post("/ingest/custom", headers={"Authorization": "Bearer test-ingest-token"},
                    json=body)
    assert r.status_code == 200, r.text
    return r.json()["mac"]


def test_each_network_publishes_its_chosen_station(client, monkeypatch):
    """Volney, 2026-09-06: with several stations, pick which one goes out;
    unset means the first weather station, as before. An explicit choice
    never substitutes (round-three review BE-F8): a chosen station with
    no reading publishes nothing, and says so."""
    from app import share_targets as st_mod
    _post_station(client, "AAAAAAAAAAAA"); _post_station(client, "BBBBBBBBBBBB")
    r = client.put("/api/sharing/pwsweather", headers=AUTH,
                   json={"station_id": "X", "api_key": "Y", "enabled": True,
                         "mac": "bb:bb:bb:bb:bb:bb"})
    assert r.status_code == 200, r.text
    g = client.get("/api/sharing", headers=AUTH).json()
    assert g["pwsweather"]["mac"] == "BB:BB:BB:BB:BB:BB"
    sent = []

    async def fake_pws(cfg, obs, now_ms):
        sent.append(obs["tempf"])
        return None
    monkeypatch.setattr(st_mod, "_send_pwsweather", fake_pws)
    now = int(time.time() * 1000)
    devices = [{"mac": "AA:AA:AA:AA:AA:AA", "lastData": dict(OBS, tempf=70.0, dateutc=now)},
               {"mac": "BB:BB:BB:BB:BB:BB", "lastData": dict(OBS, tempf=80.0, dateutc=now)}]
    st_mod._reset_for_tests()
    asyncio.run(st_mod.check(devices, now))
    assert sent == [80.0]
    # Clearing the choice returns to the first weather station.
    assert client.put("/api/sharing/pwsweather", headers=AUTH,
                      json={"mac": ""}).status_code == 200
    assert client.get("/api/sharing", headers=AUTH).json()["pwsweather"]["mac"] is None
    st_mod._reset_for_tests()
    asyncio.run(st_mod.check(devices, now + 6 * 60_000))
    assert sent == [80.0, 70.0]
    # A chosen station that is absent, silent or a monitor publishes NOTHING
    # under this network's id (BE-F8), and the runner says why.
    cfg = asyncio.run(st_mod.get_config("pwsweather"))
    assert st_mod.station_for(dict(cfg, mac="CC:CC:CC:CC:CC:CC"), devices) is None
    silent = [{"mac": "AA:AA:AA:AA:AA:AA", "lastData": dict(OBS, dateutc=now)},
              {"mac": "BB:BB:BB:BB:BB:BB", "lastData": None}]
    assert st_mod.station_for(dict(cfg, mac="BB:BB:BB:BB:BB:BB"), silent) is None
    asyncio.run(st_mod.set_config("pwsweather", dict(cfg, mac="BB:BB:BB:BB:BB:BB")))
    st_mod._reset_for_tests()
    asyncio.run(st_mod.check(silent, now + 12 * 60_000))
    assert sent == [80.0, 70.0], "station A must not go out under B's id"
    status = asyncio.run(st_mod.get_status("pwsweather"))
    assert "no reading" in status["last_error"]
    # The PUT refuses what cannot be a station: an unknown mac, a monitor.
    r = client.put("/api/sharing/pwsweather", headers=AUTH, json={"mac": "cc:cc:cc:cc:cc:cc"})
    assert r.status_code == 400 and "not a station" in r.json()["detail"]
    monitor = _post_station(client, "5D5D0882DD6E", air=True)
    r = client.put("/api/sharing/pwsweather", headers=AUTH, json={"mac": monitor})
    assert r.status_code == 400 and "air monitor" in r.json()["detail"]


def test_a_longer_cadence_does_not_widen_the_dead_station_guard():
    """Round-three review BE-F9: an hourly CWOP cadence tolerated a two-hour
    old reading. The guard is twice the DEFAULT cadence, whatever the
    operator chose."""
    now = int(time.time() * 1000)
    obs = {"dateutc": now - 45 * 60_000}
    assert st.reading_too_old(obs, now, "cwop", {"interval_min": 60}) == 45
    assert st.reading_too_old(obs, now, "cwop", None) == 45
    assert st.reading_too_old({"dateutc": now - 15 * 60_000}, now, "cwop",
                              {"interval_min": 60}) is None


def test_the_runner_hands_the_senders_the_hours_accumulation(client, monkeypatch):
    """Round-three review BE-F6: the senders read `rain_last_hour_in`,
    which the runner derives from the counters; the reading's
    `hourlyrainin` rate is never on the wire."""
    from app import db
    from app import share_targets as st_mod
    mac = "AA:AA:AA:AA:AA:AA"
    now = int(time.time() * 1000)
    asyncio.run(db.insert_observations(mac, [
        {"dateutc": now - 2 * 3_600_000, "yearlyrainin": 5.00},
        {"dateutc": now - 60_000, "yearlyrainin": 5.12, "hourlyrainin": 40.0, "tempf": 70.0},
    ]))
    seen = []

    async def fake_pws(cfg, obs, now_ms):
        seen.append(obs)
        return None
    monkeypatch.setattr(st_mod, "_send_pwsweather", fake_pws)
    asyncio.run(st_mod.set_config("pwsweather", {"enabled": True, "station_id": "X",
                                                 "api_key": "Y", "mac": mac}))
    devices = [{"mac": mac, "lastData": {"tempf": 70.0, "hourlyrainin": 40.0,
                                          "dateutc": now - 60_000}}]
    st_mod._reset_for_tests()
    asyncio.run(st_mod.check(devices, now))
    assert len(seen) == 1
    assert seen[0][st_mod.RAIN_LAST_HOUR] == pytest.approx(0.12)
    params = st_mod.pwsweather_params({"station_id": "X", "api_key": "Y"}, seen[0], NOW)
    assert params["rainin"] == pytest.approx(0.12)
    packet = st_mod.cwop_packet("CW1", 33.3, -111.9, seen[0], NOW)
    assert "r012" in packet and "r400" not in packet


def test_verify_refuses_a_stale_reading_and_is_throttled(client, monkeypatch):
    """Round-three review SEC-G6: Save and verify passed no staleness gate
    (a station dead three days was "verified" onto four networks) and had
    no throttle."""
    from app import main
    from app import share_targets as st_mod
    mac = _post_station(client, "AAAAAAAAAAAA")
    monkeypatch.setattr(main, "SHARE_TEST_EVERY_S", 0.0)
    r = client.put("/api/sharing/pwsweather", headers=AUTH,
                   json={"station_id": "X", "api_key": "Y", "enabled": True, "mac": mac})
    assert r.status_code == 200, r.text
    sent = []

    async def fake_pws(cfg, obs, now_ms):
        sent.append(now_ms)
        return None
    monkeypatch.setattr(st_mod, "_send_pwsweather", fake_pws)
    # The station's newest reading is three days old.
    from app import db
    devs = asyncio.run(db.list_devices())
    old = int(time.time() * 1000) - 3 * 86_400_000

    async def stale_devices():
        out = []
        for d in devs:
            d = dict(d)
            if d["mac"] == mac:
                d["lastData"] = dict(d["lastData"], dateutc=old)
            out.append(d)
        return out
    monkeypatch.setattr(db, "list_devices", stale_devices)
    r = client.post("/api/sharing/pwsweather/test", headers=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is False, r.text
    assert "min old" in r.json()["error"] and sent == []
    # The throttle: one verify per SHARE_TEST_EVERY_S per network.
    monkeypatch.setattr(main, "SHARE_TEST_EVERY_S", 30.0)
    main._SHARE_TEST_LAST.clear()
    client.post("/api/sharing/pwsweather/test", headers=AUTH)
    r = client.post("/api/sharing/pwsweather/test", headers=AUTH)
    assert r.status_code == 429
    # A disabled target is refused before anything is sent (§5 row 19).
    client.put("/api/sharing/windy", headers=AUTH, json={"station_id": "s", "password": "p"})
    assert client.post("/api/sharing/windy/test", headers=AUTH).status_code == 400


def test_the_wu_summary_row_and_the_interval_clear_sentinel(client, monkeypatch):
    """§5 row 19: the Weather Underground summary in GET /api/sharing; and
    BE-F12: -1 clears interval_min like it clears station."""
    from app import wu_upload
    mac = _post_station(client, "AAAAAAAAAAAA")
    assert client.get("/api/sharing", headers=AUTH).json()["wu"]["enabled"] is False
    r = client.put(f"/api/devices/{mac}/wu-station", headers=AUTH,
                   json={"wu_station_id": "KAZX1", "upload_key": "k", "upload_enabled": True})
    assert r.status_code == 200, r.text
    wu_upload._record_success(mac)
    wu = client.get("/api/sharing", headers=AUTH).json()["wu"]
    assert wu["enabled"] is True and wu["last_ok_ms"] is not None and wu["last_error"] is None
    assert client.put("/api/sharing/pwsweather", headers=AUTH,
                      json={"interval_min": 2}).status_code == 200
    assert client.get("/api/sharing", headers=AUTH).json()["pwsweather"]["interval_min"] == 2
    assert client.put("/api/sharing/pwsweather", headers=AUTH,
                      json={"interval_min": -1}).status_code == 200
    assert client.get("/api/sharing", headers=AUTH).json()["pwsweather"]["interval_min"] == 5
    assert client.put("/api/sharing/pwsweather", headers=AUTH,
                      json={"interval_min": 0}).status_code == 422


@pytest.mark.skipif(not os.environ.get("WINDY_STATION_PASSWORD"),
                    reason="live Windy read-back; set WINDY_STATION_ID and WINDY_STATION_PASSWORD")
def test_windy_accepts_humidity_and_dewptf_live():
    """§5 row 13, live: one send with the names we use, then the read-back
    must carry rh and dew_point. Run by hand; verified 2026-09-06."""
    import httpx
    sid, pw = os.environ["WINDY_STATION_ID"], os.environ["WINDY_STATION_PASSWORD"]
    now = int(time.time() * 1000)
    obs = {"tempf": 80.0, "humidity": 33.0, "dewPoint": 48.0, "dateutc": now,
           "rain_last_hour_in": 0.0}
    err = asyncio.run(st._send_windy({"station_id": sid, "password": pw}, obs, now))
    assert err is None, err
    r = httpx.get(st.WINDY_V2_READ, params={"PASSWORD": pw, "latestLimit": 1}, timeout=15)
    data = r.json()["data"]
    assert data["rh"][0] == 33 and abs(data["dew_point"][0] - 282.04) < 0.6


class _FakeHTTP:
    """Captures the one GET a sender makes and answers with a scripted
    status and body."""
    calls: list[tuple[str, dict]] = []
    status = 200
    body = ""

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, params=None, **k):
        _FakeHTTP.calls.append((url, dict(params or {})))
        class R:
            status_code = _FakeHTTP.status
            text = _FakeHTTP.body
        return R()


def test_windy_uses_the_2026_api_when_it_has_a_station_password(monkeypatch):
    """Verified live 2026-09-06: the legacy /pws/update/<key> answers 401
    for keys made after the January 2026 cut-over; the v2 endpoint takes
    the station id and its password and the WU parameter names."""
    monkeypatch.setattr(st.httpx, "AsyncClient", _FakeHTTP)
    _FakeHTTP.calls.clear(); _FakeHTTP.status = 200
    now = int(time.time() * 1000)
    obs = dict(OBS, dateutc=now, solarradiation=640.0)
    err = asyncio.run(st._send_windy({"station_id": "4kGZxkmh", "password": "pw"}, obs, now))
    assert err is None
    url, params = _FakeHTTP.calls[-1]
    assert url == st.WINDY_V2_UPDATE
    assert params["id"] == "4kGZxkmh" and params["PASSWORD"] == "pw"
    assert {"tempf", "humidity", "dewptf", "baromin", "solarradiation", "dateutc"} <= set(params)
    assert "rh" not in params and "dewpointf" not in params and "station" not in params
    # The legacy account key still rides the legacy path.
    _FakeHTTP.calls.clear()
    asyncio.run(st._send_windy({"api_key": "legacy"}, obs, now))
    url, params = _FakeHTTP.calls[-1]
    assert url == st.WINDY_LEGACY_UPDATE + "legacy" and params["station"] == 0
    # A duplicate (409) is a report Windy already holds, not a failure;
    # a 400 carries Windy's message with the secret scrubbed.
    _FakeHTTP.status = 409
    assert asyncio.run(st._send_windy({"station_id": "s", "password": "pw"}, obs, now)) is None
    _FakeHTTP.status, _FakeHTTP.body = 400, '{"message":"Provided password is invalid"}'
    assert asyncio.run(st._send_windy({"station_id": "s", "password": "pw"}, obs, now)) \
        == "HTTP 400 — Provided password is invalid"


def test_save_and_verify_sends_once_and_shows_the_values(client, monkeypatch):
    """The Save and verify button: one send now, the network's answer back,
    the status row stamped. The GET shows non-secret values so the sheet
    reads as filled in; secrets stay booleans."""
    from app import main
    monkeypatch.setattr(main, "SHARE_TEST_EVERY_S", 0.0)
    r = client.put("/api/sharing/windy", headers=AUTH,
                   json={"station_id": "4kGZxkmh", "password": "pw", "enabled": True})
    assert r.status_code == 200, r.text
    g = client.get("/api/sharing", headers=AUTH).json()["windy"]
    assert g["values"] == {"station_id": "4kGZxkmh"}
    assert g["fields"]["password"] is True and "pw" not in str(g)
    # No station with a reading yet: an honest answer, no exception.
    r = client.post("/api/sharing/windy/test", headers=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "no reading" in r.json()["error"]
    # A station reports; the send goes out and the row is stamped.
    client.post("/ingest/custom", headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF", "name": "Yard"},
                      "timestamp_utc": _dt.datetime.now(_dt.timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "outdoor": {"tempf": 80.0, "humidity": 30}, "source": "test"})
    sent = []

    async def fake_windy(cfg, obs, now_ms):
        sent.append(obs["tempf"])
        return None
    monkeypatch.setattr(st, "_send_windy", fake_windy)
    r = client.post("/api/sharing/windy/test", headers=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert sent == [80.0] and r.json()["station"] == "AA:BB:CC:DD:EE:FF"
    assert client.get("/api/sharing", headers=AUTH).json()["windy"]["last_ok_ms"] is not None
    assert client.post("/api/sharing/nope/test", headers=AUTH).status_code == 404
