"""Story engine (2.0): the schema, the registry's decline path, ranking,
and the "How Hot Is Hot?" heat-ledger producer.

Fixtures seed daily_rollups directly (the producer reads rollups, never raw
history) and pin "today" through app.climate.local_today, which the engine
threads into the ledger's to-date anchor — so every number below is fixed by
the fixture, not by the day the suite happens to run.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:2C"

# Pinned "today" for the fixture years below.
TODAY = date(2026, 8, 30)


def _seed(db, mac: str, days: list[tuple[str, float]]) -> None:
    """(day, high) rows straight into the rollups. tempf_min is seeded 40°
    below the high so the cold/degree-day halves of assemble() have
    something plausible; nothing here reads them."""
    async def run():
        async with db.connect() as conn:
            for day, hi in days:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mac, day, hi - 40.0, hi, hi, 1))
            await conn.commit()
    asyncio.run(run())


def _run(year: int, start: date, bands: list[tuple[int, float]]) -> list[tuple[str, float]]:
    """`bands` of (count, high) laid down on consecutive days from `start`."""
    out: list[tuple[str, float]] = []
    d = start
    for count, hi in bands:
        for _ in range(count):
            assert d.year == year, "fixture ran off the end of its year"
            out.append((d.isoformat(), hi))
            d += timedelta(days=1)
    return out


# The spec's worked example, reproduced exactly: 240 recorded days with
# 196/157/135/108/84/42 at the six ledger tiers.
_LADDER_240 = [(42, 112.0), (42, 106.0), (24, 101.0), (27, 96.0),
               (22, 91.0), (39, 85.0), (44, 70.0)]


@pytest.fixture()
def engine(client, monkeypatch):
    """Insights on, today pinned. Returns the app.db module for seeding."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _story(mac: str = MAC, **kw):
    return asyncio.run(stories.top_stories(mac, **kw))


# The climate family's other producers, in registry order. They decline on
# every fixture below (no rain columns, no dew point), and naming them keeps
# the decline assertions readable as the registry grows.
OTHER_CLIMATE = ["dry_spell", "humid_month", "water_year",
                 # The year's fingerprint needs all 12 months of hour
                 # rollups; these fixtures seed daily rows only.
                 "shape_of_year",
                 # Needs comfort_rollups, which these fixtures never fold.
                 "comfortable_months"]
# Registry order, which is import order: the science family's Air & Flight
# lands between the humidity month and the water year.
ALL_PRODUCERS = ["how_hot_is_hot",
                 # Registered next to its twin: same template, same weights,
                 # flipped thermometer.
                 "how_cold_is_cold",
                 # Records: the swing needs MIN_SWING_HISTORY measured days
                 # carrying BOTH extremes, which these fixtures never reach.
                 "biggest_swing",
                 "wildest_day", "dry_spell", "humid_month",
                 # Science: degree days need MIN_DEGREE_DAY_DAYS days
                 # measuring BOTH ends, which these fixtures never reach.
                 "air_flight", "degree_days",
                 # Science: needs a same-day observation; these seed rollups.
                 "fire_weather", "barometer_says",
                 # Science: the forecast scorecard needs MIN_FORECAST_DAYS
                 # day-ahead snapshots matched to rollups; no fixture here
                 # stores a forecast.
                 "forecast_vs_backyard", "water_year",
                 # The sky family declines here for two different reasons and
                 # both matter: these fixtures register no device and set no
                 # forecast location, so the two astronomical producers have
                 # no coordinates, and no fixture here covers a winter, so
                 # the growing season has no freeze to anchor on.
                 "shrinking_day", "tonights_sky", "growing_season",
                 # Last in the registry because it was last to be built:
                 # it needs storm_history rows carrying the 2.0 close
                 # capture, and no fixture here closes a storm.
                 "storm_broke_the_heat",
                 # The diurnal pair (2.0): both need 12 months of hour
                 # rollups, which no fixture here seeds.
                 "shape_of_year", "humidity_tax",
                 # Records, registered last: a record needs a row for TODAY
                 # beating a year of history, and the lightning season needs
                 # a station that has ever counted a strike. No fixture here
                 # has either.
                 # Climate (2.0): the comfort ledger needs comfort_rollups
                 # rows, which no fixture here folds.
                 "comfortable_months",
                 "record_broken", "lightning_season"]


def _heat(mac: str = MAC, **kw):
    """The heat producer's own view. Filtered to its own story type on
    purpose: these tests are about what `how_hot_is_hot` says, and the
    climate family keeps growing — an unfiltered call would re-fail them
    every time a producer lands."""
    out = _story(mac, families=[stories.FAMILY_CLIMATE], limit=12, **kw)
    return {**out, "stories": [s for s in out["stories"]
                               if s["story_type"] == "heat_ledger"]}


# ───────────────────── the producer's happy path ─────────────────────

def test_heat_ledger_leads_with_the_most_quotable_tier(engine):
    db = engine
    _seed(db, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    _seed(db, MAC, _run(2024, date(2024, 1, 1), [(50, 101.0), (190, 70.0)]))
    _seed(db, MAC, _run(2023, date(2023, 1, 1), [(150, 101.0), (90, 70.0)]))

    out = _heat()
    # Two candidates, not one: `_seed` puts tempf_min 40° below the high, so
    # this ladder's coolest band (70°F highs) carries 30°F nights and the
    # cold twin has a real story of its own on the same fixture.
    assert out["declined"] == OTHER_CLIMATE and out["candidates"] == 2
    s = out["stories"][0]

    assert (s["family"], s["story_type"]) == ("climate", "heat_ledger")
    assert s["title"] == "How Hot Is Hot?"
    assert s["id"] == "climate.heat_ledger.2025"

    # 100°F, not the highest tier reached (110) and not the most-cleared
    # one (80): the tier nearest a half of all days is the quotable one.
    assert s["hero"]["value"] == 108
    assert s["hero"]["unit"] == "days"
    assert s["hero_line"] == "108 DAYS ≥100°F"
    assert s["context"] == (
        "108 of the 240 days recorded in 2025 reached 100°F or hotter, "
        "nearly one in every two days.")

    assert s["viz"]["kind"] == "ledger_pyramid"
    assert [b["days"] for b in s["viz"]["series"]] == [196, 157, 135, 108, 84, 42]
    assert [b["threshold"] for b in s["viz"]["series"]] == [80, 90, 95, 100, 105, 110]
    assert s["viz"]["highlight"] == 100

    # A finished year is not labelled "so far".
    assert s["period"] == {"kind": "year", "label": "2025",
                           "start": "2025-01-01", "end": "2025-12-31",
                           "partial": False}

    c = s["comparison"]
    assert c["kind"] == "prior_years_full"
    assert (c["value"], c["baseline"]) == (108, 100.0)     # (150 + 50) / 2
    assert c["baseline_label"] == "2023–2024 average"
    assert (c["rank"], c["of"]) == (2, 3)                  # 2023's 150 beats it
    assert (c["delta"], c["delta_pct"], c["direction"]) == (8.0, 8.0, "above")

    # 0.40·quotability + 0.35·reach + 0.25·standout, all three present.
    assert s["interestingness"] == pytest.approx(0.871, abs=5e-4)
    assert s["score_parts"]["reach"] == 1.0                # 110°F was cleared


def test_station_attribution_carries_the_measured_span(engine):
    db = engine
    _seed(db, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    s = _heat()["stories"][0]
    assert s["station"]["mac"] == MAC
    assert s["station"]["first_day"] == "2025-01-01"
    assert s["station"]["last_day"] == "2025-08-28"
    assert s["station"]["day_count"] == 240
    # No device row was ever created. The name used to fall back to the MAC
    # — honest, and a device identifier printed across a picture people post
    # publicly. The identity is still in
    # `mac`; the NAME is something a reader can look at.
    assert s["station"]["name"] == stories.UNNAMED_STATION
    assert s["station"]["name"] != MAC


# ───────────────────────── the decline path ─────────────────────────

def test_declines_when_the_station_has_no_rollups_at_all(engine):
    assert engine is not None
    out = _story()
    assert out["stories"] == []
    # Every producer declines, and each says so by name.
    assert out["declined"] == ALL_PRODUCERS


def test_declines_below_the_coverage_floor(engine):
    """Ten days of data is not a year. Absent is not zero: the alternative
    is a pyramid of near-zeros that reads as a cool year."""
    db = engine
    _seed(db, MAC, _run(2025, date(2025, 6, 1), [(10, 115.0)]))
    out = _story()
    assert out["stories"] == []
    assert out["declined"] == ALL_PRODUCERS


def test_declines_rather_than_rendering_a_pyramid_of_zeros(engine):
    """A station that never reached 80°F has no heat ledger — it does not
    have a heat ledger full of zeros."""
    db = engine
    _seed(db, MAC, _run(2025, date(2025, 1, 1), [(200, 62.0)]))
    out = _heat()
    assert out["stories"] == []
    assert out["declined"] == ["how_hot_is_hot", *OTHER_CLIMATE]


# ───────────────────── the to-date comparison window ─────────────────────

def test_a_running_year_compares_against_the_same_window(client, monkeypatch):
    """The invariant the whole to-date ledger exists for: eight months of
    2026 must be compared with EIGHT months of 2025, never twelve. 2025
    below ends the year with 160 days ≥100°F but had only 60 by Aug 30."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: date(2026, 8, 30))

    # 2026: Jan 1 → Aug 30 is 242 days; 121 of them at 101°F.
    _seed(db, MAC, _run(2026, date(2026, 1, 1), [(121, 101.0), (121, 70.0)]))
    # 2025: 60 hot days before Aug 31, another 100 after it.
    _seed(db, MAC, _run(2025, date(2025, 1, 1), [(60, 101.0), (182, 70.0)]))
    _seed(db, MAC, _run(2025, date(2025, 8, 31), [(100, 101.0), (23, 70.0)]))

    s = _heat()["stories"][0]
    assert s["period"]["label"] == "2026 so far"
    assert s["period"]["partial"] is True and s["period"]["end"] == "2026-08-30"

    c = s["comparison"]
    assert c["kind"] == "prior_years_to_date"
    assert c["baseline"] == 60.0, "full-year 160 leaked into a to-date baseline"
    # One prior year is quoted as a year, never as an "average".
    assert c["baseline_label"] == "2025 through Aug 30"
    assert c["label"] == "vs the same window in one earlier year"
    assert (c["value"], c["delta"], c["direction"]) == (121, 61.0, "above")
    assert (c["rank"], c["of"]) == (1, 2)

    # Every tier from 80 to 100 was cleared on exactly the same days, so the
    # quotability tie breaks upward — the higher threshold is the bigger claim.
    assert s["hero_line"] == "121 DAYS ≥100°F"
    assert s["context"].endswith(", about one in every two days.")


def test_a_thin_prior_year_stays_out_of_the_baseline(client, monkeypatch):
    """A year the station spent mostly offline has fewer hot days because it
    has fewer days. Letting it into the baseline manufactures records."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    _seed(db, MAC, _run(2024, date(2024, 7, 1), [(40, 70.0)]))   # 40 days only

    s = _heat()["stories"][0]
    # No comparable year survives the coverage check, so the field is absent
    # — not a zero baseline that would read as an all-time record.
    assert s["comparison"] is None
    assert "standout" not in s["score_parts"]


def test_the_ledger_anchor_windows_every_year(client, monkeypatch):
    """insights.assemble's to-date counts, checked at the source."""
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed(db, MAC, _run(2025, date(2025, 1, 1), [(60, 101.0), (182, 70.0)]))
    _seed(db, MAC, _run(2025, date(2025, 8, 31), [(100, 101.0), (23, 70.0)]))

    from app import insights
    payload = asyncio.run(insights.assemble(MAC, today=date(2026, 8, 30)))
    assert payload["ledger_anchor"] == "08-30"
    yr = payload["years"][0]
    assert yr["tiers"]["100"] == 160 and yr["tiers_to_date"]["100"] == 60
    assert yr["days"] == 365 and yr["days_to_date"] == 242
    # Full counts are never smaller than their windowed halves.
    assert all(yr["tiers"][k] >= yr["tiers_to_date"][k] for k in yr["tiers"])


# ───────────────────────── ranking ─────────────────────────

def _fake(name: str, family: str, score: float, *, boom: bool = False,
          decline: bool = False):
    async def fn(ctx):
        if boom:
            raise RuntimeError("producer exploded")
        if decline:
            return []
        return [stories.Story(
            id=f"{family}.{name}", family=family, story_type=name,
            title=name, emoji=None,
            hero=stories.Stat("h", "h", 1, stories.UNIT_DAYS),
            hero_line="H", context="c", comparison=None, supporting=[],
            viz=None,
            period=stories.Period("year", "2026", None, None),
            station={}, interestingness=score)]
    fn.story_family = family
    fn.story_name = name
    return fn


def test_ranking_orders_by_interestingness_and_honours_limit(client, monkeypatch):
    monkeypatch.setattr(stories, "_REGISTRY", [
        _fake("mid", stories.FAMILY_CLIMATE, 0.5),
        _fake("best", stories.FAMILY_RECORDS, 0.9),
        _fake("good", stories.FAMILY_SCIENCE, 0.7),
    ])
    out = asyncio.run(stories.top_stories(MAC, limit=2))
    assert [s["title"] for s in out["stories"]] == ["best", "good"]
    assert out["candidates"] == 3          # ranking truncates, production doesn't


def test_a_broken_producer_does_not_empty_the_section(client, monkeypatch):
    monkeypatch.setattr(stories, "_REGISTRY", [
        _fake("boom", stories.FAMILY_SKY, 0.9, boom=True),
        _fake("quiet", stories.FAMILY_SKY, 0.4, decline=True),
        _fake("fine", stories.FAMILY_CLIMATE, 0.3),
    ])
    out = asyncio.run(stories.top_stories(MAC))
    assert [s["title"] for s in out["stories"]] == ["fine"]
    # Declining is reported; crashing is a bug and is logged, not reported.
    assert out["declined"] == ["quiet"]


def test_min_score_and_family_filters(client, monkeypatch):
    monkeypatch.setattr(stories, "_REGISTRY", [
        _fake("weak", stories.FAMILY_CLIMATE, 0.1),
        _fake("strong", stories.FAMILY_RECORDS, 0.8),
    ])
    out = asyncio.run(stories.top_stories(MAC, min_score=0.5))
    assert [s["title"] for s in out["stories"]] == ["strong"]
    # A family filter skips the producer entirely — it never runs, so it is
    # not reported as having declined.
    out = asyncio.run(stories.top_stories(MAC, families=["records"]))
    assert [s["title"] for s in out["stories"]] == ["strong"]
    assert out["declined"] == []


def test_the_registry_rejects_an_unknown_family():
    with pytest.raises(ValueError):
        stories.producer("weather-facts", "nope")
    assert [f for f, _ in stories.registered()] != []


# ───────────────────── copy + unit invariants ─────────────────────

def test_thresholds_state_the_comparison_honestly(engine):
    """The ledger counts `hi >= tier`, so no rendered string may say a day
    was "above" a threshold it merely reached. A card shipped saying
    "above 113°" while counting 113; this is the guard."""
    db = engine
    _seed(db, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    _seed(db, MAC, _run(2024, date(2024, 1, 1), [(50, 101.0), (190, 70.0)]))
    s = _heat()["stories"][0]
    rendered = [s["hero_line"], s["context"], s["hero"]["label"],
                s["comparison"]["label"], s["comparison"]["baseline_label"]]
    rendered += [x["label"] for x in s["supporting"]]
    rendered += [b["label"] for b in s["viz"]["series"]]
    joined = " ".join(rendered)
    assert "above" not in joined.lower(), joined
    assert "≥" in joined
    # The percentile streak names both the number and what it is.
    streak = next((x for x in s["supporting"]
                   if x["key"] == "longest_p90_streak"), None)
    assert streak is not None
    assert "≥" in streak["label"] and "90th-percentile" in streak["label"]


def test_every_stat_carries_a_native_unit(engine):
    """Units are stored API-native. Temperatures leave here as °F with the
    "F" token; the client converts, and nothing in the engine compares a
    threshold against a converted value."""
    db = engine
    _seed(db, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    s = _heat()["stories"][0]
    from app.insights import LEDGER_TIERS
    assert s["viz"]["unit"] == "F"
    assert [b["threshold"] for b in s["viz"]["series"]] == [int(t) for t in LEDGER_TIERS]
    for stat in [s["hero"], *s["supporting"]]:
        assert stat["unit"] in {"F", "days"}, stat
    hottest = next(x for x in s["supporting"] if x["key"] == "hottest")
    assert hottest["unit"] == "F" and hottest["value"] == 112.0


@pytest.mark.parametrize("count,total,expected", [
    (108, 240, "nearly one in every two days"),
    (121, 243, "about one in every two days"),
    (90, 240, "more than one in every three days"),
    (42, 240, "more than one in every six days"),
    (40, 240, "about one in every six days"),
    (196, 240, "82% of them"),
    (240, 240, "every day recorded"),
    (0, 240, ""),
    (5, 0, ""),
])
def test_share_phrase_qualifies_honestly(count, total, expected):
    assert stories._share_phrase(count, total) == expected


def test_percentile_thresholds_are_not_rounded_into_a_lie():
    assert stories._sig(113.0) == "113"
    assert stories._sig(112.6) == "112.6"


# ───────────────────────── the endpoint ─────────────────────────

def _seed_recent_year(db, mac: str = MAC) -> int:
    """A finished year relative to whenever the suite runs, so the endpoint
    tests do not need the pinned clock."""
    year = datetime.now(timezone.utc).date().year - 1
    _seed(db, mac, _run(year, date(year, 1, 1), _LADDER_240))
    return year


def test_endpoint_returns_ranked_stories(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    year = _seed_recent_year(db)
    r = client.get(f"/api/devices/{MAC}/stories?family=climate", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["mac"] == MAC
    assert body["families"] == ["records", "climate", "science", "sky"]
    # The climate family holds both ledgers now; this test is about the
    # endpoint's shape and ranking, so it names the story it wants rather
    # than assuming the family has exactly one member.
    ids = [s["id"] for s in body["stories"]]
    assert f"climate.heat_ledger.{year}" in ids
    heat = next(s for s in body["stories"]
                if s["story_type"] == "heat_ledger")
    assert heat["hero_line"] == "108 DAYS ≥100°F"
    # Ranked: every story arrives in descending interestingness.
    scores = [s["interestingness"] for s in body["stories"]]
    assert scores == sorted(scores, reverse=True)


def test_endpoint_family_filter_and_validation(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed_recent_year(db)
    r = client.get(f"/api/devices/{MAC}/stories?family=climate", headers=H)
    assert r.status_code == 200
    assert {s["story_type"] for s in r.json()["stories"]} == {
        "heat_ledger", "cold_ledger"}
    r = client.get(f"/api/devices/{MAC}/stories?family=sky", headers=H)
    assert r.status_code == 200 and r.json()["stories"] == []
    r = client.get(f"/api/devices/{MAC}/stories?family=bogus", headers=H)
    assert r.status_code == 400


def test_endpoint_is_token_gated_and_flag_gated(client, monkeypatch):
    from app.config import settings
    assert client.get(f"/api/devices/{MAC}/stories").status_code == 401
    assert client.get(f"/api/devices/{MAC}/stories",
                      headers={"Authorization": "Bearer nope"}).status_code == 401
    # INSIGHTS off (the suite's default): the rollups this reads do not exist.
    assert client.get(f"/api/devices/{MAC}/stories",
                      headers=H).status_code == 404
    monkeypatch.setattr(settings, "insights", True)
    assert client.get(f"/api/devices/{MAC}/stories", headers=H).status_code == 200


def test_endpoint_on_an_unknown_station_is_an_empty_list_not_an_error(
        client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    r = client.get("/api/devices/11:22:33:44:55:66/stories", headers=H)
    assert r.status_code == 200
    assert r.json()["stories"] == []
    assert r.json()["declined"] == ALL_PRODUCERS


def test_an_unknown_station_gets_no_sky_stories_from_the_server_location(
        client, monkeypatch):
    """`build_context` falls back to UNNAMED_STATION with device=None and
    `_station_coords` falls back to the server's forecast location, so a
    random MAC with a valid token used to get sunset cards about the
    operator's own backyard, attributed to "This Station". A station the
    server has never heard of — no device row AND no rollups — declines
    everything; a station with rollups but no device row is still real
    and keeps its sky."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "forecast_lat", 33.30)
    monkeypatch.setattr(settings, "forecast_lon", -111.84)
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    r = client.get("/api/devices/11:22:33:44:55:66/stories", headers=H)
    assert r.status_code == 200
    assert r.json()["stories"] == []
    assert r.json()["declined"] == ALL_PRODUCERS

    # Rollups, no device row: the sky is still this station's.
    _seed(db, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    out = _story(families=[stories.FAMILY_SKY], limit=8)
    assert "shrinking_day" not in out["declined"]
    assert "tonights_sky" not in out["declined"]
    assert {s["story_type"] for s in out["stories"]} >= {"daylight",
                                                         "tonights_sky"}


def test_no_user_facing_month_comes_from_strftime():
    """`_MONTHS` exists because %b/%B are locale-dependent and these
    strings ship onto share cards. Nothing else in the module may reach
    for them."""
    import re
    from pathlib import Path
    src = Path(stories.__file__).read_text().splitlines()
    hits = [(i + 1, line.strip()) for i, line in enumerate(src)
            if re.search(r":%b\}|:%B\}|%-?[bB]\b", line)
            and not line.lstrip().startswith("#")]
    assert hits == [], hits


def test_the_hottest_day_is_a_date_in_words_not_iso(engine):
    _seed(engine, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    s = _heat()["stories"][0]
    hottest = next(x for x in s["supporting"] if x["key"] == "hottest")
    assert hottest["label"] == "hottest day · January 1 2025"


# ───────────────────── the contract, as a contract ─────────────────────

# Words that place a card. `monsoon` is NOT here on purpose: the humid-month
# producer EARNS it from the station's own record (a dry place that turns
# sticky) and its neutral rendering contains no monsoon vocabulary at all —
# see the "Monsoon Meter" comment block in app/stories.py.
GEOGRAPHY_WORDS = ("arizona", "chandler", "phoenix", "sonoran", "tucson",
                   "mojave", "haboob", "desert")
# Proper names that happen to contain one of those words. The Chandler
# Burning Index is Craig Chandler's fire-weather index (1983), not the
# city's; the fire card names it the way it names Fosberg.
GEOGRAPHY_ALLOWED_PHRASES = ("Chandler Burning Index",)


def _string_literals(module) -> list[tuple[int, str]]:
    """Every string constant in the module that is NOT a docstring — the
    producers' copy, in the one place it is authored. Comments are not
    in the AST at all, so a Chandler-shaped example in a comment is fine."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(module.__file__).read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings]


def test_producer_copy_names_no_geography():
    """Cards ship to every climate. A producer written in one backyard
    reaches for that backyard's words — "a real desert drought", "the
    Phoenix summer" — and the card reads as a lie in Charleston. Data may
    carry a place name (the NOAA normals station, the owner's station
    name); the producers' OWN words may not. Static over the module's
    string literals, so it covers copy no fixture happens to render."""
    import re
    words = re.compile(r"\b(" + "|".join(GEOGRAPHY_WORDS) + r")\b",
                       re.IGNORECASE)
    allowed = re.compile("|".join(re.escape(p)
                                  for p in GEOGRAPHY_ALLOWED_PHRASES))
    hits = [(line, text) for line, text in _string_literals(stories)
            if words.search(allowed.sub("", text))]
    assert hits == [], hits


def test_the_geography_sweep_sees_through_the_allowed_phrase():
    """The allow-list strips the PHRASE, not the word: a literal that names
    the Chandler Burning Index and then says "in Chandler" still fails."""
    import re
    words = re.compile(r"\b(" + "|".join(GEOGRAPHY_WORDS) + r")\b",
                       re.IGNORECASE)
    allowed = re.compile("|".join(re.escape(p)
                                  for p in GEOGRAPHY_ALLOWED_PHRASES))
    assert not words.search(allowed.sub("", "The Chandler Burning Index reads 155"))
    assert words.search(allowed.sub("", "The Chandler Burning Index, in Chandler"))


def _one_valid_story(engine) -> dict:
    _seed(engine, MAC, _run(2025, date(2025, 1, 1), _LADDER_240))
    out = _heat()
    assert out["stories"], "fixture stopped producing the heat ledger"
    return out


@pytest.mark.parametrize("mutate,expect", [
    # One 0..1 scale across producers: ranking is a sort on it.
    (lambda s: s.__setitem__("interestingness", 1.4), "interestingness"),
    (lambda s: s.__setitem__("interestingness", float("nan")), "interestingness"),
    # A viz row is FLAT: the app's StoryJSON decodes no arrays or objects.
    (lambda s: s["viz"]["series"][0].__setitem__("days", [1, 2]), "series[0].days"),
    (lambda s: s["viz"]["series"][0].__setitem__("meta", {"a": 1}), "series[0].meta"),
    (lambda s: s["viz"]["series"][0].pop("key"), "series[0].key"),
    (lambda s: s["viz"].__setitem__("highlight", [2025]), "viz.highlight"),
    # Absent is None, never NaN (the `?? 0` family in the app).
    (lambda s: s["hero"].__setitem__("value", float("nan")), "hero.value"),
    (lambda s: s["supporting"][0].__setitem__("value", "112"), "supporting[0].value"),
    # The card composes nothing, so words must arrive as words.
    (lambda s: s.__setitem__("hero_line", 108), "hero_line"),
    (lambda s: s.__setitem__("context", ""), "context"),
    (lambda s: s["hero"].__setitem__("label", None), "hero.label"),
    # The period is one of the spans the app can name.
    (lambda s: s["period"].__setitem__("kind", "station"), "period.kind"),
    (lambda s: s["period"].__setitem__("partial", "yes"), "period.partial"),
    (lambda s: s.__setitem__("family", "weather"), "family"),
    (lambda s: s.__setitem__("score_parts", {"x": float("inf")}), "score_parts.x"),
])
def test_the_wire_contract_bites(engine, mutate, expect):
    """`tests/story_contract.py` runs on every story response the suite
    produces (conftest wraps `top_stories`). A checker that names a rule
    and passes regardless is worse than none, so each rule is proven here
    against a real payload with exactly one thing wrong."""
    import copy
    from tests.story_contract import ContractViolation, check_response
    out = copy.deepcopy(_one_valid_story(engine))
    check_response(out)                     # the untouched payload passes
    mutate(out["stories"][0])
    with pytest.raises(ContractViolation, match=re.escape(expect)):
        check_response(out)


def test_a_duplicate_story_id_is_a_contract_violation(engine):
    import copy
    from tests.story_contract import ContractViolation, check_response
    out = copy.deepcopy(_one_valid_story(engine))
    out["stories"].append(copy.deepcopy(out["stories"][0]))
    with pytest.raises(ContractViolation, match="duplicate id"):
        check_response(out)


def test_producer_copy_uses_no_em_dashes():
    """Volney's public-voice rule ([[feedback-email-draft-style]]): no
    em-dashes in anything user-facing. On a share card an em-dash is also
    the first tell on Wikipedia's "signs of AI writing" list. Static over
    the module's string literals, the same sweep as the geography test;
    comments and docstrings may use whatever they like."""
    hits = [(line, text) for line, text in _string_literals(stories)
            if "\u2014" in text]
    assert hits == [], hits
