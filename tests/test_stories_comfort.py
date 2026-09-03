"""The Comfortable Months: which month feels best when a person is actually
outside. The rules this file pins:

  · only WAKING hours count (7 am up to 10 pm). A July whose nights are
    lovely is still a July, because nobody is on the patio at 3 am;
  · comfort is a share of FEELS-LIKE readings inside the band, and the
    card prints the band it used, in the reader's units;
  · the record's ranking is the picture; this year's months ride along as
    row notes and one sentence, never as the ranking itself;
  · a month the record has not covered is left out, not drawn at zero;
  · the ledger is folded at ingest and rebuilt with the other rollups, and
    an archive that predates the table gets one rebuild at boot.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app import stories

MAC = "AA:BB:CC:00:00:CF"
TODAY = date(2026, 8, 30)

# Waking-hour comfortable share by calendar month: a desert year. Spring
# and autumn are the good months, July is the worst, and April edges March.
RECORD_SHARE = {1: 0.40, 2: 0.55, 3: 0.75, 4: 0.85, 5: 0.60, 6: 0.25,
                7: 0.08, 8: 0.12, 9: 0.30, 10: 0.70, 11: 0.65, 12: 0.45}
# This year so far (through August): a cool spring made MARCH the best.
THIS_YEAR_SHARE = {1: 0.35, 2: 0.50, 3: 0.85, 4: 0.70, 5: 0.55, 6: 0.20,
                   7: 0.05, 8: 0.10}


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    return db


def _story(mac: str = MAC, **kw):
    return asyncio.run(stories.top_stories(mac, limit=12, **kw))


def _card(out):
    return next((s for s in out["stories"]
                 if s["story_type"] == "comfort_months"), None)


def _seed(db, years: dict[int, dict[int, float]], *, mac: str = MAC,
          n: int = 300, days_per_month: int = 28,
          night_share: float | None = None,
          skip_days: set[tuple[int, int]] = frozenset()) -> None:
    """`years[year][month]` is the WAKING-hour comfortable share. Every
    waking hour gets `n` readings at that share (the rest split hot in
    summer, cold in winter); the small hours get `night_share` when given.
    Daily rollup rows are seeded so a month counts as measured."""
    async def run():
        async with db.connect() as conn:
            for year, months in years.items():
                for month, share in months.items():
                    for hour in range(24):
                        waking = hour in stories.WAKING_HOURS
                        sh = share if waking else night_share
                        if sh is None:
                            continue
                        comfy = round(n * sh)
                        rest = n - comfy
                        hot, cold = (rest, 0) if month in (5, 6, 7, 8, 9) \
                            else (0, rest)
                        await conn.execute(
                            "INSERT OR REPLACE INTO comfort_rollups "
                            "(mac, year, month, hour, n, comfortable_n, "
                            "hot_n, cold_n) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (mac, year, month, hour, n, comfy, hot, cold))
                    if (year, month) in skip_days:
                        continue
                    for d in range(1, days_per_month + 1):
                        day = date(year, month, d)
                        if day > TODAY:
                            break
                        await conn.execute(
                            "INSERT OR REPLACE INTO daily_rollups "
                            "(mac, day, tempf_min, tempf_max, tempf_sum, "
                            "tempf_n) VALUES (?, ?, ?, ?, ?, ?)",
                            (mac, day.isoformat(), 60.0, 95.0, 80.0, 1))
            await conn.commit()
    asyncio.run(run())


def _desert(db, **kw):
    _seed(db, {2024: RECORD_SHARE, 2025: RECORD_SHARE,
               2026: THIS_YEAR_SHARE}, **kw)


# ───────────────────────── the ranking ─────────────────────────

def test_the_best_month_leads_and_the_bars_rank_the_record(engine):
    _desert(engine)
    s = _card(_story())
    assert s is not None
    assert s["hero_line"] == "APRIL IS THE MONTH TO BE OUTSIDE"
    labels = [r["label"] for r in s["viz"]["series"]]
    assert labels[:3] == ["Apr", "Mar", "Oct"]
    assert labels[-1] == "Jul"
    assert [r["hero"] for r in s["viz"]["series"]].count(True) == 1
    assert s["viz"]["series"][0]["hero"] is True
    assert s["viz"]["highlight_key"] == "m04"


def test_hours_a_day_are_the_share_of_a_fifteen_hour_day(engine):
    _desert(engine)
    s = _card(_story())
    april = s["viz"]["series"][0]
    # Two record years at 0.85 and this year at 0.70, weighted by readings.
    assert april["share"] == pytest.approx((0.85 + 0.85 + 0.7) / 3, abs=1e-3)
    assert april["hours"] == pytest.approx(april["share"] * 15, abs=0.06)
    assert s["hero"]["value"] == april["hours"]
    assert s["hero"]["unit"] == "h" and s["hero"]["precision"] == 1
    assert s["viz"]["domain_max"] == 15.0


def test_the_small_hours_do_not_count(engine):
    """July's nights are perfect and July is still last: the question is
    about the hours a person is outside."""
    _desert(engine, night_share=1.0)
    s = _card(_story())
    assert s["viz"]["series"][-1]["label"] == "Jul"
    assert s["viz"]["series"][-1]["share"] < 0.1


def test_this_year_rides_along_as_notes_and_one_sentence(engine):
    _desert(engine)
    s = _card(_story())
    rows = {r["label"]: r for r in s["viz"]["series"]}
    assert rows["Mar"]["note"] == "12.8 h in 2026"
    assert "note" not in rows["Oct"]          # this year has not reached it
    assert ("So far in 2026, March has been the most comfortable month, "
            "at 12.8 hours a day." in s["context"])
    best_now = next(x for x in s["supporting"] if x["key"] == "best_this_year")
    assert best_now["label"] == "best so far in 2026 · March"
    assert best_now["value"] == 12.8


def test_when_this_year_agrees_the_sentence_says_again(engine):
    agree = {**THIS_YEAR_SHARE, 3: 0.6, 4: 0.9}
    _seed(engine, {2024: RECORD_SHARE, 2025: RECORD_SHARE, 2026: agree})
    s = _card(_story())
    assert "So far in 2026 it is April again, at 13.5 hours a day." in s["context"]


def test_the_worst_month_says_why(engine):
    _desert(engine)
    s = _card(_story())
    worst = next(x for x in s["supporting"] if x["key"] == "worst_month")
    assert worst["label"] == "worst month · July"
    assert worst["unit"] == "h" and 0.9 < worst["value"] < 1.3
    assert (f"July is the other end at {worst['value']}, the rest too hot."
            in s["context"])


def test_the_card_states_its_definition_in_the_readers_units(engine):
    _desert(engine)
    native = _card(_story())
    assert "between 60°F and 80°F" in native["context"]
    assert "between 7 am and 10 pm" in native["viz"]["footnote"]
    metric = _card(_story(units=stories.Units(temperature="celsius")))
    assert "between 16°C and 27°C" in metric["context"]
    assert "°F" not in metric["context"] and "°F" not in metric["viz"]["footnote"]
    # Hours are hours in every scale.
    assert metric["hero"]["value"] == native["hero"]["value"]


# ───────────────────────── declines ─────────────────────────

def test_declines_with_no_comfort_ledger(engine):
    out = _story()
    assert _card(out) is None
    assert "comfortable_months" in out["declined"]


def test_a_month_the_record_has_not_covered_is_left_out(engine):
    """A month with five measured days across the record draws no bar,
    rather than a bar from five days."""
    _seed(engine, {2024: RECORD_SHARE, 2025: RECORD_SHARE, 2026: THIS_YEAR_SHARE},
          skip_days={(2024, 10), (2025, 10)})
    s = _card(_story())
    assert "Oct" not in [r["label"] for r in s["viz"]["series"]]


def test_declines_when_fewer_than_six_months_rank(engine):
    _seed(engine, {2025: {m: RECORD_SHARE[m] for m in (3, 4, 5, 6, 7)}})
    assert _card(_story()) is None


def test_this_year_needs_ten_measured_days_to_be_quoted(engine):
    _seed(engine, {2024: RECORD_SHARE, 2025: RECORD_SHARE,
                   2026: THIS_YEAR_SHARE}, skip_days={(2026, 3)})
    s = _card(_story())
    rows = {r["label"]: r for r in s["viz"]["series"]}
    assert "note" not in rows["Mar"]
    assert "So far in 2026 it is April again" in s["context"]


# ───────────────────────── score ─────────────────────────

def test_a_flat_climate_is_less_of_a_story(engine):
    _desert(engine)
    desert = _card(_story())["interestingness"]
    _seed(engine, {2024: {m: 0.5 for m in range(1, 13)},
                   2025: {m: 0.52 for m in range(1, 13)}},
          mac="AA:BB:CC:00:00:D0")
    flat = _card(_story("AA:BB:CC:00:00:D0"))["interestingness"]
    assert 0.0 <= flat <= 1.0 and 0.0 <= desert <= 1.0
    assert flat < 0.15 < desert


# ───────────────────────── the ledger itself ─────────────────────────

def _ms(year, month, day, hour_local, tz="America/Phoenix") -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return int(datetime(year, month, day, hour_local,
                        tzinfo=ZoneInfo(tz)).timestamp() * 1000)


def test_ingest_folds_feels_like_into_the_ledger(engine, monkeypatch):
    from app import insights
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    rows = [
        {"dateutc": _ms(2026, 4, 10, 15), "tempf": 75.0, "feelsLike": 74.0},
        {"dateutc": _ms(2026, 4, 10, 16), "tempf": 88.0, "feelsLike": 91.0},
        {"dateutc": _ms(2026, 4, 11, 7), "tempf": 48.0, "feelsLike": 44.0},
        {"dateutc": _ms(2026, 4, 11, 8), "tempf": 60.0, "feelsLike": 60.0},
        {"dateutc": _ms(2026, 4, 11, 9), "tempf": 80.0, "feelsLike": 80.0},
        {"dateutc": _ms(2026, 4, 11, 10), "tempf": 70.0, "feelsLike": None},
    ]

    async def run():
        async with engine.connect() as conn:
            await insights.update_rollups(conn, MAC, rows)
            await conn.commit()
            got = await (await conn.execute(
                "SELECT year, month, hour, n, comfortable_n, hot_n, cold_n "
                "FROM comfort_rollups WHERE mac = ? ORDER BY hour",
                (MAC,))).fetchall()
        return [tuple(r) for r in got]
    got = asyncio.run(run())
    assert got == [
        (2026, 4, 7, 1, 0, 0, 1),
        (2026, 4, 8, 1, 1, 0, 0),      # 60°F is inside the band
        (2026, 4, 9, 1, 1, 0, 0),      # so is 80°F
        (2026, 4, 15, 1, 1, 0, 0),
        (2026, 4, 16, 1, 0, 1, 0),
    ]                                   # the None feels-like left no row


def test_rebuild_refolds_the_ledger_from_history(engine, monkeypatch):
    from app import insights
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")

    async def run():
        await engine.insert_observations(MAC, [
            {"dateutc": _ms(2025, 10, 5, 12), "tempf": 72.0, "feelsLike": 72.0},
            {"dateutc": _ms(2025, 10, 5, 13), "tempf": 92.0, "feelsLike": 95.0},
        ])
        async with engine.connect() as conn:
            # A stale row the rebuild must not keep.
            await conn.execute(
                "INSERT INTO comfort_rollups (mac, year, month, hour, n, "
                "comfortable_n, hot_n, cold_n) VALUES (?, 1999, 1, 1, 9, 9, 0, 0)",
                (MAC,))
            await conn.commit()
        await insights.rebuild()
        async with engine.connect() as conn:
            got = await (await conn.execute(
                "SELECT year, month, hour, n, comfortable_n, hot_n, cold_n "
                "FROM comfort_rollups WHERE mac = ? ORDER BY year, hour",
                (MAC,))).fetchall()
        return [tuple(r) for r in got]
    assert asyncio.run(run()) == [(2025, 10, 12, 1, 1, 0, 0),
                                  (2025, 10, 13, 1, 0, 1, 0)]


def test_an_archive_that_predates_the_ledger_gets_one_rebuild(engine):
    """Dropping the table stands in for a 1.9 database: init_db recreates
    it and marks the rollups dirty, and only when there is history to
    fold. A fresh database stays clean."""
    async def run(with_history: bool):
        async with engine.connect() as conn:
            await conn.execute("DROP TABLE comfort_rollups")
            await conn.execute("DELETE FROM server_kv WHERE k = 'rollups_dirty'")
            await conn.execute("DELETE FROM observations")
            await conn.commit()
        if with_history:
            await engine.insert_observations(MAC, [
                {"dateutc": _ms(2025, 10, 5, 12), "tempf": 72.0}])
        await engine.init_db()
        return await engine.get_kv("rollups_dirty")
    assert asyncio.run(run(False)) is None
    assert asyncio.run(run(True)) is not None


def test_a_rebuild_yields_the_writer_between_batches(engine, monkeypatch):
    """The rebuild commits per batch AND sleeps after each one, so ingest
    gets the lock in between. On 2026-09-02 the first comfort-ledger fold
    on Volney's box answered 503 to every ingest for its whole run because
    the loop re-took the lock the instant it committed."""
    from app import insights
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    monkeypatch.setattr(insights, "REBUILD_BATCH_ROWS", 2)
    pauses: list[float] = []

    async def fake_sleep(seconds):
        pauses.append(seconds)
    monkeypatch.setattr(insights.asyncio, "sleep", fake_sleep)

    async def run():
        await engine.insert_observations(MAC, [
            {"dateutc": _ms(2025, 10, 5, h), "tempf": 70.0, "feelsLike": 70.0}
            for h in range(7, 12)])                       # five rows, three batches
        return await insights.rebuild()
    stats = asyncio.run(run())
    assert stats["rows"] == 5
    # Other module loops sleep too (an hour at a time); only the batch
    # pause is counted.
    assert pauses.count(insights.REBUILD_BATCH_PAUSE_S) == 3
    assert insights.REBUILD_BATCH_PAUSE_S >= 0.5


def test_a_densely_polled_year_does_not_outvote_the_others(engine):
    """R18 finding 1. Two record years at 90% with twelve readings an hour and
    this year at 10% with sixty an hour: pooled counts said 33%; clock time
    says 63%. Every covered year weighs the same."""
    _seed(engine, {2024: {m: 0.9 for m in range(1, 13)},
                   2025: {m: 0.9 for m in range(1, 13)}}, n=12)
    _seed(engine, {2026: {m: 0.1 for m in range(1, 9)}}, n=60)
    s = _card(_story())
    april = next(r for r in s["viz"]["series"] if r["label"] == "Apr")
    # The fixture rounds readings to whole numbers: 11 of 12 and 6 of 60.
    assert april["share"] == pytest.approx((11 / 12 + 11 / 12 + 6 / 60) / 3, abs=1e-3)
    assert april["note"] == "1.5 h in 2026"


def test_the_year_count_is_the_months_own(engine):
    """A month measured in one year says one year, whatever the record's
    span. October here exists only in 2024."""
    _seed(engine, {2024: RECORD_SHARE, 2025: {m: v for m, v in RECORD_SHARE.items() if m != 10},
                   2026: THIS_YEAR_SHARE})
    # Make October the best month so the sentence is about it.
    _seed(engine, {2024: {10: 0.95}})
    s = _card(_story())
    assert s["hero_line"] == "OCTOBER IS THE MONTH TO BE OUTSIDE"
    assert "averaged over 1 year of record" in s["context"]


def test_an_hour_with_too_few_readings_has_no_share(engine):
    """Two readings in an hour are not an hour; below the floor the hour is
    left out of the month's mean rather than swinging it."""
    _seed(engine, {2024: RECORD_SHARE, 2025: RECORD_SHARE, 2026: THIS_YEAR_SHARE})
    _seed(engine, {2025: {4: 0.0}}, n=2)          # April 2025 rewritten as 2-reading hours at 0%
    s = _card(_story())
    april = next(r for r in s["viz"]["series"] if r["label"] == "Apr")
    # 2025's April dropped out entirely (no covered hours): the mean is over 2024 and 2026.
    assert april["share"] == pytest.approx((0.85 + 0.7) / 2, abs=1e-3)
