"""Story engine (2.0): the "Fire Weather" producer.

THE RULE THIS SUITE EXISTS FOR, and it is a safety rule rather than a
taste one: Fosberg and the Chandler Burning Index are WEATHER indices.
They know the air. They know nothing about fuel, terrain, drought, or what
any agency has declared. A reader who mistakes one for a fire-danger
rating has been actively misled, so the disclaimer rides ON the image via
Story.disclaimer — exactly as the aviation card's does — and the forbidden
vocabulary is swept out of every string a card can draw.

Both indices are reused from `app.derived`, the same implementations the
Science surface renders. Nothing here re-derives a fire index.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:F1"
TODAY = date(2026, 8, 30)

# A Chandler summer afternoon: hot, bone dry, breezy.
HOT_DRY = {"tempf": 108.0, "humidity": 12.0,
           "windspeedmph": 9.0, "windgustmph": 21.0}
# A soft, damp morning. The air is ordinary and there is no story in it.
MILD_DAMP = {"tempf": 68.0, "humidity": 82.0,
             "windspeedmph": 3.0, "windgustmph": 6.0}

# Words a weather card must never put next to a fire number. Each one
# claims an authority this producer does not have.
_FORBIDDEN = ("fire danger", "danger rating", "red flag", "burn ban",
              "evacuat", "official rating", "fire warning")


def _seed_obs(db, fields: dict, on: date = TODAY, hour: int = 15,
              mac: str = MAC) -> None:
    ms = int(datetime(on.year, on.month, on.day, hour, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)

    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM observations WHERE mac = ?",
                               (mac,))
            await conn.commit()
        await db.insert_observations(mac, [{"dateutc": ms, **fields}])
    asyncio.run(run())


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _fire(mac: str = MAC, **kw) -> dict | None:
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_SCIENCE], limit=8, **kw))
    return next((s for s in out["stories"]
                 if s["story_type"] == "fire_weather"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


def _strings(story: dict) -> list[str]:
    """Every string a card can draw."""
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"], story["period"]["label"],
           story["viz"]["axis_label"], story["viz"]["footnote"] or "",
           story["disclaimer"] or ""]
    out += [s["label"] for s in story["supporting"]]
    out += [e["label"] for e in story["viz"]["series"]]
    return out


# ─────────────────── the disclaimer is not decoration ───────────────────

def test_the_disclaimer_rides_on_the_image(engine):
    """`Story.disclaimer` exists for exactly this, and the science template
    renders it onto the picture. Pinned to the string, the way the aviation
    card's is, so a reword has to be deliberate."""
    _seed_obs(engine, HOT_DRY)
    s = _fire()
    assert s is not None
    assert s["disclaimer"] == stories.FIRE_DISCLAIMER
    assert s["disclaimer"] == ("Weather-driven index only · "
                               "not an official fire-danger rating.")


def test_it_never_claims_an_authority_it_does_not_have(engine):
    """A weather index is not a rating. The sweep runs over the whole
    rendered surface, not just the headline — the disclaimer is the only
    place the phrase "fire-danger rating" may appear, and only to deny it."""
    _seed_obs(engine, HOT_DRY)
    s = _fire()
    body = " ".join(t for t in _strings(s) if t != s["disclaimer"]).lower()
    for banned in _FORBIDDEN:
        assert banned not in body, (banned, body)


def test_it_says_out_loud_what_the_indices_cannot_see(engine):
    _seed_obs(engine, HOT_DRY)
    s = _fire()
    assert "describe the AIR" in s["context"]
    assert "how dry the fuels are" in s["context"]
    assert "what any agency has declared" in s["context"]


# ───────────────────────── the happy path ─────────────────────────

def test_it_reads_both_indices_from_the_shared_implementations(engine):
    """The Science surface's own functions, not a second copy."""
    from app import derived
    _seed_obs(engine, HOT_DRY)
    s = _fire()
    cbi = derived.chandler_burning_index(108.0, 12.0)
    ffwi = derived.fosberg_fwi(108.0, 12.0, 9.0)
    assert _stat(s, "chandler_index")["value"] == round(cbi, 1)
    assert _stat(s, "fosberg_index")["value"] == round(ffwi, 1)
    assert s["hero"]["value"] == round(cbi)
    assert s["hero_line"] == "EXTREME FIRE WEATHER"
    assert s["period"]["kind"] == "moment"


def test_the_band_ladder_shows_where_this_afternoon_sits(engine):
    """A bare index number against no scale is exactly how an index gets
    mistaken for a rating."""
    _seed_obs(engine, HOT_DRY)
    viz = _fire()["viz"]
    assert viz["kind"] == "index_bands"
    assert [b["label"] for b in viz["series"]] == [
        "low", "moderate", "high", "very high", "extreme"]
    assert [b["floor"] for b in viz["series"]] == sorted(
        b["floor"] for b in viz["series"])
    assert viz["highlight_key"] == "extreme"
    assert sum(1 for b in viz["series"] if b["hero"]) == 1


# ───────────── absent is not zero, and calm is not absent ─────────────

def test_a_station_with_no_anemometer_keeps_the_index_it_can_compute(engine):
    """No wind reading means no Fosberg — never a Fosberg computed from a
    zero wind speed, which would read as "calm" and understate the exact
    thing the index exists to measure."""
    _seed_obs(engine, {"tempf": 108.0, "humidity": 12.0})
    s = _fire()
    assert s is not None, "Chandler needs only temperature and humidity"
    assert _stat(s, "chandler_index") is not None
    assert _stat(s, "fosberg_index") is None
    assert _stat(s, "wind") is None
    assert "reports no wind" in s["context"]
    assert "fosberg" not in s["score_parts"]


def test_a_missing_humidity_declines_rather_than_reading_zero(engine):
    """0% humidity is the most alarming number this card can print, and it
    is what an absent sensor would become."""
    _seed_obs(engine, {"tempf": 108.0, "windspeedmph": 9.0})
    assert _fire() is None


def test_ordinary_air_has_no_story(engine):
    """A card that fires every mild afternoon teaches a reader to ignore
    it, which is the last thing a fire-weather card should do."""
    _seed_obs(engine, MILD_DAMP)
    assert _fire() is None


def test_a_stale_reading_is_not_right_now(engine):
    """The same rule the aviation card follows: a "right now" card built
    from last Tuesday is a lie about weather nobody is measuring."""
    _seed_obs(engine, HOT_DRY, on=date(2026, 8, 20))
    assert _fire() is None


def test_a_station_that_never_reported_declines(engine):
    assert engine is not None
    assert _fire() is None


# ───────────────────────── calibration ─────────────────────────

def test_worse_air_scores_higher_on_the_same_scale(engine):
    _seed_obs(engine, HOT_DRY)
    extreme = _fire()
    assert 0.0 <= extreme["interestingness"] <= 1.0

    # Warm and dry, but calm and less severe: still a story, a smaller one.
    _seed_obs(engine, {"tempf": 88.0, "humidity": 25.0,
                       "windspeedmph": 4.0})
    moderate = _fire()
    assert moderate is not None
    assert 0.0 <= moderate["interestingness"] <= 1.0
    assert moderate["interestingness"] < extreme["interestingness"]
    assert moderate["hero"]["value"] < extreme["hero"]["value"]


def test_the_units_it_quotes_are_the_readers(engine):
    """The indices are dimensionless, but the air they were computed from
    is not — and that is what a reader checks the number against."""
    _seed_obs(engine, HOT_DRY)
    c = _fire(units=stories.Units(temperature="celsius", wind="kph"))
    assert _stat(c, "temperature")["unit"] == "C"
    assert _stat(c, "temperature")["value"] == pytest.approx(42.2, abs=0.05)
    assert _stat(c, "wind")["unit"] == "km/h"
    # The index itself has no scale to convert along.
    assert _stat(c, "chandler_index")["unit"] == "index"
    assert _stat(c, "chandler_index")["value"] == _stat(_fire(),
                                                        "chandler_index")["value"]
