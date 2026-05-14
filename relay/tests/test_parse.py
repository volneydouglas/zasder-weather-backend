"""Tests for the relay's Wunderground rapidfire parser.

The parser reads the query string the AcuRite Atlas hub POSTs to
/weatherstation/updateweatherstation and produces a normalized observation
dict that mirrors what /ingest/custom expects on the backend."""
from __future__ import annotations

import os, sys

# Add the relay root to sys.path so we can `import relay`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay  # noqa: E402


def _qs(s: str) -> dict[str, list[str]]:
    """Helper: turn a Wunderground rapidfire query-string fragment into the
    parse_qs form parse_observation expects."""
    from urllib.parse import parse_qs
    return parse_qs(s, keep_blank_values=True)


def test_parses_full_atlas_payload():
    # Captured verbatim from a real AcuRite Atlas hub POST.
    qs = _qs("dateutc=2026-05-14T01:05:12&id=24C86E0A66F5&mt=Atlas"
             "&sensor=00000711&sensorbattery=normal&rssi=4&hubbattery=low"
             "&baromin=29.90&uvindex=4&lightintensity=79550"
             "&humidity=8&tempf=98.3&windspeedmph=3&winddir=284"
             "&windgustmph=4&windgustdir=253&windspeedavgmph=2"
             "&heatindex=91.9&feelslike=94.0&windchill=98.3"
             "&dewptf=26.9&dailyrainin=0.00&rainin=0.00")
    obs = relay.parse_observation(qs)
    assert obs["device"]["id"] == "24C86E0A66F5"
    assert obs["device"]["model"] == "Atlas"
    assert obs["device"]["battery_outdoor"] == "normal"
    assert obs["device"]["battery_hub"] == "low"
    assert obs["device"]["rssi"] == 4
    assert obs["timestamp_utc"] == "2026-05-14T01:05:12"
    assert obs["outdoor"]["tempf"] == 98.3
    assert obs["outdoor"]["humidity"] == 8.0
    assert obs["outdoor"]["feels_like"] == 94.0
    assert obs["outdoor"]["dew_point_f"] == 26.9
    assert obs["outdoor"]["uv"] == 4.0
    # 79550 lux / 126 ≈ 631.3 W/m²
    assert obs["outdoor"]["solar_wm2"] == 631.3
    assert obs["wind"]["speed_mph"] == 3.0
    assert obs["wind"]["gust_mph"] == 4.0
    assert obs["wind"]["direction"] == 284
    assert obs["pressure"]["relative_inhg"] == 29.9
    assert obs["source"] == "acurite-atlas"


def test_empty_strings_become_none():
    """Acurite leaves lightning fields blank when the sensor doesn't report
    them. Empty strings must NOT be coerced to 0 — they're absent values."""
    qs = _qs("dateutc=2026-05-14T01:05:12&id=AABB&strikecount=&interference="
             "&last_strike_distance=&tempf=70")
    obs = relay.parse_observation(qs)
    assert obs["lightning"]["strike_count"] is None
    assert obs["lightning"]["interference"] is None
    assert obs["lightning"]["last_strike_distance"] is None
    assert obs["outdoor"]["tempf"] == 70.0


def test_missing_lightintensity_means_no_solar():
    """If the hub doesn't report light, solar_wm2 stays None instead of 0."""
    qs = _qs("dateutc=2026-05-14T01:05:12&id=AABB&tempf=70")
    obs = relay.parse_observation(qs)
    assert obs["outdoor"]["solar_wm2"] is None
    assert obs["outdoor"]["light_lux"] is None


def test_garbage_numeric_fields_become_none():
    """The parser is defensive: anything that doesn't parse as float falls
    through to None rather than raising."""
    qs = _qs("dateutc=2026-05-14T01:05:12&id=AABB&tempf=NaN&humidity=hello")
    obs = relay.parse_observation(qs)
    # Note: float("NaN") actually succeeds in Python → produces nan, but
    # "hello" is a real failure.
    assert obs["outdoor"]["humidity"] is None
