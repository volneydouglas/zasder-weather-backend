"""The 2.2 source watchdog: a cloud poller that keeps failing raises ONE
alert naming the vendor and the kind of failure, and one recovery notice.
The AirGradient outage of 2026-09-07 (six ReadTimeouts, then recovery on
its own) is the shape these tests replay.
"""
from __future__ import annotations

import pytest


class _Cfg:
    email_scope = "device_down"


@pytest.fixture
def delivered():
    return []


@pytest.fixture
def deliver(delivered):
    async def _d(cfg, subject, body, title, push_body, **kw):
        delivered.append({"kind": kw.get("kind"), "title": title, "body": body,
                          "email_ok": kw.get("email_ok")})
        return True
    return _d


def _mods():
    # Imported lazily: at collection time the real .env would build Settings.
    from app import health_watch, source_status
    return health_watch, source_status


def _fail(name, error, at_ms, monkeypatch):
    _, source_status = _mods()
    monkeypatch.setattr(source_status, "_now_ms", lambda: at_ms)
    source_status.record_failure(name, error)


async def test_one_alert_per_episode_then_one_recovery(client, deliver, delivered, monkeypatch):
    health_watch, source_status = _mods()
    source_status.declare("airgradient", True)
    t0 = 1_788_800_000_000
    monkeypatch.setattr(source_status, "_now_ms", lambda: t0)
    source_status.record_success("airgradient", rows=2)
    for i in range(6):
        _fail("airgradient", "AirGradient request failed: ReadTimeout",
              t0 + 120_000 * (i + 1), monkeypatch)
    # 12 minutes in: under the hour, silent.
    await health_watch.check_sources(_Cfg(), t0 + 12 * 60_000, deliver)
    assert delivered == []
    # An hour and a bit: one alert, upstream wording, email allowed.
    await health_watch.check_sources(_Cfg(), t0 + 62 * 60_000, deliver)
    assert [d["kind"] for d in delivered] == ["source_down"]
    assert delivered[0]["title"] == "AirGradient's service is not answering"
    assert "1 h" in delivered[0]["body"] and "ReadTimeout" in delivered[0]["body"]
    assert "Your station and this server are fine" in delivered[0]["body"]
    assert delivered[0]["email_ok"] is True
    # Still failing next tick: no repeat.
    await health_watch.check_sources(_Cfg(), t0 + 70 * 60_000, deliver)
    assert len(delivered) == 1
    # Recovery: one notice, state cleared, and a fresh streak can alert again.
    monkeypatch.setattr(source_status, "_now_ms", lambda: t0 + 80 * 60_000)
    source_status.record_success("airgradient", rows=2)
    await health_watch.check_sources(_Cfg(), t0 + 80 * 60_000, deliver)
    assert [d["kind"] for d in delivered] == ["source_down", "source_recovered"]
    assert delivered[1]["title"] == "AirGradient is answering again"
    _fail("airgradient", "401 Unauthorized", t0 + 90 * 60_000, monkeypatch)
    await health_watch.check_sources(_Cfg(), t0 + 160 * 60_000, deliver)
    assert delivered[-1]["kind"] == "source_down"
    assert delivered[-1]["title"] == "AirGradient is rejecting the saved credentials"
    assert "Settings" in delivered[-1]["body"]


async def test_unconfigured_and_vendorless_sources_never_alert(client, deliver, delivered, monkeypatch):
    health_watch, source_status = _mods()
    t0 = 1_788_800_000_000
    source_status.declare("govee", False)                 # not set up
    source_status.declare("custom-ingest", True)          # no vendor behind it
    _fail("govee", "ConnectError", t0, monkeypatch)
    _fail("custom-ingest", "boom", t0, monkeypatch)
    await health_watch.check_sources(_Cfg(), t0 + 600 * 60_000, deliver)
    assert delivered == []


async def test_zero_minutes_disables_the_watchdog(client, deliver, delivered, monkeypatch):
    health_watch, source_status = _mods()
    t0 = 1_788_800_000_000
    source_status.declare("tempest", True)
    _fail("tempest", "ConnectTimeout", t0, monkeypatch)
    await health_watch.check_sources(_Cfg(), t0 + 600 * 60_000, deliver, quiet_minutes=0)
    assert delivered == []
    await health_watch.check_sources(_Cfg(), t0 + 600 * 60_000, deliver, quiet_minutes=30)
    assert [d["kind"] for d in delivered] == ["source_down"]


def test_copy_names_who_must_act():
    health_watch, source_status = _mods()
    t, b = health_watch.source_down_copy("Tempest", "rate_limit", 2.0, "HTTP 429")
    assert t == "Tempest is rate-limiting this server" and "quota" in b
    t, b = health_watch.source_down_copy("Govee", "ours", 0.5, "KeyError")
    assert t == "Readings from Govee are not being stored" and "30 min" in b
    assert "on the server" in b


async def test_smart_alerts_off_does_not_erase_the_watchdogs_memory(client, monkeypatch):
    """2.5, found building C2: with smart alerts OFF (the default on a new
    install) the tick cleared EVERY smart_alert_state row before the source
    watchdog ran, so a poller down for an hour re-announced its outage on
    every tick. Only the smart family's own edges may be forgotten."""
    from app import alerts, apns, db, source_status
    health_watch, _ = _mods()
    sent = []

    async def fake_deliver(cfg, subject, body, title, push_body, **kw):
        sent.append(kw.get("kind"))
        return True
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)

    async def yes():
        return True
    monkeypatch.setattr(apns, "push_configured", yes)
    t0 = 1_788_800_000_000
    source_status.declare("airgradient", True)
    monkeypatch.setattr(source_status, "_now_ms", lambda: t0)
    source_status.record_success("airgradient", rows=2)
    _fail("airgradient", "ReadTimeout", t0 + 60_000, monkeypatch)
    now = [t0 + 65 * 60_000]
    monkeypatch.setattr(alerts.time, "time", lambda: now[0] / 1000)
    cfg = await alerts.effective_config()
    assert not cfg.smart_alerts, "this test needs the switch OFF"
    mon = alerts.AlertMonitor()
    await mon._tick()
    now[0] += 60_000
    await mon._tick()
    assert sent.count("source_down") == 1
