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
    wu_import missing-readings principle, outbound). Seeded with a station
    that has only a thermometer: the upload fills a post's gaps from the
    station's composite (mirror #8), so the station itself must lack them."""
    r = client.post("/ingest/custom", headers=_ING,
                    json={"device": {"id": "AABBCCDDEEFF", "name": "Thermo"},
                          "timestamp_utc": (
                              dt.datetime.now(dt.timezone.utc)
                              - dt.timedelta(seconds=30)
                          ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "outdoor": {"tempf": 70.0}, "source": "test"})
    assert r.status_code == 200, r.text
    _drain_tasks()
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
    # ...but bad credentials are named via a fixed-marker match (R4-49), so
    # the user can tell "fix your key" from a transient hiccup.
    assert st["last_error"] == "WU rejected the station ID/key"


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


def test_overlapping_uploads_send_once(client, monkeypatch):
    """Throttle reservation is atomic with the config lookup: two overlapping
    tasks for the same mac must not both pass the check during the awaited
    DB read and double-send (CodeRabbit on R4-53)."""
    import asyncio as _asyncio
    _seed_device(client)
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    real_get = wu_upload.db.get_wu_station

    async def slow_get(mac):
        await _asyncio.sleep(0.05)   # widen the race window
        return await real_get(mac)
    monkeypatch.setattr(wu_upload.db, "get_wu_station", slow_get)

    now_ms = int(time.time() * 1000)

    async def scenario():
        return await _asyncio.gather(
            wu_upload.maybe_upload(MAC, _flat(now_ms)),
            wu_upload.maybe_upload(MAC, _flat(now_ms + 1000)))
    results = asyncio.run(scenario())
    assert sorted(results) == [False, True]
    assert len(calls) == 1


# ───────────────── partial posters (mirror #8, AcuRite Atlas) ─────────────────
#
# The Atlas sends its readings in three rotating radio messages about 10 s
# apart, and the LilyGO relay (like rtl_433 setups generally) posts each one
# as it hears it: temperature + humidity, wind direction + rain counter, or
# UV + light, with wind speed in all three. The dashboard composites the
# last few minutes of rows (db.latest_observation), so it looks complete;
# the WU forward used to send only the one partial post that happened to
# land on the 60 s throttle slot, so WU graphed temperature, humidity, UV
# and rain in sporadic patches and its current card showed wind only.

ATLAS_ID = "AABBCCDDEEFF"


def _atlas_post(client, ts: dt.datetime, kind: str, yearly_in: float = 12.30):
    body: dict = {"device": {"id": ATLAS_ID, "name": "AcuRite Atlas (SDR)"},
                  "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "wind": {"speed_mph": 4.0},
                  "source": "lilygo-relay"}
    if kind == "temp":      # message 0x25
        body["outdoor"] = {"tempf": 58.6, "humidity": 71, "dew_point_f": 49.3}
    elif kind == "rain":    # message 0x26
        body["wind"]["direction"] = 247
        body["rain"] = {"yearly_in": yearly_in}
    elif kind == "uv":      # message 0x27
        body["outdoor"] = {"uv": 3, "solar_wm2": 310.5}
    else:  # pragma: no cover
        raise ValueError(kind)
    r = client.post("/ingest/custom", headers=_ING, json=body)
    assert r.status_code == 200, r.text
    assert r.json().get("inserted") is not None
    _drain_tasks()
    return r


def _wait_calls(calls, n=1):
    deadline = time.time() + 5
    while len(calls) < n and time.time() < deadline:
        time.sleep(0.02)
    assert len(calls) >= n, "upload task never fired"


def test_partial_atlas_post_uploads_the_merged_reading(client, monkeypatch):
    """Each WU upload carries the station's whole current state, whichever
    of the three Atlas messages opened the throttle slot."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    t0 = now - dt.timedelta(seconds=40)
    _atlas_post(client, t0, "temp")                           # registers
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    _atlas_post(client, t0 + dt.timedelta(seconds=10), "rain")
    _wait_calls(calls, 1)
    p = calls[-1]
    # The post itself: wind direction + speed.
    assert p["winddir"] == 247.0 and p["windspeedmph"] == 4.0
    # The temperature message 10 s earlier: the station still has these.
    assert p["tempf"] == 58.6
    assert p["humidity"] == 71.0
    assert p["dewptf"] == 49.3

    # The next slot opens on the UV message: everything is still there.
    wu_upload._last_attempt.clear()
    _atlas_post(client, t0 + dt.timedelta(seconds=20), "uv")
    _wait_calls(calls, 2)
    p = calls[-1]
    assert p["UV"] == 3.0 and p["solarradiation"] == 310.5
    assert p["tempf"] == 58.6 and p["humidity"] == 71.0
    assert p["winddir"] == 247.0
    # The upload's timestamp is the post's own, not an older row's.
    assert p["dateutc"] == (t0 + dt.timedelta(seconds=20)).strftime(
        "%Y-%m-%d %H:%M:%S")


def test_partial_post_fill_is_bounded_and_never_invents(client, monkeypatch):
    """The fill reaches back only as far as the dashboard composite (a few
    minutes): a sensor that stopped reporting goes absent on WU rather than
    repeating its last value forever, and a field the station never had
    stays absent (no zeros)."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    t0 = now - dt.timedelta(minutes=20)
    _atlas_post(client, t0, "temp")
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    _atlas_post(client, now, "rain")      # 20 min after the last temperature
    _wait_calls(calls, 1)
    p = calls[-1]
    assert p["winddir"] == 247.0
    for absent in ("tempf", "humidity", "dewptf", "UV", "solarradiation",
                   "baromin", "indoortempf", "indoorhumidity"):
        assert absent not in p, absent


def test_counter_only_rain_reaches_wu(client, monkeypatch):
    """An Atlas over SDR posts only the lifetime rain counter. WU takes no
    counter, so the upload carries the day's total and the past hour the
    way the dashboard derives them, instead of no rain at all."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    t0 = now - dt.timedelta(minutes=3)
    _atlas_post(client, t0, "rain", yearly_in=12.30)
    _atlas_post(client, t0 + dt.timedelta(seconds=30), "temp")
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    _atlas_post(client, t0 + dt.timedelta(seconds=60), "rain", yearly_in=12.35)
    _wait_calls(calls, 1)
    p = calls[-1]
    assert p["dailyrainin"] == pytest.approx(0.05, abs=1e-6)
    assert p["rainin"] == pytest.approx(0.05, abs=1e-6)
    assert "yearlyrainin" not in p and "totalrainin" not in p


def _post_barometer(client, ts: dt.datetime, inhg: float = 30.12):
    """A co-located barometer on its own device (an Ecowitt gateway, say),
    named by SHARED_BAROMETER_SOURCE_MAC."""
    r = client.post("/ingest/custom", headers=_ING,
                    json={"device": {"id": "112233445566", "name": "Gateway"},
                          "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "indoor": {"tempf": 72.0, "humidity": 40},
                          "pressure": {"relative_inhg": inhg},
                          "source": "test"})
    assert r.status_code == 200, r.text
    _drain_tasks()


def test_shared_barometer_reaches_wu_only_while_fresh(client, monkeypatch):
    """The shared barometer fill is bounded by the same window as the
    station's own fields: a source that went quiet 30 minutes ago must not
    keep feeding WU its last pressure (CodeRabbit, PR #103)."""
    from app.config import settings
    monkeypatch.setattr(settings, "shared_barometer_source_mac",
                        "11:22:33:44:55:66")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    _post_barometer(client, now - dt.timedelta(minutes=30))
    _atlas_post(client, now - dt.timedelta(seconds=20), "temp")
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    _atlas_post(client, now - dt.timedelta(seconds=10), "rain")
    _wait_calls(calls, 1)
    p = calls[-1]
    assert p["tempf"] == 58.6
    for absent in ("baromin", "indoortempf", "indoorhumidity"):
        assert absent not in p, absent

    # A fresh reading from the same source does go through.
    _post_barometer(client, now - dt.timedelta(seconds=5), inhg=30.05)
    wu_upload._last_attempt.clear()
    _atlas_post(client, now, "uv")
    _wait_calls(calls, 2)
    assert calls[-1]["baromin"] == 30.05


def test_fill_window_is_anchored_to_the_post_not_the_last_stored_row(
        client, monkeypatch):
    """With the history throttle on, a post that adds no field is not
    stored, so the newest stored row can be minutes older than the post.
    The fill window is measured back from the post itself (CodeRabbit,
    PR #103): a temperature 9 minutes before the post stays off WU."""
    from app.config import settings
    monkeypatch.setattr(settings, "ingest_min_interval_seconds", 600)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    _atlas_post(client, now - dt.timedelta(minutes=9), "temp")
    _atlas_post(client, now - dt.timedelta(minutes=5), "rain")   # adds fields
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    r = _atlas_post(client, now, "rain")          # adds nothing: not stored
    assert r.json()["throttled"] is True
    _wait_calls(calls, 1)
    p = calls[-1]
    assert p["winddir"] == 247.0
    for absent in ("tempf", "humidity", "dewptf"):
        assert absent not in p, absent


def test_fill_never_carries_values_newer_than_the_post(client, monkeypatch):
    """A post that arrives out of order is uploaded with its own time, so it
    must not borrow readings taken after it."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    _atlas_post(client, now - dt.timedelta(seconds=10), "temp")   # newest
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    _atlas_post(client, now - dt.timedelta(minutes=2), "rain")    # late
    _wait_calls(calls, 1)
    p = calls[-1]
    assert p["winddir"] == 247.0
    assert "tempf" not in p and "humidity" not in p


# ── rain anchored to the post's own time (2.7 follow-up F27-03) ──
#
# The composite carried a day total across local midnight, and the derived
# day and hour were the rollups of NOW, so a delayed post borrowed rain
# measured after it. These drive `_current_state` on the real database with
# fixed local times in a non-UTC zone, so midnight and the hour are the
# zone's, never the test runner's.

_ZMAC = "AA:27:FF:00:00:0A"


def _prepare(monkeypatch, zone, rows, post):
    """Store `rows` (local datetimes → fields) and return what WU would be
    sent for `post` (one of them, or a post that was not stored)."""
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", zone)

    def ms(t):
        return int(t.timestamp() * 1000)

    async def run():
        await db.upsert_device(_ZMAC, {"name": "Anchored fixture"})
        await db.insert_observations(
            _ZMAC, [{"dateutc": ms(t), **f} for t, f in rows], received=False)
        return await wu_upload._current_state(_ZMAC, {"dateutc": ms(post[0]), **post[1]})
    return asyncio.run(run())


def _local(zone, *args):
    from zoneinfo import ZoneInfo
    return dt.datetime(*args, tzinfo=ZoneInfo(zone))


def test_a_day_total_is_never_carried_across_local_midnight(monkeypatch):
    """23:59:30 says 0.8 in; 00:00:30 posts only wind. The temperature
    still composites, yesterday's day total does not."""
    z = "America/Phoenix"
    before, after = _local(z, 2026, 7, 4, 23, 59, 30), _local(z, 2026, 7, 5, 0, 0, 30)
    p = _prepare(monkeypatch, z, [(before, {"tempf": 75.0, "dailyrainin": 0.8}),
                                  (after, {"windspeedmph": 4.0})],
                 (after, {"windspeedmph": 4.0}))
    assert p["tempf"] == 75.0
    assert "dailyrainin" not in p, p


def test_a_day_total_from_earlier_the_same_day_still_composites(monkeypatch):
    z = "America/Phoenix"
    t0, t1 = _local(z, 2026, 7, 5, 0, 0, 10), _local(z, 2026, 7, 5, 0, 2, 0)
    p = _prepare(monkeypatch, z, [(t0, {"tempf": 75.0, "dailyrainin": 0.02}),
                                  (t1, {"windspeedmph": 4.0})],
                 (t1, {"windspeedmph": 4.0}))
    assert p["dailyrainin"] == 0.02 and p["tempf"] == 75.0


def test_a_delayed_post_does_not_borrow_rain_measured_after_it(monkeypatch):
    """Counter 10.0 / 10.1 / 10.5 at 12:00 / 12:20 / 12:30. The 12:20
    reading, processed after 12:30, carries 0.1 in, not 0.5."""
    z = "America/Denver"
    rows = [(_local(z, 2026, 7, 5, 12, m), {"yearlyrainin": v})
            for m, v in ((0, 10.0), (20, 10.1), (30, 10.5))]
    p = _prepare(monkeypatch, z, rows, rows[1])
    assert p["dailyrainin"] == pytest.approx(0.1, abs=1e-6), p
    assert p["hourlyrainin"] == pytest.approx(0.1, abs=1e-6), p


def test_a_post_from_yesterday_gets_its_own_days_rain_not_todays(monkeypatch):
    """A replayed 23:50 post is uploaded with its own time, so it carries
    its own day's and hour's rain; the inch that fell after midnight
    belongs to another day and stays out."""
    z = "America/Chicago"
    rows = [(_local(z, 2026, 7, 4, 23, 40), {"yearlyrainin": 12.30}),
            (_local(z, 2026, 7, 4, 23, 50), {"yearlyrainin": 12.35}),
            (_local(z, 2026, 7, 5, 0, 30), {"yearlyrainin": 13.35})]
    p = _prepare(monkeypatch, z, rows, rows[1])
    assert p["dailyrainin"] == pytest.approx(0.05, abs=1e-6), p
    assert p["hourlyrainin"] == pytest.approx(0.05, abs=1e-6), p


def test_a_counter_reading_from_before_midnight_derives_no_day(monkeypatch):
    """The newest counter reading is 23:59, the post 00:00:30: what fell
    since midnight is unknown, so neither the day nor the hour is sent."""
    z = "America/Phoenix"
    rows = [(_local(z, 2026, 7, 4, 23, 0), {"yearlyrainin": 10.0}),
            (_local(z, 2026, 7, 4, 23, 59), {"yearlyrainin": 10.5})]
    post = (_local(z, 2026, 7, 5, 0, 0, 30), {"windspeedmph": 4.0})
    p = _prepare(monkeypatch, z, rows + [post], post)
    assert p["yearlyrainin"] == 10.5
    assert "dailyrainin" not in p and "hourlyrainin" not in p, p


def test_a_reset_after_the_post_cannot_inflate_it(monkeypatch):
    """The counter reset at 12:10 (10.0 to 0.2) and again at 12:30. The
    12:20 post's rain is measured from the first reset's floor through its
    own reading: 0.1, not the 0.3 the later reset's zero would give."""
    z = "America/Denver"
    rows = [(_local(z, 2026, 7, 5, 12, m), {"yearlyrainin": v})
            for m, v in ((0, 10.0), (10, 0.2), (20, 0.3), (30, 0.0))]
    p = _prepare(monkeypatch, z, rows, rows[2])
    assert p["dailyrainin"] == pytest.approx(0.1, abs=1e-6), p
    assert p["hourlyrainin"] == pytest.approx(0.1, abs=1e-6), p


def test_derived_rain_follows_the_local_clock_across_dst(monkeypatch):
    """Spring forward (Denver, 2026-03-08 02:00 MST to 03:00 MDT): the
    day starts at the MST midnight, the hour at 03:00 MDT."""
    z = "America/Denver"
    rows = [(_local(z, 2026, 3, 7, 23, 50), {"yearlyrainin": 9.9}),
            (_local(z, 2026, 3, 8, 1, 50), {"yearlyrainin": 10.0}),
            (_local(z, 2026, 3, 8, 3, 0), {"yearlyrainin": 10.2}),
            (_local(z, 2026, 3, 8, 3, 10), {"yearlyrainin": 10.3})]
    p = _prepare(monkeypatch, z, rows, rows[-1])
    assert p["dailyrainin"] == pytest.approx(0.4, abs=1e-6), p
    assert p["hourlyrainin"] == pytest.approx(0.1, abs=1e-6), p


# ── the 2.7 RC mutation step: edges nothing above pinned ──

def _through(monkeypatch, zone, rows, at):
    """Store `rows` and return db.counter_rain_through at local time `at`."""
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", zone)

    async def run():
        await db.upsert_device(_ZMAC, {"name": "Anchored fixture"})
        await db.insert_observations(
            _ZMAC, [{"dateutc": int(t.timestamp() * 1000), **f} for t, f in rows],
            received=False)
        return await db.counter_rain_through(_ZMAC, int(at.timestamp() * 1000), zone)
    return asyncio.run(run())


def test_a_counter_reading_after_a_delayed_post_is_never_read(monkeypatch):
    """A 12:20 post and a counter reading stored at 12:23: the reading is
    after the post, so it lends the post nothing, however close it is."""
    z = "America/Denver"
    rows = [(_local(z, 2026, 7, 5, 12, m), {"yearlyrainin": v})
            for m, v in ((0, 10.0), (20, 10.1), (23, 10.4))]
    p = _prepare(monkeypatch, z, rows, rows[1])
    assert p["dailyrainin"] == pytest.approx(0.1, abs=1e-6), p
    assert p["hourlyrainin"] == pytest.approx(0.1, abs=1e-6), p


def test_a_counter_reading_twenty_minutes_old_derives_nothing(monkeypatch):
    """The newest counter reading is 12:20, in the post's own day and
    hour, but 20 minutes older than it: past COUNTER_MAX_AGE_MS, so the
    rain to 12:40 is unknown, not the rain to 12:20."""
    z = "America/Denver"
    rows = [(_local(z, 2026, 7, 5, 12, m), {"yearlyrainin": v})
            for m, v in ((0, 10.0), (20, 10.1))]
    got = _through(monkeypatch, z, rows, _local(z, 2026, 7, 5, 12, 40))
    assert got == {"daily_in": None, "hourly_in": None}


def test_a_small_drop_below_the_day_start_is_zero_not_negative(monkeypatch):
    """10.0 at midnight, 9.8 at the post: a 0.2 in fall is a correction,
    not a reset (RAIN_RESET_DROP_IN is 0.5), and rain is never negative."""
    z = "America/Phoenix"
    rows = [(_local(z, 2026, 7, 5, 0, 0), {"yearlyrainin": 10.0}),
            (_local(z, 2026, 7, 5, 12, 20), {"yearlyrainin": 9.8})]
    p = _prepare(monkeypatch, z, rows, rows[1])
    assert p["dailyrainin"] == 0.0, p
    assert p["hourlyrainin"] == 0.0, p


def test_the_posts_own_values_win_over_the_composite(monkeypatch):
    """The composite fills what the post lacks; it never overwrites what
    the post says. 74 a minute ago, 75 now: WU is sent 75."""
    z = "America/Phoenix"
    p = _prepare(monkeypatch, z,
                 [(_local(z, 2026, 7, 5, 12, 0), {"tempf": 74.0})],
                 (_local(z, 2026, 7, 5, 12, 1), {"tempf": 75.0}))
    assert p["tempf"] == 75.0, p


def test_a_post_with_a_day_total_still_gets_the_derived_hour(monkeypatch):
    """The post carries its day total and the counter but no hour: the
    hour is derived from the counter, and the day stays the post's."""
    z = "America/Denver"
    post = (_local(z, 2026, 7, 5, 12, 20), {"yearlyrainin": 10.1, "dailyrainin": 0.3})
    p = _prepare(monkeypatch, z,
                 [(_local(z, 2026, 7, 5, 12, 0), {"yearlyrainin": 10.0}), post], post)
    assert p["dailyrainin"] == 0.3, p
    assert p["hourlyrainin"] == pytest.approx(0.1, abs=1e-6), p


def test_a_day_total_from_the_previous_hour_still_composites(monkeypatch):
    """The day total is scoped to the DAY: one stored at 11:58 is still
    today's at 12:01, across the hour."""
    z = "America/Phoenix"
    t0, t1 = _local(z, 2026, 7, 5, 11, 58), _local(z, 2026, 7, 5, 12, 1)
    p = _prepare(monkeypatch, z, [(t0, {"tempf": 75.0, "dailyrainin": 0.4}),
                                  (t1, {"windspeedmph": 4.0})],
                 (t1, {"windspeedmph": 4.0}))
    assert p["dailyrainin"] == 0.4 and p["tempf"] == 75.0, p


def test_a_composite_more_than_five_minutes_from_the_post_lends_nothing(monkeypatch):
    """`_current_state`'s own skew guard: a composite whose newest row is
    six minutes from the post fills nothing, four minutes still fills.
    latest_observation anchors its window too; this pins the guard on its
    own, so a widened COMPOSITE_MAX_SKEW_MS cannot hide behind it."""
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    post_ms = int(_local("America/Phoenix", 2026, 7, 5, 12, 30).timestamp() * 1000)

    def run(age_min):
        async def fake_latest(mac, reference_ms=None, day_start_ms=None):
            return {"dateutc": post_ms - age_min * 60_000, "tempf": 74.0,
                    "humidity": 30.0}
        monkeypatch.setattr(db, "latest_observation", fake_latest)
        return asyncio.run(wu_upload._current_state(
            _ZMAC, {"dateutc": post_ms, "windspeedmph": 4.0}))
    stale = run(6)
    assert "tempf" not in stale and "humidity" not in stale, stale
    assert stale["windspeedmph"] == 4.0
    assert run(4)["tempf"] == 74.0


def test_throttled_counter_rise_still_reaches_wu(client, monkeypatch):
    """With the history throttle on, a counter-only post whose counter rose
    used to be coalesced away (it adds no new field), while WU's derived
    rain reads the STORED counter, so the upload carried the older total
    (CodeRabbit, PR #103). A changed rain counter is stored, so the
    rollups, the dashboard and WU all see the rise."""
    from app.config import settings
    monkeypatch.setattr(settings, "ingest_min_interval_seconds", 600)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    _atlas_post(client, now - dt.timedelta(minutes=2), "rain", yearly_in=12.30)
    _configure(client)
    calls: list[dict] = []
    _capture_send(monkeypatch, calls)

    r = _atlas_post(client, now, "rain", yearly_in=12.35)
    assert r.json()["inserted"] == 1
    _wait_calls(calls, 1)
    p = calls[-1]
    assert p["dailyrainin"] == pytest.approx(0.05, abs=1e-6)
    assert p["rainin"] == pytest.approx(0.05, abs=1e-6)
