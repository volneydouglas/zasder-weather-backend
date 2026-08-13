"""WU live upload (1.5) — field mapping, throttle, config gating, secrecy."""
import asyncio
import datetime as dt
import time

import httpx
import pytest

# Set lazily by the autouse fixture below: app modules read env at import
# time, so the import must happen AFTER the client fixture has set the test
# env (the test_wu_import.py convention).
wu_upload = None

_H = {"Authorization": "Bearer test-api-token"}
_REVIEWER = {"Authorization": "Bearer test-reviewer-token"}
_ING = {"Authorization": "Bearer test-ingest-token"}

MAC = "AA:BB:CC:DD:EE:FF"
KEY = "sekretStationKey1"
STATION = "KAZCHAND802"


@pytest.fixture(autouse=True)
def _reset_state(client):
    """Import the module under test AFTER `client` sets the env (conftest
    reloads app.wu_upload per test, so the dicts start empty anyway — this
    keeps single-file runs honest too)."""
    global wu_upload
    from app import wu_upload as _m
    wu_upload = _m
    wu_upload._last_attempt.clear()
    wu_upload._stats.clear()
    yield
    wu_upload._last_attempt.clear()
    wu_upload._stats.clear()


def _drain_tasks():
    """Wait out any in-flight upload tasks on the TestClient's portal loop —
    every ingest schedules one, and a straggler racing a later monkeypatched
    _send (or the throttle map) would make assertions order-dependent."""
    deadline = time.time() + 5
    while wu_upload._TASKS and time.time() < deadline:
        time.sleep(0.01)


def _seed_device(client, ts: dt.datetime | None = None):
    ts = ts or dt.datetime.now(dt.timezone.utc)
    r = client.post("/ingest/custom", headers=_ING,
                    json={"device": {"id": "AABBCCDDEEFF", "name": "Atlas"},
                          "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "outdoor": {"tempf": 91.5, "humidity": 30,
                                      "dew_point_f": 55.2, "uv": 7,
                                      "solar_wm2": 812.0},
                          "indoor": {"tempf": 78.1, "humidity": 41},
                          "wind": {"speed_mph": 6.0, "gust_mph": 11.0,
                                   "direction": 224},
                          "rain": {"hourly_in": 0.04, "daily_in": 0.12},
                          "pressure": {"relative_inhg": 29.91},
                          "source": "test"})
    assert r.status_code == 200
    _drain_tasks()
    return r


def _configure(client, station=STATION, key=KEY, enabled=True):
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
                   json={"wu_station_id": station, "upload_key": key,
                         "upload_enabled": enabled})
    assert r.status_code == 200, r.text
    return r.json()


def _capture_send(monkeypatch, calls, status=200, body="success"):
    async def fake_send(params):
        calls.append(dict(params))
        return status, body
    monkeypatch.setattr(wu_upload, "_send", fake_send)


def _flat(ts_ms: int) -> dict:
    return {"dateutc": ts_ms, "tempf": 91.5, "humidity": 30.0,
            "dewPoint": 55.2, "baromrelin": 29.91, "windspeedmph": 6.0,
            "windgustmph": 11.0, "winddir": 224.0, "hourlyrainin": 0.04,
            "dailyrainin": 0.12, "solarradiation": 812.0, "uv": 7.0,
            "tempinf": 78.1, "humidityin": 41.0}


# ───────────────────────── fires on ingest ─────────────────────────

def test_upload_fires_on_ingest_with_correct_field_mapping(client, monkeypatch):
    # First post only registers the device; its own upload task gates out
    # (nothing configured yet) and _seed_device drains it.
    _seed_device(client, dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(seconds=120))
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    # Recent enough to pass the ingest timestamp bounds, second-precision so
    # the dateutc assertion is exact.
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    _seed_device(client, ts)

    # The upload is a fire-and-forget task on the TestClient's portal loop —
    # poll briefly instead of racing it.
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    assert calls, "upload task never fired"
    p = calls[0]
    assert p["ID"] == STATION
    assert p["PASSWORD"] == KEY
    assert p["action"] == "updateraw"
    assert p["softwaretype"] == "ZasderWeather"
    assert p["dateutc"] == ts.strftime("%Y-%m-%d %H:%M:%S")
    # Field mapping — WU param <- flat ingest field, units already WU-native.
    assert p["tempf"] == 91.5
    assert p["humidity"] == 30.0
    assert p["dewptf"] == 55.2                 # <- dewPoint
    assert p["baromin"] == 29.91               # <- baromrelin
    assert p["windspeedmph"] == 6.0
    assert p["windgustmph"] == 11.0
    assert p["winddir"] == 224.0
    assert p["rainin"] == 0.04                 # <- hourlyrainin (trailing 1h)
    assert p["dailyrainin"] == 0.12
    assert p["solarradiation"] == 812.0
    assert p["UV"] == 7.0                      # <- uv
    assert p["indoortempf"] == 78.1            # <- tempinf
    assert p["indoorhumidity"] == 41.0         # <- humidityin
    # Not part of WU's upload vocabulary — must not leak extra params.
    assert "feelsLike" not in p and "baromabsin" not in p

    st = wu_upload.stats(MAC)
    assert st["last_ok_ms"] is not None
    assert st["failures"] == 0 and st["last_error"] is None


def test_upload_omits_absent_fields(client, monkeypatch):
    """A station without a solar/indoor sensor must not upload zeros (the
    wu_import missing-readings principle, outbound)."""
    _seed_device(client)
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)
    flat = {"dateutc": int(time.time() * 1000), "tempf": 70.0,
            "humidity": None, "solarradiation": None}
    ok = asyncio.run(wu_upload.maybe_upload(MAC, flat))
    assert ok is True
    p = calls[0]
    assert p["tempf"] == 70.0
    for absent in ("humidity", "solarradiation", "UV", "indoortempf",
                   "indoorhumidity", "rainin", "dailyrainin"):
        assert absent not in p


# ───────────────────────── throttle ─────────────────────────

def test_throttle_suppresses_second_upload_within_60s(client, monkeypatch):
    _seed_device(client)
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)
    now_ms = int(time.time() * 1000)

    async def scenario():
        first = await wu_upload.maybe_upload(MAC, _flat(now_ms))
        second = await wu_upload.maybe_upload(MAC, _flat(now_ms + 30_000))
        return first, second
    first, second = asyncio.run(scenario())
    assert first is True and second is False
    assert len(calls) == 1

    # Outside the window the next reading uploads again.
    wu_upload._last_attempt[MAC] -= wu_upload.UPLOAD_MIN_INTERVAL_S + 1
    assert asyncio.run(wu_upload.maybe_upload(MAC, _flat(now_ms + 61_000))) is True
    assert len(calls) == 2


def test_throttle_counts_attempts_not_successes(client, monkeypatch):
    """A failing upload must not retry faster than a succeeding one — the
    throttle is WU protection, not a retry loop."""
    _seed_device(client)
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls, status=500, body="oops")
    now_ms = int(time.time() * 1000)

    async def scenario():
        a = await wu_upload.maybe_upload(MAC, _flat(now_ms))
        b = await wu_upload.maybe_upload(MAC, _flat(now_ms + 1000))
        return a, b
    a, b = asyncio.run(scenario())
    assert a is False and b is False
    assert len(calls) == 1                      # second attempt throttled
    assert wu_upload.stats(MAC)["failures"] == 1


# ───────────────────────── gating ─────────────────────────

def test_no_upload_when_disabled_or_unconfigured(client, monkeypatch):
    _seed_device(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)
    flat = _flat(int(time.time() * 1000))

    # No association at all.
    assert asyncio.run(wu_upload.maybe_upload(MAC, flat)) is False
    # Station but no key, not enabled.
    client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
               json={"wu_station_id": STATION})
    assert asyncio.run(wu_upload.maybe_upload(MAC, flat)) is False
    # Key present but forwarding switched off.
    _configure(client, enabled=False)
    assert asyncio.run(wu_upload.maybe_upload(MAC, flat)) is False
    # Enabled but the key was cleared ("" = clear, the wu-key convention).
    _configure(client, enabled=True)
    client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
               json={"upload_key": ""})
    assert asyncio.run(wu_upload.maybe_upload(MAC, flat)) is False

    assert calls == []
    # Gated readings never count as attempts either.
    assert wu_upload._last_attempt == {}


# ───────────────────────── secrecy ─────────────────────────

def test_key_never_logged_or_stored_on_failure(client, monkeypatch, caplog):
    """httpx exception reprs embed the keyed URL — the wu_import.py trap.
    Failures must record/log the exception TYPE or status only."""
    _seed_device(client)
    _configure(client)

    async def boom(params):
        raise httpx.ConnectError(
            "boom " + wu_upload.WU_UPLOAD_URL + "?ID=X&PASSWORD=" + KEY)
    monkeypatch.setattr(wu_upload, "_send", boom)
    with caplog.at_level("DEBUG"):
        ok = asyncio.run(wu_upload.maybe_upload(MAC, _flat(int(time.time() * 1000))))
    assert ok is False
    assert KEY not in caplog.text
    assert "updateweatherstation" not in caplog.text
    st = wu_upload.stats(MAC)
    assert st["failures"] == 1
    assert st["last_error"] == "ConnectError"
    assert KEY not in str(st)

    # Non-200: status only, no body echo.
    calls: list[dict] = []
    _capture_send(monkeypatch, calls, status=401,
                  body="unauthorized for " + KEY)
    wu_upload._last_attempt.clear()
    with caplog.at_level("DEBUG"):
        asyncio.run(wu_upload.maybe_upload(MAC, _flat(int(time.time() * 1000))))
    st = wu_upload.stats(MAC)
    assert st["last_error"] == "HTTP 401"
    assert st["failures"] == 2
    assert KEY not in caplog.text

    # A 200 that isn't WU's "success" ack is a failure too (INVALIDPASSWORDID
    # arrives as 200 text) — and the body must not be stored.
    _capture_send(monkeypatch, calls, status=200,
                  body="INVALIDPASSWORDID|Password and/or id are incorrect")
    wu_upload._last_attempt.clear()
    asyncio.run(wu_upload.maybe_upload(MAC, _flat(int(time.time() * 1000))))
    st = wu_upload.stats(MAC)
    assert st["failures"] == 3
    assert "INVALIDPASSWORDID" not in st["last_error"]


def test_get_never_returns_key_and_put_clears_it(client):
    _seed_device(client)
    resp = _configure(client)
    # PUT's own echo: state, never the key.
    assert resp["upload_key_set"] is True and resp["upload_enabled"] is True
    assert KEY not in str(resp)

    r = client.get(f"/api/devices/{MAC}/wu-station", headers=_H)
    j = r.json()
    assert j["wu_station_id"] == STATION
    assert j["upload_enabled"] is True
    assert j["upload_key_set"] is True
    assert "upload_key" not in j and KEY not in r.text

    # "" clears the key (write-only convention) without touching the rest.
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
                   json={"upload_key": ""})
    j = r.json()
    assert j["upload_key_set"] is False
    assert j["wu_station_id"] == STATION and j["upload_enabled"] is True

    # The sources block never carries the key either (configured goes false).
    r = client.get("/api/sources", headers=_H)
    assert KEY not in r.text
    assert r.json()["wu_upload"][MAC]["configured"] is False


# ───────────────────────── API surface ─────────────────────────

def test_put_wu_station_validation(client):
    _seed_device(client)
    # Unknown MAC still 404s (R3-08) — with the new fields too.
    r = client.put("/api/devices/00:11:22:33:44:55/wu-station", headers=_H,
                   json={"wu_station_id": "KAZX1", "upload_key": KEY,
                         "upload_enabled": True})
    assert r.status_code == 404
    # Upload config without any station to upload to is a loud 400, not a
    # silent drop.
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
                   json={"upload_key": KEY})
    assert r.status_code == 400
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
                   json={"upload_enabled": True})
    assert r.status_code == 400
    # Reviewer/demo token is read-only: GET yes, PUT 403.
    _configure(client)
    assert client.get(f"/api/devices/{MAC}/wu-station",
                      headers=_REVIEWER).status_code == 200
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=_REVIEWER,
                   json={"upload_enabled": False})
    assert r.status_code == 403
    # ...and the write didn't land.
    assert client.get(f"/api/devices/{MAC}/wu-station",
                      headers=_H).json()["upload_enabled"] is True
    # Omitted fields stay untouched; 1.4-style station-only PUT keeps the
    # upload config.
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
                   json={"wu_station_id": "KAZCHAND803"})
    j = r.json()
    assert j["wu_station_id"] == "KAZCHAND803"
    assert j["upload_enabled"] is True and j["upload_key_set"] is True
    # Clearing the station drops the whole association, key included.
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
                   json={"wu_station_id": ""})
    j = r.json()
    assert j["wu_station_id"] is None
    assert j["upload_enabled"] is False and j["upload_key_set"] is False


def test_sources_reports_upload_health(client, monkeypatch):
    _seed_device(client)
    # Nothing enabled → empty block (key always present for clients).
    j = client.get("/api/sources", headers=_H).json()
    assert j["wu_upload"] == {}

    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)
    asyncio.run(wu_upload.maybe_upload(MAC, _flat(int(time.time() * 1000))))
    j = client.get("/api/sources", headers=_H).json()
    blk = j["wu_upload"][MAC]
    assert blk["enabled"] is True and blk["configured"] is True
    assert blk["station_id"] == STATION
    assert blk["last_ok_ms"] is not None
    assert blk["failures"] == 0 and blk["last_error"] is None

    # Disabled again → the mac leaves the block.
    client.put(f"/api/devices/{MAC}/wu-station", headers=_H,
               json={"upload_enabled": False})
    j = client.get("/api/sources", headers=_H).json()
    assert j["wu_upload"] == {}
