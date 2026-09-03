"""Story engine (2.0): the Growing Season producer.

TWO RULES CARRY THIS SUITE, and neither is about arithmetic.

(a) NEVER CLAIM A YEAR WE HAVE NOT LIVED. A season still running is
    "freeze-free, 239 days and counting" — never "all year", never a total.
    The trap is that the wrong words are the FLUENT ones: "frost-free all
    year" is the phrase every gardening page uses, so a forbidden-vocabulary
    sweep runs over the whole rendered surface of a running season, in the
    same spirit as the Air & Flight suite's.

(b) A STATION WITH NO COLD DATA DECLINES. A record with no freeze in it is
    almost never a frost-free paradise — it is a station that has not been
    through a winter, or one whose thermometer arrived in April. Four
    separate decline paths below, one for each way a station can fail to
    know its own season.

The third thing worth knowing: a RUNNING season compared against finished
ones is 158 days against an average of 231, and "31% below average" is a
headline the calendar wrote rather than the weather. Every year is therefore
measured through the anchor's month-day while the season runs — the same
move `tiers_to_date` makes for the heat ledger — and the tests pin both the
numbers and the sentence that explains the frame.

Fixtures pin "today" through app.climate.local_today, the seam every story
suite uses. This producer needs no coordinates: a freeze is something the
station measured.
"""
from __future__ import annotations

import asyncio
import math
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:5F"
TODAY = date(2026, 8, 30)

# A Chandler-shaped year: a winter that just dips below freezing, a summer
# that does not. Per-year offsets give the seasons different lengths, which
# is what makes a comparison mean anything — a climate that repeated itself
# exactly would (correctly) score zero.
_YEAR_OFFSET = {2022: 0.0, 2023: -4.0, 2024: 2.5, 2025: -1.0, 2026: 6.0}

# The forbidden vocabulary for a running season. Every one of these is a
# claim about a year nobody has finished living.
_FORBIDDEN = ("all year", "the whole year", "365", "frost-free year")


def _rows(first: date, last: date, offset: dict | None = None,
          shift: float = 0.0) -> list[dict]:
    """Daily highs and lows on a smooth annual curve. `shift` moves the
    whole climate — +25°F is a station that never freezes."""
    offset = _YEAR_OFFSET if offset is None else offset
    rows: list[dict] = []
    d = first
    while d <= last:
        doy = d.timetuple().tm_yday
        hi = (72.0 + 34.0 * math.cos((doy - 200) * 2 * math.pi / 365)
              + offset.get(d.year, 0.0) + shift)
        rows.append({"day": d.isoformat(), "hi": round(hi, 1),
                     "lo": round(hi - 26.0, 1)})
        d += timedelta(days=1)
    return rows


def _southern(first: date, last: date) -> list[dict]:
    """The same climate half a world away: warmest in January, coldest in
    July. A calendar year cuts this station's growing season in half, which
    is the one thing no rewording can fix."""
    rows: list[dict] = []
    d = first
    while d <= last:
        doy = d.timetuple().tm_yday
        hi = 72.0 + 34.0 * math.cos((doy - 17) * 2 * math.pi / 365)
        rows.append({"day": d.isoformat(), "hi": round(hi, 1),
                     "lo": round(hi - 26.0, 1)})
        d += timedelta(days=1)
    return rows


def _seed(db, rows: list[dict], mac: str = MAC, *, lows: bool = True) -> None:
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE mac = ?",
                               (mac,))
            for r in rows:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?,?,?,?,?,?)",
                    (mac, r["day"], r["lo"] if lows else None, r["hi"],
                     r["hi"], 1))
            await conn.commit()
    asyncio.run(run())


@pytest.fixture()
def garden(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    state = {"today": TODAY, "db": db}
    monkeypatch.setattr(climate, "local_today", lambda: state["today"])

    async def register():
        await db.upsert_device(MAC, {"name": "Backyard"})
    asyncio.run(register())
    _seed(db, _rows(date(2022, 1, 1), TODAY))
    return state


def _story(state, rows: list[dict] | None = None, day: date | None = None,
           units: stories.Units | None = None) -> dict | None:
    if day is not None:
        state["today"] = day
    if rows is not None:
        _seed(state["db"], rows)
    kw = {"units": units} if units is not None else {}
    ranked = asyncio.run(stories.top_stories(MAC, limit=12, **kw))
    return next((s for s in ranked["stories"]
                 if s["story_type"] == "growing_season"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


def _rendered(story: dict) -> list[str]:
    """EVERY string a card can draw from this story. A vocabulary sweep that
    misses a field is a vocabulary sweep that passes."""
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"], story["period"]["label"],
           story["viz"]["axis_label"], story["viz"]["footnote"] or ""]
    out += [x["label"] for x in story["supporting"]]
    if story["comparison"]:
        c = story["comparison"]
        out += [c["label"], c["baseline_label"], c["rank_line"] or ""]
    out += [e["range_label"] for e in story["viz"]["series"]]
    out += [e["note"] or "" for e in story["viz"]["series"]]
    return out


# ═════════════ rule (a): never claim a year we have not lived ═════════════

def test_a_running_season_counts_and_never_totals(garden):
    s = _story(garden)
    assert s["hero_line"] == "FREEZE-FREE, 170 DAYS AND COUNTING"
    assert s["period"]["partial"] is True
    assert s["period"]["label"] == "2026 so far"
    assert "no autumn freeze has come yet" in s["context"]
    assert "still counting as of August 30" in s["context"]


def test_the_forbidden_vocabulary_appears_nowhere_on_a_running_season(garden):
    """"Frost-free all year" is the phrase every gardening page uses, which
    is exactly why a card generated on August 30 must not contain it."""
    s = _story(garden)
    for text in _rendered(s):
        lowered = text.lower()
        for banned in _FORBIDDEN:
            assert banned not in lowered, (banned, text)


def test_the_running_bar_says_so_in_words(garden):
    """A template that wanted to mark a bar as still growing could only
    reach for a glyph, and a glyph is a word the client invented."""
    s = _story(garden)
    hero = next(e for e in s["viz"]["series"] if e["hero"])
    assert hero["running"] is True
    assert hero["note"] == "still running"
    assert hero["first_autumn"] is None
    assert "still running" in s["viz"]["footnote"]


def test_a_partly_measured_season_explains_itself_in_words(garden):
    """The same fix the water-year bars took: `days_measured` and
    `window_days` are data a client can sort on, and the only honest way to
    put them ON a bar is a sentence the producer writes. One note slot, a
    clear precedence — a bar that is still growing says THAT first, because
    it changes how the length is read."""
    rows = [r for r in _rows(date(2022, 1, 1), TODAY)
            if not ("2024-09-01" <= r["day"] <= "2024-10-15")]
    s = _story(garden, rows)
    bars = {e["year"]: e for e in s["viz"]["series"]}
    assert bars[2024]["days_measured"] == 321
    assert bars[2024]["window_days"] == 366
    assert bars[2024]["note"] == "321 of 366 days measured"
    # A fully measured year says nothing — "365 of 365" costs space and
    # carries no information.
    assert bars[2025]["note"] is None
    # …and the running year still leads with the word that matters.
    assert bars[2026]["note"] == "still running"


def test_no_row_carries_a_date_phrase_no_card_draws(garden):
    """Every row used to ship `last_spring_label` and `first_autumn_label`
    — a date phrase per freeze, per year. No template drew either, and one
    that had would have printed three date phrases across ten rows, which
    is a table, not a picture. The hero's own two dates survive as
    supporting stats, where a card can place them; the machine-readable
    dates survive on the row. Only the prose is gone."""
    s = _story(garden)
    for entry in s["viz"]["series"]:
        assert "last_spring_label" not in entry
        assert "first_autumn_label" not in entry
        # The DATA a client can sort or place is still there.
        assert "last_spring" in entry and "first_autumn" in entry
    assert _stat(s, "last_spring_freeze") is not None
    hero_year = next(e["year"] for e in s["viz"]["series"] if e["hero"])
    assert _stat(s, "last_spring_freeze")["label"].startswith(
        "last spring freeze · ")
    assert hero_year == 2026


def test_a_closed_season_states_a_total(garden):
    """Once the autumn freeze has come, the season IS finished and may be
    quoted as one — even in December, before the calendar year is over."""
    s = _story(garden, _rows(date(2022, 1, 1), date(2025, 12, 31)),
               day=date(2026, 1, 5))
    assert s["hero_line"] == "227 FREEZE-FREE DAYS"
    assert s["period"]["partial"] is False
    assert "the first autumn freeze came November 10" in s["context"]
    assert s["comparison"]["rank_line"] == "3rd of 4 comparable seasons"


# ═════════════ the to-date frame ═════════════

def test_earlier_seasons_are_counted_through_the_same_date(garden):
    """158 days against an average of 231 is a headline the calendar wrote.
    While this season runs, every year answers the same question about the
    same window."""
    s = _story(garden)
    bars = {e["year"]: e for e in s["viz"]["series"]}
    assert bars[2026]["days"] == 170
    # The earlier years' COMPLETE spans are seven months longer, and both
    # numbers travel so a client never has to re-derive one from dates.
    assert bars[2022]["days"] == 158 and bars[2022]["days_full"] == 231
    assert bars[2025]["days"] == 156 and bars[2025]["days_full"] == 227
    assert s["comparison"]["baseline_label"].endswith("through Aug 30")
    assert s["comparison"]["kind"] == "prior_years_to_date"
    assert s["comparison"]["direction"] == "above"
    assert "counted through Aug 30" in s["viz"]["footnote"]


def test_a_running_season_ranks_only_inside_that_frame(garden):
    """It CAN be ranked — but the words have to carry the frame, or the next
    frost withdraws the claim."""
    s = _story(garden)
    assert s["comparison"]["rank"] == 1
    assert s["comparison"]["rank_line"] == \
        "the longest so far of 5 comparable seasons to this date"
    assert "longest season on record" not in s["context"]


def test_the_score_survives_a_running_season(garden):
    """Scoring a running season against finished ones would have forced the
    dimensions off; inside the to-date frame they all apply."""
    s = _story(garden)
    assert set(s["score_parts"]) == {"extremity", "departure", "shift"}
    assert s["interestingness"] > 0.5


# ═════════════ rule (b): four ways to decline ═════════════

def test_declines_when_the_station_measured_no_lows_at_all(garden):
    """A rollup row with no `tempf_min` is a day the station was up and the
    thermometer was not. A freeze could have hidden in every one of them."""
    _seed(garden["db"], _rows(date(2022, 1, 1), TODAY), lows=False)
    assert _story(garden) is None


def test_declines_when_the_station_has_never_recorded_a_freeze(garden):
    """The one that would flatter. Four full years, not one freeze — far
    more often a missing winter than a missing frost, and this producer
    cannot tell the two apart, so it says nothing."""
    assert _story(garden, _rows(date(2022, 1, 1), TODAY, shift=25.0)) is None


def test_declines_when_the_station_arrived_after_the_cold_season(garden):
    """A station switched on in March cannot rule out a February freeze, and
    "the last spring freeze was March 3" would be a claim about a month
    nobody watched."""
    assert _story(garden, _rows(date(2026, 3, 1), TODAY)) is None


def test_declines_below_the_measured_day_floor(garden):
    """Six weeks is not a season."""
    assert _story(garden, _rows(date(2026, 1, 1), date(2026, 2, 14)),
                  day=date(2026, 2, 14)) is None


def test_declines_when_a_calendar_year_would_cut_the_season_in_half(garden):
    """A southern-hemisphere station's growing season straddles New Year.
    No rewording fixes that, and inventing a "growing year" the way the
    water year is invented is a bigger change than one producer should make
    on its own — so it declines instead of drawing a halved season."""
    assert _story(garden, _southern(date(2022, 1, 1), TODAY)) is None


def test_a_gap_in_the_middle_of_a_year_declines_that_year(garden):
    """Coverage is not just about when the station arrived. A summer spent
    offline leaves a first-autumn-freeze claim nobody can back."""
    rows = [r for r in _rows(date(2022, 1, 1), TODAY)
            if not (r["day"] >= "2026-03-01" and r["day"] <= "2026-08-01")]
    s = _story(garden, rows)
    assert s is not None                    # earlier years still speak
    assert s["viz"]["highlight_key"] == "2025"
    assert 2026 not in {e["year"] for e in s["viz"]["series"]}


# ═════════════ the numbers ═════════════

def test_the_freezes_and_the_definition_travel_with_the_story(garden):
    s = _story(garden)
    assert _stat(s, "last_spring_freeze")["label"] == \
        "last spring freeze · March 13"
    # The VALUE is the day of the year — the number a timeline places a
    # marker at. The date itself is in the label.
    assert _stat(s, "last_spring_freeze")["value"] == 72
    assert _stat(s, "freeze_days")["label"] == \
        "days at or below 32°F in 2026 so far"
    assert "A freeze is a day this station's low reached 32°F or colder." \
        in s["context"]
    assert _stat(s, "days_measured")["value"] == 242


def test_a_first_season_teaches_the_concept_and_does_not_lead(garden):
    """One year on record: nothing to rank against, nothing to depart from.
    The story is worth telling and never worth leading with."""
    s = _story(garden, _rows(date(2026, 1, 1), TODAY))
    assert s["score_parts"] == {"first_season": stories.GROWING_BASE}
    assert s["interestingness"] == stories.GROWING_BASE
    assert s["comparison"] is None
    assert len(s["viz"]["series"]) == 1
    assert "AND COUNTING" in s["hero_line"]


# ═════════════ units ═════════════

def test_a_celsius_render_leaks_no_fahrenheit(garden):
    """The freeze threshold stays Fahrenheit through every comparison and
    converts only at the moment of rendering. Banding days against a
    converted constant is the bug this repo keeps re-shipping."""
    metric = stories.Units(temperature="celsius", wind="kph", rain="mm",
                           pressure="hPa")
    s = _story(garden, units=metric)
    for text in _rendered(s):
        assert "°F" not in text and "inHg" not in text and "mph" not in text
    assert "A freeze is a day this station's low reached 0°C or colder." \
        in s["context"]
    assert _stat(s, "freeze_days")["label"] == \
        "days at or below 0°C in 2026 so far"
    units = {x["unit"] for x in s["supporting"] if x["unit"]}
    assert units <= {"days", "C"}


def test_the_freeze_membership_does_not_move_with_the_reader(garden):
    """Only the printed threshold converts. If a day changed sides when the
    reader switched to Celsius, a constant was being compared against a
    converted value."""
    metric = stories.Units(temperature="celsius")
    native = _story(garden)
    celsius = _story(garden, units=metric)
    assert native["hero"]["value"] == celsius["hero"]["value"]
    assert native["score_parts"] == celsius["score_parts"]
    assert _stat(native, "freeze_days")["value"] == \
        _stat(celsius, "freeze_days")["value"]
    assert _stat(native, "last_spring_freeze")["value"] == \
        _stat(celsius, "last_spring_freeze")["value"]
    # The coldest low IS a reading, so it converts with the offset.
    cold_f = _stat(native, "coldest_low")["value"]
    cold_c = _stat(celsius, "coldest_low")["value"]
    assert cold_c == pytest.approx((cold_f - 32) * 5 / 9, abs=0.06)


# ═════════════ determinism ═════════════

def test_the_same_anchor_produces_the_same_season_twice(garden):
    assert _story(garden) == _story(garden)


def test_moving_the_anchor_moves_the_frame(garden):
    """The to-date window follows the pinned date, so a week later every
    bar in the chart is a week longer."""
    first = _story(garden, day=TODAY)
    later = _story(garden, _rows(date(2022, 1, 1), TODAY + timedelta(days=7)),
                   day=TODAY + timedelta(days=7))
    assert later["hero"]["value"] == first["hero"]["value"] + 7
    bars_first = {e["year"]: e["days"] for e in first["viz"]["series"]}
    bars_later = {e["year"]: e["days"] for e in later["viz"]["series"]}
    assert all(bars_later[y] == bars_first[y] + 7 for y in bars_first)


# ═════════════ cross-producer calibration ═════════════

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_a_record_late_frost_outranks_an_ordinary_year_of_itself(garden):
    """`interestingness` is comparable across producers, so it has to be
    comparable across DATA first. A season a full month longer than any this
    station has recorded must score far above a season that landed on the
    station's own average."""
    record = _story(garden)["interestingness"]
    _YEAR_OFFSET[2026] = -1.0                       # an utterly ordinary year
    try:
        ordinary = _story(garden, _rows(date(2022, 1, 1), TODAY))
    finally:
        _YEAR_OFFSET[2026] = 6.0
    assert ordinary["score_parts"]["departure"] < 0.1
    assert ordinary["interestingness"] < 0.25
    assert record > 2 * ordinary["interestingness"]


def test_an_ordinary_season_yields_to_the_heat_ledger(garden):
    """The direction that makes it calibration rather than a thumb on the
    scale."""
    _YEAR_OFFSET[2026] = -1.0
    try:
        _seed(garden["db"], _rows(date(2022, 1, 1), TODAY))
        ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    finally:
        _YEAR_OFFSET[2026] = 6.0
    assert _best_of(ranked, "heat_ledger") > 0.0, "the fixture must earn one"
    assert _best_of(ranked, "growing_season") < \
        _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] != "growing_season"
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


# ═════════════ the endpoint ═════════════

def test_the_endpoint_serves_the_growing_season(garden, client):
    r = client.get(f"/api/devices/{MAC}/stories?family=sky&limit=5", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "growing_season" in {s["story_type"] for s in body["stories"]}
    # No coordinates on this station: the two astronomical producers decline
    # and this one still speaks. That split is the family's whole point.
    assert "shrinking_day" in body["declined"]
    assert "tonights_sky" in body["declined"]


def test_a_bar_bounded_by_the_record_says_so(garden):
    """A year that never froze at either end did not run freeze-to-freeze:
    its bar starts and stops at the edge of the record, so its LENGTH is
    not the measurement the bars beside it are showing. That is the part of
    the old per-row freeze labels worth keeping, and the note slot is where
    a card can actually draw it."""
    rows = _rows(date(2022, 1, 1), TODAY,
                 offset={2022: 0.0, 2023: 25.0, 2024: 2.5,
                         2025: -1.0, 2026: 6.0})
    s = _story(garden, rows)
    bars = {e["year"]: e for e in s["viz"]["series"]}
    assert bars[2023]["last_spring"] is None
    assert bars[2023]["first_autumn"] is None
    assert bars[2023]["note"] == "no freeze at either end"
    # A year bounded by real freezes at both ends needs no qualifier.
    assert bars[2024]["note"] is None
    # Still-running keeps precedence over the missing autumn freeze it
    # already implies — "yet" versus "recorded" never has to be decided.
    assert bars[2026]["note"] == "still running"
    # And the new wording stays out of the forbidden vocabulary.
    for text in _rendered(s):
        for banned in _FORBIDDEN:
            assert banned not in text.lower(), (banned, text)
