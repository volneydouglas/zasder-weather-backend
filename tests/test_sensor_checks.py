"""Sensor checks (2.5, C4): what the plausibility bands refused, counted.

Refusals used to be a log line only, so a gust sensor sending 255 mph all
week looked, in the app, like a quiet week.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token", "Content-Type": "application/json"}
MAC = "AA:BB:CC:00:00:F4"


def _post(client, minutes_ago, gust):
    ts = datetime.fromtimestamp(time.time() - minutes_ago * 60, timezone.utc)
    r = client.post("/ingest/custom", headers=IH, json={
        "device": {"id": MAC, "name": "Yard"},
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdoor": {"tempf": 71.0},
        "wind": {"speed_mph": 5.0, "gust_mph": gust}})
    assert r.status_code in (200, 201, 202), r.text


def test_refusals_are_counted_per_field_and_flushed(client):
    from app import db
    for k in range(6):
        _post(client, 60 - k * 5, 255.0 if k % 2 else 12.0)
    body = client.get(f"/api/devices/{MAC}/sensor-health", headers=H).json()
    rows = {r["field"]: r for r in body["fields"]}
    # Three gusts of 255 refused; the sibling speed channel went with them
    # (the anemometer rule), so it carries refusals too.
    assert rows["windgustmph"]["rejected"] == 3
    assert rows["windgustmph"]["accepted"] == 3
    assert rows["windspeedmph"]["rejected"] == 3
    assert rows["tempf"]["rejected"] == 0 and rows["tempf"]["accepted"] == 6
    assert body["fields"][0]["rejected"] >= body["fields"][-1]["rejected"]
    # Counted before a flush; still counted after it, exactly once.
    asyncio.run(db.flush_qc_rejections())
    assert db._QC_PENDING == {}
    again = {r["field"]: r for r in client.get(
        f"/api/devices/{MAC}/sensor-health", headers=H).json()["fields"]}
    assert again["windgustmph"]["rejected"] == 3


def test_a_clean_station_reports_no_refusals(client):
    _post(client, 10, 12.0)
    rows = client.get(f"/api/devices/{MAC}/sensor-health", headers=H).json()["fields"]
    assert rows and all(r["rejected"] == 0 for r in rows)


def test_sensor_checks_need_the_token(client):
    assert client.get(f"/api/devices/{MAC}/sensor-health").status_code == 401


def test_a_failed_flush_keeps_its_counts_for_the_next_tick(client, monkeypatch):
    """Greptile, PR #48: the counts left memory before the write, so a
    locked database lost them and the sensor checks undercounted."""
    import contextlib
    from app import db
    db._QC_PENDING.clear()
    db.note_rejections("AA:BB:CC:00:00:77", ["windgustmph=255"])
    real = db.connect

    @contextlib.asynccontextmanager
    async def broken():
        raise RuntimeError("database is locked")
        yield  # pragma: no cover
    monkeypatch.setattr(db, "connect", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(db.flush_qc_rejections())
    db.note_rejections("AA:BB:CC:00:00:77", ["windgustmph=255"])
    assert sum(db._QC_PENDING.values()) == 2, "the failed flush's count is back"
    monkeypatch.setattr(db, "connect", real)
    asyncio.run(db.flush_qc_rejections())
    assert not db._QC_PENDING


def test_deleting_a_station_takes_its_sensor_checks_with_it(client):
    """R25-10 (the 2.5 detailed review): delete_device left the stored
    refusal counts, the pending ones and the cached report, so a MAC
    re-added later inherited the old sensor's refusals."""
    from app import db, main
    _post(client, 30, 255.0)
    asyncio.run(db.flush_qc_rejections())
    db.note_rejections(MAC, ["windgustmph=255"])            # still pending
    client.get(f"/api/devices/{MAC}/sensor-health", headers=H)   # cached
    assert any(k[0] == MAC for k in main._SENSOR_HEALTH_CACHE)
    r = client.delete(f"/api/devices/{MAC}", headers=H)
    assert r.status_code == 200 and r.json()["sensor_qc"] >= 1
    assert not any(k[0] == MAC for k in db._QC_PENDING)
    assert not any(k[0] == MAC for k in main._SENSOR_HEALTH_CACHE)

    async def stored():
        async with db.connect() as conn:
            return (await (await conn.execute(
                "SELECT COUNT(*) FROM sensor_qc_daily WHERE mac=?", (MAC,))).fetchone())[0]
    assert asyncio.run(stored()) == 0


def test_a_restore_drops_the_old_databases_pending_and_cached_state(client):
    """R25-09: the restore hook cleared the older caches but not the 2.5
    ones, and the next alert tick flushed the old database's pending
    refusal counts into the restored one."""
    from app import db, main, restore
    db.note_rejections(MAC, ["windgustmph=255"])
    main._ARRIVAL_CACHE[MAC] = (0.0, {"stale": True})
    main._SENSOR_HEALTH_CACHE[(MAC, 30)] = (0.0, {"stale": True})
    hook = next(h for h in restore.POST_SWAP_HOOKS
                if h.__name__ == "_reconcile_after_restore")
    client.portal.call(hook)
    assert not db._QC_PENDING
    assert MAC not in main._ARRIVAL_CACHE and not main._SENSOR_HEALTH_CACHE


def test_accepted_and_rejected_count_the_same_days(client):
    """R25-31 (the 2.5 detailed review): refusals were read from the UTC
    midnight 30 days back, accepted readings from a rolling instant later
    that day, so the percentage mixed two windows. A reading a minute
    after that midnight now counts on both sides."""
    import calendar as _cal
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
    edge = datetime.fromtimestamp(_cal.timegm(time.strptime(day, "%Y-%m-%d")) + 60,
                                  timezone.utc)
    r = client.post("/ingest/custom", headers=IH, json={
        "device": {"id": MAC, "name": "Yard"},
        "timestamp_utc": edge.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdoor": {"tempf": 71.0}})
    assert r.status_code in (200, 201, 202), r.text
    body = client.get(f"/api/devices/{MAC}/sensor-health?days=30", headers=H).json()
    rows = {x["field"]: x for x in body["fields"]}
    assert rows["tempf"]["accepted"] >= 1


def test_a_delete_waits_for_a_flush_in_flight(client, monkeypatch):
    """PR #50 review: a flush that had taken a station's counts out of
    the pending buffer wrote them back after the delete committed. With
    the shared lock the delete runs after the flush and removes them."""
    from app import db
    db._QC_PENDING.clear()
    db.note_rejections(MAC, ["windgustmph=255"])
    _post(client, 5, 12.0)                         # the device exists
    real_connect = db.connect
    order: list[str] = []

    import contextlib

    async def race():
        # An Event, not a fixed sleep: the delete starts only once the
        # flush is inside its connection, however slow the runner
        # (CodeRabbit, PR #50).
        reached = asyncio.Event()

        @contextlib.asynccontextmanager
        async def slow_connect():
            async with real_connect() as conn:
                order.append("flush-connected")
                reached.set()
                await asyncio.sleep(0.05)          # the delete arrives here
                yield conn

        monkeypatch.setattr(db, "connect", slow_connect)
        flush = asyncio.create_task(db.flush_qc_rejections())
        await reached.wait()
        monkeypatch.setattr(db, "connect", real_connect)
        await db.delete_device(MAC)
        order.append("deleted")
        await flush

        async with real_connect() as conn:
            return (await (await conn.execute(
                "SELECT COUNT(*) FROM sensor_qc_daily WHERE mac=?", (MAC,))).fetchone())[0]
    left = asyncio.run(race())
    assert order == ["flush-connected", "deleted"]
    assert left == 0, "the flushed counts were deleted with the station"
