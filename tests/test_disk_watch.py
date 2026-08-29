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
        await dw.check(_cfg(), now, fake_deliver)
        assert delivered == [("disk_low", None)]
        # Same tier again: edge-triggered, no repeat.
        await dw.check(_cfg(), now + 60_000, fake_deliver)
        assert len(delivered) == 1

        # Escalate to urgent — warning severity breaks quiet hours.
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(96.0))
        await dw.check(_cfg(), now + 120_000, fake_deliver)
        assert delivered[-1] == ("disk_low", "warning")

        # Back down into warn territory: tier recorded, nothing sent.
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(90.0))
        await dw.check(_cfg(), now + 180_000, fake_deliver)
        assert len(delivered) == 2
        states = await db.get_smart_alert_states()
        assert states[("server", "disk_low")] == 1

        # Cleared, but every channel fails → state must NOT advance, so
        # the recovery retries next tick instead of vanishing.
        handled = False
        monkeypatch.setattr(dw, "snapshot", lambda: _stats(50.0))
        await dw.check(_cfg(), now + 240_000, fake_deliver)
        assert delivered[-1] == ("disk_recovered", None)
        states = await db.get_smart_alert_states()
        assert states[("server", "disk_low")] == 1
        handled = True
        await dw.check(_cfg(), now + 300_000, fake_deliver)
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
