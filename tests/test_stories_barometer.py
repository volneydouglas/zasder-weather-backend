"""Story engine (2.0): "The Barometer Says…".

A forecast from 1920 beside one from this morning. The Negretti & Zambra
slide rule turns one pressure reading and its three-hour trend into a
sentence; the card sets that next to what a numerical model said about the
same day and lets the reader enjoy the comparison.

IT DOES NOT SCORE THEM. "Who won" is the forecast-verification backlog item
wearing a costume — honest scoring needs forecasts captured at issue time,
matched to outcomes, over a season. This suite asserts the absence of a
winner as carefully as it asserts the presence of the pair, because a
scoreboard is exactly the feature a reader would assume had been earned.

The Zambretti implementation is app.derived's, the one the Science surface
already renders. Nothing here re-implements a slide rule.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:B0"
TODAY = date(2026, 8, 30)


def _ms(on: date, hour: int) -> int:
    return int(datetime(on.year, on.month, on.day, hour, 0,
                        tzinfo=timezone.utc).timestamp() * 1000)


def _seed_pressure(db, now_inhg: float, three_h_ago_inhg: float,
                   on: date = TODAY, mac: str = MAC) -> None:
    """Two readings three hours apart — the window the WMO tendency and the
    slide rule are both defined on."""
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM observations WHERE mac = ?", (mac,))
            await conn.commit()
        await db.insert_observations(mac, [
            {"dateutc": _ms(on, 12), "tempf": 95.0, "humidity": 20.0,
             "baromrelin": three_h_ago_inhg},
            {"dateutc": _ms(on, 15), "tempf": 104.0, "humidity": 18.0,
             "baromrelin": now_inhg},
        ])
    asyncio.run(run())


def _seed_forecast(db, hi: float | None = 104.0, lo: float | None = 79.0,
                   pop: float | None = 20.0, on: date = TODAY) -> None:
    async def run():
        await db.insert_forecast_snapshots(
            "open-meteo", _ms(on, 6),
            [{"valid_date": on.isoformat(), "lead_days": 0,
              "tmax_f": hi, "tmin_f": lo, "pop": pop, "precip_in": 0.0}])
    asyncio.run(run())


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _barom(mac: str = MAC, **kw) -> dict | None:
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_SCIENCE], limit=8, **kw))
    return next((s for s in out["stories"]
                 if s["story_type"] == "barometer_says"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


# ───────────────────────── the pair, not a contest ─────────────────────────

def test_it_sets_the_two_forecasts_side_by_side(engine):
    _seed_pressure(engine, 29.62, 29.74)          # falling hard
    _seed_forecast(engine)
    s = _barom()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("science", "barometer_says")
    assert s["title"] == "The Barometer Says…"
    rows = {e["key"]: e for e in s["viz"]["series"]}
    assert set(rows) == {"zambretti", "modern"}
    assert rows["zambretti"]["label"] == "The slide rule, 1920"
    assert rows["zambretti"]["verdict"] == s["hero_line"].capitalize() \
        or rows["zambretti"]["verdict"].upper() == s["hero_line"]
    assert "104°F over 79°F" in rows["modern"]["verdict"]
    assert "20% chance of rain" in rows["modern"]["verdict"]


def test_there_is_no_winner_anywhere_on_the_card(engine):
    """The scoreboard is the backlog item. A card that hinted at one would
    be claiming a verification pipeline that does not exist."""
    _seed_pressure(engine, 29.62, 29.74)
    _seed_forecast(engine)
    s = _barom()
    # The footnote is excluded and checked separately: like the fire card's
    # disclaimer, it is the one string allowed to use these words, and only
    # in order to refuse them.
    blob = " ".join([
        s["hero_line"], s["context"], s["viz"]["axis_label"],
        s["hero"]["label"],
        *[x["label"] for x in s["supporting"]],
        *[e["label"] + " " + e["verdict"] for e in s["viz"]["series"]],
    ]).lower()
    for banned in ("won", "winner", "beat", "more accurate", "score",
                   "correct", "right more often", "wins"):
        assert banned not in blob, (banned, blob)
    # No comparison object either: that field is where a delta would live,
    # and a delta between two forecasts IS a scoreboard.
    assert s["comparison"] is None
    # …and the footnote says out loud why there is no winner.
    foot = s["viz"]["footnote"]
    assert "not scored against each other" in foot
    assert "a season of forecasts matched to what happened" in foot


def test_the_slide_rule_verdict_is_deriveds(engine):
    """One implementation, shared with the Science surface."""
    from app import derived
    _seed_pressure(engine, 29.62, 29.74)
    s = _barom()
    code, word = derived.pressure_tendency_code(29.62 - 29.74)
    says = derived.zambretti(29.62 * 33.8639, word)
    assert s["hero_line"] == says.upper()
    assert says in s["context"]
    assert _stat(s, "tendency_code")["value"] == code
    assert word in _stat(s, "trend_3h")["label"]


def test_it_explains_where_the_old_answer_came_from(engine):
    _seed_pressure(engine, 29.62, 29.74)
    s = _barom()
    assert "Negretti & Zambra slide rule" in s["context"]
    assert "no satellites and no model" in s["context"]


# ─────────────── the modern half is optional, and says so ───────────────

def test_no_stored_forecast_leaves_the_slide_rule_unopposed_not_unbeaten(engine):
    """The distinction is the point: a missing second opinion is a missing
    second opinion, never a win."""
    _seed_pressure(engine, 29.62, 29.74)
    s = _barom()
    assert s is not None
    assert [e["key"] for e in s["viz"]["series"]] == ["zambretti"]
    assert "unopposed here, not unbeaten" in s["context"]
    assert _stat(s, "forecast_high") is None
    # Availability is NOT interestingness: having no forecast beside it
    # must not change what this card scores. Only the barometer counts.
    assert set(s["score_parts"]) == {"motion"}


def test_a_forecast_for_another_day_is_not_todays(engine):
    _seed_pressure(engine, 29.62, 29.74)
    _seed_forecast(engine, on=TODAY + timedelta(days=2))
    s = _barom()
    assert [e["key"] for e in s["viz"]["series"]] == ["zambretti"]


def test_the_freshest_issue_for_today_wins(engine):
    _seed_pressure(engine, 29.62, 29.74)
    _seed_forecast(engine, hi=99.0, lo=70.0, pop=5.0)

    async def later():
        from app import db
        await db.insert_forecast_snapshots(
            "open-meteo", _ms(TODAY, 11),
            [{"valid_date": TODAY.isoformat(), "lead_days": 0,
              "tmax_f": 104.0, "tmin_f": 79.0, "pop": 20.0,
              "precip_in": 0.0}])
    asyncio.run(later())
    s = _barom()
    assert _stat(s, "forecast_high")["value"] == 104.0


# ───────────────────────── decline paths ─────────────────────────

def test_no_reading_three_hours_back_declines(engine):
    """Zambretti is a function OF the trend. Defaulting an unknown trend to
    "steady" would invent the input and print a confident sentence on it."""
    async def run():
        from app import db
        await db.insert_observations(MAC, [
            {"dateutc": _ms(TODAY, 15), "tempf": 104.0,
             "baromrelin": 29.62}])
    asyncio.run(run())
    assert _barom() is None


def test_a_station_with_no_sea_level_pressure_declines(engine):
    async def run():
        from app import db
        await db.insert_observations(MAC, [
            {"dateutc": _ms(TODAY, 12), "tempf": 95.0, "baromabsin": 28.0},
            {"dateutc": _ms(TODAY, 15), "tempf": 104.0, "baromabsin": 28.1}])
    asyncio.run(run())
    assert _barom() is None


def test_a_stale_reading_is_not_right_now(engine):
    _seed_pressure(engine, 29.62, 29.74, on=date(2026, 8, 20))
    assert _barom() is None


def test_a_station_that_never_reported_declines(engine):
    assert engine is not None
    assert _barom() is None


# ───────────────────────── units and calibration ─────────────────────────

def test_pressure_and_its_change_both_convert(engine):
    """A pressure CHANGE is a difference, but every pressure unit is linear
    with no offset, so one conversion serves both — and the SIGN, which is
    the whole meaning, has to survive it."""
    _seed_pressure(engine, 29.62, 29.74)
    c = _barom(units=stories.Units(pressure="hPa"))
    assert _stat(c, "pressure")["unit"] == "hPa"
    assert _stat(c, "pressure")["value"] == pytest.approx(1003.0, abs=0.6)
    assert _stat(c, "trend_3h")["value"] < 0, "a falling barometer stays falling"


def test_a_moving_barometer_outscores_a_still_one(engine):
    _seed_pressure(engine, 29.62, 29.74)          # −0.12 inHg in three hours
    _seed_forecast(engine)
    falling = _barom()
    assert 0.0 <= falling["interestingness"] <= 1.0
    assert falling["score_parts"]["motion"] == 1.0

    _seed_pressure(engine, 29.90, 29.90)          # dead still
    _seed_forecast(engine)
    still = _barom()
    assert still is not None
    assert 0.0 <= still["interestingness"] <= 1.0
    assert still["score_parts"]["motion"] == 0.0
    assert still["interestingness"] < falling["interestingness"]
    # The pair being present or absent moves nothing.
    assert set(falling["score_parts"]) == {"motion"}


def test_a_foreign_providers_forecast_for_today_is_not_the_modern_one(engine):
    """The card labels its second column "open-meteo". A second provider
    now exists in `forecast_snapshots` (the Zambretti ledger stores its own
    daily call there, with no temperatures), so the lookup must ask for the
    provider it names, not the newest row for the date whoever wrote it."""
    _seed_pressure(engine, 29.62, 29.74)

    async def foreign():
        await engine.insert_forecast_snapshots(
            "zambretti", _ms(TODAY, 9),
            [{"valid_date": TODAY.isoformat(), "lead_days": 0,
              "tmax_f": 50.0, "tmin_f": 40.0, "pop": 90.0,
              "precip_in": None}])
    asyncio.run(foreign())
    s = _barom()
    assert s is not None
    # Nothing from open-meteo: the slide rule stands unopposed.
    assert {e["key"] for e in s["viz"]["series"]} == {"zambretti"}
    assert _stat(s, "forecast_high") is None
    assert "unopposed" in s["context"]

    # Both present, the foreign row newer: open-meteo's numbers, not the
    # newest row's.
    _seed_forecast(engine)                      # issued 06:00, open-meteo
    s = _barom()
    assert _stat(s, "forecast_high")["value"] == 104.0
    assert _stat(s, "forecast_pop")["value"] == 20


def _ms_at(on: date, hour: int, minute: int) -> int:
    return int(datetime(on.year, on.month, on.day, hour, minute,
                        tzinfo=timezone.utc).timestamp() * 1000)


def _seed_pair(db, now_hm: tuple[int, int], past_hm: tuple[int, int],
               on: date = TODAY, mac: str = MAC) -> None:
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM observations WHERE mac = ?", (mac,))
            await conn.commit()
        await db.insert_observations(mac, [
            {"dateutc": _ms_at(on, *past_hm), "tempf": 95.0,
             "baromrelin": 29.74},
            {"dateutc": _ms_at(on, *now_hm), "tempf": 104.0,
             "baromrelin": 29.62},
        ])
    asyncio.run(run())


def test_a_change_labelled_three_hours_is_not_allowed_to_span_six(engine):
    """`compute_call` looks for its anchor at or before obs−3h with a 3h
    freshness floor, so after an outage the anchor can sit anywhere in
    [obs−6h, obs−3h]. The card's labels ("change in three hours", "falling
    for three hours") are written here, so the card declines when the
    span it measured is not the span it names."""
    # 15:00 now, 09:00 anchor: a six-hour span the label would call three.
    _seed_pair(engine, (15, 0), (9, 0))
    assert _barom() is None
    # 3h20 is within a poller's slack of three hours and still ships.
    _seed_pair(engine, (15, 0), (11, 40))
    s = _barom()
    assert s is not None
    assert _stat(s, "trend_3h")["label"].startswith("change in three hours")
    # And exactly three hours, the textbook case, is untouched.
    _seed_pair(engine, (15, 0), (12, 0))
    assert _barom() is not None
