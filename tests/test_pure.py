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

def test_flatten_wind_dir_deg_fallback():
    # The WLL poller posts wind.dir_deg (docs say wind.direction); reading
    # only "direction" silently dropped Davis wind direction for weeks.
    p = _payload(tempf=70)
    p["wind"] = {"speed_mph": 4.7, "dir_deg": 247}
    flat = ingest._flatten(p)
    assert flat["winddir"] == 247

def test_flatten_wind_direction_wins_and_zero_survives():
    p = _payload(tempf=70)
    p["wind"] = {"direction": 0, "dir_deg": 90}   # 0° = north, must not fall through
    assert ingest._flatten(p)["winddir"] == 0

def test_flatten_maps_solar_block():
    # WLL local poller posts a dedicated solar block (not outdoor.solar_wm2);
    # it used to be dropped entirely so Davis-via-WLL never stored solar.
    p = _payload(tempf=70)
    p["solar"] = {"radiation_wm2": 815, "uv": 5}
    flat = ingest._flatten(p)
    assert flat["solarradiation"] == 815
    assert flat["uv"] == 5

def test_flatten_solar_block_zero_not_dropped():
    p = _payload(tempf=70)
    p["solar"] = {"radiation_wm2": 0, "uv": 0}
    flat = ingest._flatten(p)
    assert flat["solarradiation"] == 0
    assert flat["uv"] == 0

def test_flatten_outdoor_solar_wins_over_block():
    p = _payload(tempf=70, solar_wm2=400, uv=3)
    p["solar"] = {"radiation_wm2": 815, "uv": 5}
    flat = ingest._flatten(p)
    assert flat["solarradiation"] == 400
    assert flat["uv"] == 3


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
    # guessable backdoor — it must meet the same 32-char floor. Pinned to
    # ValidationError NAMING the field (R3-121): a bare raises(Exception)
    # passed for any unrelated failure inside Settings(...).
    import pytest
    from pydantic import ValidationError
    from app.config import Settings
    with pytest.raises(ValidationError, match="reviewer_api_token"):
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
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    tok = apns.make_jwt("TEAMID1234", "KEYID5678", pem, now=1000)
    assert _jwt.get_unverified_header(tok)["kid"] == "KEYID5678"
    # VERIFY the ES256 signature (R3-141): with verify_signature=False a
    # broken signing call emitting a well-formed-but-unsigned token passed.
    claims = _jwt.decode(tok, key.public_key(), algorithms=["ES256"])
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


# ── rain-chart derivation from cumulative yearlyrainin (db._derive_hourly_rain) ──
from app import db as _db  # noqa: E402


def test_derive_hourly_rain_from_yearly_bump():
    """A 0.01" cumulative bump becomes a trailing-1h rainfall the chart can
    show, for SDR sources that only post yearlyrainin (hourlyrainin=None)."""
    H = 3_600_000
    t0 = 1_700_000_000_000
    rows = [
        {"dateutc": t0 + 0 * H, "yearlyrainin": 0.73, "hourlyrainin": None},
        {"dateutc": t0 + 1 * H, "yearlyrainin": 0.73, "hourlyrainin": None},
        {"dateutc": t0 + 2 * H, "yearlyrainin": 0.74, "hourlyrainin": None},  # +0.01 fell
        {"dateutc": t0 + 3 * H, "yearlyrainin": 0.74, "hourlyrainin": None},  # ages out next hr
    ]
    _db._derive_hourly_rain(rows)
    assert rows[2]["hourlyrainin"] == 0.01   # rain shows during the event hour
    assert rows[3]["hourlyrainin"] == 0.0    # trailing hour has moved past it
    assert rows[0]["hourlyrainin"] == 0.0


def test_derive_hourly_rain_preserves_real_values():
    """Rows that already carry a real hourlyrainin (AmbientWeather) untouched."""
    rows = [{"dateutc": 1_700_000_000_000, "yearlyrainin": 5.0, "hourlyrainin": 0.2}]
    _db._derive_hourly_rain(rows)
    assert rows[0]["hourlyrainin"] == 0.2


def test_derive_hourly_rain_clamps_counter_reset():
    """A yearlyrainin drop (year rollover / recalibration) clamps to 0, not
    a negative rainfall."""
    H = 3_600_000
    t0 = 1_700_000_000_000
    rows = [
        {"dateutc": t0, "yearlyrainin": 9.0, "hourlyrainin": None},
        {"dateutc": t0 + 2 * H, "yearlyrainin": 0.0, "hourlyrainin": None},  # reset
    ]
    _db._derive_hourly_rain(rows)
    assert rows[1]["hourlyrainin"] == 0.0


# ── update checker version compare (app/updates.py) ──
from app import updates as _updates  # noqa: E402


def test_parse_version_variants():
    assert _updates.parse_version("v1.2.3") == (1, 2, 3)
    assert _updates.parse_version("1.2.3") == (1, 2, 3)
    assert _updates.parse_version("1.2.3-rc1") == (1, 2, 3)
    assert _updates.parse_version("2.0") == (2, 0)
    assert _updates.parse_version("garbage") == (0,)


def test_is_newer():
    assert _updates.is_newer("1.1.0", "1.0.0") is True
    assert _updates.is_newer("1.0.1", "1.0.0") is True
    assert _updates.is_newer("2.0.0", "1.9.9") is True
    assert _updates.is_newer("1.0.0", "1.0.0") is False
    assert _updates.is_newer("0.9.0", "1.0.0") is False
    assert _updates.is_newer("v1.2.0", "1.1.0") is True


# ── public dashboard renderer (app/public_dashboard.py) ──
from app import public_dashboard as _pdash  # noqa: E402


def test_public_dashboard_svg_chart():
    svg = _pdash.svg_chart([(0, 70.0), (1000, 72.0), (2000, 71.0)], "#ff9e33")
    assert "<svg" in svg and "polyline" in svg and "#ff9e33" in svg


def test_public_dashboard_svg_empty():
    assert "no data" in _pdash.svg_chart([], "#fff")
    assert "no data" in _pdash.svg_chart([(0, 5.0)], "#fff")  # <2 points


def test_public_dashboard_resolve_fields():
    assert _pdash.resolve_fields(None) == _pdash.CORE_FIELDS
    assert _pdash.resolve_fields("tempf,humidity") == ["tempf", "humidity"]
    assert _pdash.resolve_fields("bogus,tempf") == ["tempf"]
    assert _pdash.resolve_fields("nonsense") == _pdash.CORE_FIELDS  # falls back


def test_public_dashboard_render_station():
    html = _pdash.render_station(
        "Davis Vantage Pro 2",
        {"tempf": 91.4, "feelsLike": 95.0, "humidity": 46},
        {"tempf": [(0, 90.0), (1, 91.4)], "humidity": [(0, 45.0), (1, 46.0)]},
        ["tempf", "humidity"],
    )
    assert "Davis Vantage Pro 2" in html
    assert "91°" in html                 # hero temp rounded
    assert "feels 95°" in html
    assert "Temperature" in html and "Humidity" in html


def test_public_dashboard_svg_chart_feels_overlay():
    """Temp chart overlays a second (feels-like) dashed line + legend."""
    svg = _pdash.svg_chart(
        [(0, 88.0), (1000, 90.0), (2000, 91.0)], "#ff9e33",
        overlay=[(0, 92.0), (1000, 95.0), (2000, 97.0)],
        overlay_color="#ff5a5f", primary_label="Temp", overlay_label="Feels like",
    )
    assert svg.count("polyline") == 2        # temp + feels lines
    assert "stroke-dasharray" in svg         # overlay is dashed
    assert "Feels like" in svg and "chart-legend" in svg
    # Axis spans BOTH series (feels hits 97, temp bottoms at 88).
    assert "97" in svg and "88" in svg


def test_public_dashboard_wind_rose():
    # Wind mostly from the SW at a spread of speeds → a real rose.
    samples = [(225.0, 3.0), (225.0, 8.0), (230.0, 12.0), (220.0, 18.0),
               (240.0, 22.0), (200.0, 6.0), (270.0, 4.0), (180.0, 9.0)]
    svg = _pdash.svg_wind_rose(samples)
    assert "<svg" in svg and "rose-svg" in svg
    assert "<path" in svg                    # at least one petal drawn
    assert ">N<" in svg and ">S<" in svg     # cardinal labels
    assert "rose-legend" in svg and "mph" in svg


def test_public_dashboard_wind_rose_empty():
    assert "no wind data" in _pdash.svg_wind_rose([])
    assert "no wind data" in _pdash.svg_wind_rose([(90.0, 5.0), (90.0, 6.0)])  # <3


def test_public_dashboard_render_station_wind_rose_tile():
    html = _pdash.render_station(
        "Davis", {"tempf": 90.0, "windspeedmph": 5},
        {"windspeedmph": [(0, 4.0), (1, 6.0), (2, 5.0)],
         "tempf": [(0, 89.0), (1, 90.0)]},
        ["tempf", "windspeedmph"],
        wind_samples=[(225.0, 4.0), (230.0, 6.0), (220.0, 5.0)],
    )
    assert "Wind rose" in html and "rose-svg" in html


def test_public_dashboard_records_strip():
    recs = {"periods": {"all": {"fields": {
        "tempf": {"max": 116.2, "min": 38.0, "maxAt": 1751500000000, "minAt": 1736000000000},
        "feelsLike": {"max": 121.0, "min": 33.0, "maxAt": 1751500000000, "minAt": 1736000000000},
        "dewPoint": {"max": 74.0, "min": 5.0, "maxAt": 1751500000000, "minAt": 1736000000000},
        "windgustmph": {"max": 41.0, "maxAt": 1751600000000},
    }}}}
    html = _pdash.render_records(recs, "America/Phoenix")
    assert "Records" in html and "Hottest" in html and "Coldest" in html
    assert "116" in html and "Peak gust" in html and "41" in html
    # feels-like + dew point records
    assert "Hottest feels" in html and "121" in html
    assert "Highest dew pt" in html and "74" in html and "Lowest dew pt" in html

def test_public_dashboard_records_empty():
    assert _pdash.render_records(None, "UTC") == ""
    assert _pdash.render_records({"periods": {}}, "UTC") == ""


# ───────────────────── smart (derived) alert logic ─────────────────────

def test_smart_condition_frost():
    sc = alerts.smart_condition
    kw = dict(frost_f=35.0, heat_f=105.0, drop_inhg=0.06)
    assert sc("frost", tempf=32.0, **kw) is True
    assert sc("frost", tempf=35.0, **kw) is True     # at threshold
    assert sc("frost", tempf=40.0, **kw) is False
    assert sc("frost", tempf=None, **kw) is False    # no reading → no alert

def test_smart_condition_heat():
    sc = alerts.smart_condition
    kw = dict(frost_f=35.0, heat_f=105.0, drop_inhg=0.06)
    assert sc("heat", feels=108.0, **kw) is True
    assert sc("heat", feels=100.0, **kw) is False

def test_smart_condition_pressure_drop():
    sc = alerts.smart_condition
    kw = dict(frost_f=35.0, heat_f=105.0, drop_inhg=0.06)
    assert sc("pressure_drop", pressure_delta_3h=-0.10, **kw) is True   # fell 0.10
    assert sc("pressure_drop", pressure_delta_3h=-0.03, **kw) is False  # small dip
    assert sc("pressure_drop", pressure_delta_3h=+0.20, **kw) is False  # rising
    assert sc("pressure_drop", pressure_delta_3h=None, **kw) is False

def test_build_smart_message():
    t, b = alerts.build_smart_message("frost", "Davis", tempf=31.0)
    assert "Frost" in t and "31" in b
    t, b = alerts.build_smart_message("heat", "Davis", feels=110.0)
    assert "heat" in t.lower() and "110" in b
    t, b = alerts.build_smart_message("pressure_drop", "Davis", pressure_delta_3h=-0.12)
    assert "Pressure" in t and "0.12" in b


# ───────────────────── Prometheus /metrics rendering ─────────────────────

from app import metrics as _metrics  # noqa: E402

def test_prometheus_render():
    devices = [{"mac": "AA:BB:CC:DD:EE:FF", "name": 'Davis "Backyard"',
                "lastSeen": 1000, "lastData": {"tempf": 88.1, "humidity": 40,
                                               "baromrelin": 29.92}}]
    out = _metrics.render_prometheus(devices, now_ms=6000)
    assert "# TYPE zasder_temperature_fahrenheit gauge" in out
    # MAC is masked to its last 2 bytes (see _mask_mac) — /metrics is open when
    # enabled, matching the status page's privacy posture.
    assert 'zasder_temperature_fahrenheit{mac="··:··:··:··:EE:FF",name="Davis \\"Backyard\\""} 88.1' in out
    assert "AA:BB:CC:DD:EE:FF" not in out
    assert "zasder_humidity_percent" in out
    assert "zasder_device_last_seen_seconds" in out and "} 5" in out  # (6000-1000)/1000

def test_prometheus_skips_missing_and_nan():
    devices = [{"mac": "M", "name": "N", "lastSeen": None,
                "lastData": {"tempf": None, "uv": float("nan")}}]
    out = _metrics.render_prometheus(devices, now_ms=1000)
    assert "zasder_temperature" not in out   # None skipped
    assert "zasder_uv_index" not in out       # NaN skipped
    assert "last_seen" not in out             # no lastSeen


# ───────────────────── MQTT / Home Assistant discovery ─────────────────────

from app import mqtt_publish as _mq  # noqa: E402

def test_mqtt_discovery_messages():
    dev = {"mac": "AA:BB:CC:DD:EE:FF", "name": "Davis",
           "lastData": {"tempf": 88.0}}
    msgs = dict(_mq.discovery_messages(dev, "zasder", "homeassistant"))
    topic = "homeassistant/sensor/zasder_aabbccddeeff/tempf/config"
    assert topic in msgs
    cfg = msgs[topic]
    assert cfg["unique_id"] == "zasder_aabbccddeeff_tempf"
    assert cfg["state_topic"] == "zasder/aabbccddeeff/state"
    assert cfg["device_class"] == "temperature" and cfg["unit_of_measurement"] == "°F"
    assert cfg["device"]["identifiers"] == ["zasder_aabbccddeeff"]
    assert cfg["value_template"] == "{{ value_json.tempf }}"

def test_mqtt_state_message_only_present_fields():
    dev = {"mac": "AA:BB:CC:DD:EE:FF", "name": "Davis",
           "lastData": {"tempf": 88.0, "humidity": 40, "uv": None}}
    topic, payload = _mq.state_message(dev, "zasder")
    assert topic == "zasder/aabbccddeeff/state"
    assert payload == {"tempf": 88.0, "humidity": 40}   # None dropped


# ───────────────────── wind-gust glitch guard ─────────────────────

def test_is_gust_glitch():
    g = ingest._is_gust_glitch
    # The reported case: 58 mph gust with 4.56 mph sustained → glitch.
    assert g(58.0, 4.56, min_mph=30.0, max_factor=4.0) is True
    # Real breezy day: 30 gust / 9 sustained (factor 3.3) → kept.
    assert g(30.0, 9.0, min_mph=30.0, max_factor=4.0) is False
    # Below the floor is never flagged, even at a high ratio.
    assert g(20.0, 1.0, min_mph=30.0, max_factor=4.0) is False
    # Unknown sustained speed → can't judge → kept.
    assert g(58.0, None, min_mph=30.0, max_factor=4.0) is False
    # Disabled (factor 0) → never flags.
    assert g(58.0, 4.0, min_mph=30.0, max_factor=0.0) is False
    assert g(None, 4.0, min_mph=30.0, max_factor=4.0) is False


def test_is_gust_glitch_zero_sustained_is_not_a_glitch():
    """A squall front hitting a calm station reads 0 sustained with a real high
    gust. Multiplying by zero used to discard every gust above the floor —
    precisely when gusts matter most."""
    g = ingest._is_gust_glitch
    assert g(45.0, 0.0, min_mph=30.0, max_factor=4.0) is False   # real squall gust
    assert g(58.0, 4.56, min_mph=30.0, max_factor=4.0) is True   # still a glitch


# ───────────── AWN client must never leak credentials into errors ─────────────

def test_ambient_client_error_has_no_credentials():
    """AWN takes the keys as QUERY PARAMS and httpx's HTTPStatusError embeds the
    full URL — log.exception on it wrote both secrets to the logs on every
    401/429/5xx. The client must raise a scrubbed error instead."""
    import asyncio
    import httpx
    from app.ambient_client import AmbientWeatherClient, AmbientWeatherError

    APP, API = "SECRET_APP_KEY_123", "SECRET_API_KEY_456"

    def handler(request: httpx.Request) -> httpx.Response:
        assert APP in str(request.url)          # keys really are on the wire
        return httpx.Response(429, text="rate limited")

    c = AmbientWeatherClient(APP, API, min_interval=0.0)
    c._client = httpx.AsyncClient(base_url="https://rt.ambientweather.net/v1",
                                  transport=httpx.MockTransport(handler))
    try:
        asyncio.run(c.list_devices())
        raise AssertionError("expected an error")
    except AmbientWeatherError as e:
        msg = str(e)
        assert APP not in msg and API not in msg, f"credentials leaked: {msg}"
        assert "429" in msg and "/devices" in msg      # still useful for debugging


def test_wind_rose_survives_non_finite_samples():
    """A NaN/inf wind direction reaching int() 500s the public status page for
    anonymous visitors (cloud pollers write lastData through unscrubbed)."""
    bad = [(float("nan"), 5.0), (float("inf"), 6.0), (225.0, float("nan"))]
    assert "no wind data" in _pdash.svg_wind_rose(bad)      # all filtered, no crash
    ok = bad + [(225.0, 4.0), (230.0, 6.0), (220.0, 5.0)]
    assert "<svg" in _pdash.svg_wind_rose(ok)               # good samples still render

def test_num_rejects_infinity():
    assert _pdash._num(float("inf")) is None
    assert _pdash._num(float("nan")) is None
    assert _pdash._num("12.5") == 12.5


def test_metrics_num_rejects_non_finite():
    """metrics.py has its OWN _num; Infinity there emits `inf`, which is not a
    valid Prometheus sample and breaks the whole scrape."""
    assert _metrics._num(float("inf")) is None
    assert _metrics._num(float("-inf")) is None
    assert _metrics._num(float("nan")) is None
    assert _metrics._num("29.92") == 29.92

def test_metrics_render_skips_non_finite_values():
    devices = [{"mac": "AA:BB:CC:DD:EE:FF", "name": "D", "lastSeen": 1000,
                "lastData": {"tempf": float("inf"), "humidity": 40}}]
    out = _metrics.render_prometheus(devices, now_ms=2000)
    assert "inf" not in out                      # no invalid sample emitted
    assert "zasder_humidity_percent" in out      # good values still exported

def test_public_dashboard_fmt_keeps_units():
    """_fmt output is all the reader sees (no separate unit element), so a
    temperature must not render bare next to '30.04 inHg'."""
    assert _pdash._fmt(115.3, "°F") == "115°F"
    assert _pdash._fmt(26.0, "%") == "26%"
    assert _pdash._fmt(4.6, "mph") == "5 mph"
    assert _pdash._fmt(29.92, "inHg") == "29.92 inHg"
    assert _pdash._fmt(66.0, "") == "66.00"      # chart axis label stays bare
    assert _pdash._fmt(None, "°F") == "—"


# ───────────────── _flatten offset-bearing timestamps (R2-25) ─────────────────

def test_flatten_converts_numeric_offset_to_utc():
    """An offset-bearing ISO timestamp ("+02:00") parses AWARE; the fix
    converts it with .astimezone(utc). A regression to .replace(tzinfo=utc)
    would re-LABEL the wall clock instead of converting — a silent 2-hour
    stored-timestamp error the naive/Z-suffix tests can't catch."""
    flat = ingest._flatten({**_payload(tempf=70),
                            "timestamp_utc": "2026-05-14T03:09:47+02:00"})
    # Same instant as 2026-05-14T01:09:47Z.
    assert flat["dateutc"] == 1778720987000


# ───────────────── tokens_match truth table + token validators (R2-162) ─────────────────
# tokens_match sits at EVERY auth gate; a regression here is an auth bypass.

def test_tokens_match_truth_table():
    from app.config import tokens_match
    # Empty/None on either side must never authenticate.
    assert tokens_match("", "secret") is False
    assert tokens_match("secret", None) is False
    assert tokens_match("secret", "") is False
    assert tokens_match("", "") is False
    # str vs str.
    assert tokens_match("secret", "secret") is True
    assert tokens_match("secret", "other") is False
    # Iterable membership (the valid_api_tokens set shape).
    assert tokens_match("secret", ("other", "secret")) is True
    assert tokens_match("secret", ("a", "b")) is False
    # Empty-string candidates in the set are skipped, not matched.
    assert tokens_match("secret", ("", "secret")) is True
    # Deliberate no-early-exit: a match FOLLOWED by non-matches still wins
    # (the loop checks every candidate; result must not be clobbered).
    assert tokens_match("secret", ("secret", "zzz")) is True


def test_token_validator_rejects_placeholders():
    """A deploy that never edited .env.example must refuse to start, not run
    live with 'change-me' as the API credential."""
    import pytest
    from pydantic import ValidationError
    from app.config import Settings
    for bad in ("change-me", "generate-a-long-random-string",
                "replace-with-anything-here", "your-token-here"):
        with pytest.raises(ValidationError):
            Settings(api_token=bad)
    with pytest.raises(ValidationError):
        Settings(api_token="a" * 32, ingest_token="Change-Me")   # case-insensitive
    # A LONG placeholder (>= 32 chars) isolates the placeholder check from the
    # length floor — without this case, deleting the placeholder branch passes
    # because every short placeholder also trips the length check.
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(api_token="replace-with-your-own-long-random-token")


def test_token_validator_enforces_length_floor_with_test_exemption():
    import pytest
    from pydantic import ValidationError
    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(api_token="a" * 31)                 # 31 chars: too short
    assert Settings(api_token="a" * 32).api_token == "a" * 32
    with pytest.raises(ValidationError):
        Settings(api_token="a" * 32, ingest_token="b" * 20)
    # `test-` prefix is exempt so unit tests keep readable tokens.
    assert Settings(api_token="test-short").api_token == "test-short"


def test_token_validator_rejects_identical_api_and_ingest_tokens():
    """api_token == ingest_token would make revoking one revoke both —
    the model validator must refuse the config outright."""
    import pytest
    from pydantic import ValidationError
    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(api_token="a" * 32, ingest_token="a" * 32)
    s = Settings(api_token="a" * 32, ingest_token="b" * 32)
    assert s.ingest_token == "b" * 32


def test_token_validator_blank_handling():
    """Blank secondary tokens coerce to None (feature off); a blank api_token
    must fail loudly at boot — it used to start the app fine and then 401
    every /api/* request with zero startup diagnostic."""
    import pytest
    from pydantic import ValidationError
    from app.config import Settings
    s = Settings(api_token="a" * 32, ingest_token="", reviewer_api_token="  ")
    assert s.ingest_token is None and s.reviewer_api_token is None
    with pytest.raises(ValidationError):
        Settings(api_token="")
    with pytest.raises(ValidationError):
        Settings(api_token="   ")


# ───────── WeatherLink client must never leak credentials into errors (R2-24) ─────────

def test_weatherlink_client_error_has_no_credentials():
    """Mirror of test_ambient_client_error_has_no_credentials: the WL api-key
    travels as a QUERY PARAM, and httpx's HTTPStatusError message embeds the
    full URL — a refactor back to r.raise_for_status() would write
    WEATHERLINK_API_KEY into the poller logs on every 401/5xx."""
    import asyncio
    import httpx
    from app.weatherlink_client import WeatherLinkClient, WeatherLinkError

    KEY, SECRET = "SECRET_WL_KEY_123", "SECRET_WL_SECRET_456"

    def handler(request: httpx.Request) -> httpx.Response:
        assert KEY in str(request.url)          # the key really is on the wire
        return httpx.Response(401, text="unauthorized")

    c = WeatherLinkClient(KEY, SECRET)
    c._http = httpx.AsyncClient(base_url="https://api.weatherlink.com/v2",
                                transport=httpx.MockTransport(handler))
    try:
        asyncio.run(c.list_stations())
        raise AssertionError("expected WeatherLinkError")
    except WeatherLinkError as e:
        msg = str(e)
        assert KEY not in msg and SECRET not in msg, f"credentials leaked: {msg}"
        assert "401" in msg and "/stations" in msg   # still useful for debugging


def test_weatherlink_client_non_json_error_has_no_credentials():
    """The non-JSON branch raises too — and must be equally scrubbed."""
    import asyncio
    import httpx
    from app.weatherlink_client import WeatherLinkClient, WeatherLinkError

    KEY = "SECRET_WL_KEY_123"
    c = WeatherLinkClient(KEY, "sec")
    c._http = httpx.AsyncClient(
        base_url="https://api.weatherlink.com/v2",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    try:
        asyncio.run(c.list_stations())
        raise AssertionError("expected WeatherLinkError")
    except WeatherLinkError as e:
        assert KEY not in str(e) and "/stations" in str(e)


# ───────────────── APNs env resolution + BadDeviceToken pruning (R2-174) ─────────────────

def test_resolve_env_stored_wins_and_refuses_to_guess(monkeypatch):
    """The token's own recorded env always wins; when neither the token nor
    APNS_ENV names a known host the caller must get (None, False) and refuse
    to send — guessing sandbox once wiped every production token (CR-19)."""
    monkeypatch.setattr(apns.settings, "apns_env", "production")
    assert apns._resolve_env({"env": "sandbox"}) == ("sandbox", True)
    assert apns._resolve_env({"env": ""}) == ("production", False)
    assert apns._resolve_env({}) == ("production", False)
    monkeypatch.setattr(apns.settings, "apns_env", "staging")   # typo'd env
    assert apns._resolve_env({}) == (None, False)
    assert apns._resolve_env({"env": "sandbox"}) == ("sandbox", True)


def _mock_apns_transport(monkeypatch, handler):
    """Route apns._push_tokens' AsyncClient through an httpx.MockTransport."""
    import httpx
    real = httpx.AsyncClient
    def factory(*a, **kw):
        return real(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(apns.httpx, "AsyncClient", factory)
    monkeypatch.setattr(apns, "_provider_jwt", lambda: "test-jwt")


def test_push_tokens_refuses_to_guess_host(monkeypatch):
    """With an unresolvable env the send is counted failed WITHOUT any HTTP
    call — not defaulted to some host."""
    import asyncio
    import httpx
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact any APNs host on a guessed env")
    _mock_apns_transport(monkeypatch, handler)
    monkeypatch.setattr(apns.settings, "apns_env", "staging")
    res = asyncio.run(apns._push_tokens([{"token": "a" * 64, "env": ""}], "T", "B"))
    assert res == {"sent": 0, "dead": [], "failed": 1}


def test_push_tokens_bad_device_token_not_pruned_on_guessed_env(monkeypatch):
    """400 BadDeviceToken also means "right token, wrong environment". When the
    env was GUESSED from APNS_ENV (not recorded by the token itself) the token
    must land in `failed` (retried), never in `dead` (send_to_all deletes those
    — unrecoverable on a guess)."""
    import asyncio
    import httpx
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"reason": "BadDeviceToken"})
    _mock_apns_transport(monkeypatch, handler)
    monkeypatch.setattr(apns.settings, "apns_env", "production")
    res = asyncio.run(apns._push_tokens([{"token": "a" * 64, "env": ""}], "T", "B"))
    assert res["failed"] == 1 and res["dead"] == []          # kept for retry


def test_push_tokens_bad_device_token_pruned_when_env_from_token(monkeypatch):
    """Same 400 — but when the token recorded its OWN env there is no
    ambiguity, so it is legitimately dead and returned for pruning."""
    import asyncio
    import httpx
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.push.apple.com" in str(request.url)      # production host
        return httpx.Response(400, json={"reason": "BadDeviceToken"})
    _mock_apns_transport(monkeypatch, handler)
    monkeypatch.setattr(apns.settings, "apns_env", "production")
    res = asyncio.run(apns._push_tokens(
        [{"token": "b" * 64, "env": "production"}], "T", "B"))
    assert res["dead"] == ["b" * 64] and res["failed"] == 0


# ───────────────── weatherlink_poller.build_payload contract (R2-175) ─────────────────
# Exactly the bug class backend CR-02/CR-03 were: a silently mis-named key
# means the backend stores None and the iOS tile goes blank.

from app import weatherlink_poller as _wp  # noqa: E402


def _wl_current(iss_extra=None, sensor_type=43):
    iss = {"ts": 1778720987, "tx_id": 2, "temp": 88.4, "hum": 40.2,
           "dew_point": 60.1, "heat_index": 90.2, "uv_index": 5.5,
           "solar_rad": 812,
           "wind_speed_avg_last_2_min": 4.2, "wind_speed_last": 0.0,
           "wind_speed_hi_last_2_min": 9.0,
           "wind_dir_scalar_avg_last_2_min": 247.6, "wind_dir_last": 10,
           "rainfall_year_in": 1.02, "rainfall_day_in": 0.04,
           "rainfall_last_60_min_in": 0.01}
    iss.update(iss_extra or {})
    return {"sensors": [
        {"sensor_type": sensor_type, "data": [iss]},
        {"sensor_type": 365, "data": [{"temp_in": 75.1, "hum_in": 41.0}]},
        {"sensor_type": 242, "data": [{"bar_sea_level": 29.92}]},
    ]}


def test_wl_build_payload_flattens_to_non_null_row():
    """The emitted payload, fed through the real ingest flattener, must yield a
    non-null value for every mapped column — the end-to-end contract."""
    p = _wp.build_payload({"station_name": "Davis"}, _wl_current())
    assert p is not None
    assert p["device"]["id"] == "5D5D05000002"      # synthetic-MAC scheme, tx_id=2
    assert p["source"] == "davis-vp2-cloud"
    flat = ingest._flatten(p)
    for k, want in (("tempf", 88.4), ("humidity", 40), ("dewPoint", 60.1),
                    ("feelsLike", 90.2), ("uv", 5.5), ("solarradiation", 812.0),
                    ("windspeedmph", 4.2), ("windgustmph", 9.0), ("winddir", 248),
                    ("yearlyrainin", 1.02), ("dailyrainin", 0.04),
                    ("hourlyrainin", 0.01), ("tempinf", 75.1), ("humidityin", 41),
                    ("baromrelin", 29.92)):
        assert flat[k] == want, f"{k}: {flat[k]!r} != {want!r}"
    assert flat["dateutc"] == 1778720987000


def test_wl_build_payload_accepts_both_iss_sensor_types():
    """Davis rotates the VP2 ISS between sensor_type 43 and 46; dropping either
    silently loses 700+ observations a day (seen live 2026-05-22)."""
    for stype in (43, 46):
        p = _wp.build_payload({}, _wl_current(sensor_type=stype))
        assert p is not None and p["outdoor"]["tempf"] == 88.4


def test_wl_build_payload_adds_yearly_rain_baseline(monkeypatch):
    """A mid-year-installed ISS starts its yearly counter at 0; the configured
    baseline must be ADDED so the stored value is actual YTD rain."""
    monkeypatch.setattr(_wp.settings, "weatherlink_yearly_rain_baseline_in", 3.0)
    p = _wp.build_payload({}, _wl_current())
    assert p["rain"]["yearly_in"] == 1.02 + 3.0


def test_wl_build_payload_prefers_2min_averages_and_keeps_zero_wind():
    """2-min avg wind (4.2) wins over the single-sample wind_speed_last (0.0);
    without the averages, wind_speed_last is used even at 0.0 — a calm reading
    must survive, not vanish (the zero-value bug class fixed twice already)."""
    p = _wp.build_payload({}, _wl_current())
    assert p["wind"]["speed_mph"] == 4.2 and p["wind"]["direction"] == 248
    p0 = _wp.build_payload({}, _wl_current(
        {"wind_speed_avg_last_2_min": None, "wind_dir_scalar_avg_last_2_min": None}))
    assert p0["wind"]["speed_mph"] == 0.0        # zero kept, not dropped
    assert p0["wind"]["direction"] == 10          # falls back to wind_dir_last


def test_wl_build_payload_none_without_iss_or_timestamp():
    """No ISS sensor (or no ts) → no usable observation → None, not a bogus
    half-payload."""
    assert _wp.build_payload({}, {"sensors": [
        {"sensor_type": 365, "data": [{"temp_in": 1}]}]}) is None
    assert _wp.build_payload({}, {"sensors": [
        {"sensor_type": 43, "data": [{"temp": 70.0}]}]}) is None    # no ts
    assert _wp.build_payload({}, {"sensors": []}) is None


# ───────────── _flatten yearly_in overflow-string coercion (R2-164 / CR-61) ─────────────

def test_flatten_yearly_in_overflow_string_becomes_none():
    """_scrub_numbers only filters values that arrive AS numbers; the string
    "1e999" passes through and float() turns it into inf — which Starlette's
    JSONResponse (allow_nan=False) then 500s on every /current read containing
    the row. The post-coercion finiteness check must null it instead."""
    payload = {
        "device":        {"id": "5D5D010002C7"},
        "timestamp_utc": "2026-05-24T07:40:15Z",
        "rain":          {"yearly_in": "1e999"},
    }
    out = ingest._flatten(payload)
    assert out is not None
    assert out["yearlyrainin"] is None


def test_flatten_yearly_in_overflow_string_with_offset_configured(monkeypatch):
    """Same vector with a per-MAC rain offset configured: the offset math
    (max(0, inf - offset) == inf) must never resurrect the non-finite value."""
    from app import config
    monkeypatch.setattr(config.settings, "ingest_yearly_rain_offsets",
                        {"5D:5D:01:00:02:C7": 2.85})
    payload = {
        "device":        {"id": "5D5D010002C7"},
        "timestamp_utc": "2026-05-24T07:40:15Z",
        "rain":          {"yearly_in": "1e999"},
    }
    out = ingest._flatten(payload)
    assert out is not None
    assert out["yearlyrainin"] is None
