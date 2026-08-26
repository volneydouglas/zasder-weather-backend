"""Pillar C derived metrics: every formula pinned to a literature anchor,
plus the endpoint's absent-is-not-zero behavior and the forecast
snapshotter's storage."""
from __future__ import annotations

import asyncio
import datetime as _dt
import time

import pytest

from app import derived as d

AUTH = {"Authorization": "Bearer test-api-token"}
INGEST = {"Authorization": "Bearer test-ingest-token"}


# ── pure formulas, literature anchors ───────────────────────────────────

def test_wet_bulb_stull_worked_example():
    # Stull 2011's own example: 20 °C, RH 50 % → Tw ≈ 13.7 °C.
    tw = d.wet_bulb_f(68.0, 50.0)
    assert tw is not None
    assert abs((tw - 32) * 5 / 9 - 13.7) < 0.3


def test_wet_bulb_refuses_outside_validity():
    assert d.wet_bulb_f(68.0, 3.0) is None       # RH < 5
    assert d.wet_bulb_f(140.0, 50.0) is None     # 60 °C > validity
    assert d.wet_bulb_f(None, 50.0) is None
    assert d.wet_bulb_f(float("nan"), 50.0) is None


def test_frost_point_sits_above_dew_point_below_freezing():
    dew = d.dew_point_f(20.0, 70.0)
    frost = d.frost_point_f(20.0, 70.0)
    assert dew is not None and frost is not None
    assert frost > dew                     # ice saturation > water
    assert frost < 32.0
    # No frost story when the dew point is above freezing:
    assert d.frost_point_f(70.0, 80.0) is None


def test_delta_t_bands_are_celsius():
    # Hot dry desert afternoon: huge depression, well past the >10 °C
    # "do not spray" band.
    dt = d.delta_t_c(104.0, 15.0)
    assert dt is not None and dt > 10.0
    # Cool humid morning: small depression.
    dt2 = d.delta_t_c(60.0, 90.0)
    assert dt2 is not None and dt2 < 2.5


def test_fosberg_anchor_and_monotonicity():
    hot_dry_windy = d.fosberg_fwi(90.0, 10.0, 20.0)
    assert hot_dry_windy is not None
    assert 50.0 < hot_dry_windy < 62.0     # hand-computed ≈ 56
    # More humidity → lower index; more wind → higher.
    assert d.fosberg_fwi(90.0, 60.0, 20.0) < hot_dry_windy
    assert d.fosberg_fwi(90.0, 10.0, 30.0) > hot_dry_windy


def test_chandler_burning_index_bands():
    extreme = d.chandler_burning_index(95.0, 20.0)
    assert extreme is not None and 95.0 < extreme < 110.0
    mild = d.chandler_burning_index(60.0, 80.0)
    assert mild is not None and mild < 50.0


def test_density_altitude_plausible_and_needs_station_pressure():
    da = d.density_altitude_ft(95.0, 60.0, 28.0)
    assert da is not None and 3500 < da < 6000
    # Cold dense air at sea-level station pressure → below-zero DA.
    da2 = d.density_altitude_ft(30.0, 20.0, 30.10)
    assert da2 is not None and da2 < 500
    assert d.density_altitude_ft(95.0, 60.0, None) is None


def test_degree_days_convention():
    assert d.heating_degree_days(50.0, 30.0) == 25.0
    assert d.cooling_degree_days(95.0, 75.0) == 20.0
    assert d.heating_degree_days(80.0, 60.0) == 0.0
    assert d.heating_degree_days(30.0, 50.0) is None    # hi < lo = garbage


def test_pressure_tendency_bands():
    assert d.pressure_tendency_code(0.04) == (2, "rising")
    assert d.pressure_tendency_code(-0.04) == (7, "falling")
    assert d.pressure_tendency_code(0.01) == (4, "steady")
    assert d.pressure_tendency_code(None) is None


def test_zambretti_canonical_anchors():
    # High steady pressure = settled; deep falling = the bottom of the
    # falling slice; rising at high pressure = settled again.
    assert d.zambretti(1030.0, "steady") == "Settled fine"
    assert d.zambretti(975.0, "falling") == "Very unsettled, rain"
    assert d.zambretti(1030.0, "rising") == "Settled fine"
    # Falling clamps INSIDE its 1–9 slice — never a steady/rising text.
    assert d.zambretti(1050.0, "falling") == "Settled fine"
    assert d.zambretti(900.0, "rising") == "Stormy, much rain"
    assert d.zambretti(1013.0, "sideways") is None


# ── endpoint ────────────────────────────────────────────────────────────

def test_derived_endpoint_absent_is_not_zero(client):
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.post("/ingest/custom", headers=INGEST,
                    json={"device": {"id": "AABBCC000007", "name": "Derived"},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": 104.0, "humidity": 15.0},
                          "wind": {"speed_mph": 8.0},
                          "pressure": {"relative_inhg": 29.80,
                                       "absolute_inhg": 28.60},
                          "source": "test"})
    assert r.status_code == 200
    j = client.get("/api/devices/AA:BB:CC:00:00:07/derived",
                   headers=AUTH).json()
    assert 10.0 < j["deltaTC"] < 25.0
    assert j["fosbergFwi"] > 0
    assert j["chandlerBurningIndex"] > 50
    assert "densityAltitudeFt" in j
    # No frost story at 104 °F, and no 3h-old pressure yet → no tendency.
    assert "frostPointF" not in j
    assert "pressureTendency" not in j and "barometerSays" not in j


def test_derived_endpoint_404_without_data(client):
    r = client.get("/api/devices/DE:AD:BE:EF:00:01/derived", headers=AUTH)
    assert r.status_code == 404


# ── forecast snapshotter ────────────────────────────────────────────────

def test_forecast_snapshots_store_and_prune(client, monkeypatch):
    from app import forecast_snapshots as fs, db

    async def fake_fetch(lat, lon):
        return {"time": ["2026-08-25", "2026-08-26"],
                "temperature_2m_max": [108.1, 106.0],
                "temperature_2m_min": [84.0, 82.5],
                "precipitation_probability_max": [20, 55],
                "precipitation_sum": [0.0, 0.12]}

    monkeypatch.setattr(fs, "_fetch_daily", fake_fetch)
    devices = [{"mac": "AA", "info": {"coords": {"coords":
               {"lat": 33.3, "lon": -111.9}}}}]
    now = int(time.time() * 1000)
    # An ancient run, older than the 400-day retention: the write below
    # must sweep it (the "prune" this test's name promised but never
    # exercised — TEST_GAP_AUDIT).
    asyncio.run(db.insert_forecast_snapshots(
        "open-meteo", now - 401 * 86_400_000,
        [{"valid_date": "2020-01-01", "lead_days": 0}]))
    asyncio.run(fs.check(devices, now))

    async def rows():
        async with db.connect() as conn:
            return [dict(r) for r in await (await conn.execute(
                "SELECT * FROM forecast_snapshots ORDER BY valid_date"
            )).fetchall()]
    stored = asyncio.run(rows())
    assert len(stored) == 2, "the ancient run must be pruned on write"
    assert all(r["valid_date"] != "2020-01-01" for r in stored)
    assert stored[0]["provider"] == "open-meteo"
    assert stored[0]["tmax_f"] == 108.1
    assert stored[1]["pop"] == 55
    # Throttle: a second tick inside 6h stores nothing new.
    asyncio.run(fs.check(devices, now + 60_000))
    assert len(asyncio.run(rows())) == 2
