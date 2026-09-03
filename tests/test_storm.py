"""Storm summary alerts.

Doren's request, 2026-08-17: one notification a set time after the LAST
reported rain, summarising the event rather than pinging during it.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import storm  # noqa: E402


HOUR = 3_600_000
START = 1_787_000_000_000        # arbitrary fixed epoch ms


def _summary(**kw) -> storm.StormSummary:
    base = dict(started_ms=START, ended_ms=START + int(5.8 * HOUR),
                total_in=1.21, peak_rate_in_hr=4.0,
                min_tempf=70.0, max_tempf=80.0, max_gust_mph=25.0)
    base.update(kw)
    return storm.StormSummary(**base)


# ───────────────────────────── rain detection ─────────────────────────────

def test_increment_is_the_rise_in_the_counter():
    assert storm.rain_increment(1.00, 1.03) == pytest.approx(0.03)
    assert storm.rain_increment(0.0, 0.01) == pytest.approx(0.01)


def test_a_counter_reset_is_not_rainfall():
    """dailyrainin resets at midnight and yearlyrainin at New Year. A reset
    read as a negative must not subtract from a total, and — more importantly
    — must not be mistaken for rain when the sign flips back."""
    assert storm.rain_increment(1.21, 0.0) == 0.0
    assert storm.rain_increment(52.0, 0.0) == 0.0


def test_an_absurd_jump_is_ignored():
    """Decode garbage that slipped the ingest bands must not invent a storm."""
    assert storm.rain_increment(0.0, 99.0) == 0.0


def test_a_missing_reading_is_not_rain():
    assert storm.rain_increment(None, 1.0) == 0.0
    assert storm.rain_increment(1.0, None) == 0.0


def test_yearly_counter_is_preferred_over_daily():
    """A storm running past midnight would see dailyrainin reset to zero,
    read as "no rain", close early, and report half the event as a whole one.
    Yearly is monotonic across that boundary."""
    field, value = storm.counter_value(
        {"yearlyrainin": 52.0, "dailyrainin": 1.21})
    assert (field, value) == ("yearlyrainin", 52.0)
    # Falls back when the source only sends a daily total.
    assert storm.counter_value({"dailyrainin": 1.21}) == ("dailyrainin", 1.21)
    # A station with no rain gauge at all has no counter.
    assert storm.counter_value({"tempf": 70.0}) is None


def test_non_finite_counters_are_rejected():
    assert storm.counter_value({"yearlyrainin": float("nan")}) is None
    assert storm.counter_value({"yearlyrainin": float("inf")}) is None
    # A bool is an int subclass in Python and must not read as a counter.
    assert storm.counter_value({"yearlyrainin": True}) is None


# ───────────────────────────── closing the event ───────────────────────────

def test_the_storm_closes_only_after_the_full_quiet_window():
    last = START
    assert not storm.should_close(last, last + 29 * 60_000, 30)
    assert storm.should_close(last, last + 30 * 60_000, 30)
    assert storm.should_close(last, last + 90 * 60_000, 30)


def test_a_storm_that_never_rained_never_closes():
    assert not storm.should_close(None, START, 30)


def test_a_drizzle_is_not_reported():
    """A single bucket tip must not send a "storm summary" of 0.01 inches —
    that is how people learn to ignore the notification that matters."""
    assert not storm.worth_reporting(_summary(total_in=0.01), 0.05)
    assert storm.worth_reporting(_summary(total_in=0.05), 0.05)
    assert storm.worth_reporting(_summary(total_in=1.21), 0.05)


# ───────────────────────────── the message ────────────────────────────────

def test_message_matches_the_requested_format():
    """Pinned against the exact shape Doren asked for."""
    title, body = storm.build_storm_message("Davis", _summary(), "UTC")
    assert title == "Davis Storm Summary"
    lines = body.split("\n")
    assert lines[0].startswith("Time: ") and lines[0].endswith("(5.8h)")
    assert lines[1] == 'Total: 1.21" | Max Rate: 4.00"/h'
    assert lines[2] == "Hi 80°F | Lo 70°F | Gust: 25 mph"


def test_times_are_local_and_lowercase():
    """"4:08pm", not "16:08" and not "4:08PM" — the staleness alerts use a
    24h stamp, this one deliberately does not."""
    # 21:58 UTC → 4:58pm in America/Chicago.
    ms = 1_787_004_000_000
    _, body = storm.build_storm_message("D", _summary(started_ms=ms,
                                                      ended_ms=ms + HOUR),
                                        "America/Chicago")
    assert "pm" in body.split("\n")[0]
    assert "PM" not in body

    # An unknown zone degrades to UTC rather than raising mid-alert.
    _, utc = storm.build_storm_message("D", _summary(), "Not/AZone")
    assert utc.startswith("Time: ")


def test_optional_readings_are_omitted_not_dashed():
    """A station with no wind sensor should not be told its max gust was "—".
    Same absent-is-not-zero rule as the rest of the app."""
    _, body = storm.build_storm_message(
        "D", _summary(max_gust_mph=None, min_tempf=None, max_tempf=None,
                      peak_rate_in_hr=None), "UTC")
    assert "Gust" not in body
    assert "Temps" not in body
    assert "Max Rate" not in body
    # The one line that always survives is the total.
    assert 'Total: 1.21"' in body


def test_a_hostile_device_name_cannot_break_the_email():
    """A newline in a name raises ValueError when EmailMessage builds the
    header, which would break every alert for that device."""
    title, _ = storm.build_storm_message("Bad\nName\r", _summary(), "UTC")
    assert "\n" not in title and "\r" not in title
    assert storm.build_storm_message("", _summary(), "UTC")[0] == "device Storm Summary"


def test_duration_is_never_negative():
    """Out-of-order readings exist in this codebase (the rain guards document
    them), so an end before a start must not print a negative duration."""
    s = _summary(started_ms=START + HOUR, ended_ms=START)
    assert s.duration_hours == 0.0


# ──────────────────── end to end, through the real monitor ─────────────────
#
# The pure functions above prove the logic. These prove the WIRING — that a
# storm actually opens, accumulates, closes and delivers with the right
# numbers pulled from stored history.

import asyncio          # noqa: E402
import importlib        # noqa: E402


@pytest.fixture
def wired(temp_env: str, monkeypatch):
    """Fresh app modules bound to a temp DB, with delivery captured."""
    for mod in ("app.config", "app.db", "app.storm", "app.alerts"):
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import alerts, db
    asyncio.run(db.init_db())

    sent: list[tuple[str, str]] = []

    async def fake_deliver(cfg, subject, body, title, push_body, email_ok=True, **kw):
        sent.append((title, push_body))
        return True

    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    return alerts, db, sent


def _obs(ts_ms: int, yearly: float, **kw):
    row = {"dateutc": ts_ms, "yearlyrainin": yearly}
    row.update(kw)
    return row


def _run_tick(alerts, mac, last_data, now_ms):
    monitor = alerts.AlertMonitor()
    cfg = alerts.EffectiveAlertConfig(
        enabled=True, transport_configured=True,
        recipients=["x@example.com"], default_threshold_min=15.0,
        repeat_hours=0.0, smtp_host="localhost", smtp_port=25,
        smtp_username=None, smtp_password=None, smtp_from="a@example.com",
        smtp_tls=False, smtp_ssl=False, email_scope="all")
    devices = [{"mac": mac, "name": "Davis", "lastData": last_data}]
    asyncio.run(monitor._check_storm_summaries(cfg, devices, now_ms))


def test_a_storm_opens_accumulates_and_reports(wired):
    """The whole cycle: rain starts, rain continues, rain stops, and 30
    minutes later exactly one summary arrives with the real numbers."""
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:01"
    t0 = START

    # Six minutes of rain, 0.2in each, with temps and a gust to summarise.
    rows = [_obs(t0 + i * 60_000, 52.0 + 0.2 * i,
                 tempf=70.0 + i, windgustmph=10.0 + 3 * i,
                 hourlyrainin=0.5 * i)
            for i in range(7)]
    asyncio.run(db.insert_observations(mac, rows))

    # Tick through the rain: each tick sees a higher counter.
    for i in range(7):
        _run_tick(alerts, mac, rows[i], t0 + i * 60_000)
    assert sent == [], "must not report while it is still raining"

    state = asyncio.run(db.get_storm_state(mac))
    assert state["started_ms"] is not None, "a storm should be open"

    # Rain stops. 29 minutes later: still quiet, still nothing.
    dry = _obs(t0 + 6 * 60_000, 52.0 + 0.2 * 6, tempf=76.0)
    _run_tick(alerts, mac, dry, t0 + 35 * 60_000)
    assert sent == []

    # 30 minutes after the LAST rain: exactly one summary.
    _run_tick(alerts, mac, dry, t0 + 36 * 60_000 + 1)
    assert len(sent) == 1
    title, body = sent[0]
    assert title == "Davis Storm Summary"
    assert 'Total: 1.20"' in body          # six increments of 0.20
    assert 'Max Rate: 3.00"/h' in body     # max hourlyrainin over the window
    assert "Hi 76°F | Lo 70°F" in body
    assert "Gust: 28 mph" in body

    # And the event is closed, so it cannot fire twice.
    _run_tick(alerts, mac, dry, t0 + 120 * 60_000)
    assert len(sent) == 1, "a closed storm must not report again"


def test_a_drizzle_closes_silently(wired):
    """Below the floor the event still has to close, or it stays open forever
    and the next real storm is reported as one enormous merged event."""
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:02"
    t0 = START
    rows = [_obs(t0, 10.00, tempf=60.0), _obs(t0 + 60_000, 10.01, tempf=60.0)]
    asyncio.run(db.insert_observations(mac, rows))

    _run_tick(alerts, mac, rows[0], t0)
    _run_tick(alerts, mac, rows[1], t0 + 60_000)
    _run_tick(alerts, mac, rows[1], t0 + 40 * 60_000)

    assert sent == [], "0.01in is not a storm"
    state = asyncio.run(db.get_storm_state(mac)) or {}
    assert state.get("started_ms") is None, "the event must still be closed"


def test_a_dry_station_never_opens_a_storm(wired):
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:03"
    flat = _obs(START, 10.0, tempf=70.0)
    asyncio.run(db.insert_observations(mac, [flat]))
    for i in range(5):
        _run_tick(alerts, mac, flat, START + i * 600_000)
    assert sent == []
    state = asyncio.run(db.get_storm_state(mac)) or {}
    assert state.get("started_ms") is None


def test_switching_counter_field_does_not_fabricate_a_storm(wired):
    """yearly and daily are different scales. A source that reports only daily
    on one tick and yearly on the next must not read as half an inch of rain.

    The values matter. An earlier version went yearly 52.0 → daily 0.10, which
    the `prev_field == field` guard never had to catch: `rain_increment` sees a
    negative delta and returns 0.0 by itself, so deleting the guard left every
    assertion passing. Reversing it to daily 0.10 → yearly 0.60 makes the
    cross-scale delta POSITIVE and under `_MAX_SANE_INCREMENT_IN`, so the field
    guard is the only thing standing between this and an opened storm.

    Realistic, too: a poller that drops `dailyrainin` from a payload and keeps
    reporting `yearlyrainin` produces exactly this sequence.
    """
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:04"
    asyncio.run(db.insert_observations(mac, [_obs(START, 0.60, tempf=70.0)]))

    _run_tick(alerts, mac, {"dateutc": START, "dailyrainin": 0.10}, START)
    state = asyncio.run(db.get_storm_state(mac)) or {}
    assert state.get("counter_field") == "dailyrainin", (
        "precondition: tick one must have stored the daily counter, or the "
        "test is not exercising a field switch at all")

    _run_tick(alerts, mac, {"dateutc": START + 60_000, "yearlyrainin": 0.60},
              START + 60_000)

    state = asyncio.run(db.get_storm_state(mac)) or {}
    assert state.get("started_ms") is None, "field switch must not open a storm"
    assert state.get("counter_field") == "yearlyrainin", (
        "the new field must still be adopted, so the NEXT tick can measure "
        "a real increment against it")
    assert sent == []


def test_a_text_rain_value_does_not_abort_the_alert_tick(wired):
    """SQLite keeps text in a REAL column and the ingest paths store what the
    poller handed them, so a garbled reading can reach `storm_window_stats`
    as a string. `float()` on it used to raise, and the exception escaped the
    whole alert-monitor tick — one bad row would have silenced every alert
    for every device, not just this summary.
    """
    _alerts, db, _sent = wired
    mac = "AA:BB:CC:DD:EE:07"
    asyncio.run(db.insert_observations(mac, [
        _obs(START, 1.00, tempf=70.0, windgustmph=20.0),
        _obs(START + 600_000, 1.40, tempf=72.0, windgustmph=25.0),
        _obs(START + 1_200_000, 1.60, tempf=71.0, windgustmph=22.0),
    ]))

    async def _poison():
        async with db.connect() as conn:
            # Text in every column the summary reads, including the two the
            # SQL aggregates over — MIN/MAX order text ABOVE all numbers, so
            # this also makes MAX(tempf) return a string.
            await conn.execute(
                "UPDATE observations SET yearlyrainin = 'n/a', tempf = 'bad', "
                "windgustmph = '--' WHERE mac = ? AND dateutc_ms = ?",
                (mac, START + 600_000))
            await conn.commit()
    asyncio.run(_poison())

    stats = asyncio.run(db.storm_window_stats(mac, START, START + 1_200_000,
                                              "yearlyrainin"))
    # The bad row is skipped, not fatal, and the increment is measured across
    # the gap between the two rows that are still good.
    assert stats["total_in"] == pytest.approx(0.60)
    assert stats["max_tempf"] == pytest.approx(71.0)
    assert stats["min_tempf"] == pytest.approx(70.0)
    assert stats["max_gust_mph"] == pytest.approx(22.0)

    # And the summary it feeds still formats, which is the failure the caller
    # would actually have seen.
    from app import storm
    summary = storm.StormSummary(
        started_ms=START, ended_ms=START + 1_200_000,
        total_in=stats["total_in"], peak_rate_in_hr=stats["peak_rate_in_hr"],
        min_tempf=stats["min_tempf"], max_tempf=stats["max_tempf"],
        max_gust_mph=stats["max_gust_mph"])
    title, body = storm.build_storm_message("Davis", summary, "America/Phoenix")
    assert "0.60" in body and "71°F" in body


def test_a_downward_counter_revision_is_not_counted_twice():
    """WeatherFlow revises the day's total downward and then climbs again. On
    2026-08-19 a live Tempest went 0.104 -> 0.025 -> 0.123 in half an hour.

    Comparing consecutive readings ignores the drop (correctly) and then
    counts the whole re-climb as new rain — reporting ~0.20in for a storm
    that dropped ~0.12in. Measuring against a high-water mark counts each
    hundredth once, however often the source walks back over it.
    """
    observed = [0.0, 0.101, 0.104, 0.025, 0.026, 0.104, 0.123, 0.11, 0.111]
    peak, total = None, 0.0
    for v in observed:
        inc, peak = storm.counter_progress(peak, v)
        total += inc
    assert total == pytest.approx(0.123, abs=0.001)


def test_a_period_reset_starts_a_new_mark():
    """A drop to ~zero is midnight or New Year, not a revision. Holding the
    peak there would swallow every drop of the new period until it re-passed
    the old total — the failure the revision guard could easily cause."""
    peak, total = None, 0.0
    for v in [1.10, 1.21, 0.0, 0.05, 0.30]:
        inc, peak = storm.counter_progress(peak, v)
        total += inc
    # 1.10 is the opening baseline, not rainfall: (1.21-1.10) + (0.30-0.0).
    assert total == pytest.approx(0.41, abs=0.001)


def test_a_revision_below_the_reset_floor_is_treated_as_a_reset():
    """The two cases are told apart by how far the counter falls. Anything at
    or under two bucket-tips is a rollover."""
    inc, peak = storm.counter_progress(1.50, 0.01)
    assert inc == 0.0 and peak == 0.01      # reset: re-baselined
    inc, peak = storm.counter_progress(1.50, 0.90)
    assert inc == 0.0 and peak == 1.50      # revision: mark held


def test_a_stale_baseline_rebaselines_instead_of_opening_a_storm(wired):
    """Review 2026-08-20: with summaries disabled (or no alert channel) the
    counter baseline freezes; on re-enable, a fortnight's accumulated rise
    under the 2.0in sanity cap read as one fresh increment and opened a
    'storm' back-dated weeks — one summary spanning ~336 hours with the
    whole gap's rain and monthly temperature extremes. A baseline older
    than the staleness cap must rebaseline silently instead."""
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:07"
    t0 = START
    asyncio.run(db.insert_observations(mac, [_obs(t0, 10.0, tempf=70.0)]))
    _run_tick(alerts, mac, _obs(t0, 10.0, tempf=70.0), t0)

    # Two silent weeks in which 0.8in of real weather accumulated.
    t1 = t0 + 14 * 86_400_000
    wet = _obs(t1, 10.8, tempf=75.0)
    asyncio.run(db.insert_observations(mac, [wet]))
    _run_tick(alerts, mac, wet, t1)

    state = asyncio.run(db.get_storm_state(mac)) or {}
    assert state.get("started_ms") is None, \
        "a fortnight-old baseline opened a back-dated storm"
    assert state.get("counter_value") == 10.8, "baseline must move forward"
    assert sent == []

    # And a REAL storm right after still opens normally.
    t2 = t1 + 60_000
    wetter = _obs(t2, 10.9, tempf=74.0)
    asyncio.run(db.insert_observations(mac, [wetter]))
    _run_tick(alerts, mac, wetter, t2)
    state = asyncio.run(db.get_storm_state(mac)) or {}
    assert state.get("started_ms") is not None, "real rain must still open"


# ── storm history (1.9 shareables) ──────────────────────────────────────

def test_storm_history_records_and_lists(client):
    """record_storm keeps the newest 50 per station; the endpoint serves
    them newest-first with every stat the Storm Report card renders."""
    import asyncio

    from app import db

    mac = "AA:BB:CC:00:00:99"

    async def seed():
        for i in range(52):
            await db.record_storm(mac, {
                "started_ms": 1_000_000 + i * 10_000,
                "ended_ms": 1_005_000 + i * 10_000,
                "total_in": 0.1 * (i + 1),
                "peak_rate_in_hr": 1.5,
                "max_gust_mph": 38.0,
                "min_tempf": 68.0, "max_tempf": 75.0})
        return await db.list_storms(mac, limit=50)

    rows = asyncio.run(seed())
    assert len(rows) == 50                      # pruned, newest kept
    assert rows[0]["total_in"] == pytest.approx(5.2)
    assert rows[0]["max_gust_mph"] == 38.0

    r = client.get(f"/api/devices/{mac}/storms?limit=3",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    storms = r.json()["storms"]
    assert len(storms) == 3
    assert storms[0]["ended_ms"] > storms[1]["ended_ms"]
    # Read-gated like every station read.
    assert client.get(f"/api/devices/{mac}/storms").status_code == 401


def test_storm_history_serves_null_peak_rate(client):
    """R12 W2: peak_rate is MAX(hourlyrainin) and SDR/LilyGO stations store
    NULL there while still recording storms (counter-based detection). The
    row must store and serve as null — the iOS decode treats the field as
    optional, and one numeric-coerced 0.0 would be a fake reading."""
    import asyncio

    from app import db

    asyncio.run(db.record_storm("5D:5D:05:00:00:01", {
        "started_ms": 1_787_000_000_000, "ended_ms": 1_787_003_600_000,
        "total_in": 0.42, "peak_rate_in_hr": None,
        "max_gust_mph": None, "min_tempf": None, "max_tempf": None}))
    r = client.get("/api/devices/5D:5D:05:00:00:01/storms",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    row = r.json()["storms"][0]
    assert row["total_in"] == 0.42
    assert row["peak_rate_in_hr"] is None, \
        "a rate the station never measured must serve as null, not 0"


def test_one_sided_temps_still_render():
    """R14: a sensor that came up mid-storm has only one of min/max —
    dropping BOTH silently was absent-is-not-zero backwards."""
    _, body = storm.build_storm_message(
        "D", _summary(min_tempf=None), "UTC")
    assert "Hi 80°F" in body and "Lo" not in body
    _, body = storm.build_storm_message(
        "D", _summary(max_tempf=None), "UTC")
    assert "Lo 70°F" in body and "Hi" not in body


def test_stat_line_fits_a_lock_screen_banner():
    """R14: the "fits one line" promise was pinned by one 32-char fixture,
    not a budget. Worst realistic case — three-digit sub-zero temps and a
    three-digit gust — must stay under ~40 chars so "mph" can't wrap."""
    _, body = storm.build_storm_message(
        "D", _summary(min_tempf=-40.0, max_tempf=-10.0,
                      max_gust_mph=199.0), "UTC")
    stat_line = body.split("\n")[2]
    assert stat_line == "Hi -10°F | Lo -40°F | Gust: 199 mph"
    assert len(stat_line) <= 40


# ── storm-close capture (2.0) ───────────────────────────────────────────
#
# The one set of numbers in this repo that cannot be recomputed later:
# history thinning ages the minute-by-minute rows either side of a storm
# down to one per bucket, so the before/after pair is measured at close or
# never measured at all. These tests pin the windows, the NULL semantics,
# and the migration that reaches an existing database.

# The 1.9 shape of storm_history, verbatim — the table every upgrading
# server already has. Recreated here so the migration is exercised against
# the real predecessor rather than against a table that already has the
# columns.
_LEGACY_STORM_DDL = """
CREATE TABLE storm_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mac             TEXT NOT NULL,
    started_ms      INTEGER NOT NULL,
    ended_ms        INTEGER NOT NULL,
    total_in        REAL NOT NULL,
    peak_rate_in_hr REAL,
    max_gust_mph    REAL,
    min_tempf       REAL,
    max_tempf       REAL
)
"""

CAPTURE_COLS = ("pre_tempf", "post_tempf", "temp_drop_f",
                "pressure_change_inhg", "dew_change_f")


def _columns(db, table: str) -> list[str]:
    async def run():
        async with db.connect() as conn:
            cur = await conn.execute(f"PRAGMA table_info({table})")
            return [r[1] for r in await cur.fetchall()]
    return asyncio.run(run())


def _rewind_to_1_9(db, storms: list[dict]) -> None:
    """Put the database back into its 1.9 shape with real storms in it."""
    async def run():
        async with db.connect() as conn:
            await conn.execute("DROP TABLE storm_history")
            await conn.execute(_LEGACY_STORM_DDL)
            for s in storms:
                await conn.execute(
                    "INSERT INTO storm_history (mac, started_ms, ended_ms, "
                    "total_in, peak_rate_in_hr, max_gust_mph, min_tempf, "
                    "max_tempf) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["mac"], s["started_ms"], s["ended_ms"], s["total_in"],
                     s.get("peak_rate_in_hr"), s.get("max_gust_mph"),
                     s.get("min_tempf"), s.get("max_tempf")))
            await conn.commit()
    asyncio.run(run())


def _storm_rows(db, mac: str) -> list[dict]:
    return asyncio.run(db.list_storms(mac, limit=50))


def _heat_then_storm(db, mac: str, t0: int) -> None:
    """An hour of desert afternoon, a storm, and the hour that followed.

    108°F before, a cold outflow, 84°F after — the motivating example,
    laid down as real observations so the capture reads them the way it
    will read a live storm.
    """
    rows = []
    # The hour before: hot, dry, low pressure, low dew point. The PEAK is
    # 40 minutes before the rain; the reading at the storm's start is
    # already falling, which is exactly why the window is an hour wide.
    for mins, t in ((60, 104.0), (40, 108.0), (20, 106.0), (0, 99.0)):
        rows.append(_obs(t0 - mins * 60_000, 10.00, tempf=t,
                         dewPoint=40.0, baromrelin=29.80))
    # The storm itself: six minutes of rain.
    for i in range(1, 7):
        rows.append(_obs(t0 + i * 60_000, 10.00 + 0.15 * i, tempf=92.0 - 3 * i,
                         dewPoint=60.0, baromrelin=29.88,
                         windgustmph=36.0, hourlyrainin=0.9))
    # The hour after the last rain: the low arrives behind the rain, and
    # the barometer has jumped.
    for mins, t in ((5, 86.0), (20, 84.0), (40, 85.0)):
        rows.append(_obs(t0 + (6 + mins) * 60_000, 10.90, tempf=t,
                         dewPoint=64.0, baromrelin=29.92))
    asyncio.run(db.insert_observations(mac, rows))


def _tick_through(alerts, mac: str, t0: int) -> None:
    """Drive the monitor across the storm above and out the far side.

    The DRY tick at t0 matters: the tracker back-dates a storm's start to
    the previous reading it saw, so a monitor that only wakes up once the
    rain is already falling would place the start a minute late and pull a
    wet reading into the "before" window.
    """
    _run_tick(alerts, mac, _obs(t0, 10.00, tempf=99.0), t0)
    for i in range(1, 7):
        _run_tick(alerts, mac,
                  _obs(t0 + i * 60_000, 10.00 + 0.15 * i,
                       tempf=92.0 - 3 * i, windgustmph=36.0),
                  t0 + i * 60_000)
    _run_tick(alerts, mac, _obs(t0 + 6 * 60_000, 10.90, tempf=85.0),
              t0 + 46 * 60_000 + 1)


def test_the_capture_columns_migrate_onto_an_existing_database(wired):
    """A server upgrading from 1.9 already HAS storm_history with rows in
    it. CREATE IF NOT EXISTS never reaches an existing table, so the ALTER
    list is the only path — the same rule the alert_prefs columns pay for.
    """
    _alerts, db, _sent = wired
    mac = "AA:BB:CC:DD:EE:20"
    _rewind_to_1_9(db, [{"mac": mac, "started_ms": START,
                         "ended_ms": START + HOUR, "total_in": 0.42,
                         "max_gust_mph": 31.0, "min_tempf": 70.0,
                         "max_tempf": 88.0}])
    assert not set(CAPTURE_COLS) & set(_columns(db, "storm_history")), \
        "precondition: the fixture must be back in its 1.9 shape"

    asyncio.run(db.init_db())
    assert set(CAPTURE_COLS) <= set(_columns(db, "storm_history"))

    # The storm that predates the capture keeps every number it had...
    row = _storm_rows(db, mac)[0]
    assert (row["total_in"], row["max_gust_mph"]) == (0.42, 31.0)
    # ...and NULLs, forever, on everything nobody measured for it.
    assert all(row[c] is None for c in CAPTURE_COLS)


def test_the_migration_is_idempotent(wired):
    """init_db runs on every boot. The second one must be a no-op, not a
    duplicate-column error that crash-loops the server."""
    _alerts, db, _sent = wired
    mac = "AA:BB:CC:DD:EE:21"
    _rewind_to_1_9(db, [{"mac": mac, "started_ms": START,
                         "ended_ms": START + HOUR, "total_in": 1.10}])
    for _ in range(3):
        asyncio.run(db.init_db())
    cols = _columns(db, "storm_history")
    assert len(cols) == len(set(cols)), "a column must not be added twice"
    assert set(CAPTURE_COLS) <= set(cols)
    assert len(_storm_rows(db, mac)) == 1, "the migration must not lose rows"


def test_the_backfill_fills_only_storms_whose_observations_survive(wired):
    """The one-time pass re-runs the real windowed reads. A storm whose
    hours are still on disk gets its capture; a storm whose hours have been
    thinned away keeps NULLs. Nothing is estimated in either case."""
    _alerts, db, _sent = wired
    kept, gone = "AA:BB:CC:DD:EE:22", "AA:BB:CC:DD:EE:23"
    t0 = START
    _heat_then_storm(db, kept, t0)
    _rewind_to_1_9(db, [
        {"mac": kept, "started_ms": t0, "ended_ms": t0 + 6 * 60_000,
         "total_in": 0.90},
        # Same shape, no observations anywhere near it — the thinned case.
        {"mac": gone, "started_ms": t0 - 400 * HOUR,
         "ended_ms": t0 - 399 * HOUR, "total_in": 0.90},
    ])
    asyncio.run(db.init_db())

    filled = _storm_rows(db, kept)[0]
    assert filled["pre_tempf"] == 108.0
    assert filled["post_tempf"] == 84.0
    assert filled["temp_drop_f"] == 24.0

    empty = _storm_rows(db, gone)[0]
    assert all(empty[c] is None for c in CAPTURE_COLS), \
        "a storm whose raw hours are gone must never be estimated"


def test_the_capture_is_taken_when_the_storm_closes(wired):
    """End to end through the real monitor: the summary fires and the
    before/after pair lands on the storm row in the same write."""
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:24"
    t0 = START
    _heat_then_storm(db, mac, t0)
    _tick_through(alerts, mac, t0)
    assert len(sent) == 1, "the summary itself must be unchanged"

    row = _storm_rows(db, mac)[0]
    assert row["pre_tempf"] == 108.0, "the hour's PEAK, not the reading at t0"
    assert row["post_tempf"] == 84.0
    assert row["temp_drop_f"] == 24.0
    assert row["pressure_change_inhg"] == pytest.approx(0.12, abs=1e-6)
    assert row["dew_change_f"] == pytest.approx(24.0, abs=1e-6)


def test_the_summary_notification_is_untouched_by_the_capture(wired):
    """Three rounds of wording went into that third line. Capturing five
    new numbers must not move one character of it."""
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:25"
    t0 = START
    _heat_then_storm(db, mac, t0)
    _tick_through(alerts, mac, t0)
    _title, body = sent[0]
    # The summary's Hi/Lo are the extremes INSIDE the rain window, which is
    # a different question from the before/after pair — and it keeps the
    # answer it has always given.
    assert body.split("\n")[2] == "Hi 99°F | Lo 74°F | Gust: 36 mph"


def test_the_before_window_stops_at_one_hour(wired):
    """The window is an hour and the hour is permanent. A reading ninety
    minutes before the rain is the afternoon, not the storm's "before"."""
    _alerts, db, _sent = wired
    mac = "AA:BB:CC:DD:EE:26"
    t0 = START
    _heat_then_storm(db, mac, t0)
    # A hotter reading, safely outside the window.
    asyncio.run(db.insert_observations(mac, [
        _obs(t0 - 90 * 60_000, 10.00, tempf=118.0, dew_point=38.0,
             baromrelin=29.78)]))
    cap = asyncio.run(db.storm_close_capture(
        mac, t0, t0 + 6 * 60_000, t0 + 66 * 60_000))
    assert cap["pre_tempf"] == 108.0


def test_the_last_wet_reading_is_not_the_storms_own_after(wired):
    """`ended_ms` is the storm's final RAINING reading. Folding it into the
    after-window would let the episode supply its own post-storm low."""
    _alerts, db, _sent = wired
    mac = "AA:BB:CC:DD:EE:27"
    t0 = START
    asyncio.run(db.insert_observations(mac, [
        _obs(t0, 10.0, tempf=100.0),
        _obs(t0 + 60_000, 10.5, tempf=55.0),      # the last WET reading
        _obs(t0 + 10 * 60_000, 10.5, tempf=80.0),
    ]))
    cap = asyncio.run(db.storm_close_capture(
        mac, t0, t0 + 60_000, t0 + 40 * 60_000))
    assert cap["post_tempf"] == 80.0, "55°F was during the storm, not after"
    assert cap["temp_drop_f"] == 20.0


def test_a_station_with_no_barometer_captures_no_pressure_change(wired):
    """Absent is not zero: a change nobody could measure is NULL, and a
    NULL is what makes the story decline instead of drawing a flat line."""
    _alerts, db, _sent = wired
    mac = "AA:BB:CC:DD:EE:28"
    t0 = START
    asyncio.run(db.insert_observations(mac, [
        _obs(t0 - 10 * 60_000, 10.0, tempf=101.0),
        _obs(t0 + 20 * 60_000, 10.6, tempf=79.0),
    ]))
    cap = asyncio.run(db.storm_close_capture(
        mac, t0, t0 + 60_000, t0 + 40 * 60_000))
    assert cap["temp_drop_f"] == 22.0
    assert cap["pressure_change_inhg"] is None
    assert cap["dew_change_f"] is None


def test_a_storm_with_no_surviving_observations_captures_nothing(wired):
    _alerts, db, _sent = wired
    cap = asyncio.run(db.storm_close_capture(
        "AA:BB:CC:DD:EE:29", START, START + HOUR, START + 2 * HOUR))
    assert all(v is None for v in cap.values())


def test_the_capture_serves_through_the_storms_endpoint(client):
    """The card reads these off /storms, so the columns have to make the
    trip — and a NULL has to arrive as null, not as a coerced 0."""
    import asyncio as _a

    from app import db

    mac = "AA:BB:CC:DD:EE:2A"
    _a.run(db.record_storm(mac, {
        "started_ms": START, "ended_ms": START + HOUR, "total_in": 0.90,
        "peak_rate_in_hr": 1.8, "max_gust_mph": 36.0,
        "min_tempf": 74.0, "max_tempf": 108.0,
        "pre_tempf": 108.0, "post_tempf": 84.0, "temp_drop_f": 24.0,
        "pressure_change_inhg": 0.12, "dew_change_f": None}))
    r = client.get(f"/api/devices/{mac}/storms",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    row = r.json()["storms"][0]
    assert (row["pre_tempf"], row["post_tempf"]) == (108.0, 84.0)
    assert row["temp_drop_f"] == 24.0
    assert row["dew_change_f"] is None


def test_a_failed_capture_still_records_the_storm(wired, monkeypatch):
    """The capture is enrichment; the row is the record. The storm state
    is cleared before either runs, so a capture that raises had been
    taking the whole storm_history row down with it — permanently, since
    nothing retries a closed storm (CodeRabbit, PR #35)."""
    alerts, db, sent = wired
    mac = "AA:BB:CC:DD:EE:2B"
    t0 = START

    async def boom(*_a, **_kw):
        raise RuntimeError("capture exploded")
    monkeypatch.setattr(db, "storm_close_capture", boom)

    _heat_then_storm(db, mac, t0)
    _tick_through(alerts, mac, t0)
    assert len(sent) == 1, "the summary itself must still go out"
    rows = _storm_rows(db, mac)
    assert len(rows) == 1, "the storm row must survive the capture failure"
    row = rows[0]
    assert row["total_in"] == pytest.approx(0.90)
    assert row["max_gust_mph"] == 36.0
    # Absent is not zero: the pair nobody measured is NULL, not 0.
    assert row["pre_tempf"] is None and row["temp_drop_f"] is None


def test_record_storm_placeholders_follow_the_capture_columns(wired, monkeypatch):
    """The INSERT's column list is generated from _STORM_CAPTURE_COLS; its
    VALUES list must be too, or the next capture column added raises an
    OperationalError at the moment every summary is delivered. Pinned by
    shrinking the tuple: a hard-coded 13 placeholders against 12 columns
    fails the insert."""
    _alerts, db, _sent = wired
    mac = "AA:BB:CC:DD:EE:2C"
    monkeypatch.setattr(db, "_STORM_CAPTURE_COLS", db._STORM_CAPTURE_COLS[:-1])
    asyncio.run(db.record_storm(mac, {
        "started_ms": START, "ended_ms": START + HOUR, "total_in": 0.40,
        "max_gust_mph": 20.0, "min_tempf": 70.0, "max_tempf": 90.0,
        "pre_tempf": 90.0, "post_tempf": 75.0, "temp_drop_f": 15.0}))
    row = _storm_rows(db, mac)[0]
    assert row["total_in"] == pytest.approx(0.40)
    assert row["pre_tempf"] == 90.0 and row["temp_drop_f"] == 15.0
