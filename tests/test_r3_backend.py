"""Round-3 review regression tests (backend/app findings R3-xx).

Each test names the finding it pins and fails against the pre-fix code.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:DD:EE:FF"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_obs(client, extra_outdoor=None, extra=None, ts=None):
    body = {"device": {"id": "AABBCCDDEEFF", "name": "Yard"},
            "timestamp_utc": ts or _now_iso(),
            "outdoor": {"tempf": 75.0, "humidity": 40, **(extra_outdoor or {})},
            "wind": {}, "rain": {}, "pressure": {"relative_inhg": 29.9},
            "source": "test"}
    if extra:
        body.update(extra)
    return client.post("/ingest/custom", headers=IH, json=body)


# ───────────── R3-01: httpx INFO logging must not reach server logs ─────────────
# httpx logs every request URL at INFO ("HTTP Request: GET https://...apiKey=X"),
# and main.py's basicConfig(level=INFO) would print it — leaking the WU/AWN/
# WeatherLink keys carried as query params into the Fly log stream.

def test_httpx_request_logging_is_suppressed(client):
    # The logger's OWN level must be pinned (not just the effective level:
    # under pytest the root logger already has handlers, so basicConfig
    # no-ops and an effective-level check passes vacuously). In production
    # root sits at INFO, and an unset httpx logger inherits it.
    for name in ("httpx", "httpcore"):
        lvl = logging.getLogger(name).level
        assert lvl >= logging.WARNING, \
            f"{name} logger level {lvl} would print keyed request URLs at INFO"


# ───────────── R3-09: overflow STRINGS must not bind Inf into REAL columns ─────────────
# _scrub_numbers only filters values that arrive AS numbers; the string
# "1e999" sailed through, SQLite's REAL affinity coerced it to Inf at insert,
# and AVG()/MIN()/MAX() then 500'd /records, /summary and bucketed /history
# forever (JSONResponse serializes with allow_nan=False).

def test_flatten_overflow_string_metrics_become_none(client):
    from app.ingest import _flatten
    flat = _flatten({
        "device": {"id": "AABBCCDDEEFF"},
        "timestamp_utc": _now_iso(),
        "outdoor": {"tempf": "1e999", "humidity": "not-a-number"},
        "wind": {"speed_mph": "1e999"},
        "rain": {"daily_in": "1e999"},
        "pressure": {"relative_inhg": "1e999"},
    })
    assert flat is not None
    for key in ("tempf", "humidity", "windspeedmph", "baromrelin",
                "dailyrainin", "feelsLike"):
        assert flat[key] is None, f"{key} survived as {flat[key]!r}"
    # ...while legitimate numeric strings coerce instead of vanishing.
    flat2 = _flatten({
        "device": {"id": "AABBCCDDEEFF"},
        "timestamp_utc": _now_iso(),
        "outdoor": {"tempf": "75.5"},
    })
    assert flat2["tempf"] == 75.5


def test_records_survive_overflow_string_metric(client):
    r = _post_obs(client, extra_outdoor={"tempf": "1e999"},
                  extra={"wind": {"speed_mph": "1e999"},
                         "pressure": {"relative_inhg": "1e999"}})
    assert r.status_code == 200, r.text
    # A normal reading alongside, so the aggregates have real data too.
    earlier = datetime.now(timezone.utc).timestamp() - 3600
    r = _post_obs(client, ts=datetime.fromtimestamp(
        earlier, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    for url in (f"/api/devices/{MAC}/records",
                f"/api/devices/{MAC}/summary?field=tempf",
                f"/api/devices/{MAC}/history?hours=48"):
        resp = client.get(url, headers=H)
        assert resp.status_code == 200, f"{url} -> {resp.status_code}"


# ───────────── R3-10 / R3-126: public page survives malformed STORED values ─────────────
# The anonymous status page called float() on stored observation values.
# Junk already in the DB (stored before the ingest scrub existed) must not
# 500 the public `/` page.

def test_public_page_survives_junk_stored_observation(client, monkeypatch):
    from app import db as dbmod
    from app.config import settings
    assert _post_obs(client).status_code == 200          # device row exists
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Inject a poisoned row directly — simulates a DB written before the
    # ingest-boundary scrub (the string passes _scrub_nonfinite untouched).
    added = asyncio.run(dbmod.insert_observations(
        MAC, [{"dateutc": now_ms + 1, "tempf": "hot"}]))
    assert added == 1
    r = client.get("/")
    assert r.status_code == 200
    monkeypatch.setattr(settings, "public_dashboard", True)
    r = client.get("/")
    assert r.status_code == 200


# ───────────── R3-02 (backend): /api/import/wu falls back to the server key ─────────────
# api_key was REQUIRED in the body, so a LAN user who followed the app's own
# advice (set the key server-side, never POST it over cleartext) could not
# start an import at all.

def test_wu_import_falls_back_to_server_stored_key(client, monkeypatch):
    from app import wu_import
    _post_obs(client)
    client.put(f"/api/devices/{MAC}/wu-station", headers=H,
               json={"wu_station_id": "KAZCHAND668"})
    assert client.put("/api/config/wu-key", headers=H,
                      json={"api_key": "serverkey123"}).status_code == 200
    captured = {}
    def fake_start(mac, station, api_key, start, end, dry_run, force=False):
        captured.update(mac=mac, api_key=api_key)
        return True
    monkeypatch.setattr(wu_import, "start_import", fake_start)
    r = client.post("/api/import/wu", headers=H,
                    json={"mac": MAC, "start_date": "2023-04-08",
                          "end_date": "2023-04-08"})
    assert r.status_code == 200, r.text
    assert captured["api_key"] == "serverkey123"
    # An explicit body key still wins over the stored one.
    r = client.post("/api/import/wu", headers=H,
                    json={"mac": MAC, "api_key": "bodykey12345",
                          "start_date": "2023-04-08", "end_date": "2023-04-08"})
    assert r.status_code == 200
    assert captured["api_key"] == "bodykey12345"
    # Present-but-short keys are still rejected by validation.
    r = client.post("/api/import/wu", headers=H,
                    json={"mac": MAC, "api_key": "short",
                          "start_date": "2023-04-08"})
    assert r.status_code == 422


def test_wu_import_400_when_no_key_anywhere(client):
    _post_obs(client)
    client.put(f"/api/devices/{MAC}/wu-station", headers=H,
               json={"wu_station_id": "KAZCHAND668"})
    r = client.post("/api/import/wu", headers=H,
                    json={"mac": MAC, "start_date": "2023-04-08"})
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()


# ───────────── R3-08: put_wu_station requires an existing device ─────────────
# A typo'd MAC created a wu_station_map row for a nonexistent device, which
# then attached to any future device registering under that MAC.

def test_put_wu_station_unknown_device_404(client):
    r = client.put("/api/devices/00:00:00:00:00:00/wu-station", headers=H,
                   json={"wu_station_id": "KAZCHAND668"})
    assert r.status_code == 404
    # ...and a known device still associates + clears fine.
    _post_obs(client)
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=H,
                   json={"wu_station_id": "kazchand668"})
    assert r.status_code == 200 and r.json()["wu_station_id"] == "KAZCHAND668"
    r = client.put(f"/api/devices/{MAC}/wu-station", headers=H,
                   json={"wu_station_id": ""})
    assert r.status_code == 200 and r.json()["wu_station_id"] is None


# ───────────── R3-20 (backend): /history accepts a DST-lengthened month ─────────────
# A 31-day month containing a DST fall-back transition is 745 absolute hours;
# the 744 cap 422'd the app's whole-month request every November.

def test_history_accepts_745_hours(client):
    _post_obs(client)
    assert client.get(f"/api/devices/{MAC}/history?hours=745",
                      headers=H).status_code == 200
    assert client.get(f"/api/devices/{MAC}/history?hours=746",
                      headers=H).status_code == 422


# ───────────── R3-27 (backend): relay battery fields reach battout/battin ─────────────
# sdr-relay and davis-relay post device.battery_outdoor/"battery_hub" =
# "normal"/"low"; _flatten never mapped them, so the iOS battery icon and the
# battery-low notable were permanently inert for every relay-fed station.

def test_ingest_maps_relay_battery_fields(client):
    r = _post_obs(client, extra={"device": {"id": "AABBCCDDEEFF",
                                            "battery_outdoor": "low",
                                            "battery_hub": "normal"}})
    assert r.status_code == 200, r.text
    cur = client.get(f"/api/devices/{MAC}/current", headers=H).json()
    assert cur.get("battout") == 0       # AWN convention: 0 = low, 1 = ok
    assert cur.get("battin") == 1
    # Unknown/absent battery state stays None rather than guessing.
    r = _post_obs(client, extra={"device": {"id": "AABBCCDDEEFF",
                                            "battery_outdoor": "weird"}},
                  ts=_now_iso().replace("Z", "") + "Z")
    assert r.status_code == 200


# ───────────── R3-12 (pragmatic slice): concurrent rebuilds serialize ─────────────

def test_concurrent_rebuilds_serialize_and_stay_correct(client, monkeypatch):
    from app import insights
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    base_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    from app import db as dbmod

    async def scenario():
        rows = [{"dateutc": base_ms - i * 60_000, "tempf": 70.0 + i}
                for i in range(5)]
        await dbmod.insert_observations(MAC, rows)
        r1, r2 = await asyncio.gather(insights.rebuild(), insights.rebuild())
        return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1 == {"rows": 5} and r2 == {"rows": 5}

    async def folded_n():
        async with dbmod.connect() as db:
            cur = await db.execute(
                "SELECT SUM(tempf_n) FROM hour_rollups WHERE mac = ?", (MAC,))
            return (await cur.fetchone())[0]

    # Serialized rebuilds leave exactly one fold per stored row; interleaved
    # ones double-fold the additive sums.
    assert asyncio.run(folded_n()) == 5
    body = client.get("/api/insights?mac=" + MAC, headers=H).json()
    assert body["day_count"] == 1
