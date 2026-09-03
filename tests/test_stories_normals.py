"""Story engine (2.0): the "vs the 30-year normal" context line.

`app.normals` has held NOAA/NCEI 1991-2020 normals since 1.7 and no
producer ever read them; every "normal" on a card was the station's own
history. Now three cards carry ONE sentence against the real normal for
the date: the heat ledger's hottest day, the cold ledger's coldest night,
and the wildest day's own reading.

The rules this suite holds:
  · CACHE ONLY. `normals.today()` fetches from NCEI on a cold cache; a
    producer never may. The engine reads `normals.cached_year`, which
    reads server_kv and nothing else, and the line is absent when nothing
    is cached. Absent, never computed from the station's own history.
  · A departure converts by SCALE for a Celsius reader.
  · The NCEI station's name appears in the chart footnote only, never in
    copy (the geography rule).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import normals, stories  # noqa: E402

MAC = "AA:BB:CC:00:00:9C"
TODAY = date(2026, 8, 30)
LAT, LON = 33.3004, -111.9378
STATION_ID = "USW00023183"
STATION_NAME = "PHOENIX SKY HARBOR"
# Two engineered days on a flat record: the hottest afternoon and the
# coldest night of 2026, each with a known normal for its date.
HOT_DAY, HOT_HI = date(2026, 7, 3), 110.0
# 40°F reaches the warm-climate ladder's mildest tier (45°F); 50°F would
# reach nothing and the cold ledger would rightly decline.
COLD_DAY, COLD_LO = date(2026, 1, 12), 40.0
NORMALS = {"07-03": {"high": 104.0, "low": 80.0},
           "01-12": {"high": 66.0, "low": 44.0}}


def _seed(db, mac: str = MAC) -> None:
    """A flat 85/60 record from July 2025 through today, plus the two
    engineered days."""
    async def run():
        async with db.connect() as conn:
            d = date(2025, 7, 1)
            while d <= TODAY:
                hi, lo = 85.0, 60.0
                if d == HOT_DAY:
                    hi = HOT_HI
                if d == COLD_DAY:
                    lo = COLD_LO
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mac, d.isoformat(), lo, hi, hi, 1))
                d += timedelta(days=1)
            await conn.commit()
    asyncio.run(run())


def _place(db, coords: tuple[float, float] | None, mac: str = MAC) -> None:
    async def run():
        await db.upsert_device(mac, {"name": "Backyard"})
        if coords is not None:
            await db.set_device_location(mac, coords[0], coords[1],
                                         "Backyard", 1_700_000_000_000)
    asyncio.run(run())


def _cache(db, days: dict = NORMALS, station: bool = True) -> None:
    """What `normals.today()` leaves in server_kv after one live call."""
    async def run():
        if station:
            await db.set_kv(
                f"{normals._KV_STATION}.{normals._coord_key(LAT, LON)}",
                json.dumps({"key": normals._coord_key(LAT, LON),
                            "id": STATION_ID, "name": STATION_NAME}))
        await db.set_kv(f"{normals._KV_DATA}.{STATION_ID}",
                        json.dumps({"station": STATION_ID,
                                    "fetched_ms": 1_700_000_000_000,
                                    "days": days}))
    asyncio.run(run())


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "forecast_lat", None)
    monkeypatch.setattr(settings, "forecast_lon", None)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)

    # The network is OFF. A producer that reaches NCEI fails this suite.
    async def boom(*a, **k):
        raise AssertionError("a story producer went to the network")
    monkeypatch.setattr(normals, "_find_station", boom)
    monkeypatch.setattr(normals, "_fetch_year", boom)
    _seed(db)
    return db


def _stories(units: stories.Units | None = None) -> dict[str, dict]:
    kw = {"units": units} if units is not None else {}
    out = asyncio.run(stories.top_stories(MAC, limit=20, **kw))
    by_type: dict[str, dict] = {}
    for s in out["stories"]:
        # The wildest day ships two scopes; the year scope is the one with
        # the engineered day.
        if s["story_type"] == "wildest_day" and s["period"]["kind"] != "year":
            continue
        by_type[s["story_type"]] = s
    return by_type


def _copy(story: dict) -> str:
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"]]
    out += [x["label"] for x in story["supporting"]]
    if story["comparison"]:
        c = story["comparison"]
        out += [c["label"], c["baseline_label"], c["rank_line"] or ""]
    out += [b["label"] for b in story["viz"]["series"]]
    out.append(story["viz"]["axis_label"] or "")
    return " ".join(out)


# ───────────────────────── present ─────────────────────────

def test_the_three_cards_carry_the_line_from_the_cache(engine):
    _place(engine, (LAT, LON))
    _cache(engine)
    s = _stories()

    heat = s["heat_ledger"]
    assert heat["context"].endswith(
        " The hottest day, July 3, ran 6°F above the 30-year normal high "
        "for the date.")
    cold = s["cold_ledger"]
    assert cold["context"].endswith(
        " The coldest night, January 12, ran 4°F below the 30-year normal "
        "low for the date.")
    wild = s["wildest_day"]
    assert wild["hero_line"].startswith("JUL 3 WAS")
    assert wild["context"].endswith(
        " Its high ran 6°F above the 30-year normal high for the date.")


def test_the_station_name_is_a_footnote_never_copy(engine):
    _place(engine, (LAT, LON))
    _cache(engine)
    for s in _stories().values():
        if "30-year normal" not in s["context"]:
            continue
        assert STATION_NAME not in _copy(s), s["story_type"]
        assert s["viz"]["footnote"] == (
            "Normals for the date: NOAA/NCEI 1991-2020 U.S. Climate Normals, "
            "PHOENIX SKY HARBOR.")


def test_a_reading_on_the_normal_says_so_rather_than_zero(engine):
    _place(engine, (LAT, LON))
    _cache(engine, {"07-03": {"high": HOT_HI, "low": 80.0}})
    heat = _stories()["heat_ledger"]
    assert heat["context"].endswith(
        "The hottest day, July 3, landed right on the 30-year normal high "
        "for the date.")


# ───────────────────────── absent ─────────────────────────

def test_nothing_cached_means_no_line_and_no_footnote(engine):
    """No kv, no line. NOT a line computed from the station's own history:
    that fallback is the one the normals module was built to refuse."""
    _place(engine, (LAT, LON))
    s = _stories()
    for story in s.values():
        assert "30-year normal" not in story["context"], story["story_type"]
    for kind in ("heat_ledger", "cold_ledger", "wildest_day"):
        assert s[kind]["viz"]["footnote"] is None, kind


def test_a_station_choice_with_no_year_behind_it_is_nothing(engine):
    _place(engine, (LAT, LON))
    _cache(engine, station=False)      # data under the id, no station choice
    assert all("30-year normal" not in s["context"]
               for s in _stories().values())
    assert asyncio.run(normals.cached_year(LAT, LON)) is None


def test_a_station_with_no_coordinates_gets_no_line(engine):
    """The cache is warm for Chandler; this station has no position and
    the server has no forecast location. Nothing to look up."""
    _place(engine, None)
    _cache(engine)
    for s in _stories().values():
        assert "30-year normal" not in s["context"], s["story_type"]


def test_a_date_missing_from_the_table_is_absent_not_zero(engine):
    _place(engine, (LAT, LON))
    _cache(engine, {"01-12": {"high": 66.0, "low": 44.0}})    # no July 3
    s = _stories()
    assert "30-year normal" not in s["heat_ledger"]["context"]
    assert s["heat_ledger"]["viz"]["footnote"] is None
    assert "30-year normal" in s["cold_ledger"]["context"]


def test_cached_year_reads_kv_and_only_kv(engine):
    _cache(engine)
    got = asyncio.run(normals.cached_year(LAT, LON))
    assert got == (STATION_NAME, NORMALS)
    # A different location is a different key, and a miss.
    assert asyncio.run(normals.cached_year(48.85, 2.35)) is None


# ───────────────────────── the reader's units ─────────────────────────

def test_a_celsius_reader_sees_the_departure_by_scale(engine):
    """6°F above normal is 3.3°C above normal. Through the reading
    conversion it would be −14.4°C, which is not a departure at all."""
    _place(engine, (LAT, LON))
    _cache(engine)
    s = _stories(stories.Units(temperature="celsius"))
    assert s["heat_ledger"]["context"].endswith(
        " The hottest day, July 3, ran 3.3°C above the 30-year normal high "
        "for the date.")
    assert s["wildest_day"]["context"].endswith(
        " Its high ran 3.3°C above the 30-year normal high for the date.")
    for story in s.values():
        assert "°F" not in story["context"], story["story_type"]
