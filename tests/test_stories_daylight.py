"""Story engine (2.0): the Shrinking Day producer.

Three things carry this suite.

FIRST, THE MATH IS BORROWED AND HAS TO STAY BORROWED. `app.almanac` is a
constant-for-constant port of the SolarMath the app already ships, so this
station's sunset must be the same minute in a share card and in the almanac
card beside it. A handful of tests pin real clock times for a real place on
a real date; if somebody "improves" the port, they fail loudly rather than
letting the two halves of the product drift a minute apart.

SECOND, A GUESSED LATITUDE IS AN INVISIBLE LIE. A sunset half an hour wrong
looks perfectly plausible anywhere on earth, so the producer declines
without coordinates rather than assuming a position — and Null Island reads
as unknown for the same reason `station_elevation_ft`'s 0.0 does.

THIRD, THE SCORE IS SCALED BY LATITUDE. An equinox on the equator is a
seven-minute annual swing and no story at all, so the calibration tests
assert the equator LOSES on the day a mid-latitude station wins.

Fixtures pin "today" through app.climate.local_today, the same seam the
other story suites use, and move the station with the real operator
location-override path.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:5C"
TZ = "America/Phoenix"

# Chandler, Arizona — the reference station, and a latitude with a real but
# unremarkable seasonal swing (about four and a half hours).
CHANDLER = (33.3062, -111.8413)
# Kampala. Six degrees of latitude, seven minutes of annual daylight swing:
# the place where this card has nothing to say.
EQUATOR = (0.3476, 32.5825)
# Utqiagvik, Alaska. Midnight sun and polar night, both of which are
# MEASUREMENTS about a sky rather than gaps in a record.
POLAR = (71.2906, -156.7886)
# Seattle: eight hours of annual daylight swing and a heat record nobody
# would print. The mirror image of Chandler, and the fixture that proves
# these two producers trade places on merit rather than by rank order.
SEATTLE = (47.6062, -122.3321)
MILD_MAC = "AA:BB:CC:00:00:5D"

TODAY = date(2026, 8, 30)
FIRST = date(2025, 1, 1)

# 2026's turning points, in Chandler's local clock. Checked against the
# published instants: the September equinox is 2026-09-23 00:13 UTC, which
# is the 22nd in Arizona.
JUNE_SOLSTICE = date(2026, 6, 21)
DECEMBER_SOLSTICE = date(2026, 12, 21)
SEPTEMBER_EQUINOX = date(2026, 9, 22)

_SHAPES = (0.02, 0.31, 0.11, 0.64, 0.07, 0.45, 0.22, 0.88, 0.05, 0.37,
           0.16, 0.53, 0.09, 0.71, 0.27, 0.13, 0.42, 0.03, 0.59, 0.19,
           0.34, 0.08, 0.48, 0.24, 0.95, 0.12, 0.40, 0.06, 0.66)


def _rows(last: date = date(2026, 12, 31)) -> list[dict]:
    """A plain Chandler-shaped record. This producer reads NONE of it — it
    needs only a location and a date — but the station attribution wants a
    span, and the calibration tests need a heat ledger to lose to."""
    rows: list[dict] = []
    d, i = FIRST, 0
    while d <= last:
        doy = d.timetuple().tm_yday
        hi = 70.0 + 38.0 * (1 - abs(doy - 200) / 200.0) + 6 * _SHAPES[i % 29]
        rows.append({"day": d.isoformat(), "hi": round(hi, 1),
                     "lo": round(hi - 24.0, 1)})
        d += timedelta(days=1)
        i += 1
    return rows


def _mild_rows() -> list[dict]:
    """Temperate: highs from the low fifties to the mid eighties, so the
    heat ledger exists (it clears 80°F) and is worth almost nothing."""
    rows: list[dict] = []
    d, i = FIRST, 0
    while d <= date(2026, 12, 31):
        doy = d.timetuple().tm_yday
        hi = 50.0 + 28.0 * (1 - abs(doy - 200) / 200.0) + 6 * _SHAPES[i % 29]
        rows.append({"day": d.isoformat(), "hi": round(hi, 1),
                     "lo": round(hi - 20.0, 1)})
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


def _place(db, coords: tuple[float, float] | None,
           mac: str = MAC) -> None:
    """Register the station and, when coords are given, set them through the
    real operator location-override path — the one `db.list_devices` merges
    over whatever the ingest path stamped. `None` clears the override AND
    the row's own coords, which is how a station with no position is
    spelled."""
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
    # The server-wide forecast location is the producer's SECOND source of
    # coordinates. Blanked by default so the device row is what is under
    # test; one test switches it back on deliberately.
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
    """The daylight story for one day at one place, or None when the
    producer declined.

    ⚠️ MOVING THE STATION MEANS MOVING ITS CLOCK. The producer asks for the
    events inside a LOCAL day, and the local day is `settings.timezone` —
    the same zone that stamped every rollup row. Dropping a station in
    Kampala while its server still thinks it is in Arizona genuinely has no
    sunrise and no sunset inside the same local day, and the producer
    correctly declines. Tests that relocate the station relocate its zone.
    """
    if day is not None:
        state["today"] = day
    if tz is not None:
        state["settings"].timezone = tz
    if coords is not ...:
        _place(state["db"], coords)
    kw = {"units": units} if units is not None else {}
    ranked = asyncio.run(stories.top_stories(MAC, limit=12, **kw))
    return next((s for s in ranked["stories"]
                 if s["story_type"] == "daylight"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


# ───────────────── the ported math ─────────────────

def test_the_sun_events_match_the_almanac_the_app_already_ships(sky):
    """A real place, a real date, the clock times an almanac prints. The
    port is not allowed to drift from the app's SolarMath, because both
    render into the same product — a share card generated on the server sits
    beside the almanac card in the same screen."""
    s = _story(sky, TODAY)
    assert s is not None
    assert _stat(s, "sunrise")["label"] == "sunrise · 6:00 am"
    assert _stat(s, "sunset")["label"] == "sunset · 6:56 pm"
    # Civil twilight, the sun 6° down — about twenty-five minutes either side
    # of the sun itself at this latitude in August.
    assert _stat(s, "first_light")["label"] == "first light · 5:35 am"
    assert _stat(s, "last_light")["label"] == "last light · 7:21 pm"
    assert _stat(s, "daylight_today")["label"] == "daylight today · 12h 56m"


def test_the_clock_value_is_minutes_past_local_midnight(sky):
    """The label carries the words; the VALUE carries the number a dial
    needs. A client that had to parse "6:00 am" back into a position would
    be composing, which is the client's forbidden move."""
    s = _story(sky, TODAY)
    assert _stat(s, "sunrise")["value"] == 6 * 60
    assert _stat(s, "sunset")["value"] == 18 * 60 + 56
    assert _stat(s, "sunrise")["unit"] == "min"


# ───────────────── coordinates, or nothing ─────────────────

def test_declines_when_the_station_has_no_coordinates(sky):
    """The family's load-bearing decline. A guessed latitude produces a
    plausible sunset for the wrong continent."""
    assert _story(sky, TODAY, coords=None) is None
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert "shrinking_day" in ranked["declined"]
    assert "tonights_sky" in ranked["declined"]


def test_null_island_reads_as_unknown_not_as_a_place(sky):
    """(0, 0) is where an unset coordinate pair goes to look real — the same
    trap `station_elevation_ft`'s 0.0-means-off default sets."""
    assert _story(sky, TODAY, coords=(0.0, 0.0)) is None


def test_an_impossible_coordinate_declines_rather_than_raising(sky):
    """Coordinates are operator data and are not validated at write time —
    the lesson nws_watch pays for. A latitude of 200 must not reach the
    trigonometry."""
    assert _story(sky, TODAY, coords=(200.0, -111.8)) is None


def test_the_configured_forecast_location_is_the_second_source(sky):
    """Not a guess: it is the position the operator set for this server's
    own forecasts, which alerts.py already falls back to."""
    assert _story(sky, TODAY, coords=None) is None
    sky["settings"].forecast_lat, sky["settings"].forecast_lon = CHANDLER
    s = _story(sky, TODAY)
    assert s is not None
    assert _stat(s, "sunrise")["label"] == "sunrise · 6:00 am"


# ───────────────── the seasonal editions ─────────────────

def test_the_longest_day_gets_its_own_edition(sky):
    s = _story(sky, JUNE_SOLSTICE)
    assert s["title"] == "The Longest Day"
    assert s["hero_line"] == "THE LONGEST DAY OF THE YEAR · 14H 21M"
    assert s["score_parts"]["edition"] > 0
    # The derivative story is at its weakest here — the day length is
    # standing still — which is exactly why the edition is a floor and not a
    # dimension inside the mean.
    assert s["score_parts"]["swing"] < 0.05
    assert s["interestingness"] > 0.6


def test_the_shortest_day_lands_on_the_solstice_not_the_day_after(sky):
    """The two days flanking a solstice differ by hundredths of a second —
    four orders of magnitude finer than the model's own accuracy. Deciding
    on that noise would print "the shortest day of the year" on December 22
    while every calendar in the country said the 21st."""
    from app import almanac
    tz = __import__("zoneinfo").ZoneInfo(TZ)
    d21 = almanac.daylight_seconds(*CHANDLER, DECEMBER_SOLSTICE, tz)
    d22 = almanac.daylight_seconds(*CHANDLER,
                                   DECEMBER_SOLSTICE + timedelta(days=1), tz)
    assert d22 < d21                       # arithmetically shorter…
    assert abs(d22 - d21) < 1.0            # …by a third of a second
    s = _story(sky, DECEMBER_SOLSTICE)
    assert s["title"] == "The Shortest Day"
    assert s["hero_line"].startswith("THE SHORTEST DAY OF THE YEAR")
    assert _story(sky, DECEMBER_SOLSTICE + timedelta(days=1))["title"] != \
        "The Shortest Day"


def test_an_equinox_edition_triggers_within_three_days_and_not_at_four(sky):
    near = _story(sky, SEPTEMBER_EQUINOX + timedelta(days=3))
    far = _story(sky, SEPTEMBER_EQUINOX + timedelta(days=4))
    assert "FALL EQUINOX" in near["hero_line"]
    assert "edition" in near["score_parts"]
    assert "FALL EQUINOX" not in far["hero_line"]
    assert "edition" not in far["score_parts"]
    assert near["interestingness"] > far["interestingness"]


def test_the_edition_names_the_day_it_is_about(sky):
    assert "FALL EQUINOX TODAY" in _story(sky, SEPTEMBER_EQUINOX)["hero_line"]
    assert "IN 2 DAYS" in _story(
        sky, SEPTEMBER_EQUINOX - timedelta(days=2))["hero_line"]
    assert "1 DAY AGO" in _story(
        sky, SEPTEMBER_EQUINOX + timedelta(days=1))["hero_line"]


def test_the_midnight_sun_is_a_measurement_not_a_missing_value(sky):
    """Twenty-four hours of daylight is something this sky ACTUALLY DID.
    Reporting it as absent would be the mirror image of the bug this repo
    keeps re-shipping."""
    s = _story(sky, JUNE_SOLSTICE, coords=POLAR, tz="America/Anchorage")
    assert s["title"] == "The Midnight Sun"
    assert s["hero_line"] == "THE SUN DOES NOT SET TODAY"
    assert _stat(s, "daylight_today")["value"] == 24 * 60
    assert s["interestingness"] > 0.9


def test_polar_night_reports_zero_daylight_and_still_finds_first_light(sky):
    s = _story(sky, DECEMBER_SOLSTICE, coords=POLAR,
               tz="America/Anchorage")
    assert s["title"] == "Polar Night"
    assert s["hero_line"] == "THE SUN DOES NOT RISE TODAY"
    assert _stat(s, "daylight_today")["value"] == 0
    # The sun never clears the horizon and the sky still brightens: civil
    # twilight is a different question from sunrise, and both are answered.
    assert _stat(s, "first_light") is not None
    assert "0m of daylight" in s["context"]


# ───────────────── the ordinary day ─────────────────

def test_an_ordinary_day_leads_with_what_has_gone_since_the_solstice(sky):
    """The number the design review called the real story."""
    s = _story(sky, TODAY)
    assert s["title"] == "The Shrinking Day"
    assert s["hero_line"] == "1H 26M OF DAYLIGHT GONE SINCE THE SUMMER SOLSTICE"
    assert _stat(s, "since_solstice")["value"] == 86
    assert _stat(s, "change_today")["value"] == -117
    assert "1m 57s less than yesterday" in s["context"]


def test_a_lengthening_day_says_so(sky):
    s = _story(sky, date(2026, 3, 1))
    assert s["title"] == "The Lengthening Day"
    assert "BACK SINCE THE WINTER SOLSTICE" in s["hero_line"]
    assert _stat(s, "change_today")["value"] > 0


def test_a_quarter_of_an_hour_is_the_floor_for_the_solstice_headline(sky):
    """Under fifteen minutes nobody has noticed, so the card leads with the
    day length itself rather than a change too small to feel."""
    s = _story(sky, JUNE_SOLSTICE + timedelta(days=5))
    assert _stat(s, "since_solstice")["value"] < 15
    assert s["hero_line"] == "14H 21M OF DAYLIGHT TODAY"


def test_the_comparison_is_against_the_solstice_and_carries_no_rank(sky):
    s = _story(sky, TODAY)
    c = s["comparison"]
    assert c["kind"] == "since_last_solstice"
    assert c["baseline_label"] == "14h 21m on June 21"
    assert c["direction"] == "below"
    # There is no leaderboard in an orbit. Every year does this.
    assert c["rank_line"] is None


def test_the_chart_marks_today_and_both_extremes(sky):
    s = _story(sky, TODAY)
    viz = s["viz"]
    assert viz["kind"] == "daylight_year"
    assert viz["highlight_key"] == TODAY.isoformat()
    notes = {e["note"]: e["key"] for e in viz["series"] if e["note"]}
    assert notes["today"] == TODAY.isoformat()
    assert notes["longest day"] == JUNE_SOLSTICE.isoformat()
    assert notes["shortest day"] == DECEMBER_SOLSTICE.isoformat()
    # Every point carries its own finished words; the card formats nothing.
    assert all(e["label"] and e["length_label"] for e in viz["series"])
    assert "equinox" in viz["footnote"]


# ───────────────── latitude is a scale, not a normalizer ─────────────────

def test_the_equator_has_no_seasons_of_light(sky):
    """Every other producer scores against the station's own distribution,
    and doing only that here would hand Kampala a perfect equinox for a
    seven-minute annual swing."""
    equator = _story(sky, date(2026, 3, 20), coords=EQUATOR,
                     tz="Africa/Kampala")
    assert equator["score_parts"]["amplitude"] < 0.02
    assert equator["interestingness"] < 0.05
    # Same date, same producer, a latitude where it means something.
    chandler = _story(sky, date(2026, 3, 20), coords=CHANDLER, tz=TZ)
    assert chandler["interestingness"] > 10 * equator["interestingness"]


def test_the_equinox_edition_cannot_rescue_a_station_with_no_seasons(sky):
    s = _story(sky, date(2026, 3, 20), coords=EQUATOR,
               tz="Africa/Kampala")
    assert "SPRING EQUINOX" in s["hero_line"]      # the edition still fires
    assert s["score_parts"]["edition"] < 0.05      # …and is worth nothing


# ───────────────── determinism ─────────────────

def test_the_same_anchor_produces_the_same_sky_twice(sky):
    """No wall clock anywhere. Two calls in the same second, and a run
    tomorrow with the anchor pinned to today, must be identical."""
    first = _story(sky, TODAY)
    second = _story(sky, TODAY)
    assert first == second


def test_moving_the_anchor_moves_the_sky(sky):
    """The other half of determinism: the payload has to depend on the
    pinned date, or the seam is not doing anything."""
    assert _story(sky, TODAY) != _story(sky, TODAY + timedelta(days=1))


# ───────────────── units ─────────────────

def test_a_celsius_render_leaks_no_fahrenheit_and_no_imperial(sky):
    """This story carries no temperature at all, which is precisely why the
    test matters: a producer that never converts anything must also never
    LEAVE anything in the native scale by accident."""
    metric = stories.Units(temperature="celsius", wind="kph", rain="mm",
                           pressure="hPa")
    s = _story(sky, TODAY, units=metric)
    texts = [s["title"], s["hero_line"], s["context"], s["hero"]["label"],
             s["period"]["label"], s["viz"]["axis_label"],
             s["viz"]["footnote"]]
    texts += [x["label"] for x in s["supporting"]]
    texts += [s["comparison"]["label"], s["comparison"]["baseline_label"]]
    for text in texts:
        assert "°F" not in text and "inHg" not in text and "mph" not in text
    units = {x["unit"] for x in s["supporting"] if x["unit"]}
    # Durations and dates have no imperial/metric axis; there is nothing for
    # a client to send and nothing for Units to decide.
    assert units <= {"min", "s", "days"}


def test_the_story_is_identical_in_both_scales(sky):
    """Nothing here depends on the reader's units, so nothing here may
    change with them — including the score."""
    metric = stories.Units(temperature="celsius", wind="kph", rain="mm",
                           pressure="hPa")
    assert _story(sky, TODAY) == _story(sky, TODAY, units=metric)


# ───────────────── cross-producer calibration ─────────────────

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_the_shortest_day_leads_where_the_light_actually_swings(sky):
    """`interestingness` is documented as comparable ACROSS producers, so
    they have to swap places on merit. Seattle in December: eight and a half
    hours of daylight, the shortest day of the year, and a heat record
    nobody would ever print. The sky is the story."""
    _place(sky["db"], SEATTLE, mac=MILD_MAC)
    _seed(sky["db"], _mild_rows(), mac=MILD_MAC)
    sky["settings"].timezone = "America/Los_Angeles"
    sky["today"] = DECEMBER_SOLSTICE
    ranked = asyncio.run(stories.top_stories(MILD_MAC, limit=12))
    s = next(x for x in ranked["stories"] if x["story_type"] == "daylight")
    assert s["title"] == "The Shortest Day"
    # Eight hours of annual swing saturates the amplitude scale, so the
    # edition floor arrives undiminished — the opposite of Kampala.
    assert s["score_parts"]["amplitude"] == 1.0
    assert _best_of(ranked, "daylight") > 0.8
    assert _best_of(ranked, "heat_ledger") > 0.0, "the fixture must earn one"
    assert _best_of(ranked, "daylight") > _best_of(ranked, "heat_ledger")


def test_an_ordinary_tuesday_yields_to_a_real_heat_ledger(sky):
    """The direction that makes it calibration rather than a thumb on the
    scale. Late August in Chandler: the day length is doing exactly what it
    does every August, and 100°F days are not."""
    sky["today"] = TODAY
    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert _best_of(ranked, "daylight") < 0.5
    assert _best_of(ranked, "heat_ledger") > 0.7
    assert _best_of(ranked, "daylight") < _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] != "daylight"
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


def test_the_same_place_ranks_its_own_days_in_the_right_order(sky):
    """Within one producer, on one station: the solstice beats the equinox
    edition beats an ordinary day. If that order ever inverts, the edition
    floors and the derivative terms are fighting each other."""
    solstice = _story(sky, DECEMBER_SOLSTICE)["interestingness"]
    equinox = _story(sky, SEPTEMBER_EQUINOX)["interestingness"]
    ordinary = _story(sky, TODAY)["interestingness"]
    assert solstice > equinox > ordinary


# ───────────────── the endpoint ─────────────────

def test_the_sky_family_filters_to_exactly_these_producers(sky):
    r = asyncio.run(stories.top_stories(MAC, limit=12, families=["sky"]))
    assert {s["story_type"] for s in r["stories"]} <= {
        "daylight", "tonights_sky", "growing_season"}
    assert {s["family"] for s in r["stories"]} == {"sky"}


def test_the_endpoint_serves_the_sky_family(sky, client):
    r = client.get(f"/api/devices/{MAC}/stories?family=sky&limit=5", headers=H)
    assert r.status_code == 200
    types = {s["story_type"] for s in r.json()["stories"]}
    assert "daylight" in types
