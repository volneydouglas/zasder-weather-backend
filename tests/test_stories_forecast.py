"""Story engine (2.0): "The Forecast vs. the Backyard".

`forecast_snapshots` has held every Open-Meteo call AS ISSUED since 1.8
and nothing joined it to the rollups. This producer does, for the
day-ahead call over one month: mean signed error on the high and the low,
the largest single miss, and the rain calls.

Fixtures seed the snapshot table and daily_rollups directly with a KNOWN
bias, so every number below is fixed by the fixture and every string is
asserted verbatim. Errors are forecast minus measured: "ran warm" means
the model promised more heat than arrived.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:F0"
# The 30th: August is complete through the 29th and the current month is
# the one with data, so the card is "August 2026 so far".
TODAY = date(2026, 8, 30)


def _ms(on: date, hour: int) -> int:
    return int(datetime(on.year, on.month, on.day, hour, 0,
                        tzinfo=timezone.utc).timestamp() * 1000)


def _seed_rollups(db, days: list[tuple[date, float, float, float | None]],
                  mac: str = MAC) -> None:
    """(day, measured high, measured low, rain or None) rows."""
    async def run():
        async with db.connect() as conn:
            for d, hi, lo, rain in days:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n, "
                    "rain_total) VALUES (?,?,?,?,?,?,?)",
                    (mac, d.isoformat(), lo, hi, hi, 1, rain))
            await conn.commit()
    asyncio.run(run())


def _seed_forecasts(db, rows: list[tuple[date, float | None, float | None,
                                         float | None]],
                    provider: str = "open-meteo", lead_days: int = 1,
                    issued_hour: int = 6, days_before: int = 1) -> None:
    """(valid date, forecast high, forecast low, pop) rows, each issued the
    morning before its valid date."""
    async def run():
        for d, hi, lo, pop in rows:
            await db.insert_forecast_snapshots(
                provider, _ms(d - timedelta(days=days_before), issued_hour),
                [{"valid_date": d.isoformat(), "lead_days": lead_days,
                  "tmax_f": hi, "tmin_f": lo, "pop": pop,
                  "precip_in": None}])
    asyncio.run(run())


def _month(db, n: int = 24, hi_bias: float = 3.2, lo_bias: float = -1.0,
           start: date = date(2026, 8, 1), rain: bool = False) -> list[date]:
    """`n` matched days from `start`: measured 100/75, forecast biased by
    exactly `hi_bias` / `lo_bias`. With `rain`, a known rain-call ledger:
    day 3 called and rained (hit), day 5 called and dry (false alarm), day
    8 rained uncalled (miss); every other day quiet on both sides."""
    days = [start + timedelta(days=i) for i in range(n)]
    rollups, forecasts = [], []
    for i, d in enumerate(days):
        rained = rain and i in (2, 7)
        called = rain and i in (2, 4)
        rollups.append((d, 100.0, 75.0,
                        (0.25 if rained else 0.0) if rain else None))
        forecasts.append((d, 100.0 + hi_bias, 75.0 + lo_bias,
                          (80.0 if called else 10.0) if rain else None))
    _seed_rollups(db, rollups)
    _seed_forecasts(db, forecasts)
    return days


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _out(mac: str = MAC, **kw) -> dict:
    return asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_SCIENCE], limit=8, **kw))


def _card(mac: str = MAC, **kw) -> dict | None:
    return next((s for s in _out(mac, **kw)["stories"]
                 if s["story_type"] == "forecast_vs_backyard"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


# ───────────────────────── a month with a known bias ─────────────────────────

def test_a_warm_biased_month_is_measured_and_written_out(engine):
    _month(engine)                     # 24 days, high +3.2°F, low −1.0°F
    s = _card()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("science",
                                              "forecast_vs_backyard")
    assert s["title"] == "The Forecast Ran Warm"
    assert s["hero_line"] == "THE DAY-AHEAD HIGH RAN 3.2°F WARM"
    assert s["hero"]["value"] == 3.2 and s["hero"]["unit"] == "F"
    assert s["hero"]["label"] == "day-ahead high · mean miss over 24 days"
    # Every number on the card is the fixture's, signed forecast − measured.
    assert _stat(s, "high_bias")["value"] == 3.2
    assert _stat(s, "low_bias")["value"] == -1.0
    assert _stat(s, "days_matched")["value"] == 24
    assert _stat(s, "days_matched")["unit"] == "days"
    # The comparison is the model's mean high against the measured mean
    # high, and its rank line is the server's sentence.
    c = s["comparison"]
    assert c["kind"] == "day_ahead_vs_measured"
    assert (c["value"], c["baseline"]) == (103.2, 100.0)
    assert c["direction"] == "above" and c["delta"] == 3.2
    assert c["delta_pct"] is None
    assert c["rank_line"] == "the day-ahead high ran 3.2°F warm over 24 days"
    assert s["period"] == {"kind": "month", "label": "August 2026 so far",
                           "start": "2026-08-01", "end": "2026-08-24",
                           "partial": True}
    assert "the day-ahead high ran 3.2°F warm on average" in s["context"]
    assert "the low ran 1°F cool" in s["context"]


def test_the_largest_miss_names_its_day_and_side(engine):
    days = _month(engine)
    # One cold bust on the 14th: the model said 103.2, the station hit 112.
    _seed_rollups(engine, [(days[13], 112.0, 75.0, None)])
    s = _card()
    worst = _stat(s, "largest_miss")
    assert worst["label"] == "largest miss · high on August 14"
    assert worst["value"] == pytest.approx(103.2 - 112.0, abs=0.05)
    assert "The largest miss was the high on August 14, 8.8°F too cool." \
        in s["context"]
    rows = {e["key"]: e for e in s["viz"]["series"]}
    assert rows["largest"]["label"] == "largest miss · Aug 14"
    assert rows["largest"]["value"] == pytest.approx(-8.8, abs=0.05)
    # The bar is the SIZE of the miss on a ten-degree yardstick.
    assert rows["largest"]["score"] == pytest.approx(0.88, abs=0.005)


def test_a_cool_biased_month_flips_every_word(engine):
    _month(engine, hi_bias=-2.5, lo_bias=-4.0)
    s = _card()
    # The low carries the larger miss, so the low leads the card.
    assert s["title"] == "The Forecast Ran Cool"
    assert s["hero_line"] == "THE DAY-AHEAD LOW RAN 4°F COOL"
    assert s["hero"]["value"] == -4.0
    assert s["comparison"]["direction"] == "below"
    assert s["comparison"]["rank_line"] == \
        "the day-ahead low ran 4°F cool over 24 days"
    assert s["viz"]["highlight_key"] == "low"
    rows = {e["key"]: e for e in s["viz"]["series"]}
    assert rows["low"]["owned"] is True and rows["high"]["owned"] is False
    assert rows["low"]["label"] == "low · ran cool"


def test_a_perfect_month_says_so_without_inventing_a_direction(engine):
    _month(engine, hi_bias=0.0, lo_bias=0.0)
    s = _card()
    assert s["title"] == "The Forecast Was Spot On"
    assert s["hero_line"] == "THE DAY-AHEAD HIGH WAS SPOT ON"
    assert s["comparison"]["direction"] == "level"
    assert "warm" not in s["hero_line"].lower()
    assert "cool" not in s["hero_line"].lower()
    rows = {e["key"]: e for e in s["viz"]["series"]}
    assert rows["high"]["label"] == "high · spot on"


# ───────────────────────── the rain calls ─────────────────────────

def test_rain_calls_are_graded_as_hits_false_alarms_and_misses(engine):
    _month(engine, rain=True)
    s = _card()
    assert _stat(s, "rain_hits")["value"] == 1
    assert _stat(s, "rain_false_alarms")["value"] == 1
    assert _stat(s, "rain_misses")["value"] == 1
    assert ("The model called for rain on 2 days: 1 came true and 1 stayed "
            "dry, and rain fell on 1 day it never called for.") in s["context"]


def test_no_rain_calls_and_no_rain_omits_the_sentence_not_zero_of_zero(engine):
    _month(engine, rain=False)         # no pop stored, no rain measured
    s = _card()
    assert _stat(s, "rain_hits") is None
    assert _stat(s, "rain_false_alarms") is None
    assert _stat(s, "rain_misses") is None
    assert "rain" not in s["context"].lower()
    assert "0 of 0" not in s["context"]
    assert s["score_parts"]["rain_misses"] == 0.0


def test_a_gauge_less_station_has_no_rain_record_not_a_perfect_one(engine):
    """pop stored on every day, rain measured on none: absent is not zero,
    so nothing is graded and nothing is claimed."""
    days = _month(engine, rain=False)
    _seed_forecasts(engine, [(d, 103.2, 74.0, 80.0) for d in days])
    s = _card()
    assert _stat(s, "rain_hits") is None
    assert "rain" not in s["context"].lower()


# ───────────────────────── declines ─────────────────────────

def test_declines_below_ten_matched_days(engine):
    _month(engine, n=9)
    out = _out()
    assert _card() is None
    assert "forecast_vs_backyard" in out["declined"]


def test_ten_matched_days_is_enough_and_marks_the_month_partial(engine):
    _month(engine, n=10)
    s = _card()
    assert s is not None and s["period"]["partial"] is True
    assert _stat(s, "days_matched")["value"] == 10


def test_today_is_never_matched_because_its_high_has_not_happened(engine):
    _month(engine, n=30)               # runs through the 30th, which is today
    s = _card()
    assert _stat(s, "days_matched")["value"] == 29
    assert s["period"]["end"] == "2026-08-29"


def test_falls_back_to_the_last_complete_month(engine):
    _month(engine, n=31, start=date(2026, 7, 1))
    _month(engine, n=5, start=date(2026, 8, 1))
    s = _card()
    assert s["period"]["label"] == "July 2026"
    assert s["period"]["partial"] is False
    assert _stat(s, "days_matched")["value"] == 31
    assert s["id"] == "science.forecast_vs_backyard.2026-07"


def test_only_the_day_ahead_call_from_the_scored_provider_counts(engine):
    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(24)]
    _seed_rollups(engine, [(d, 100.0, 75.0, None) for d in days])
    # A two-day lead, and a provider with no temperatures: neither is the
    # day-ahead call this card grades.
    _seed_forecasts(engine, [(d, 110.0, 80.0, None) for d in days],
                    lead_days=2, days_before=2)
    _seed_forecasts(engine, [(d, None, None, None) for d in days],
                    provider="zambretti")
    assert _card() is None
    _seed_forecasts(engine, [(d, 101.0, 75.0, None) for d in days])
    s = _card()
    assert _stat(s, "high_bias")["value"] == 1.0


def test_the_freshest_day_ahead_issue_wins(engine):
    days = _month(engine, hi_bias=5.0)
    # A later run the same morning revised every high down to a 1° miss.
    _seed_forecasts(engine, [(d, 101.0, 74.0, None) for d in days],
                    issued_hour=18)
    s = _card()
    assert _stat(s, "high_bias")["value"] == 1.0


def test_declines_when_a_side_is_missing_rather_than_scoring_half(engine):
    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(24)]
    _seed_rollups(engine, [(d, 100.0, 75.0, None) for d in days])
    _seed_forecasts(engine, [(d, 103.0, None, None) for d in days])
    assert _card() is None


def test_declines_with_no_snapshots_at_all(engine):
    _seed_rollups(engine, [(date(2026, 8, 1) + timedelta(days=i),
                            100.0, 75.0, None) for i in range(24)])
    assert _card() is None


# ───────────────────────── units ─────────────────────────

def test_a_celsius_reader_gets_departures_by_scale_and_readings_by_offset(engine):
    _month(engine)                     # +3.2°F on the high
    s = _card(units=stories.Units(temperature="celsius"))
    # 3.2°F of BIAS is 1.8°C of bias, never −16°C.
    assert s["hero"]["value"] == pytest.approx(3.2 * 5 / 9, abs=0.05)
    assert s["hero"]["unit"] == "C"
    assert s["hero_line"] == "THE DAY-AHEAD HIGH RAN 1.8°C WARM"
    assert _stat(s, "low_bias")["value"] == pytest.approx(-1.0 * 5 / 9,
                                                          abs=0.05)
    # The comparison's two READINGS carry the offset.
    c = s["comparison"]
    assert c["value"] == pytest.approx((103.2 - 32) * 5 / 9, abs=0.05)
    assert c["baseline"] == pytest.approx((100.0 - 32) * 5 / 9, abs=0.05)
    assert c["delta"] == pytest.approx(3.2 * 5 / 9, abs=0.05)
    # The yardstick converts too, and nothing on the card says Fahrenheit.
    assert s["viz"]["axis_label"] == "size of the miss · a full bar is 5.6°C"
    blob = " ".join([s["title"], s["hero_line"], s["context"],
                     s["viz"]["axis_label"], s["viz"]["footnote"],
                     *[x["label"] for x in s["supporting"]]])
    assert "°F" not in blob


# ───────────────────────── scoring and voice ─────────────────────────

def test_interestingness_is_calibrated_to_the_spec(engine):
    _month(engine, hi_bias=0.0, lo_bias=0.0)
    perfect = _card()
    assert perfect["interestingness"] == pytest.approx(0.1, abs=0.01)

    _month(engine, hi_bias=5.0, lo_bias=0.0)
    biased = _card()
    assert biased["interestingness"] == pytest.approx(0.8, abs=0.01)
    assert biased["score_parts"]["bias"] == 1.0

    _month(engine, hi_bias=5.0, lo_bias=0.0, rain=True)
    wrong_rain = _card()
    assert wrong_rain["interestingness"] > biased["interestingness"]
    assert 0.0 <= wrong_rain["interestingness"] <= 1.0
    assert wrong_rain["score_parts"]["rain_misses"] == pytest.approx(2 / 3, abs=1e-3)


def test_the_provider_is_the_model_in_prose_and_named_in_the_footnote(engine):
    _month(engine)
    s = _card()
    assert "the model" in s["context"]
    assert "open-meteo" not in s["context"]
    assert "open-meteo" in s["viz"]["footnote"]
    assert s["viz"]["kind"] == "chaos_dimensions"
    assert [e["key"] for e in s["viz"]["series"]] == ["high", "low",
                                                      "largest"]
    # Flat rows, no arrays inside them, no dashes the card would have to
    # read around.
    for row in s["viz"]["series"]:
        assert not any(isinstance(v, (list, dict)) for v in row.values())
    blob = " ".join([s["title"], s["hero_line"], s["context"],
                     s["viz"]["axis_label"], s["viz"]["footnote"],
                     s["comparison"]["rank_line"],
                     *[x["label"] for x in s["supporting"]],
                     *[e["label"] for e in s["viz"]["series"]]])
    assert "—" not in blob and "–" not in blob


def test_endpoint_serves_the_card(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _month(db)
    r = client.get(f"/api/devices/{MAC}/stories?family=science&limit=8",
                   headers=H)
    assert r.status_code == 200
    s = next(s for s in r.json()["stories"]
             if s["story_type"] == "forecast_vs_backyard")
    assert s["hero_line"] == "THE DAY-AHEAD HIGH RAN 3.2°F WARM"
    assert ("science", "forecast_vs_backyard") in stories.registered()
