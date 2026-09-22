"""Disk-space watchdog (1.9): tier hysteresis, the alert edges, and the
`/api/version` disk block the apps read."""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app.disk_watch import (build_message, fmt_bytes,  # noqa: E402
                            tier_for)
# Captured at collection time, BEFORE conftest's temp_env replaces the
# module attribute with a healthy fake — the one place the real statvfs
# path still gets exercised.
from app.disk_watch import snapshot as real_snapshot  # noqa: E402


def test_tier_thresholds():
    assert tier_for(0.0, 0) == 0
    assert tier_for(84.9, 0) == 0
    assert tier_for(85.0, 0) == 1
    assert tier_for(94.9, 0) == 1
    assert tier_for(95.0, 0) == 2
    assert tier_for(100.0, 0) == 2


def test_tier_hysteresis_holds_near_the_boundary():
    # Hovering just under a threshold keeps the tier once entered…
    assert tier_for(84.5, 1) == 1
    assert tier_for(94.5, 2) == 2
    # …and dropping past the margin actually clears it.
    assert tier_for(83.0, 1) == 0
    assert tier_for(93.0, 2) == 1
    # A fresh look at the same value without prior state stays calm.
    assert tier_for(84.5, 0) == 0


def test_fmt_bytes_scales():
    assert fmt_bytes(512 * 1024**2) == "512 MB"
    assert fmt_bytes(int(2.1 * 1024**3)) == "2.1 GB"
    assert fmt_bytes(50 * 1024**3) == "50 GB"


def test_messages_name_the_free_space():
    title, body = build_message(2, 96.0, 300 * 1024**2, 8 * 1024**3)
    assert "96%" in title and "300 MB" in body and "8.0 GB" in body
    title, body = build_message(1, 87.0, int(1.2 * 1024**3), 8 * 1024**3)
    assert "87%" in title and "1.2 GB" in body
    title, _ = build_message(0, 72.0, 2 * 1024**3, 8 * 1024**3)
    assert "recovered" in title


def _cfg():
    return SimpleNamespace(enabled=False, email_scope="device_down",
                           recipients=[])


def _stats(pct: float) -> dict:
    total = 8 * 1024**3
    return {"total_bytes": total,
            "free_bytes": int(total * (100 - pct) / 100),
            "used_pct": pct}


def test_alert_edges(client, monkeypatch):
    """0→1 warns, 1→2 goes urgent, 2→1 is silent, →0 recovers — and a
    failed delivery keeps the old tier so the next tick retries."""
    import app.disk_watch as dw
    from app import db
    delivered: list[tuple[str, str | None]] = []
    handled = True

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append((kw.get("kind"), kw.get("severity")))
        return handled

    now = int(time.time() * 1000)

    async def run():
        nonlocal handled
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(87.0))
        # A rise takes two consecutive believed ticks (2.4, D2).
        await dw.check(_cfg(), now, fake_deliver)
        assert delivered == []
        await dw.check(_cfg(), now + 60_000, fake_deliver)
        assert delivered == [("disk_low", None)]
        # Same tier again: edge-triggered, no repeat.
        await dw.check(_cfg(), now + 120_000, fake_deliver)
        assert len(delivered) == 1

        # Escalate to urgent — warning severity breaks quiet hours.
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(96.0))
        await dw.check(_cfg(), now + 180_000, fake_deliver)
        assert len(delivered) == 1
        await dw.check(_cfg(), now + 240_000, fake_deliver)
        assert delivered[-1] == ("disk_low", "warning")

        # Back down into warn territory: tier recorded, nothing sent.
        # A FALL is acted on the first tick — an early all-clear hurts
        # nobody, and the rise it would undo can no longer fire.
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(90.0))
        await dw.check(_cfg(), now + 300_000, fake_deliver)
        assert len(delivered) == 2
        states = await db.get_smart_alert_states()
        assert states[("server", "disk_low")] == 1

        # Cleared, but every channel fails → state must NOT advance, so
        # the recovery retries next tick instead of vanishing.
        handled = False
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(50.0))
        await dw.check(_cfg(), now + 360_000, fake_deliver)
        assert delivered[-1] == ("disk_recovered", None)
        states = await db.get_smart_alert_states()
        assert states[("server", "disk_low")] == 1
        handled = True
        await dw.check(_cfg(), now + 420_000, fake_deliver)
        states = await db.get_smart_alert_states()
        assert states[("server", "disk_low")] == 0

    asyncio.run(run())


def test_no_snapshot_is_silence(client, monkeypatch):
    """Unstattable path → no claim at all. Absent is not zero-percent."""
    import app.disk_watch as dw

    async def boom(*a, **kw):  # pragma: no cover — must not be called
        raise AssertionError("deliver called with no disk stats")

    monkeypatch.setattr(dw, "snapshot", lambda: None)
    asyncio.run(dw.check(_cfg(), int(time.time() * 1000), boom))


def test_real_snapshot_shape(client):
    """The genuine statvfs path (conftest stubs the module attribute for
    everyone else). Values are host-dependent; the shape is not."""
    disk = real_snapshot()
    assert disk is not None
    assert disk["total_bytes"] > 0
    assert 0 <= disk["free_bytes"] <= disk["total_bytes"]
    assert 0.0 <= disk["used_pct"] <= 100.0


def test_api_version_carries_disk_block(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    disk = r.json()["disk"]
    assert disk is not None
    assert disk["total_bytes"] > 0
    assert disk["free_bytes"] >= 0
    assert 0.0 <= disk["used_pct"] <= 100.0


def test_api_version_disk_null_when_unstattable(client, monkeypatch):
    import app.disk_watch as dw
    monkeypatch.setattr(dw, "snapshot", lambda: None)
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["disk"] is None


def test_used_pct_denominator_is_writable_space(monkeypatch):
    """used/(used+free), not used/total: `free` is f_bavail, so with 5%
    root-reserved blocks the old formula plateaued near 95 while writes
    already failed (CodeRabbit, PR #33)."""
    import collections
    import shutil as _sh

    import app.disk_watch as dw

    Usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(_sh, "disk_usage",
                        lambda p: Usage(total=100, used=90, free=5))
    disk = real_snapshot()
    assert disk["used_pct"] == 94.7           # 90 / 95, not 90 / 100
    monkeypatch.setattr(_sh, "disk_usage",
                        lambda p: Usage(total=100, used=95, free=0))
    assert real_snapshot()["used_pct"] == 100.0


# ─────────── 2.4 (D2): a backup's own copy is not the volume filling ───────────


def _recorder():
    seen: list[tuple[str, str | None]] = []

    async def deliver(cfg, subject, body, pt, pb, **kw):
        seen.append((kw.get("kind"), kw.get("severity")))
        return True
    return seen, deliver


def test_a_rise_needs_two_consecutive_believed_ticks(client, monkeypatch):
    """Doren's box, 2026-09-20 01:51Z: 88% for two minutes while his
    nightly backup's VACUUM INTO copy sat on the volume. One tick of a
    high number is no longer an alert."""
    import app.disk_watch as dw
    seen, deliver = _recorder()
    now = int(time.time() * 1000)

    async def run():
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(88.0))
        await dw.check(_cfg(), now, deliver)
        assert seen == []
        # …and a transient that clears before the second tick announces
        # nothing at all, ever.
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(60.0))
        await dw.check(_cfg(), now + 60_000, deliver)
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(88.0))
        await dw.check(_cfg(), now + 120_000, deliver)
        assert seen == []
        await dw.check(_cfg(), now + 180_000, deliver)
        assert seen == [("disk_low", None)]

    asyncio.run(run())


def test_a_wobbling_rise_restarts_the_count(client, monkeypatch):
    """One tick at warn, one at urgent, one at warn: neither tier was
    seen twice running, so nothing is announced."""
    import app.disk_watch as dw
    seen, deliver = _recorder()
    now = int(time.time() * 1000)

    async def run():
        for i, pct in enumerate((87.0, 96.0, 87.0)):
            monkeypatch.setattr(dw, "snapshot", lambda p=pct: _stats(p))
            await dw.check(_cfg(), now + i * 60_000, deliver)
        assert seen == []

    asyncio.run(run())


def test_a_backup_in_flight_skips_the_tick(client, monkeypatch):
    """The disk is fuller because WE are copying the database onto it.
    Skipped, not measured — and the ticks either side of the job still
    count, so a genuine rise is not slowed by a backup between them."""
    import app.copy_jobs as cj
    import app.disk_watch as dw
    seen, deliver = _recorder()
    now = int(time.time() * 1000)

    async def run():
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(88.0))
        await dw.check(_cfg(), now, deliver)            # believed tick 1
        with cj.running("backup"):
            monkeypatch.setattr(dw, "snapshot", lambda: _stats(99.0))
            for i in range(1, 4):
                await dw.check(_cfg(), now + i * 60_000, deliver)
            assert seen == []                            # not even urgent
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(88.0))
        await dw.check(_cfg(), now + 240_000, deliver)   # believed tick 2
        assert seen == [("disk_low", None)]

    asyncio.run(run())


def test_a_restore_job_counts_as_in_flight(client, monkeypatch):
    """restore.py drives its state across a route and a background task
    rather than a `with` block, so copy_jobs reads it where it lives."""
    import app.copy_jobs as cj
    from app import restore
    from app.config import settings
    assert cj.in_flight() is None
    assert cj.in_flight(settings.database_path) is None
    for state in ("receiving", "validating", "swapping"):
        restore.JOB["state"] = state
        assert cj.in_flight() == "restore"
        # The production call names the database's filesystem (2.4
        # review): a restore always writes beside the database, so it
        # counts for that device, and the path-filtered read returned
        # before it ever looked.
        assert cj.in_flight(settings.database_path) == "restore"
    restore.JOB["state"] = "done"
    assert cj.in_flight() is None
    assert cj.in_flight(settings.database_path) is None


def test_a_restore_in_flight_skips_the_disk_tick(client, monkeypatch):
    """The watchdog's own call, end to end: a 400 MB upload landing beside
    the database is not a rise worth an alarm."""
    import app.disk_watch as dw
    from app import restore
    seen, deliver = _recorder()
    now = int(time.time() * 1000)

    async def run():
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(88.0))
        await dw.check(_cfg(), now, deliver)
        restore.JOB["state"] = "receiving"
        try:
            monkeypatch.setattr(dw, "snapshot", lambda: _stats(99.0))
            for i in range(1, 4):
                await dw.check(_cfg(), now + i * 60_000, deliver)
            assert seen == []
        finally:
            restore.JOB["state"] = "done"
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(88.0))
        await dw.check(_cfg(), now + 240_000, deliver)
        assert seen == [("disk_low", None)]

    asyncio.run(run())


def test_a_copy_on_another_filesystem_does_not_stop_the_watch(client, monkeypatch):
    """The backup destination can be the TEMPDIR, which on a Fly machine
    is the root filesystem. A copy over there says nothing about the data
    volume, and the watchdog must keep measuring it (CodeRabbit, PR
    #40)."""
    import tempfile

    import app.copy_jobs as cj
    import app.disk_watch as dw
    from app.config import settings
    seen, deliver = _recorder()
    now = int(time.time() * 1000)
    db_path = settings.database_path

    async def run():
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(88.0))
        # A copy onto the tempdir: only skipped if the two share a
        # filesystem, which on this machine they may. Ask copy_jobs the
        # same question the watchdog does rather than assuming.
        with cj.running("backup", tempfile.gettempdir()):
            same_fs = cj.in_flight(db_path) is not None
            for i in range(2):
                await dw.check(_cfg(), now + i * 60_000, deliver)
        assert seen == ([] if same_fs else [("disk_low", None)])
        # A copy onto the database's OWN directory always stops it.
        cj._reset_for_tests()
        dw._reset_for_tests()
        with cj.running("backup", db_path):
            assert cj.in_flight(db_path) == "backup"
            for i in range(2, 4):
                await dw.check(_cfg(), now + i * 60_000, deliver)

    asyncio.run(run())


def test_a_copy_that_did_not_say_where_still_counts():
    """Not knowing is the loud side: an unlabelled destination is treated
    as landing on the watched volume."""
    import app.copy_jobs as cj
    from app.config import settings
    with cj.running("restore staging"):
        assert cj.in_flight(settings.database_path) == "restore staging"
        assert cj.in_flight() == "restore staging"


def test_copy_job_registration_nests_and_unwinds():
    import app.copy_jobs as cj
    assert cj.in_flight() is None
    with cj.running("backup"):
        with cj.running("backup"):
            assert cj.in_flight() == "backup"
        assert cj.in_flight() == "backup"
    assert cj.in_flight() is None
    try:
        with cj.running("pre-upgrade snapshot"):
            raise RuntimeError("the copy died")
    except RuntimeError:
        pass
    assert cj.in_flight() is None
