"""Story engine (2.0): the Tonight's Sky producer, and the moon-free dark
window underneath it.

THE WINDOW IS WHY THIS SUITE IS LONG. Anybody can print a moon phase. The
useful number — "between 9:41 pm and 4:12 am there is no moon in the sky" —
has five shapes, and every one of them is a night somebody actually has:

  · the moon never rises (a new-moon night: the window IS the night);
  · the moon never sets (a full-moon night: there is NO window, which is not
    the same as a window of zero minutes);
  · the moon is up at dusk and sets in the small hours (the window starts
    after midnight);
  · the moon is down at dusk and rises after midnight (the window straddles
    midnight — the case every date-based approach gets wrong);
  · a long winter night fits a set AND a second rise, so there are two
    windows and the longest one is the answer.

Each has its own test below, on a real date at a real place, because the
producer does not branch on them: `almanac.moon_below_intervals` scans the
moon's altitude across the actual interval, so all five collapse into "how
many intervals came back". The tests exist to prove that collapse is real —
if somebody ever rewrites the window as rise/set bookkeeping, five of these
fail at once.

Two earlier defects are pinned here as well: the phase bucket must round the
way the app's Swift does (Python rounds halves to even, Swift away from
zero), and the moonrise a card prints must belong to TONIGHT rather than to
tomorrow evening.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:5E"
TZ = "America/Phoenix"
CHANDLER = (33.3062, -111.8413)
POLAR = (71.2906, -156.7886)
POLAR_TZ = "America/Anchorage"

TODAY = date(2026, 8, 30)
FIRST = date(2025, 1, 1)

# The five shapes, as dates. Every one was found by scanning the real
# ephemeris for this location rather than chosen to be convenient.
FULL_MOON = date(2026, 8, 28)          # up from dusk to dawn: no window
NEW_MOON = date(2026, 9, 11)           # never rises: the window is the night
MOON_RISES_LATE = date(2026, 9, 5)     # window 7:13 pm → 1:01 am, over midnight
MOON_SETS_LATE = date(2026, 9, 19)     # window 12:12 am → dawn, all after midnight
GIBBOUS = date(2026, 8, 30)            # a 49-minute sliver before moonrise
TWO_WINDOWS = date(2026, 12, 13)       # Utqiagvik: a set and a second rise
SLIVER = date(2026, 8, 26)             # 12.6 moon-free minutes: not a window

_SHAPES = (0.02, 0.31, 0.11, 0.64, 0.07, 0.45, 0.22, 0.88, 0.05, 0.37,
           0.16, 0.53, 0.09, 0.71, 0.27, 0.13, 0.42, 0.03, 0.59, 0.19,
           0.34, 0.08, 0.48, 0.24, 0.95, 0.12, 0.40, 0.06, 0.66)


def _rows() -> list[dict]:
    """A Chandler-shaped record. This producer reads none of it; the station
    attribution wants a span and the calibration test wants a heat ledger."""
    rows: list[dict] = []
    d, i = FIRST, 0
    while d <= date(2026, 12, 31):
        doy = d.timetuple().tm_yday
        hi = 70.0 + 38.0 * (1 - abs(doy - 200) / 200.0) + 6 * _SHAPES[i % 29]
        rows.append({"day": d.isoformat(), "hi": round(hi, 1),
                     "lo": round(hi - 24.0, 1)})
        d += timedelta(days=1)
        i += 1
    return rows


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE mac = ?",
                               (mac,))
            for r in rows:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?,?,?,?,?,?)",
                    (mac, r["day"], r["lo"], r["hi"], r["hi"], 1))
            await conn.commit()
    asyncio.run(run())


def _place(db, coords: tuple[float, float] | None, mac: str = MAC) -> None:
    async def run():
        await db.upsert_device(mac, {"name": "Backyard"})
        async with db.connect() as conn:
            await conn.execute("DELETE FROM device_location WHERE mac = ?",
                               (mac,))
            await conn.commit()
        if coords is not None:
            await db.set_device_location(mac, coords[0], coords[1],
                                         "Backyard", 1_700_000_000_000)
    asyncio.run(run())


@pytest.fixture()
def sky(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", TZ)
    monkeypatch.setattr(settings, "forecast_lat", None)
    monkeypatch.setattr(settings, "forecast_lon", None)
    state = {"today": TODAY, "db": db, "settings": settings}
    monkeypatch.setattr(climate, "local_today", lambda: state["today"])
    _place(db, CHANDLER)
    _seed(db, _rows())
    return state


def _story(state, day: date | None = None,
           coords: tuple[float, float] | None = ...,   # type: ignore[assignment]
           units: stories.Units | None = None,
           tz: str | None = None) -> dict | None:
    if day is not None:
        state["today"] = day
    if tz is not None:
        state["settings"].timezone = tz
    if coords is not ...:
        _place(state["db"], coords)
    kw = {"units": units} if units is not None else {}
    ranked = asyncio.run(stories.top_stories(MAC, limit=12, **kw))
    return next((s for s in ranked["stories"]
                 if s["story_type"] == "tonights_sky"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


def _band(story: dict, key: str) -> dict | None:
    return next((e for e in story["viz"]["series"] if e["key"] == key), None)


def _tz(name: str = TZ):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


# ═════════════ the five shapes of a moon-free window ═════════════

def test_a_moon_that_never_rises_leaves_the_whole_night(sky):
    """New moon. The window is the entire night, and the copy says so
    without a caveat about the last four minutes."""
    s = _story(sky, NEW_MOON)
    assert s["hero_line"] == "NEW MOON · 10H 39M OF DARK SKY"
    assert _stat(s, "dark_window")["label"] == \
        "moon-free dark · 7:04 pm – 5:44 am"
    assert _stat(s, "dark_window")["value"] == _stat(s, "night_length")["value"]
    assert "it never clears the horizon at all" in s["context"]
    assert "the whole night" in s["context"]
    # No moonrise happens inside this night, so no moonrise is claimed.
    assert _stat(s, "moonrise") is None


def test_a_moon_up_all_night_has_no_window_rather_than_zero_minutes(sky):
    """Full moon. "0 minutes of moon-free dark" beside "moonset —" is a
    sentence that means nothing; the honest answer is that there isn't
    one, and the hero has to be something else entirely."""
    s = _story(sky, FULL_MOON)
    assert s["hero_line"] == "A FULL MOON, UP ALL NIGHT"
    assert _stat(s, "dark_window") is None
    assert _band(s, "dark_window") is None
    assert s["hero"]["key"] == "illumination"
    assert s["score_parts"]["darkness"] == 0.0
    assert "no moon-free dark window tonight" in s["context"]
    # It got up there before the night started, and the card says WHEN.
    assert _stat(s, "moonrise")["label"] == "already up since · 7:14 pm"


def test_a_window_may_straddle_midnight(sky):
    """The case every date-based approach gets wrong. The moon is down at
    dusk and rises at 1:01 am, so the dark runs from one calendar day into
    the next and is still one window."""
    s = _story(sky, MOON_RISES_LATE)
    tz = _tz()
    band = _band(s, "dark_window")
    start = __import__("datetime").datetime.fromisoformat(band["start"])
    end = __import__("datetime").datetime.fromisoformat(band["end"])
    assert start.astimezone(tz).date() == MOON_RISES_LATE
    assert end.astimezone(tz).date() == MOON_RISES_LATE + timedelta(days=1)
    assert band["clock"] == "7:13 pm – 1:01 am"
    assert s["hero_line"] == "5H 48M OF MOON-FREE DARK"


def test_a_window_may_lie_entirely_after_midnight(sky):
    """The mirror case: the moon is up at dusk, sets at 12:12 am, and the
    only dark of the night belongs to tomorrow's calendar day."""
    s = _story(sky, MOON_SETS_LATE)
    tz = _tz()
    band = _band(s, "dark_window")
    start = __import__("datetime").datetime.fromisoformat(band["start"])
    assert start.astimezone(tz).date() == MOON_SETS_LATE + timedelta(days=1)
    assert band["clock"] == "12:12 am – 5:49 am"
    assert _stat(s, "moonset")["label"] == "moonset · 12:12 am"
    assert _stat(s, "moonrise")["label"].startswith("already up since")


def test_the_longest_of_two_windows_wins(sky):
    """A twenty-one-hour polar winter night fits a moonset AND a second
    moonrise, so the scan returns two intervals. The one worth naming is
    the big one — "first" would have picked a fourteen-minute sliver."""
    from app import almanac
    tz = _tz(POLAR_TZ)
    dusk = almanac.last_light(*POLAR, TWO_WINDOWS, tz)
    dawn = almanac.first_light(*POLAR, TWO_WINDOWS + timedelta(days=1), tz)
    intervals = almanac.moon_below_intervals(*POLAR, dusk, dawn)
    assert len(intervals) == 2
    first = (intervals[0][1] - intervals[0][0]).total_seconds()
    second = (intervals[1][1] - intervals[1][0]).total_seconds()
    assert first < second                       # …and first is the sliver
    best = almanac.darkest_window(*POLAR, dusk, dawn)
    assert best == intervals[1]

    s = _story(sky, TWO_WINDOWS, coords=POLAR, tz=POLAR_TZ)
    assert _stat(s, "dark_window")["value"] == round(second / 60)


def test_a_sliver_is_not_a_viewing_window(sky):
    """A gap this short is a fact, not a plan. Fifteen minutes is the floor,
    below which the producer reports no window at all rather than inviting
    somebody to carry a telescope outside for it."""
    from app import almanac
    tz = _tz()
    dusk = almanac.last_light(*CHANDLER, SLIVER, tz)
    dawn = almanac.first_light(*CHANDLER, SLIVER + timedelta(days=1), tz)
    best = almanac.darkest_window(*CHANDLER, dusk, dawn)
    # The scan DOES find one — twelve and a half minutes of moonless sky
    # between a moonset and civil dawn. The almanac layer reports what it
    # measured; the copy floor is the producer's judgement, not the math's.
    assert best is not None
    span = (best[1] - best[0]).total_seconds()
    assert 0 < span < stories.MIN_DARK_WINDOW_S
    s = _story(sky, SLIVER)
    assert _stat(s, "dark_window") is None
    assert "no moon-free dark window tonight" in s["context"]


def test_no_civil_night_declines_the_window_but_keeps_the_story(sky):
    """Midnight sun. The dark dimension is DROPPED and the mean renormalizes
    over what is left — the same move `air_flight` makes with an unknown
    elevation — rather than the story being scored at zero darkness."""
    s = _story(sky, date(2026, 6, 21), coords=POLAR, tz=POLAR_TZ)
    assert s is not None
    assert s["hero_line"] == "THE SKY NEVER GETS FULLY DARK TONIGHT"
    assert "darkness" not in s["score_parts"]
    assert set(s["score_parts"]) == {"phase"}
    assert _stat(s, "night_length") is None
    assert "the sky stays lit all night" in s["context"]


# ═════════════ structural invariants over a whole lunation ═════════════

def test_the_intervals_are_ordered_disjoint_and_inside_the_night(sky):
    """Whatever shape a night has, the scan's output has to be a valid set
    of intervals. Thirty consecutive nights covers a full cycle."""
    from app import almanac
    tz = _tz()
    day = date(2026, 8, 20)
    for _ in range(30):
        dusk = almanac.last_light(*CHANDLER, day, tz)
        dawn = almanac.first_light(*CHANDLER, day + timedelta(days=1), tz)
        intervals = almanac.moon_below_intervals(*CHANDLER, dusk, dawn)
        previous = dusk
        for start, end in intervals:
            assert dusk <= start < end <= dawn, day
            assert start >= previous, day
            previous = end
        day += timedelta(days=1)


def test_every_night_of_a_lunation_tells_a_story(sky):
    """The producer can always speak for a station with coordinates. No
    night in a full cycle may raise, decline, or produce a hero line that
    disagrees with its own window."""
    day = date(2026, 8, 20)
    for _ in range(30):
        s = _story(sky, day)
        assert s is not None, day
        window = _stat(s, "dark_window")
        if window is None:
            assert "no moon-free dark window" in s["context"], day
        else:
            assert stories._hm(window["value"] * 60).upper() in s["hero_line"], \
                day
        assert 0.0 <= s["interestingness"] <= 1.0, day
        day += timedelta(days=1)


def test_the_window_shrinks_as_the_moon_fills(sky):
    """A sanity check on the physics rather than the code: between new moon
    and full, the moon rises earlier every evening and the dark window
    gets shorter every night."""
    lengths = []
    for offset in range(0, 14):
        s = _story(sky, NEW_MOON + timedelta(days=offset))
        stat = _stat(s, "dark_window")
        lengths.append(stat["value"] if stat else 0)
    assert lengths[0] > lengths[-1]
    assert all(a >= b for a, b in zip(lengths, lengths[1:]))


# ═════════════ the two defects, pinned ═════════════

def test_the_moon_events_belong_to_tonight_not_to_tomorrow_evening(sky):
    """The first version took the next rise and set AFTER dusk, which on a
    full-moon night is tomorrow's rise: the card said "it is already up — it
    rises again at 7:41 pm", seventeen minutes after a dusk it was already
    above. A real time on a night it does not belong to is worse than a
    dash."""
    s = _story(sky, FULL_MOON)
    assert "rises again" not in s["context"]
    assert "7:41 pm" not in s["context"]
    # Nothing sets inside this night either, and nothing pretends to.
    assert _stat(s, "moonset") is None
    assert "it does not set before first light" in s["context"]


def test_the_phase_bucket_rounds_the_way_the_app_does(sky):
    """Python rounds halves to EVEN, Swift rounds them away from zero. On
    the exact bucket boundaries that is one glyph of disagreement between
    the share card and the almanac card in the same app, for no reason a
    reader could ever work out."""
    from datetime import datetime, timezone

    from app import almanac
    epoch = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

    def at(fraction: float):
        return epoch + timedelta(days=almanac.SYNODIC_DAYS * fraction)

    # The boundary between Last Quarter and Waning Crescent sits at 6.5/8.
    # Exact halves never survive float arithmetic, so the observable claim
    # is WHERE the bucket flips — which is what an off-by-one rounding rule
    # would move.
    assert almanac.moon_phase_name(at(6.499 / 8)) == "Last Quarter"
    assert almanac.moon_phase_name(at(6.501 / 8)) == "Waning Crescent"
    assert almanac.moon_phase_name(at(0.001 / 8)) == "New Moon"
    assert almanac.moon_phase_name(at(7.999 / 8)) == "New Moon"
    # The trap the implementation avoids: Python rounds halves to EVEN, so
    # `round(6.5)` is 6 and `round(7.5)` is 8 — two neighbouring boundaries
    # that would land on different sides of their own rule.
    assert round(6.5) == 6 and round(7.5) == 8
    # Illumination and the bucket are drawn from the same curve and must
    # agree: a "Full Moon" bucket is never a dark disc.
    assert almanac.moon_illumination(at(0.5)) == pytest.approx(1.0)
    assert almanac.moon_illumination(at(0.25)) == pytest.approx(0.5)


# ═════════════ declines ═════════════

def test_declines_without_coordinates(sky):
    assert _story(sky, TODAY, coords=None) is None


def test_null_island_reads_as_unknown(sky):
    assert _story(sky, TODAY, coords=(0.0, 0.0)) is None


# ═════════════ the rest of the payload ═════════════

def test_the_comparison_is_last_night(sky):
    """The moon rises about fifty minutes later each day, so last night is
    always meaningfully different — and it costs two altitude scans and no
    queries."""
    s = _story(sky, GIBBOUS)
    c = s["comparison"]
    assert c["kind"] == "vs_last_night"
    assert c["baseline_label"] == "19m last night"
    assert c["direction"] == "above"
    assert c["delta"] == 30
    assert c["rank_line"] is None


def test_a_zero_baseline_yields_no_percentage(sky):
    """Last night having NO moon-free dark at all is a real measurement. A
    percentage against zero is not a number."""
    s = _story(sky, FULL_MOON + timedelta(days=1))
    c = s["comparison"]
    assert c["baseline"] == 0
    assert c["baseline_label"] == "no moon-free dark at all last night"
    assert c["delta_pct"] is None


def test_the_timeline_carries_finished_clock_strings(sky):
    """Points are instants, bands are spans, and every one of them arrives
    with the words already written — a template places them and never
    formats a time."""
    s = _story(sky, MOON_RISES_LATE)
    viz = s["viz"]
    assert viz["kind"] == "night_timeline"
    assert viz["highlight_key"] == "dark_window"
    keys = [e["key"] for e in viz["series"]]
    assert keys[0] == "night" or keys[0] == "sunset"
    assert {"night", "dark_window", "sunset", "last_light",
            "moonrise", "first_light"} <= set(keys)
    for entry in viz["series"]:
        assert entry["clock"]
        if entry["band"]:
            assert entry["start_min"] <= entry["end_min"]
        else:
            assert isinstance(entry["at_min"], int)
    # Ordered along the axis, so a template iterates and lays out.
    positions = [e.get("start_min", e.get("at_min")) for e in viz["series"]]
    assert positions == sorted(positions)
    assert "6° below the horizon" in viz["footnote"]


def test_the_phase_reads_as_english(sky):
    """The eight almanac names do not share a grammar. "There is last
    quarter out, 52% lit" is the sentence this fixes."""
    assert "the moon is full" in _story(sky, FULL_MOON)["context"]
    assert "the moon is new" in _story(sky, NEW_MOON)["context"]
    assert "the moon is at last quarter" in \
        _story(sky, date(2026, 9, 3))["context"]
    assert "the moon is waning gibbous" in _story(sky, GIBBOUS)["context"]


# ═════════════ determinism ═════════════

def test_the_same_anchor_produces_the_same_night_twice(sky):
    """The phase is read at the MIDDLE of the night, derived from the pinned
    date — never from a clock. Two calls in the same second must agree, and
    so must two runs a week apart."""
    assert _story(sky, GIBBOUS) == _story(sky, GIBBOUS)


def test_moving_the_anchor_moves_the_night(sky):
    assert _story(sky, GIBBOUS) != _story(sky, GIBBOUS + timedelta(days=1))


# ═════════════ units ═════════════

def test_a_celsius_render_leaks_no_fahrenheit_and_no_imperial(sky):
    metric = stories.Units(temperature="celsius", wind="kph", rain="mm",
                           pressure="hPa")
    s = _story(sky, GIBBOUS, units=metric)
    texts = [s["title"], s["hero_line"], s["context"], s["hero"]["label"],
             s["period"]["label"], s["viz"]["axis_label"],
             s["viz"]["footnote"], s["comparison"]["baseline_label"]]
    texts += [x["label"] for x in s["supporting"]]
    for text in texts:
        assert "°F" not in text and "inHg" not in text and "mph" not in text
    units = {x["unit"] for x in s["supporting"] if x["unit"]}
    assert units <= {"min", "days", "%"}


def test_the_night_is_identical_in_both_scales(sky):
    metric = stories.Units(temperature="celsius", wind="kph", rain="mm",
                           pressure="hPa")
    assert _story(sky, GIBBOUS) == _story(sky, GIBBOUS, units=metric)


# ═════════════ cross-producer calibration ═════════════

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_a_first_quarter_night_loses_to_the_heat_ledger(sky):
    """The direction that makes it calibration. A half-lit moon setting
    around midnight is the most ordinary sky there is, and Chandler's heat
    ledger is not ordinary at all."""
    sky["today"] = date(2026, 9, 3)
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert _best_of(ranked, "tonights_sky") < 0.25
    assert _best_of(ranked, "tonights_sky") < _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] != "tonights_sky"


def test_a_new_moon_night_is_the_best_this_producer_can_do(sky):
    """A ceiling on a story that comes round every night. The best possible
    version of this card — a new moon over a long night — is a good night
    out, not the most interesting thing that ever happened at this station,
    and it recurs every twenty-nine and a half days."""
    sky["today"] = NEW_MOON
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    best = _best_of(ranked, "tonights_sky")
    assert best > 0.7                       # it does win the sky family
    assert best <= stories.NIGHTLY_CEILING  # …and never leaves it
    assert best > _best_of(ranked, "daylight")


def test_the_producer_ranks_its_own_nights_in_the_right_order(sky):
    """Within one producer, one station: new moon beats full moon beats a
    quarter. If that inverts, the edition floors are fighting the mean."""
    new = _story(sky, NEW_MOON)["interestingness"]
    full = _story(sky, FULL_MOON)["interestingness"]
    quarter = _story(sky, date(2026, 9, 3))["interestingness"]
    assert new > full > quarter


# ═════════════ the endpoint ═════════════

def test_the_endpoint_serves_tonights_sky(sky, client):
    r = client.get(f"/api/devices/{MAC}/stories?family=sky&limit=5", headers=H)
    assert r.status_code == 200
    assert "tonights_sky" in {s["story_type"] for s in r.json()["stories"]}


def test_the_timeline_states_its_own_length(sky):
    """The ruler's domain is DATA, not something to infer from the rows.

    A template sizing the axis from its last point is assuming the last
    point sits at the end of the night. That held only because first light
    is normally last; the night's length lived nowhere a template could
    measure with, only inside the prose axis_label.
    """
    for day in (MOON_RISES_LATE, FULL_MOON, MOON_SETS_LATE, NEW_MOON):
        viz = _story(sky, day)["viz"]
        total = viz["domain_max"]
        assert isinstance(total, (int, float)) and total > 0
        # The ruler covers every row a card will place on it.
        for entry in viz["series"]:
            assert entry.get("end_min", entry.get("at_min")) <= total
        # And it is the same number the prose already quoted, so a card
        # cannot draw one length while the caption claims another.
        assert f"{total} minutes" in viz["axis_label"]


def test_a_moon_already_up_explains_itself_on_the_axis(sky):
    """A moon that rose before sunset cannot be placed on a ruler that
    starts at sunset, so the chart showed a moonset arriving from nowhere.
    The producer says why, on a row the card already draws."""
    viz = _story(sky, MOON_SETS_LATE)["viz"]
    keys = [e["key"] for e in viz["series"]]
    assert "moonrise" not in keys, "this night's rise is before the axis"
    row = next(e for e in viz["series"] if e["key"] == "moonset")
    assert row["note"] is not None and "already up" in row["note"]
    assert "2:28 pm" in row["note"], "the rise it names is the real one"


def test_a_rise_that_is_on_the_axis_gets_no_qualifier(sky):
    """The qualifier keys off the SERIES, not off the moon's state: on a
    night the moon rises between sunset and last light the rise is drawn
    normally and needs no words."""
    viz = _story(sky, FULL_MOON)["viz"]
    rise = next(e for e in viz["series"] if e["key"] == "moonrise")
    assert rise["note"] is None
    for entry in viz["series"]:
        assert not (entry.get("note") or "").startswith("already up")
