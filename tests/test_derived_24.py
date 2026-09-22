"""The 2.4 derived metrics (item 6).

Each one is checked against a number from its own source rather than
against itself, because a formula that agrees with its own last run is a
formula nobody has checked.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import derived as d  # noqa: E402


def test_vapour_pressure_deficit_is_zero_at_saturation():
    assert d.vapour_pressure_deficit_kpa(70, 100) == 0.0
    # 25 °C (77 °F) at 50 % is the worked example in every greenhouse
    # note: saturation ~3.17 kPa, so the deficit is about 1.58.
    vpd = d.vapour_pressure_deficit_kpa(77, 50)
    assert 1.5 < vpd < 1.65
    # Drier air, bigger deficit, always.
    assert d.vapour_pressure_deficit_kpa(77, 20) > vpd
    assert d.vapour_pressure_deficit_kpa(77, 120) is None
    assert d.vapour_pressure_deficit_kpa(None, 50) is None


def test_humidex_matches_environment_canadas_own_example():
    # Their published worked example: 30 °C with a dew point of 15 °C
    # gives a humidex of 34.
    #  30 °C = 86 °F, and a 15 °C dew point at 30 °C is about 40 % RH.
    value = d.humidex(86.0, 40.0)
    assert 33 <= value <= 35
    # It is an index on the Celsius scale, and below about 20 it means
    # nothing, but the function still answers rather than inventing a
    # cutoff the caller did not ask for.
    assert d.humidex(50, 50) is not None
    assert d.humidex(86, None) is None


def test_apparent_temperature_reads_below_air_in_a_dry_breeze():
    """The reason to have it: heat index gives up below 80 °F and wind
    chill above 50 °F, and the middle of the year has neither."""
    still = d.apparent_temperature_f(75, 30, 0)
    windy = d.apparent_temperature_f(75, 30, 20)
    assert windy < still
    # Humid and still reads above the air temperature.
    assert d.apparent_temperature_f(85, 80, 0) > 85
    # No wind reading is calm, not missing: the formula still answers.
    assert d.apparent_temperature_f(75, 30, None) is not None


def test_cloud_base_is_the_spread_rule_and_says_nothing_else():
    # 4.4 °F of spread is 1000 ft.
    assert round(d.cloud_base_ft(70, 65.6)) == 1000
    assert round(d.cloud_base_ft(70, 61.2)) == 2000
    # Saturated air puts the base on the deck rather than below it.
    assert d.cloud_base_ft(60, 62) == 0.0
    assert d.cloud_base_ft(70, None) is None


def test_wind_run_adds_up_the_gaps_it_can_account_for():
    hour = 3_600_000
    # Ten mph for two hours is twenty miles.
    samples = [(0, 10.0), (hour, 10.0), (2 * hour, 10.0)]
    assert round(d.wind_run_mi(samples), 1) == 20.0
    # A gap longer than an hour is NOT bridged: a day's wind run that
    # silently includes guesses is worse than one that is short.
    gappy = [(0, 10.0), (6 * hour, 10.0), (7 * hour, 10.0)]
    assert round(d.wind_run_mi(gappy), 1) == 10.0
    assert d.wind_run_mi([]) is None
    assert d.wind_run_mi([(0, 5.0)]) is None


def test_sunshine_needs_both_a_floor_and_a_share_of_clear_sky():
    # Bright noon: well over both.
    assert d.is_sunshine(900, 1000) is True
    # Thin overcast at noon clears the 120 floor but not the share.
    assert d.is_sunshine(300, 1000) is False
    # Clear but low sun: the share is what carries it, not the floor.
    assert d.is_sunshine(150, 180) is True
    # Night has no clear-sky maximum to compare against.
    assert d.is_sunshine(0, 0) is None
    assert d.is_sunshine(None, 1000) is None


def test_aqi_matches_the_epa_breakpoints():
    assert d.aqi_pm25(0)[0] == 0
    # The top of Good is 9.0 µg/m³ → AQI 50.
    assert d.aqi_pm25(9.0) == (50, "Good")
    assert d.aqi_pm25(9.1)[1] == "Moderate"
    assert d.aqi_pm25(35.4) == (100, "Moderate")
    assert d.aqi_pm25(35.5)[1] == "Unhealthy for sensitive groups"
    assert d.aqi_pm25(55.5)[1] == "Unhealthy"
    assert d.aqi_pm25(500)[0] == 500
    assert d.aqi_pm25(-1) is None
    assert d.aqi_pm25(None) is None


def test_chill_hours_can_go_backwards_which_is_the_whole_point():
    assert d.chill_hours_utah(40) == 1.0
    assert d.chill_hours_utah(35) == 0.5
    assert d.chill_hours_utah(30) == 0.0        # too cold to count
    assert d.chill_hours_utah(50) == 0.5
    assert d.chill_hours_utah(58) == 0.0
    # A warm winter afternoon genuinely undoes chill.
    assert d.chill_hours_utah(63) == -0.5
    assert d.chill_hours_utah(70) == -1.0
    assert d.chill_hours_utah(None) is None


def test_evapotranspiration_is_a_summer_afternoon_not_a_number_at_night():
    # A hot, dry, sunny, breezy hour: FAO-56 hourly ET0 lands a few
    # hundredths of an inch. Anything near an inch an hour would be a
    # unit error, which is what this bound is for.
    et = d.evapotranspiration_in(95, 20, 8, 900, 29.92, hours=1)
    assert 0.01 < et < 0.06
    # Night: no sun, so almost nothing, and never negative.
    night = d.evapotranspiration_in(70, 80, 2, 0, 29.92, hours=1)
    assert 0 <= night < 0.01
    # A missing input is missing, not a zero.
    assert d.evapotranspiration_in(95, 20, 8, None, 29.92) is None
    assert d.evapotranspiration_in(95, 20, 8, 900, 29.92, hours=0) is None


def test_evapotranspiration_adds_up_the_same_however_finely_it_is_cut():
    # A station posting every five minutes hands the accumulator twelve
    # slices an hour; a Davis posting hourly hands it one. The hour's ET
    # must not depend on which. It did: the radiation term was scaled by
    # the step twice, so the finer the slices the less the sun counted,
    # until at ten-second steps a summer noon evaporated nothing.
    whole = d.evapotranspiration_in(100, 20, 5, 800, 29.0, hours=1)
    sliced = sum(d.evapotranspiration_in(100, 20, 5, 800, 29.0, hours=1 / 12)
                 for _ in range(12))
    assert abs(whole - sliced) / whole < 1e-6
    # And the sun is still a term: a sunny slice beats the same slice
    # with the sun switched off, at every step size.
    for hours in (1, 1 / 12, 1 / 360):
        sunny = d.evapotranspiration_in(100, 20, 5, 800, 29.0, hours=hours)
        dark = d.evapotranspiration_in(100, 20, 5, 0, 29.0, hours=hours)
        assert sunny > dark * 1.5


def test_solar_elevation_knows_noon_from_midnight():
    """Phoenix, a summer solstice. The sun is high at local noon and
    below the horizon at local midnight, and a clear-sky envelope built
    on it is zero at night rather than overcast."""
    lat, lon = 33.30, -111.94
    # 2026-06-21 19:00 UTC is noon in Phoenix (UTC-7, no DST).
    import datetime as dt
    noon_ms = int(dt.datetime(2026, 6, 21, 19, 0,
                              tzinfo=dt.timezone.utc).timestamp() * 1000)
    midnight_ms = int(dt.datetime(2026, 6, 21, 7, 0,
                                  tzinfo=dt.timezone.utc).timestamp() * 1000)
    high = d.solar_elevation_deg(noon_ms, lat, lon)
    low = d.solar_elevation_deg(midnight_ms, lat, lon)
    assert 75 < high < 85          # within a few degrees of overhead
    assert low < 0
    assert d.clear_sky_wm2(noon_ms, lat, lon) > 900
    assert d.clear_sky_wm2(midnight_ms, lat, lon) == 0.0
    # A sensible refusal rather than a wrong number.
    assert d.solar_elevation_deg(noon_ms, 200, 0) is None
    assert d.solar_elevation_deg(noon_ms, None, 0) is None


def test_the_derived_route_carries_them_and_omits_what_it_cannot_know(client):
    """A station with no solar head has no sunshine hours, which is not
    the same as a day with no sun — so the key is absent rather than
    zero."""
    H = {"Authorization": "Bearer test-api-token"}
    ing = {"Authorization": "Bearer test-ingest-token"}
    client.post("/ingest/custom", headers=ing, json={
        "device": {"id": "AABBCC000061", "model": "Derived"},
        "timestamp_utc": "2026-05-14T20:00:00Z",
        "outdoor": {"tempf": 95.0, "humidity": 20.0},
        "wind": {"speed_mph": 8.0},
        "pressure": {"relative_inhg": 29.92, "absolute_inhg": 28.5},
        "source": "test"})
    r = client.get("/api/devices/AA:BB:CC:00:00:61/derived", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    # Instantaneous ones, from the reading itself.
    assert body["vapourPressureDeficitKpa"] > 3.0
    assert "humidex" in body
    assert "apparentTemperatureF" in body
    # No PM sensor, so no AQI at all rather than a confident Good.
    assert "aqiPm25" not in body
    # No solar head, so no sunshine and no evapotranspiration.
    assert "sunshineHoursToday" not in body
    assert "evapotranspirationInToday" not in body


def test_the_accumulated_metrics_actually_arrive(client):
    """The route answered 200 with every accumulated key absent, for
    every station: `derived` was never bound inside `_accumulated_today`,
    the NameError was swallowed by the caller's except, and the test
    above only checked that keys were ABSENT for a station without the
    sensor, which a broken function also satisfies (CodeRabbit, PR #40).
    Three readings a minute apart, this hour, and the wind run and the
    chill hours must be there."""
    import datetime as dt
    H = {"Authorization": "Bearer test-api-token"}
    ing = {"Authorization": "Bearer test-ingest-token"}
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    for minutes in (3, 2, 1):
        at = now - dt.timedelta(minutes=minutes)
        r = client.post("/ingest/custom", headers=ing, json={
            "device": {"id": "AABBCC000062", "model": "Derived"},
            "timestamp_utc": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outdoor": {"tempf": 40.0, "humidity": 50.0},
            "wind": {"speed_mph": 12.0},
            "source": "test"})
        assert r.status_code == 200, r.text
    body = client.get("/api/devices/AA:BB:CC:00:00:62/derived", headers=H).json()
    assert body.get("windRunMiToday", 0) > 0, body
    assert "chillHoursToday" in body, body


def test_a_high_cadence_day_is_summed_to_its_end(client, monkeypatch):
    """R24-04 (2.4 release review): the accumulation stopped at 20 000
    rows without saying so, so twelve hours at two-second cadence gave
    111.1 miles of wind run at a constant 10 mph instead of 120.0."""
    import asyncio
    import time as _time
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app import db, main
    H = {"Authorization": "Bearer test-api-token"}
    ing = {"Authorization": "Bearer test-ingest-token"}
    client.post("/ingest/custom", headers=ing, json={
        "device": {"id": "AABBCC000063", "model": "Derived"},
        "timestamp_utc": "2026-09-20T00:00:00Z",
        "outdoor": {"tempf": 70.0}, "source": "test"})
    mac = "AA:BB:CC:00:00:63"
    base = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp() * 1000)

    async def run():
        rows = [{"dateutc": base + i * 2000, "tempf": 60.0, "windspeedmph": 10.0}
                for i in range(21_601)]
        assert await db.insert_observations(mac, rows) == 21_601
        monkeypatch.setattr(main, "time", SimpleNamespace(
            **(vars(_time) | {"time": lambda: (base + 12 * 3_600_000 + 1) / 1000})))
        out: dict = {}
        await main._accumulated_today(mac, {}, out)
        return out
    out = asyncio.run(run())
    assert out["windRunMiToday"] == 120.0


def test_the_window_reader_pages_to_the_end(client):
    """db.observation_window pages observation_rows until the window is
    exhausted; 4 999, 5 000 and 5 001 rows all come back whole."""
    import asyncio
    from app import db
    mac = "AA:BB:CC:00:00:65"
    async def run():
        for n in (4_999, 5_000, 5_001, 10_001):
            m = f"AA:BB:CC:00:{n // 100:02X}:{n % 100:02X}"
            rows = [{"dateutc": 1_700_000_000_000 + i * 1000, "tempf": 50.0}
                    for i in range(n)]
            assert await db.insert_observations(m, rows) == n
            got = await db.observation_window(m, 0, 1_800_000_000_000)
            assert len(got) == n, n
            assert got[-1]["dateutc_ms"] == rows[-1]["dateutc"]
    asyncio.run(run())
