"""Story engine (2.1): "1920 vs 2026", the barometer scorecard.

barometer_says promised a scoreboard once a season of calls existed. This
is that card: the ledger's morning calls matched to the station's own
gauge, and to the numerical model's forecast as it stood BEFORE the call.
The suite pins the classification of the slide rule's sentences, the
matching rules (unscorable days dropped, never counted dry; the model
judged only on days it had a word in before 09:00), the season floor, and
the anchor rule the ledger and the card now share.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402
from app import zambretti_ledger as zl  # noqa: E402

MAC = "AA:BB:CC:00:00:5C"
TODAY = date(2026, 8, 30)


def _ms(on: date, hour: int, minute: int = 0) -> int:
    return int(datetime(on.year, on.month, on.day, hour, minute,
                        tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


async def _file_call(db, on: date, sentence: str, mac: str = MAC) -> None:
    async with db.connect() as conn:
        await zl._ensure_table(conn)
        await conn.execute(
            "INSERT OR IGNORE INTO zambretti_calls "
            "(mac, day, issued_ms, slp_inhg, trend, call) VALUES (?, ?, ?, ?, ?, ?)",
            (mac, on.isoformat(), _ms(on, 9), 29.9, "steady", sentence))
        await conn.commit()


async def _rain_day(db, on: date, inches: float | None, mac: str = MAC) -> None:
    """A daily rollup row for the day; None = the gauge never reported."""
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, tempf_max, "
            "tempf_sum, tempf_n, rain_total) VALUES (?, ?, 70, 90, 160, 2, ?)",
            (mac, on.isoformat(), inches))
        await conn.commit()


async def _forecast(db, on: date, pop: float, issued_hour: int = 6) -> None:
    await db.insert_forecast_snapshots(
        stories.FORECAST_PROVIDER, _ms(on, issued_hour),
        [{"valid_date": on.isoformat(), "lead_days": 0,
          "tmax_f": 90.0, "tmin_f": 70.0, "pop": pop, "precip_in": 0.0}])


def _season(db, days: int, *, rain_every: int = 3, with_model: bool = True,
            model_right: bool = True):
    """`days` consecutive days ending yesterday: the slide rule calls rain
    on every third day, it rains on every `rain_every`th day, and the
    model (when present) is right or wrong on every day."""
    async def run():
        for i in range(days):
            on = TODAY - timedelta(days=days - i)
            rained = (i % rain_every) == 0
            calls_rain = (i % 3) == 0
            await _file_call(db, on, "Rain at times, worse later" if calls_rain
                             else "Fine weather")
            await _rain_day(db, on, 0.25 if rained else 0.0)
            if with_model:
                pop = 80.0 if (rained == model_right) else 10.0
                await _forecast(db, on, pop)
    asyncio.run(run())


def _card(mac: str = MAC) -> dict | None:
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_SCIENCE], limit=12, min_score=0.0))
    return next((s for s in out["stories"]
                 if s["story_type"] == "barometer_scorecard"), None)


def test_every_slide_rule_sentence_is_filed_as_rain_or_dry():
    from app import derived
    for sentence in derived._ZAMBRETTI_TEXT.values():
        assert zl.call_expects_rain(sentence) is not None, sentence
    assert zl.call_expects_rain("Fine, possibly showers") is False, \
        "a hedged 'fine' is a dry call; the headline led with fine"
    assert zl.call_expects_rain("Stormy, much rain") is True
    assert zl.call_expects_rain("Partly cloudy") is None, \
        "a sentence the slide rule never says scores as nothing"


def test_the_card_declines_below_a_season(engine):
    _season(engine, zl.SCORECARD_MIN_DAYS - 1)
    assert _card() is None


def test_a_season_scores_both_instruments_on_the_same_days(engine):
    _season(engine, 45, rain_every=3, with_model=True, model_right=True)
    card = _card()
    assert card is not None
    # Calls rain on every third day and it rains on every third day: the
    # slide rule is right every day; the model was told to be right too.
    assert card["hero"]["value"] == 100
    assert card["hero_line"] == "RIGHT 100% OF DAYS"
    stats = {s["key"]: s["value"] for s in card["supporting"]}
    assert stats["days"] == 45 and stats["rain_days"] == 15
    assert stats["zambretti_hits"] == 45
    assert stats["modern_days"] == 45 and stats["modern_hits"] == 45
    assert card["comparison"]["direction"] == "level"
    assert card["viz"]["kind"] == "scorecard_bars"
    keys = [s["key"] for s in card["viz"]["series"]]
    assert keys == ["zambretti", "modern"]
    assert card["period"]["kind"] == "spell" and card["period"]["partial"] is True
    assert card["score_parts"]["season"] == pytest.approx(45 / 180, abs=1e-4)


def test_a_wrong_model_loses_to_the_slide_rule(engine):
    _season(engine, 40, rain_every=3, with_model=True, model_right=False)
    card = _card()
    assert card is not None
    assert card["comparison"]["direction"] == "above", \
        "the slide rule beat the model, so the hero sits ABOVE the baseline"
    assert card["comparison"]["baseline"] == 0.0
    assert "slide rule ahead by 100 points" in card["context"]


def test_days_the_gauge_never_reported_are_dropped_not_dry(engine):
    _season(engine, 40)

    async def blank_out():
        for i in range(5):
            on = TODAY - timedelta(days=40 - i)
            await _rain_day(engine, on, None)      # gauge silent
    asyncio.run(blank_out())
    card = _card()
    assert card is not None
    stats = {s["key"]: s["value"] for s in card["supporting"]}
    assert stats["days"] == 35, "a silent gauge is not a dry day"
    assert stats["modern_days"] == 35


def test_five_silent_days_can_take_the_card_below_the_season_floor(engine):
    _season(engine, zl.SCORECARD_MIN_DAYS + 2)

    async def blank_out():
        for i in range(5):
            on = TODAY - timedelta(days=zl.SCORECARD_MIN_DAYS + 2 - i)
            await _rain_day(engine, on, None)
    asyncio.run(blank_out())
    assert _card() is None


def test_the_model_is_judged_only_on_what_it_said_before_the_call(engine):
    """A forecast issued at 11:00, after the 09:00 call, is a second look
    the barometer never got; it must not count. With none before the
    call on any day, the slide rule is scored alone."""
    async def run():
        for i in range(32):
            on = TODAY - timedelta(days=32 - i)
            await _file_call(engine, on, "Fine weather")
            await _rain_day(engine, on, 0.0)
            await _forecast(engine, on, 90.0, issued_hour=11)
    asyncio.run(run())
    card = _card()
    assert card is not None
    assert card["comparison"] is None
    assert [s["key"] for s in card["viz"]["series"]] == ["zambretti"]
    assert "scored alone" in card["context"]
    assert "verdict" not in card["score_parts"]


def test_a_rain_day_is_one_tip_of_the_gauge(engine):
    """0.01 in is rain; 0.0 is not; the threshold is in storage units."""
    async def run():
        for i in range(31):
            on = TODAY - timedelta(days=31 - i)
            await _file_call(engine, on, "Rain at times, worse later")
            await _rain_day(engine, on, 0.01 if i % 2 else 0.0)
    asyncio.run(run())
    card = _card()
    assert card is not None
    stats = {s["key"]: s["value"] for s in card["supporting"]}
    assert stats["rain_days"] == 15
    assert stats["zambretti_hits"] == 15


def test_the_ledger_now_refuses_the_stale_anchor_the_card_refused(engine):
    """Before 2.1 compute_call accepted an anchor up to six hours old
    while the card declined past 3h30. One constant now, both readers."""
    from app import db
    on = TODAY

    async def run():
        await db.insert_observations(MAC, [
            {"dateutc": _ms(on, 15) - zl.TREND_MS - zl.ANCHOR_SLACK_MS - 60_000,
             "baromrelin": 29.80},
            {"dateutc": _ms(on, 15), "baromrelin": 29.95},
        ])
        obs = await db.latest_observation(MAC)
        return await zl.compute_call(MAC, obs)
    assert asyncio.run(run()) is None
    assert stories.BAROMETER_ANCHOR_SLACK_MS == zl.ANCHOR_SLACK_MS


def test_an_anchor_inside_the_slack_still_files(engine):
    from app import db
    on = TODAY

    async def run():
        await db.insert_observations(MAC, [
            {"dateutc": _ms(on, 15) - zl.TREND_MS - zl.ANCHOR_SLACK_MS + 60_000,
             "baromrelin": 29.80},
            {"dateutc": _ms(on, 15), "baromrelin": 29.95},
        ])
        obs = await db.latest_observation(MAC)
        return await zl.compute_call(MAC, obs)
    call = asyncio.run(run())
    assert call is not None and call.trend == "rising"
