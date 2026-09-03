"""Story engine (2.0): The Storm That Broke the Heat.

The one producer that cannot read its own inputs out of history. Every
other story here is derived from rollups that keep forever; this one needs
the readings either side of a storm, which history thinning ages away
within days — so they are CAPTURED AT CLOSE onto the storm row and this
producer either finds them there or declines.

That makes the NULL semantics the load-bearing part of the suite. A storm
closed before 2.0 carries NULL in those columns permanently, and a producer
that filled a NULL with a zero would publish a card claiming a storm that
moved nothing. Half the tests below are about refusing to do that.

Fixtures seed daily_rollups directly (the station distribution the storm is
ranked against) plus storm_history through the real `db.record_storm`, and
pin "today" through app.climate.local_today — the same seam every other
story suite uses, so every number here is fixed by the fixture rather than
by the day the suite runs.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:3D"

TODAY = date(2026, 8, 30)
BASE_START = date(2025, 9, 1)          # 364 days of record ending on TODAY

# The suite runs with TIMEZONE=UTC (conftest), so local clock == UTC here.
STORM_DAY = date(2026, 8, 19)


def _ms(d: date, hour: int, minute: int) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute,
                        tzinfo=timezone.utc).timestamp() * 1000)


# A monsoon afternoon: rain from 4:08pm to 5:20pm.
STORM_START = _ms(STORM_DAY, 16, 8)
STORM_END = _ms(STORM_DAY, 17, 20)


# A 29-long cycle of shape factors — coprime with 7 and with every month
# length, so the pattern never lines up with a week or a month. Squares give
# the heavy upper tail real weather has. Deterministic on purpose: against a
# monotonous fixture every deviation is a station record and the ranking
# being tested cannot be tested at all.
_SHAPES = (0.02, 0.31, 0.11, 0.64, 0.07, 0.45, 0.22, 0.88, 0.05, 0.37,
           0.16, 0.53, 0.09, 0.71, 0.27, 0.13, 0.42, 0.03, 0.59, 0.19,
           0.34, 0.08, 0.48, 0.24, 0.95, 0.12, 0.40, 0.06, 0.66)


def _shape(i: int, phase: int) -> float:
    return _SHAPES[(i * phase + phase) % len(_SHAPES)]


def _rows(start: date = BASE_START, end: date = TODAY,
          swing_base: float = 15.0, swing_span: float = 8.0) -> list[dict]:
    """A hot station whose ordinary DAYS move 15–23°F between dawn and
    mid-afternoon. That distribution is the whole comparison: a 24°F fall
    inside one storm has to be ranked against it."""
    rows: list[dict] = []
    d, i = start, 0
    while d <= end:
        u, v = _shape(i, 1), _shape(i, 7)
        w, x, y = _shape(i, 11), _shape(i, 13), _shape(i, 17)
        hi = round(88.0 + 14.0 * u, 1)
        rows.append({
            "day": d.isoformat(),
            "hi": hi,
            "lo": round(hi - (swing_base + swing_span * v), 1),
            "gust": round(10.0 + 34.0 * w * w, 1),
            "p_lo": 29.80,
            "p_hi": round(29.85 + 0.22 * x * x, 3),
            "rain": round(0.5 * y ** 3, 2) if y > 0.6 else 0.0,
        })
        d += timedelta(days=1)
        i += 1
    return rows


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    """Rollup rows in, exactly as ingest would have folded them. A key left
    out arrives as SQL NULL — how "this station has no rain gauge" is
    spelled, and it must not read as zero anywhere downstream."""
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE mac = ?",
                               (mac,))
            for r in rows:
                hi = r.get("hi")
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n, "
                    " windgustmph_max, baromrelin_min, baromrelin_max, "
                    " rain_total) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (mac, r["day"], r.get("lo"), hi, hi,
                     1 if hi is not None else 0, r.get("gust"),
                     r.get("p_lo"), r.get("p_hi"), r.get("rain")))
            await conn.commit()
    asyncio.run(run())


# The motivating example, as a stored storm row: 108°F before, 84°F after.
CAPTURED = {
    "started_ms": STORM_START, "ended_ms": STORM_END,
    "total_in": 0.90, "peak_rate_in_hr": 1.80, "max_gust_mph": 36.0,
    "min_tempf": 78.0, "max_tempf": 99.0,
    "pre_tempf": 108.0, "post_tempf": 84.0, "temp_drop_f": 24.0,
    "pressure_change_inhg": 0.09, "dew_change_f": 12.0,
}

# The same storm as 1.9 recorded it: every capture column NULL, because the
# columns did not exist when it closed. Every storm on every upgrading
# server looks like this, forever.
LEGACY = {k: v for k, v in CAPTURED.items()
          if k not in ("pre_tempf", "post_tempf", "temp_drop_f",
                       "pressure_change_inhg", "dew_change_f")}


def _storm(db, mac: str = MAC, **overrides) -> None:
    asyncio.run(db.record_storm(mac, {**CAPTURED, **overrides}))


@pytest.fixture()
def engine(client, monkeypatch):
    """Insights on, today pinned. Returns the app.db module for seeding."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _records(mac: str = MAC, **kw) -> dict:
    return asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_RECORDS], limit=8, **kw))


def _story(mac: str = MAC, **kw) -> dict | None:
    return next((s for s in _records(mac, **kw)["stories"]
                 if s["story_type"] == "storm_broke_the_heat"), None)


def _declined(mac: str = MAC, **kw) -> list[str]:
    return _records(mac, **kw)["declined"]


# ───────────────────────── the happy path ─────────────────────────

def test_the_storm_that_broke_the_heat(engine):
    _seed(engine, _rows())
    _storm(engine)
    s = _story()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("records",
                                              "storm_broke_the_heat")
    assert s["title"] == "The Storm That Broke the Heat"
    assert s["emoji"] == "⛈️"
    assert s["id"] == f"records.storm_broke_the_heat.{STORM_END}"

    # The line people repeat out loud.
    assert s["hero_line"] == "108°F BEFORE, 84°F AFTER"
    # ...and the single number the card sets in type is the difference
    # between the two halves of it.
    assert s["hero"]["key"] == "temp_drop"
    assert (s["hero"]["value"], s["hero"]["unit"]) == (24, "F")

    assert s["context"] == (
        "Rain began at 4:08 pm on August 19 and the last of it fell "
        "1h 12m later. The hour before, this station read 108°F; the hour "
        "after, 84°F, a fall of 24°F. It brought 0.90 in of rain, a 36 mph "
        "gust and a 0.09 inHg barometer rise. That afternoon was hotter "
        "than every day this station has recorded.")

    assert s["period"]["kind"] == "moment"
    assert s["period"]["label"] == "August 19, 2026"
    assert s["period"]["partial"] is False


def test_the_supporting_stats_are_measurements_not_scores(engine):
    _seed(engine, _rows())
    _storm(engine)
    stats = {x["key"]: x for x in _story()["supporting"]}
    assert (stats["temp_before"]["value"], stats["temp_before"]["unit"]) == (
        108, "F")
    assert stats["temp_after"]["value"] == 84
    assert (stats["rain"]["value"], stats["rain"]["unit"]) == (0.9, "in")
    assert (stats["peak_rate"]["value"], stats["peak_rate"]["unit"]) == (
        1.8, "in/hr")
    assert (stats["gust"]["value"], stats["gust"]["unit"]) == (36, "mph")
    # Direction lives in the WORDS and the value stays positive, so a card
    # never has to render a minus sign it did not ask for.
    assert stats["pressure_change"]["label"] == "the barometer rose"
    assert stats["pressure_change"]["value"] == 0.09
    assert stats["dew_change"]["label"] == "the dew point rose"
    assert stats["dew_change"]["value"] == 12.0
    assert (stats["duration"]["value"], stats["duration"]["unit"]) == (
        72, "min")


def test_the_comparison_ranks_the_drop_against_whole_days(engine):
    """An hour of storm against how far this station's temperature travels
    in a whole day — the comparison that makes the number mean something,
    and the only distribution with enough entries to rank in."""
    _seed(engine, _rows())
    _storm(engine)
    c = _story()["comparison"]
    assert c["kind"] == "station_daily_swings"
    assert c["value"] == 24.0
    assert c["of"] == 364 and c["rank"] == 1
    assert c["rank_line"] == "the biggest of 364 recorded daily swings"
    assert c["direction"] == "above"
    assert c["baseline_label"] == "the median daily swing of 364 recorded days"
    # No unit in the words: the numbers beside it carry the token, and a
    # Fahrenheit suffix here is exactly how a Celsius card leaks.
    assert "°F" not in c["baseline_label"]


def test_the_chart_explains_which_bar_is_which(engine):
    """Two bars, two different KINDS of number — a peak and a trough. A
    picture that draws both without saying so is asking the reader to guess
    what makes the fall impressive."""
    _seed(engine, _rows())
    _storm(engine)
    viz = _story()["viz"]
    assert viz["kind"] == "storm_before_after"
    assert [b["key"] for b in viz["series"]] == ["before", "after"]
    assert [b["value"] for b in viz["series"]] == [108, 84]
    assert [b["note"] for b in viz["series"]] == ["hottest reading",
                                                  "coolest reading"]
    assert viz["highlight_key"] == "after"
    assert viz["footnote"] == (
        "Before is the hottest reading in the hour ending when the rain "
        "started; after is the coolest in the hour that followed the last "
        "drop.")


def test_the_best_captured_storm_wins(engine):
    """Several stored storms, one story: the one that ranks highest against
    this station's own record, not the newest."""
    _seed(engine, _rows())
    older = _ms(date(2026, 7, 2), 15, 0)
    _storm(engine, started_ms=older, ended_ms=older + 3_600_000,
           total_in=0.10, max_gust_mph=18.0, pre_tempf=99.0,
           post_tempf=88.0, temp_drop_f=11.0, pressure_change_inhg=0.02)
    _storm(engine)                       # the 24°F fall, on Aug 19
    newest = _ms(date(2026, 8, 27), 18, 0)
    _storm(engine, started_ms=newest, ended_ms=newest + 1_800_000,
           total_in=0.06, max_gust_mph=15.0, pre_tempf=96.0,
           post_tempf=87.0, temp_drop_f=9.0, pressure_change_inhg=0.01)

    out = _records()
    told = [s for s in out["stories"]
            if s["story_type"] == "storm_broke_the_heat"]
    assert len(told) == 1, "one storm story, not one per storm"
    assert told[0]["id"] == f"records.storm_broke_the_heat.{STORM_END}"


# ───────────────────────── the decline paths ─────────────────────────

def test_a_pre_2_0_storm_declines_rather_than_rendering_zeros(engine):
    """THE test this producer exists to pass. Every storm on an upgrading
    server carries NULL in the capture columns, permanently. NULL means "we
    did not measure this" — not "the temperature did not move" — and the
    difference between those two is a share card that lies."""
    _seed(engine, _rows())
    asyncio.run(engine.record_storm(MAC, LEGACY))
    assert _story() is None
    assert "storm_broke_the_heat" in _declined()

    # And the storm itself is still on file, unchanged — declining to tell
    # a story is not the same as forgetting the storm.
    rows = asyncio.run(engine.list_storms(MAC))
    assert rows[0]["total_in"] == 0.90
    assert rows[0]["pre_tempf"] is None


def test_a_half_capture_declines_too(engine):
    """One side measured and the other not: a thermometer that came back up
    after the rain has an "after" and no "before". There is no fall to
    report and inventing one from the summary's own extremes would be the
    same zero bug wearing a different hat."""
    _seed(engine, _rows())
    _storm(engine, pre_tempf=None, temp_drop_f=None)
    assert _story() is None


def test_a_station_with_no_storms_at_all_declines(engine):
    _seed(engine, _rows())
    assert _story() is None
    # The two 2.0 record producers decline alongside: no row for today
    # beats a year of history here, and no day ever counted a strike.
    assert _declined() == ["storm_broke_the_heat", "record_broken",
                           "lightning_season"]


def test_declines_when_the_station_has_too_little_history(engine):
    """Under MIN_STORY_DAYS there is no station distribution — only a
    handful of daily swings, each of which would look like a record."""
    _seed(engine, _rows(date(2026, 8, 10), TODAY))     # 21 days
    _storm(engine)
    assert _story() is None


def test_declines_when_no_daily_swing_can_be_ranked(engine):
    """Rollup rows with every temperature column NULL: days the station was
    up and measuring nothing. Plenty of "data", nothing to rank."""
    _seed(engine, [{"day": r["day"], "gust": r["gust"]} for r in _rows()])
    _storm(engine)
    assert _story() is None


def test_a_small_fall_declines_on_the_absolute_floor(engine):
    """A station whose days barely move would rank a four-degree dip in its
    own top quartile. The ranking is right and the sentence would still be
    ridiculous, so there is a floor underneath it."""
    _seed(engine, _rows(swing_base=4.0, swing_span=2.0))
    _storm(engine, pre_tempf=94.0, post_tempf=89.0, temp_drop_f=5.0)
    assert _story() is None
    assert stories.STORM_MIN_DROP_F == 8.0


def test_an_ordinary_fall_declines_against_the_stations_own_record(engine):
    """The other half of the same rule, and the reason the floor is not the
    only test: on a station whose days routinely swing 40°F, a 24°F fall is
    a Tuesday. Same storm, same numbers, different station — no story."""
    _seed(engine, _rows(swing_base=36.0, swing_span=10.0))
    _storm(engine)
    assert _story() is None


def test_a_storm_that_warmed_the_air_is_not_this_story(engine):
    """A nocturnal front can leave the air warmer than it found it. That is
    real, and it is not a story about breaking heat — the hero line would
    read "84°F BEFORE, 108°F AFTER" under the title."""
    _seed(engine, _rows())
    _storm(engine, pre_tempf=84.0, post_tempf=108.0, temp_drop_f=-24.0)
    assert _story() is None


def test_a_sensorless_station_still_gets_its_story(engine):
    """The other side of absent-is-not-zero: no barometer and no dew sensor
    means those dimensions DROP and the weighted mean renormalizes onto
    what is left. It does not mean the pressure held still."""
    _seed(engine, _rows())
    _storm(engine, pressure_change_inhg=None, dew_change_f=None)
    s = _story()
    assert s is not None
    keys = {x["key"] for x in s["supporting"]}
    assert "pressure_change" not in keys and "dew_change" not in keys
    assert "barometer" not in s["context"]
    assert s["score_parts"]["dimensions"] == 3
    assert "pressure_score" not in s["score_parts"]


def test_the_title_only_claims_heat_when_there_was_heat(engine):
    """The card is named for a claim, so the claim is checked. A thirty-
    degree fall off a mild afternoon still earns its card — it just does
    not get to say it broke a heat that was never there."""
    _seed(engine, _rows())
    _storm(engine, pre_tempf=74.0, post_tempf=48.0, temp_drop_f=26.0)
    s = _story()
    assert s is not None
    assert s["title"] == "The Storm That Cooled the Day"
    assert s["hero_line"] == "74°F BEFORE, 48°F AFTER"
    assert "hotter than" not in s["context"]
    assert s["score_parts"]["heat_share"] < stories.STORM_HEAT_SHARE


def test_the_heat_claim_quotes_a_percentage_when_it_is_not_the_top(engine):
    """Between the claim line and the record, the sentence says how far up
    the station's own record the afternoon sat."""
    _seed(engine, _rows())
    _storm(engine, pre_tempf=99.0, post_tempf=75.0)
    s = _story()
    assert s["title"] == "The Storm That Broke the Heat"
    assert "hotter than 93% of the days this station has recorded" in \
        s["context"]
    assert "every day this station" not in s["context"]


# ───────────────────────── units ─────────────────────────

METRIC = stories.Units(temperature="celsius", wind="kph", rain="mm",
                       pressure="hPa")


def _strings(story: dict) -> list[str]:
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"], story["period"]["label"],
           story["viz"]["axis_label"] or "", story["viz"]["footnote"] or ""]
    out += [x["label"] for x in story["supporting"]]
    c = story["comparison"]
    out += [c["label"], c["baseline_label"], c["rank_line"] or ""]
    out += [b["label"] for b in story["viz"]["series"]]
    return out


def test_a_celsius_render_leaks_no_fahrenheit(engine):
    _seed(engine, _rows())
    _storm(engine)
    s = _story(units=METRIC)
    for text in _strings(s):
        assert "°F" not in text, text
        assert "inHg" not in text, text
        assert " mph" not in text, text
    units = {x["unit"] for x in s["supporting"] if x["unit"]}
    units |= {s["hero"]["unit"], s["viz"]["unit"]}
    units |= {b["unit"] for b in s["viz"]["series"]}
    assert units == {"C", "km/h", "mm", "mm/hr", "hPa", "min"}


def test_the_fall_converts_by_scale_not_by_the_reading_formula(engine):
    """The exact bug this repo has shipped before. A 24°F FALL is a 13.3°C
    fall — it is a difference, and differences take the scale conversion
    only. Run it through the reading formula and 24°F becomes −4.4°C, which
    is a temperature, and the card announces that the storm cooled the air
    by minus four degrees."""
    _seed(engine, _rows())
    _storm(engine)
    s = _story(units=METRIC)

    assert s["hero"]["value"] == pytest.approx(13.3, abs=0.05)
    assert s["hero"]["value"] != pytest.approx((24.0 - 32) * 5 / 9, abs=0.05)
    assert s["hero"]["unit"] == "C"

    # The READINGS on either side take the offset conversion, in the same
    # story, in the same breath — which is what makes the pair a trap.
    assert s["hero_line"] == "42.2°C BEFORE, 28.9°C AFTER"
    stats = {x["key"]: x for x in s["supporting"]}
    assert stats["temp_before"]["value"] == pytest.approx(42.2, abs=0.05)
    # A dew-point CHANGE is a difference too: 12°F rise = 6.7°C rise, never
    # −11.1°C.
    assert stats["dew_change"]["value"] == pytest.approx(6.7, abs=0.05)
    assert stats["dew_change"]["value"] > 0

    # ...and so is the comparison, on both sides of it.
    c = s["comparison"]
    assert c["value"] == pytest.approx(13.3, abs=0.05)
    assert c["baseline"] == pytest.approx(
        _story()["comparison"]["baseline"] * 5 / 9, abs=0.05)


def test_the_same_storm_in_two_scales_is_the_same_story(engine):
    """Only the words and the numbers move. A story that changed its mind
    about which storm was interesting when the reader switched to Celsius
    would mean a threshold was being compared against a converted value."""
    _seed(engine, _rows())
    _storm(engine)
    native, metric = _story(), _story(units=METRIC)
    assert native["id"] == metric["id"]
    assert native["interestingness"] == metric["interestingness"]
    assert native["score_parts"] == metric["score_parts"]
    assert native["period"] == metric["period"]
    assert native["comparison"]["rank"] == metric["comparison"]["rank"]
    assert native["title"] == metric["title"]


def test_the_pressure_rise_reads_in_hectopascals(engine):
    _seed(engine, _rows())
    _storm(engine)
    stats = {x["key"]: x for x in _story(units=METRIC)["supporting"]}
    assert stats["pressure_change"]["value"] == pytest.approx(
        0.09 * 33.8639, abs=0.05)
    assert stats["pressure_change"]["unit"] == "hPa"


# ───────────────────────── determinism ─────────────────────────

def test_the_story_is_identical_run_to_run(engine):
    """Same anchor, same database, same bytes — the pinned "today" is the
    only clock this producer is allowed to read."""
    _seed(engine, _rows())
    _storm(engine)
    first, second = _story(), _story()
    assert first == second


# ───────────────── cross-producer calibration ─────────────────

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_a_heat_breaking_storm_outranks_the_heat_ledger(engine):
    """`interestingness` is documented as comparable ACROSS producers, so
    these have to land on one 0..1 scale and swap places on merit. A storm
    that took twenty-four degrees off a station-record afternoon is the
    story on this station today."""
    _seed(engine, _rows())
    _storm(engine)
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert {"storm_broke_the_heat", "heat_ledger"} <= {
        s["story_type"] for s in ranked["stories"]}
    assert (_best_of(ranked, "storm_broke_the_heat")
            > _best_of(ranked, "heat_ledger"))
    assert ranked["stories"][0]["story_type"] == "storm_broke_the_heat"
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


def test_a_merely_adequate_storm_yields_to_the_heat_ledger(engine):
    """The direction that makes it calibration rather than a thumb on the
    scale. Same station, same highs; only the storm changes. A fall that
    clears the notable line and nothing else still produces a story, and it
    must rank BELOW the ledger it beat a moment ago."""
    _seed(engine, _rows())
    _storm(engine, total_in=0.02, peak_rate_in_hr=0.10, max_gust_mph=12.0,
           pre_tempf=99.0, post_tempf=77.0, temp_drop_f=22.0,
           pressure_change_inhg=0.01, dew_change_f=2.0)
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert {"storm_broke_the_heat", "heat_ledger"} <= {
        s["story_type"] for s in ranked["stories"]}
    assert (_best_of(ranked, "storm_broke_the_heat")
            < _best_of(ranked, "heat_ledger"))
    assert ranked["stories"][0]["story_type"] != "storm_broke_the_heat"
    # The fall alone still carries it; everything else scored a measured
    # zero against this station's own record, and the renormalized mean
    # says so out loud.
    told = next(s for s in ranked["stories"]
                if s["story_type"] == "storm_broke_the_heat")
    assert told["score_parts"]["drop_score"] > stories.STORM_DROP_NOTABLE
    assert told["score_parts"]["gust_score"] == 0.0


# ───────────────────────── the endpoint ─────────────────────────

def test_the_endpoint_serves_the_storm(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows())
    _storm(db)

    r = client.get(f"/api/devices/{MAC}/stories?family=records&limit=8",
                   headers=H)
    assert r.status_code == 200
    body = r.json()
    # The record producers decline by name: this fixture's today beats no
    # year of history and never counted a strike.
    assert body["declined"] == ["record_broken", "lightning_season"]
    s = next(x for x in body["stories"]
             if x["story_type"] == "storm_broke_the_heat")
    assert s["hero_line"] == "108°F BEFORE, 84°F AFTER"

    # And in the reader's scale, end to end through the query string.
    r = client.get(f"/api/devices/{MAC}/stories?family=records&limit=8"
                   "&temp_unit=celsius&rain_unit=mm&wind_unit=kph"
                   "&pressure_unit=hPa", headers=H)
    s = next(x for x in r.json()["stories"]
             if x["story_type"] == "storm_broke_the_heat")
    assert s["hero_line"] == "42.2°C BEFORE, 28.9°C AFTER"
    assert "°F" not in s["context"]


def test_the_registry_lists_the_producer():
    assert ("records", "storm_broke_the_heat") in stories.registered()
