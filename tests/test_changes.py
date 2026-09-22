"""The weather-change timeline (2.4 item 9).

The detector is pure, so almost everything here builds a window of rows
by hand and reads the list back. The two rules the repo keeps
re-learning are both tested directly: absent is not zero (a station with
no barometer has no pressure turn, not a flat one), and a wind direction
is an ANGLE (a scalar mean of 350 and 10 is 180, the opposite way).
"""
from __future__ import annotations

import asyncio

from app import changes

MINUTE = 60_000
T0 = 1_757_000_000_000     # a fixed epoch, so nothing here depends on today


def rows(n: int = 60, step_min: int = 5, **series) -> list[dict]:
    """n rows, `step_min` apart, each field either a constant or a list."""
    out = []
    for i in range(n):
        row = {"dateutc_ms": T0 + i * step_min * MINUTE}
        for key, value in series.items():
            v = value[i] if isinstance(value, list) else value
            if v is not None:
                row[key] = v
        out.append(row)
    return out


def kinds(found):
    return [c["kind"] for c in found]


def test_a_short_window_says_nothing():
    assert changes.detect(rows(3, tempf=70.0)) == []


def test_every_kind_is_declared():
    found = changes.detect(rows(
        60, tempf=[60.0 + i for i in range(60)],
        dailyrainin=[0.0] * 20 + [0.05 * (i - 19) for i in range(20, 30)]
                    + [0.5] * 30,
        baromrelin=[30.0 - 0.01 * i for i in range(30)]
                   + [29.7 + 0.01 * i for i in range(30)],
        windgustmph=[10.0] * 30 + [40.0] + [10.0] * 29,
    ))
    assert found, "this window changes in several ways"
    for c in found:
        assert c["kind"] in changes.KINDS, c


def test_rain_start_and_stop_come_from_the_day_counter():
    # Dry, then the counter advances for half an hour, then dry again for
    # long enough that the shower is genuinely over.
    total = [0.0] * 10 + [0.01 * (i - 9) for i in range(10, 16)] + [0.06] * 44
    found = changes.detect(rows(60, dailyrainin=total, tempf=70.0))
    assert kinds(found).count("rain_started") == 1
    assert kinds(found).count("rain_stopped") == 1
    started = next(c for c in found if c["kind"] == "rain_started")
    stopped = next(c for c in found if c["kind"] == "rain_stopped")
    assert started["at_ms"] < stopped["at_ms"]


def test_rain_still_falling_at_the_end_has_no_stop():
    total = [0.0] * 10 + [0.01 * (i - 9) for i in range(10, 60)]
    found = changes.detect(rows(60, dailyrainin=total, tempf=70.0))
    assert "rain_started" in kinds(found)
    assert "rain_stopped" not in kinds(found)


def test_a_counter_that_falls_is_midnight_not_rain():
    # 0.40 in through the day, then the counter resets. A reset is not a
    # shower and is not negative rain (ref_rain_counters).
    total = [0.40] * 30 + [0.0] * 30
    found = changes.detect(rows(60, dailyrainin=total, tempf=70.0))
    assert "rain_started" not in kinds(found)


def test_two_showers_an_hour_apart_are_two_starts():
    total = [0.0] * 5 + [0.05] + [0.05] * 19 + [0.10] + [0.10] * 34
    found = changes.detect(rows(60, dailyrainin=total, tempf=70.0))
    assert kinds(found).count("rain_started") == 2


def test_no_barometer_is_no_pressure_turn():
    found = changes.detect(rows(60, tempf=70.0))
    assert "pressure_turn" not in kinds(found)


def test_a_real_pressure_turn_is_found_and_named():
    # Four hours down, four hours up, five minutes apart.
    down = [30.10 - 0.005 * i for i in range(48)]
    up = [down[-1] + 0.005 * i for i in range(48)]
    found = changes.detect(rows(96, dailyrainin=None,
                                baromrelin=down + up, tempf=70.0))
    turns = [c for c in found if c["kind"] == "pressure_turn"]
    assert len(turns) == 1
    assert "up" in turns[0]["title"]
    assert turns[0]["value"] > 0
    # Stamped at the barometer's LOW, which is where it turned (2.4
    # review). The trailing three-hour delta only changes sign about
    # ninety minutes after the minimum and clears the threshold later
    # still, and a timeline whose whole point is "when" said "Pressure
    # turned up" a hundred minutes late.
    low_at = T0 + 48 * 5 * MINUTE
    assert abs(turns[0]["at_ms"] - low_at) <= 10 * MINUTE


def test_a_turn_down_is_stamped_at_the_high():
    up = [29.80 + 0.005 * i for i in range(48)]
    down = [up[-1] - 0.005 * i for i in range(48)]
    found = changes.detect(rows(96, dailyrainin=None,
                                baromrelin=up + down, tempf=70.0))
    turns = [c for c in found if c["kind"] == "pressure_turn"]
    assert len(turns) == 1 and "down" in turns[0]["title"]
    assert abs(turns[0]["at_ms"] - (T0 + 48 * 5 * MINUTE)) <= 10 * MINUTE


def test_a_flat_barometer_never_turns():
    flat = [30.00, 30.00, 30.01, 30.00, 29.99, 30.00] * 16
    found = changes.detect(rows(96, baromrelin=flat, tempf=70.0))
    assert "pressure_turn" not in kinds(found)


def test_wind_shift_is_measured_as_an_angle():
    # North (350) for an hour, then east (90). A scalar mean of 350 and
    # 90 is 220, which is south west — the opposite side of the compass
    # from where this wind ever blew.
    direction = [350.0] * 30 + [90.0] * 30
    found = changes.detect(rows(60, step_min=2, winddir=direction,
                                windspeedmph=8.0, tempf=70.0))
    shifts = [c for c in found if c["kind"] == "wind_shift"]
    assert len(shifts) == 1
    assert shifts[0]["detail"].startswith("About 100 degrees"), shifts[0]
    assert shifts[0]["title"] == "Wind swung north to east"


def test_calm_air_has_no_direction_to_shift():
    direction = [0.0] * 30 + [180.0] * 30
    found = changes.detect(rows(60, step_min=2, winddir=direction,
                                windspeedmph=0.5, tempf=70.0))
    assert "wind_shift" not in kinds(found)


def test_the_peak_gust_has_to_be_worth_a_line():
    quiet = changes.detect(rows(60, windgustmph=6.0, tempf=70.0))
    assert "gust_peak" not in kinds(quiet)
    windy = changes.detect(rows(
        60, windgustmph=[8.0] * 30 + [41.0] + [8.0] * 29, tempf=70.0))
    peak = next(c for c in windy if c["kind"] == "gust_peak")
    assert peak["value"] == 41.0 and peak["unit"] == "mph"


def test_temperature_turning_points_need_a_real_swing():
    flat = changes.detect(rows(60, tempf=[70.0 + (i % 3) for i in range(60)]))
    assert "temp_peak" not in kinds(flat)
    swung = changes.detect(rows(60, tempf=[50.0 + i for i in range(60)]))
    assert kinds(swung).count("temp_peak") == 1
    assert kinds(swung).count("temp_low") == 1


def test_the_sun_needs_coordinates():
    solar = [0.0] * 30 + [900.0] * 30
    assert "cleared" not in kinds(changes.detect(
        rows(60, solarradiation=solar, tempf=70.0)))


def test_clearing_is_a_sustained_crossing_of_the_clear_sky_envelope():
    # Midday over Chandler, five minutes apart: overcast, then clear and
    # STAYING clear. 2026-09-21 18:00Z is late morning there.
    noon = 1_758_477_600_000
    solar = [60.0] * 20 + [900.0] * 20
    found = changes.detect(
        [{"dateutc_ms": noon + i * 5 * MINUTE, "solarradiation": solar[i],
          "tempf": 85.0} for i in range(40)],
        lat=33.3, lon=-111.84)
    assert kinds(found).count("cleared") == 1


def test_one_cloud_crossing_the_sun_is_not_a_change():
    noon = 1_758_477_600_000
    solar = [900.0] * 20 + [60.0] * 2 + [900.0] * 18
    found = changes.detect(
        [{"dateutc_ms": noon + i * 5 * MINUTE, "solarradiation": solar[i],
          "tempf": 85.0} for i in range(40)],
        lat=33.3, lon=-111.84)
    assert "clouded_over" not in kinds(found)
    assert "cleared" not in kinds(found)


def test_a_night_of_zero_solar_is_not_an_overcast_day():
    midnight = 1_758_434_400_000      # 2026-09-21 06:00Z, night in Chandler
    found = changes.detect(
        [{"dateutc_ms": midnight + i * 5 * MINUTE, "solarradiation": 0.0,
          "tempf": 70.0} for i in range(40)],
        lat=33.3, lon=-111.84)
    assert "clouded_over" not in kinds(found)


def test_the_timeline_is_ordered_and_capped():
    found = changes.detect(rows(
        200, step_min=5, tempf=[50.0 + (i % 40) for i in range(200)],
        baromrelin=[30.0 + 0.01 * ((i // 20) % 2 * 2 - 1) * (i % 20)
                    for i in range(200)],
        windgustmph=[20.0 + (i % 30) for i in range(200)],
        dailyrainin=[0.01 * i for i in range(200)]))
    assert len(found) <= changes.MAX_CHANGES
    assert found == sorted(found, key=lambda c: c["at_ms"])


def test_no_prose_carries_a_number_in_units_the_reader_may_not_use():
    """The F04 rule (2.3): a value the app has to re-say in °C or km/h
    rides `value` with its `unit`, never a formatted string the app can
    only show as sent. Degrees and the counts are unit-neutral."""
    found = changes.detect(rows(
        96, tempf=[50.0 + i for i in range(96)],
        baromrelin=[30.10 - 0.005 * i for i in range(48)]
                   + [29.86 + 0.005 * i for i in range(48)],
        windgustmph=[8.0] * 48 + [41.0] + [8.0] * 47))
    assert found
    for c in found:
        assert c["unit"] is None or c["unit"] in changes.UNITS, c
        for text in (c["title"], c["detail"]):
            for banned in ("°", " mph", " in ", " inHg"):
                assert banned not in text, (banned, c)


def test_compass_names_the_eight_points():
    assert changes.compass(0) == "north"
    assert changes.compass(359) == "north"
    assert changes.compass(90) == "east"
    assert changes.compass(225) == "south west"


def test_the_route_answers_for_a_station_with_no_history(client):
    r = client.get("/api/devices/AA:BB:CC:DD:EE:01/changes",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["changes"] == []
    assert body["hours"] == changes.WINDOW_HOURS_DEFAULT
    assert body["to_ms"] - body["from_ms"] == 24 * 3_600_000


def test_the_route_refuses_an_absurd_window(client):
    r = client.get("/api/devices/AA:BB:CC:DD:EE:01/changes?hours=500",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 422


def test_the_route_needs_a_token(client):
    assert client.get("/api/devices/AA:BB:CC:DD:EE:01/changes").status_code == 401


def test_the_mcp_tool_answers_for_a_known_station(client):
    """The timeline is a tool too (2.4): it reads raw observations, so
    unlike records or stories it answers on a server with insights off."""
    mac = "AA:BB:CC:DD:EE:02"
    r = client.post("/ingest/custom",
                    json={"device": {"id": mac, "model": "Test"},
                          "timestamp_utc": "2026-09-21T18:00:00Z",
                          "outdoor": {"tempf": 88.0, "humidity": 20},
                          "source": "test"},
                    headers={"Authorization": "Bearer test-ingest-token"})
    assert r.status_code == 200, r.text
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "weather_changes",
                   "arguments": {"mac": mac, "hours": 6}}},
        headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    assert body["result"].get("isError") is not True, body


def test_the_mcp_tool_refuses_an_unknown_station(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "weather_changes",
                   "arguments": {"mac": "11:22:33:44:55:66"}}},
        headers={"Authorization": "Bearer test-api-token"})
    assert r.json()["result"]["isError"] is True


def test_a_three_day_window_at_minute_density_is_not_quadratic():
    """The cap is 72 hours and a station posts every minute, so the
    worst case is ~4300 rows. Both the pressure turn and the wind shift
    started out rescanning the window per row, which is tens of millions
    of steps inside an async handler — the shape of the /current rain
    scan this release deleted. Linear now, and this is the guard."""
    import time as _t

    n = 72 * 60
    # Solar that disagrees with itself every few minutes, with
    # coordinates, so the sun detector runs too: its "does the new
    # state hold" check walked to the end of the window on every
    # disagreeing sample (2.4 review).
    window = rows(n, step_min=1,
                  tempf=[70.0 + (i % 30) for i in range(n)],
                  baromrelin=[30.0 + 0.001 * (i % 200) for i in range(n)],
                  winddir=[(i * 7) % 360 for i in range(n)],
                  windspeedmph=[9.0] * n,
                  windgustmph=[15.0 + (i % 20) for i in range(n)],
                  solarradiation=[900.0 if (i // 3) % 2 else 40.0
                                  for i in range(n)],
                  dailyrainin=[0.0] * n)
    began = _t.monotonic()
    changes.detect(window, lat=33.3, lon=-111.9)
    took = _t.monotonic() - began
    assert took < 2.0, f"a full window took {took:.1f}s"
    # And the same fixture, sustained, does find the sun — the guard
    # above is not exercising an early return.
    n = 8 * 60
    day = rows(n, step_min=1, tempf=80.0,
               solarradiation=[40.0] * (4 * 60) + [900.0] * (4 * 60))
    for r in day:
        r["dateutc_ms"] += 3_600_000     # T0 is 08:33 in Phoenix; start at 09:33
    assert "cleared" in kinds(changes.detect(day, lat=33.3, lon=-111.9))


def test_rain_stopped_needs_rain_readings_not_just_time(monkeypatch):
    """R24-06 (2.4 release review): after the counter moved, thirty
    minutes of temperature-only rows made the timeline say "Rain
    stopped". A disconnected gauge is not dry weather: the stop clock
    runs on the newest row that carried a rain reading."""
    r = [{"dateutc_ms": T0, "dailyrainin": 0.0},
         {"dateutc_ms": T0 + MINUTE, "dailyrainin": 0.01}]
    r += [{"dateutc_ms": T0 + i * MINUTE, "tempf": 70.0} for i in range(2, 31)]
    assert "rain_stopped" not in kinds(changes.detect(r))
    assert "rain_started" in kinds(changes.detect(r))
    # Unchanged rain readings for the same stretch DO stop it.
    r2 = [{"dateutc_ms": T0, "dailyrainin": 0.0},
          {"dateutc_ms": T0 + MINUTE, "dailyrainin": 0.01}]
    r2 += [{"dateutc_ms": T0 + i * MINUTE, "dailyrainin": 0.01} for i in range(2, 31)]
    assert "rain_stopped" in kinds(changes.detect(r2))


def test_a_sun_hold_must_be_observed_to_the_end_of_the_span(monkeypatch):
    """R24-07 (2.4 release review): five cloudy minutes then three sunny
    ones said "Cleared up" at once, though the sunny readings spanned two
    minutes. Three agreeing points are not twenty minutes; the last mark
    inside the span has to reach its end."""
    monkeypatch.setattr(changes.derived, "clear_sky_wm2", lambda *a: 1000.0)
    short = [{"dateutc_ms": T0 + i * MINUTE,
              "solarradiation": 100.0 if i < 5 else 900.0} for i in range(8)]
    assert "cleared" not in kinds(changes.detect(short, lat=33.0, lon=-112.0))
    full = [{"dateutc_ms": T0 + i * MINUTE,
             "solarradiation": 100.0 if i < 5 else 900.0} for i in range(30)]
    assert "cleared" in kinds(changes.detect(full, lat=33.0, lon=-112.0))
    # Sparse but covering: three points at 0, 10 and 19 minutes hold.
    sparse = [{"dateutc_ms": T0 + m * MINUTE, "solarradiation": 100.0}
              for m in (0, 1, 2, 3, 4)]
    sparse += [{"dateutc_ms": T0 + (5 + m) * MINUTE, "solarradiation": 900.0}
               for m in (0, 10, 19)]
    assert "cleared" in kinds(changes.detect(sparse, lat=33.0, lon=-112.0))


def test_a_high_cadence_window_is_read_to_its_end(client):
    """R24-04 (2.4 release review): assemble read one 5000-row batch, so
    a station posting every two seconds had the newest hours of its
    window missing and the timeline still claimed the window's full
    end. The gust at the very last row must be visible."""
    from app import db
    mac = "AA:BB:CC:00:00:64"
    async def run():
        rows = [{"dateutc": T0 + i * 2000, "tempf": 70.0, "windspeedmph": 10.0,
                 "windgustmph": 12.0} for i in range(21_600)]
        rows.append({"dateutc": T0 + 21_600 * 2000, "tempf": 70.0,
                     "windspeedmph": 10.0, "windgustmph": 40.0})
        assert await db.insert_observations(mac, rows) == 21_601
        out = await changes.assemble(mac, 24, now_ms=T0 + 21_600 * 2000 + 1)
        gusts = [c for c in out["changes"] if c["kind"] == "gust_peak"]
        assert gusts and gusts[-1]["value"] == 40.0
        assert gusts[-1]["at_ms"] == T0 + 21_600 * 2000
    asyncio.run(run())
