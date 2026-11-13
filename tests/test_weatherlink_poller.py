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


def _current(sensor_type=43, iss=None, data_structure_type=None):
    sensors = []
    if iss is not None:
        s = {"sensor_type": sensor_type, "data": [iss]}
        if data_structure_type is not None:
            s["data_structure_type"] = data_structure_type
        sensors.append(s)
    sensors.append({"sensor_type": 365,
                    "data": [{"temp_in": 78.0, "hum_in": 40.4}]})
    sensors.append({"sensor_type": 242,
                    "data": [{"bar_sea_level": 29.92}]})
    return {"sensors": sensors}


def _vue_wll_cloud(**over):
    """A Vantage Vue behind a WeatherLink Live as /v2/current reports it
    (data_structure_type 10): a sensor_type the ISS set never listed, and
    rain as bucket-tip COUNTS with a rain_size code (2 = 0.2 mm tips)."""
    d = {"ts": int(time.time()), "tx_id": 1,
         "temp": 80.6, "hum": 65.0, "dew_point": 68.0, "heat_index": 84.0,
         "wind_speed_last": 3.0, "wind_dir_last": 90,
         "wind_speed_avg_last_2_min": 4.0, "wind_speed_hi_last_2_min": 9.0,
         "wind_dir_scalar_avg_last_2_min": 95.0,
         "rain_size": 2, "rain_rate_last": 0, "rainfall_last_60_min": 3,
         "rainfall_daily": 25, "rainfall_monthly": 400, "rainfall_year": 9000,
         "uv_index": 6.0, "solar_rad": 650}
    d.update(over)
    return d


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


def test_a_vantage_vue_behind_a_weatherlink_live_is_the_iss(wl):
    """Thierry, 2026-11-12: keys and station id right, green check, and
    "no station yet" for hours. His Vue's record carried sensor_type 37
    and data_structure_type 10; the ISS set was {43, 46} and the poller
    recorded success with zero rows. The structure names the record."""
    cur = _current(sensor_type=37, iss=_vue_wll_cloud(), data_structure_type=10)
    payload = wl.build_payload({"station_name": "Vue"}, cur)
    assert payload is not None, "a Vue through a WLL must be ISS data"
    assert payload["outdoor"]["tempf"] == 80.6
    assert payload["wind"]["speed_mph"] == 4.0 and payload["wind"]["gust_mph"] == 9.0
    # 0.2 mm tips: 25 tips = 5.0 mm = 0.19685 in; the month and year too.
    assert payload["rain"]["daily_in"] == pytest.approx(25 * 0.2 / 25.4)
    assert payload["rain"]["hourly_in"] == pytest.approx(3 * 0.2 / 25.4)
    assert payload["rain"]["monthly_in"] == pytest.approx(400 * 0.2 / 25.4)
    assert payload["rain"]["yearly_in"] == pytest.approx(9000 * 0.2 / 25.4)
    assert payload["indoor"]["tempf"] == 78.0 and payload["pressure"]["relative_inhg"] == 29.92


def test_a_wll_record_spells_txid_and_may_carry_no_ts(wl):
    """Read live from a WeatherLink Live on 2026-11-12: the transmitter is
    `txid` (the console says `tx_id`) and the record has no `ts` of its
    own; the answer's `generated_at` is the time then."""
    iss = _vue_wll_cloud(); iss.pop("ts"); iss.pop("tx_id"); iss["txid"] = 3
    cur = _current(sensor_type=37, iss=iss, data_structure_type=10)
    cur["generated_at"] = 1_700_000_000
    payload = wl.build_payload({}, cur)
    assert payload is not None
    assert payload["device"]["id"] == wl._synth_mac(3)
    assert payload["timestamp_utc"].startswith("2023-11-14T22:13:20")
    cur.pop("generated_at")
    assert wl.build_payload({}, cur) is None, "no time anywhere is still no reading"


def test_rain_size_codes_scale_the_tip_counts(wl):
    for code, inches_per_tip in ((1, 0.01), (2, 0.2 / 25.4), (3, 0.1 / 25.4), (4, 0.001)):
        cur = _current(sensor_type=37, iss=_vue_wll_cloud(rain_size=code, rainfall_daily=10),
                       data_structure_type=10)
        assert wl.build_payload({}, cur)["rain"]["daily_in"] == pytest.approx(10 * inches_per_tip), code
    # No rain_size at all reads as the US 0.01" bucket, never as zero rain.
    cur = _current(sensor_type=37, iss=_vue_wll_cloud(rain_size=None, rainfall_daily=10), data_structure_type=10)
    assert wl.build_payload({}, cur)["rain"]["daily_in"] == pytest.approx(0.10)


def test_the_console_record_still_reads_inches_first(wl):
    """The 6313 console's `_in` fields win over a counts field if both
    ever appear, and a record with ONLY the counts still converts."""
    both = _iss_data(rainfall_daily=999, rain_size=1)
    assert wl.build_payload({}, _current(iss=both))["rain"]["daily_in"] == 0.5
    cur = _current(sensor_type=99, iss=_iss_data(), data_structure_type=23)
    assert wl.build_payload({}, cur) is not None, "data_structure_type 23 is the console ISS"


def test_an_unlisted_record_that_looks_like_an_iss_is_one(wl):
    """No sensor_type we know, no structure we know, but an outdoor
    temperature and a wind field: the next catalog entry Davis adds must
    not be a repeat of this bug."""
    cur = _current(sensor_type=12345, iss=_vue_wll_cloud(), data_structure_type=None)
    assert wl.build_payload({}, cur) is not None


def test_classify_sensor_keeps_indoor_and_barometer_apart(wl):
    assert wl.classify_sensor({"sensor_type": 365, "data": [{"temp_in": 70}]}) == "indoor"
    # Through the cloud, Davis's 12 is a GENERIC non-ISS record: the fields
    # decide, never the number (CodeRabbit, PR #153).
    assert wl.classify_sensor({"sensor_type": 1, "data_structure_type": 12, "data": [{"temp_in": 70}]}) == "indoor"
    assert wl.classify_sensor({"sensor_type": 1, "data_structure_type": 12, "data": [{"bar_sea_level": 30}]}) == "barom"
    assert wl.classify_sensor({"sensor_type": 1, "data_structure_type": 12, "data": [{"battery": 1}]}) is None
    # The console names its own: 19 barometer, 21 inside temp/hum.
    assert wl.classify_sensor({"sensor_type": 1, "data_structure_type": 21, "data": [{}]}) == "indoor"
    assert wl.classify_sensor({"sensor_type": 1, "data_structure_type": 19, "data": [{}]}) == "barom"
    assert wl.classify_sensor({"sensor_type": 500, "data": [{"battery": 1}]}) is None


def test_a_later_non_barometer_record_never_replaces_the_barometer(wl):
    """CodeRabbit, PR #153: a structure-12 record without pressure that
    follows the real barometer must not blank pressure for that poll."""
    cur = _current(sensor_type=37, iss=_vue_wll_cloud(), data_structure_type=10)
    # A generic structure-12 record is not a barometer at all (fields decide).
    cur["sensors"].append({"sensor_type": 500, "data_structure_type": 12, "data": [{"battery": 1}]})
    # A record that IS a barometer by type but carries no pressure this poll
    # (an empty console record, structure 19) must not blank the one before.
    cur["sensors"].append({"sensor_type": 242, "data_structure_type": 19, "data": [{"bar_trend": 0}]})
    cur["sensors"].append({"sensor_type": 365, "data_structure_type": 21, "data": [{"hum_in": 1}]})
    payload = wl.build_payload({}, cur)
    assert payload["pressure"]["relative_inhg"] == 29.92
    assert payload["indoor"]["pressure_inhg"] == 29.92 and payload["indoor"]["tempf"] == 78.0


def test_an_auxiliary_transmitter_first_in_the_answer_does_not_take_the_iss_slot(wl):
    """CodeRabbit, PR #153: `sensors[]` has no documented order and an
    auxiliary transmitter reports as structure 10 too. The richest record
    is the ISS whatever the order; the aux's id never names the device."""
    aux = {"ts": int(time.time()), "tx_id": 2, "temp": 60.0, "hum": 70.0}
    cur = {"sensors": [{"sensor_type": 37, "data_structure_type": 10, "data": [aux]},
                       {"sensor_type": 37, "data_structure_type": 10, "data": [_vue_wll_cloud(tx_id=1)]}]}
    payload = wl.build_payload({}, cur)
    assert payload["outdoor"]["tempf"] == 80.6 and payload["device"]["id"] == wl._synth_mac(1)
    cur["sensors"].reverse()
    assert wl.build_payload({}, cur)["device"]["id"] == wl._synth_mac(1)
    # Two equally rich records: a known ISS sensor_type wins, then the lower id.
    cur = {"sensors": [{"sensor_type": 37, "data_structure_type": 10, "data": [_vue_wll_cloud(tx_id=2)]},
                       {"sensor_type": 43, "data_structure_type": 10, "data": [_vue_wll_cloud(tx_id=5)]}]}
    assert wl.build_payload({}, cur)["device"]["id"] == wl._synth_mac(5)
    cur = {"sensors": [{"sensor_type": 37, "data_structure_type": 10, "data": [_vue_wll_cloud(tx_id=2)]},
                       {"sensor_type": 37, "data_structure_type": 10, "data": [_vue_wll_cloud(tx_id=1)]}]}
    assert wl.build_payload({}, cur)["device"]["id"] == wl._synth_mac(1)
    # A richer record WITHOUT a temperature (wind + rain + humidity, tx 2)
    # must not win the slot and take the poll's reading down with it
    # (CodeRabbit, PR #153): the record with a temperature is the ISS.
    windy = {"ts": int(time.time()), "tx_id": 2, "hum": 70.0, "wind_speed_last": 9.0,
             "wind_speed_avg_last_2_min": 8.0, "rainfall_daily": 4, "rain_size": 1, "solar_rad": 100}
    plain = {"ts": int(time.time()), "tx_id": 1, "temp": 61.0, "hum": 50.0}
    cur = {"sensors": [{"sensor_type": 37, "data_structure_type": 10, "data": [windy]},
                       {"sensor_type": 37, "data_structure_type": 10, "data": [plain]}]}
    payload = wl.build_payload({}, cur)
    assert payload is not None and payload["outdoor"]["tempf"] == 61.0
    assert payload["device"]["id"] == wl._synth_mac(1)
    # Two records and NEITHER has a temperature (a thermometer fault): the
    # richer one still goes through with its wind and rain, rather than the
    # poll storing nothing (CodeRabbit, PR #153).
    thin = {"ts": int(time.time()), "tx_id": 3, "hum": 40.0}
    cur = {"sensors": [{"sensor_type": 37, "data_structure_type": 10, "data": [thin]},
                       {"sensor_type": 37, "data_structure_type": 10, "data": [windy]}]}
    payload = wl.build_payload({}, cur)
    assert payload is not None and "tempf" not in payload.get("outdoor", {})
    assert payload["wind"]["speed_mph"] == 8.0 and payload["rain"]["daily_in"] == pytest.approx(0.04)
    assert payload["device"]["id"] == wl._synth_mac(2)
    assert wl.describe_sensors({"sensors": [{"sensor_type": 37, "data_structure_type": 10},
                                            {"sensor_type": 242}]}) == "37/10, 242/None"
    assert wl.describe_sensors({}) == "no sensors at all"


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
