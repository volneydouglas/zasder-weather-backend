"""Story engine (2.0): the Wildest Day producer — the day the database
nominates, at both scopes.

Fixtures seed daily_rollups directly and pin "today" through
app.climate.local_today, the same seam the heat-ledger suite uses, so every
number here is fixed by the fixture rather than by the day the suite runs.

The baseline station below has REAL day-to-day spread, laid down from a
fixed cycle of shape factors rather than an RNG (deterministic, and stable
across interpreters). That matters more than it looks: against a monotonous
fixture every deviation is a station record, so a barely-unusual day sweeps
every axis and the whole point of the chaos score — that owning several
dimensions is harder than owning one — cannot be tested at all.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:2C"

TODAY = date(2026, 8, 30)
BASE_START = date(2025, 9, 1)          # 364 days of record ending on TODAY

# The two engineered days. AUG 5 owns the station's record gust and nothing
# else; AUG 19 owns four axes at once while placing second on the gust. That
# pair is the whole argument for a chaos score over a leaderboard.
SINGLE_MAX_DAY = "2026-08-05"
WILD_DAY = "2026-08-19"


def _row(day: str, *, lo, hi, gust, p_lo, p_hi, rain) -> dict:
    return {"day": day, "lo": lo, "hi": hi, "gust": gust,
            "p_lo": p_lo, "p_hi": p_hi, "rain": rain}


# A 29-long cycle of shape factors, 29 being coprime with 7 and with every
# month length so the pattern never lines up with a week or a month. Squares
# and cubes give the heavy upper tail real weather has: most days ordinary,
# a few genuinely gusty.
_SHAPES = (0.02, 0.31, 0.11, 0.64, 0.07, 0.45, 0.22, 0.88, 0.05, 0.37,
           0.16, 0.53, 0.09, 0.71, 0.27, 0.13, 0.42, 0.03, 0.59, 0.19,
           0.34, 0.08, 0.48, 0.24, 0.95, 0.12, 0.40, 0.06, 0.66)


def _shape(i: int, phase: int) -> float:
    return _SHAPES[(i * phase + phase) % len(_SHAPES)]


def _baseline(start: date = BASE_START, end: date = TODAY) -> list[dict]:
    rows: list[dict] = []
    d, i = start, 0
    while d <= end:
        u, v = _shape(i, 1), _shape(i, 7)
        w, x, y = _shape(i, 11), _shape(i, 13), _shape(i, 17)
        hi = round(88.0 + 14.0 * u, 1)
        rows.append(_row(d.isoformat(),
                         lo=round(hi - (18.0 + 12.0 * v), 1), hi=hi,
                         gust=round(10.0 + 38.0 * w * w, 1),
                         p_lo=29.80,
                         p_hi=round(29.85 + 0.22 * x * x, 3),
                         rain=round(0.6 * y ** 3, 2) if y > 0.6 else 0.0))
        d += timedelta(days=1)
        i += 1
    return rows


def _wild_rows(**overrides: dict) -> list[dict]:
    """The baseline with the two engineered days, plus anything a test adds."""
    by_day = {r["day"]: r for r in _baseline()}
    by_day[SINGLE_MAX_DAY] = _row(SINGLE_MAX_DAY, lo=71.0, hi=92.0, gust=60.0,
                                  p_lo=29.80, p_hi=29.92, rain=0.0)
    by_day[WILD_DAY] = _row(WILD_DAY, lo=50.0, hi=95.0, gust=55.0,
                            p_lo=29.40, p_hi=30.10, rain=1.20)
    for day, row in overrides.items():
        by_day[day] = {"day": day, **row}
    return [by_day[k] for k in sorted(by_day)]


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    """Rollup rows in, exactly as ingest would have folded them. A key left
    out arrives as SQL NULL — that is how "this station has no rain gauge"
    is spelled, and it must not read as zero anywhere downstream."""
    async def run():
        async with db.connect() as conn:
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


@pytest.fixture()
def engine(client, monkeypatch):
    """Insights on, today pinned. Returns the app.db module for seeding."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _wild(mac: str = MAC, **kw) -> dict:
    return asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_RECORDS], limit=8, **kw))


def _by_scope(out: dict) -> dict[str, dict]:
    """This producer's own stories, by scope. Filtered to `wildest_day` on
    purpose: the records family keeps growing and Biggest Swing also
    carries a "month" period, so an unfiltered map would let one producer
    silently overwrite the other's entry."""
    return {s["period"]["kind"]: s for s in out["stories"]
            if s["story_type"] == "wildest_day"}


# ───────────────────────── the happy path ─────────────────────────

def test_the_wildest_day_is_the_one_that_owns_several_axes_at_once(engine):
    _seed(engine, _wild_rows())
    out = _wild()
    # The records family's other producer needs a captured storm and this
    # fixture seeds rollups only, so it declines — by name, which is the
    # whole point of the field.
    assert out["declined"] == ["storm_broke_the_heat", "record_broken",
                               "lightning_season"]
    scopes = _by_scope(out)
    assert set(scopes) == {"year", "month"}

    s = scopes["month"]
    assert (s["family"], s["story_type"]) == ("records", "wildest_day")
    assert s["title"] == "Wildest Day" and s["emoji"] == "⚡"
    assert s["id"] == "records.wildest_day.month.2026-08"
    assert s["hero_line"] == "AUG 19 WAS AUGUST'S WILDEST DAY SO FAR"

    # Four owned of five ranked: the gust belongs to Aug 5, which is the
    # point — owning most of the month still beats owning one record.
    assert s["hero"]["key"] == "extremes_owned" and s["hero"]["value"] == 4
    assert s["score_parts"]["dimensions"] == 5
    assert s["context"] == (
        "August 19 held August's biggest departure from normal, widest "
        "temperature swing, most rain and widest pressure swing, four of "
        "the five measurements this station can rank.")

    owned = {b["key"] for b in s["viz"]["series"] if b["owned"]}
    assert owned == {"anomaly", "swing", "rain", "pressure"}
    assert s["viz"]["kind"] == "chaos_dimensions"

    # The comparison is against the station's whole record, which is the
    # claim the score actually makes.
    c = s["comparison"]
    assert c["kind"] == "station_days"
    assert (c["rank"], c["of"], c["direction"]) == (1, 364, "above")
    assert c["baseline_label"] == "the median of 364 recorded days"


def test_the_multi_dimension_day_beats_the_single_max_day(engine):
    """Aug 5 holds the station's strongest gust ever recorded. Aug 19 does
    not, and still wins — a leaderboard would have picked Aug 5 and been
    wrong about which day anyone remembers."""
    _seed(engine, _wild_rows())
    s = _by_scope(_wild())["month"]
    assert s["hero_line"].startswith("AUG 19")

    ctx = asyncio.run(stories.build_context(MAC))
    rows = asyncio.run(ctx.daily())
    days = stories._measure_days(rows, {})
    pools = stories._pools(days)
    stories._score_days(days, pools)
    scored = {d.day: d for d in days}

    single, wild = scored[SINGLE_MAX_DAY], scored[WILD_DAY]
    # The single-max day tops the gust axis outright...
    assert single.scores["gust"] > wild.scores["gust"]
    assert single.scores["gust"] == max(d.scores.get("gust", 0.0) for d in days)
    # ...and loses on both halves of the composite anyway.
    assert wild.breadth > single.breadth
    assert wild.intensity > single.intensity
    assert wild.chaos > single.chaos


def test_supporting_stats_carry_measurements_and_native_units(engine):
    """Units are stored API-native and every stat says which. The anomaly
    stat quotes the READING with the departure in its label — the viz bar
    and the stat must not disagree about what number they are showing."""
    _seed(engine, _wild_rows())
    s = _by_scope(_wild())["month"]
    stats = {x["key"]: x for x in s["supporting"]}
    assert stats["rain"]["value"] == 1.2 and stats["rain"]["unit"] == "in"
    assert stats["pressure"]["value"] == 0.7 and stats["pressure"]["unit"] == "inHg"
    assert stats["swing"]["value"] == 45.0 and stats["swing"]["unit"] == "F"
    assert stats["days_considered"]["value"] == 30

    # The gust is the WINNER'S gust — Aug 5 holds the month's strongest —
    # so the label must not call it the month's. A superlative attached to
    # a second-place number is the card lying about its own value.
    assert stats["gust"]["value"] == 55.0 and stats["gust"]["unit"] == "mph"
    assert stats["gust"]["label"] == "peak gust"
    # An OWNED tile carries its whole-record rank (2.0 round-5 review: the
    # tiles used to repeat the bar values verbatim). The rank is against
    # every day that measured the dimension, so the tile says something
    # the bar beside it cannot.
    assert stats["rain"]["label"] == ("most rain of August 2026 so far · "
                                      "1st of 364 days")

    anomaly = stats["anomaly"]
    assert anomaly["unit"] == "F"
    assert anomaly["value"] == 50.0, "the stat must quote the low, not the delta"
    assert anomaly["label"].startswith("coldest morning · ")
    assert "below the August normal low" in anomaly["label"]

    bars = {b["key"]: b for b in s["viz"]["series"]}
    for key, bar in bars.items():
        assert (bar["value"], bar["unit"]) == (stats[key]["value"],
                                               stats[key]["unit"])
    # Ordered by score, so the card draws the strongest bar first.
    scores = [b["score"] for b in s["viz"]["series"]]
    assert scores == sorted(scores, reverse=True)
    assert s["viz"]["highlight"] == s["viz"]["series"][0]["key"]


# ───────────────────── absent is not zero ─────────────────────

def test_a_station_with_no_rain_gauge_still_gets_a_wildest_day(engine):
    """The dimension is DROPPED and the weighted mean renormalizes over
    what is left — the same move `how_hot_is_hot` makes when its standout
    term is missing. A rain-gauge-less station is not a station where it
    never rains."""
    rows = [{**r, "rain": None} for r in _wild_rows()]
    _seed(engine, rows)
    s = _by_scope(_wild())["month"]
    assert s["hero_line"] == "AUG 19 WAS AUGUST'S WILDEST DAY SO FAR"
    assert s["score_parts"]["dimensions"] == 4
    assert {b["key"] for b in s["viz"]["series"]} == {
        "anomaly", "swing", "gust", "pressure"}
    assert not any(x["key"] == "rain" for x in s["supporting"])


def test_a_dropped_dimension_is_not_a_zero_dragging_the_mean_down(engine):
    """The renormalization, measured: removing the rain gauge must not
    lower the winner's intensity, which is exactly what counting the
    missing dimension as zero would do."""
    _seed(engine, _wild_rows())
    with_rain = _by_scope(_wild())["month"]["score_parts"]
    _seed(engine, [{**r, "rain": None} for r in _wild_rows()])
    without = _by_scope(_wild())["month"]["score_parts"]
    assert without["dimensions"] == with_rain["dimensions"] - 1
    assert without["intensity"] >= with_rain["intensity"] - 1e-9
    assert without["breadth"] >= with_rain["breadth"] - 1e-9


def test_a_measured_dry_day_is_a_real_zero_not_a_missing_dimension(engine):
    """The other half of the rule. Aug 5 measured no rain — that is a
    reading, so the dimension is PRESENT and scores zero. Dropping it would
    quietly reward the day for the rain it did not get."""
    _seed(engine, _wild_rows())
    ctx = asyncio.run(stories.build_context(MAC))
    days = stories._measure_days(asyncio.run(ctx.daily()), {})
    pools = stories._pools(days)
    stories._score_days(days, pools)
    single = next(d for d in days if d.day == SINGLE_MAX_DAY)
    assert single.values["rain"] == 0.0
    assert single.scores["rain"] == 0.0


# ───────────────────── rain provenance ─────────────────────

def test_a_haptic_phantom_cannot_win_when_the_tipping_gauge_disagrees(engine):
    """The Tempest lesson, enforced end to end: a bumped mast phantom-tips
    a piezo sensor, so on a dual-rain station the tipping gauge wins. That
    preference lives at ingest (ecowitt._rain), and the producer inherits
    it by reading only the columns it feeds — proven here by running a real
    dual-rain form through the transform.
    """
    from app import ecowitt, insights
    from zoneinfo import ZoneInfo

    form = {"PASSKEY": "A" * 32, "dateutc": "2026-08-25 12:00:00",
            "tempf": "92.0", "windgustmph": "18.0", "baromrelin": "29.90",
            # The mast got bumped: three inches of piezo "rain" while the
            # WH40 tipping gauge sat at zero.
            "dailyrainin": "0.00", "drain_piezo": "3.00",
            "yearlyrainin": "4.00", "yrain_piezo": "9.00"}
    normalized = ecowitt.normalize(form)
    assert normalized["rain"]["daily_in"] == 0.0
    assert normalized["rain"]["yearly_in"] == 4.0

    # Fold it the way ingest would, then let the producer rank the day.
    stamp = datetime.fromisoformat(
        normalized["timestamp_utc"].replace("Z", "+00:00"))
    obs = {"dateutc": int(stamp.timestamp() * 1000),
           "tempf": 92.0, "windgustmph": 18.0, "baromrelin": 29.90,
           "dailyrainin": normalized["rain"]["daily_in"],
           "yearlyrainin": normalized["rain"]["yearly_in"]}
    params = insights.rollup_params(obs, ZoneInfo("UTC"))
    assert params["day"] == "2026-08-25"
    assert params["dailyrainin"] == 0.0, "piezo rain reached the rollups"

    rows = _wild_rows()
    phantom = {"day": "2026-08-25", "lo": 71.0, "hi": 92.0, "gust": 18.0,
               "p_lo": 29.80, "p_hi": 29.92,
               "rain": params["dailyrainin"]}
    _seed(engine, [r for r in rows if r["day"] != "2026-08-25"] + [phantom])

    s = _by_scope(_wild())["month"]
    assert s["hero_line"] == "AUG 19 WAS AUGUST'S WILDEST DAY SO FAR"
    assert not s["hero_line"].startswith("AUG 25")

    ctx = asyncio.run(stories.build_context(MAC))
    days = stories._measure_days(asyncio.run(ctx.daily()), {})
    bumped = next(d for d in days if d.day == "2026-08-25")
    assert bumped.values["rain"] == 0.0


def test_the_producer_never_reads_a_rain_rate_out_of_hourlyrainin():
    """hourlyrainin is a RATE, not an accumulation, and reading it as one
    is a documented way to invent rain. The peak-rate dimension takes the
    already-computed `peak_rate_in_hr` from storm_history instead; this
    module must not name the raw field at all."""
    import inspect
    source = inspect.getsource(stories)
    assert "hourlyrainin" not in source.replace(
        "Nothing here touches `hourlyrainin`", "")


def test_rain_falls_back_to_the_yearly_counter_but_never_across_new_year():
    """The yearly-counter fallback for sources with no daily total. Jan 1 is
    excluded: the counter resets there, so the day's delta would be the
    whole previous year running backwards."""
    mid = {"day": "2026-06-04", "rain_total": None,
           "yearly_min": 3.0, "yearly_max": 3.4}
    assert stories._day_rain_in(mid) == pytest.approx(0.4)
    jan1 = {**mid, "day": "2026-01-01"}
    assert stories._day_rain_in(jan1) is None
    assert stories._day_rain_in({"day": "2026-06-04"}) is None
    # A measured zero survives as a zero.
    assert stories._day_rain_in({"day": "2026-06-04", "rain_total": 0.0}) == 0.0


# ───────────────────── peak rain rate ─────────────────────

def test_peak_rain_rate_rides_storm_history_and_is_absent_elsewhere(engine):
    """The one dimension the daily rollups do not carry. It comes from the
    bounded storm_history table — never from a scan of the 1.15M-row
    observations table — so it exists for days with a recorded storm and is
    ABSENT (not zero) for every other day."""
    from app import db as dbmod

    def ms(day: str, hour: int = 15) -> int:
        return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)

    async def record():
        for day, rate in (("2026-08-19", 4.10), ("2026-06-02", 0.60),
                          ("2026-05-11", 0.35), ("2026-04-07", 0.22)):
            await dbmod.record_storm(MAC, {
                "started_ms": ms(day), "ended_ms": ms(day, 17),
                "total_in": 0.5, "peak_rate_in_hr": rate})
    _seed(engine, _wild_rows())
    asyncio.run(record())

    ctx = asyncio.run(stories.build_context(MAC))
    rates = asyncio.run(stories._storm_peak_rates(ctx))
    assert rates["2026-08-19"] == 4.10
    assert "2026-08-05" not in rates

    s = _by_scope(_wild())["month"]
    assert s["score_parts"]["dimensions"] == 6
    rate_stat = next(x for x in s["supporting"] if x["key"] == "rate")
    assert (rate_stat["value"], rate_stat["unit"]) == (4.1, "in/hr")
    assert "rate" in {b["key"] for b in s["viz"]["series"] if b["owned"]}


def test_a_lone_storm_cannot_become_a_dimension(engine):
    """Under MIN_DIM_POOL comparable values there is no distribution to
    rank against, so the axis is dropped station-wide rather than handing
    its only sample a perfect score."""
    from app import db as dbmod
    _seed(engine, _wild_rows())
    asyncio.run(dbmod.record_storm(MAC, {
        "started_ms": int(datetime(2026, 8, 19, 15, tzinfo=timezone.utc)
                          .timestamp() * 1000),
        "ended_ms": int(datetime(2026, 8, 19, 17, tzinfo=timezone.utc)
                        .timestamp() * 1000),
        "total_in": 1.2, "peak_rate_in_hr": 4.1}))
    s = _by_scope(_wild())["month"]
    assert s["score_parts"]["dimensions"] == 5
    assert not any(x["key"] == "rate" for x in s["supporting"])


# ───────────────────────── decline paths ─────────────────────────

def test_declines_when_the_station_has_too_little_history(engine):
    """Under MIN_STORY_DAYS there is no station distribution, only a
    handful of numbers that would each look like a record."""
    _seed(engine, _baseline(date(2026, 8, 10), TODAY))     # 21 days
    out = _wild()
    assert out["stories"] == [] and out["declined"] == ["biggest_swing", "wildest_day",
                                            "storm_broke_the_heat",
                                            "record_broken", "lightning_season"]


def test_declines_when_no_dimension_can_be_ranked(engine):
    """A year of rollup rows with every sensor column NULL — days the
    station was up but measuring nothing. There is plenty of "data" and
    nothing to rank, which is a decline, never a story about zeros."""
    _seed(engine, [{"day": r["day"]} for r in _baseline()])
    out = _wild()
    assert out["stories"] == [] and out["declined"] == ["biggest_swing", "wildest_day",
                                            "storm_broke_the_heat",
                                            "record_broken", "lightning_season"]


def test_a_temperature_only_station_still_gets_its_two_axes(engine):
    """The other side of the same rule: highs and lows alone are two real
    dimensions, and the weighted mean renormalizes onto them rather than
    counting four missing sensors as calm."""
    rows = [{"day": r["day"], "hi": r["hi"], "lo": r["lo"]}
            for r in _wild_rows()]
    _seed(engine, rows)
    s = _by_scope(_wild())["month"]
    assert s["score_parts"]["dimensions"] == 2
    assert {b["key"] for b in s["viz"]["series"]} == {"anomaly", "swing"}
    assert s["hero_line"] == "AUG 19 WAS AUGUST'S WILDEST DAY SO FAR"


def test_declines_when_nothing_in_the_period_stood_out(engine):
    """A flat station: every day is the same day. There is a maximum, but
    nothing clears WILD_NOTABLE against the station's own record, and a
    wildest day that owns nothing is a calendar entry, not a story."""
    rows = [_row(r["day"], lo=70.0, hi=90.0, gust=15.0,
                 p_lo=29.80, p_hi=29.90, rain=0.0) for r in _baseline()]
    _seed(engine, rows)
    out = _wild()
    assert out["stories"] == [] and out["declined"] == ["biggest_swing", "wildest_day",
                                            "storm_broke_the_heat",
                                            "record_broken", "lightning_season"]


def test_a_station_with_no_rollups_declines(engine):
    assert engine is not None
    out = _wild()
    assert out["stories"] == [] and out["declined"] == ["biggest_swing", "wildest_day",
                                            "storm_broke_the_heat",
                                            "record_broken", "lightning_season"]


# ───────────────────────── the two scopes ─────────────────────────

def test_month_and_year_scopes_pick_their_own_winners(engine):
    """One producer, two scopes. March was the wilder day of the year;
    August still gets its own callout, which is what the monthly card
    renders."""
    march = {"lo": 35.0, "hi": 96.0, "gust": 58.0,
             "p_lo": 29.30, "p_hi": 30.20, "rain": 1.80}
    _seed(engine, _wild_rows(**{"2026-03-11": march}))
    scopes = _by_scope(_wild())

    assert scopes["year"]["id"] == "records.wildest_day.year.2026"
    assert scopes["year"]["hero_line"] == "MAR 11 WAS 2026'S WILDEST DAY SO FAR"
    assert scopes["year"]["period"] == {
        "kind": "year", "label": "2026 so far", "start": "2026-01-01",
        "end": "2026-08-30", "partial": True}

    assert scopes["month"]["id"] == "records.wildest_day.month.2026-08"
    assert scopes["month"]["hero_line"] == "AUG 19 WAS AUGUST'S WILDEST DAY SO FAR"
    # A month still running is labelled "so far" as strictly as a year is:
    # on the 30th, "August's wildest day" is a claim about eleven days
    # nobody has measured yet.
    assert scopes["month"]["period"] == {
        "kind": "month", "label": "August 2026 so far", "start": "2026-08-01",
        "end": "2026-08-30", "partial": True}

    # Different days, so neither claim is a subset of the other.
    assert "redundant_scope" not in scopes["month"]["score_parts"]
    # Both scopes rank against the SAME station-wide distributions — a month
    # normalized against its own 30 days would crown a wildest day every
    # month at a percentile that means nothing.
    assert (scopes["year"]["comparison"]["of"]
            == scopes["month"]["comparison"]["of"] == 364)


def test_one_day_winning_both_scopes_ranks_the_month_below_the_year(engine):
    """The month's claim is true but not new — the year already said it. It
    still ships (the monthly card fetches that scope by name) and simply
    ranks under its own superset."""
    _seed(engine, _wild_rows())
    out = _wild()
    scopes = _by_scope(out)
    assert scopes["year"]["hero_line"] == "AUG 19 WAS 2026'S WILDEST DAY SO FAR"
    assert scopes["month"]["hero_line"] == "AUG 19 WAS AUGUST'S WILDEST DAY SO FAR"
    assert scopes["month"]["score_parts"]["redundant_scope"] == pytest.approx(
        stories.REDUNDANT_SCOPE)
    assert (scopes["month"]["interestingness"]
            < scopes["year"]["interestingness"])
    # Ranked, the year story leads.
    assert out["stories"][0]["period"]["kind"] == "year"


def test_the_newest_period_with_data_wins_not_the_calendar_s(client, monkeypatch):
    """A station that stopped reporting still has a real story about when it
    was reporting. Inventing an empty "this month" would be the zero bug in
    another costume."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: date(2027, 3, 4))
    _seed(db, _wild_rows())
    scopes = _by_scope(_wild())
    assert scopes["month"]["period"]["label"] == "August 2026"
    # A period that ended is not labelled "so far", and its end is the real
    # end of the period rather than a date the station never saw.
    assert scopes["month"]["period"]["partial"] is False
    assert scopes["month"]["period"]["end"] == "2026-08-31"
    assert scopes["year"]["period"] == {
        "kind": "year", "label": "2026", "start": "2026-01-01",
        "end": "2026-12-31", "partial": False}
    assert scopes["year"]["hero_line"] == "AUG 19 WAS 2026'S WILDEST DAY"


# ───────────────────────── determinism ─────────────────────────

def test_the_pinned_today_seam_fixes_the_whole_story(engine, monkeypatch):
    """Same rollups, two different "todays": only the labels that describe
    the window may move, never the day the engine picks or the score it
    gives it. Without the seam these tests would drift across midnight."""
    _seed(engine, _wild_rows())
    first = _by_scope(_wild())
    again = _by_scope(_wild())
    assert first == again

    from app import climate
    monkeypatch.setattr(climate, "local_today", lambda: date(2026, 12, 31))
    later = _by_scope(_wild())
    assert (later["year"]["hero_line"]
            == "AUG 19 WAS 2026'S WILDEST DAY SO FAR")
    assert (later["year"]["interestingness"]
            == first["year"]["interestingness"])
    assert later["year"]["period"]["end"] == "2026-12-31"


def test_mid_rank_scoring_shares_ties_and_earns_its_confidence():
    """The normalization, on its own. Mid-rank so tied values share a
    position, divided by the pool size rather than (n - 1) so the largest
    of four recorded values scores 0.875 — a single wet day on record
    cannot be called a once-in-a-station event."""
    assert stories._rank_share([1.0, 2.0, 3.0, 4.0], 4.0) == pytest.approx(0.875)
    assert stories._rank_share([1.0, 2.0, 3.0, 4.0], 1.0) == pytest.approx(0.125)
    assert stories._rank_share([5.0], 5.0) == pytest.approx(0.5)
    # Ties share one position instead of the later one taking the top.
    assert stories._rank_share([1.0, 4.0, 4.0], 4.0) == pytest.approx(2 / 3)
    assert stories._rank_share([4.0, 4.0], 4.0) == pytest.approx(0.5)
    assert stories._rank_share([1.0, 2.0, 3.0, 4.0], 9.0) == pytest.approx(1.0)


def test_normalization_is_against_the_station_not_an_absolute_threshold(engine):
    """A wild day in Chandler is not a wild day in Irwin PA. The same
    weather, scaled down by a factor of ten, produces the same story with
    the same score — nothing here compares against a fixed number of mph."""
    _seed(engine, _wild_rows())
    loud = _by_scope(_wild())["month"]

    def quieter(r: dict) -> dict:
        out = dict(r)
        base = 60.0
        for k in ("lo", "hi"):
            out[k] = base + (r[k] - base) / 10.0
        out["gust"] = r["gust"] / 10.0
        out["p_lo"] = 29.90 + (r["p_lo"] - 29.90) / 10.0
        out["p_hi"] = 29.90 + (r["p_hi"] - 29.90) / 10.0
        out["rain"] = r["rain"] / 10.0
        return out

    _seed(engine, [quieter(r) for r in _wild_rows()], mac="AA:BB:CC:00:00:99")
    quiet = _by_scope(_wild("AA:BB:CC:00:00:99"))["month"]
    assert quiet["hero_line"] == loud["hero_line"]
    assert quiet["interestingness"] == loud["interestingness"]
    assert quiet["supporting"][0]["value"] != loud["supporting"][0]["value"]


# ───────────────────── cross-producer calibration ─────────────────────

def _best_of(ranked: dict, story_type: str) -> float:
    return max(s["interestingness"] for s in ranked["stories"]
               if s["story_type"] == story_type)


def test_a_genuinely_wild_day_outranks_the_heat_ledger(engine):
    """`interestingness` is documented as comparable ACROSS producers, so
    the two have to land on one 0..1 scale and swap places on merit. Same
    station, same highs, in both this test and the next — only the presence
    of a wild day changes, and only the ranking should move with it."""
    _seed(engine, _wild_rows())
    ranked = asyncio.run(stories.top_stories(MAC, limit=8))
    # A subset check, not equality: the registry keeps growing and these two
    # tests are about where THESE two land relative to each other.
    scores = {s["story_type"] for s in ranked["stories"]}
    assert {"wildest_day", "heat_ledger"} <= scores
    assert _best_of(ranked, "wildest_day") > _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] == "wildest_day"
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


def test_an_unremarkable_wildest_day_yields_to_the_heat_ledger(engine):
    """The direction that makes it calibration rather than a thumb on the
    scale. Drop the engineered days and the month's winner is just the
    busiest ordinary day — it still produces a story, and it must rank
    BELOW the same heat ledger it beat a moment ago."""
    _seed(engine, _baseline())
    ranked = asyncio.run(stories.top_stories(MAC, limit=8))
    assert {"wildest_day", "heat_ledger"} <= {s["story_type"]
                                              for s in ranked["stories"]}
    assert _best_of(ranked, "wildest_day") < _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] == "heat_ledger"

    # The winner owns no single extreme, which is the honest thing to say
    # about it — the copy must not imply a record it did not set.
    month = _by_scope(ranked)["month"]
    assert month["score_parts"]["extremes_owned"] == 0
    assert month["hero"]["value"] == 0
    assert "set no single record" in month["context"]
    assert "held" not in month["context"]


# ───────────────────────── the endpoint ─────────────────────────

def test_endpoint_serves_the_records_family(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _wild_rows())

    r = client.get(f"/api/devices/{MAC}/stories?family=records&limit=8",
                   headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["declined"] == ["storm_broke_the_heat", "record_broken",
                                "lightning_season"]
    kinds = {s["period"]["kind"] for s in body["stories"]}
    assert kinds == {"year", "month"}
    assert all(s["family"] == "records" for s in body["stories"])

    # Unfiltered, both producers participate in one ranking.
    r = client.get(f"/api/devices/{MAC}/stories?limit=8", headers=H)
    types = [s["story_type"] for s in r.json()["stories"]]
    assert "wildest_day" in types and "heat_ledger" in types


def test_the_registry_lists_both_producers():
    assert ("records", "wildest_day") in stories.registered()
    assert ("climate", "how_hot_is_hot") in stories.registered()
