"""Pure-function tests — no DB, no fixtures, no env required.

These cover the parsing + flattening helpers that turn a Wunderground-format
hub query string (or a normalized JSON payload) into the flat shape the
backend stores."""
from __future__ import annotations

import os

# These modules don't read env at import time, so we can import them directly.
os.environ.setdefault("API_TOKEN", "x")
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


# ───────────────────────── _device_label ─────────────────────────

def test_device_label_uses_payload_overrides():
    name, loc = ingest._device_label({"device": {"name": "Backyard", "location": "Phoenix"}})
    assert name == "Backyard"
    assert loc == "Phoenix"

def test_device_label_pretty_for_known_source():
    name, loc = ingest._device_label({"device": {}, "source": "acurite-atlas"})
    assert name == "AcuRite Atlas"
    assert loc is None

def test_device_label_includes_model_when_distinct():
    name, _ = ingest._device_label({"device": {"model": "Iris"}, "source": "acurite-atlas"})
    assert name == "AcuRite Atlas (Iris)"
