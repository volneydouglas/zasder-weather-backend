"""WeatherFlow Tempest cloud client + poller transform.

Fixture is a real observation from Doren's station (173303, Irwin PA),
captured 2026-08-16 during a thunderstorm — which is why the lightning and
rain fields are populated.
"""
from __future__ import annotations

import os

import httpx
import pytest

# Set BEFORE importing app: config.py reads the environment at import time and
# .env carries a placeholder that fails the length floor. The `test-` prefix is
# exempt, but only under pytest (see Settings' token validator).
os.environ.setdefault("API_TOKEN", "test-api-token")

from app.tempest_client import TempestClient, TempestError  # noqa: E402
from app.tempest_poller import build_payload, synth_mac  # noqa: E402


# Real payload, trimmed. NOTE the units: everything is METRIC.
OBS = {
    "air_temperature": 27.2,
    "relative_humidity": 80,
    "dew_point": 23.4,
    "feels_like": 30.2,
    "uv": 0.04,
    "solar_radiation": 5,
    "wind_avg": 2.4,
    "wind_gust": 4.2,
    "wind_lull": 1.3,
    "wind_direction": 270,
    "sea_level_pressure": 1015.6,
    "station_pressure": 974.8,
    "precip_accum_local_day": 3.747711,
    "precip_accum_last_1hr": 0.262306,
    "lightning_strike_count_last_1hr": 807,
    "timestamp": 1786910886,   # 2026-08-16T20:08:06Z
}

# What the SAME response advertises about the owner's display preference.
# This is not what the numbers are.
STATION_UNITS = {
    "units_temp": "f", "units_wind": "mph", "units_precip": "in",
    "units_pressure": "inhg", "units_other": "imperial",
}


def test_build_payload_converts_metric_to_api_native():
    """Storage is API-native (°F, mph, inHg, inches) — the conversion is the
    whole job here, and every constant is cross-checked by hand."""
    p = build_payload(173303, OBS, "Chaucer Drive")
    assert p is not None
    assert p["device"]["id"] == "5D5D0602A4F7"
    assert p["device"]["name"] == "Chaucer Drive"
    assert p["source"] == "tempest"
    assert p["timestamp_utc"].startswith("2026-08-16T")

    o = p["outdoor"]
    assert o["tempf"] == pytest.approx(80.96, abs=0.01)     # 27.2 °C
    assert o["dew_point_f"] == pytest.approx(74.12, abs=0.01)
    assert o["feels_like"] == pytest.approx(86.36, abs=0.01)
    assert o["humidity"] == 80                               # already %
    assert o["uv"] == pytest.approx(0.04)
    assert o["solar_wm2"] == pytest.approx(5.0)              # already W/m²

    w = p["wind"]
    assert w["speed_mph"] == pytest.approx(5.37, abs=0.01)   # 2.4 m/s
    assert w["gust_mph"] == pytest.approx(9.4, abs=0.01)     # 4.2 m/s
    assert w["direction"] == 270

    r = p["rain"]
    assert r["daily_in"] == pytest.approx(0.148, abs=0.001)  # 3.7477 mm
    assert r["hourly_in"] == pytest.approx(0.01, abs=0.001)

    # sea_level_pressure (1015.6 mb), NOT station_pressure (974.8 mb).
    assert p["pressure"]["relative_inhg"] == pytest.approx(29.991, abs=0.002)


def test_station_pressure_is_not_used_as_relative():
    """Regression for a band rejection that would only bite at altitude.

    station_pressure is the uncorrected reading — 974.8 mb is 28.79 inHg, which
    survives the 24-33 inHg band here but would fall straight through it for a
    station a few thousand feet up. baromrelin means the sea-level-corrected
    value, and Tempest hands us that separately.
    """
    p = build_payload(173303, OBS)
    rel = p["pressure"]["relative_inhg"]
    assert rel > 29.5, "looks like the raw station pressure leaked through"
    assert rel == pytest.approx(1015.6 / 33.8639, abs=0.002)


def test_response_is_metric_regardless_of_station_units():
    """The trap this integration is most likely to fall into.

    `station_units` reports what the station's OWNER sees, not what the
    response contains. Doren's station advertises Fahrenheit and returns
    Celsius. Reading that block as the payload's units lands 50 °F off with
    nothing looking broken.
    """
    assert STATION_UNITS["units_temp"] == "f"
    p = build_payload(173303, OBS)
    # 27.2 treated as °F would store 27.2; converted from °C it is 80.96.
    assert p["outdoor"]["tempf"] == pytest.approx(80.96, abs=0.01)
    assert p["outdoor"]["tempf"] != pytest.approx(27.2, abs=0.01)


def test_wind_lull_is_never_reported_as_sustained():
    """wind_lull is the interval MINIMUM. Putting it in a sustained-wind
    field would seed the records/QC path with a number below the average."""
    p = build_payload(173303, OBS)
    lull_mph = 1.3 * 2.2369362920544
    assert p["wind"]["speed_mph"] != pytest.approx(lull_mph, abs=0.01)
    assert p["wind"]["speed_mph"] > p["wind"]["gust_mph"] * 0.0  # sanity
    assert p["wind"]["gust_mph"] >= p["wind"]["speed_mph"]


def test_partial_observation_omits_missing_blocks():
    """A hub reporting only temperature must still ingest, without inventing
    a 0 mph wind or a 0.00 in rain total for hardware that said nothing."""
    p = build_payload(173303, {"timestamp": 1786910886, "air_temperature": 20.0})
    assert p is not None
    assert p["outdoor"]["tempf"] == pytest.approx(68.0)
    assert "wind" not in p
    assert "rain" not in p
    assert "pressure" not in p


def test_no_timestamp_or_no_data_returns_none():
    assert build_payload(173303, {"air_temperature": 20.0}) is None
    assert build_payload(173303, {"timestamp": 1786910886}) is None
    assert build_payload(173303, {}) is None


def test_non_finite_and_string_values_are_dropped():
    """A NaN reaching the ingest path becomes a records/QC problem later —
    same reasoning as the plausibility bands."""
    p = build_payload(173303, {
        "timestamp": 1786910886,
        "air_temperature": float("nan"),
        "relative_humidity": "78",          # string
        "wind_avg": float("inf"),
        "sea_level_pressure": 1015.6,
    })
    assert p is not None
    assert "outdoor" not in p or "tempf" not in p.get("outdoor", {})
    assert "wind" not in p
    assert p["pressure"]["relative_inhg"] == pytest.approx(29.991, abs=0.002)


def test_synth_mac_scheme():
    """06 is the WeatherFlow type tag (01 AcuRite, 02 Fine Offset, 05 Davis),
    and the low three bytes are the station id."""
    assert synth_mac(173303) == "5D5D0602A4F7"
    assert synth_mac(1) == "5D5D06000001"
    # Absurd ids are masked rather than producing a malformed MAC.
    assert len(synth_mac(0xFFFFFFFF)) == 12


# ───────────────────────────── credential safety ─────────────────────────────

def test_error_never_carries_the_token():
    """The token rides as a QUERY PARAM and httpx embeds the full URL in
    HTTPStatusError. This is the third time this shape has appeared in this
    backend (AmbientWeather, WeatherLink); the log must not get the secret.
    """
    # Fully synthetic. An earlier fixture kept three of the five segments of
    # a REAL Tempest personal access token and only masked the middle two —
    # 24 of its 32 hex characters, published to the mirror. A partially
    # redacted credential is still a credential.
    #
    # The variable is not called `secret`/`token` on purpose: the mirror's
    # secret sweep is name-anchored, so `secret = "<uuid>"` is precisely the
    # shape it must abort on, and a fixture must not have to be exempted
    # from the guard it exists to exercise.
    planted = "abcdef01-2345-6789-abcd-ef0123456789"
    request = httpx.Request(
        "GET", f"https://swd.weatherflow.com/swd/rest/stations?token={planted}")
    response = httpx.Response(401, request=request)
    err = TempestError.from_http(
        httpx.HTTPStatusError("401", request=request, response=response))
    assert planted not in str(err)
    assert "token" not in str(err)
    assert "401" in str(err) and "/swd/rest/stations" in str(err)


async def test_status_envelope_failure_raises(monkeypatch):
    """The API answers 200 with a status envelope for some failures, so a 2xx
    alone is not success — an unauthorized token would otherwise look like an
    empty station and the poller would report healthy forever."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": {"status_code": 401, "status_message": "UNAUTHORIZED"}})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return original(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    with pytest.raises(TempestError) as ei:
        await TempestClient("tok").stations()
    assert "401" in str(ei.value)
    assert "tok" not in str(ei.value)


async def test_empty_obs_is_none_not_an_error(monkeypatch):
    """An unplugged hub answers SUCCESS with an empty obs list. That is a
    quiet station, not a credential failure, and must not be logged as one."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": {"status_code": 0}, "obs": []})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return original(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    assert await TempestClient("tok").station_observation(173303) is None


def test_redact_scrubs_a_tempest_url():
    """Belt and braces behind TempestError. If a raw httpx error ever escapes
    an unwrapped path, `source_status.redact` is what stands between the token
    and /api/sources — and the poller's own log line runs through it too.

    (The other half of the defence is main.py silencing httpx at WARNING,
    because httpx logs every request at INFO with the full URL. Verified: a
    standalone harness without that line prints the token in plaintext.)
    """
    from app import source_status
    leaked = ("Tempest request failed: GET https://swd.weatherflow.com/swd/"
              "rest/observations/station/173303?token=675419cf-DEAD-BEEF")
    out = source_status.redact(leaked)
    assert "675419cf-DEAD-BEEF" not in out
    assert "station/173303" in out, "the useful part must survive"


# ─────────────────────── station metadata (name + coords) ───────────────────

# Trimmed real /stations record for 173303.
STATION = {
    "station_id": 173303,
    "name": "Chaucer Drive",
    "public_name": "Chaucer Drive",
    "latitude": 40.31781,
    "longitude": -79.69794,
    "timezone": "America/New_York",
    "station_meta": {"elevation": 342.84},
}


def test_station_metadata_supplies_name_and_coords():
    """Tempest hands over the station's coordinates, which almost no other
    source here does. Without them a station gets no forecast and no sunrise,
    so its condition glyph falls back to the neutral "cannot tell" state —
    which is the whole point of today's WeatherIcons work.

    ingest._payload_coords reads exactly this shape.
    """
    p = build_payload(173303, OBS, None, STATION)
    assert p["device"]["name"] == "Chaucer Drive"
    assert p["device"]["coords"] == {"lat": 40.31781, "lon": -79.69794}


def test_configured_name_beats_the_station_name():
    p = build_payload(173303, OBS, "Backyard Tempest", STATION)
    assert p["device"]["name"] == "Backyard Tempest"


def test_location_is_not_a_duplicate_of_the_name():
    """public_name is a copy of name and there is no city field, so emitting
    `location` would render the same string twice in the app."""
    p = build_payload(173303, OBS, None, STATION)
    assert "location" not in p["device"]


def test_missing_or_partial_station_metadata_is_harmless():
    """The metadata read is best-effort — a failure there must not stop
    observations from being recorded."""
    p = build_payload(173303, OBS, None, None)
    assert p is not None and "coords" not in p["device"]
    half = build_payload(173303, OBS, None, {"name": "X", "latitude": 40.0})
    assert half["device"]["name"] == "X"
    assert "coords" not in half["device"], "a lone latitude is not a location"



def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_lightning_survives_the_flattener_into_storage():
    """The `lightning` block was being dropped by ingest._flatten, which only
    picks known blocks — the same silent loss the `solar` comment there
    records. The module docstring claimed bonus fields survive in data_json;
    they do not, because data_json stores the FLATTENED dict, not the payload.

    Pins the whole path: poller block -> flattener -> stored keys.
    """
    from app import ingest
    payload = {
        "device": {"id": "5D5D0603822E"},
        "timestamp_utc": _now_iso(),
        "outdoor": {"tempf": 77.0, "humidity": 83},
        "wind": {}, "rain": {}, "pressure": {},
        "lightning": {"strike_count": 23, "strike_count_last_1hr": 731,
                      "strike_count_last_3hr": 761,
                      "last_distance_mi": 6.2,
                      "last_strike_ms": 1787204673000},
        "source": "tempest",
    }
    flat = ingest._flatten(payload)
    assert flat is not None
    assert flat["lightningcount"] == 23
    assert flat["lightning_last_1hr"] == 731
    assert flat["lightning_distance_mi"] == 6.2
    # The epoch must stay an int: the metric coercion would reshape it.
    assert flat["lightning_last_strike_ms"] == 1787204673000


def test_a_reading_without_lightning_gains_no_lightning_keys():
    """Most sources have no lightning sensor. They must not acquire empty
    columns that render as a tile reading zero strikes — the 'absent is not
    zero' family this backend keeps relearning."""
    from app import ingest
    flat = ingest._flatten({
        "device": {"id": "AABBCCDDEEFF"},
        "timestamp_utc": _now_iso(),
        "outdoor": {"tempf": 70.0}, "wind": {}, "rain": {}, "pressure": {},
        "source": "test",
    })
    assert flat is not None
    assert not [k for k in flat if k.startswith("lightning")]


def test_a_lightning_only_observation_is_not_dropped():
    """Review 2026-08-20: the 'nothing but a timestamp' check predated the
    lightning block, so an obs whose only content was strike data (faulted
    air/wind sensors mid-thunderstorm) was discarded — and the
    interval-scoped counters reset server-side, losing the storm's
    lightning permanently. Strike data alone IS an observation."""
    from app.tempest_poller import build_payload
    obs = {"timestamp": 1787204673,
           "lightning_strike_count": 12,
           "lightning_strike_count_last_1hr": 380,
           "lightning_strike_last_distance": 8}
    payload = build_payload(173303, obs)
    assert payload is not None, "lightning-only observation dropped"
    assert payload["lightning"]["strike_count_last_1hr"] == 380
    # And a truly empty obs is still rejected.
    assert build_payload(173303, {"timestamp": 1787204673}) is None


async def test_poller_401_lands_in_source_status_without_the_token(monkeypatch):
    """CODE_REVIEW_R5 R5-48 (the CR-01/R3-01 regression shape, third
    module): drive one REAL poll tick against a mocked 401 — the token
    rides the URL as ?token=… — and assert the failure recorded in
    source_status carries neither the token nor any token= query."""
    import asyncio
    import json as _json
    import httpx
    from app import source_status, tempest_client, tempest_poller

    secret = "SECRET-TEMPEST-TOKEN-12345678"
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("token") == secret  # really sent
        return httpx.Response(401, json={})
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler)))

    client = tempest_client.TempestClient(secret)
    poller = tempest_poller.TempestPoller(client, 229934, 3600, None)
    await poller.start()            # metadata lookup 401s (best-effort)
    await asyncio.sleep(0.05)       # first _run tick fails and records
    await poller.stop()

    snap = _json.dumps(source_status.snapshot())
    tempest_rows = [r for r in source_status.snapshot()
                    if r.get("name") == "tempest"]
    assert tempest_rows and tempest_rows[0].get("last_error"), \
        "the failed tick never reached source_status"
    assert secret not in snap, "token leaked into /api/sources"
    assert "token=" not in snap, "token query leaked into /api/sources"
