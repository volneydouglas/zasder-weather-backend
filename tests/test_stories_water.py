"""Story engine (2.0): the Water Year producer.

The card exists to teach one idea — a wet season that starts in November
and ends in March is ONE season, and the calendar cuts it in half at New
Year — so the tests that matter are the ones about the boundary and about
what the station actually measured.

    A WATER YEAR IS NAMED FOR THE CALENDAR YEAR IT ENDS IN.

With the default October start that puts November 2025 inside water year
2026, which is the USGS convention and the one every other hydrology number
a reader might compare against uses. It falls out of the bounds arithmetic
rather than being asserted, and the boundary is exercised in BOTH
directions below: September 30 is the last day of one water year, October 1
the first day of the next.

The second rule is the repo's oldest: a water year the station only partly
recorded is not a total. `insights.comparable_to_date` decides which
earlier years may stand beside the current one and the same coverage floors
decide whether the current one may be quoted at all — a station that joined
in February has not had a dry water year, it has had four months of not
knowing.

Fixtures seed daily_rollups directly and pin "today" through
app.climate.local_today, the same seam the other story suites use.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:2C"

TODAY = date(2026, 8, 30)
FIRST = date(2023, 10, 1)

# Rain laid down by hand, by water year, as (month, day, inches). Two
# seasons a year — a winter one that STRADDLES New Year and a late-summer
# one — because the straddle is the entire point of the card. Water year
# 2026 is deliberately a bust.
_SEASONS: dict[int, tuple[tuple[int, int, float], ...]] = {
    2024: ((11, 12, 0.44), (12, 21, 0.90), (1, 8, 0.61), (2, 14, 0.75),
           (3, 2, 0.38), (7, 19, 0.52), (8, 4, 0.83), (8, 21, 0.29)),
    2025: ((10, 30, 0.21), (12, 27, 1.12), (1, 19, 0.48), (2, 3, 0.66),
           (3, 15, 0.55), (7, 28, 0.40), (8, 12, 0.71), (8, 25, 0.33)),
    2026: ((12, 22, 0.52), (1, 30, 0.24), (2, 18, 0.19), (7, 30, 0.36),
           (8, 9, 0.27)),
}


def _rain_map(start_month: int = 10) -> dict[str, float]:
    """(month, day, inches) per water year → day → inches. A month at or
    after the start month belongs to the PREVIOUS calendar year; that is
    the straddle, written out once here so the fixture and the producer
    reach it from opposite directions."""
    out: dict[str, float] = {}
    for label, days in _SEASONS.items():
        for month, day, amount in days:
            year = label - 1 if month >= start_month else label
            out[date(year, month, day).isoformat()] = amount
    return out


def _rows(start: date = FIRST, end: date = TODAY, *,
          missing: frozenset[str] = frozenset(),
          unmeasured: frozenset[str] = frozenset()) -> list[dict]:
    """One rollup row per calendar day.

    `missing` days produce NO ROW — the station was offline. `unmeasured`
    days produce a row whose rain column is NULL — the station was up and
    the gauge was not. Neither may be read as a dry day.
    """
    rain_on = _rain_map()
    rows: list[dict] = []
    d = start
    while d <= end:
        key = d.isoformat()
        if key in missing:
            d += timedelta(days=1)
            continue
        hi = 70.0 + 35.0 * (1 - abs(d.timetuple().tm_yday - 200) / 200.0)
        rows.append({"day": key, "lo": round(hi - 25.0, 1), "hi": round(hi, 1),
                     "rain": (None if key in unmeasured
                              else rain_on.get(key, 0.0))})
        d += timedelta(days=1)
    return rows


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    """A key left out arrives as SQL NULL — how "this station has no rain
    gauge" is spelled, and it must not read as zero anywhere. The station's
    rollups are REPLACED so a leftover row cannot fill a hole a test opened
    on purpose."""
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE mac = ?",
                               (mac,))
            for r in rows:
                hi = r.get("hi")
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n, "
                    " rain_total) VALUES (?,?,?,?,?,?,?)",
                    (mac, r["day"], r.get("lo"), hi, hi,
                     1 if hi is not None else 0, r.get("rain")))
            await conn.commit()
    asyncio.run(run())


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _water(mac: str = MAC, **kw) -> dict:
    """Only this producer's story. The climate family holds four producers
    now and this suite is about what one of them says."""
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_CLIMATE], limit=12, **kw))
    return {**out, "stories": [s for s in out["stories"]
                               if s["story_type"] == "water_year"]}


def _one(mac: str = MAC, **kw) -> dict:
    got = _water(mac, **kw)["stories"]
    assert len(got) == 1, "the producer emits exactly one story"
    return got[0]


# ───────────────── the convention, both directions ─────────────────

def test_a_water_year_is_named_for_the_year_it_ends_in():
    """November 2025 is inside water year 2026. That is the USGS convention
    and the one every hydrology number a reader might compare against uses.
    Exercised on both sides of the boundary: September 30 is the last day of
    one water year, October 1 the first day of the next."""
    label = stories._water_year_label
    assert label(date(2025, 11, 15), 10) == 2026
    assert label(date(2025, 10, 1), 10) == 2026        # the first day
    assert label(date(2026, 9, 30), 10) == 2026        # the last day
    assert label(date(2026, 10, 1), 10) == 2027        # one day later
    assert label(date(2026, 2, 28), 10) == 2026        # deep in the middle

    bounds = stories._water_year_bounds
    assert bounds(2026, 10) == (date(2025, 10, 1), date(2026, 9, 30))
    assert bounds(2027, 10) == (date(2026, 10, 1), date(2027, 9, 30))
    # A southern-hemisphere operator on a July start gets the same shape.
    assert label(date(2026, 7, 1), 7) == 2027
    assert label(date(2026, 6, 30), 7) == 2026
    assert bounds(2027, 7) == (date(2026, 7, 1), date(2027, 6, 30))


def test_the_boundary_is_climates_one_definition_not_a_second_copy():
    """`climate.water_year_start` already decides where a water year begins
    — /api/climate publishes it — so this producer builds on it rather than
    re-deriving it. Two copies would eventually disagree about which year
    the rain in front of you belongs to."""
    from app.climate import water_year_start
    for month in (1, 4, 7, 10, 12):
        d = FIRST
        while d <= TODAY:
            label = stories._water_year_label(d, month)
            start, end = stories._water_year_bounds(label, month)
            assert start == water_year_start(d, month), (d, month)
            assert start <= d <= end, (d, month)
            d += timedelta(days=17)


def test_a_calendar_year_water_year_is_declined_outright(client, monkeypatch):
    """The setting exists so an operator who does not want the distinction
    can switch it off. With a January start the water year IS the calendar
    year, there is no concept left to teach, and a card explaining that
    October is not January would be nonsense."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    _seed(db, _rows())
    assert _water()["stories"]

    monkeypatch.setattr(settings, "water_year_start_month", 1)
    out = _water()
    assert out["stories"] == [] and "water_year" in out["declined"]


def test_the_leap_day_anchor_walks_back_to_a_day_that_exists():
    """The anchor arithmetic's one trap: a water year measured "through
    today" must be measured through the same month-day in every earlier
    year, and every fourth today has no counterpart. The same clamp
    `insights.window_days_to_anchor` makes for a Feb 29 anchor."""
    shift = stories._shift_year
    assert shift(date(2028, 2, 29), -1) == date(2027, 2, 28)
    assert shift(date(2028, 2, 29), -4) == date(2024, 2, 29)
    assert shift(date(2026, 3, 1), -1) == date(2025, 3, 1)
    assert shift(date(2025, 10, 1), 1) == date(2026, 10, 1)


# ───────────────── the running year ─────────────────

def test_the_card_teaches_the_concept_and_states_its_coverage(engine):
    """A number nobody can check is a number nobody should trust — so the
    copy carries the definition AND how much of the window the station was
    there for."""
    _seed(engine, _rows())
    s = _one()
    assert (s["family"], s["story_type"]) == ("climate", "water_year")
    assert s["title"] == "Water Year" and s["emoji"] == "💧"
    assert s["id"] == "climate.water_year.2026"
    assert s["period"] == {"kind": "water_year",
                           "label": "water year 2026 so far",
                           "start": "2025-10-01", "end": "2026-08-30",
                           "partial": True}
    assert s["context"].startswith(
        "A water year runs October 1 to September 30, so one wet season "
        "stays in one column instead of being cut in half at New Year.")
    assert "measured 334 of the 334 days in water year 2026 so far" in \
        s["context"]
    # 0.52 + 0.24 + 0.19 + 0.36 + 0.27
    assert s["hero"]["value"] == pytest.approx(1.58)
    assert s["hero"]["unit"] == "in"
    assert s["hero"]["label"] == "rain since October 1 2025"


def test_the_calendar_year_total_rides_along_as_the_teaching_stat(engine):
    """The teaching moment as a number: the SAME rain counted the way every
    other app counts it. The December storm is inside the water year and
    outside the calendar year to date, and the gap between the two totals
    IS the card."""
    _seed(engine, _rows())
    s = _one()
    ytd = next(x for x in s["supporting"] if x["key"] == "calendar_ytd")
    # Jan 30 + Feb 18 + Jul 30 + Aug 9 — the Dec 22 storm is not in it.
    assert ytd["value"] == pytest.approx(1.06)
    assert s["hero"]["value"] > ytd["value"]
    assert ytd["label"] == "the same rain counted from January 1"


def test_the_bars_and_the_hero_never_disagree(engine):
    _seed(engine, _rows())
    s = _one()
    bars = {b["water_year"]: b for b in s["viz"]["series"]}
    assert s["viz"]["kind"] == "water_year_bars"
    assert sorted(bars) == [2024, 2025, 2026]
    assert bars[2026]["rain"] == s["hero"]["value"]
    assert bars[2026]["hero"] is True
    assert s["viz"]["highlight_key"] == "2026"
    assert bars[2025]["label"] == "October 2024 – September 2025"
    # Every year measured through the same point in its own year.
    assert s["viz"]["axis_label"] == "rain from October 1, through Aug 30"
    # 2024 started with the record on Oct 1 2023, so all three windows are
    # the same length and every bar is a real, comparable measurement.
    assert all(b["comparable"] for b in bars.values())
    assert bars[2024]["rain"] == pytest.approx(4.72)
    assert bars[2025]["rain"] == pytest.approx(4.46)


def test_the_comparison_ranks_against_prior_water_years(engine):
    _seed(engine, _rows())
    c = _one()["comparison"]
    assert c["kind"] == "prior_water_years_to_date"
    assert c["label"] == "vs two earlier water years"
    assert c["value"] == pytest.approx(1.58)
    assert c["baseline"] == pytest.approx(4.59)          # (4.72 + 4.46) / 2
    assert c["baseline_label"] == \
        "the 2024–2025 average, 4.59 in through Aug 30"
    assert (c["rank"], c["of"], c["direction"]) == (3, 3, "below")
    assert c["rank_line"] == "3rd of 3 comparable water years"
    assert c["delta_pct"] == pytest.approx(-65.6, abs=0.2)


def test_a_big_departure_leads_with_the_percentage(engine):
    """The review's rain-race note, applied: "66% BELOW NORMAL" is a
    headline and "1.58 in" is a measurement. The hero STAT stays the total
    so the chart and the hero can never disagree about the same number."""
    _seed(engine, _rows())
    s = _one()
    assert s["hero_line"] == "66% BELOW NORMAL FOR THE DATE"
    assert s["hero"]["value"] == pytest.approx(1.58)
    assert stories.WATER_YEAR_PCT_HERO == 15.0


def test_a_small_departure_leads_with_the_amount(engine):
    """The other direction. A 4% departure dressed as a percentage reads
    bigger than it is, so the amount takes the headline back."""
    seasons = dict(_SEASONS)
    _SEASONS[2026] = ((12, 22, 1.20), (1, 30, 1.10), (2, 18, 0.90),
                      (7, 30, 0.80), (8, 9, 0.59))
    try:
        _seed(engine, _rows())
        s = _one()
    finally:
        _SEASONS[2026] = seasons[2026]
    assert s["hero_line"] == "4.59 IN SINCE OCTOBER 1"
    assert abs(s["comparison"]["delta_pct"]) < stories.WATER_YEAR_PCT_HERO


def test_one_earlier_year_is_a_year_not_a_normal(client, monkeypatch):
    """"The 2025 average" of a single number invites the reader to imagine a
    climatology that isn't there. Same rule the heat ledger pays for."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows(start=date(2024, 10, 1)))
    s = _one()
    assert s["comparison"]["label"] == "vs one earlier water year"
    assert s["comparison"]["baseline_label"].startswith("water year 2025,")
    assert s["hero_line"] == "65% LESS RAIN THAN WATER YEAR 2025"
    assert "NORMAL" not in s["hero_line"]
    baseline = next(x for x in s["supporting"] if x["key"] == "baseline")
    assert baseline["label"] == "water year 2025 to this date"
    assert stories.MIN_NORMAL_YEARS == 2


# ───────────────── the boundary, end to end ─────────────────

def test_september_30_closes_the_water_year_and_october_1_opens_the_next(
        client, monkeypatch):
    """The boundary in both directions, through the whole producer.

    On September 30 water year 2026 is COMPLETE and says so — no "so far".
    One day later water year 2027 exists but holds a single day, which is
    not a total, so the card falls back to the completed year rather than
    presenting one day of October as a season.
    """
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)

    monkeypatch.setattr(climate, "local_today", lambda: date(2026, 9, 30))
    _seed(db, _rows(end=date(2026, 9, 30)))
    closed = _one()
    assert closed["period"] == {"kind": "water_year",
                                "label": "water year 2026",
                                "start": "2025-10-01", "end": "2026-09-30",
                                "partial": False}
    assert "so far" not in closed["context"]
    assert closed["comparison"]["kind"] == "prior_water_years_full"
    # A finished year is not "below normal FOR THE DATE" — it is simply
    # below normal, which is the same wording rule "so far" enforces
    # everywhere else in this engine.
    assert closed["hero_line"] == "66% BELOW NORMAL"

    monkeypatch.setattr(climate, "local_today", lambda: date(2026, 10, 1))
    _seed(db, _rows(end=date(2026, 10, 1)))
    rolled = _one()
    assert stories._water_year_label(date(2026, 10, 1), 10) == 2027
    assert rolled["period"]["label"] == "water year 2026"
    assert rolled["id"] == "climate.water_year.2026"
    # And nothing anywhere presents one day of October as a season.
    assert "2027" not in rolled["hero_line"] + rolled["context"]


def test_a_young_water_year_takes_over_once_it_is_a_total(client, monkeypatch):
    """Six weeks in, water year 2027 has enough measured days to be quoted
    and takes the card back from its predecessor."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: date(2026, 11, 15))
    _seed(db, _rows(end=date(2026, 11, 15)))
    s = _one()
    assert s["period"]["label"] == "water year 2027 so far"
    assert s["period"]["start"] == "2026-10-01"
    assert s["hero"]["label"] == "rain since October 1 2026"
    # Forty-six days, all measured, and a MEASURED zero is a real number.
    days = next(x for x in s["supporting"] if x["key"] == "days_measured")
    assert days["value"] == 46
    assert "measured 46 of the 46 days" in s["context"]


# ───────────────── absent is not zero ─────────────────

def test_declines_when_the_station_has_no_rain_gauge(engine):
    """Every rain reading NULL. That is not the driest water year on record
    — it is no water-year measurement at all."""
    _seed(engine, [{**r, "rain": None} for r in _rows()])
    out = _water()
    assert out["stories"] == [] and "water_year" in out["declined"]


def test_a_half_recorded_water_year_is_not_a_total(client, monkeypatch):
    """A station that joined in February has not had a dry water year; it
    has had four months of not knowing, and 1.06 in printed as the season's
    total would be the absent-is-not-zero bug wearing a rain gauge. The card
    falls back to the last water year the station actually covered."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    # The gauge goes silent for the whole first half of water year 2026.
    dark = frozenset(
        (date(2025, 10, 1) + timedelta(days=i)).isoformat() for i in range(180))
    _seed(db, _rows(unmeasured=dark))

    s = _one()
    assert s["period"]["label"] == "water year 2025"
    assert s["id"] == "climate.water_year.2025"
    bars = {b["water_year"]: b for b in s["viz"]["series"]}
    # 2026 is still DRAWN — it is real data — and flagged so a client can
    # grey it rather than reading a half-covered season as a dry one.
    assert bars[2026]["days_measured"] == 154
    assert bars[2026]["comparable"] is False
    assert bars[2025]["comparable"] is True


def test_every_bar_says_what_precision_its_own_number_carries(engine):
    """`rain` arrives already converted and rounded to the reader's scale.
    Without a precision field the card had to INFER it from the digits, and
    that guess is thinnest exactly where it matters — a millimetre value
    ending in .0 (field report from the card templates, 2026-08-30)."""
    _seed(engine, _rows())
    for units, expected in ((None, 2), (stories.Units(rain="mm"), 1)):
        s = _one(**({"units": units} if units else {}))
        assert all(b["precision"] == expected for b in s["viz"]["series"])
        # It is the SAME number the hero stat was rounded to, so a card can
        # print the bar label and the hero without them disagreeing.
        assert s["hero"]["precision"] == expected


def test_a_partly_measured_bar_explains_itself_in_words(client, monkeypatch):
    """`days_measured` and `window_days` are data a client can sort on. The
    only honest way to put them ON a bar is a sentence, and composing a
    sentence is the client's forbidden move — so the card was drawing
    neither. The producer writes it."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    dark = {(date(2025, 10, 1) + timedelta(days=i)).isoformat()
            for i in range(180)}
    _seed(db, _rows(unmeasured=dark))

    bars = {b["water_year"]: b for b in _one()["viz"]["series"]}
    assert bars[2026]["note"] == "154 of 334 days measured"
    # A fully covered year says nothing: "334 of 334 days measured" is a
    # label that costs space and carries no information.
    assert bars[2025]["days_measured"] == bars[2025]["window_days"]
    assert bars[2025]["note"] is None


def test_a_thin_prior_year_stays_out_of_the_baseline(client, monkeypatch):
    """The same one definition the dry spell and the heat ledger use. A
    water year the station only half covered has less rain because it has
    fewer days; letting it into the baseline manufactures a drought."""
    from app import climate, db, insights
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    # The record starts in April 2024 — water year 2024 covers 149 of its
    # 334-day window, nowhere near comparable with 2026's full one.
    _seed(db, _rows(start=date(2024, 4, 4)))

    s = _one()
    assert s["comparison"]["label"] == "vs one earlier water year"
    bars = {b["water_year"]: b for b in s["viz"]["series"]}
    assert bars[2024]["comparable"] is False
    assert bars[2025]["comparable"] is True
    assert insights.comparable_to_date(bars[2024]["days_measured"], 334) is False
    # The faded bar gets the same sentence the dry spell's chart uses — one
    # grey, one explanation, however many cards a reader meets it on.
    assert s["viz"]["footnote"] == stories.INCOMPARABLE_FOOTNOTE


def test_no_faded_bar_means_no_footnote(engine):
    """An unconditional legend is noise on the cards that do not need it."""
    _seed(engine, _rows())
    s = _one()
    assert all(b["comparable"] for b in s["viz"]["series"])
    assert s["viz"]["footnote"] is None


def test_a_gap_in_the_record_costs_coverage_not_rain(engine):
    """A missing day is not a dry day. Ten days out of the record leave the
    total alone (no rain fell on them either way) and show up honestly in
    the coverage count instead of quietly inflating the station's record."""
    _seed(engine, _rows())
    whole = _one()
    offline = frozenset(
        (date(2026, 5, 1) + timedelta(days=i)).isoformat() for i in range(10))
    _seed(engine, _rows(missing=offline))
    holed = _one()

    assert holed["hero"]["value"] == whole["hero"]["value"]
    days = next(x for x in holed["supporting"] if x["key"] == "days_measured")
    assert days["value"] == 324 and "324 of the 334 days" in holed["context"]


# ───────────────── units ─────────────────

METRIC = stories.Units(temperature="celsius", wind="kph", rain="mm",
                       pressure="hPa")


def _strings(story: dict) -> list[str]:
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"], story["period"]["label"],
           story["disclaimer"] or ""]
    out += [x["label"] for x in story["supporting"]]
    if story["comparison"]:
        c = story["comparison"]
        out += [c["label"], c["baseline_label"], c["rank_line"] or ""]
    out.append(story["viz"]["axis_label"] or "")
    out += [e["label"] for e in story["viz"]["series"]]
    return out


def test_a_millimetre_render_leaks_no_inch_string(engine):
    """Every total stays in stored inches through the comparisons and
    converts only as it becomes words. A threshold compared against a
    converted value is the bug this repo keeps re-shipping.

    The sweep matches a NUMBER followed by the unit rather than the bare
    word: this card's copy is full of honest English "in"s ("in one column",
    "in water year 2026") and a naive substring search would either fail on
    the prose or be quietly disabled to stop it."""
    import re
    inches = re.compile(r"\d\s*in\b")
    _seed(engine, _rows())
    mm = _one(units=METRIC)
    for text in _strings(mm):
        assert not inches.search(text), text
        assert "°F" not in text and "inHg" not in text, text
    # …and the same sweep does find the inches in the native render, so the
    # test is proving something.
    assert any(inches.search(t) for t in _strings(_one()))
    assert {x["unit"] for x in mm["supporting"] if x["unit"]} <= {"mm", "days"}
    assert mm["hero"]["unit"] == "mm" and mm["viz"]["unit"] == "mm"
    assert mm["hero_line"] == "66% BELOW NORMAL FOR THE DATE"

    native = _one()
    assert mm["hero"]["value"] == pytest.approx(
        native["hero"]["value"] * 25.4, abs=0.1)
    # One decimal of a millimetre, so the rain-day definition the card
    # states out loud does not print as "0 mm".
    rain_days = next(x for x in mm["supporting"] if x["key"] == "rain_days")
    assert rain_days["label"] == "days with at least 0.3 mm"


def test_the_same_rain_in_two_scales_tells_the_same_story(engine):
    """Only the words and the numbers move. A story that changed its mind
    about what was interesting when the reader switched to millimetres would
    mean a threshold was being compared against a converted value."""
    _seed(engine, _rows())
    native, metric = _one(), _one(units=METRIC)
    assert native["interestingness"] == metric["interestingness"]
    assert native["score_parts"] == metric["score_parts"]
    assert native["period"] == metric["period"]
    assert native["id"] == metric["id"]
    assert native["viz"]["highlight_key"] == metric["viz"]["highlight_key"]
    assert [e["days_measured"] for e in native["viz"]["series"]] == \
        [e["days_measured"] for e in metric["viz"]["series"]]


# ───────────────── determinism ─────────────────

def test_the_pinned_today_seam_fixes_the_whole_story(engine):
    _seed(engine, _rows())
    first, again = _one(), _one()
    assert first == again


# ───────────────── scoring ─────────────────

def test_a_record_dry_year_scores_like_a_record_wet_one(client, monkeypatch):
    """Extremity is folded around the middle on purpose: the DRIEST season
    on record is exactly as remarkable as the wettest, and a plain rank
    share would score a record drought at zero."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)

    _seed(db, _rows())
    dry = _one()
    assert dry["score_parts"]["extremity"] == pytest.approx(0.6667, abs=1e-3)

    seasons = _SEASONS[2026]
    _SEASONS[2026] = ((12, 22, 3.10), (1, 30, 2.40), (2, 18, 1.90),
                      (7, 30, 2.60), (8, 9, 1.80))
    try:
        _seed(db, _rows())
        wet = _one()
    finally:
        _SEASONS[2026] = seasons
    assert wet["score_parts"]["extremity"] == dry["score_parts"]["extremity"]
    assert wet["comparison"]["direction"] == "above"
    assert wet["hero_line"].endswith("ABOVE NORMAL FOR THE DATE")


def test_a_first_year_station_still_gets_the_teaching_card(client, monkeypatch):
    """No prior water year means no departure and no rank — but the concept
    is still worth explaining and the total is still real. The weighted mean
    renormalizes onto what is left rather than scoring absent dimensions as
    zero, exactly as the heat ledger does when its standout term is
    missing."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows(start=date(2025, 10, 1)))
    s = _one()
    assert s["comparison"] is None
    assert s["score_parts"].keys() == {"concentration"}
    assert s["hero_line"] == "1.58 IN SINCE OCTOBER 1"
    assert s["viz"]["series"][0]["water_year"] == 2026
    assert len(s["viz"]["series"]) == 1


# ───────────────── cross-producer calibration ─────────────────

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_a_failed_wet_season_outranks_the_heat_ledger(engine):
    """`interestingness` is documented as comparable ACROSS producers, so
    they have to land on one 0..1 scale and swap places on merit. A season
    that produced ONE storm and nothing else — 92% under normal, all of it
    on a single afternoon — is the story on this station, over a heat
    ledger the same fixture also earns."""
    seasons = _SEASONS[2026]
    _SEASONS[2026] = ((12, 22, 0.35),)
    try:
        _seed(engine, _rows())
        ranked = asyncio.run(stories.top_stories(MAC, limit=12))
        s = next(x for x in ranked["stories"]
                 if x["story_type"] == "water_year")
    finally:
        _SEASONS[2026] = seasons
    assert s["score_parts"]["departure"] == 1.0
    assert s["score_parts"]["concentration"] == 1.0
    assert _best_of(ranked, "water_year") > 0.85
    assert _best_of(ranked, "water_year") > _best_of(ranked, "heat_ledger")
    # The dry spell leads this particular fixture, and correctly so: a
    # season with one storm in it also contains the longest rainless run the
    # station has ever measured. Two producers agreeing that the drought is
    # the story is calibration working, not a tie to break.
    assert {s["story_type"] for s in ranked["stories"][:2]} == {
        "dry_spell", "water_year"}


def test_an_ordinary_water_year_yields_to_the_heat_ledger(engine):
    """The direction that makes it calibration rather than a thumb on the
    scale. A season that landed on its own normal still produces a story —
    the concept is worth teaching — and must rank BELOW the ledger it beat
    a moment ago."""
    seasons = _SEASONS[2026]
    _SEASONS[2026] = ((12, 22, 1.20), (1, 30, 1.10), (2, 18, 0.90),
                      (7, 30, 0.80), (8, 9, 0.59))
    try:
        _seed(engine, _rows())
        ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    finally:
        _SEASONS[2026] = seasons
    assert _best_of(ranked, "water_year") < 0.30
    assert _best_of(ranked, "water_year") < _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] != "water_year"
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


# ───────────────── the endpoint ─────────────────

def test_endpoint_serves_the_water_year(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "water_year_start_month", 10)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows())
    r = client.get(f"/api/devices/{MAC}/stories?family=climate&limit=12",
                   headers=H)
    assert r.status_code == 200
    s = next(x for x in r.json()["stories"] if x["story_type"] == "water_year")
    assert s["hero_line"] == "66% BELOW NORMAL FOR THE DATE"
    assert ("climate", "water_year") in stories.registered()
