"""Story engine (2.0): the "How Cold Is Cold?" cold-ledger producer.

The heat ledger's twin, and the twinning is the point: same pyramid
template, same three score parts, same weights, so the two are rankable
against each other on ONE 0..1 scale. A remarkable winter has to be able
to outrank a merely warm summer in the same feed, and it can only do that
if neither producer is quietly scoring on a scale of its own.

The rule this suite exists to protect is the one the spec states in a
sentence: never invent a freeze that didn't happen, and never confuse a
station that has NEVER FROZEN with a station that has NO COLD SENSOR.
Those two look identical in a ledger of zeros and are opposite facts.

Fixtures seed daily_rollups directly and pin "today" through
app.climate.local_today, the seam every story suite uses.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

MAC = "AA:BB:CC:00:00:C0"
TODAY = date(2026, 8, 30)


def _seed(db, rows: list[tuple[str, float, float | None]],
          mac: str = MAC) -> None:
    """(day, high, low) straight into the rollups. A None low is a day the
    station recorded a high and nothing else — the shape that separates
    "never froze" from "never measured"."""
    async def run():
        async with db.connect() as conn:
            for day, hi, lo in rows:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mac, day, lo, hi, hi, 1))
            await conn.commit()
    asyncio.run(run())


def _year(y: int, bands: list[tuple[int, float, float | None]],
          start: date | None = None) -> list[tuple[str, float, float | None]]:
    """`bands` of (count, high, low) on consecutive days."""
    out: list[tuple[str, float, float | None]] = []
    d = start or date(y, 1, 1)
    for count, hi, lo in bands:
        for _ in range(count):
            out.append((d.isoformat(), hi, lo))
            d += timedelta(days=1)
    return out


# A desert year with a real winter: 60 nights at 30°F, 40 more at 34°F.
_WINTER_2025 = [(60, 60.0, 30.0), (40, 70.0, 34.0),
                (120, 95.0, 60.0), (145, 105.0, 75.0)]
# The same station, a milder winter: 40 freezing nights instead of 60.
_WINTER_2024 = [(40, 60.0, 30.0), (60, 70.0, 34.0),
                (120, 95.0, 60.0), (146, 105.0, 75.0)]


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _cold(mac: str = MAC, **kw) -> dict | None:
    out = asyncio.run(stories.top_stories(
        mac, families=[stories.FAMILY_CLIMATE], limit=12, **kw))
    return next((s for s in out["stories"]
                 if s["story_type"] == "cold_ledger"), None)


def _stat(story: dict, key: str) -> dict | None:
    return next((s for s in story["supporting"] if s["key"] == key), None)


# ───────────────────────── the happy path ─────────────────────────

def test_the_cold_ledger_counts_nights_and_says_so(engine):
    _seed(engine, _year(2025, _WINTER_2025))
    _seed(engine, _year(2024, _WINTER_2024))
    s = _cold()
    assert s is not None
    assert (s["family"], s["story_type"]) == ("climate", "cold_ledger")
    assert s["title"] == "How Cold Is Cold?"
    assert s["id"] == "climate.cold_ledger.2025"

    # NIGHTS, everywhere. A card reading "18 days ≤32°F" is describing
    # something that happened while everyone was asleep.
    assert s["hero"]["unit"] == "nights"
    assert s["hero_line"] == "60 NIGHTS ≤32°F"
    assert "one in every six nights" in s["context"]
    assert "days" not in s["context"]
    assert _stat(s, "nights_recorded")["unit"] == "nights"

    # The coldest night is a date and a reading, not a rank.
    assert _stat(s, "coldest_night")["value"] == 30.0
    # A date in words, through the same helper every other date uses.
    assert _stat(s, "coldest_night")["label"] == "coldest night · January 1 2025"
    assert _stat(s, "freezing_nights")["value"] == 60


def test_it_shares_the_pyramid_template_with_the_heat_card(engine):
    """"Two cards for one template." The template is selected by `kind`, so
    the kind must MATCH the heat card's; the palette flip is the client's,
    keyed on story_type, which is why the series shape cannot drift."""
    _seed(engine, _year(2025, _WINTER_2025))
    s = _cold()
    viz = s["viz"]
    assert viz["kind"] == "ledger_pyramid"
    for bar in viz["series"]:
        # `days` is the shared template's name for the bar length; `nights`
        # rides alongside for a card that wants the honest noun. They are
        # the same number and must never disagree.
        assert bar["days"] == bar["nights"]
        assert bar["label"].startswith("≤")
        assert bar["key"] == f"tier_{int(bar['threshold'])}"
    # The ladder descends: colder thresholds further down.
    assert [b["threshold"] for b in viz["series"]] == sorted(
        [b["threshold"] for b in viz["series"]], reverse=True)
    assert viz["highlight_key"] == f"tier_{viz['highlight']}"


def test_the_comparison_ranks_the_coldest_year_first(engine):
    _seed(engine, _year(2025, _WINTER_2025))
    _seed(engine, _year(2024, _WINTER_2024))
    c = _cold()["comparison"]
    # More nights at the tier is MORE extreme, so rank 1 is the coldest —
    # the same direction the heat card ranks in, on a flipped thermometer.
    assert (c["value"], c["baseline"]) == (60, 40.0)
    assert (c["rank"], c["of"]) == (1, 2)
    assert c["direction"] == "above"
    assert "coldest" in c["rank_line"]


# ─────────────────── never invent a freeze that didn't happen ───────────────────

def test_a_station_with_no_low_readings_declines(engine):
    """The rule the whole card hangs on. A year of highs with no lows is a
    station with no cold sensor, and its cold ledger is not a row of
    zeros — it does not exist. Absent is not zero."""
    _seed(engine, _year(2025, [(300, 95.0, None)]))
    assert _cold() is None


def test_a_station_that_never_gets_cold_declines(engine):
    """Lows measured all year and not one of them reached even the mildest
    tier. That is a real climate, not a cold story."""
    _seed(engine, _year(2025, [(300, 95.0, 68.0)]))
    assert _cold() is None


def test_a_year_too_thin_to_judge_declines(engine):
    _seed(engine, _year(2025, [(20, 60.0, 28.0)]))
    assert _cold() is None


def test_the_tomatoes_line_needs_a_winter_that_was_watched(engine):
    """A measured zero earns the card's warmest line. A record that simply
    wasn't there in January has the same zero and earns nothing — that is
    absent-is-not-zero wearing a gardening apron."""
    # Mild lows all year: cold tiers are the WARM ladder (45/36/32/28/25),
    # nights reach 45 and 36 but never freeze.
    _seed(engine, _year(2025, [(120, 60.0, 38.0), (245, 95.0, 62.0)]))
    s = _cold()
    assert s is not None
    assert _stat(s, "freezing_nights")["value"] == 0
    assert "The tomatoes survived" in s["context"]
    assert "Not one night in 2025 reached 32°F" in s["context"]


def test_a_summer_only_record_stays_quiet_about_freezes(engine):
    """Same zero, no claim: the station arrived in June."""
    _seed(engine, _year(2025, [(150, 95.0, 44.0)], start=date(2025, 6, 1)))
    s = _cold()
    assert s is not None, "nights at 45°F are a real cold ladder"
    assert _stat(s, "freezing_nights")["value"] == 0
    assert "tomatoes" not in s["context"]
    assert "Not one night" not in s["context"]


# ───────────────────────── calibration ─────────────────────────

def test_a_harder_winter_outranks_a_milder_one_on_the_same_scale(engine):
    """Calibration, asserted in BOTH directions: the score has to move the
    right way AND stay inside 0..1, or the twin cards cannot share a feed
    with each other or with the heat ledger."""
    _seed(engine, _year(2025, _WINTER_2025))
    _seed(engine, _year(2024, _WINTER_2024))
    hard = _cold()
    assert 0.0 <= hard["interestingness"] <= 1.0
    assert set(hard["score_parts"]) == {"quotability", "reach", "standout"}
    # The colder year of the two ranks first, so its standout is maximal.
    assert hard["score_parts"]["standout"] == 1.0

    # Now the mild year is the newest one and has the weaker story: the
    # same winter shape with a quarter of the freezing nights.
    _seed(engine, _year(2026, [(15, 60.0, 30.0), (25, 70.0, 34.0),
                               (200, 95.0, 62.0)]))
    mild = _cold()
    assert 0.0 <= mild["interestingness"] <= 1.0
    assert mild["interestingness"] < hard["interestingness"]


def test_every_threshold_converts_only_at_formatting(engine):
    """Units convert at the moment a number becomes words, never before.
    The ladder is Fahrenheit through every comparison above."""
    _seed(engine, _year(2025, _WINTER_2025))
    c = _cold(units=stories.Units(temperature="celsius"))
    assert c["hero"]["unit"] == "nights"          # a count has no scale
    assert "≤0°C" in c["hero_line"]               # 32°F, said in Celsius
    assert _stat(c, "coldest_night")["unit"] == "C"
    assert _stat(c, "coldest_night")["value"] == pytest.approx(-1.1, abs=0.05)
    # The count itself is identical in both scales — it is a fact about
    # nights, not about degrees.
    f = _cold()
    assert c["hero"]["value"] == f["hero"]["value"]
