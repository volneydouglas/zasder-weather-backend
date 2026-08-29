"""Round-2 review regression tests (backend/app findings R2-xx).

Each test names the finding it pins and fails against the pre-fix code —
mutation-checked for the Mediums (R2-04..R2-08) plus the sharper Lows.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import alerts, apns, ingest  # noqa: E402
from app.config import Settings  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token",
      "Content-Type": "application/json"}
MAC = "AA:BB:CC:DD:EE:FF"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_obs(client, ts_iso: str, outdoor=None, rain=None, name="Yard"):
    return client.post("/ingest/custom", headers=IH, json={
        "device": {"id": MAC, "name": name},
        "timestamp_utc": ts_iso,
        "outdoor": outdoor or {"tempf": 75},
        **({"rain": rain} if rain else {}),
    })


# ───────────────────── R2-04: reviewer token collisions ─────────────────────

def test_reviewer_token_must_differ_from_api_token():
    with pytest.raises(Exception, match="reviewer_api_token"):
        Settings(api_token="a" * 32, ingest_token="b" * 32,
                 reviewer_api_token="a" * 32)

def test_reviewer_token_must_differ_from_ingest_token():
    with pytest.raises(Exception, match="reviewer_api_token"):
        Settings(api_token="a" * 32, ingest_token="b" * 32,
                 reviewer_api_token="b" * 32)

def test_api_and_ingest_tokens_must_still_differ():
    with pytest.raises(Exception, match="ingest_token"):
        Settings(api_token="a" * 32, ingest_token="a" * 32)

def test_distinct_tokens_are_accepted():
    s = Settings(api_token="a" * 32, ingest_token="b" * 32,
                 reviewer_api_token="c" * 32)
    assert s.reviewer_api_token == "c" * 32


# ───────────────── R2-05: overflow-string rain must not store inf ─────────────────

def test_flatten_rejects_overflow_string_yearly_rain():
    out = ingest._flatten({
        "device": {"id": "AABBCCDDEEFF"},
        "timestamp_utc": "2026-08-01T12:00:00Z",
        "rain": {"yearly_in": "1e999"},          # float("1e999") == inf
    })
    assert out is not None and out["yearlyrainin"] is None

def test_feels_like_rejects_overflow_string_inputs():
    # Strings bypass _scrub_numbers; float() must not launder them into inf.
    assert ingest._compute_feels_like("1e999", 50, 5.0) is None
    assert ingest._compute_feels_like(85.0, "1e999", 0.0) == pytest.approx(85.0, abs=1)
    assert ingest._compute_feels_like(40.0, 50, "1e999") == 40.0

def test_overflow_string_rain_does_not_500_current(client):
    r = _post_obs(client, "2026-08-01T12:00:00Z", rain={"yearly_in": "1e999"})
    assert r.status_code == 200, r.text
    cur = client.get(f"/api/devices/{MAC}/current", headers=H)
    assert cur.status_code == 200, cur.text
    assert cur.json().get("yearlyrainin") is None


# ───────────── R2-06: delete_device cascade covers location + smart state ─────────────

def test_delete_device_removes_location_and_smart_alert_state(client, monkeypatch):
    from app import db
    from app.config import settings
    # Insights on, so the ingest below folds rollup rows we can then assert
    # are cascaded away (R3-08: 1.4 reintroduced the orphaned-table pattern).
    monkeypatch.setattr(settings, "insights", True)
    assert _post_obs(client, "2026-08-01T12:00:00Z").status_code == 200
    client.put(f"/api/devices/{MAC}/location", headers=H,
               json={"lat": 33.3, "lon": -111.9, "label": "Home"})
    asyncio.run(db.upsert_smart_alert_state(MAC, "frost", 1, 123))
    # 1.4 MAC-keyed state: WU station association + insights rollups.
    assert client.put(f"/api/devices/{MAC}/wu-station", headers=H,
                      json={"wu_station_id": "KAZCHAND668"}).status_code == 200
    body = client.delete(f"/api/devices/{MAC}", headers=H).json()
    assert body["location"] == 1
    assert body["smart_alert_state"] == 1
    assert body["wu_station"] == 1
    assert body["daily_rollups"] == 1
    assert body["hour_rollups"] == 1
    assert asyncio.run(db.device_locations()) == {}
    assert asyncio.run(db.get_smart_alert_states()) == {}
    # A re-registered MAC must not inherit the old WU station association
    # (the Import screen defaults to it — an import would pull the OLD
    # station's archive into the new device)...
    assert asyncio.run(db.get_wu_station(MAC)) is None
    # ...nor serve the deleted device's statistics / fold onto stale sums.
    from app import insights
    assert asyncio.run(insights.assemble(MAC))["day_count"] == 0
    # A re-registered MAC must not inherit the old location.
    assert _post_obs(client, "2026-08-01T13:00:00Z").status_code == 200
    devs = client.get("/api/devices", headers=H).json()
    d = next(x for x in devs if x["mac"] == MAC)
    coords = (d.get("info") or {}).get("coords") or {}
    assert coords.get("location") != "Home"


# ───────────── R2-07: device-down alert retries after failed delivery ─────────────

def test_device_down_alert_retries_when_delivery_fails(client, monkeypatch):
    from app import db
    now = datetime.now(timezone.utc)
    # Device last reported an hour ago (default stale threshold: 15 min).
    assert _post_obs(client, _iso(now - timedelta(hours=1))).status_code == 200
    last_seen = client.get("/api/devices", headers=H).json()[0]["lastSeen"]
    # Seed a prior OK state so this tick sees an OK→stale transition.
    asyncio.run(db.upsert_alert_state(MAC, "ok", last_seen,
                                      last_seen, None))

    calls = {"n": 0}
    delivered = {"v": False}
    async def fake_deliver(cfg, subj, body, pt, pb, **kw):
        calls["n"] += 1
        return delivered["v"]
    async def fake_push_configured():
        return True                     # let _tick run without SMTP config
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts.apns, "push_configured", fake_push_configured)

    mon = alerts.AlertMonitor()
    asyncio.run(mon._tick())
    asyncio.run(mon._tick())
    assert calls["n"] == 2, ("initial device-down alert must be retried "
                             "while delivery keeps failing")
    # Delivery recovers → alert lands once, then goes quiet (edge-triggered).
    delivered["v"] = True
    asyncio.run(mon._tick())
    assert calls["n"] == 3
    asyncio.run(mon._tick())
    assert calls["n"] == 3, "successful delivery must not re-fire"
    assert asyncio.run(db.get_alert_states())[MAC]["state"] == "stale"


# ───────────── R2-08: relay mode must not wipe sandbox/guessed-env tokens ─────────────

def test_relay_mode_does_not_prune_mismatched_or_guessed_env_tokens(client, monkeypatch):
    from app import db
    # Stored envs: sandbox (mismatched vs APNS_ENV=production), production
    # (matches), and none (env will be guessed from APNS_ENV).
    client.post("/api/push/register", headers=H, json={"token": "a" * 64, "env": "sandbox"})
    client.post("/api/push/register", headers=H, json={"token": "b" * 64, "env": "production"})
    client.post("/api/push/register", headers=H, json={"token": "c" * 64})
    client.put("/api/push/relay", headers=H, json={
        "relay_url": "https://weather.zasder.com/api/relay/push",
        "relay_token": "rtok"})

    seen = {}
    async def fake_relay(tokens, title, body, url, token, **kw):
        seen["tokens"] = list(tokens)
        return {"sent": 0, "dead": list(tokens), "failed": 0}
    monkeypatch.setattr(apns, "_push_via_relay", fake_relay)

    asyncio.run(apns.send_to_all("T", "B"))
    # The sandbox token was never sent (one env per relay batch) …
    assert seen["tokens"] == ["b" * 64, "c" * 64]
    left = {t["token"] for t in asyncio.run(db.list_push_tokens())}
    # … and neither it nor the guessed-env token was pruned; only the token
    # whose OWN stored env matched the batch env may be trusted as dead.
    assert "a" * 64 in left
    assert "c" * 64 in left
    assert "b" * 64 not in left


# ───────────────── R2-20: re-arm deadband (hysteresis) ─────────────────

def test_rule_cleared_requires_margin():
    # above 100: dipping to 99.9 must NOT re-arm; 98.9 must.
    assert alerts.rule_cleared("above", 100.0, 99.9, 1.0) is False
    assert alerts.rule_cleared("above", 100.0, 98.9, 1.0) is True
    # below 32: 32.5 must NOT re-arm with a 1° margin; 33.1 must.
    assert alerts.rule_cleared("below", 32.0, 32.5, 1.0) is False
    assert alerts.rule_cleared("below", 32.0, 33.1, 1.0) is True
    # equalTo 50 (±0.5 trigger band): needs to leave band by the margin.
    assert alerts.rule_cleared("equalTo", 50.0, 50.8, 1.0) is False
    assert alerts.rule_cleared("equalTo", 50.0, 51.6, 1.0) is True

def test_rearm_transition_requires_continuous_dwell():
    """The dwell clock behind wind-chatter suppression (Doren, 2026-08-23:
    instantaneous wind poked over a 10 mph rule every ~8 min while lulls
    re-armed it through the deadband — one breezy afternoon, alerts all
    afternoon). Re-arm needs the value clear for the WHOLE window,
    continuously."""
    dwell = 15 * 60_000
    # Not cleared: never re-arms, and any running clock resets.
    assert alerts.rearm_transition(False, None, 0, dwell) == (False, None)
    assert alerts.rearm_transition(False, 1_000, 999_000, dwell) == (False, None)
    # First cleared tick starts the clock, no re-arm yet.
    assert alerts.rearm_transition(True, None, 5_000, dwell) == (False, 5_000)
    # Still clearing but short of the window: clock holds, no re-arm.
    assert alerts.rearm_transition(True, 5_000, 5_000 + dwell - 1, dwell) \
        == (False, 5_000)
    # Window complete: re-arm, clock cleared.
    assert alerts.rearm_transition(True, 5_000, 5_000 + dwell, dwell) \
        == (True, None)


def test_smart_cleared_requires_margin():
    kw = dict(frost_f=35.0, heat_f=105.0, drop_inhg=0.06)
    assert alerts.smart_cleared("frost", tempf=35.5, **kw) is False
    assert alerts.smart_cleared("frost", tempf=36.5, **kw) is True
    assert alerts.smart_cleared("heat", feels=104.5, **kw) is False
    assert alerts.smart_cleared("heat", feels=103.5, **kw) is True
    assert alerts.smart_cleared("frost", tempf=None, **kw) is False  # no data ≠ cleared


# ───────────────── R2-32 / R2-50 / R2-116 / R2-113 / R2-52: Settings guards ─────────────────

def test_test_prefix_exemption_only_applies_under_pytest(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "_under_pytest", lambda: False)
    with pytest.raises(Exception, match="32 characters"):
        Settings(api_token="test-short")

def test_test_prefix_tokens_still_work_in_the_suite():
    assert Settings(api_token="test-short").api_token == "test-short"

def test_blank_api_token_is_rejected_at_boot():
    with pytest.raises(Exception, match="blank"):
        Settings(api_token="   ")

def test_invalid_timezone_is_rejected_at_boot():
    with pytest.raises(Exception, match="IANA"):
        Settings(api_token="a" * 32, timezone="America/Phoenx")
    assert Settings(api_token="a" * 32, timezone="America/Phoenix").timezone \
        == "America/Phoenix"

def test_aw_placeholder_change_me_does_not_start_poller():
    s = Settings(api_token="a" * 32, aw_application_key="change-me",
                 aw_api_key="x" * 32)
    assert s.aw_configured is False

def test_malformed_mac_map_key_is_dropped():
    s = Settings(api_token="a" * 32,
                 alert_stale_minutes_by_mac={"GARBAGE": 5, "5d5d0200007d": 10})
    assert s.alert_stale_minutes_by_mac == {"5D:5D:02:00:00:7D": 10.0}


# ───────────────── R2-51: alert recipient / SMTP header hygiene ─────────────────

def test_recipient_with_embedded_comma_rejected(client):
    r = client.put("/api/alerts", headers=H,
                   json={"recipients": ["a@b.com,c@d.com"]})
    assert r.status_code == 400

def test_smtp_from_with_newline_rejected(client):
    r = client.put("/api/alerts", headers=H,
                   json={"smtp_from": "x@example.com\nBcc: evil@example.com"})
    assert r.status_code == 400

def test_plain_recipient_still_accepted(client):
    assert client.put("/api/alerts", headers=H,
                      json={"recipients": ["ok@example.com"]}).status_code == 200


# ───────────────── R2-53: forecast device-coords fallback must 400, not 500 ─────────────────

def test_forecast_with_non_numeric_device_coords_returns_400(client, monkeypatch):
    from app import db
    from app.config import settings as _settings
    # Pin the device-coords fallback: get_forecast only reads the device blob
    # when BOTH env coords are unset, so a suite environment with
    # FORECAST_LAT/LON set would skip the coercion branch under test (and hit
    # the real Open-Meteo endpoint). Same setup as test_api.py's forecast tests.
    monkeypatch.setattr(_settings, "forecast_lat", None)
    monkeypatch.setattr(_settings, "forecast_lon", None)
    asyncio.run(db.upsert_device(MAC, {
        "name": "Yard", "auto_name": "Yard",
        "info": {"coords": {"coords": {"lat": "not-a-number", "lon": "nope"}}},
        "lastData": {"dateutc": 1_000},
    }))
    r = client.get("/api/forecast", headers=H)
    assert r.status_code == 400, r.text


# ───────────────── R2-54: out-of-order post must not rename the device ─────────────────

def test_upsert_device_name_is_monotonic(client):
    from app import db
    def up(name, ts):
        asyncio.run(db.upsert_device(MAC, {
            "name": name, "auto_name": "Auto", "info": {},
            "lastData": {"dateutc": ts}}))
    up(None, 2000)
    up("StaleName", 1000)      # out-of-order replay carrying an explicit name
    devs = asyncio.run(db.list_devices())
    assert devs[0]["name"] == "Auto", "an older post must not rename the device"
    up("FreshName", 3000)      # newer explicit rename still applies
    assert asyncio.run(db.list_devices())[0]["name"] == "FreshName"


# ───────────────── R2-55: corrupt JSON rows must degrade, not 500 ─────────────────

def test_corrupt_rows_do_not_500_read_endpoints(client):
    from app.config import settings
    assert _post_obs(client, "2026-08-01T12:00:00Z").status_code == 200
    con = sqlite3.connect(settings.database_path)
    con.execute("INSERT INTO observations (mac, dateutc_ms, data_json) "
                "VALUES (?, ?, ?)", (MAC, 1_754_000_000_000, "{corrupt"))
    con.execute("INSERT INTO devices (mac, name, info_json, last_seen_ms) "
                "VALUES ('11:22:33:44:55:66', 'Broken', '{also-corrupt', 1)")
    con.commit(); con.close()
    assert client.get("/api/devices", headers=H).status_code == 200
    assert client.get(f"/api/devices/{MAC}/current", headers=H).status_code == 200
    start = 1_754_000_000_000 - 3_600_000
    r = client.get(f"/api/devices/{MAC}/history",
                   headers=H, params={"start_ms": start,
                                      "end_ms": 1_754_000_000_000 + 3_600_000})
    assert r.status_code == 200


# ───────────────── R2-56: timestamp sanity bounds ─────────────────

def test_far_future_timestamp_is_clamped_to_server_time(client):
    r = _post_obs(client, "2100-01-01T00:00:00Z")
    assert r.status_code == 200, r.text
    now_ms = int(time.time() * 1000)
    assert r.json()["ts_ms"] <= now_ms + 16 * 60 * 1000
    last_seen = client.get("/api/devices", headers=H).json()[0]["lastSeen"]
    assert last_seen <= now_ms + 16 * 60 * 1000, \
        "a future clock must not silence the staleness monitor"

def test_ancient_timestamp_is_rejected(client):
    assert _post_obs(client, "2020-01-01T00:00:00Z").status_code == 400

def test_slightly_future_timestamp_is_kept_verbatim(client):
    ts = datetime.now(timezone.utc) + timedelta(minutes=5)
    r = _post_obs(client, _iso(ts))
    assert r.status_code == 200
    assert abs(r.json()["ts_ms"] - ts.timestamp() * 1000) < 2000


# ───────────────── R2-58: chart-index probe parses columns ─────────────────

def test_stale_chart_index_missing_tempf_is_rebuilt(client):
    from app import db
    from app.config import settings
    # Stale index: every expected column present as a SUBSTRING (tempf ⊂
    # tempinf, humidity ⊂ humidityin) but tempf/humidity missing as COLUMNS —
    # exactly the case the old substring probe waved through.
    stale_cols = [c for c in db._CHART_INDEX_COLS if c not in ("tempf", "humidity")]
    con = sqlite3.connect(settings.database_path)
    con.execute("DROP INDEX idx_obs_chart")
    con.execute("CREATE INDEX idx_obs_chart ON observations ("
                + ", ".join(stale_cols) + ")")
    con.commit(); con.close()
    asyncio.run(db.init_db())
    con = sqlite3.connect(settings.database_path)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='idx_obs_chart'"
                      ).fetchone()[0]
    con.close()
    assert set(db._CHART_INDEX_COLS) <= db._index_columns(sql), \
        "init_db must rebuild an index that lost a covered column"


# ───────────────── R2-62: control chars in device names ─────────────────

def test_device_name_control_chars_are_sanitized_in_messages():
    subject, body = alerts.build_alert("stale", "Bad\nName", MAC, 0,
                                       11 * 60_000, 10, "UTC")
    assert "\n" not in subject and "\r" not in subject
    assert "Bad Name" in subject
    # The BODY carries the name too (R3-142: it was never asserted — and the
    # old second clause was a no-op `"BadName".replace("", "")`). The body
    # legitimately contains newlines of its own; the NAME inside it must not.
    assert "Bad\nName" not in body and "Bad Name" in body
    title, tbody = alerts.build_threshold_message("Evil\r\nDevice", "tempf",
                                                  101.0, "above", 100.0)
    assert "\n" not in title and "\r" not in title
    assert "Evil\r\nDevice" not in tbody
    t2, _ = alerts.build_smart_message("frost", "X\nY", tempf=30.0)
    assert "\n" not in t2


# ───────────────── R2-63: make_jwt honors now=0 ─────────────────

def test_make_jwt_treats_zero_now_as_epoch():
    import jwt as _jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    tok = apns.make_jwt("TEAM", "KEY", pem, now=0)
    # Signature verified (R3-141) — see test_apns_make_jwt_structure.
    assert _jwt.decode(tok, key.public_key(), algorithms=["ES256"])["iat"] == 0


# ───────────────── R2-68: insert_observations scrubs non-finite ─────────────────

def test_insert_observations_scrubs_non_finite_floats(client):
    from app import db
    asyncio.run(db.insert_observations(MAC, [{
        "dateutc": 1_754_000_000_000, "tempf": float("inf"),
        "humidity": float("nan"), "windspeedmph": 5.0,
        "nested": {"x": float("-inf")},
    }]))
    cur = client.get(f"/api/devices/{MAC}/current", headers=H)
    assert cur.status_code == 200, cur.text
    body = cur.json()
    assert body["tempf"] is None and body["humidity"] is None
    assert body["windspeedmph"] == 5.0


# ───────────────── R2-115: push token shape bounds ─────────────────

def test_push_register_rejects_junk_tokens(client):
    assert client.post("/api/push/register", headers=H,
                       json={"token": "has space in it"}).status_code == 422
    assert client.post("/api/push/register", headers=H,
                       json={"token": "x" * 600}).status_code == 422
    assert client.post("/api/push/register", headers=H,
                       json={"token": "a" * 64, "env": "production"}).status_code == 200


# ───────────────── PR9: per-MAC throttle-lock registry must not grow forever ─────────────────

def test_throttle_lock_registry_evicts_idle_macs():
    """The production event loop lives for the process lifetime, so a lock
    left in the per-loop dict per MAC is an unbounded cache fed by request
    input (any ingest-token holder can invent MACs). The guard must still
    serialize concurrent tasks on the SAME mac while they overlap."""
    async def run():
        loop = asyncio.get_running_loop()
        inside = {"AA:AA": 0, "BB:BB": 0}   # different MACs MAY overlap
        overlap_seen = False

        async def worker(mac: str):
            nonlocal overlap_seen
            async with ingest._throttle_lock(mac):
                inside[mac] += 1
                if inside[mac] > 1:
                    overlap_seen = True
                await asyncio.sleep(0.005)
                inside[mac] -= 1

        await asyncio.gather(*[worker("AA:AA") for _ in range(5)],
                             *[worker("BB:BB") for _ in range(3)])
        assert not overlap_seen, "two tasks were inside one MAC's critical section"
        # Every task has left: the registry for this loop must be empty again.
        assert ingest._THROTTLE_LOCKS.get(loop, {}) == {}

    asyncio.run(run())


# ───────────────── rain glitch guard: level shifts rebaseline ─────────────────

import datetime as _dt

# ONE clock snapshot for every rain timestamp in this module, truncated to
# the second. _rain_ts used to re-read now() per call, so the sub-second
# phase could shift a truncated timestamp by one second between two calls —
# which flipped the EXACT-boundary assertions in the slow-cadence test
# (they pin < vs <= at precisely the confirmation gap) into a rare
# whole-suite-only flake.
_RAIN_NOW = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _rain_ts(hours_ago: float) -> str:
    """Timestamps must be in the PAST: the R2-56 sanity clamp pulls
    future-dated posts to server time, which collapsed fixed 2026 dates into
    one bucket and made these assertions read a single row."""
    t = _RAIN_NOW - _dt.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_rain(client, yearly, ts):
    return client.post("/ingest/custom", headers=IH,
                       json={"device": {"id": "AABBCCDD0A10"},
                             "timestamp_utc": ts,
                             "outdoor": {"tempf": 70},
                             "rain": {"yearly_in": yearly}, "source": "t"})


def _yearlies(client):
    rows = client.get("/api/devices/AA:BB:CC:DD:0A:10/history?hours=24",
                      headers=H).json()["rows"]
    return [r.get("yearlyrainin") for r in rows]


def test_rain_level_shift_rebaselines_after_confirmation(client):
    """The lockout this exists for: removing the yearly-rain offsets moved a
    device's reported level up ~16 inches in one step. The guard rejected it —
    correctly indistinguishable from a spike on the first reading — but
    rejected rows store NULL, so the baseline never advanced and EVERY later
    reading was nulled too. Rain never recovered without hand-editing the DB.
    One confirming reading at the new level must rebaseline."""
    from app import ingest
    ingest._rain_reject.clear()
    assert _post_rain(client, 1.09, _rain_ts(8)).status_code == 200
    # +16" in 2 h (history buckets are coarse — spaced so each
    # reading lands in its own bucket): rejected (spike or shift — can't tell yet), stored NULL.
    assert _post_rain(client, 17.1, _rain_ts(6)).status_code == 200
    # Same level two hours later: confirmed shift, accepted.
    assert _post_rain(client, 17.12, _rain_ts(4)).status_code == 200
    ys = _yearlies(client)
    assert ys[-3:] == [1.09, None, 17.12], ys
    # ...and the counter keeps working from the new baseline.
    assert _post_rain(client, 17.15, _rain_ts(2)).status_code == 200
    assert _yearlies(client)[-1] == 17.15


def test_rain_one_shot_spike_still_dropped(client):
    """The behaviour the guard originally shipped for must survive the
    rebaseline: a decode glitch spikes ONCE and the next reading returns to
    the true level, so nothing confirms and nothing rebaselines."""
    from app import ingest
    ingest._rain_reject.clear()
    assert _post_rain(client, 2.00, _rain_ts(9)).status_code == 200
    assert _post_rain(client, 55.0, _rain_ts(7)).status_code == 200
    assert _post_rain(client, 2.01, _rain_ts(5)).status_code == 200
    assert _yearlies(client)[-3:] == [2.00, None, 2.01]
    # A LATER unrelated spike must not be "confirmed" by the stale rejection.
    assert _post_rain(client, 60.0, _rain_ts(3)).status_code == 200
    assert _yearlies(client)[-1] is None


def test_rain_duplicate_retry_does_not_confirm_itself(client):
    """A sender retrying the identical rejected packet (same dateutc) has a
    zero increment, which trivially passed the plausibility check — the
    duplicate confirmed its own spike. Corroboration must be a LATER reading."""
    from app import ingest
    ingest._rain_reject.clear()
    ts_spike = _rain_ts(6)
    assert _post_rain(client, 1.00, _rain_ts(8)).status_code == 200
    assert _post_rain(client, 50.0, ts_spike).status_code == 200   # rejected
    assert _post_rain(client, 50.0, ts_spike).status_code == 200   # retry, same ts
    assert _yearlies(client)[-1] is None, "duplicate retry rebaselined itself"


def test_rain_burst_duplicate_decodes_cannot_confirm(client):
    """The production hole this closes: rtl_433 decodes ONE radio
    transmission 2-3 times a few seconds apart, and a neighboring sensor on
    a colliding ID (constant 20.22 counter vs our 17.12) landed in history
    because its own duplicate decode arrived seconds later and 'confirmed'
    it. Frames inside the burst gap are the same observation, not
    corroboration."""
    from app import ingest
    ingest._rain_reject.clear()
    # Minutes apart so the +3.1 jump exceeds max_rate (2 in/hr).
    assert _post_rain(client, 17.12, _rain_ts(10 / 60)).status_code == 200
    assert _post_rain(client, 20.22, _rain_ts(5 / 60)).status_code == 200
    # Duplicate decode 3 seconds later — strictly later ts, identical value.
    assert _post_rain(client, 20.22, _rain_ts(5 / 60 - 3 / 3600)).status_code == 200
    assert "AA:BB:CC:DD:0A:10" in ingest._rain_reject, \
        "burst duplicate confirmed itself"
    ys = _yearlies(client)
    assert 20.22 not in ys and 17.12 in ys and ys[-1] is None, ys


def test_rain_alternating_second_source_never_lands(client):
    """Two transmitters posting different counters under one MAC: real
    readings between intruder sightings clear the pending rejection, so the
    intruder never strings together the two consecutive sightings a
    rebaseline requires."""
    from app import ingest
    ingest._rain_reject.clear()
    # Minutes apart, matching production: +3.1 inches over a few minutes is
    # far beyond max_rate (2 in/hr) — hours apart it would read as plausible
    # rain and never even reach the guard.
    assert _post_rain(client, 17.12, _rain_ts(30 / 60)).status_code == 200
    assert _post_rain(client, 20.22, _rain_ts(24 / 60)).status_code == 200  # intruder
    assert _post_rain(client, 17.12, _rain_ts(18 / 60)).status_code == 200  # real: clears
    assert "AA:BB:CC:DD:0A:10" not in ingest._rain_reject
    # Next intruder sighting is >90s after the first, but the pending
    # rejection is gone — it must start over, not confirm.
    assert _post_rain(client, 20.22, _rain_ts(6 / 60)).status_code == 200
    assert "AA:BB:CC:DD:0A:10" in ingest._rain_reject
    ys = _yearlies(client)
    assert 20.22 not in ys and 17.12 in ys and ys[-1] is None, ys


def test_rain_slow_cadence_level_shift_still_confirms(client):
    """The burst gap must not break genuine shifts on a fast-posting sensor:
    repeats of the SAME rejected level keep the FIRST-seen timestamp, so the
    confirmation window is measured from the first sighting — otherwise a
    sensor posting every 60s (< the confirmation gap) could never rebaseline.
    Gap-derived offsets, not literals, so the boundary pin survives the gap
    being recalibrated (90s → 300s after the 2026-08-19 collision)."""
    from app import ingest
    ingest._rain_reject.clear()
    ingest._rain_tombstone.clear()
    gap_s = ingest._RAIN_CONFIRM_MIN_GAP_MS / 1000
    assert _post_rain(client, 1.00, _rain_ts(30 / 60)).status_code == 200
    base = (gap_s + 120) / 3600  # hours ago; shift readings 60s apart from here
    assert _post_rain(client, 17.12, _rain_ts(base)).status_code == 200            # rejected
    assert _post_rain(client, 17.12, _rain_ts(base - 60 / 3600)).status_code == 200  # +60s: inside gap
    assert "AA:BB:CC:DD:0A:10" in ingest._rain_reject
    # Exact boundary (gap counts from the FIRST sighting): gap-1s still
    # pending, the gap exactly confirms — pins < vs <= in the gap check.
    assert _post_rain(client, 17.12, _rain_ts(base - (gap_s - 1) / 3600)).status_code == 200
    assert "AA:BB:CC:DD:0A:10" in ingest._rain_reject
    assert _post_rain(client, 17.12, _rain_ts(base - gap_s / 3600)).status_code == 200
    assert "AA:BB:CC:DD:0A:10" not in ingest._rain_reject, \
        "shift never confirmed — first-seen timestamp was not preserved"
    cur = client.get("/api/devices/AA:BB:CC:DD:0A:10/current", headers=H).json()
    assert cur["yearlyrainin"] == 17.12


def test_rain_reject_registry_is_bounded():
    """Every rejection adds a MAC entry, removed only when that MAC later takes
    a non-glitch path — a token holder spraying synthetic ids grew it without
    bound. Oldest-evicted at the cap; the evicted device just needs one extra
    confirming reading."""
    from app import ingest
    ingest._rain_reject.clear()
    for i in range(ingest._RAIN_REJECT_MAX + 100):
        mac = f"AA:BB:{i >> 8:02X}:{i & 255:02X}:00:01"
        ingest._record_rain_rejection(mac, 50.0, float(i))
    assert len(ingest._rain_reject) == ingest._RAIN_REJECT_MAX
    # Oldest evicted, newest kept.
    assert "AA:BB:00:00:00:01" not in ingest._rain_reject
    assert f"AA:BB:{(ingest._RAIN_REJECT_MAX + 99) >> 8:02X}:{(ingest._RAIN_REJECT_MAX + 99) & 255:02X}:00:01" in ingest._rain_reject
    ingest._rain_reject.clear()


def test_rain_out_of_order_packet_cannot_roll_rejection_back(client):
    """An older packet arriving after a rejection must not overwrite the
    pending entry with an earlier timestamp — lowering rej_ts would let a
    REPLAY of the newer spike pass the strictly-later check and self-confirm."""
    from app import ingest
    ingest._rain_reject.clear()
    assert _post_rain(client, 1.00, _rain_ts(9)).status_code == 200
    assert _post_rain(client, 50.0, _rain_ts(5)).status_code == 200   # spike, rejected
    assert _post_rain(client, 49.9, _rain_ts(7)).status_code == 200   # OLDER spike packet
    assert _post_rain(client, 50.0, _rain_ts(5)).status_code == 200   # replay of first
    # History alone can't witness this: the write-throttle merges a same-
    # timestamp replay into the existing (NULLed) row, so assert the guard's
    # own state — confirmation POPS the entry; a still-pending entry proves
    # the replay did not vouch for itself.
    assert "AA:BB:CC:DD:0A:10" in ingest._rain_reject, \
        "replayed spike self-confirmed (rejection was popped)"
    assert _yearlies(client)[-1] is None


def test_rain_future_clamped_retries_cannot_self_confirm(client):
    """The future-skew clamp rewrites dateutc to server 'now', so two retries
    of the SAME broken-clock packet used to get fresh, increasing clamped
    timestamps — the second 'confirmed' the first. Confirmation must key on
    the device's claimed time, which a retry repeats verbatim."""
    from app import ingest
    ingest._rain_reject.clear()
    assert _post_rain(client, 1.00, _rain_ts(8)).status_code == 200
    # Generated at test time so it stays future-dated forever — a fixed
    # "2030-01-01" would silently stop exercising the clamp in 2030.
    future = (datetime.now(timezone.utc)
              + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _post_rain(client, 50.0, future).status_code == 200
    assert _post_rain(client, 50.0, future).status_code == 200   # identical retry
    assert "AA:BB:CC:DD:0A:10" in ingest._rain_reject, \
        "clamped retry self-confirmed (rejection was popped)"
    assert _yearlies(client)[-1] is None
