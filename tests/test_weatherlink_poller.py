"""weatherlink_poller.build_payload — the WeatherLink→ingest transform
(R3-135). This is the file whose comments carry the "700+ observations
vanished" incident; the transform had zero tests.
"""
from __future__ import annotations

import importlib
import time

import pytest


@pytest.fixture
def wl(temp_env: str):
    """Import the module under test with the test env in place."""
    for mod in ["app.config", "app.weatherlink_poller"]:
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import weatherlink_poller
    return weatherlink_poller


def _iss_data(**over):
    d = {"ts": int(time.time()), "tx_id": 1,
         "temp": 95.2, "hum": 30.4, "dew_point": 55.1, "heat_index": 99.0,
         "uv_index": 7.5, "solar_rad": 800,
         "wind_speed_avg_last_2_min": 5.5,
         "wind_speed_hi_last_2_min": 12.0,
         "wind_dir_scalar_avg_last_2_min": 200.4,
         "wind_speed_last": 0.0, "wind_dir_last": 10,
         "rainfall_year_in": 10.0, "rainfall_day_in": 0.5,
         "rainfall_last_60_min_in": 0.1}
    d.update(over)
    return d


def _current(sensor_type=43, iss=None):
    sensors = []
    if iss is not None:
        sensors.append({"sensor_type": sensor_type, "data": [iss]})
    sensors.append({"sensor_type": 365,
                    "data": [{"temp_in": 78.0, "hum_in": 40.4}]})
    sensors.append({"sensor_type": 242,
                    "data": [{"bar_sea_level": 29.92}]})
    return {"sensors": sensors}


def test_sensor_types_43_and_46_both_produce_iss_data(wl):
    """(a) Davis cloud rotates between 43 and 46 for the same ISS — dropping
    either is the exact silent-data-loss incident (700+ observations)."""
    for stype in (43, 46):
        payload = wl.build_payload({}, _current(sensor_type=stype, iss=_iss_data()))
        assert payload is not None, f"sensor_type {stype} must be ISS data"
        assert payload["outdoor"]["tempf"] == 95.2


def test_no_iss_sensor_returns_none(wl):
    assert wl.build_payload({}, _current(iss=None)) is None
    assert wl.build_payload({}, {"sensors": []}) is None


def test_missing_or_zero_ts_returns_none(wl):
    assert wl.build_payload({}, _current(iss=_iss_data(ts=0))) is None
    iss = _iss_data()
    del iss["ts"]
    assert wl.build_payload({}, _current(iss=iss)) is None


def test_yearly_rain_baseline_added_daily_hourly_pass_through(wl, monkeypatch):
    """(d) The operator baseline shifts ONLY the cumulative yearly counter;
    daily/hourly are true period values and must pass through untouched."""
    monkeypatch.setattr(wl.settings, "weatherlink_yearly_rain_baseline_in", 5.25)
    rain = wl.build_payload({}, _current(iss=_iss_data()))["rain"]
    assert rain["yearly_in"] == pytest.approx(15.25)
    assert rain["daily_in"] == 0.5
    assert rain["hourly_in"] == 0.1


def test_wind_prefers_2min_averages_and_falls_back_to_last(wl):
    """(e) The 2-min average beats the 2.5s spot sample; when the averages
    are absent the *_last fields must still be read (0 mph is a reading)."""
    w = wl.build_payload({}, _current(iss=_iss_data()))["wind"]
    assert w["speed_mph"] == 5.5
    assert w["direction"] == 200        # rounded scalar avg, not wind_dir_last
    assert w["gust_mph"] == 12.0
    no_avg = _iss_data(wind_speed_avg_last_2_min=None,
                       wind_dir_scalar_avg_last_2_min=None)
    w = wl.build_payload({}, _current(iss=no_avg))["wind"]
    assert w["speed_mph"] == 0.0        # falls back to wind_speed_last
    assert w["direction"] == 10


def test_synth_mac_matches_relay_scheme(wl):
    """(f) 5D:5D:05:HH:HH:HH — must match sdr-relay/davis-relay so cloud and
    SDR post to the same device row."""
    assert wl._synth_mac(0x7D) == "5D5D0500007D"
    assert wl._synth_mac(0x0A0B0C) == "5D5D050A0B0C"


def test_field_names_match_ingest_flatten_contract(wl, temp_env):
    """(g) build_payload's key names ARE the ingest contract — a rename here
    silently drops the field end-to-end (tile goes blank, nothing errors).
    Run the real payload through ingest._flatten and pin every mapping."""
    from app import ingest
    payload = wl.build_payload({"station_name": "Davis"}, _current(iss=_iss_data()))
    flat = ingest._flatten(payload)
    assert flat is not None, "payload timestamp must be accepted"
    assert flat["tempf"] == 95.2
    assert flat["feelsLike"] == 99.0            # heat_index → feels_like
    assert flat["dewPoint"] == 55.1             # dew_point_f
    assert flat["humidity"] == 30.0             # rounded by build_payload
    assert flat["uv"] == 7.5
    assert flat["solarradiation"] == 800.0      # solar_wm2
    assert flat["windspeedmph"] == 5.5
    assert flat["windgustmph"] == 12.0
    assert flat["winddir"] == 200
    assert flat["baromrelin"] == 29.92          # relative_inhg
    assert flat["tempinf"] == 78.0
    assert flat["humidityin"] == 40.0
    assert flat["hourlyrainin"] == 0.1
    assert flat["dailyrainin"] == 0.5
    assert flat["yearlyrainin"] == pytest.approx(10.0)


# ── _run failure accounting (1.9 test debt, TEST_GAP_AUDIT Tier 1) ──────
# Tempest and AirGradient each pin their poller's failure path into
# source_status; WeatherLink never got the same test — a regression in
# _run's accounting (or a credential leaking into /api/sources through
# the error string) would ship green.

async def test_run_failure_lands_in_source_status_without_credentials(
        wl, monkeypatch):
    """One REAL _run tick against a mocked 401: the failure must be
    recorded on davis-cloud, and neither the api-key (rides the URL as a
    query param) nor the api-secret (a header) may reach the snapshot."""
    import asyncio
    import json as _json

    import httpx

    from app import source_status, weatherlink_client

    key, secret = "WL-API-KEY-1234567890", "WL-API-SECRET-0987654321"
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("api-key") == key       # really sent
        assert request.headers.get("X-Api-Secret") == secret
        return httpx.Response(401, json={})

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler), **kw))

    client = weatherlink_client.WeatherLinkClient(key, secret)
    poller = wl.WeatherlinkPoller(client, 12345, 3600)
    await poller.start()            # discovery 401s (best-effort)
    await asyncio.sleep(0.05)       # first _run tick fails and records
    await poller.stop()

    rows = [r for r in source_status.snapshot()
            if r.get("name") == "davis-cloud"]
    assert rows and rows[0]["last_error"], \
        "the failed tick never reached source_status"
    assert rows[0]["consecutive_failures"] >= 1
    snap = _json.dumps(source_status.snapshot())
    assert key not in snap, "api-key leaked into /api/sources"
    assert secret not in snap, "api-secret leaked into /api/sources"
    assert "api-key=" not in snap, "key query leaked into /api/sources"


async def test_run_no_iss_data_is_success_with_zero_rows(wl, monkeypatch):
    """The API answering with no ISS sensor is NOT a failure — it must
    record success with rows=0 (credentials fine, station contributing
    nothing), clearing any prior error rather than raising a false alarm."""
    import asyncio

    import httpx

    from app import source_status, weatherlink_client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stations":
            return httpx.Response(200, json={"stations": []})
        return httpx.Response(200, json={"sensors": [
            {"sensor_type": 365, "data": [{"temp_in": 78.0}]}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler), **kw))

    client = weatherlink_client.WeatherLinkClient("k" * 20, "s" * 20)
    poller = wl.WeatherlinkPoller(client, 12345, 3600)
    await poller.start()
    await asyncio.sleep(0.05)
    await poller.stop()

    row = next(r for r in source_status.snapshot()
               if r.get("name") == "davis-cloud")
    assert row["last_error"] is None
    assert row["consecutive_failures"] == 0
    assert row["last_rows"] == 0
    assert row["last_success_ms"] is not None
