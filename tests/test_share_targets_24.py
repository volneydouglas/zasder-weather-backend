"""The three networks added in 2.4 (item 8).

Met Office WOW, AWEKAS and OpenWeatherMap. What is pinned is the unit
conversion at each boundary, because two of these are metric and an
upload that sends Fahrenheit into a field named `temp` does not error,
it just publishes a station that appears to be on fire.
"""
from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import share_targets as st  # noqa: E402

WHEN = dt.datetime(2026, 9, 21, 14, 5, tzinfo=dt.timezone.utc)
OBS = {
    "tempf": 68.0, "humidity": 55.0, "dewPoint": 50.0,
    "windspeedmph": 10.0, "windgustmph": 20.0, "winddir": 180.0,
    "baromrelin": 29.92, "dailyrainin": 0.50, "solarradiation": 400.0,
    "uv": 5.0, st.RAIN_LAST_HOUR: 0.10,
}


def test_wow_speaks_the_same_imperial_dialect_as_the_others():
    p = st.wow_params({"station_id": "abc-123", "api_key": "123456"},
                      OBS, WHEN)
    assert p["siteid"] == "abc-123"
    assert p["siteAuthenticationKey"] == "123456"
    assert p["dateutc"] == "2026-09-21 14:05:00"
    # Imperial, unconverted, like PWSweather.
    assert p["tempf"] == 68.0 and p["baromin"] == 29.92
    assert p["rainin"] == 0.10 and p["dailyrainin"] == 0.50
    # A reading the station does not have is absent, never zero.
    assert "uv" not in st.wow_params({}, {"tempf": 68.0}, WHEN)


def test_awekas_is_metric_and_that_is_the_whole_trap():
    """AWEKAS takes ONE query parameter, `val`, a semicolon-joined list
    in a fixed order (the WeeWX reference implementation is the
    documentation everybody actually reads). Named parameters, which is
    what shipped first, are simply ignored and every upload rejected."""
    import hashlib
    p = st.awekas_params({"station_id": "me", "api_key": "secret"},
                         OBS, WHEN,
                         coords=(33.3, -111.94))
    assert set(p) == {"val"}
    v = p["val"].split(";")
    assert len(v) == 25
    assert v[0] == "me"
    # The password is hashed the way their API documents, so the plain
    # one never rides in a query string.
    assert v[1] == hashlib.md5(b"secret").hexdigest()
    assert v[2] == "21.09.2026" and v[3] == "14:05"
    assert v[4] == "20.0"                       # 68 °F
    assert v[5] == "55"
    assert v[6] == "1013.2"                     # 29.92 inHg
    assert v[7] == "12.7"                       # 0.5 in
    assert v[8] == "16.1"                       # 10 mph
    assert v[9] == "180"
    assert v[13] == "en"
    assert v[15] == "32.2"
    assert v[16] == "400.0"
    assert v[17] == "5.0"
    assert v[21] == "2.5"                       # 0.1 in/h
    assert v[22].startswith("ZasderWeather-")
    assert v[23] == "-111.94" and v[24] == "33.3"
    # A reading with nothing but a temperature leaves every other slot
    # empty rather than zero.
    lean = st.awekas_params({"station_id": "me", "api_key": "s"},
                            {"tempf": 68.0}, WHEN)["val"].split(";")
    assert len(lean) == 25 and lean[4] == "20.0"
    assert lean[5] == "" and lean[7] == "" and lean[23] == ""


def test_openweathermap_is_si_and_json():
    m = st.owm_measurement({"station_id": "owm-1"}, OBS, WHEN)
    assert m["station_id"] == "owm-1"
    assert m["dt"] == int(WHEN.timestamp())
    assert m["temperature"] == 20.0             # °C
    assert m["wind_speed"] == 4.47              # m/s
    assert m["wind_gust"] == 8.94
    assert m["pressure"] == 1013.2              # hPa
    assert m["rain_1h"] == 2.54                 # mm from 0.1 in
    assert m["dew_point"] == 10.0
    # A station with no gust sends no gust.
    lean = st.owm_measurement({"station_id": "x"}, {"tempf": 68.0}, WHEN)
    assert "wind_gust" not in lean and "rain_1h" not in lean


def test_every_network_has_a_floor_a_sender_and_shown_fields():
    """A target the tables disagree about is a target that either never
    sends or sends too fast, and both are silent."""
    from app import main as M
    for target in st.TARGETS:
        assert target in st.MIN_INTERVAL_MIN, target
        assert target in st.SENDERS, target
        assert target in M._SHARE_FIELDS, target
        assert st.MIN_INTERVAL_MIN[target] >= 1
    # And nothing shows a credential back: only station_id is in the
    # allow-list, whatever the other field is called.
    for target, fields in M._SHARE_FIELDS.items():
        shown = [f for f in fields if f in M._SHARE_SHOWN]
        assert "api_key" not in shown and "password" not in shown, target
