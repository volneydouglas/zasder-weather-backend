"""Rain-start nowcast (1.7): bucket selection, message, and the check()
gates — opt-in, one-sky-per-server, wet-hour suppressor, cooldown."""
from __future__ import annotations

import os
import time

import pytest

# Set BEFORE importing app: config.py reads the environment at import time
# (same dance as test_tempest.py).
os.environ.setdefault("API_TOKEN", "test-api-token")

from app import nowcast  # noqa: E402


NOW = 1_800_000_000_000          # fixed epoch ms; buckets built relative to it


def _series(*mm, start_offset_min=0):
    """15-min buckets starting at NOW + offset."""
    return [(NOW + (start_offset_min + i * 15) * 60_000, v)
            for i, v in enumerate(mm)]


def test_first_wet_bucket_picks_first_meaningful_rain():
    s = _series(0.0, 0.0, 0.4, 1.2, 0.1)
    hit = nowcast.first_wet_bucket(s, NOW)
    assert hit is not None
    start, total = hit
    assert start == s[2][0]
    # Total = everything within 2h of the wet bucket (0.4+1.2+0.1).
    assert total == pytest.approx(1.7)


def test_first_wet_bucket_ignores_drizzle_and_past_buckets():
    # Below the threshold everywhere → no alert.
    assert nowcast.first_wet_bucket(_series(0.1, 0.05, 0.1), NOW) is None
    # A wet bucket fully in the past must not fire "rain expected".
    past = _series(2.0, 0.0, 0.0, start_offset_min=-40)
    assert nowcast.first_wet_bucket(past, NOW) is None


def test_first_wet_bucket_respects_lead_window():
    # Rain at +75 min is outside the 60-min lead → stay quiet for now
    # (the next poll gets it when it enters the window).
    s = _series(0.0, 0.0, 0.0, 0.0, 0.0, 2.0)
    assert nowcast.first_wet_bucket(s, NOW) is None


def test_build_message_localizes_and_converts():
    title, body = nowcast.build_message("Backyard", NOW + 30 * 60_000,
                                        total_mm_2h=5.08,
                                        tz_name="America/Phoenix")
    assert "Rain expected around" in title
    assert "Backyard" in body
    assert "0.20 in" in body        # 5.08 mm converted, display-only


def _device(with_coords=True, hourly=0.0):
    d = {"mac": "AA", "name": "Yard", "lastData": {"hourlyrainin": hourly}}
    if with_coords:
        d["info"] = {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}
    else:
        d["info"] = {}
    return d


class _Cfg:
    rain_start = True
    email_scope = "all"


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    """Reset the poll throttle and fake the kv store — check() must not
    depend on whichever app.db instance a client fixture rebuilt."""
    nowcast._reset_for_tests()
    kv = {}

    async def fake_get(key):
        return kv.get(key)

    async def fake_set(key, value):
        kv[key] = value

    monkeypatch.setattr(nowcast.db, "get_kv", fake_get)
    monkeypatch.setattr(nowcast.db, "set_kv", fake_set)


def test_check_alerts_once_then_cooldown(monkeypatch):
    import asyncio

    async def run():
        now = int(time.time() * 1000)
        series = [(now + (10 + i * 15) * 60_000, v) for i, v in enumerate([1.0, 2.0])]
        sent = []

        async def fake_fetch(lat, lon):
            return series

        async def fake_deliver(cfg, subject, body, ptitle, pbody, email_ok=True, **kw):
            sent.append(subject)
            return True

        monkeypatch.setattr(nowcast, "fetch_minutely", fake_fetch)
        await nowcast.check(_Cfg(), [_device()], now, fake_deliver)
        assert len(sent) == 1, "first detection alerts"
        # Second check after the throttle: same event, cooldown holds.
        nowcast._reset_for_tests()
        await nowcast.check(_Cfg(), [_device()], now + 60_000, fake_deliver)
        assert len(sent) == 1, "cooldown must swallow the repeat"

    asyncio.run(run())


def test_check_gates(monkeypatch):
    import asyncio

    async def run():
        now = int(time.time() * 1000)
        series = [(now + (10 + i * 15) * 60_000, v) for i, v in enumerate([1.0, 2.0])]
        sent = []

        async def fake_fetch(lat, lon):
            return series

        async def fake_deliver(cfg, subject, body, ptitle, pbody, email_ok=True, **kw):
            sent.append(subject)
            return True

        monkeypatch.setattr(nowcast, "fetch_minutely", fake_fetch)

        off = _Cfg(); off.rain_start = False
        await nowcast.check(off, [_device()], now, fake_deliver)
        assert sent == [], "opt-in: disabled config never polls"

        nowcast._reset_for_tests()
        await nowcast.check(_Cfg(), [_device(with_coords=False)], now, fake_deliver)
        assert sent == [], "no coordinates, no nowcast"

        nowcast._reset_for_tests()
        await nowcast.check(_Cfg(), [_device(hourly=0.25)], now, fake_deliver)
        assert sent == [], "a wet trailing hour suppresses the warning"

    asyncio.run(run())


def test_check_starts_live_activity_alongside_the_push(monkeypatch):
    """Phase 2: a delivered rain-start alert also fans out an ActivityKit
    start payload — countdown epoch in content-state, stale shortly after
    onset, dismissal an hour out. And best-effort means a Live Activity
    failure must neither undo the alert stamp nor raise."""
    import asyncio
    import app.apns as apns

    async def run():
        now = int(time.time() * 1000)
        start = now + 30 * 60_000

        async def fake_fetch(lat, lon):
            return [(start, 2.54)]           # one wet bucket, 0.10 in

        async def fake_deliver(cfg, subject, body, ptitle, pbody,
                               email_ok=True, **kw):
            return True

        sent = {}

        async def fake_la(payload, title, body):
            sent["payload"] = payload
            return {"sent": 2, "dead": [], "failed": 0}

        monkeypatch.setattr(nowcast, "fetch_minutely", fake_fetch)
        monkeypatch.setattr(apns, "send_live_activity_start", fake_la)
        await nowcast.check(_Cfg(), [_device()], now, fake_deliver)

        aps = sent["payload"]["aps"]
        assert aps["event"] == "start"
        assert aps["attributes-type"] == "RainStartActivityAttributes"
        assert aps["attributes"] == {"stationName": "Yard"}
        assert aps["content-state"]["startMs"] == start
        assert aps["content-state"]["totalIn"] == 0.1
        assert aps["stale-date"] == (start + 30 * 60_000) // 1000
        assert aps["dismissal-date"] == (start + 60 * 60_000) // 1000

        # Failure path: LA blows up, the alert state must still stamp
        # (no re-alert next tick) and check() must not raise.
        nowcast._reset_for_tests()
        state = await nowcast._get_state()
        assert state.get("alerted_at_ms")     # stamped by the run above

        async def boom(payload, title, body):
            raise RuntimeError("apns down")
        monkeypatch.setattr(apns, "send_live_activity_start", boom)
        # Cooldown suppresses a resend anyway; drive a fresh event far out.
        later = now + 4 * 3600_000
        async def fetch2(lat, lon):
            return [(later + 20 * 60_000, 3.0)]
        monkeypatch.setattr(nowcast, "fetch_minutely", fetch2)
        await nowcast.check(_Cfg(), [_device()], later, fake_deliver)
        state = await nowcast._get_state()
        assert state.get("alerted_at_ms") == later

    asyncio.run(run())
