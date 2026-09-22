"""The rolling 24 hours of source health (2.4, item 3).

`source_status` says whether a poller is working right now and forgets
it at a restart. This is the record that answers the question people
actually ask after a gap in their charts, which is what happened
overnight and whose fault it was.

The three verdicts are the point. A station that stops updating looks
identical in every app whatever the cause, and the fix is completely
different in each case.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import health_watch, source_history as sh  # noqa: E402

HOUR = 3_600_000
H = {"Authorization": "Bearer test-api-token"}


NOW = 1_700_000_000_000


def _row(**kw):
    base = dict(name="tempest", label="Tempest", configured=True,
                consecutive_failures=0, last_success_ms=NOW - 60_000,
                last_rows=3, last_rows_ms=NOW - 60_000, last_error_kind=None)
    base.update(kw)
    return base


def test_the_verdict_says_where_the_chain_broke():
    # Readings arrived.
    assert sh.verdict_for(NOW, _row()) == sh.OK
    # The service did not answer, or refused the key. Not the owner's
    # hardware and not ours.
    assert sh.verdict_for(NOW, _row(consecutive_failures=2,
                               last_error_kind="upstream")) == sh.VENDOR
    assert sh.verdict_for(NOW, _row(consecutive_failures=1,
                               last_error_kind="credentials")) == sh.VENDOR
    assert sh.verdict_for(NOW, _row(consecutive_failures=1,
                               last_error_kind="rate_limit")) == sh.VENDOR
    # It worked and the failure was on this side of the wire.
    assert sh.verdict_for(NOW, _row(consecutive_failures=1,
                               last_error_kind="ours")) == sh.OURS
    # The service answered and had nothing for this station. THE
    # distinction the whole feature exists for.
    assert sh.verdict_for(NOW, _row(last_rows=0,
                                    last_rows_ms=NOW - 20 * 60_000)) == sh.DEVICE
    # A source nobody configured, and one that has not had a first
    # success yet, are both "no opinion" rather than a failure.
    assert sh.verdict_for(NOW, _row(configured=False)) == sh.UNKNOWN
    assert sh.verdict_for(NOW, _row(last_success_ms=None)) == sh.UNKNOWN


def test_one_empty_tick_is_not_a_quiet_station():
    """LIVE on the own box the Tempest strip read `doddddddddddo`: a
    poller ticks every minute against a station that uploads every one
    to five minutes, so roughly every other tick stores nothing, and a
    single empty tick was read as the device gone quiet. With the hour
    painted by its worst minute, one such minute owned the hour."""
    # A row was stored a minute ago and this tick stored nothing: fine.
    assert sh.verdict_for(NOW, _row(last_rows=0,
                                    last_rows_ms=NOW - 60_000)) == sh.OK
    # Nothing stored for twenty minutes while the service answers: quiet.
    assert sh.verdict_for(NOW, _row(last_rows=0,
                                    last_rows_ms=NOW - 20 * 60_000)) == sh.DEVICE
    # A source that has answered for twenty minutes and never once had a
    # row for us is quiet from the moment the grace runs out.
    assert sh.verdict_for(NOW, _row(last_rows=0,
                                    last_rows_ms=NOW - 20 * 60_000,
                                    last_success_ms=NOW)) == sh.DEVICE
    # The grace is the constant, not a coincidence of the fixture.
    edge = NOW - sh.DEVICE_QUIET_MS
    assert sh.verdict_for(NOW, _row(last_rows=0, last_rows_ms=edge)) == sh.OK
    assert sh.verdict_for(NOW, _row(last_rows=0, last_rows_ms=edge - 1)) == sh.DEVICE


def test_the_status_row_remembers_when_a_row_last_arrived():
    """`last_rows_ms` is what the verdict measures the quiet from. It
    moves on a tick that stored rows, holds through empty ticks, and a
    source that never sent a count is given the benefit from its first
    success."""
    from app import source_status as ss
    ss.declare("tempest", True)
    def row():
        return next(r for r in ss.snapshot() if r["name"] == "tempest")
    ss.record_success("tempest", rows=2)
    first = row()["last_rows_ms"]
    assert first is not None
    ss.record_success("tempest", rows=0)
    assert row()["last_rows_ms"] == first
    assert row()["last_rows"] == 0
    # No count at all (an older poller): the success itself is the mark.
    ss.declare("govee", True)
    ss.record_success("govee")
    assert next(r for r in ss.snapshot() if r["name"] == "govee")["last_rows_ms"] is not None


def test_a_steady_source_costs_a_write_a_quarter_hour(client):
    async def run():
        t = 1_700_000_000_000
        await sh.record("tempest", sh.OK, t)
        # Same verdict a minute later: nothing is appended and the run's
        # end does not even move, or a 10 second poller would write on
        # every tick forever.
        await sh.record("tempest", sh.OK, t + 60_000)
        runs = await sh.history("tempest", t + 60_000)
        assert runs == [{"from_ms": t, "until_ms": t, "verdict": "ok"}]
        # Past the keepalive the same run simply grows.
        await sh.record("tempest", sh.OK, t + 16 * 60_000)
        runs = await sh.history("tempest", t + 16 * 60_000)
        assert len(runs) == 1 and runs[0]["until_ms"] == t + 16 * 60_000
        # A different verdict closes the old run and opens a new one.
        await sh.record("tempest", sh.VENDOR, t + 20 * 60_000)
        runs = await sh.history("tempest", t + 20 * 60_000)
        assert [r["verdict"] for r in runs] == ["ok", "vendor"]
        assert runs[0]["until_ms"] == runs[1]["from_ms"]
    asyncio.run(run())


async def _hold(name: str, verdict: str, start: int, end: int) -> None:
    """Stamp a verdict the way the watcher does, once per keep-alive, so
    the run is OBSERVED across the span. A single record an hour later
    used to be read as the hour in between; since R24-05 (2.4 release
    review) silence longer than two keep-alives is a gap, not a run."""
    t = start
    while t < end:
        await sh.record(name, verdict, t)
        t += sh.KEEPALIVE_MS


def test_the_record_only_ever_speaks_for_a_day(client):
    async def run():
        t = 1_700_000_000_000
        await _hold("tempest", sh.OK, t, t + HOUR)
        await sh.record("tempest", sh.VENDOR, t + HOUR)
        # Still inside the window: the older run is clipped to the edge
        # rather than claiming to reach back further than it does.
        later = t + 24 * HOUR + 30 * 60_000
        runs = await sh.history("tempest", later)
        assert [r["verdict"] for r in runs] == ["ok", "vendor"]
        assert runs[0]["from_ms"] == later - sh.WINDOW_MS
        # And a day and a half of silence reads as NOTHING KNOWN, not as
        # a day and a half of whatever was true when the server stopped.
        # The server was not watching; saying anything else would be a
        # lie about a period nobody saw.
        assert await sh.history("tempest", t + 36 * HOUR) == []
    asyncio.run(run())


def test_the_summary_never_claims_a_day_it_did_not_see(client):
    async def run():
        t = 1_700_000_000_000
        await _hold("tempest", sh.OK, t, t + 2 * HOUR)
        await sh.record("tempest", sh.OK, t + 2 * HOUR)
        s = sh.summarise(await sh.history("tempest", t + 2 * HOUR), t + 2 * HOUR)
        # Two hours of record, not a day: the denominator is what was
        # actually watched.
        assert s["covered_ms"] == 2 * HOUR
        assert s["ok_fraction"] == 1.0
        assert s["worst"] is None and s["current"] == "ok"
        assert s["current_since_ms"] == t
    asyncio.run(run())


def test_the_summary_names_the_worst_thing_that_happened(client):
    async def run():
        t = 1_700_000_000_000
        await _hold("ambientweather", sh.OK, t, t + HOUR)
        await _hold("ambientweather", sh.DEVICE, t + HOUR, t + 2 * HOUR)
        await _hold("ambientweather", sh.VENDOR, t + 2 * HOUR, t + 3 * HOUR)
        await sh.record("ambientweather", sh.OK, t + 3 * HOUR)
        runs = await sh.history("ambientweather", t + 3 * HOUR)
        s = sh.summarise(runs, t + 3 * HOUR)
        # ours beats vendor beats device: the worst is whoever has to act
        # most urgently, and ours is the one we can actually fix.
        assert s["worst"] == "vendor"
        assert s["totals_ms"]["device"] == HOUR
        assert s["current"] == "ok"
    asyncio.run(run())


def test_a_corrupt_record_reads_as_no_record(client):
    async def run():
        from app import db
        await db.set_kv("source_health.v1.tempest", "{not json")
        assert await sh.history("tempest", 1) == []
        await db.set_kv("source_health.v1.tempest",
                        '[{"from_ms": "x", "verdict": "ok"}]')
        assert await sh.history("tempest", 1) == []
    asyncio.run(run())


def test_the_outage_mail_says_what_the_day_looked_like(client):
    t = 1_700_000_000_000
    summary = sh.summarise([
        {"from_ms": t, "until_ms": t + 20 * HOUR, "verdict": "ok"},
        {"from_ms": t + 20 * HOUR, "until_ms": t + 22 * HOUR, "verdict": "vendor"},
        {"from_ms": t + 22 * HOUR, "until_ms": t + 23 * HOUR, "verdict": "device"},
    ], t + 23 * HOUR)
    line = health_watch.day_summary_line("Tempest", summary)
    assert "23 hours" in line and "20 hours" in line
    assert "2 hours of the service not answering" in line
    assert "1 hour of the station reporting nothing" in line
    assert "60 minutes" not in line
    # A record too short to mean anything says so rather than dressing an
    # hour up as a day.
    thin = sh.summarise([{"from_ms": t, "until_ms": t + 60_000,
                          "verdict": "ok"}], t + 60_000)
    assert "less than an hour" in health_watch.day_summary_line("Tempest", thin)


def test_recovery_counts_this_outage_and_not_the_last_one():
    t = 1_700_000_000_000
    runs = [
        {"from_ms": t, "until_ms": t + 2 * HOUR, "verdict": "vendor"},
        {"from_ms": t + 2 * HOUR, "until_ms": t + 8 * HOUR, "verdict": "ok"},
        {"from_ms": t + 8 * HOUR, "until_ms": t + 11 * HOUR, "verdict": "vendor"},
    ]
    assert health_watch._outage_ms(runs, t + 11 * HOUR) == 3 * HOUR
    assert health_watch._spell_duration(3 * HOUR) == "3 hours"
    assert health_watch._spell_duration(45 * 60_000) == "45 minutes"
    assert health_watch._spell_duration(HOUR) == "1 hour"
    assert health_watch._spell_duration(90 * 60_000) == "1.5 hours"
    assert health_watch._spell_duration(60_000) == "1 minute"
    assert health_watch._spell_duration(48 * HOUR) == "2 days"


def test_the_sources_route_carries_the_strip(client, monkeypatch):
    from app import source_status
    source_status.declare("tempest", True)
    source_status.record_success("tempest", rows=2)
    r = client.get("/api/sources", headers=H)
    assert r.status_code == 200
    row = next(s for s in r.json()["sources"] if s["name"] == "tempest")
    assert "health_24h" in row
    assert row["health_24h"]["window_ms"] == sh.WINDOW_MS
    # An unconfigured source gets no strip rather than an empty one that
    # would draw as a day of nothing.
    source_status.declare("govee", False)
    r = client.get("/api/sources", headers=H)
    govee = next(s for s in r.json()["sources"] if s["name"] == "govee")
    assert "health_24h" not in govee


def test_the_strip_is_one_character_an_hour(client):
    t = 1_700_000_000_000
    day = 24 * HOUR
    runs = [
        {"from_ms": t - day, "until_ms": t - 4 * HOUR, "verdict": "ok"},
        {"from_ms": t - 4 * HOUR, "until_ms": t - 2 * HOUR, "verdict": "vendor"},
        {"from_ms": t - 2 * HOUR, "until_ms": t, "verdict": "ok"},
    ]
    strip = sh.buckets(runs, t)
    assert len(strip) == 24
    assert strip == "o" * 20 + "vv" + "oo"
    # An hour with ten minutes of outage in it is an hour with an outage
    # in it: the worst verdict wins the bucket, never the longest.
    brief = [
        {"from_ms": t - 2 * HOUR, "until_ms": t - HOUR - 10 * 60_000,
         "verdict": "ok"},
        {"from_ms": t - HOUR - 10 * 60_000, "until_ms": t - HOUR,
         "verdict": "ours"},
        {"from_ms": t - HOUR, "until_ms": t, "verdict": "ok"},
    ]
    assert sh.buckets(brief, t)[-2:] == "xo"
    # Hours nobody watched are absent, not healthy.
    assert sh.buckets([], t) == "-" * 24


def test_the_devices_route_carries_the_strip_once_per_source(client):
    from app import db, source_status
    source_status.declare("tempest", True)
    source_status.record_success("tempest", rows=1)

    async def seed():
        await db.upsert_device("AA:BB:CC:00:00:91",
                               {"name": "T", "info": {"source": "tempest"}})
        await db.insert_observations("AA:BB:CC:00:00:91",
                                     [{"dateutc": 1_700_000_000_000,
                                       "tempf": 70.0}])
        await sh.record("tempest", sh.OK, 1_700_000_000_000)
    asyncio.run(seed())

    r = client.get("/api/devices", headers=H)
    assert r.status_code == 200
    ours = [d for d in r.json() if d["mac"] == "AA:BB:CC:00:00:91"]
    assert ours, "the seeded station is missing from /api/devices"
    health = ours[0].get("source_health")
    assert health and health.get("name") == "tempest"
    assert len(health["health_24h"]["hours"]) == 24


def test_a_run_does_not_bridge_six_unobserved_hours(client):
    """R24-05 (2.4 release review): OK at T and OK at T+6h used to extend
    the first run across the six hours nobody watched, and the summary
    said six hours covered, 100 percent ok. A gap longer than two
    keep-alives closes the old run where it was and starts a new one, so
    the strip draws the gap as absent and the denominator is honest."""
    async def run():
        await sh.record("gapped", sh.OK, NOW)
        await sh.record("gapped", sh.OK, NOW + 6 * HOUR)
        runs = await sh.history("gapped", NOW + 6 * HOUR)
        out = sh.summarise(runs, NOW + 6 * HOUR)
        assert len(runs) == 2
        assert out["covered_ms"] < 2 * sh.KEEPALIVE_MS
        assert out["hours"].count("-") >= 5
        # A short gap still extends the run.
        await sh.record("steady", sh.OK, NOW)
        await sh.record("steady", sh.OK, NOW + 20 * 60_000)
        steady = await sh.history("steady", NOW + 20 * 60_000)
        assert len(steady) == 1
        assert steady[0]["until_ms"] == NOW + 20 * 60_000
    asyncio.run(run())
