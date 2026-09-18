"""2.3 item 5: the Lightning Live Activity — open / update / end on the
heat_watch rails, with the wire contract pinned as exact content-state
dicts (the iOS decode struct mirrors these keys)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

AUTH = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:41"
# A fixed clock keeps every stamp in the pinned dicts literal.
T0 = 1_768_500_000_000
MIN = 60_000


def _cfg(**kw):
    base = dict(lightning_live_activity=True, lightning_live_mi=10.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_apns(monkeypatch, sent: int = 1):
    from app import apns
    calls = {"start": [], "update": []}

    async def fake_start(payload, title, body, **kw):
        calls["start"].append((kw.get("activity"), payload))
        return {"sent": sent, "dead": [], "failed": 0}

    async def fake_update(activity, payload, title, body, **kw):
        calls["update"].append((activity, payload))
        return {"sent": 1, "dead": [], "failed": 0}

    monkeypatch.setattr(apns, "send_live_activity_start", fake_start)
    monkeypatch.setattr(apns, "send_live_activity_update", fake_update)
    return calls


def _dev(now_ms, count=None, dist=None, hour=None, obs_age_ms=0, **extra):
    last = {"dateutc": now_ms - obs_age_ms, "tempf": 80.0,
            "baromrelin": 29.9, "windspeedmph": 3.0}
    if count is not None:
        last["lightningcount"] = count
    if dist is not None:
        last["lightning_distance_mi"] = dist
    if hour is not None:
        last["lightning_last_1hr"] = hour
    last.update(extra)
    return [{"mac": MAC, "name": "Tempest", "lastData": last}]


def _state():
    from app import lightning_live as ll
    from app import live_state as ls
    return asyncio.run(ls.load(ll._KV_PREFIX + MAC))


def test_lightning_lifecycle_pins_the_wire_contract(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll
    cfg = _cfg()

    async def run():
        # First sight of the counter: a baseline, never an episode.
        await ll.check(cfg, _dev(T0, count=5, dist=8.0), T0)
        # A new strike 8 mi out opens the card.
        await ll.check(cfg, _dev(T0 + MIN, count=6, dist=8.0), T0 + MIN)
        # Two minutes on, another strike 3 mi closer: inside the five-
        # minute gap, but the closer-by-two-miles rule pushes at once.
        await ll.check(cfg, _dev(T0 + 2 * MIN, count=7, dist=5.0), T0 + 2 * MIN)
        # No new strike, inside the gap: silent.
        await ll.check(cfg, _dev(T0 + 4 * MIN, count=7, dist=5.0), T0 + 4 * MIN)
        # Past the gap, still no strike: the beat carries the remembered
        # trend and the all-clear clock (last strike + 30 min).
        await ll.check(cfg, _dev(T0 + 8 * MIN, count=7, dist=5.0), T0 + 8 * MIN)
        # 30 minutes after the last strike: the card ends.
        await ll.check(cfg, _dev(T0 + 32 * MIN, count=7, dist=5.0), T0 + 32 * MIN)

    asyncio.run(run())

    assert len(calls["start"]) == 1
    activity, payload = calls["start"][0]
    assert activity == "lightning"
    aps = payload["aps"]
    assert aps["attributes-type"] == "LightningWatchActivityAttributes"
    assert aps["attributes"] == {"station": "Tempest", "mac": MAC}
    assert aps["content-state"] == {
        "distanceMi": 8.0, "strikes1h": 1, "lastStrikeMs": T0 + MIN,
        "openedMs": T0 + MIN, "trend": "steady", "allClearAtMs": None,
        "ended": False}

    ups = [p["aps"] for a, p in calls["update"] if a == "lightning"]
    assert [u["event"] for u in ups] == ["update", "update", "end"]
    assert ups[0]["content-state"] == {
        "distanceMi": 5.0, "strikes1h": 2, "lastStrikeMs": T0 + 2 * MIN,
        "openedMs": T0 + MIN, "trend": "closer", "allClearAtMs": None,
        "ended": False}
    assert ups[1]["content-state"] == {
        "distanceMi": 5.0, "strikes1h": 2, "lastStrikeMs": T0 + 2 * MIN,
        "openedMs": T0 + MIN, "trend": "closer",
        "allClearAtMs": T0 + 32 * MIN, "ended": False}
    # The end beat is the only one that says so: the card reads "all
    # clear" for the linger, not the last live trend headline.
    assert ups[2]["content-state"] == {
        "distanceMi": 5.0, "strikes1h": 2, "lastStrikeMs": T0 + 2 * MIN,
        "openedMs": T0 + MIN, "trend": "closer",
        "allClearAtMs": T0 + 32 * MIN, "ended": True}
    assert ups[2]["dismissal-date"] == (T0 + 62 * MIN) // 1000
    # The episode is gone; the counter baseline survives for the next one.
    st = _state()
    assert "openedMs" not in st and st["baseline"] == 7


def test_lightning_push_gap_holds_a_farther_strike(client, monkeypatch):
    """Inside the gap, a strike that is NOT two miles closer stays silent
    (the fatigue rule); the next scheduled beat carries it as 'farther'."""
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll
    cfg = _cfg()

    async def run():
        await ll.check(cfg, _dev(T0, count=1, dist=6.0), T0)
        await ll.check(cfg, _dev(T0 + MIN, count=2, dist=6.0), T0 + MIN)      # opens
        await ll.check(cfg, _dev(T0 + 2 * MIN, count=3, dist=9.0), T0 + 2 * MIN)
        await ll.check(cfg, _dev(T0 + 3 * MIN, count=4, dist=5.0), T0 + 3 * MIN)
        await ll.check(cfg, _dev(T0 + 7 * MIN, count=4, dist=5.0), T0 + 7 * MIN)

    asyncio.run(run())
    ups = [p["aps"]["content-state"] for a, p in calls["update"]]
    assert len(ups) == 1
    assert ups[0]["trend"] == "closer" and ups[0]["distanceMi"] == 5.0
    assert ups[0]["strikes1h"] == 3


def test_lightning_uses_the_detectors_hour_count_when_larger(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll
    cfg = _cfg()

    async def run():
        await ll.check(cfg, _dev(T0, count=10, dist=4.0, hour=0), T0)
        await ll.check(cfg, _dev(T0 + MIN, count=13, dist=4.0, hour=12), T0 + MIN)

    asyncio.run(run())
    assert calls["start"][0][1]["aps"]["content-state"]["strikes1h"] == 12


def test_lightning_refuses_absent_fields_and_far_strikes(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll
    cfg = _cfg()

    async def run():
        # No detector at all: no opinion, no state.
        await ll.check(cfg, _dev(T0), T0)
        await ll.check(cfg, _dev(T0 + MIN), T0 + MIN)
        # A counter with no distance: a rise is not a placeable strike.
        await ll.check(cfg, _dev(T0, count=1), T0)
        await ll.check(cfg, _dev(T0 + MIN, count=2), T0 + MIN)
        # A strike beyond the threshold.
        await ll.check(cfg, _dev(T0 + 2 * MIN, count=3, dist=12.0), T0 + 2 * MIN)
        # A counter RESET that lands above zero is not new lightning.
        await ll.check(cfg, _dev(T0 + 3 * MIN, count=1, dist=2.0), T0 + 3 * MIN)

    asyncio.run(run())
    assert calls["start"] == [] and calls["update"] == []
    assert "openedMs" not in _state()


def test_lightning_threshold_is_the_pref(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll

    async def run():
        await ll.check(_cfg(lightning_live_mi=15.0), _dev(T0, count=1, dist=12.0), T0)
        await ll.check(_cfg(lightning_live_mi=15.0),
                       _dev(T0 + MIN, count=2, dist=12.0), T0 + MIN)

    asyncio.run(run())
    assert len(calls["start"]) == 1


def test_lightning_pref_gate(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll
    off = _cfg(lightning_live_activity=False)

    async def run():
        await ll.check(off, _dev(T0, count=1, dist=3.0), T0)
        await ll.check(off, _dev(T0 + MIN, count=2, dist=3.0), T0 + MIN)

    asyncio.run(run())
    assert calls["start"] == []


def test_lightning_never_opens_stale_and_ends_when_stale(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll
    from app import live_state as ls
    cfg = _cfg()
    old = ls.STALE_MS + MIN

    async def run():
        await ll.check(cfg, _dev(T0, count=1, dist=3.0), T0)
        # A rise seen on a stale observation opens nothing.
        await ll.check(cfg, _dev(T0 + MIN, count=2, dist=3.0, obs_age_ms=old), T0 + MIN)
        assert calls["start"] == []
        # Fresh again with a new strike: opens.
        await ll.check(cfg, _dev(T0 + 2 * MIN, count=3, dist=3.0), T0 + 2 * MIN)
        assert len(calls["start"]) == 1
        # The station goes quiet: the card ends, well before the all-clear.
        await ll.check(cfg, _dev(T0 + 4 * MIN, count=3, dist=3.0, obs_age_ms=old), T0 + 4 * MIN)

    asyncio.run(run())
    ups = [p["aps"] for a, p in calls["update"]]
    assert len(ups) == 1 and ups[0]["event"] == "end"
    assert "openedMs" not in _state()


def test_lightning_tokenless_start_is_not_recorded(client, monkeypatch):
    """No token accepted the start: the episode is not recorded, and the
    NEXT strike retries (updates can never conjure a missing Activity)."""
    calls = _fake_apns(monkeypatch, sent=0)
    from app import lightning_live as ll
    from app import live_state as ls
    cfg = _cfg()

    async def run():
        await ll.check(cfg, _dev(T0, count=1, dist=3.0), T0)
        await ll.check(cfg, _dev(T0 + MIN, count=2, dist=3.0), T0 + MIN)
        assert "openedMs" not in await ls.load(ll._KV_PREFIX + MAC)
        await ll.check(cfg, _dev(T0 + 2 * MIN, count=2, dist=3.0), T0 + 2 * MIN)
        await ll.check(cfg, _dev(T0 + 3 * MIN, count=3, dist=3.0), T0 + 3 * MIN)

    asyncio.run(run())
    assert len(calls["start"]) == 2 and calls["update"] == []


def test_lightning_ignores_air_monitors(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import lightning_live as ll
    dev = [{"mac": "AA:BB:CC:00:00:42", "name": "Air",
            "lastData": {"dateutc": T0, "pm25": 4, "lightningcount": 3,
                         "lightning_distance_mi": 1.0}}]
    asyncio.run(ll.check(_cfg(), dev, T0))
    dev[0]["lastData"]["lightningcount"] = 4
    asyncio.run(ll.check(_cfg(), dev, T0 + MIN))
    assert calls["start"] == []


def test_lightning_prefs_round_trip(client):
    r = client.get("/api/alerts", headers=AUTH).json()
    assert r["lightning_live_activity"] is True and r["lightning_live_mi"] == 10.0
    assert client.put("/api/alerts", headers=AUTH,
                      json={"lightning_live_activity": False,
                            "lightning_live_mi": 15.5}).status_code == 200
    r = client.get("/api/alerts", headers=AUTH).json()
    assert r["lightning_live_activity"] is False and r["lightning_live_mi"] == 15.5
    # Bounded: a threshold outside the model's range is refused.
    assert client.put("/api/alerts", headers=AUTH,
                      json={"lightning_live_mi": 0}).status_code == 422


def test_update_tokens_accept_the_new_activities(client):
    for act in ("lightning", "wind", "freeze"):
        r = client.post("/api/push/live-activity-token", headers=AUTH,
                        json={"token": act * 8, "env": "production",
                              "kind": "update", "activity": act})
        assert r.status_code == 200, act
    r = client.post("/api/push/live-activity-token", headers=AUTH,
                    json={"token": "x" * 16, "env": "production",
                          "kind": "update", "activity": "disco"})
    assert r.status_code == 400


def test_one_failing_card_never_stops_its_siblings(client, monkeypatch):
    """The tick wiring: each of the three checks is wrapped on its own, so
    a lightning exception still lets the wind and freeze cards run (the
    lightning_watch guard shape)."""
    import app.alerts as alerts
    from app import freeze_live, lightning_live, wind_live
    from app.alerts import AlertMonitor
    ran: list[str] = []

    async def push_on():
        return True

    async def boom(cfg, devices, now_ms):
        raise RuntimeError("lightning card fell over")

    def record(label):
        async def _check(cfg, devices, now_ms):
            ran.append(label)
        return _check

    monkeypatch.setattr(alerts.apns, "push_configured", push_on)
    monkeypatch.setattr(lightning_live, "check", boom)
    monkeypatch.setattr(wind_live, "check", record("wind"))
    monkeypatch.setattr(freeze_live, "check", record("freeze"))
    asyncio.run(AlertMonitor()._tick())
    assert ran == ["wind", "freeze"]
