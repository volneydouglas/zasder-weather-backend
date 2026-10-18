"""Daily ETo history and the water balance (2.7, docs/2.7/WATER_BALANCE.md).

The stored daily ET (app/daily_et.py), its method choice, the nightly job,
the lazy fill, the invalidations, the two routes, and the Watering card
reading the same stored values.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import daily_et, db, derived, watering  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:E7:00:00:00:01"
MAC2 = "AA:E7:00:00:00:02"
LAT, LON = 33.3, -111.8


def _ms(day: date, hour: float = 0.0) -> int:
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(base.timestamp() * 1000 + hour * 3_600_000)


async def _device(mac: str = MAC, located: bool = True) -> None:
    info = {"coords": {"coords": {"lat": LAT, "lon": LON}}} if located else {}
    await db.upsert_device(mac, {"name": "Yard", "info": info})


async def _rollup(mac: str, day: date, *, tmin=None, tmax=None, rain=None,
                  solar_max=None, span_h: float | None = None) -> None:
    first = last = None
    if span_h is not None:
        first, last = _ms(day, 0.0), _ms(day, span_h)
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, "
            "tempf_max, rain_total, solarradiation_max, obs_first_ms, "
            "obs_last_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mac, day.isoformat(), tmin, tmax, rain, solar_max, first, last))
        await conn.commit()


async def _raw_day(mac: str, day: date, until_h: float,
                   step_min: int = 15) -> None:
    """Every input Penman-Monteith needs, from 00:00 to `until_h`
    inclusive. The integral spans exactly `until_h` hours: the first
    reading has no step before it."""
    rows = []
    k = 0
    while k * step_min / 60 <= until_h + 1e-9:
        h = k * step_min / 60
        sun = max(0.0, 900.0 * (1 - abs(h - 12.5) / 6.5))
        rows.append({"dateutc": _ms(day, h), "tempf": 75.0 + 20 * sun / 900,
                     "humidity": 25.0, "windspeedmph": 6.0,
                     "solarradiation": sun, "baromabsin": 28.6})
        k += 1
    await db.insert_observations(mac, rows, received=False)


async def _put(mac: str, day: date, et: float, method: str) -> None:
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO daily_et (mac, day, et_in, method, "
            "computed_ms) VALUES (?, ?, ?, ?, 1)",
            (mac, day.isoformat(), et, method))
        await conn.commit()


async def _rows(mac: str = MAC) -> dict[str, tuple[float, str]]:
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT day, et_in, method FROM daily_et WHERE mac = ? "
            "ORDER BY day", (mac,))).fetchall()
    return {r["day"]: (r["et_in"], r["method"]) for r in rows}


def _yesterday() -> date:
    return daily_et.local_today() - timedelta(days=1)


# ── the shared integration ───────────────────────────────────────────────

def test_the_integration_counts_only_steps_of_an_hour_or_less():
    row = {"tempf": 90.0, "humidity": 20.0, "windspeedmph": 5.0,
           "solarradiation": 800.0, "baromabsin": 28.6}
    t0 = 1_790_000_000_000
    rows = [dict(row, dateutc_ms=t0), dict(row, dateutc_ms=t0 + 3_600_000),
            # a 90-minute gap integrates nothing
            dict(row, dateutc_ms=t0 + 3_600_000 + 5_400_000),
            dict(row, dateutc_ms=t0 + 3_600_000 + 5_400_000 + 1_800_000)]
    et, hours = derived.evapotranspiration_over(rows)
    assert hours == pytest.approx(1.5)
    one_hour = derived.evapotranspiration_in(90.0, 20.0, 5.0, 800.0, 28.6)
    assert et == pytest.approx(one_hour * 1.5)
    # No solar head: nothing at all, not zero.
    assert derived.evapotranspiration_over(
        [dict(r, solarradiation=None) for r in rows]) is None


# ── method choice ────────────────────────────────────────────────────────

def test_penman_monteith_at_the_coverage_threshold_hargreaves_just_below(client):
    """PM_MIN_HOURS is the line: a day whose readings span exactly 20 h is
    Penman-Monteith, one 15 minutes short is Hargreaves from the rollup."""
    assert daily_et.PM_MIN_HOURS == 20.0
    day = _yesterday() - timedelta(days=3)

    async def go():
        for mac in (MAC, MAC2):
            await _device(mac)
            await _rollup(mac, day, tmin=75, tmax=95, rain=0, solar_max=900,
                          span_h=23.9)
        await _raw_day(MAC, day, until_h=20.0)
        await _raw_day(MAC2, day, until_h=19.75)
        return (await daily_et.ensure(MAC, day, day, LAT),
                await daily_et.ensure(MAC2, day, day, LAT))
    (at, _), (below, _) = asyncio.run(go())
    iso = day.isoformat()
    assert at[iso][1] == "penman-monteith"
    assert below[iso][1] == "hargreaves"
    assert below[iso][0] == pytest.approx(watering.hargreaves_et0_in(
        75, 95, LAT, day.timetuple().tm_yday))
    # Penman-Monteith is the shared integration over that day's rows.
    async def pm():
        lo, hi = daily_et.day_bounds_ms(day)
        return derived.evapotranspiration_over(await db.et_input_rows(MAC, lo, hi))
    et, hours = asyncio.run(pm())
    assert hours == 20.0 and at[iso][0] == pytest.approx(et)
    # And both are stored.
    assert asyncio.run(_rows(MAC))[iso][1] == "penman-monteith"
    assert asyncio.run(_rows(MAC2))[iso][1] == "hargreaves"


def test_a_day_thinned_coarser_than_an_hour_falls_back_to_hargreaves(client):
    """Thinning keeps one row per bucket; past an hour apart no step counts,
    so the old day is a Hargreaves day from its untouched rollup."""
    day = _yesterday() - timedelta(days=200)

    async def go():
        await _device()
        await _rollup(MAC, day, tmin=60, tmax=88, rain=0, solar_max=850,
                      span_h=23.9)
        await _raw_day(MAC, day, until_h=23.0, step_min=120)
        return await daily_et.ensure(MAC, day, day, LAT)
    got, _ = asyncio.run(go())
    assert got[day.isoformat()][1] == "hargreaves"


def test_five_minute_thinning_still_reads_penman_monteith(client):
    day = _yesterday() - timedelta(days=200)

    async def go():
        await _device()
        await _rollup(MAC, day, tmin=60, tmax=88, rain=0, solar_max=850,
                      span_h=23.9)
        await _raw_day(MAC, day, until_h=23.9, step_min=5)
        return await daily_et.ensure(MAC, day, day, LAT)
    got, _ = asyncio.run(go())
    assert got[day.isoformat()][1] == "penman-monteith"


def test_no_rollup_still_reads_penman_monteith(client):
    """Insights off: there is no rollup row to say whether the sun was up,
    so the raw readings are asked, and a fully instrumented day is
    Penman-Monteith (Hargreaves, which needs the rollup, is not there)."""
    day = _yesterday() - timedelta(days=5)

    async def go():
        await _device()
        await _raw_day(MAC, day, until_h=23.75)
        lo, hi = daily_et.day_bounds_ms(day)
        want = derived.evapotranspiration_over(await db.et_input_rows(MAC, lo, hi))
        return await daily_et.compute_day(MAC, day, LAT, None), want
    got, want = asyncio.run(go())
    assert want is not None and want[1] >= daily_et.PM_MIN_HOURS
    assert got == (pytest.approx(want[0]), "penman-monteith")


def test_no_location_is_null_never_zero_and_says_why(client):
    day = _yesterday()

    async def go():
        await _device(located=False)
        await _rollup(MAC, day, tmin=70, tmax=100, rain=0.1, solar_max=900)
        await _raw_day(MAC, day, until_h=23.75)
    asyncio.run(go())
    body = client.get(f"/api/devices/{MAC}/water-balance", headers=H).json()
    assert all(d["et_in"] is None and d["method"] is None for d in body["days"])
    assert body["totals"]["et_in"] is None and body["methods"] == {}
    assert body["note"] == "ET needs the station's location."
    # The day's rain still shows; the balance never starts without ET.
    assert body["days"][-1]["rain_in"] == 0.1
    assert body["days"][-1]["balance_in"] is None
    assert asyncio.run(_rows()) == {}


def test_an_uncovered_day_is_null_not_stored_and_retried_when_it_changes(client):
    """A rollup that saw two hours of the day is not a measured day; the
    attempt is not stored, and the day is tried again once its rollup
    changes (here: the rest of the day arrived)."""
    day = _yesterday() - timedelta(days=2)

    async def go():
        await _device()
        await _rollup(MAC, day, tmin=80, tmax=81, rain=0, span_h=2.0)
        first, _ = await daily_et.ensure(MAC, day, day, LAT)
        stored = await _rows()
        await _rollup(MAC, day, tmin=72, tmax=101, rain=0, span_h=23.5)
        second, _ = await daily_et.ensure(MAC, day, day, LAT)
        return first, stored, second
    first, stored, second = asyncio.run(go())
    assert first == {} and stored == {}
    assert second[day.isoformat()][1] == "hargreaves"


def test_a_station_with_no_temperatures_gets_the_note(client):
    asyncio.run(_device())
    body = client.get(f"/api/devices/{MAC}/water-balance", headers=H).json()
    assert body["note"] == daily_et.NO_TEMPS_NOTE
    assert body["totals"] == {"et_in": None, "rain_in": None,
                              "balance_in": None, "days_used": 0, "days": 30}


# ── the response shape ───────────────────────────────────────────────────

def _mixed_window():
    """Fri 2026-09-25 .. Mon 2026-10-05: stored ET on five days (three PM,
    two Hargreaves), rain on five, null days in the middle."""
    d = date.fromisoformat

    async def go():
        await _device()
        await _put(MAC, d("2026-09-26"), 0.2, "penman-monteith")
        await _put(MAC, d("2026-09-28"), 0.1, "hargreaves")
        await _put(MAC, d("2026-09-29"), 0.3, "penman-monteith")
        await _put(MAC, d("2026-10-01"), 0.25, "penman-monteith")
        await _put(MAC, d("2026-10-05"), 0.15, "hargreaves")
        for day, rain in (("2026-09-26", 0.0), ("2026-09-27", 0.5),
                          ("2026-09-29", 1.0), ("2026-10-01", 0.0),
                          ("2026-10-05", 0.05)):
            await _rollup(MAC, d(day), rain=rain)
    asyncio.run(go())


def test_a_mixed_window(client):
    _mixed_window()
    r = client.get(f"/api/devices/{MAC}/water-balance",
                   params={"start": "2026-09-25", "end": "2026-10-05"},
                   headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mac"] == MAC and body["timezone"] == "UTC"
    assert (body["start"], body["end"]) == ("2026-09-25", "2026-10-05")
    days = {x["day"]: x for x in body["days"]}
    assert list(days) == [(date(2026, 9, 25) + timedelta(days=k)).isoformat()
                          for k in range(11)]
    # Null until the first usable day, then carried across days missing
    # either side.
    assert days["2026-09-25"] == {"day": "2026-09-25", "et_in": None,
                                  "method": None, "rain_in": None,
                                  "balance_in": None}
    assert days["2026-09-26"]["balance_in"] == -0.2
    assert days["2026-09-27"] == {"day": "2026-09-27", "et_in": None,
                                  "method": None, "rain_in": 0.5,
                                  "balance_in": -0.2}
    assert days["2026-09-28"]["rain_in"] is None
    assert days["2026-09-28"]["balance_in"] == -0.2
    assert days["2026-09-29"]["balance_in"] == 0.5
    assert days["2026-09-30"]["balance_in"] == 0.5
    assert days["2026-10-01"]["balance_in"] == 0.25
    assert days["2026-10-04"]["balance_in"] == 0.25
    assert days["2026-10-05"] == {"day": "2026-10-05", "et_in": 0.15,
                                  "method": "hargreaves", "rain_in": 0.05,
                                  "balance_in": 0.15}
    # Weeks start Monday; the first starts before the window and sums
    # only the days inside it.
    # A period's balance counts only its days with both (amended 10-15):
    # the first week's 0.5 in of rain fell on a day with no ET and is not
    # set against the 26th's ET, so the week is -0.2, not 0.3.
    assert body["weeks"] == [
        {"start": "2026-09-21", "et_in": 0.2, "rain_in": 0.5,
         "balance_in": -0.2, "days_used": 1},
        {"start": "2026-09-28", "et_in": 0.65, "rain_in": 1.0,
         "balance_in": 0.45, "days_used": 2},
        {"start": "2026-10-05", "et_in": 0.15, "rain_in": 0.05,
         "balance_in": -0.1, "days_used": 1}]
    assert body["months"] == [
        {"month": "2026-09", "et_in": 0.6, "rain_in": 1.5,
         "balance_in": 0.5, "days_used": 2},
        {"month": "2026-10", "et_in": 0.4, "rain_in": 0.05,
         "balance_in": -0.35, "days_used": 2}]
    assert body["totals"] == {"et_in": 1.0, "rain_in": 1.55,
                              "balance_in": 0.15, "days_used": 4, "days": 11}
    assert body["methods"] == {"penman-monteith": 3, "hargreaves": 2}
    assert "note" not in body


def test_a_period_with_no_values_of_a_field_is_null():
    rows = [{"et_in": None, "rain_in": 0.2}, {"et_in": None, "rain_in": None}]
    assert daily_et._period(rows) == {"et_in": None, "rain_in": 0.2,
                                      "balance_in": None, "days_used": 0}
    # Rain on a day with no ET is in the rain sum and out of the balance.
    rows.append({"et_in": 0.1, "rain_in": 0.0})
    assert daily_et._period(rows) == {"et_in": 0.1, "rain_in": 0.2,
                                      "balance_in": -0.1, "days_used": 1}


# ── the window ───────────────────────────────────────────────────────────

def test_window_defaults_clamps_and_refusals(client):
    asyncio.run(_device(located=False))     # nothing to compute: fast
    y = _yesterday()
    url = f"/api/devices/{MAC}/water-balance"
    body = client.get(url, headers=H).json()
    assert body["end"] == y.isoformat()
    assert body["start"] == (y - timedelta(days=29)).isoformat()
    assert len(body["days"]) == 30
    # Only complete days: an end in the future is clamped to yesterday.
    body = client.get(url, params={"end": (y + timedelta(days=5)).isoformat()},
                      headers=H).json()
    assert body["end"] == y.isoformat() and len(body["days"]) == 30
    # At most 400 days: start moves forward.
    body = client.get(url, params={
        "start": (y - timedelta(days=600)).isoformat(),
        "end": y.isoformat()}, headers=H).json()
    assert body["start"] == (y - timedelta(days=399)).isoformat()
    assert len(body["days"]) == 400
    # An explicit window inside the limits is served as asked.
    body = client.get(url, params={"start": "2026-01-01", "end": "2026-01-03"},
                      headers=H).json()
    assert [d["day"] for d in body["days"]] == [
        "2026-01-01", "2026-01-02", "2026-01-03"]
    # A start after the end is a 422, as is a start after a clamped end.
    assert client.get(url, params={"start": "2026-01-05", "end": "2026-01-03"},
                      headers=H).status_code == 422
    assert client.get(url, params={"start": daily_et.local_today().isoformat()},
                      headers=H).status_code == 422
    assert client.get(url, params={"start": "not-a-day"},
                      headers=H).status_code == 422
    # Unknown station: 404, both routes. And the token is required.
    for path in ("water-balance", "water-balance.csv"):
        assert client.get(f"/api/devices/AA:00:00:00:00:99/{path}",
                          headers=H).status_code == 404
        assert client.get(f"/api/devices/{MAC}/{path}").status_code == 401


def test_the_csv(client):
    _mixed_window()
    r = client.get(f"/api/devices/{MAC}/water-balance.csv",
                   params={"start": "2026-09-25", "end": "2026-10-05"},
                   headers=H)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == (
        'attachment; filename="water-balance-AAE700000001-'
        '2026-09-25-2026-10-05.csv"')
    lines = r.text.splitlines()
    assert lines[0] == "day,et_in,method,rain_in,balance_in"
    assert len(lines) == 12
    assert lines[1] == "2026-09-25,,,,"
    assert lines[2] == "2026-09-26,0.200,penman-monteith,0.00,-0.200"
    assert lines[3] == "2026-09-27,,,0.50,-0.200"
    assert lines[11] == "2026-10-05,0.150,hargreaves,0.05,0.150"


# ── when values are made ─────────────────────────────────────────────────

def test_the_nightly_job_stores_yesterday_once(client, monkeypatch):
    y = _yesterday()
    calls = []
    real = daily_et.compute_day

    async def spy(mac, day, lat, rollup):
        calls.append((mac, day))
        return await real(mac, day, lat, rollup)
    monkeypatch.setattr(daily_et, "compute_day", spy)

    async def go():
        await _device()
        await _device(MAC2, located=False)
        await _rollup(MAC, y, tmin=70, tmax=99, rain=0)
        await _rollup(MAC, y - timedelta(days=1), tmin=70, tmax=99, rain=0)
        now = _ms(daily_et.local_today(), 1.0)       # 01:00 local
        first = await daily_et.store_yesterday_all(now)
        second = await daily_et.store_yesterday_all(now + 900_000)
        return first, second
    first, second = asyncio.run(go())
    assert (first, second) == (1, 0)
    assert calls == [(MAC, y)], "once, and never for a station without a place"
    assert set(asyncio.run(_rows())) == {y.isoformat()}


def test_the_nightly_job_waits_for_the_day_to_settle(client):
    """At 00:05 a relay may still be draining yesterday: the newest day
    that may be stored is the day before."""
    y = _yesterday()

    async def go():
        await _device()
        for k in (1, 2):
            await _rollup(MAC, y - timedelta(days=k - 1), tmin=70, tmax=99, rain=0)
        return await daily_et.store_yesterday_all(
            _ms(daily_et.local_today(), 5 / 60))
    assert asyncio.run(go()) == 1
    assert set(asyncio.run(_rows())) == {(y - timedelta(days=1)).isoformat()}


def test_the_lazy_fill_is_bounded_and_the_rest_goes_to_the_background(
        client, monkeypatch):
    y = _yesterday()
    spawned = []
    monkeypatch.setattr(daily_et, "LAZY_INLINE_DAYS", 3)
    monkeypatch.setattr(daily_et, "_spawn_fill",
                        lambda mac, days, lat, since=None: spawned.append(list(days)))

    async def seed():
        await _device()
        for k in range(10):
            await _rollup(MAC, y - timedelta(days=k), tmin=70, tmax=99, rain=0)
    asyncio.run(seed())
    first = (y - timedelta(days=9)).isoformat()
    body = client.get(f"/api/devices/{MAC}/water-balance",
                      params={"start": first, "end": y.isoformat()},
                      headers=H).json()
    # Newest first: the three most recent days are there now.
    assert [d["et_in"] is not None for d in body["days"]] == [False] * 7 + [True] * 3
    assert body["note"] == daily_et.FILLING_NOTE
    assert len(asyncio.run(_rows())) == 3
    assert spawned == [[y - timedelta(days=k) for k in range(3, 10)]]
    # The background fill finishes the window.
    asyncio.run(daily_et._fill_in_background(MAC, spawned[0], LAT))
    body = client.get(f"/api/devices/{MAC}/water-balance",
                      params={"start": first, "end": y.isoformat()},
                      headers=H).json()
    assert all(d["method"] == "hargreaves" for d in body["days"])
    assert "note" not in body and body["methods"] == {"hargreaves": 10}


# ── when they are thrown away ────────────────────────────────────────────

def test_rows_landing_on_a_past_day_drop_its_stored_et(client):
    """An import (WU, WeeWX, CSV: all insert through insert_observations)
    or a relay's backlog changes a complete day: its stored value goes and
    the next read recomputes it. Today's readings touch nothing."""
    from app import archive_import as ai
    y = _yesterday()
    old = y - timedelta(days=40)

    async def go():
        await _device()
        for d in (y, old, y - timedelta(days=1)):
            await _put(MAC, d, 0.2, "penman-monteith")
        await db.insert_observations(MAC, [{"dateutc": _ms(daily_et.local_today(), 0.01),
                                            "tempf": 70.0}])
        after_today = set(await _rows())
        await db.insert_observations(MAC, [{"dateutc": _ms(y, 12.0), "tempf": 70.0}])
        after_backlog = set(await _rows())
        await ai.run_import(MAC, iter([{"dateutc": _ms(old, 6.0), "tempf": 60.0}]),
                            kind="weewx")
        return after_today, after_backlog, set(await _rows())
    after_today, after_backlog, after_import = asyncio.run(go())
    assert after_today == {y.isoformat(), old.isoformat(),
                           (y - timedelta(days=1)).isoformat()}
    assert after_backlog == {old.isoformat(), (y - timedelta(days=1)).isoformat()}
    assert after_import == {(y - timedelta(days=1)).isoformat()}


def test_a_restore_drops_every_stored_value(client):
    from app import restore
    asyncio.run(_device())
    asyncio.run(_put(MAC, _yesterday(), 0.2, "penman-monteith"))
    daily_et._ATTEMPTED[(MAC, "2026-01-01")] = (0.0, None)
    hook = next(h for h in restore.POST_SWAP_HOOKS
                if h.__name__ == "_reconcile_after_restore")
    client.portal.call(hook)
    assert asyncio.run(_rows()) == {} and not daily_et._ATTEMPTED


def test_a_rollup_rebuild_drops_the_stored_values(client, monkeypatch):
    from app import insights
    monkeypatch.setattr(db.settings, "insights", True)
    asyncio.run(_device())
    asyncio.run(_put(MAC, _yesterday(), 0.2, "hargreaves"))
    asyncio.run(insights.rebuild())
    assert asyncio.run(_rows()) == {}


def test_a_location_change_drops_the_hargreaves_days_only(client):
    y = _yesterday()

    async def go():
        await _device()
        await _put(MAC, y, 0.2, "hargreaves")
        await _put(MAC, y - timedelta(days=1), 0.3, "penman-monteith")
        await db.set_device_location(MAC, 40.0, -105.0, None, 1)
        return await _rows()
    assert set(asyncio.run(go())) == {(y - timedelta(days=1)).isoformat()}


# ── upstream coordinate corrections (2.7 follow-up F27-04) ──────────────
#
# Ingest and the cloud pollers write a station's coordinates through
# db.upsert_device, which replaced them without touching the Hargreaves
# days computed from the old latitude: `ensure` then served the complete
# cached window from the old place. An accepted latitude change now drops
# them the way a manual location change does; nothing else does.

def _upstream(lat, lon=LON, seen_ms=None):
    info = {"name": "Yard", "info": {"coords": {"coords": {"lat": lat, "lon": lon}}}}
    if seen_ms is not None:
        info["lastData"] = {"dateutc": seen_ms}
    return db.upsert_device(MAC, info)


async def _settled_hargreaves(day, lat=LAT, seen_ms=None):
    """A located station with one settled Hargreaves day stored."""
    await _upstream(lat, seen_ms=seen_ms)
    await _rollup(MAC, day, tmin=60, tmax=90, rain=0)
    await daily_et.ensure(MAC, day, day, lat)
    assert (await _rows())[day.isoformat()][1] == "hargreaves"


def _lat_of_device():
    async def go():
        return daily_et.station_lat(
            next(d for d in await db.list_devices() if d["mac"] == MAC))
    return go()


def test_an_accepted_upstream_latitude_correction_recomputes_et(client):
    day = daily_et.local_today() - timedelta(days=3)

    async def go():
        await _settled_hargreaves(day)
        await _put(MAC, day - timedelta(days=1), 0.3, "penman-monteith")
        since = daily_et.epoch()
        await _upstream(55.0)                 # an ingest / cloud metadata update
        # An invalidation, as set_device_location opens: every fill that
        # began before the correction is stale.
        assert not daily_et.current(MAC, since)
        lat = await _lat_of_device()
        assert lat == 55.0
        left = await _rows()
        daily_et._ATTEMPTED.clear()           # a restart remembers nothing
        values, _ = await daily_et.ensure(MAC, day, day, lat)
        return left, values[day.isoformat()], await _rows()
    left, value, stored = asyncio.run(go())
    want = watering.hargreaves_et0_in(60, 90, 55.0, day.timetuple().tm_yday)
    assert set(left) == {(day - timedelta(days=1)).isoformat()}, left
    assert value == (want, "hargreaves")
    assert stored[day.isoformat()] == (want, "hargreaves")


def test_unchanged_upstream_coordinates_drop_nothing(client):
    """Every ordinary post repeats the coordinates: no invalidation, no
    delete, the epoch does not move."""
    day = _yesterday() - timedelta(days=1)

    async def go():
        await _settled_hargreaves(day)
        before, epoch = await _rows(), daily_et.epoch()
        for _ in range(3):
            await _upstream(LAT)
        await db.upsert_device(MAC, {"name": "Yard"})     # no coordinates at all
        return before, await _rows(), epoch, daily_et.epoch()
    before, after, e0, e1 = asyncio.run(go())
    assert after == before and e1 == e0


def test_a_post_without_coordinates_keeps_the_last_ones(client):
    """CodeRabbit, PR #111: a missing coordinate is not a move. A post
    with none keeps the station's last valid pair (the ET stays, and the
    apps still see the place), so nothing churns."""
    day = _yesterday() - timedelta(days=1)

    async def go():
        await _settled_hargreaves(day)
        before, epoch = await _rows(), daily_et.epoch()
        await db.upsert_device(MAC, {"name": "Yard", "info": {"source": "relay"}})
        return before, await _rows(), epoch, daily_et.epoch(), await _lat_of_device()
    before, after, e0, e1, lat = asyncio.run(go())
    assert after == before and e1 == e0
    assert lat == LAT


def test_a_move_after_a_post_without_coordinates_still_drops_the_old_et(client):
    """Coordinates, then a post without any, then new coordinates: the
    move is measured from the last real place, so the old latitude's
    Hargreaves days still go."""
    day = _yesterday() - timedelta(days=1)

    async def go():
        await _settled_hargreaves(day)
        await db.upsert_device(MAC, {"name": "Yard"})
        await _upstream(55.0)
        return await _rows(), await _lat_of_device()
    rows, lat = asyncio.run(go())
    assert rows == {} and lat == 55.0


def test_an_operator_set_location_wins_over_an_upstream_change(client):
    """The station's latitude is the override's: upstream coordinates
    moving under it change nothing ET reads."""
    day = _yesterday() - timedelta(days=1)

    async def go():
        await _upstream(LAT)
        await db.set_device_location(MAC, 40.0, -105.0, None, 1)
        await _rollup(MAC, day, tmin=60, tmax=90, rain=0)
        await daily_et.ensure(MAC, day, day, 40.0)
        before = await _rows()
        await _upstream(55.0)
        return before, await _rows(), await _lat_of_device()
    before, after, lat = asyncio.run(go())
    assert before and after == before and lat == 40.0


def test_rejected_older_metadata_drops_nothing(client):
    """An out-of-order post's coordinates are not accepted (the
    last_seen guard), so they cannot invalidate either."""
    day = _yesterday() - timedelta(days=1)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)

    async def go():
        await _settled_hargreaves(day, seen_ms=now)
        before = await _rows()
        await _upstream(55.0, seen_ms=now - 3_600_000)
        return before, await _rows(), await _lat_of_device()
    before, after, lat = asyncio.run(go())
    assert after == before and lat == LAT


def test_an_upstream_correction_during_a_fill_is_not_undone(client, monkeypatch):
    y = _yesterday()

    async def go():
        await _upstream(LAT)
        await _rollup(MAC, y, tmin=60, tmax=90, rain=0)
        await _race(monkeypatch, MAC, y, LAT, lambda: _upstream(55.0))
        left = await _rows()
        daily_et._ATTEMPTED.clear()
        values, _ = await daily_et.ensure(MAC, y, y, 55.0)
        return left, values[y.isoformat()]
    left, value = asyncio.run(go())
    assert left == {}, f"the old latitude's ET survived the correction: {left}"
    assert value == (watering.hargreaves_et0_in(60, 90, 55.0, y.timetuple().tm_yday),
                     "hargreaves")


# ── the Watering card reads the same values ──────────────────────────────

def test_the_watering_card_reads_the_stored_values(client):
    """Rollups alone would give Hargreaves ~0.3 in a day and "water"; the
    stored Penman-Monteith days say the air took little, and the card and
    the water balance agree day by day."""
    today = daily_et.local_today()

    async def go():
        await _device()
        for k in range(1, 8):
            d = today - timedelta(days=k)
            await _rollup(MAC, d, tmin=80, tmax=106, rain=0)
            await _put(MAC, d, 0.01, "penman-monteith")
    asyncio.run(go())
    card = client.get(f"/api/devices/{MAC}/watering", headers=H).json()
    assert card["verdict"] == "skip" and card["et_in"] == 0.07
    assert card["method"] == "penman-monteith"
    hist = client.get(f"/api/devices/{MAC}/water-balance", headers=H).json()
    tail = {d["day"]: d["et_in"] for d in hist["days"][-7:]}
    assert {d["day"]: d["et_in"] for d in card["days"]} == tail


def test_the_watering_card_stores_what_it_computes(client):
    """A week the nightly job never saw (a fresh install): the card's own
    read fills the store, so the history shows the same numbers."""
    today = daily_et.local_today()

    async def go():
        await _device()
        for k in range(1, 8):
            await _rollup(MAC, today - timedelta(days=k), tmin=80, tmax=106,
                          rain=0)
        return await watering.for_station(MAC, today, LAT)
    call = asyncio.run(go())
    assert call["verdict"] == "water" and call["method"] == "hargreaves"
    stored = asyncio.run(_rows())
    assert len(stored) == 7
    assert [d["et_in"] for d in call["days"]] == [
        round(stored[d["day"]][0], 3) for d in call["days"]]


def test_a_mixed_week_says_mixed():
    days = [{"method": "hargreaves"}, {"method": "penman-monteith"},
            {"method": None}]
    assert watering._method_of(days) == "mixed"
    assert watering._method_of([{"method": None}]) == "hargreaves"


# ── the Science row is unchanged, on the shared integration ─────────────

def test_the_science_today_row_is_the_shared_integration(client):
    from app import main
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    span_min = int((now - midnight).total_seconds() // 60)
    if span_min < 30:
        pytest.skip("too close to midnight for a window of readings")

    async def go():
        await _device()
        rows = [{"dateutc": int((midnight + timedelta(minutes=m)).timestamp() * 1000),
                 "tempf": 90.0, "humidity": 20.0, "windspeedmph": 5.0,
                 "solarradiation": 700.0, "baromabsin": 28.6}
                for m in range(1, span_min, 10)]
        await db.insert_observations(MAC, rows)
        out: dict = {}
        await main._accumulated_today(MAC, {}, out)
        lo = int(midnight.timestamp() * 1000)
        et = derived.evapotranspiration_over(
            await db.observation_window(MAC, lo, int(now.timestamp() * 1000)))
        return out, et
    out, et = asyncio.run(go())
    assert out["evapotranspirationInToday"] == round(et[0], 3)
    assert out["evapotranspirationSource"] == "penman-monteith"


# ── migration from the 2.6 schema ────────────────────────────────────────

def test_a_26_database_gains_the_table_and_keeps_its_data(temp_env):
    """The 2.6 schema has no daily_et. A database at that schema (built
    here by init_db and then losing the table, the shape every upgraded
    box has) migrates on boot with its readings intact and serves ET."""
    import importlib
    import sqlite3
    import app.config
    import app.db
    importlib.reload(app.config)
    importlib.reload(app.db)
    from app import db as fresh_db
    path = app.config.settings.database_path

    asyncio.run(fresh_db.init_db())
    asyncio.run(fresh_db.upsert_device(MAC, {"name": "Old", "info": {
        "coords": {"coords": {"lat": LAT, "lon": LON}}}}))
    asyncio.run(fresh_db.insert_observations(MAC, [{"dateutc": 1_700_000_000_000,
                                                    "tempf": 70.0}]))
    con = sqlite3.connect(path)
    con.execute("DROP TABLE daily_et")
    con.commit()
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='daily_et'"
                           ).fetchone()
    con.close()

    asyncio.run(fresh_db.init_db())
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(daily_et)")]
    n_obs = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    con.close()
    assert cols == ["mac", "day", "et_in", "method", "computed_ms"]
    assert n_obs == 1

    async def use():
        day = date(2026, 1, 1)
        await fresh_db.insert_observations(MAC, [{"dateutc": _ms(day, 3.0),
                                                  "tempf": 50.0}])
        async with fresh_db.connect() as conn:
            await conn.execute(
                "INSERT INTO daily_et VALUES (?, ?, 0.1, 'hargreaves', 1)",
                (MAC, "2026-01-02"))
            await conn.commit()
        await fresh_db.insert_observations(MAC, [{"dateutc": _ms(date(2026, 1, 2), 3.0),
                                                  "tempf": 50.0}])
        async with fresh_db.connect() as conn:
            return (await (await conn.execute(
                "SELECT COUNT(*) FROM daily_et")).fetchone())[0]
    assert asyncio.run(use()) == 0


def test_the_response_has_exactly_the_contracts_keys(client):
    """The wire shape the three apps decode is pinned in one file,
    tests/fixtures/water_balance_example.json (the contract's example).
    bin/tests/test_water_balance_wire_parity.py holds the iOS and Android
    models to the same file; this holds the server to it, so a renamed or
    added key fails here before it is a silent nil on a phone."""
    import json
    from pathlib import Path
    want = json.loads((Path(__file__).parent / "fixtures"
                       / "water_balance_example.json").read_text())
    _mixed_window()
    body = client.get(f"/api/devices/{MAC}/water-balance",
                      params={"start": "2026-09-25", "end": "2026-10-05"},
                      headers=H).json()
    optional = {"note"}
    assert set(body) <= set(want) and set(want) - set(body) <= optional
    for key in ("days", "weeks", "months"):
        assert set(body[key][0]) == set(want[key][0]), key
    assert set(body["totals"]) == set(want["totals"])


# ── R27-03 (2.7 review): a stale fill cannot outlive its invalidation ────
# A fill reads its inputs, awaits the computation, then stores. An
# invalidation (a location change, rows landing on the day, a rebuild or
# restore, a device delete) that runs while it is suspended used to be
# undone by the fill's write. Each test holds a real computation at the
# point the review found and runs the real invalidation there.

def _hold_compute(monkeypatch):
    """compute_day, paused after it has its answer and before ensure()
    stores it. Returns (entered, release, original)."""
    entered, release = asyncio.Event(), asyncio.Event()
    original = daily_et.compute_day

    async def held(*args):
        value = await original(*args)
        entered.set()
        await release.wait()
        return value
    monkeypatch.setattr(daily_et, "compute_day", held)
    return entered, release, original


async def _race(monkeypatch, mac, day, lat, invalidate):
    """One fill of `day`, with `invalidate()` run while it is suspended.
    Returns what the fill handed its reader."""
    entered, release, original = _hold_compute(monkeypatch)
    task = asyncio.create_task(daily_et.ensure(mac, day, day, lat))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await invalidate()
    finally:
        release.set()
    got = await task
    monkeypatch.setattr(daily_et, "compute_day", original)
    return got


def test_a_location_change_during_a_fill_is_not_undone(client, monkeypatch):
    y = _yesterday()
    lo, hi = daily_et.day_bounds_ms(y)

    async def go():
        await _device()
        await _rollup(MAC, y, tmin=60, tmax=90, rain=0)
        await _race(monkeypatch, MAC, y, LAT, lambda: db.set_device_location(
            MAC, 55.0, LON, None, hi))
        left = await _rows()
        # A restart forgets everything in memory; the read after it sees
        # only what was stored.
        daily_et._ATTEMPTED.clear()
        values, _ = await daily_et.ensure(MAC, y, y, 55.0)
        return left, values[y.isoformat()], await _rows()
    left, value, stored = asyncio.run(go())
    want = watering.hargreaves_et0_in(60, 90, 55.0, y.timetuple().tm_yday)
    assert left == {}, f"the old latitude's ET survived the move: {left}"
    assert value == (want, "hargreaves")
    assert stored == {y.isoformat(): (want, "hargreaves")}


def test_rows_landing_on_the_day_during_a_fill_are_not_undone(
        client, monkeypatch):
    monkeypatch.setattr(db.settings, "insights", True)
    y = _yesterday()
    lo, hi = daily_et.day_bounds_ms(y)

    async def go():
        await _device()
        await db.insert_observations(MAC, [
            {"dateutc": lo, "tempf": 60.0},
            {"dateutc": hi - 60_000, "tempf": 80.0}], received=False)
        await _race(monkeypatch, MAC, y, LAT, lambda: db.insert_observations(
            MAC, [{"dateutc": lo + 12 * 3_600_000, "tempf": 100.0}],
            received=False))
        left = await _rows()
        daily_et._ATTEMPTED.clear()
        values, _ = await daily_et.ensure(MAC, y, y, LAT)
        return left, values[y.isoformat()]
    left, value = asyncio.run(go())
    want = watering.hargreaves_et0_in(60, 100, LAT, y.timetuple().tm_yday)
    assert left == {}, f"the pre-import ET came back: {left}"
    assert value == (want, "hargreaves")


def test_a_rebuild_clear_during_a_fill_is_not_undone(client, monkeypatch):
    y = _yesterday()

    async def go():
        await _device()
        await _rollup(MAC, y, tmin=60, tmax=90, rain=0)
        await _race(monkeypatch, MAC, y, LAT, lambda: daily_et.clear(MAC))
        return await _rows()
    assert asyncio.run(go()) == {}


def test_a_restore_clear_during_a_fill_is_not_undone(client, monkeypatch):
    y = _yesterday()

    async def go():
        await _device()
        await _rollup(MAC, y, tmin=60, tmax=90, rain=0)
        await _race(monkeypatch, MAC, y, LAT, lambda: daily_et.clear())
        return await _rows()
    assert asyncio.run(go()) == {}


def test_a_deleted_device_gets_no_et_back_from_a_fill(client, monkeypatch):
    """A Penman-Monteith day, which reads no latitude: only the generation
    keeps the deleted station's row out."""
    y = _yesterday()

    async def go():
        await _device()
        await _rollup(MAC, y, tmin=75, tmax=95, rain=0, solar_max=900,
                      span_h=23.9)
        await _raw_day(MAC, y, until_h=23.75)
        values, _ = await _race(monkeypatch, MAC, y, LAT,
                                lambda: db.delete_device(MAC))
        return values, await _rows()
    values, stored = asyncio.run(go())
    assert values[_yesterday().isoformat()][1] == "penman-monteith"
    assert stored == {}


def test_a_latitude_read_before_a_move_is_not_stored_after_it(client):
    """The caller reads the latitude, the move lands, THEN the fill
    starts: no generation was live when the fill began, so the store
    itself checks the latitude it computed with against the station's."""
    y = _yesterday()

    async def go():
        await _device()
        await _rollup(MAC, y, tmin=60, tmax=90, rain=0)
        await db.set_device_location(MAC, 55.0, LON, None, 1)
        values, _ = await daily_et.ensure(MAC, y, y, LAT)   # the stale read
        return values, await _rows()
    values, stored = asyncio.run(go())
    # The reader still gets an answer for what it asked; nothing is kept.
    assert values[y.isoformat()][1] == "hargreaves"
    assert stored == {}


def test_a_stale_background_fill_stops(client, monkeypatch):
    """The background fill carries the epoch of the read that spawned it
    (and that read's latitude): once that is stale, the rest of its work
    is for inputs that are gone, so it stops instead of computing days it
    may not store."""
    y = _yesterday()
    days = [y - timedelta(days=k) for k in range(3)]
    calls = []
    original = daily_et.compute_day

    async def counted(*args):
        calls.append(args[1])
        return await original(*args)

    async def go():
        await _device()
        for d in days:
            await _rollup(MAC, d, tmin=60, tmax=90, rain=0)
        epoch = daily_et.epoch()
        await db.insert_observations(MAC, [{"dateutc": _ms(y, 12.0),
                                            "tempf": 70.0}], received=False)
        monkeypatch.setattr(daily_et, "compute_day", counted)
        await daily_et._fill_in_background(MAC, days, LAT, epoch)
        return await _rows()
    assert asyncio.run(go()) == {} and calls == []


def test_parallel_fills_and_a_second_invalidation(client, monkeypatch):
    """Two fills of the same day in flight (the inline read and the
    Watering card), a move under both, then a second move: only the last
    latitude is ever stored."""
    y = _yesterday()

    async def go():
        await _device()
        await _rollup(MAC, y, tmin=60, tmax=90, rain=0)
        entered, release, original = _hold_compute(monkeypatch)
        a = asyncio.create_task(daily_et.ensure(MAC, y, y, LAT))
        b = asyncio.create_task(daily_et.ensure(MAC, y, y, LAT,
                                                bounded=False))
        await asyncio.wait_for(entered.wait(), 5)
        await db.set_device_location(MAC, 45.0, LON, None, 1)
        await db.set_device_location(MAC, 55.0, LON, None, 2)
        release.set()
        await asyncio.gather(a, b)
        monkeypatch.setattr(daily_et, "compute_day", original)
        left = await _rows()
        await daily_et.ensure(MAC, y, y, 55.0)
        return left, await _rows()
    left, stored = asyncio.run(go())
    want = watering.hargreaves_et0_in(60, 90, 55.0, y.timetuple().tm_yday)
    assert left == {}
    assert stored == {y.isoformat(): (want, "hargreaves")}


def test_no_store_while_the_stations_own_invalidation_is_open(client):
    """Between a station's delete and the end of its invalidation, a fill
    that took its epoch before it stores nothing for that station; a
    station with no invalidation open still stores."""
    y = _yesterday()

    async def go():
        await _device()
        await _device(MAC2)
        since = daily_et.epoch()
        with daily_et.invalidating(MAC):
            async with db.connect() as conn:
                await conn.execute("DELETE FROM daily_et WHERE mac = ?", (MAC,))
                await conn.commit()
            mine = await daily_et._store(MAC, y, 0.2, daily_et.PM, since=since, lat=LAT)
            other = await daily_et._store(MAC2, y, 0.2, daily_et.PM, since=since, lat=LAT)
        return mine, other, await _rows(MAC), await _rows(MAC2)
    mine, other, rows, rows2 = asyncio.run(go())
    assert mine is False and rows == {}
    assert other is True and set(rows2) == {y.isoformat()}


def test_a_nested_invalidation_stays_open_until_the_last_one_ends(client):
    """Two invalidations of one station overlap: the first to end leaves
    the other open, and nothing may store until it ends too."""
    daily_et.invalidation_began(MAC)
    daily_et.invalidation_began(MAC)
    try:
        daily_et.invalidation_ended(MAC)
        assert daily_et.current(MAC, daily_et.epoch()) is False
    finally:
        daily_et.invalidation_ended(MAC)
    assert daily_et.current(MAC, daily_et.epoch()) is True


def test_an_invalidation_that_fails_does_not_wedge_storage(client):
    """The generation closes even when the invalidating write raises, or
    every later fill of the station would be refused forever."""
    y = _yesterday()

    async def go():
        await _device()
        await _rollup(MAC, y, tmin=60, tmax=90, rain=0)
        with pytest.raises(RuntimeError):
            with daily_et.invalidating(MAC):
                raise RuntimeError("the write failed")
        await daily_et.ensure(MAC, y, y, LAT)
        return await _rows()
    assert set(asyncio.run(go())) == {_yesterday().isoformat()}


def test_every_daily_et_delete_opens_an_invalidation():
    """Static guard: a new path that deletes stored ET without opening a
    generation would bring the race back for that path."""
    import inspect
    import re
    from app import daily_et as det, db as dbm
    for mod in (dbm, det):
        src = inspect.getsource(mod)
        for m in re.finditer(r"DELETE FROM daily_et", src):
            # The enclosing function: from the last def before the match.
            start = max(src.rfind("\nasync def ", 0, m.start()),
                        src.rfind("\ndef ", 0, m.start()))
            body = src[start:m.start()]
            name = body.split("(")[0].split()[-1]
            if name == "_drop_daily_et_days":
                # Called only from insert_observations, which opens it.
                caller = src[src.find("\nasync def insert_observations"):]
                caller = caller[:caller.find("\nasync def ", 1)]
                assert "et_guard.open()" in caller, name
                continue
            assert "invalidating(" in body or "Invalidation(" in body, (
                f"{mod.__name__}.{name} deletes daily_et rows without "
                f"opening an invalidation")
