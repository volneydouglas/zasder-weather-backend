"""The 24 hour strip for a PUSHED station (2.5, C13).

A station fed from the owner's own network (a relay board, the WLL
bridge, the local Ecowitt push, a custom script) has no poller and so
no source record: Doren's "Chaucer Drive" showed no strip at all while
his Tempest and Govee did. Its day is built from when readings actually
arrived, and a gap only counts against the station while this server
was demonstrably running (the server watch), because a server that was
off has no opinion about the hours it missed.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import health_watch, source_history as sh  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MIN = 60_000
HOUR = 3_600_000
NOW = 1_700_000_000_000


def _watched_all_day():
    return [{"from_ms": NOW - 24 * HOUR, "until_ms": NOW, "verdict": sh.OK}]


def test_steady_readings_are_a_clean_day():
    stamps = list(range(NOW - 24 * HOUR, NOW, 5 * MIN))
    day = sh.arrival_day(stamps, _watched_all_day(), NOW)
    assert day["hours"] == "o" * 24
    assert day["worst"] is None and day["ok_fraction"] == 1.0
    assert day["kind"] == "push"


def test_a_gap_while_the_server_watched_is_the_station():
    # Readings every 5 minutes except a three hour hole ending 6 h ago.
    stamps = [t for t in range(NOW - 24 * HOUR, NOW, 5 * MIN)
              if not (NOW - 9 * HOUR <= t < NOW - 6 * HOUR)]
    day = sh.arrival_day(stamps, _watched_all_day(), NOW)
    assert day["worst"] == sh.DEVICE
    # The hole is hours 15..17 of 24 (oldest first).
    assert day["hours"][15:18] == "ddd"
    assert day["hours"][:15] == "o" * 15 and day["hours"][18:] == "o" * 6


def test_fifteen_quiet_minutes_are_not_an_outage():
    """The 2.4 source-health rule: a station is quiet only after 15
    minutes with nothing stored. A 10 minute cadence never flags."""
    stamps = list(range(NOW - 24 * HOUR, NOW, 10 * MIN))
    assert sh.arrival_day(stamps, _watched_all_day(), NOW)["hours"] == "o" * 24


def test_a_gap_nobody_watched_is_unknown_not_the_station():
    stamps = [t for t in range(NOW - 24 * HOUR, NOW, 5 * MIN)
              if not (NOW - 9 * HOUR <= t < NOW - 6 * HOUR)]
    # The server was off for exactly that stretch.
    watched = [{"from_ms": NOW - 24 * HOUR, "until_ms": NOW - 9 * HOUR,
                "verdict": sh.OK},
               {"from_ms": NOW - 6 * HOUR, "until_ms": NOW, "verdict": sh.OK}]
    day = sh.arrival_day(stamps, watched, NOW)
    assert "d" not in day["hours"]
    # Hour 15 opens inside the cadence of the last reading; the rest of
    # the hole is nobody's.
    assert day["hours"][15:18] == "o--"
    assert day["worst"] is None


def test_a_reading_proves_the_server_was_up():
    """No server-watch record yet (the first day after this ships): the
    hours with readings are still readings, and the silences are unknown."""
    stamps = list(range(NOW - 4 * HOUR, NOW, 5 * MIN))
    day = sh.arrival_day(stamps, [], NOW)
    assert day["hours"] == "-" * 20 + "o" * 4
    assert day["covered_ms"] >= 4 * HOUR - 5 * MIN


def test_a_station_gone_quiet_now_says_so():
    stamps = list(range(NOW - 24 * HOUR, NOW - 2 * HOUR, 5 * MIN))
    day = sh.arrival_day(stamps, _watched_all_day(), NOW)
    assert day["current"] == sh.DEVICE
    assert day["hours"][-2:] == "dd"


def test_the_tick_records_the_server_watch(client):
    async def go():
        await health_watch.record_sources(NOW)
        await health_watch.record_sources(NOW + 20 * MIN)
        return await sh.history(sh.SERVER_WATCH, NOW + 20 * MIN)
    runs = asyncio.run(go())
    assert runs and runs[-1]["verdict"] == sh.OK
    assert runs[0]["from_ms"] == NOW and runs[-1]["until_ms"] == NOW + 20 * MIN


def test_the_record_is_written_with_every_alert_channel_off(client, monkeypatch):
    """The alert tick returns early on a box with no email, push or
    webhook. The 24 hour record (and the server watch it depends on) is
    written before that gate, or a box without alerts has no strips."""
    from app import alerts
    seen = []

    async def fake_record(now_ms):
        seen.append(now_ms)
    monkeypatch.setattr(health_watch, "record_sources", fake_record)
    asyncio.run(alerts.AlertMonitor()._tick())
    assert seen, "record_sources did not run on a box with alerts closed"


def test_the_devices_route_carries_a_pushed_stations_day(client):
    from app import db
    mac = "AA:BB:CC:00:00:C1"

    async def seed():
        import time
        now = int(time.time() * 1000)
        await db.upsert_device(mac, {"name": "Chaucer Drive",
                                     "info": {"source": "wll-bridge"}})
        await db.insert_observations(
            mac, [{"dateutc": t, "tempf": 70.0}
                  for t in range(now - 3 * HOUR, now - MIN, 5 * MIN)])
        await sh.record(sh.SERVER_WATCH, sh.OK, now - 3 * HOUR)
        await sh.record(sh.SERVER_WATCH, sh.OK, now - MIN)
    asyncio.run(seed())

    r = client.get("/api/devices", headers=H)
    assert r.status_code == 200
    ours = [d for d in r.json() if d["mac"] == mac][0]
    assert ours["source_health"] is None
    day = ours["arrival_24h"]
    assert len(day["hours"]) == 24 and day["hours"].endswith("ooo")
    assert day["kind"] == "push"


def test_a_polled_station_gets_its_own_day_with_the_vendors_hours_marked(client):
    """09-26: the outdoor AirGradient went quiet for twelve minutes while
    the indoor one kept reporting, and every strip stayed green because
    the poller's record is per SOURCE. A polled station now has its own
    arrival day, and an hour the vendor was down is the vendor's, not the
    station's."""
    import time
    from app import db, source_status
    mac = "AA:BB:CC:00:00:C2"
    source_status.declare("tempest", True)
    source_status.record_success("tempest", rows=1)
    now = int(time.time() * 1000)

    async def seed():
        await db.upsert_device(mac, {"name": "T", "info": {"source": "tempest"}})
        # Readings every 5 min except a three hour hole ending 6 h ago,
        # and a separate hour, 12 h ago, that the station missed on its own.
        await db.insert_observations(mac, [
            {"dateutc": t, "tempf": 70.0}
            for t in range(now - 24 * HOUR, now - MIN, 5 * MIN)
            if not (now - 9 * HOUR <= t < now - 6 * HOUR)
            and not (now - 12 * HOUR <= t < now - 11 * HOUR)])
        # Stamped the way the tick stamps, every quarter hour: a stamp
        # more than two keep-alives after the last one starts a new run.
        # The vendor was down for exactly the three hour hole.
        for k in range(0, 24 * 4 + 1):
            t = now - 24 * HOUR + k * 15 * MIN
            await sh.record(sh.SERVER_WATCH, sh.OK, t)
            await sh.record("tempest", sh.VENDOR if now - 9 * HOUR <= t < now - 6 * HOUR
                            else sh.OK, t)
    asyncio.run(seed())
    ours = [d for d in client.get("/api/devices", headers=H).json() if d["mac"] == mac][0]
    day = ours["arrival_24h"]
    assert day["kind"] == "polled"
    assert day["hours"][15:18] == "vvv", day["hours"]
    assert day["hours"][12] == "d", day["hours"]
    assert set(day["hours"][18:]) == {"o"} and set(day["hours"][:12]) == {"o"}


def test_an_import_does_not_paint_the_strip(client):
    """R25-A05 (the 2.5 additional review): arrival slots were read from
    the readings' own timestamps, so importing the last day painted a day
    the server never saw as healthy. From when stamping began, only a
    receipt counts; an import is not one."""
    import time as _t
    from app import archive_import, db, main
    mac = "AA:BB:CC:25:05:01"
    now = int(_t.time() * 1000)

    async def run():
        await db.set_kv("arrival_rx.since_ms", str(now - 30 * 3_600_000))
        await db.upsert_device(mac, {"name": "Imported"})
        rows = [{"dateutc": t, "tempf": 70.0, "source": "csv-import"}
                for t in range(now - 24 * 3_600_000, now - 600_000, 300_000)]
        await archive_import.run_import(mac, rows, kind="csv")
        main._ARRIVAL_CACHE.clear()
        return await main._arrival_day(mac, [], now)
    day = asyncio.run(run())
    assert "o" not in day["hours"], day


def test_a_backlog_flush_lights_only_the_slot_it_arrived_in(client):
    """A relay flushing an hour of saved readings after an outage: the
    readings are stored with their own times, but the server received them
    all just now, so only the current slot counts as heard."""
    import time as _t
    from app import db
    mac = "AA:BB:CC:25:05:02"
    now = int(_t.time() * 1000)

    async def run():
        await db.set_kv("arrival_rx.since_ms", str(now - 30 * 3_600_000))
        await db.upsert_device(mac, {"name": "Relay"})
        await db.insert_observations(mac, [
            {"dateutc": t, "tempf": 70.0} for t in range(now - 3_600_000, now, 300_000)])
        return await db.arrival_slots(mac, now - 2 * 3_600_000, 300_000)
    slots = asyncio.run(run())
    assert slots == [(now // 300_000) * 300_000], slots


def test_history_before_stamping_began_still_reads_timestamps(client):
    """The first day after the upgrade must not come up blank: the window
    before arrival_rx.since_ms reads the readings' own timestamps."""
    import time as _t
    from app import db
    mac = "AA:BB:CC:25:05:03"
    now = int(_t.time() * 1000)

    async def run():
        await db.upsert_device(mac, {"name": "Old"})
        await db.insert_observations(mac, [{"dateutc": now - 5 * 3_600_000, "tempf": 70.0}],
                                     received=False)
        await db.set_kv("arrival_rx.since_ms", str(now - 3_600_000))
        return await db.arrival_slots(mac, now - 6 * 3_600_000, 300_000)
    slots = asyncio.run(run())
    assert ((now - 5 * 3_600_000) // 300_000) * 300_000 in slots


def test_the_first_receipt_slot_after_the_upgrade_counts(client):
    """Greptile, PR #52: the cut-over was an exact time and receipt stamps
    are floored to five minutes, so a reading received in the first
    partial slot was dropped."""
    import time as _t
    from app import db
    mac = "AA:BB:CC:25:05:04"
    now = int(_t.time() * 1000)

    async def run():
        slot = (now // 300_000) * 300_000
        await db.set_kv("arrival_rx.since_ms", str(slot + 1))     # mid-slot cut-over
        await db.upsert_device(mac, {"name": "New"})
        await db.insert_observations(mac, [{"dateutc": now, "tempf": 70.0}])
        return slot, await db.arrival_slots(mac, now - 3_600_000, 300_000)
    slot, slots = asyncio.run(run())
    assert slot in slots, slots


def test_an_import_in_the_first_day_does_not_paint_the_legacy_window(client):
    """Greptile, PR #52: before arrival_rx.since_ms the strip reads reading
    timestamps, and an import in the first day after the upgrade loaded
    rows there; imported rows never count."""
    import time as _t
    from app import archive_import, db
    mac = "AA:BB:CC:25:05:05"
    now = int(_t.time() * 1000)

    async def run():
        await db.set_kv("arrival_rx.since_ms", str(now))
        await db.upsert_device(mac, {"name": "Fresh upgrade"})
        rows = [{"dateutc": t, "tempf": 70.0, "source": "csv-import"}
                for t in range(now - 6 * 3_600_000, now - 600_000, 300_000)]
        await archive_import.run_import(mac, rows, kind="csv")
        return await db.arrival_slots(mac, now - 8 * 3_600_000, 300_000)
    assert asyncio.run(run()) == []
