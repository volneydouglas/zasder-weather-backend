"""Pure-function tests — no DB, no fixtures, no env required.

These cover the parsing + flattening helpers that turn a Wunderground-format
hub query string (or a normalized JSON payload) into the flat shape the
backend stores."""
from __future__ import annotations

import os

# These modules don't read env at import time, so we can import them directly.
os.environ.setdefault("API_TOKEN", "test-api-token")
from app import ingest


# ───────────────────────── _format_mac ─────────────────────────

def test_format_mac_uppercase_colonized():
    assert ingest._format_mac("24c86e0a66f5") == "24:C8:6E:0A:66:F5"

def test_format_mac_already_colonized_passthrough():
    assert ingest._format_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"

def test_format_mac_non_mac_passthrough():
    # Custom IDs that aren't 12-hex are kept verbatim
    assert ingest._format_mac("ecowitt-gw1100-XYZ") == "ecowitt-gw1100-XYZ"

def test_format_mac_empty_returns_empty():
    assert ingest._format_mac("") == ""


# ───────────────────────── _flatten ─────────────────────────

def _payload(**outdoor):
    return {
        "device": {"id": "AA:BB:CC:DD:EE:FF"},
        "timestamp_utc": "2026-05-14T01:09:47",
        "outdoor": outdoor,
        "wind": {},
        "rain": {},
        "pressure": {},
        "source": "acurite-atlas",
    }

def test_flatten_maps_outdoor_fields():
    flat = ingest._flatten(_payload(tempf=98.3, humidity=8, dew_point_f=27))
    assert flat["tempf"] == 98.3
    assert flat["humidity"] == 8
    assert flat["dewPoint"] == 27
    assert flat["dateutc"] == 1778720987000  # 2026-05-14T01:09:47Z in ms

def test_flatten_handles_z_suffix_iso():
    flat = ingest._flatten({**_payload(tempf=70), "timestamp_utc": "2026-05-14T01:09:47Z"})
    assert flat["dateutc"] == 1778720987000  # same instant

def test_flatten_returns_none_on_missing_timestamp():
    p = _payload(tempf=70); p.pop("timestamp_utc")
    assert ingest._flatten(p) is None

def test_flatten_returns_none_on_garbage_timestamp():
    p = _payload(tempf=70); p["timestamp_utc"] = "not a date"
    assert ingest._flatten(p) is None


# ───────────────────── _device_label / _auto_device_name ─────────────────────

def test_device_label_returns_explicit_name():
    """When device.name is in the POST, _device_label echoes it as the
    explicit name (overrides any auto-derived fallback in upsert)."""
    name, loc = ingest._device_label({"device": {"name": "Backyard", "location": "Phoenix"}})
    assert name == "Backyard"
    assert loc == "Phoenix"

def test_device_label_returns_none_when_name_absent():
    """No explicit device.name → _device_label returns None so the
    UPSERT preserves whatever name's already in the row (a secondary
    source shouldn't flip the friendly name set by the primary)."""
    name, loc = ingest._device_label({"device": {}, "source": "acurite-atlas"})
    assert name is None
    assert loc is None

def test_auto_device_name_pretty_for_known_source():
    """The auto-derived name is the fallback used only on first INSERT."""
    name = ingest._auto_device_name({"device": {}, "source": "acurite-atlas"})
    assert name == "AcuRite Atlas"

def test_auto_device_name_includes_model_when_distinct():
    name = ingest._auto_device_name({"device": {"model": "Iris"}, "source": "acurite-atlas"})
    assert name == "AcuRite Atlas (Iris)"


# ───────────────────── ingest_yearly_rain_offsets parsing ─────────────────────
# Reviewer-noted edge cases on the offset env: lowercase MAC keys parsed as
# dict (not JSON string), compact 12-hex form, and bad numeric values.

def test_offset_validator_uppercases_dict_input():
    from app.config import Settings
    s = Settings(ingest_yearly_rain_offsets={"5d:5d:01:00:02:c7": 2.85})
    assert s.ingest_yearly_rain_offsets == {"5D:5D:01:00:02:C7": 2.85}

def test_offset_validator_uppercases_json_string_input():
    from app.config import Settings
    s = Settings(ingest_yearly_rain_offsets='{"5d:5d:01:00:02:c7":2.85}')
    assert s.ingest_yearly_rain_offsets == {"5D:5D:01:00:02:C7": 2.85}

def test_offset_validator_colonizes_compact_mac():
    from app.config import Settings
    s = Settings(ingest_yearly_rain_offsets={"5D5D010002C7": 2.85})
    assert s.ingest_yearly_rain_offsets == {"5D:5D:01:00:02:C7": 2.85}

def test_offset_validator_drops_nonnumeric_offset():
    from app.config import Settings
    s = Settings(ingest_yearly_rain_offsets={"5D:5D:01:00:02:C7": "not-a-number"})
    assert s.ingest_yearly_rain_offsets == {}

def test_offset_validator_empty_string_is_empty_dict():
    from app.config import Settings
    s = Settings(ingest_yearly_rain_offsets="")
    assert s.ingest_yearly_rain_offsets == {}


# ───────────────────── _flatten yearly_in coercion ─────────────────────
# Reviewer-noted: float(yearly_in) on "abc" raised an unhandled exception
# when an offset was configured. Must coerce to None instead.

def test_flatten_yearly_in_nonnumeric_string_becomes_none():
    payload = {
        "device":        {"id": "5D5D010002C7"},
        "timestamp_utc": "2026-05-24T07:40:15Z",
        "rain":          {"yearly_in": "abc"},
    }
    out = ingest._flatten(payload)
    assert out is not None
    assert out["yearlyrainin"] is None

def test_flatten_yearly_in_numeric_string_parsed():
    payload = {
        "device":        {"id": "5D5D010002C7"},
        "timestamp_utc": "2026-05-24T07:40:15Z",
        "rain":          {"yearly_in": "3.58"},
    }
    out = ingest._flatten(payload)
    assert out is not None
    assert out["yearlyrainin"] == 3.58


# ───────────────────── staleness-alert decision logic ─────────────────────
# Pure transition logic in app.alerts.decide — no DB, no SMTP.

from app import alerts  # noqa: E402

_MIN = 60_000          # 1 minute in ms
_THRESH = 10 * _MIN    # 10-minute staleness threshold

def test_decide_baselines_on_first_sight_no_alert():
    # First time we see a device (prior=None) we record state but never alert,
    # so a device that was already dead at startup doesn't trigger.
    d = alerts.decide(None, last_seen_ms=0, now_ms=100 * _MIN, threshold_ms=_THRESH, repeat_ms=0)
    assert d.state == "stale" and d.event is None

def test_decide_ok_to_stale_fires():
    prior = {"state": "ok", "changed_ms": 0, "notified_ms": None}
    now = 100 * _MIN
    d = alerts.decide(prior, last_seen_ms=now - 11 * _MIN, now_ms=now, threshold_ms=_THRESH, repeat_ms=0)
    assert d.state == "stale" and d.event == "stale" and d.changed_ms == now

def test_decide_stale_to_ok_recovers():
    prior = {"state": "stale", "changed_ms": 50 * _MIN, "notified_ms": 50 * _MIN}
    now = 100 * _MIN
    d = alerts.decide(prior, last_seen_ms=now - 1 * _MIN, now_ms=now, threshold_ms=_THRESH, repeat_ms=0)
    assert d.state == "ok" and d.event == "recovered"

def test_decide_stable_ok_no_event():
    prior = {"state": "ok", "changed_ms": 0, "notified_ms": None}
    now = 100 * _MIN
    d = alerts.decide(prior, last_seen_ms=now - 2 * _MIN, now_ms=now, threshold_ms=_THRESH, repeat_ms=0)
    assert d.state == "ok" and d.event is None

def test_decide_no_repeat_when_disabled():
    prior = {"state": "stale", "changed_ms": 0, "notified_ms": 0}
    now = 100 * _MIN
    d = alerts.decide(prior, last_seen_ms=0, now_ms=now, threshold_ms=_THRESH, repeat_ms=0)
    assert d.event is None

def test_decide_repeat_after_interval():
    prior = {"state": "stale", "changed_ms": 0, "notified_ms": 0}
    now = 100 * _MIN
    d = alerts.decide(prior, last_seen_ms=0, now_ms=now, threshold_ms=_THRESH, repeat_ms=60 * _MIN)
    assert d.event == "repeat" and d.state == "stale"

def test_build_alert_stale_subject_and_body():
    subj, body = alerts.build_alert("stale", "Crestview (SDR)", "5D:5D:02:00:00:7D",
                                    last_seen_ms=0, now_ms=11 * _MIN, threshold_min=10,
                                    tz_name="America/Phoenix")
    assert "not reporting" in subj and "Crestview (SDR)" in subj
    assert "5D:5D:02:00:00:7D" in body and "threshold 10 min" in body

def test_build_alert_recovered_subject():
    subj, _ = alerts.build_alert("recovered", "Crestview (SDR)", "5D:5D:02:00:00:7D",
                                 last_seen_ms=11 * _MIN, now_ms=12 * _MIN, threshold_min=10,
                                 tz_name="UTC")
    assert "reporting again" in subj


# ───────────────────── alert threshold env parsing ─────────────────────

def test_alert_threshold_map_normalizes_and_drops_bad():
    from app.config import Settings
    s = Settings(alert_stale_minutes_by_mac={"5d5d0200007d": 10, "C8:C9:A3:55:85:62": "nope"})
    assert s.alert_stale_minutes_by_mac == {"5D:5D:02:00:00:7D": 10.0}


# ───────────────────── feels-like derivation ─────────────────────
# SDR/custom sources post raw temp but no feels_like; the backend derives it.

def test_feels_like_matches_awn_heat_index():
    # AWN reported 95.09 for 99.3F / 15% RH — raw Rothfusz regression.
    assert ingest._compute_feels_like(99.3, 15, 0.89) == 95.09

def test_feels_like_wind_chill_when_cold_and_windy():
    fl = ingest._compute_feels_like(20.0, 50, 15.0)
    assert fl is not None and fl < 20.0          # wind chill below air temp

def test_feels_like_neutral_returns_air_temp():
    assert ingest._compute_feels_like(65.0, 40, 2.0) == 65.0

def test_feels_like_none_when_temp_unknown():
    assert ingest._compute_feels_like(None, 50, 5.0) is None

def test_flatten_derives_feelslike_for_sdr_without_it():
    # SDR-style payload: temp + humidity, no feels_like provided.
    payload = {
        "device": {"id": "5D5D020000 7D".replace(" ", "")},
        "timestamp_utc": "2026-05-25T07:40:15Z",
        "outdoor": {"tempf": 99.3, "humidity": 15},
        "wind": {"speed_mph": 0.89},
    }
    out = ingest._flatten(payload)
    assert out is not None and out["feelsLike"] == 95.09

def test_flatten_passes_through_provided_feelslike():
    payload = {
        "device": {"id": "AABBCCDDEEFF"},
        "timestamp_utc": "2026-05-25T07:40:15Z",
        "outdoor": {"tempf": 99.3, "humidity": 15, "feels_like": 88.0},
    }
    out = ingest._flatten(payload)
    assert out is not None and out["feelsLike"] == 88.0


# ───────────────────── rain-glitch detection ─────────────────────
# Cumulative yearly-rain spikes from SDR decode errors get rejected.

def test_rain_glitch_flags_impossible_spike():
    # +6 in over 1 minute — no real rain does that.
    assert ingest._is_rain_glitch(6.0, 60 / 3600, 2.0) is True

def test_rain_glitch_allows_normal_increase():
    # +0.05 in over a minute — plausible heavy rain.
    assert ingest._is_rain_glitch(0.05, 60 / 3600, 2.0) is False

def test_rain_glitch_allows_accumulation_over_a_gap():
    # 1.5 in over a 1-hour data gap is within 2 in/hr + floor.
    assert ingest._is_rain_glitch(1.5, 1.0, 2.0) is False

def test_rain_glitch_ignores_decrease():
    # Counter reset / negative delta isn't a "spike".
    assert ingest._is_rain_glitch(-3.0, 0.02, 2.0) is False

def test_rain_glitch_disabled_when_rate_zero():
    assert ingest._is_rain_glitch(99.0, 0.01, 0.0) is False


def test_reviewer_token_must_meet_length_floor():
    # [P3] reviewer_api_token is accepted on /api/*, so a short one is a
    # guessable backdoor — it must meet the same 32-char floor.
    import pytest
    from app.config import Settings
    with pytest.raises(Exception):
        Settings(api_token="a" * 32, reviewer_api_token="123")


# ───────────────────── APNs push helpers ─────────────────────
from app import apns  # noqa: E402

def test_apns_build_payload_shape():
    assert apns.build_payload("Title", "Body") == {
        "aps": {"alert": {"title": "Title", "body": "Body"}, "sound": "default"}}

def test_apns_make_jwt_structure():
    import jwt as _jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    tok = apns.make_jwt("TEAMID1234", "KEYID5678", pem, now=1000)
    assert _jwt.get_unverified_header(tok)["kid"] == "KEYID5678"
    claims = _jwt.decode(tok, options={"verify_signature": False})
    assert claims["iss"] == "TEAMID1234" and claims["iat"] == 1000

def test_build_push_offline_and_recovered():
    now = 100 * 60_000
    t, b = alerts.build_push("stale", "Crestview (SDR)", now - 11 * 60_000, now, 10)
    assert "offline" in t and "No data" in b
    t2, _ = alerts.build_push("recovered", "Crestview (SDR)", now - 1 * 60_000, now, 10)
    assert "back online" in t2


# ───────────────────── threshold rules ─────────────────────
def test_evaluate_rule_above_fires_once_then_rearms():
    assert alerts.evaluate_rule("above", 100.0, 102.0, 0) == (True, True)   # clear→trigger fires
    assert alerts.evaluate_rule("above", 100.0, 103.0, 1) == (True, False)  # stays triggered, no re-fire
    assert alerts.evaluate_rule("above", 100.0, 98.0, 1) == (False, False)  # clears → re-arms

def test_evaluate_rule_below_and_equal():
    assert alerts.evaluate_rule("below", 32.0, 30.0, 0) == (True, True)
    assert alerts.evaluate_rule("below", 32.0, 40.0, 0) == (False, False)
    assert alerts.evaluate_rule("equalTo", 50.0, 50.3, 0) == (True, True)   # within tolerance
    assert alerts.evaluate_rule("equalTo", 50.0, 51.0, 0) == (False, False)

def test_build_threshold_message():
    title, body = alerts.build_threshold_message("Crestview", "tempf", 102.3, "above", 100)
    assert "Crestview" in title and "Temperature" in title
    assert "102.3°F" in body and "> 100°F" in body
