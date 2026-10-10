"""The lightning alert at a distance the owner picks (2.6, Volney 10-10:
"a lightning alert for a specific distance"). Only a strike at or inside
the distance opens or escalates an alert; every strike still restarts the
NWS 30 minute all-clear clock. The alert has its own switch, which
follows the weather-watches switch until the owner sets it."""
import asyncio

import pytest
import time
from types import SimpleNamespace

H = {"Authorization": "Bearer test-api-token"}


def _cfg(limit=None):
    return SimpleNamespace(email_scope="all", lightning_alert_mi=limit)


def _dev(count, dist):
    return [{"mac": "AA:BB:CC:00:00:64", "name": "Arts",
             "lastData": {"lightningcount": count, "lightning_distance_mi": dist}}]


def _run(limit, steps):
    from app import lightning_watch as lw
    delivered = []

    async def deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append((kw.get("kind"), pt))
        return True
    now = int(time.time() * 1000)

    async def go():
        for minute, count, dist in steps:
            await lw.check(_cfg(limit), _dev(count, dist), now + minute * 60_000, deliver)
    asyncio.run(go())
    return delivered


def test_within_is_inclusive_and_needs_a_distance(client):
    from app.lightning_watch import within
    assert within(None, None) and within(30.0, None)
    assert within(10.0, 10.0) and within(3.0, 10.0)
    assert not within(10.5, 10.0)
    assert not within(None, 10.0)


def test_far_strikes_stay_quiet_until_one_comes_inside(client):
    got = _run(10.0, [
        (0, 10, 20.0),   # baseline
        (1, 12, 20.0),   # strikes at 20 mi: outside 10, quiet
        (6, 14, 14.0),   # 14 mi: still outside
        (12, 16, 8.0),   # inside: the alert
    ])
    assert [k for k, _ in got] == ["lightning"]
    assert got[0][1] == "Arts: Lightning detected within 10 mi"


def test_a_strike_with_no_distance_does_not_count_against_a_limit(client):
    assert _run(10.0, [(0, 10, None), (1, 12, None)]) == []
    # With no limit set, it alerts as it always has.
    assert [k for k, _ in _run(None, [(0, 10, None), (1, 12, None)])] == ["lightning"]


def test_far_strikes_keep_the_all_clear_clock_running(client):
    got = _run(10.0, [
        (0, 10, 20.0),
        (1, 12, 6.0),    # inside: alert
        (20, 14, 18.0),  # outside, but a strike: the clock restarts
        (45, 14, 18.0),  # 44 min after the near strike, 25 after the far
        (51, 14, 18.0),  # 31 min after the last strike: all clear
    ])
    assert [k for k, _ in got] == ["lightning", "lightning_clear"]


def test_no_limit_alerts_at_any_distance_as_before(client):
    got = _run(None, [(0, 10, 24.0), (1, 12, 24.0)])
    assert got == [("lightning", "Arts: Lightning detected")]


# ── the switch and the setting through the API ──────────────────────────

def test_the_switch_follows_watches_until_set_and_the_distance_round_trips(client):
    g = client.get("/api/alerts", headers=H).json()
    assert g["lightning_alert"] == g["smart_alerts_enabled"]
    assert g["lightning_alert_mi"] == 0
    # Watches off, lightning on: lightning stands alone.
    assert client.put("/api/alerts", headers=H, json={
        "smart_alerts": False, "lightning_alert": True,
        "lightning_alert_mi": 10}).status_code == 200
    g = client.get("/api/alerts", headers=H).json()
    assert g["lightning_alert"] is True and g["smart_alerts_enabled"] is False
    assert g["lightning_alert_mi"] == 10
    # 0 goes back to any distance.
    assert client.put("/api/alerts", headers=H, json={"lightning_alert_mi": 0}).status_code == 200
    assert client.get("/api/alerts", headers=H).json()["lightning_alert_mi"] == 0
    # Out of range is refused, not clamped.
    for bad in (0.5, 41, -1):
        assert client.put("/api/alerts", headers=H,
                          json={"lightning_alert_mi": bad}).status_code in (400, 422), bad


@pytest.mark.parametrize("watches,lightning,runs", [
    (False, True, True),     # its own switch, watches off
    (True, False, False),    # watches on, lightning off
    (True, None, True),      # never set: follows watches (1.8 to 2.5)
    (False, None, False),
])
def test_a_real_tick_dispatches_lightning_on_its_own_switch(client, monkeypatch,
                                                            watches, lightning, runs):
    """A full monitor tick, not just the effective config: the watcher runs
    exactly when the lightning switch says (follow-up review)."""
    from app import alerts, lightning_watch
    calls = []

    async def spy(cfg, devices, now_ms, deliver):
        calls.append(cfg.lightning_alert_mi)
    monkeypatch.setattr(lightning_watch, "check", spy)

    async def no_send(*a, **k):
        return True
    monkeypatch.setattr(alerts, "_deliver", no_send)
    # One open channel (push), or the tick returns before any alert runs.
    from app import apns

    async def push_on():
        return True
    monkeypatch.setattr(apns, "push_configured", push_on)
    body = {"smart_alerts": watches, "lightning_alert_mi": 6}
    if lightning is not None:
        body["lightning_alert"] = lightning
    assert client.put("/api/alerts", headers=H, json=body).status_code == 200
    asyncio.run(alerts.AlertMonitor()._tick())
    assert calls == ([6.0] if runs else [])


def test_a_backup_carries_the_lightning_alert(client):
    client.put("/api/alerts", headers=H, json={"lightning_alert": True, "lightning_alert_mi": 12})
    from app import config_backup as cb
    assert "lightning_alert" in cb._ALERT_PREF_KEYS and "lightning_alert_mi" in cb._ALERT_PREF_KEYS
    assert cb._coerce_alert_pref("lightning_alert_mi", 12) == 12.0
    assert cb._coerce_alert_pref("lightning_alert_mi", 0.5) is cb._INVALID
    assert cb._coerce_alert_pref("lightning_alert_mi", None) is None
    assert cb._coerce_alert_pref("lightning_alert", True) == 1


def test_a_rule_alert_row_names_its_rule(client):
    """Addendum review A26-05: the Mac pairs a logged rule alert with its
    rule by id, not by title. Every other kind carries null."""
    from app import alerts

    async def go():
        cfg = await alerts.effective_config()
        await alerts._deliver(cfg, "s", "b", "Yard: Temperature above 100", "b",
                              kind="rule", mac="AA:BB:CC:00:00:01", route="rule/42")
        await alerts._deliver(cfg, "s", "b", "Yard: offline", "b",
                              kind="device_down", mac="AA:BB:CC:00:00:01")
    asyncio.run(go())
    rows = client.get("/api/alerts/recent", headers=H).json()["alerts"]
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["rule"]["rule_id"] == 42
    assert by_kind["device_down"]["rule_id"] is None
