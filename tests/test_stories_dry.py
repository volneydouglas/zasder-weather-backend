"""Story engine (2.0): the Dry Spell producer.

One rule matters more than everything else in this file and it is the
reason the producer exists at all:

    A DRY SPELL IS BUILT FROM MEASURED-ZERO DAYS, NOT FROM MISSING DAYS.

A station that was unplugged for ten days in the middle of a drought has
not had one long dry spell, it has had two — and the ten days in between
are not evidence of anything. The pair of fixtures below is the same
weather twice, differing only in whether those ten days were RECORDED, and
the whole card changes with them.

Fixtures seed daily_rollups directly and pin "today" through
app.climate.local_today, the same seam the other story suites use, so every
number here is fixed by the fixture rather than by the day the suite runs.
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
FIRST = date(2024, 1, 1)

# Day-of-year offsets that recorded rain, per year. Laid out by hand rather
# than generated: the 2026 list simply STOPS after April 4, which is what a
# desert spring looks like and what the card is about. The gaps in the 2024
# and 2025 lists give each of those years a long spell of its own so the
# year-bar comparison has something honest to compare against.
_RAIN_DOY = {
    2024: (10, 24, 41, 55, 70, 88, 200, 214, 221, 228, 236, 244, 300, 320, 350),
    2025: (12, 26, 44, 60, 74, 92, 205, 219, 226, 233, 241, 249, 305, 325, 355),
    2026: (14, 28, 46, 62, 76, 94),
}
RAIN_IN = 0.35


def _rain_days() -> set[str]:
    out: set[str] = set()
    for year, doys in _RAIN_DOY.items():
        for doy in doys:
            out.add((date(year, 1, 1) + timedelta(days=doy - 1)).isoformat())
    return out


def _rows(start: date = FIRST, end: date = TODAY, *,
          missing: frozenset[str] = frozenset(),
          unmeasured: frozenset[str] = frozenset()) -> list[dict]:
    """One rollup row per calendar day.

    `missing` days produce NO ROW — the station was offline. `unmeasured`
    days produce a row whose rain column is NULL — the station was up and
    the rain gauge was not. Both are "we don't know", and neither may be
    read as a dry day.
    """
    rain_on = _rain_days()
    rows: list[dict] = []
    d = start
    while d <= end:
        key = d.isoformat()
        if key in missing:
            d += timedelta(days=1)
            continue
        # A plain seasonal temperature curve: nothing here reads it, but
        # assemble() needs highs to build a year at all.
        hi = 70.0 + 35.0 * (1 - abs(d.timetuple().tm_yday - 200) / 200.0)
        rows.append({"day": key, "lo": round(hi - 25.0, 1), "hi": round(hi, 1),
                     "rain": (None if key in unmeasured
                              else RAIN_IN if key in rain_on else 0.0)})
        d += timedelta(days=1)
    return rows


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    """A key left out arrives as SQL NULL — that is how "this station has no
    rain gauge" is spelled, and it must not read as zero anywhere.

    The station's rollups are REPLACED, not merged: several tests below seed
    the same station twice to show the same weather with and without a hole
    in the record, and a leftover row would quietly fill the hole back in.
    """
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
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _dry(mac: str = MAC, **kw) -> dict:
    """Only this producer's story. The climate family holds three producers
    now and this suite is about what one of them says."""
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_CLIMATE], limit=12, **kw))
    return {**out, "stories": [s for s in out["stories"]
                               if s["story_type"] == "dry_spell"]}


def _one(mac: str = MAC) -> dict:
    stories_ = _dry(mac)["stories"]
    assert len(stories_) == 1, "the producer emits exactly one story"
    return stories_[0]


# ───────────────── the rule the whole card rests on ─────────────────

def test_a_gap_in_the_record_breaks_the_streak(engine):
    """THE test. April 5 through August 30 is 148 measured-dry days. Take
    ten of those days out of the record — the station was offline — and the
    run is no longer 148 days, because nobody knows what happened on the
    days that are gone. The producer must report the longer of the two
    surviving halves and must never name the span it cannot vouch for."""
    _seed(engine, _rows())
    whole = _one()
    assert whole["hero"]["value"] == 148
    assert whole["hero_line"] == "148 DAYS AND COUNTING. NOT A DROP."
    assert whole["period"]["start"] == "2026-04-05"

    offline = frozenset(
        (date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(10))
    _seed(engine, _rows(missing=offline))
    holed = _one()

    # 81 days: June 11 through August 30. The 57 days before the outage are
    # a separate spell, and the ten days themselves are nobody's.
    assert holed["hero"]["value"] == 81
    assert holed["period"]["start"] == "2026-06-11"
    assert holed["hero_line"] == "81 DAYS AND COUNTING. NOT A DROP."
    assert "148" not in holed["hero_line"] + holed["context"]

    # And nothing anywhere in the payload still claims the unbroken run.
    lengths = [b["days"] for b in holed["viz"]["series"]]
    assert 148 not in lengths
    assert max(lengths) < 148


def test_a_silent_rain_gauge_breaks_the_streak_exactly_like_a_missing_day(engine):
    """The subtler half. The station was UP for those ten days — there are
    rollup rows, with temperatures — but the rain column is NULL. Absent is
    not zero: a day whose rain was never measured cannot be counted as a day
    it did not rain, so the run breaks in exactly the same place."""
    silent = frozenset(
        (date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(10))
    _seed(engine, _rows(unmeasured=silent))
    quiet = _one()
    assert quiet["hero"]["value"] == 81
    assert quiet["period"]["start"] == "2026-06-11"

    offline = silent
    _seed(engine, _rows(missing=offline))
    gone = _one()
    # Identical stories: "the station was offline" and "the gauge said
    # nothing" are the same amount of knowledge.
    assert quiet["hero"]["value"] == gone["hero"]["value"]
    assert quiet["period"] == gone["period"]
    assert quiet["context"] == gone["context"]


def test_the_spell_scanner_never_carries_a_run_across_a_hole():
    """The scanner on its own, away from the engine. Three shapes: a clean
    run, a run split by a missing day, and a run split by an unmeasured
    one."""
    def rows(*spec: tuple[str, float | None]) -> list[dict]:
        return [{"day": d, "rain_total": r} for d, r in spec]

    clean = stories._dry_spells(rows(
        ("2026-03-01", 0.0), ("2026-03-02", 0.0), ("2026-03-03", 0.0),
        ("2026-03-04", 0.5)))
    assert [(s.days, s.broke_on) for s in clean] == [(3, "2026-03-04")]

    holed = stories._dry_spells(rows(
        ("2026-03-01", 0.0), ("2026-03-02", 0.0),
        # 03-03 was never recorded at all
        ("2026-03-04", 0.0), ("2026-03-05", 0.0), ("2026-03-06", 0.5)))
    assert [s.days for s in holed] == [2, 2]
    # The first run did not break on rain — it ran out of record, so it
    # names no breaking day rather than borrowing the next one it can find.
    assert holed[0].broke_on is None and holed[1].broke_on == "2026-03-06"

    silent = stories._dry_spells(rows(
        ("2026-03-01", 0.0), ("2026-03-02", 0.0), ("2026-03-03", None),
        ("2026-03-04", 0.0), ("2026-03-05", 0.0)))
    assert [s.days for s in silent] == [2, 2]

    # A trace at the definition's own line is rain, not a dry day.
    from app.insights import RAIN_DAY_MIN_IN
    edge = stories._dry_spells(rows(
        ("2026-03-01", 0.0), ("2026-03-02", RAIN_DAY_MIN_IN),
        ("2026-03-03", 0.0)))
    assert [s.days for s in edge] == [1, 1]


# ───────────────────────── the happy path ─────────────────────────

def test_the_card_states_its_definition_and_its_coverage_rule(engine):
    """A number nobody can check is a number nobody should trust. The copy
    carries the rain-day definition AND the fact that missing days break the
    run, because both are load-bearing for the claim."""
    _seed(engine, _rows())
    s = _one()
    assert (s["family"], s["story_type"]) == ("climate", "dry_spell")
    assert s["title"] == "Dry Spell" and s["emoji"] == "🏜️"
    assert s["id"] == "climate.dry_spell.2026-04-05"
    assert s["context"] == (
        "April 5 2026 through August 30 2026: 148 straight days on which "
        "this station measured less than 0.01 in of rain, the smallest "
        "amount a tipping bucket records. Every one of them was measured. "
        "A day the station missed breaks the run.")

    from app.insights import RAIN_DAY_MIN_IN
    assert f"{RAIN_DAY_MIN_IN:.2f} in" in s["context"]
    assert s["hero"]["unit"] == "days"
    assert s["period"] == {"kind": "spell", "label": "April 5 2026 to "
                           "August 30 2026", "start": "2026-04-05",
                           "end": "2026-08-30", "partial": True}


def test_the_year_bars_and_the_hero_never_disagree(engine):
    """The hero is the biggest bar, by construction — the record is picked
    from the same windowed per-year measurement the chart draws, so a card
    cannot headline a number its own chart does not contain."""
    _seed(engine, _rows())
    s = _one()
    bars = {b["year"]: b for b in s["viz"]["series"]}
    assert s["viz"]["kind"] == "dry_spell_years"
    assert sorted(bars) == [2024, 2025, 2026]
    assert bars[2026]["days"] == s["hero"]["value"]
    assert bars[2026]["hero"] is True and bars[2026]["ongoing"] is True
    assert s["viz"]["highlight"] == 2026
    assert all(b["comparable"] for b in bars.values())
    # Every year measured through the same date, so the axis says so.
    assert s["viz"]["axis_label"].endswith("through Aug 30")
    # 2024 and 2025 each ran ~111 days dry by Aug 30 — real bars, not zeros.
    assert bars[2024]["days"] == 111 and bars[2025]["days"] == 112


# ─────────────────── the chart's own words ───────────────────

def test_every_bar_carries_its_dates_as_copy_not_only_as_iso(engine):
    """A row that shipped raw `start`/`end` and nothing else handed the
    template a choice it is not allowed to make: formatting a date range is
    composing, and composing is the client's forbidden move. The ISO pair
    stays as DATA; `range_label` is the copy beside it."""
    _seed(engine, _rows())
    bars = {b["year"]: b for b in _one()["viz"]["series"]}
    assert bars[2026]["range_label"] == "Apr 5 – Aug 30 2026"
    assert bars[2026]["start"] == "2026-04-05"
    assert bars[2026]["end"] == "2026-08-30"
    # A run that crosses New Year names both years; one that does not, one.
    from datetime import date as _d
    assert stories._range_label(_d(2025, 12, 12), _d(2026, 3, 3)) == \
        "Dec 12 2025 – Mar 3 2026"


def test_a_running_bar_says_so_in_words_rather_than_a_glyph(engine):
    """The card could only mark a live spell with a symbol, and a symbol is
    a word the client invented. The producer says it instead."""
    _seed(engine, _rows())
    bars = {b["year"]: b for b in _one()["viz"]["series"]}
    assert bars[2026]["note"] == "still running"
    assert bars[2024]["note"] is None and bars[2025]["note"] is None


def test_a_faded_bar_gets_a_sentence_explaining_the_fade(client, monkeypatch):
    """THE gap this closes. The chart greys a year that could not be
    compared and, until now, the finished IMAGE said nothing about why —
    a shareable picture drawing a distinction the viewer cannot interpret.
    The explanation is written here because the card may not write it."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)

    _seed(db, _rows())
    # Every year comparable: no faded bar, so no legend. An unconditional
    # footnote would be noise on the cards that do not need it.
    assert _one()["viz"]["footnote"] is None

    _seed(db, _rows(start=date(2025, 7, 1)))
    s = _one()
    bars = {b["year"]: b for b in s["viz"]["series"]}
    assert bars[2025]["comparable"] is False
    assert s["viz"]["footnote"] == stories.INCOMPARABLE_FOOTNOTE
    assert "compared" in s["viz"]["footnote"]


def test_the_comparison_ranks_against_prior_years(engine):
    _seed(engine, _rows())
    c = _one()["comparison"]
    assert c["kind"] == "prior_years_to_date"
    assert c["label"] == "vs the longest run in two earlier years"
    assert (c["value"], c["baseline"]) == (148, 111.5)
    assert c["baseline_label"] == "2024–2025 average through Aug 30"
    assert (c["rank"], c["of"], c["direction"]) == (1, 3, "above")


# ─────────────────── live spell vs record spell ───────────────────

def test_a_live_spell_leads_and_says_so(engine):
    _seed(engine, _rows())
    s = _one()
    assert s["hero_line"].endswith("AND COUNTING. NOT A DROP.")
    assert s["period"]["partial"] is True
    record = next(x for x in s["supporting"] if x["key"] == "record_spell")
    assert record["value"] == 148
    assert record["label"] == "and it is the station's longest run"


def test_a_short_live_spell_yields_the_hero_slot_to_the_record(engine):
    """The record led for years; a four-day dry patch does not take the
    headline off it. The live run still ships, as a supporting stat."""
    rain = dict(_RAIN_DOY)
    # Rain on Aug 26 2026 (doy 238), so only four dry days remain.
    _RAIN_DOY[2026] = (*rain[2026], 238)
    try:
        _seed(engine, _rows())
        s = _one()
    finally:
        _RAIN_DOY[2026] = rain[2026]
    assert s["hero_line"] == "143 DAYS. NOT A DROP."
    assert s["period"]["partial"] is False
    current = next(x for x in s["supporting"] if x["key"] == "current_spell")
    assert current["value"] == 4
    # The rain that ended the hero spell is named, with its amount.
    broke = next(x for x in s["supporting"] if x["key"] == "broke_amount")
    assert broke["value"] == pytest.approx(RAIN_IN)
    assert broke["label"].endswith("2026-08-26")


def test_a_station_that_went_dark_is_not_still_counting(client, monkeypatch):
    """"And counting" is a claim about right now. The same rollups, read
    three weeks later, must lose the phrase — the station stopped reporting
    and nobody knows whether it has rained since."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows())
    assert "AND COUNTING" in _one()["hero_line"]

    monkeypatch.setattr(climate, "local_today", lambda: date(2026, 9, 20))
    stale = _one()
    assert stale["hero_line"] == "148 DAYS. NOT A DROP."
    assert stale["period"]["partial"] is False
    assert "record_spell" not in {x["key"] for x in stale["supporting"]}


# ─────────────────── prior-year comparability ───────────────────

def test_a_thin_prior_year_stays_out_of_the_baseline(client, monkeypatch):
    """A year the station joined in July has a short longest-run because it
    has few days. Letting it into the baseline manufactures a record — the
    same failure `insights.comparable_to_date` exists to prevent, and the
    same one definition decides it here."""
    from app import climate, db, insights
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    # 2025 starts on July 1: sixty-one days inside the Jan 1 → Aug 30
    # window every year is measured over, against 2026's two hundred and
    # forty-two. Real data, and nowhere near comparable.
    _seed(db, _rows(start=date(2025, 7, 1)))

    s = _one()
    assert s["comparison"] is None, "an uncovered year became a baseline"
    assert "standout" not in s["score_parts"]
    bars = {b["year"]: b for b in s["viz"]["series"]}
    # The year is still DRAWN — it is real data — but flagged so the client
    # can grey it rather than reading it as an equal.
    assert bars[2025]["comparable"] is False
    assert bars[2026]["comparable"] is True
    assert insights.comparable_to_date(61, 242) is False


# ───────────────────────── decline paths ─────────────────────────

def test_declines_when_the_station_has_no_rain_gauge(engine):
    """Every rain reading NULL. That is not one enormous dry spell — it is
    no dry spells at all, because no day was ever measured dry."""
    rows = [{**r, "rain": None} for r in _rows()]
    _seed(engine, rows)
    out = _dry()
    assert out["stories"] == [] and "dry_spell" in out["declined"]


def test_declines_with_too_little_history(engine):
    _seed(engine, _rows(start=date(2026, 8, 15)))
    out = _dry()
    assert out["stories"] == [] and "dry_spell" in out["declined"]


def test_declines_when_there_is_no_distribution_of_spells(engine):
    """Under MIN_SPELL_POOL runs there is nothing to rank the hero against,
    so the producer cannot say whether the run is remarkable HERE — which is
    the only claim the card makes."""
    rows = _rows(start=date(2025, 1, 1))
    # Rain on every day but two isolated stretches: two spells, not three.
    rows = [{**r, "rain": (0.0 if r["day"][5:] in {"03-01", "03-02", "07-04",
                                                   "07-05"} else RAIN_IN)}
            for r in rows]
    _seed(engine, rows)
    out = _dry()
    assert out["stories"] == [] and "dry_spell" in out["declined"]


def test_declines_when_the_longest_run_is_not_a_sentence(engine):
    """A copy floor, deliberately absolute and deliberately small: "three
    days. Not a drop." is not something anybody shares, in any climate."""
    rows = _rows(start=date(2025, 1, 1))
    rows = [{**r, "rain": (0.0 if (i % 4) else RAIN_IN)}
            for i, r in enumerate(rows)]
    _seed(engine, rows)
    out = _dry()
    assert out["stories"] == [] and "dry_spell" in out["declined"]
    assert stories.MIN_SPELL_DAYS == 5


# ───────────────────────── determinism ─────────────────────────

def test_the_pinned_today_seam_fixes_the_whole_story(engine):
    _seed(engine, _rows())
    first, again = _one(), _one()
    assert first == again


def test_the_score_is_station_relative_not_a_day_count(engine):
    """A wet climate's longest gap and a desert's are the same STORY only if
    they mean the same thing where they happened. Nothing here compares a
    run against an absolute number of days: the score is how far the run
    towers over an ordinary run at that station, and where it sits in that
    station's own distribution of runs."""
    _seed(engine, _rows())
    drought = _one()
    assert drought["score_parts"]["dominance"] > 0.85
    assert drought["score_parts"]["standout"] == 1.0

    # A station that rains every few days: same producer, much lower score.
    rows = _rows(start=date(2025, 1, 1))
    rows = [{**r, "rain": (0.0 if (i % 7) else RAIN_IN)}
            for i, r in enumerate(rows)]
    _seed(engine, rows, mac="AA:BB:CC:00:00:99")
    wet = _one("AA:BB:CC:00:00:99")
    assert wet["hero"]["value"] == 6
    assert wet["interestingness"] < drought["interestingness"]
    assert wet["score_parts"]["dominance"] < 0.2


# ───────────────── cross-producer calibration ─────────────────

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_a_real_drought_outranks_the_heat_ledger(engine):
    """`interestingness` is documented as comparable ACROSS producers, so
    the four have to land on one 0..1 scale and swap places on merit. A
    148-day rainless run is the story on this station."""
    _seed(engine, _rows())
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert ranked["stories"][0]["story_type"] == "dry_spell"
    assert _best_of(ranked, "dry_spell") > _best_of(ranked, "heat_ledger")
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


def test_an_ordinary_dry_patch_yields_to_the_heat_ledger(engine):
    """The direction that makes it calibration rather than a thumb on the
    scale. Rain every seventh day on a hot station: the dry spell still
    produces a story and must rank BELOW the ledger it beat a moment ago."""
    rows = _rows(start=date(2025, 1, 1))
    rows = [{**r, "rain": (0.0 if (i % 7) else RAIN_IN)}
            for i, r in enumerate(rows)]
    _seed(engine, rows)
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert _best_of(ranked, "dry_spell") < _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] != "dry_spell"


# ───────────────────────── the endpoint ─────────────────────────

def test_endpoint_serves_the_dry_spell(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows())
    r = client.get(f"/api/devices/{MAC}/stories?family=climate&limit=12",
                   headers=H)
    assert r.status_code == 200
    body = r.json()
    hero = next(s for s in body["stories"] if s["story_type"] == "dry_spell")
    assert hero["hero_line"] == "148 DAYS AND COUNTING. NOT A DROP."
    assert ("climate", "dry_spell") in stories.registered()


def test_a_finished_year_frame_re_decides_comparability_over_whole_years(
        client, monkeypatch):
    """The bars compare WHOLE years once the newest year is finished
    (`cutoff_for` returns Dec 31), so comparability has to be decided in
    that same frame. `insights.assemble` publishes its flag against the
    TO-DATE window (Jan 1 → today's month-day): a year covered Jan–Aug and
    dark Sep–Dec passes that test and used to enter the whole-year baseline
    with a longest run that never had the chance to span its unmeasured
    autumn. Same rows, two frames, two honest answers."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    dark_2023 = frozenset(
        (date(2023, 9, 1) + timedelta(days=i)).isoformat()
        for i in range((date(2023, 12, 31) - date(2023, 9, 1)).days + 1))

    # Running-year frame: 2023 covered all 242 days of the Jan 1 → Aug 30
    # window, so it IS comparable, and the published flag agrees.
    _seed(db, _rows(start=date(2023, 1, 1), missing=dark_2023))
    s = _one()
    assert s["period"]["partial"] is True
    bars = {b["year"]: b for b in s["viz"]["series"]}
    assert bars[2023]["comparable"] is True

    # Finished-year frame: the newest year is 2025 and every bar is a
    # whole year. 2023 measured 243 of them; against 2025's 365 that is
    # nowhere near comparable — even though the to-date flag, measured
    # through Aug 30, still says it is.
    _seed(db, _rows(start=date(2023, 1, 1), end=date(2025, 12, 31),
                    missing=dark_2023))
    payload = asyncio.run(stories.build_context(MAC)).insights()
    payload = asyncio.run(payload)
    flags = {int(y["year"]): y.get("comparable_to_date") for y in payload["years"]}
    assert flags[2023] is True, "the to-date flag is not what changed"
    s = _one()
    assert s["period"]["partial"] is False
    bars = {b["year"]: b for b in s["viz"]["series"]}
    assert bars[2023]["comparable"] is False
    assert bars[2024]["comparable"] is True
    assert s["comparison"]["of"] == 2, "2023 must not be in the baseline"
    assert s["viz"]["footnote"] == stories.INCOMPARABLE_FOOTNOTE


def test_the_window_label_does_not_follow_the_process_locale(engine):
    """"through Aug 30" ships onto a share card. `%b` is LOCALE-dependent
    and would print "août" under a French LC_TIME, so the month must come
    from the engine's own spelling, never strftime."""
    import locale
    try:
        locale.setlocale(locale.LC_TIME, "fr_FR.UTF-8")
    except locale.Error:
        pytest.skip("fr_FR.UTF-8 is not installed on this machine")
    try:
        assert f"{TODAY:%b}" != "Aug", "the locale did not take; test is moot"
        _seed(engine, _rows())
        s = _one()
        assert s["comparison"]["baseline_label"].endswith("through Aug 30")
        assert s["viz"]["axis_label"].endswith("through Aug 30")
    finally:
        locale.setlocale(locale.LC_TIME, "C")
