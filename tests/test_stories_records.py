"""Story engine (2.0): the "Record Broken" producer.

The Strava-PR card: today's rollup row, still filling at ingest, has
already passed a mark the station never reached. Fixtures seed a known
archive ending on a pinned TODAY and put the old record on a known day, so
every string below is asserted verbatim.

Two rules this suite exists to hold:
  · a young archive breaks no records: under MIN_RECORD_DAYS measured days
    there is no card, however hot the morning;
  · a tie is not a record, and neither is a margin inside the sensor's
    own jitter. The margins are compared in NATIVE units before anything
    is converted, so the same day breaks the same record in every scale.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:5B"
TODAY = date(2026, 8, 30)
# 400 measured days INCLUDING today, comfortably over the 365 floor.
ARCHIVE_DAYS = 400
FIRST = TODAY - timedelta(days=ARCHIVE_DAYS - 1)
# The standing record, and the day it was set. A later day ties it so the
# "set" date is the earliest one.
OLD_HOT_DAY = date(2025, 8, 5)
OLD_HOT_TIE = date(2025, 9, 1)
OLD_HOT = 103.4


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    """Rollup rows as ingest folds them. A key left out is SQL NULL."""
    async def run():
        async with db.connect() as conn:
            for r in rows:
                hi = r.get("hi")
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n, "
                    " windgustmph_max, rain_total) VALUES (?,?,?,?,?,?,?,?)",
                    (mac, r["day"], r.get("lo"), hi, hi,
                     1 if hi is not None else 0, r.get("gust"),
                     r.get("rain")))
            await conn.commit()
    asyncio.run(run())


def _archive(first: date = FIRST, last: date = TODAY - timedelta(days=1),
             ) -> dict[str, dict]:
    """A calm, bounded background: highs 85–95, lows 60–70, gusts 10–30,
    rain 0 except a few 0.20-inch days. The old records sit on top."""
    out: dict[str, dict] = {}
    d, i = first, 0
    while d <= last:
        wobble = (i * 7) % 11
        out[d.isoformat()] = {"day": d.isoformat(),
                              "hi": 85.0 + wobble, "lo": 60.0 + wobble,
                              "gust": 10.0 + 2 * wobble,
                              "rain": 0.20 if i % 37 == 5 else 0.0}
        d += timedelta(days=1)
        i += 1
    # A younger archive that starts after these days simply lacks them;
    # its record is then the background's 95°F.
    for day in (OLD_HOT_DAY, OLD_HOT_TIE):
        if day.isoformat() in out:
            out[day.isoformat()]["hi"] = OLD_HOT
    return out


def _today(**fields) -> dict:
    return {"day": TODAY.isoformat(), **fields}


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _records(units: stories.Units | None = None) -> dict:
    kw = {"units": units} if units is not None else {}
    return asyncio.run(stories.top_stories(
        MAC, families=[stories.FAMILY_RECORDS], limit=12, **kw))


def _card(units: stories.Units | None = None) -> dict | None:
    return next((s for s in _records(units)["stories"]
                 if s["story_type"] == "record_broken"), None)


def _stat(story: dict, key: str) -> dict:
    return next(s for s in story["supporting"] if s["key"] == key)


def _strings(story: dict) -> list[str]:
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"], story["period"]["label"]]
    out += [x["label"] for x in story["supporting"]]
    c = story["comparison"]
    out += [c["label"], c["baseline_label"], c["rank_line"]]
    out += [story["viz"]["axis_label"], story["viz"]["footnote"]]
    out += [b["label"] for b in story["viz"]["series"]]
    return out


# ───────────────────────── the happy path ─────────────────────────

def test_a_hotter_afternoon_than_any_on_record_is_a_card(engine):
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=104.2, lo=80.0, gust=12.0, rain=0.0)])
    s = _card()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("records", "record_broken")
    assert s["title"] == "Hottest day on record"
    assert s["id"] == "records.record_broken.2026-08-30.high.all_time"
    assert s["hero_line"] == "104.2°F SO FAR TODAY"
    assert s["hero"] == {"key": "today_high", "label": "high so far today",
                         "value": 104.2, "unit": "F", "precision": 1}
    # "So far": a record from a day still running says so everywhere.
    assert s["period"] == {"kind": "moment", "label": "August 30 so far",
                           "start": "2026-08-30", "end": "2026-08-30",
                           "partial": True}
    c = s["comparison"]
    assert c["kind"] == "vs_prior_record"
    assert (c["value"], c["baseline"]) == (104.2, 103.4)
    # The old record was SET on Aug 5 2025; Sep 1 only tied it.
    assert c["baseline_label"] == "previous record · August 5 2025"
    assert c["rank_line"] == ("104.2°F so far today, past 103.4°F on "
                              "August 5 2025, the hottest of 400 days on "
                              "record")
    assert (c["rank"], c["of"], c["direction"]) == (1, 400, "above")
    assert c["delta"] == pytest.approx(0.8, abs=1e-6)
    assert c["delta_pct"] is None, "a percentage of a temperature"
    assert s["context"] == (
        "Today's high stands at 104.2°F, past the 103.4°F measured here on "
        "August 5 2025. That makes it the hottest of 400 days on record, "
        "and it can only climb before midnight.")
    # An all-time record is the most interesting thing a day can do.
    assert s["interestingness"] == 1.0
    assert s["score_parts"]["scope"] == 1.0


def test_the_tiles_are_new_old_how_long_it_stood_and_the_years(engine):
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=104.2, lo=80.0, gust=12.0, rain=0.0)])
    s = _card()
    assert [x["key"] for x in s["supporting"]] == [
        "today_high", "previous_record", "record_stood", "years_of_record"]
    assert _stat(s, "previous_record")["value"] == 103.4
    assert _stat(s, "previous_record")["label"] == "previous record · August 5 2025"
    assert _stat(s, "record_stood") == {
        "key": "record_stood", "label": "days the old record stood",
        "value": (TODAY - OLD_HOT_DAY).days, "unit": "days", "precision": 0}
    years = _stat(s, "years_of_record")
    assert years["unit"] == "years" and years["value"] == pytest.approx(1.1, abs=0.05)


def test_the_chart_is_today_and_the_days_it_passed(engine):
    """The ranked-list kind the engine already renders: today as the full
    bar, then the top of the old order. Two near-equal bars for a record
    beaten by a hair is what a hair looks like."""
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=104.2, lo=80.0, gust=12.0, rain=0.0)])
    v = _card()["viz"]
    assert v["kind"] == "chaos_dimensions"
    assert v["highlight_key"] == "today" and v["highlight"] == "today"
    assert len(v["series"]) == stories.RECORD_ROWS
    first = v["series"][0]
    assert (first["key"], first["label"], first["score"], first["owned"]) == (
        "today", "so far today", 1.0, True)
    assert first["value"] == 104.2 and first["unit"] == "F"
    # The old record's two days, set date first, then the best of the rest.
    assert [b["key"] for b in v["series"][1:3]] == ["2025-08-05", "2025-09-01"]
    assert v["series"][1]["label"] == "Aug 5 2025"
    assert v["series"][1]["score"] < 1.0
    assert v["series"][1]["score"] > v["series"][3]["score"]
    assert all(not b["owned"] for b in v["series"][1:])
    assert "0.5°F" in v["footnote"]
    # Every row keyed, keys unique: the viz contract every template relies on.
    keys = [b["key"] for b in v["series"]]
    assert len(set(keys)) == len(keys)


# ───────────────────────── the decline paths ─────────────────────────

def test_a_tie_is_not_a_record(engine):
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=OLD_HOT, lo=80.0, gust=12.0, rain=0.0)])
    assert _card() is None
    assert "record_broken" in _records()["declined"]


def test_a_margin_inside_the_jitter_is_not_a_record(engine):
    """0.4°F over the old mark: a sensor's own wobble, not weather."""
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=OLD_HOT + 0.4, lo=80.0, gust=12.0, rain=0.0)])
    assert _card() is None
    _seed(engine, [_today(hi=OLD_HOT + 0.5, lo=80.0, gust=12.0, rain=0.0)])
    assert _card() is not None


def test_a_young_archive_breaks_no_records(engine):
    """200 days of history. The same 104.2°F afternoon, and no card: a
    record on a two-hundred-day archive is a description of the archive."""
    rows = list(_archive(first=TODAY - timedelta(days=199)).values())
    _seed(engine, rows)
    _seed(engine, [_today(hi=104.2, lo=80.0, gust=12.0, rain=0.0)])
    assert len(rows) + 1 == 200
    assert _card() is None
    assert "record_broken" in _records()["declined"]


def test_no_row_for_today_is_no_card(engine):
    """A station that stopped reporting has no "so far today", however
    hot its last day was."""
    archive = _archive()
    archive[(TODAY - timedelta(days=1)).isoformat()]["hi"] = 110.0
    _seed(engine, list(archive.values()))
    assert _card() is None


def test_the_floor_is_counted_per_metric(engine):
    """A rain gauge added last month cannot set a rain record on the
    thermometer's year of history: 400 days of temperature, 40 of rain."""
    archive = _archive()
    for i, r in enumerate(archive.values()):
        if i < ARCHIVE_DAYS - 41:
            r["rain"] = None
    _seed(engine, list(archive.values()))
    _seed(engine, [_today(hi=90.0, lo=70.0, gust=12.0, rain=2.50)])
    assert _card() is None


# ───────────────────────── month-of-year ─────────────────────────

def test_a_month_record_that_is_not_an_all_time_one_scores_lower(engine):
    """The all-time mark sits in September; today beats every prior
    August. A real record with a smaller claim, and the copy says both."""
    archive = _archive()
    archive[OLD_HOT_DAY.isoformat()]["hi"] = 95.0      # no August record now
    archive[OLD_HOT_TIE.isoformat()]["hi"] = 103.9     # Sept 1 holds all-time
    archive["2025-08-20"]["hi"] = 99.5                 # the August mark
    _seed(engine, list(archive.values()))
    _seed(engine, [_today(hi=101.0, lo=80.0, gust=12.0, rain=0.0)])
    s = _card()
    assert s is not None
    assert s["title"] == "Hottest August day on record"
    assert s["id"] == "records.record_broken.2026-08-30.high.month"
    assert s["interestingness"] == 0.7
    assert s["comparison"]["baseline"] == 99.5
    assert s["comparison"]["baseline_label"] == "previous record · August 20 2025"
    # Aug 2025 (31) + Aug 2026 through the 29th (29) + today = 61.
    assert s["comparison"]["rank_line"].endswith(
        "the hottest of 61 August days on record")
    assert s["context"] == (
        "Today's high stands at 101°F, past the 99.5°F measured here on "
        "August 20 2025. That makes it the hottest of 61 August days on "
        "record, though 103.9°F on September 1 2025 still holds the "
        "all-time mark, and it can only climb before midnight.")
    assert _stat(s, "years_of_record")["label"] == "years of August days on record"


def test_a_cold_record_reads_downward(engine):
    archive = _archive()
    archive["2026-01-15"]["lo"] = 28.0
    _seed(engine, list(archive.values()))
    _seed(engine, [_today(hi=90.0, lo=27.0, gust=12.0, rain=0.0)])
    s = _card()
    assert s["title"] == "Coldest night on record"
    assert s["hero_line"] == "27°F SO FAR TODAY"
    assert s["comparison"]["direction"] == "below"
    assert s["comparison"]["delta"] == pytest.approx(-1.0)
    assert s["comparison"]["rank_line"].endswith("the coldest of 400 nights on record")
    assert s["context"].endswith("and it can only fall before midnight.")
    assert "how far below this station's median low" in s["viz"]["axis_label"]


def test_one_card_the_biggest_claim(engine):
    """A high beaten by 0.8°F and a gust beaten by 20 mph on the same
    day: one card, and it is the one that cleared its threshold by more."""
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=104.2, lo=80.0, gust=50.0, rain=0.0)])
    out = _records()
    cards = [s for s in out["stories"] if s["story_type"] == "record_broken"]
    assert len(cards) == 1
    s = cards[0]
    assert s["title"] == "Strongest gust on record"
    assert s["hero_line"] == "50 MPH SO FAR TODAY"
    assert s["hero"]["unit"] == "mph" and s["hero"]["precision"] == 0
    assert s["comparison"]["delta_pct"] == pytest.approx(66.7, abs=0.05)
    assert s["comparison"]["rank_line"].endswith("the strongest of 400 gusts on record")


def test_a_wet_day_record_uses_the_rain_axis(engine):
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=90.0, lo=70.0, gust=12.0, rain=1.35)])
    s = _card()
    assert s["title"] == "Wettest day on record"
    assert s["hero_line"] == "1.35 IN SO FAR TODAY"
    assert s["viz"]["axis_label"] == "share of today's total"
    assert s["viz"]["series"][1]["value"] == 0.2
    assert s["viz"]["series"][1]["score"] == pytest.approx(0.2 / 1.35, abs=1e-3)


# ───────────────────────── the reader's units ─────────────────────────

def test_a_celsius_reader_gets_readings_by_offset_and_no_fahrenheit(engine):
    """104.2°F is 40.1°C by OFFSET; the 0.8°F margin is 0.4°C by SCALE.
    Run the margin through the reading conversion and it becomes −17.3°C,
    which is the trap this module keeps two conversions for. And the
    record is still broken: the margin was judged in native units."""
    _seed(engine, list(_archive().values()))
    _seed(engine, [_today(hi=104.2, lo=80.0, gust=12.0, rain=0.0)])
    s = _card(stories.Units(temperature="celsius", wind="kph", rain="mm"))
    assert s is not None
    assert s["hero"]["value"] == pytest.approx(40.1, abs=0.05)
    assert s["hero"]["unit"] == "C"
    assert s["hero_line"] == "40.1°C SO FAR TODAY"
    c = s["comparison"]
    assert c["baseline"] == pytest.approx(39.7, abs=0.05)
    assert c["delta"] == pytest.approx(0.8 * 5 / 9, abs=0.05)
    assert c["delta"] != pytest.approx((0.8 - 32) * 5 / 9, abs=0.5)
    assert c["rank_line"].startswith("40.1°C so far today, past 39.7°C on")
    for text in _strings(s):
        assert "°F" not in text, text
    assert "0.3°C" in s["viz"]["footnote"]          # the margin, by scale
    units = {x["unit"] for x in s["supporting"]} | {s["hero"]["unit"]}
    assert "F" not in units
    assert {b["unit"] for b in s["viz"]["series"]} == {"C"}
    assert s["viz"]["series"][1]["value"] == pytest.approx(39.7, abs=0.05)


def test_the_producer_is_registered_in_the_records_family():
    assert ("records", "record_broken") in stories.registered()
