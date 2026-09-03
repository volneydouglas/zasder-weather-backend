"""Story engine (2.0): the "Lightning Season" producer.

THE CAPABILITY FILTER IS THE WHOLE PRODUCER. `lightning_last_1hr` leaks
onto stations with no detector (a console posts 0, a relay forwards it),
and the rollups fold it into `lightning_max` as a real-looking 0. A
station whose entire record is zeros has never detected a strike and is
not a station where it never thunders: no card, however many rows.

The closest strike is the one number that comes from the observations
table (distance is not in the rollups), through a bounded window read
gated on the trailing hour holding strikes, the same gate the app's tile
and chart apply, so a stale "last strike distance" cannot become the
closest strike of a storm that ended weeks earlier.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:7A"
TODAY = date(2026, 8, 30)
SINCE = TODAY - timedelta(days=stories.LIGHTNING_WINDOW_DAYS - 1)   # Jun 2


def _seed(db, rows: list[tuple[str, float | None]], mac: str = MAC) -> None:
    """(day, lightning_max) rollup rows. None is a day the detector said
    nothing; 0.0 is a measured calm day."""
    async def run():
        async with db.connect() as conn:
            for day, peak in rows:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, lightning_max) VALUES (?, ?, ?)",
                    (mac, day, peak))
            await conn.commit()
    asyncio.run(run())


def _days(first: date, last: date, peak: float | None = 0.0
          ) -> list[tuple[str, float | None]]:
    out, d = [], first
    while d <= last:
        out.append((d.isoformat(), peak))
        d += timedelta(days=1)
    return out


def _with(rows: list[tuple[str, float | None]],
          strikes: dict[str, float]) -> list[tuple[str, float | None]]:
    return [(d, strikes.get(d, p)) for d, p in rows]


def _seed_obs(db, rows: list[tuple[date, int, int | None, float | None]],
              mac: str = MAC) -> None:
    """(day, hour UTC, strikes in the trailing hour, distance in miles)
    observations. The suite pins TIMEZONE=UTC so the local day is the
    calendar day."""
    async def run():
        payload = []
        for d, hour, strikes, dist in rows:
            ms = int(datetime(d.year, d.month, d.day, hour, 0,
                              tzinfo=timezone.utc).timestamp() * 1000)
            obs: dict = {"dateutc": ms, "tempf": 90.0}
            if strikes is not None:
                obs["lightning_last_1hr"] = strikes
            if dist is not None:
                obs["lightning_distance_mi"] = dist
            payload.append(obs)
        await db.insert_observations(mac, payload)
    asyncio.run(run())


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
                 if s["story_type"] == "lightning_season"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


# The season: five lightning days inside the window, the busiest on
# Aug 19, and a calm detector on every other day.
SEASON = {"2026-06-20": 40.0, "2026-07-08": 120.0, "2026-07-30": 15.0,
          "2026-08-12": 210.0, "2026-08-19": 731.0}


@pytest.fixture()
def season(engine):
    _seed(engine, _with(_days(date(2026, 1, 1), TODAY), SEASON))
    # Aug 12: a strike two miles out while the trailing hour held strikes.
    # Aug 19: the busiest hour, eight miles out. Aug 25: the detector still
    # reporting a half-mile "last strike" with NO strikes in the hour, the
    # stale value the gate exists for.
    _seed_obs(engine, [(date(2026, 8, 12), 15, 12, 2.1),
                       (date(2026, 8, 19), 16, 731, 8.0),
                       (date(2026, 8, 25), 12, 0, 0.5)])
    return engine


# ───────────────────────── the capability filter ─────────────────────────

def test_a_station_that_never_counted_a_strike_has_no_card(engine):
    """240 days of lightning_max = 0: the leaked field, folded faithfully.
    Rows are not a detector."""
    _seed(engine, _days(date(2026, 1, 1), TODAY, 0.0))
    assert _card() is None
    assert "lightning_season" in _records()["declined"]


def test_rows_with_no_lightning_at_all_are_no_card_either(engine):
    _seed(engine, _days(date(2026, 1, 1), TODAY, None))
    assert _card() is None


def test_one_strike_anywhere_in_the_archive_unlocks_a_measured_zero(engine):
    """The mirror: a detector that once counted a strike is a detector,
    and its zeros are real calm days. It still needs a season to talk
    about, so this is a decline for the OTHER reason."""
    rows = _with(_days(date(2025, 1, 1), TODAY), {"2025-07-04": 55.0})
    _seed(engine, rows)
    out = _records()
    assert "lightning_season" in out["declined"]


# ───────────────────────── the floor ─────────────────────────

def test_two_lightning_days_are_a_storm_not_a_season(engine):
    rows = _with(_days(date(2026, 1, 1), TODAY),
                 {"2026-08-12": 210.0, "2026-08-19": 731.0})
    _seed(engine, rows)
    assert _card() is None
    # Three is the floor, and a strike day from before the window does
    # not count toward it.
    rows = _with(_days(date(2026, 1, 1), TODAY),
                 {"2026-03-01": 90.0, "2026-08-12": 210.0, "2026-08-19": 731.0})
    _seed(engine, rows)
    assert _card() is None
    rows = _with(_days(date(2026, 1, 1), TODAY),
                 {"2026-07-01": 90.0, "2026-08-12": 210.0, "2026-08-19": 731.0})
    _seed(engine, rows)
    assert _card() is not None


# ───────────────────────── the happy path ─────────────────────────

def test_five_lightning_days_with_a_known_closest_strike(season):
    s = _card()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("records", "lightning_season")
    assert s["title"] == "Lightning Season"
    assert s["id"] == "records.lightning_season.2026-08-30"
    assert s["hero"] == {"key": "lightning_days",
                         "label": "days with lightning in the last 90 days",
                         "value": 5, "unit": "days", "precision": 0}
    assert s["hero_line"] == "5 LIGHTNING DAYS IN 90"
    assert s["context"] == (
        "Lightning was detected here on 5 of the last 90 days, with the "
        "detector reporting on 90 of them. The busiest was August 19, "
        "peaking at 731 strikes an hour, and the closest strike came "
        "within 2.1 mi on August 12.")
    assert s["period"] == {"kind": "spell", "label": "the last 90 days",
                           "start": SINCE.isoformat(), "end": "2026-08-30",
                           "partial": True}

    assert _stat(s, "busiest_day") == {
        "key": "busiest_day", "label": "busiest day · August 19",
        "value": 731, "unit": "strikes/hr", "precision": 0}
    # MIN distance, never max, and labelled "closest". The stale half-mile
    # reading on Aug 25 (no strikes in the hour) did not win.
    assert _stat(s, "closest_strike") == {
        "key": "closest_strike", "label": "closest strike · August 12",
        "value": 2.1, "unit": "mi", "precision": 1}
    assert _stat(s, "days_reporting")["value"] == 90


def test_the_chart_is_the_busiest_days_as_bars(season):
    v = _card()["viz"]
    assert v["kind"] == "chaos_dimensions"
    assert v["unit"] == "strikes/hr"
    assert [b["key"] for b in v["series"]] == [
        "2026-08-19", "2026-08-12", "2026-07-08", "2026-06-20", "2026-07-30"]
    assert v["series"][0] == {"key": "2026-08-19", "label": "Aug 19",
                              "score": 1.0, "value": 731,
                              "unit": "strikes/hr", "precision": 0,
                              "owned": True}
    assert v["series"][1]["score"] == pytest.approx(210 / 731, abs=1e-3)
    assert sum(1 for b in v["series"] if b["owned"]) == 1
    assert v["highlight_key"] == "2026-08-19"
    assert "no daily total" in v["footnote"]


def test_no_distance_source_drops_the_tile_and_rescores_on_frequency(engine):
    """A source that carries no distance: no closest-strike tile, no
    proximity term, and the score is frequency alone rather than
    frequency plus a proximity of zero."""
    _seed(engine, _with(_days(date(2026, 1, 1), TODAY), SEASON))
    s = _card()
    assert s is not None
    assert _stat(s, "closest_strike") is None
    assert "closest strike" not in s["context"]
    assert s["context"].endswith("peaking at 731 strikes an hour.")
    assert set(s["score_parts"]) == {"frequency"}
    assert s["interestingness"] == pytest.approx(5 / stories.LIGHTNING_BUSY_DAYS)


def test_a_stale_distance_with_no_strikes_in_the_hour_is_not_a_strike(engine):
    _seed(engine, _with(_days(date(2026, 1, 1), TODAY), SEASON))
    _seed_obs(engine, [(date(2026, 8, 25), 12, 0, 0.5),
                       (date(2026, 8, 26), 12, None, 0.3)])
    assert _stat(_card(), "closest_strike") is None


def test_the_comparison_needs_the_detector_to_have_covered_last_year(engine):
    """Same 90 days a year earlier: only when the detector was reporting
    then. A year before the detector was installed is not a year with no
    lightning."""
    _seed(engine, _with(_days(date(2026, 1, 1), TODAY), SEASON))
    assert _card()["comparison"] is None

    # Last year's window measured, with two lightning days in it.
    _seed(engine, _with(_days(date(2025, 5, 1), date(2025, 9, 30)),
                        {"2025-07-15": 30.0, "2025-08-02": 80.0}))
    c = _card()["comparison"]
    assert c is not None
    assert c["kind"] == "prior_year_same_window"
    assert (c["value"], c["baseline"], c["delta"]) == (5, 2, 3)
    assert c["direction"] == "above"
    assert c["baseline_label"] == "lightning days, Jun 2 – Aug 30 2025"
    assert c["rank_line"] == "the busier of 2 comparable seasons"


# ───────────────────────── the reader's units ─────────────────────────

def test_a_metric_reader_gets_kilometres_and_no_miles(season):
    """The app has no distance preference, so distance rides the rain
    axis: millimetres means kilometres. 2.1 mi is 3.4 km."""
    s = _card(stories.Units(temperature="celsius", wind="kph", rain="mm",
                            pressure="hPa"))
    closest = _stat(s, "closest_strike")
    assert closest["unit"] == "km"
    assert closest["value"] == pytest.approx(3.4, abs=0.05)
    assert "within 3.4 km on August 12" in s["context"]
    texts = [s["context"], s["hero_line"], s["viz"]["axis_label"],
             s["viz"]["footnote"]] + [x["label"] for x in s["supporting"]]
    assert not any(" mi" in t for t in texts), texts


def test_the_producer_is_registered_in_the_records_family():
    assert ("records", "lightning_season") in stories.registered()
