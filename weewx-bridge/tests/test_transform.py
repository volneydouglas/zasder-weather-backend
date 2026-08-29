"""The bridge's pure transform — runnable anywhere, no WeeWX needed."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin", "user"))
from zasder import record_to_payload  # noqa: E402

TS = 1_787_155_200          # 2026-08-19 16:00:00 UTC


def _record(**over):
    d = {"dateTime": TS, "usUnits": 1,
         "outTemp": 104.6, "outHumidity": 14.0, "dewpoint": 47.1,
         "inTemp": 82.2, "inHumidity": 33.0,
         "barometer": 29.842, "windSpeed": 6.4, "windGust": 13.1,
         "windDir": 204.0, "rainRate": 0.0, "dayRain": 0.12,
         "UV": 9.1, "radiation": 861.0}
    d.update(over)
    return d


def test_full_record_maps_to_ingest_shape():
    p = record_to_payload(_record(), device_id="patio", station_name="Patio")
    assert p["device"] == {"id": "patio", "model": "WeeWX", "name": "Patio"}
    assert p["source"] == "weewx"
    assert p["timestamp_utc"] == "2026-08-19T16:00:00Z"
    assert p["outdoor"]["tempf"] == 104.6
    assert p["outdoor"]["solar_wm2"] == 861.0
    assert p["indoor"]["humidity"] == 33.0
    assert p["wind"] == {"speed_mph": 6.4, "gust_mph": 13.1,
                         "direction": 204.0}
    assert p["rain"] == {"hourly_in": 0.0, "daily_in": 0.12}
    assert p["pressure"] == {"relative_inhg": 29.842}


def test_absent_is_omitted_never_zero():
    """A temperature-only station must not invent wind/rain/solar keys —
    the backend treats absent as absent, and so must we."""
    p = record_to_payload({"dateTime": TS, "outTemp": 71.2})
    assert p["outdoor"] == {"tempf": 71.2}
    for block in ("indoor", "wind", "rain", "pressure"):
        assert block not in p
    # None values are dropped inside a block too.
    p = record_to_payload({"dateTime": TS, "outTemp": 71.2,
                           "outHumidity": None})
    assert "humidity" not in p["outdoor"]


def test_zero_readings_survive():
    """0 is data (calm wind, night solar) — only None/absent is silence."""
    p = record_to_payload({"dateTime": TS, "windSpeed": 0.0,
                           "radiation": 0.0, "UV": 0.0})
    assert p["wind"]["speed_mph"] == 0.0
    assert p["outdoor"]["solar_wm2"] == 0.0
    assert p["outdoor"]["uv"] == 0.0


def test_garbage_values_are_dropped():
    p = record_to_payload(_record(outTemp="abc", windSpeed=float("nan"),
                                  windGust=float("inf")))
    assert "tempf" not in p["outdoor"]
    assert "speed_mph" not in p.get("wind", {})
    assert "gust_mph" not in p.get("wind", {})


def test_unusable_timestamp_returns_none():
    assert record_to_payload({"dateTime": None, "outTemp": 70}) is None
    assert record_to_payload({"dateTime": 0, "outTemp": 70}) is None
    assert record_to_payload({}) is None


def test_default_device_id_is_stable():
    p = record_to_payload(_record())
    assert p["device"]["id"] == "weewx"
    assert "name" not in p["device"]
