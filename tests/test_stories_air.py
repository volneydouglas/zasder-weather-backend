"""Story engine (2.0): the Air & Flight producer.

Two rules carry this file, and neither is about arithmetic.

FIRST, THE FRAMING. This card is local weather science and it must never
read as an aviation product. There is no METAR source behind this server,
so there are no visibility, ceiling or official-altimeter rows — inventing
them would be fabricating a product we cannot back — and the words "pilot"
and "PIREP" appear nowhere. The disclaimer ships ON the image. The forbidden
-vocabulary sweep below is an invariant test in the same spirit as the
rain-rate one: it is cheap, it is exhaustive over the rendered surface, and
it fails loudly the first time somebody makes the copy sound more official.

SECOND, ELEVATION. Absent is not zero, and at altitude the difference is
worth thousands of feet: reading an unset elevation as sea level would hand
a Denver station a five-thousand-foot "penalty" that is pure fiction. So the
contrast declines and the story ships without it, and the pair of tests that
prove that are the point of the fixture.

Fixtures seed one observation row and pin "today" through
app.climate.local_today, the same seam the other story suites use.
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

# A Chandler summer afternoon, in the units the database actually stores.
# `baromabsin` is the STATION barometer; `baromrelin` is the sea-level
# figure that must never reach the density chain.
HOT = {"tempf": 108.0, "humidity": 12.0, "dewPoint": 44.0,
       "baromabsin": 28.02, "baromrelin": 29.62,
       "windspeedmph": 9.0, "windgustmph": 21.0}

# The same station in January: cold, dry, high pressure. The air is DENSER
# than the standard atmosphere and the density altitude goes negative, which
# is a real reading and not an error.
COLD = {"tempf": 31.0, "humidity": 60.0, "dewPoint": 19.0,
        "baromabsin": 28.90, "baromrelin": 30.35}

ELEVATION_FT = 1200.0


def _seed_obs(db, fields: dict, on: date = TODAY, hour: int = 15,
              mac: str = MAC) -> None:
    """One observation at `hour` UTC on `on`. The suite pins TIMEZONE=UTC,
    so the local day is the calendar day.

    The station's rows are REPLACED: `latest_observation` composites the
    newest row with the ~5 minutes behind it, so a leftover from an earlier
    seed in the same test would quietly fill in fields this one meant to
    leave absent.
    """
    ms = int(datetime(on.year, on.month, on.day, hour, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)

    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM observations WHERE mac = ?",
                               (mac,))
            await conn.commit()
        await db.insert_observations(mac, [{"dateutc": ms, **fields}])
    asyncio.run(run())


def _seed_rollups(db, first: date, last: date, mac: str = MAC) -> None:
    """Plain daily rows, purely so the station attribution has a span. This
    producer reads none of them — it is the one story about right now."""
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE mac = ?",
                               (mac,))
            d = first
            while d <= last:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?,?,?,?,?,?)",
                    (mac, d.isoformat(), 70.0, 100.0, 100.0, 1))
                d += timedelta(days=1)
            await conn.commit()
    asyncio.run(run())


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "station_elevation_ft", ELEVATION_FT)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _science(mac: str = MAC, **kw) -> dict:
    return asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_SCIENCE], limit=8, **kw))


def _one(mac: str = MAC, **kw) -> dict:
    """This producer's single story. Filtered to `air_flight` on purpose:
    the science family keeps growing — Fire Weather reads the same current
    observation and fires on the same hot, dry fixtures — and these tests
    are about what the aviation card says."""
    out = _science(mac, **kw)
    mine = [s for s in out["stories"] if s["story_type"] == "air_flight"]
    assert len(mine) == 1, "the producer emits exactly one story"
    return mine[0]


def _strings(story: dict) -> list[str]:
    """Every rendered string in one story — the whole surface a card can
    draw. A vocabulary sweep that misses a field is a sweep that passes."""
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"], story["period"]["label"],
           story["station"]["name"] or "", story["disclaimer"] or ""]
    out += [x["label"] for x in story["supporting"]]
    if story["comparison"]:
        c = story["comparison"]
        out += [c["label"], c["baseline_label"], c["rank_line"] or ""]
    if story["viz"]:
        out.append(story["viz"]["axis_label"] or "")
        out += [str(e.get("label", "")) for e in story["viz"]["series"]]
    return out


# ───────────────── the framing, which is the feature ─────────────────

# Vocabulary that would turn a weather card into an aviation product we
# cannot back. "flight planning" is deliberately absent from this list and
# checked separately: it appears exactly once, inside the disclaimer, in the
# sentence that says this is NOT for it.
FORBIDDEN = ("pilot", "pirep", "metar", "taf", "notam", "visibility",
             "ceiling", "altimeter", "runway", "clearance", "airspace")


def test_the_card_never_becomes_an_aviation_product(engine):
    """The non-negotiable one. No METAR source exists behind this server, so
    every visibility or ceiling row would be invented — and a card that
    sounds official is one somebody eventually flies on."""
    _seed_obs(engine, HOT)
    s = _one()
    blob = " ".join(_strings(s)).lower()
    for word in FORBIDDEN:
        assert word not in blob, f"aviation vocabulary leaked: {word!r}"
    # No row anywhere claims a measurement this server does not take.
    keys = ({x["key"] for x in s["supporting"]} | {s["hero"]["key"]}
            | {e["key"] for e in s["viz"]["series"]})
    assert keys.isdisjoint({"visibility", "ceiling", "altimeter",
                            "flight_category"})
    # "Air & Flight" is the title the review settled on and the only place
    # the word belongs. "Flight planning" appears exactly once, inside the
    # sentence that says this is NOT for it.
    assert blob.count("flight") == 2
    assert "flight plan" not in blob.replace(s["disclaimer"].lower(), "")
    assert "flight" not in s["context"].lower()
    assert "flight" not in s["hero_line"].lower()


def test_the_disclaimer_rides_on_the_image(engine):
    """`Story.disclaimer` exists for this card and the science template
    renders it onto the graphic — it is not a footnote and not optional."""
    _seed_obs(engine, HOT)
    s = _one()
    assert s["family"] == "science"
    assert s["disclaimer"] == stories.AIR_DISCLAIMER
    assert s["disclaimer"] == "Local weather science · not for flight planning."
    assert s["title"] == "Air & Flight" and s["story_type"] == "air_flight"


# ───────────────── the physics, borrowed not rebuilt ─────────────────

def test_the_density_altitude_is_the_one_the_science_screen_shows(engine):
    """`derived.density_altitude_ft` is THE definition in this codebase —
    the NWS chain the Science screen already renders. This producer calls
    it; a second formula here would eventually disagree with the tile the
    same user is looking at."""
    from app import derived
    _seed_obs(engine, HOT)
    s = _one()
    expected = derived.density_altitude_ft(HOT["tempf"], HOT["dewPoint"],
                                           HOT["baromabsin"])
    assert s["hero"]["value"] == round(expected)
    assert s["hero"]["unit"] == "ft"
    assert s["hero_line"] == f"THE AIR IS ACTING LIKE {round(expected):,} FT"


def test_it_uses_station_pressure_and_declines_rather_than_borrowing_sea_level(
        engine):
    """The classic wrong-answer path: the chain takes STATION pressure, and
    handing it a sea-level reading returns a confident number wrong by
    roughly the station's own elevation — which is the very quantity this
    card is about. A station that reports only `baromrelin` gets no story."""
    _seed_obs(engine, HOT)
    with_abs = _one()
    assert str(HOT["baromabsin"]) in with_abs["context"]
    assert str(HOT["baromrelin"]) not in with_abs["context"]

    sea_level_only = {k: v for k, v in HOT.items() if k != "baromabsin"}
    _seed_obs(engine, sea_level_only)
    out = _science()
    # This card declines; Fire Weather reads the same observation and needs
    # no pressure at all, so it correctly still has something to say.
    assert "air_flight" in out["declined"]
    assert [s for s in out["stories"] if s["story_type"] == "air_flight"] == []


def test_the_dew_point_falls_back_to_the_same_magnus_form(engine):
    """A source that posts temperature and humidity but no dew point still
    gets the card, computed the way `/api/devices/{mac}/derived` computes it
    — one dew point per observation, however many surfaces quote it."""
    from app import derived
    no_dew = {k: v for k, v in HOT.items() if k != "dewPoint"}
    _seed_obs(engine, no_dew)
    s = _one()
    magnus = derived.dew_point_f(HOT["tempf"], HOT["humidity"])
    stat = next(x for x in s["supporting"] if x["key"] == "dew_point")
    assert stat["value"] == pytest.approx(round(magnus, 1))
    assert s["hero"]["value"] == round(
        derived.density_altitude_ft(HOT["tempf"], magnus, HOT["baromabsin"]))


def test_the_humidity_share_is_the_same_chain_run_twice(engine):
    """"640 ft of it is the humidity" is the surprising half of the science,
    and it is a subtraction between two runs of ONE formula, never a second
    approximation of the same physics."""
    from app import derived
    _seed_obs(engine, HOT)
    s = _one()
    wet = derived.density_altitude_ft(HOT["tempf"], HOT["dewPoint"],
                                      HOT["baromabsin"])
    dry = derived.density_altitude_ft(HOT["tempf"], stories.BONE_DRY_DEW_F,
                                      HOT["baromabsin"])
    stat = next(x for x in s["supporting"] if x["key"] == "moisture_share")
    assert stat["value"] == round(wet - dry)
    assert stat["unit"] == "ft"


def test_wind_is_labelled_as_not_being_part_of_the_calculation(engine):
    """Wind is on the card because it is the other thing the air is doing.
    Listing it silently among the inputs would teach the physics wrong."""
    _seed_obs(engine, HOT)
    s = _one()
    wind = next(x for x in s["supporting"] if x["key"] == "wind")
    assert "not part of the density calculation" in wind["label"]
    assert wind["unit"] == "mph"


# ───────────────── elevation: absent is not sea level ─────────────────

def test_a_known_elevation_becomes_the_contrast(engine):
    _seed_obs(engine, HOT)
    s = _one()
    assert "1,200 ft this station actually sits at" in s["context"]
    c = s["comparison"]
    assert c["kind"] == "station_elevation"
    assert c["baseline"] == 1200
    assert c["value"] == s["hero"]["value"]
    assert c["delta"] == s["hero"]["value"] - 1200
    assert c["direction"] == "above"
    penalty = next(x for x in s["supporting"] if x["key"] == "altitude_penalty")
    assert penalty["value"] == c["delta"]
    assert [e["key"] for e in s["viz"]["series"]] == ["station", "density"]
    assert s["viz"]["highlight_key"] == "density"
    # The gap is drawn as a signed bracket, and a sign is not a sentence:
    # on a shared image "+3,084 ft" has to explain itself (field report from
    # the card templates, 2026-08-30). The describing phrase lived only in a
    # supporting stat, which the bracket cannot reach.
    density = s["viz"]["series"][1]
    assert density["above_station"] == c["delta"]
    assert density["above_station_label"] == \
        f"{density['above_station']:,} ft above the station"


def test_the_bracket_label_flips_with_the_air(client, monkeypatch):
    """Cold dense air genuinely sits BELOW the ground it is standing on, and
    the bracket has to say so rather than printing a minus sign and hoping."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "station_elevation_ft", ELEVATION_FT)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed_obs(db, COLD)

    density = _one()["viz"]["series"][1]
    assert density["above_station"] < 0
    assert density["above_station_label"] == \
        f"{abs(density['above_station']):,} ft below the station"


def test_an_unknown_elevation_declines_the_contrast_not_the_story(
        client, monkeypatch):
    """THE test. `station_elevation_ft` defaults to 0.0 and its documented
    meaning there is "off", so 0.0 reads as UNKNOWN — never as sea level. A
    mile-high station that never configured one must not be handed a
    five-thousand-foot penalty out of thin air. The density altitude is a
    complete fact on its own, so the story ships and only the contrast goes."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "station_elevation_ft", 0.0)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed_obs(db, HOT)

    s = _one()
    assert s["comparison"] is None
    assert "penalty" not in s["score_parts"]
    assert {x["key"] for x in s["supporting"]}.isdisjoint(
        {"station_elevation", "altitude_penalty"})
    assert [e["key"] for e in s["viz"]["series"]] == ["density"]
    assert s["viz"]["series"][0]["above_station"] is None
    # …and no label for a bracket that has nothing to span.
    assert s["viz"]["series"][0]["above_station_label"] is None
    # And no sentence anywhere pretends to know where the station is. (The
    # viz axis still reads "feet above sea level" — that is what a density
    # altitude IS measured from, and it claims nothing about the station.)
    for text in _strings(s):
        assert "sits at" not in text
        assert "station's elevation" not in text
    # The hero survives intact: this is a decline of one line, not of a card.
    assert s["hero"]["value"] > 0
    assert s["score_parts"].keys() == {"thinness", "moisture"}


def test_an_absurd_elevation_is_treated_as_unknown(client, monkeypatch):
    """A fat-fingered env value (metres pasted into a feet field, a stray
    zero) must not headline. Nothing on land sits at 120,000 ft."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed_obs(db, HOT)
    for absurd in (120000.0, -9000.0, float("nan")):
        monkeypatch.setattr(settings, "station_elevation_ft", absurd)
        assert _one()["comparison"] is None, absurd
    # A NEGATIVE but believable elevation is knowledge, not absence — the
    # Salton Sea and the Dead Sea shore are real places with weather.
    monkeypatch.setattr(settings, "station_elevation_ft", -220.0)
    assert _one()["comparison"]["baseline"] == -220
    assert _one()["comparison"]["delta_pct"] is None


# ───────────────── the cold, dense side ─────────────────

def test_dense_air_flips_the_words_as_well_as_the_number(engine):
    """A cold, dry, high-pressure morning genuinely measures BELOW sea
    level. "Acting like −647 ft" is not a sentence, and calling that air
    "thin" would be the card contradicting its own number."""
    _seed_obs(engine, COLD)
    s = _one()
    assert s["hero"]["value"] < 0
    assert s["hero_line"].endswith("FT BELOW SEA LEVEL")
    assert "as dense as a standard day" in s["context"]
    assert "as thin as" not in s["context"]
    assert "cold dense air is heavier than standard" in s["context"]
    assert s["comparison"]["direction"] == "below"
    penalty = next(x for x in s["supporting"] if x["key"] == "altitude_penalty")
    assert penalty["value"] > 0 and "handing back" in penalty["label"]


# ───────────────── decline paths ─────────────────

def test_declines_when_the_station_has_never_reported(engine):
    assert engine is not None
    out = _science()
    assert out["stories"] == [] and out["declined"] == ["air_flight", "degree_days", "fire_weather",
                                            "barometer_says",
                                            "forecast_vs_backyard",
                                            "humidity_tax"]


def test_declines_when_the_newest_observation_is_not_from_today(engine):
    """"Right now" is a claim about right now. Read three days later the
    same row must lose the card — the barometer has moved and nobody is
    measuring the air this sentence describes."""
    _seed_obs(engine, HOT)
    assert _one()["hero"]["value"] > 0

    _seed_obs(engine, HOT, on=TODAY - timedelta(days=1))
    assert _science()["stories"] == []
    # A clock ahead of the anchor is the same amount of "we don't know when
    # this was" as a stale one.
    _seed_obs(engine, HOT, on=TODAY + timedelta(days=1))
    assert _science()["stories"] == []


def test_declines_without_a_temperature(engine):
    _seed_obs(engine, {k: v for k, v in HOT.items() if k != "tempf"})
    assert _science()["stories"] == []


def test_declines_without_any_moisture_reading(engine):
    """No dew point and no humidity to derive one: the virtual temperature
    has no moisture term and the chain cannot run. A card that assumed dry
    air would be the zero bug in a lab coat."""
    _seed_obs(engine, {k: v for k, v in HOT.items()
                       if k not in ("dewPoint", "humidity")})
    assert _science()["stories"] == []


def test_an_unusable_timestamp_is_a_time_we_do_not_have(engine):
    """A missing epoch, a garbled one, and one so far out of range that the
    calendar refuses it are all "we do not know when this was", which is not
    the same as "it was now"."""
    assert engine is not None
    ms = int(datetime(2026, 8, 30, 15, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    assert stories._obs_local_day({"dateutc": ms}) == TODAY
    assert stories._obs_local_day({}) is None
    assert stories._obs_local_day({"dateutc": "half past four"}) is None
    assert stories._obs_local_day({"dateutc": float("nan")}) is None
    # Seconds pasted where milliseconds belong go the other way — 2026 in
    # seconds reads as 1970 in milliseconds, which is a real date and a very
    # stale one, so the same-day rule catches it rather than this guard.
    assert stories._obs_local_day({"dateutc": ms // 1000}) == date(1970, 1, 21)
    # Milliseconds pasted where seconds belong land past the calendar's end.
    assert stories._obs_local_day({"dateutc": 10 ** 18}) is None


def test_declines_when_the_row_carries_no_timestamp(engine):
    """End to end: the composite `latest_observation` reads `dateutc` out of
    the stored payload, so a row that never carried one gets no card."""
    _seed_obs(engine, HOT)

    async def strip():
        import json
        async with engine.connect() as conn:
            row = await (await conn.execute(
                "SELECT data_json FROM observations WHERE mac = ?",
                (MAC,))).fetchone()
            payload = json.loads(row["data_json"])
            payload.pop("dateutc", None)
            await conn.execute(
                "UPDATE observations SET data_json = ? WHERE mac = ?",
                (json.dumps(payload), MAC))
            await conn.commit()
    asyncio.run(strip())
    assert _science()["stories"] == []


# ───────────────── units ─────────────────

METRIC = stories.Units(temperature="celsius", wind="kph", rain="mm",
                       pressure="hPa")


def test_a_celsius_render_leaks_no_fahrenheit_or_inhg(engine):
    """Every reading stays API-native through the physics and converts only
    as it becomes words. A threshold compared against a converted value is
    the bug this repo keeps re-shipping, and the density chain is exactly
    where it would happen — it eats °F, °F and inHg."""
    _seed_obs(engine, HOT)
    metric = _one(units=METRIC)
    for text in _strings(metric):
        assert "°F" not in text, text
        assert "inHg" not in text, text
        assert "mph" not in text, text
    units = {x["unit"] for x in metric["supporting"] if x["unit"]}
    assert units <= {"C", "hPa", "km/h", "ft", "%"}

    native = _one()
    # Same air, same physics, same score: only the words moved.
    assert native["hero"]["value"] == metric["hero"]["value"]
    assert native["interestingness"] == metric["interestingness"]
    assert native["score_parts"] == metric["score_parts"]
    assert native["viz"]["series"] == metric["viz"]["series"]


def test_the_conversions_round_trip(engine):
    _seed_obs(engine, HOT)
    metric = _one(units=METRIC)
    temp = next(x for x in metric["supporting"] if x["key"] == "temperature")
    assert temp["value"] == pytest.approx((108.0 - 32) * 5 / 9, abs=0.05)
    press = next(x for x in metric["supporting"]
                 if x["key"] == "station_pressure")
    assert press["value"] == pytest.approx(28.02 * 33.8639, abs=0.05)
    wind = next(x for x in metric["supporting"] if x["key"] == "wind")
    assert wind["value"] == round(9.0 * 1.609344)
    assert f"{press['value']:.1f} hPa" in metric["context"]


def test_altitude_stays_in_feet_in_every_scale(engine):
    """Density altitude is DEFINED in feet by the chain the Science screen
    already renders, and the app carries no altitude preference to send.
    Both scales therefore quote the same number in the same unit — the
    alternative is a fifth unit axis in a contract the client cannot speak."""
    _seed_obs(engine, HOT)
    for units in (None, METRIC):
        s = _one(**({} if units is None else {"units": units}))
        assert s["hero"]["unit"] == "ft"
        assert s["viz"]["unit"] == "ft"
        assert s["hero_line"].endswith(" FT")


# ───────────────── determinism ─────────────────

def test_the_pinned_today_seam_fixes_the_whole_story(engine):
    _seed_obs(engine, HOT)
    first, again = _one(), _one()
    assert first == again
    assert first["id"] == "science.air_flight.2026-08-30"
    assert first["period"] == {"kind": "moment", "label": "right now",
                               "start": "2026-08-30", "end": "2026-08-30",
                               "partial": False}


# ───────────────── cross-producer calibration ─────────────────

def _best_of(ranked: dict, story_type: str) -> float:
    return max((s["interestingness"] for s in ranked["stories"]
                if s["story_type"] == story_type), default=0.0)


def test_thin_air_wins_on_the_day_it_is_about(client, monkeypatch):
    """A mile-high station at 99°F with a wet airmass: the air is acting
    like 9,200 ft and that is the story on that station on that afternoon.
    `interestingness` is documented as comparable ACROSS producers, so it
    has to be able to beat the heat ledger on the ledger's own data."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "station_elevation_ft", 5000.0)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed_rollups(db, date(2026, 1, 1), TODAY)
    _seed_obs(db, {"tempf": 99.0, "humidity": 30.0, "dewPoint": 63.0,
                   "baromabsin": 24.60})

    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert ranked["stories"][0]["story_type"] == "air_flight"
    assert _best_of(ranked, "air_flight") > 0.70
    assert _best_of(ranked, "air_flight") > _best_of(ranked, "heat_ledger")


def test_ordinary_air_yields_to_everything_else(client, monkeypatch):
    """The direction that makes it calibration rather than a thumb on the
    scale. Same station, same rollups, a mild spring evening: the producer
    still has something true to say and it must rank BELOW the ledger it
    beat a moment ago."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "station_elevation_ft", ELEVATION_FT)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed_rollups(db, date(2026, 1, 1), TODAY)
    _seed_obs(db, {"tempf": 68.0, "humidity": 35.0, "dewPoint": 39.0,
                   "baromabsin": 28.55})

    ranked = asyncio.run(stories.top_stories(MAC, limit=12))
    assert _best_of(ranked, "air_flight") < 0.25
    assert _best_of(ranked, "air_flight") < _best_of(ranked, "heat_ledger")
    assert ranked["stories"][0]["story_type"] != "air_flight"
    assert all(0.0 <= s["interestingness"] <= 1.0 for s in ranked["stories"])


# ───────────────── the endpoint ─────────────────

def test_endpoint_serves_the_science_family(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "station_elevation_ft", ELEVATION_FT)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed_obs(db, HOT)

    r = client.get(f"/api/devices/{MAC}/stories?family=science&limit=8",
                   headers=H)
    assert r.status_code == 200
    body = r.json()
    # Degree days needs a year of daily extremes and this fixture seeds
    # observations, so it declines by name. Fire Weather reads the same hot
    # dry observation this card does and ships alongside it.
    # Degree days needs a year of daily extremes; the barometer needs a
    # reading three hours back to take a trend from, and this fixture seeds
    # a single observation. Both decline by name.
    assert body["declined"] == ["degree_days", "barometer_says",
                                "forecast_vs_backyard", "humidity_tax"]
    s = next(s for s in body["stories"] if s["story_type"] == "air_flight")
    assert s["family"] == "science"
    assert s["disclaimer"] == stories.AIR_DISCLAIMER
    # Both science cards carry their own disclaimer ON the image, and the
    # two must never be swapped: one is about flying, one about fire.
    fire = next(s for s in body["stories"]
                if s["story_type"] == "fire_weather")
    assert fire["disclaimer"] == stories.FIRE_DISCLAIMER
    assert fire["disclaimer"] != stories.AIR_DISCLAIMER
    assert ("science", "air_flight") in stories.registered()
