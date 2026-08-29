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
