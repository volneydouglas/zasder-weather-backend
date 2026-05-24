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
