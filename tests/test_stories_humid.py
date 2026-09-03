"""Story engine (2.0): the humidity / storm month family — one producer,
two renderings (the month's character, and the dew-point band timeline).

The rule this suite exists to hold down is the NAMING one. "Monsoon" is a
Chandler word, and a card that used it for a station in Ohio would be a
weather app inventing a season. The producer earns the word from the
station's own distribution — the month is humid, the rest of the record is
not, and rain actually fell — so the same code writes "Monsoon Meter" for a
desert July and "Humidity & Rain" for a month that has not earned it.

The other rule is the standing one: dew point is a SENSOR. A station
without one has not had a dry month, it has had no reading, and the
producer declines rather than rendering a histogram of zeros.

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
FIRST = date(2025, 1, 1)

# A desert year in dew points (°F): bone dry for nine months, then the wet
# season arrives. These are the DAY'S PEAK dew point, which is what places a
# day in a band — the muggiest hour is the one people remember.
_DEW_BY_MONTH = {1: 30.0, 2: 32.0, 3: 34.0, 4: 33.0, 5: 32.0, 6: 40.0,
                 7: 62.0, 8: 66.0, 9: 55.0, 10: 45.0, 11: 38.0, 12: 32.0}

# A seven-long wobble, so no two days are identical and no week is. Fixed,
# not random: every number in this file must be reproducible.
_WOBBLE = (9.0, -5.0, 4.0, 7.0, -3.0, 2.0, 6.0)

# Days of August 2026 that recorded rain — a monsoon month has storms, and
# the flavour rule counts them.
_RAIN_DAYS_AUG = (3, 4, 11, 17, 18, 19, 24, 27)
STORM_IN = 0.42


def _rows(start: date = FIRST, end: date = TODAY, *, shift: float = 0.0,
          flat: float | None = None, dew: bool = True,
          rain: bool = True) -> list[dict]:
    """One rollup row per calendar day.

    `shift` moves every dew point (a drier or wetter version of the same
    station); `flat` replaces the seasonal curve with one value all year —
    a place that is muggy every month of its life. `dew=False` leaves the
    column NULL, which is how "no dew-point sensor" is spelled.
    """
    rows: list[dict] = []
    d = start
    i = 0
    while d <= end:
        base = flat if flat is not None else _DEW_BY_MONTH[d.month]
        value = base + _WOBBLE[i % len(_WOBBLE)] + shift
        wet = (rain and d.year == 2026 and d.month == 8
               and d.day in _RAIN_DAYS_AUG)
        rows.append({
            "day": d.isoformat(),
            "lo": 72.0, "hi": 104.0,
            "dew": round(value, 1) if dew else None,
            "rain": (STORM_IN if wet else 0.0) if rain else None,
        })
        d += timedelta(days=1)
        i += 1
    return rows


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    """Rollups replaced, not merged — several tests seed the same station
    twice to show the same month under a different sensor set."""
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE mac = ?",
                               (mac,))
            for r in rows:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n, "
                    " dew_point_min, dew_point_max, rain_total) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (mac, r["day"], r.get("lo"), r.get("hi"), r.get("hi"), 1,
                     r.get("dew"), r.get("dew"), r.get("rain")))
            await conn.commit()
    asyncio.run(run())


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _air(mac: str = MAC) -> dict[str, dict]:
    """This family's two stories, keyed by story type."""
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_CLIMATE], limit=12))
    return {s["story_type"]: s for s in out["stories"]
            if s["story_type"] in {"humid_month", "dew_point_bands"}}


def _declined(mac: str = MAC) -> list[str]:
    return asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_CLIMATE], limit=12))["declined"]


# ───────────────────── the naming rule ─────────────────────

def test_a_desert_wet_season_earns_the_monsoon_title(engine):
    """All three conditions met at once: August is humid, the other 550-odd
    days of this station's record are not, and rain actually fell. THAT is
    what the word means — a contrast, not a latitude."""
    _seed(engine, _rows())
    s = _air()["humid_month"]
    assert s["title"] == "Monsoon Meter" and s["emoji"] == "🌩️"
    assert _air()["dew_point_bands"]["title"] == "Humidity Invasion"
    assert s["score_parts"]["flavour_monsoon"] == 1.0


def test_the_same_month_on_a_drier_station_gets_a_neutral_title(engine):
    """Every dew point twenty degrees lower and nothing else changed. The
    card still ships — the month is still the month — and it contains no
    monsoon vocabulary anywhere."""
    _seed(engine, _rows(shift=-20.0))
    cards = _air()
    assert cards["humid_month"]["title"] == "Humidity & Rain"
    assert cards["dew_point_bands"]["title"] == "How Humid Was It?"
    assert cards["humid_month"]["score_parts"]["flavour_monsoon"] == 0.0

    blob = " ".join(
        part
        for c in cards.values()
        for part in [c["title"], c["hero_line"], c["context"],
                     *(x["label"] for x in c["supporting"])])
    assert "monsoon" not in blob.lower()
    assert "sticky" not in blob.lower()


def test_a_station_humid_all_year_has_no_invasion_to_report(engine):
    """The other side of the contrast. Dew points in the seventies every
    month: the month IS sticky, so the card says so, but nothing arrived —
    it never left. Monsoon language would be a lie about the seasons."""
    _seed(engine, _rows(flat=68.0))
    cards = _air()
    assert cards["humid_month"]["title"] == "The Sticky Season"
    assert cards["dew_point_bands"]["title"] == "Days You Felt It"
    assert cards["humid_month"]["score_parts"]["flavour_monsoon"] == 0.0
    blob = " ".join(c["title"] + c["context"] for c in cards.values())
    assert "monsoon" not in blob.lower()


def test_the_flavour_rule_is_three_measured_conditions(engine):
    """The rule on its own, each condition switched independently. No
    latitude, no region, no station list — three numbers the station
    measured about itself."""
    assert engine is not None
    f = stories._flavour
    assert f(0.70, 0.05, 8) == "monsoon"
    # The month is humid but the station is humid too: no invasion.
    assert f(0.70, 0.60, 8) == "sticky"
    # The month is humid and dry-station, but nothing fell: muggy, not a
    # monsoon. A monsoon is weather, not discomfort.
    assert f(0.70, 0.05, 1) == "sticky"
    # No rain sensor at all — the same answer, for the same reason.
    assert f(0.70, 0.05, None) == "sticky"
    # The month simply is not humid.
    assert f(0.10, 0.05, 8) == "neutral"
    # Not enough history to know what this station's normal is, so no
    # contrast may be claimed.
    assert f(0.70, None, 8) == "sticky"


def test_the_monsoon_title_needs_rain_that_was_actually_measured(engine):
    """Same dew points, rain column NULL. Absent is not zero — the producer
    cannot see storms, so it does not claim a monsoon, and it renders no
    rain figures at all rather than a confident 0.00 in."""
    _seed(engine, _rows(rain=False))
    s = _air()["humid_month"]
    assert s["title"] == "The Sticky Season"
    keys = {x["key"] for x in s["supporting"]}
    assert "rain_total" not in keys and "rain_days" not in keys
    assert "0.00 in" not in s["context"]
    assert "wetness" not in s["score_parts"]


# ───────────────────── the character card ─────────────────────

def test_the_character_card_leads_with_the_peak_dew_point(engine):
    _seed(engine, _rows())
    s = _air()["humid_month"]
    assert (s["family"], s["story_type"]) == ("climate", "humid_month")
    assert s["id"] == "climate.humid_month.2026-08"
    assert s["hero"]["key"] == "peak_dew_point"
    # The hero STAT and the hero LINE are one claim about one reading, so
    # they carry the same precision. They used to disagree: the card printed
    # "DEW POINT PEAKED AT 74°F" across the top and "74.0°F" in the stat
    # beneath it, because `_sig` had earned no decimal while the stat was
    # pinned at one (card-template field report, 2026-08-30).
    assert s["hero"]["unit"] == "F"
    assert s["hero_line"] == (
        f"DEW POINT PEAKED AT "
        f"{s['hero']['value']:.{s['hero']['precision']}f}°F")
    assert s["hero"]["precision"] == stories._sig_precision(s["hero"]["value"])
    # This fixture peaks on a whole degree, which is the case that broke.
    assert s["hero"]["precision"] == 0
    # A fractional peak still earns its decimal, in both places at once.
    assert stories._sig_precision(74.3) == 1
    assert stories.UNITS_NATIVE.temp_deg(74.3) == "74.3°F"
    assert s["period"] == {"kind": "month", "label": "August 2026 so far",
                           "start": "2026-08-01", "end": "2026-08-30",
                           "partial": True}

    stats = {x["key"]: x for x in s["supporting"]}
    assert stats["rain_total"]["unit"] == "in"
    assert stats["rain_days"]["value"] == len(_RAIN_DAYS_AUG)
    assert stats["rain_total"]["value"] == pytest.approx(
        round(len(_RAIN_DAYS_AUG) * STORM_IN, 2))
    assert stats["wettest_day"]["unit"] == "in"
    # Every stat leaves in storage units; the client converts, and nothing
    # in the engine compares a threshold against a converted value.
    assert {x["unit"] for x in s["supporting"]} <= {"F", "in", "days", None}

    # The Monsoon Meter itself: one point per day, dew line plus rain bars.
    assert s["viz"]["kind"] == "humidity_month"
    assert len(s["viz"]["series"]) == 30
    # The highlight names the day the hero came from, so the card can mark
    # the same point the headline is about.
    peak = next(p for p in s["viz"]["series"]
                if p["day"] == s["viz"]["highlight"])
    assert peak["dew_point"] == s["hero"]["value"]
    wet = [p for p in s["viz"]["series"] if p["rain"]]
    assert len(wet) == len(_RAIN_DAYS_AUG)
    assert all(p["band"] is not None for p in s["viz"]["series"])


def test_a_day_the_station_measured_nothing_is_a_break_not_a_zero(engine):
    """A missing dew point draws a gap in the line. Rendering it as 0°F
    would put the driest air ever recorded on Earth into an August chart."""
    rows = _rows()
    for r in rows:
        if r["day"] in {"2026-08-08", "2026-08-09"}:
            r["dew"] = None
            r["rain"] = None
    _seed(engine, rows)
    s = _air()["humid_month"]
    points = {p["day"]: p for p in s["viz"]["series"]}
    assert points["2026-08-08"]["dew_point"] is None
    assert points["2026-08-08"]["band"] is None
    assert points["2026-08-08"]["rain"] is None
    # And the two days are not counted as days that were measured.
    measured = next(x for x in s["supporting"]
                    if x["key"] == "dew_days_measured")
    assert measured["value"] == 28

    # A break in the line is a visual distinction with no words on it, and
    # the wrong reading of one is the worst reading available: a gap looks
    # like a plunge to bone-dry air. The producer explains it, because the
    # card is forbidden from writing that sentence itself.
    assert s["viz"]["footnote"] == (
        "Gaps in the line are days this station measured no dew point, "
        "not dry days.")


def test_an_unbroken_line_carries_no_footnote(engine):
    """The legend appears only when there is a gap to explain."""
    _seed(engine, _rows())
    assert _air()["humid_month"]["viz"]["footnote"] is None


def test_the_per_day_band_is_a_shading_spec_the_bands_card_shares(engine):
    """`band` stays on the Monsoon Meter and this is what it is for: the
    dew-point line is continuous, so the comfort bands cannot be read off
    it, and a template shades each day's column by its band name instead.
    The names are the SAME stable strings the band-timeline card uses, so
    one palette serves both and neither template decides where a band
    begins."""
    _seed(engine, _rows())
    out = _air()
    line_bands = {p["band"] for p in out["humid_month"]["viz"]["series"]
                  if p["band"]}
    ladder_bands = {b["band"] for b in out["dew_point_bands"]["viz"]["series"]}
    assert line_bands and line_bands <= ladder_bands
    # Band membership is decided on the stored Fahrenheit reading, so the
    # shading a template draws is identical in every unit system.
    metric = stories.Units(temperature="celsius")
    other = asyncio.run(stories.top_stories(
        MAC, families=[stories.FAMILY_CLIMATE], limit=12, units=metric))
    mm = next(s for s in other["stories"] if s["story_type"] == "humid_month")
    assert [p["band"] for p in mm["viz"]["series"]] == \
        [p["band"] for p in out["humid_month"]["viz"]["series"]]


# ───────────────────── the band timeline ─────────────────────

def test_the_band_card_counts_days_by_their_peak(engine):
    _seed(engine, _rows())
    s = _air()["dew_point_bands"]
    assert s["story_type"] == "dew_point_bands"
    assert s["id"] == "climate.dew_bands.2026-08"
    names = [b["band"] for b in s["viz"]["series"]]
    assert names == ["very dry", "dry", "noticeable", "humid", "very humid"]
    assert sum(b["days"] for b in s["viz"]["series"]) == 30
    assert sum(b["share"] for b in s["viz"]["series"]) == pytest.approx(1.0)

    # Every band carries its own °F range: a band drawn without its numbers
    # is a mood, and the reader cannot place their own day in it.
    labels = [b["label"] for b in s["viz"]["series"]]
    assert labels[0] == "very dry · under 50°F"
    assert labels[2] == "noticeable · 60–65°F"
    assert labels[4] == "very humid · 70°F and up"

    # Hero: the highest band holding a real number of days, not a single
    # sticky afternoon.
    hero = next(b for b in s["viz"]["series"] if b["band"] == s["viz"]["highlight"])
    assert hero["days"] >= stories.BAND_HERO_MIN
    assert s["hero_line"] == f"{hero['days']} {hero['band'].upper()} DAYS"
    assert "PEAK" in s["context"].upper()


def test_the_band_edges_are_the_documented_comfort_scale():
    """Absolute °F, unlike every threshold in the Wildest Day scorer, and
    deliberately: a 70°F dew point is oppressive in Chandler and in
    Charleston alike. Human comfort does not renormalize per station."""
    assert stories._dew_band(49.9) == "very dry"
    assert stories._dew_band(50.0) == "dry"
    assert stories._dew_band(59.9) == "dry"
    assert stories._dew_band(60.0) == "noticeable"
    assert stories._dew_band(64.9) == "noticeable"
    assert stories._dew_band(65.0) == "humid"
    assert stories._dew_band(69.9) == "humid"
    assert stories._dew_band(70.0) == "very humid"
    assert stories._dew_band(95.0) == "very humid"
    assert stories.HUMID_DEW_F == 65.0


def test_the_second_rendering_ranks_below_the_first(engine):
    """Two cards about one month. Both ship — the app fetches each by story
    type — but Worth Sharing must not open with the same August twice, so
    the timeline rides below its own character card."""
    _seed(engine, _rows())
    cards = _air()
    # Its own key: this is a second RENDERING of one month, not a month
    # inside its year, and the score part says which.
    assert "redundant_scope" not in cards["dew_point_bands"]["score_parts"]
    assert cards["dew_point_bands"]["score_parts"]["redundant_rendering"] == (
        pytest.approx(stories.REDUNDANT_SCOPE))


# ───────────────────────── decline paths ─────────────────────────

def test_declines_when_the_station_has_no_dew_point_sensor(engine):
    """The one that matters. A station with no dew-point sensor has not had
    a very dry month — it has had no reading, and a band histogram of
    thirty "very dry" days would be a fabrication."""
    _seed(engine, _rows(dew=False))
    assert _air() == {}
    assert "humid_month" in _declined()


def test_declines_when_the_month_is_too_thin_to_describe(engine):
    """A band histogram over four days is a rumour. The station is fine and
    the month is real; there is simply not enough of it yet."""
    rows = _rows()
    for r in rows:
        if r["day"].startswith("2026-08") and r["day"] > "2026-08-04":
            r["dew"] = None
    _seed(engine, rows)
    assert _air() == {}
    assert "humid_month" in _declined()
    assert stories.MIN_BAND_DAYS == 10


def test_declines_with_too_little_history(engine):
    _seed(engine, _rows(start=date(2026, 8, 15)))
    assert _air() == {}
    assert "humid_month" in _declined()


def test_the_newest_month_with_data_wins_not_the_calendar_s(client, monkeypatch):
    """A station that stopped reporting still has a real story about the
    last month it measured. Inventing an empty "this month" would be the
    zero bug in another costume."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: date(2027, 3, 4))
    _seed(db, _rows())
    s = _air()["humid_month"]
    assert s["period"]["label"] == "August 2026"
    assert s["period"]["partial"] is False
    assert s["period"]["end"] == "2026-08-31"


# ───────────────────────── determinism ─────────────────────────

def test_the_pinned_today_seam_fixes_the_whole_story(engine):
    _seed(engine, _rows())
    assert _air() == _air()


# ───────────────── cross-producer calibration ─────────────────

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_a_wet_season_month_outranks_the_heat_ledger(engine):
    """`interestingness` is comparable ACROSS producers, so all four land on
    one 0..1 scale and swap places on merit."""
    _seed(engine, _rows())
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert _best_of(ranked, "humid_month") > _best_of(ranked, "heat_ledger")
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


def test_an_ordinary_dry_month_yields_to_the_heat_ledger(engine):
    """The direction that makes it calibration rather than a thumb on the
    scale. A station that is bone dry every month of the year, August
    included: the card still ships — it is still the month — and it must
    rank BELOW the same heat ledger the wet season beat a moment ago.

    This is exactly what the absolute band ladder buys. Station-relative
    axes alone would have crowned this month too, because it is still the
    most humid one this station has."""
    _seed(engine, _rows(flat=32.0, rain=False))
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert _best_of(ranked, "humid_month") < _best_of(ranked, "heat_ledger")
    assert _best_of(ranked, "humid_month") > 0.0, "it still tells its story"
    parts = next(s for s in ranked["stories"]
                 if s["story_type"] == "humid_month")["score_parts"]
    assert parts["reach"] == pytest.approx(0.2)     # the very-dry rung
    assert parts["saturation"] == 0.0


# ───────────────────────── the endpoint ─────────────────────────

def test_endpoint_serves_the_humidity_family(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows())
    r = client.get(f"/api/devices/{MAC}/stories?family=climate&limit=12",
                   headers=H)
    assert r.status_code == 200
    types = [s["story_type"] for s in r.json()["stories"]]
    assert "humid_month" in types and "dew_point_bands" in types
    assert ("climate", "humid_month") in stories.registered()
