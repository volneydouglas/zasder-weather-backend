"""2.3 item 5: the Wind Ramp Live Activity — the ramp test against the kv
tick history, open / new-peak / cadence / easing / end, with the wire
contract pinned as exact content-state dicts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

AUTH = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:51"
T0 = 1_768_500_000_000
MIN = 60_000


def _cfg(**kw):
    base = dict(wind_live_activity=True, wind_live_mph=35.0)
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


def _dev(now_ms, gust=None, speed=None, wdir=None, obs_age_ms=0):
    last = {"dateutc": now_ms - obs_age_ms, "tempf": 70.0, "baromrelin": 29.9}
    if gust is not None:
        last["windgustmph"] = gust
    if speed is not None:
        last["windspeedmph"] = speed
    if wdir is not None:
        last["winddir"] = wdir
    return [{"mac": MAC, "name": "Ridge", "lastData": last}]


def _state():
    from app import live_state as ls
    from app import wind_live as wl
    return asyncio.run(ls.load(wl._KV_PREFIX + MAC))


async def _seed(cfg, gust: float, minutes: tuple[int, ...] = (0, 5, 10, 15, 20)):
    """A calm half hour of history, one tick every five minutes."""
    from app import wind_live as wl
    for m in minutes:
        await wl.check(cfg, _dev(T0 + m * MIN, gust=gust, speed=gust / 2, wdir=270),
                       T0 + m * MIN)


def test_wind_lifecycle_pins_the_wire_contract(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import wind_live as wl
    cfg = _cfg()

    def tick(m, gust, speed=None, wdir=270):
        return wl.check(cfg, _dev(T0 + m * MIN, gust=gust, speed=speed, wdir=wdir),
                        T0 + m * MIN)

    async def run():
        await _seed(cfg, 20.0)
        # A ramp: 20 → 40 over the half hour, and 40 is over the 35 line.
        await tick(25, 40.0, speed=25.0)
        # A new peak inside the gap pushes at once.
        await tick(26, 45.0, speed=28.0)
        # Not a peak, inside the gap: silent.
        await tick(28, 44.0, speed=27.0)
        # Past the gap: a cadence beat.
        await tick(31, 44.0, speed=27.0, wdir=275.4)
        # The blow drops under 70% of the peak (31.5) AND under the line.
        for m in (32, 37, 42):
            await tick(m, 20.0, speed=10.0)
        # 15 minutes under 70% of the peak: easing.
        await tick(47, 20.0, speed=10.0)
        for m in (52, 57):
            await tick(m, 20.0, speed=10.0)
        # 30 minutes under the threshold (since 32): the event ends.
        await tick(62, 18.0, speed=9.0, wdir=None)

    asyncio.run(run())

    assert len(calls["start"]) == 1
    activity, payload = calls["start"][0]
    assert activity == "wind"
    aps = payload["aps"]
    assert aps["attributes-type"] == "WindRampActivityAttributes"
    assert aps["attributes"] == {"station": "Ridge", "mac": MAC}
    assert aps["content-state"] == {
        "gustMph": 40.0, "peakMph": 40.0, "peakMs": T0 + 25 * MIN,
        "directionDeg": 270, "speedMph": 25.0, "openedMs": T0 + 25 * MIN,
        "easing": False}

    ups = [p["aps"] for a, p in calls["update"] if a == "wind"]
    assert [u["event"] for u in ups] == ["update"] * 7 + ["end"]
    # The new peak, pushed at once.
    assert ups[0]["content-state"] == {
        "gustMph": 45.0, "peakMph": 45.0, "peakMs": T0 + 26 * MIN,
        "directionDeg": 270, "speedMph": 28.0, "openedMs": T0 + 25 * MIN,
        "easing": False}
    # The cadence beat: the peak survived the silent tick; direction rounds.
    assert ups[1]["content-state"] == {
        "gustMph": 44.0, "peakMph": 45.0, "peakMs": T0 + 26 * MIN,
        "directionDeg": 275, "speedMph": 27.0, "openedMs": T0 + 25 * MIN,
        "easing": False}
    # 37 and 42: still under 15 minutes of calm.
    assert [u["content-state"]["easing"] for u in ups[2:4]] == [False, False]
    # 47: easing (52 and 57 are cadence beats that keep saying so).
    assert ups[4]["content-state"] == {
        "gustMph": 20.0, "peakMph": 45.0, "peakMs": T0 + 26 * MIN,
        "directionDeg": 270, "speedMph": 10.0, "openedMs": T0 + 25 * MIN,
        "easing": True}
    assert [u["content-state"]["easing"] for u in ups[5:7]] == [True, True]
    # The end beat: an absent direction is null, never 0.
    assert ups[7]["content-state"] == {
        "gustMph": 18.0, "peakMph": 45.0, "peakMs": T0 + 26 * MIN,
        "directionDeg": None, "speedMph": 9.0, "openedMs": T0 + 25 * MIN,
        "easing": True}
    assert ups[7]["dismissal-date"] == (T0 + 92 * MIN) // 1000
    st = _state()
    assert "openedMs" not in st and st["hist"]


def test_wind_a_steady_blow_is_not_a_ramp(client, monkeypatch):
    """Blowing 40 all morning, then 42: over the line, but nothing rose."""
    calls = _fake_apns(monkeypatch)
    from app import wind_live as wl
    cfg = _cfg()

    async def run():
        await _seed(cfg, 40.0)
        await wl.check(cfg, _dev(T0 + 25 * MIN, gust=42.0), T0 + 25 * MIN)

    asyncio.run(run())
    assert calls["start"] == []


def test_wind_needs_history_before_it_can_claim_a_rise(client, monkeypatch):
    """One tick after a restart: 50 mph with nothing to compare against
    opens nothing; a window under 15 minutes deep opens nothing either."""
    calls = _fake_apns(monkeypatch)
    from app import wind_live as wl
    cfg = _cfg()

    async def run():
        await wl.check(cfg, _dev(T0, gust=50.0), T0)
        await wl.check(cfg, _dev(T0 + 5 * MIN, gust=50.0), T0 + 5 * MIN)
        await wl.check(cfg, _dev(T0 + 10 * MIN, gust=55.0), T0 + 10 * MIN)
        assert calls["start"] == []
        # The window reaches back 20 minutes now, and the low was 20 → 50.
        await _seed(cfg, 20.0, minutes=(30, 35, 40, 45))
        await wl.check(cfg, _dev(T0 + 50 * MIN, gust=50.0), T0 + 50 * MIN)

    asyncio.run(run())
    assert len(calls["start"]) == 1


def test_wind_threshold_is_the_pref(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import wind_live as wl

    async def run():
        cfg = _cfg(wind_live_mph=50.0)
        await _seed(cfg, 20.0)
        await wl.check(cfg, _dev(T0 + 25 * MIN, gust=40.0), T0 + 25 * MIN)
        assert calls["start"] == []
        cfg = _cfg(wind_live_mph=25.0)
        await wl.check(cfg, _dev(T0 + 26 * MIN, gust=40.0), T0 + 26 * MIN)

    asyncio.run(run())
    assert len(calls["start"]) == 1


def test_wind_pref_gate_and_absent_gust(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import live_state as ls
    from app import wind_live as wl

    async def run():
        off = _cfg(wind_live_activity=False)
        await _seed(off, 20.0)
        await wl.check(off, _dev(T0 + 25 * MIN, gust=60.0), T0 + 25 * MIN)
        assert calls["start"] == []
        assert await ls.load(wl._KV_PREFIX + MAC) == {}
        # No anemometer: no history, no opinion.
        on = _cfg()
        for m in (0, 5, 10, 15, 20, 25):
            await wl.check(on, _dev(T0 + m * MIN), T0 + m * MIN)

    asyncio.run(run())
    assert calls["start"] == [] and _state() == {}


def test_wind_never_opens_stale_and_ends_when_stale(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import live_state as ls
    from app import wind_live as wl
    cfg = _cfg()
    old = ls.STALE_MS + MIN

    async def run():
        await _seed(cfg, 20.0)
        await wl.check(cfg, _dev(T0 + 25 * MIN, gust=50.0, obs_age_ms=old), T0 + 25 * MIN)
        assert calls["start"] == []
        await wl.check(cfg, _dev(T0 + 26 * MIN, gust=50.0), T0 + 26 * MIN)
        assert len(calls["start"]) == 1
        await wl.check(cfg, _dev(T0 + 28 * MIN, gust=50.0, obs_age_ms=old), T0 + 28 * MIN)

    asyncio.run(run())
    ups = [p["aps"] for a, p in calls["update"]]
    assert len(ups) == 1 and ups[0]["event"] == "end"
    assert "openedMs" not in _state()


def test_wind_easing_backs_off_when_the_gusts_return(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import wind_live as wl
    cfg = _cfg()

    async def run():
        await _seed(cfg, 20.0)
        await wl.check(cfg, _dev(T0 + 25 * MIN, gust=50.0), T0 + 25 * MIN)
        for m in (30, 35, 40, 45):
            await wl.check(cfg, _dev(T0 + m * MIN, gust=30.0), T0 + m * MIN)
        # 45: fifteen minutes under 35 (70% of 50) — easing.
        # A gust back to 40 clears it on the next beat.
        await wl.check(cfg, _dev(T0 + 50 * MIN, gust=40.0), T0 + 50 * MIN)

    asyncio.run(run())
    ups = [p["aps"]["content-state"]["easing"] for a, p in calls["update"]]
    assert ups[-2:] == [True, False]


def test_wind_prefs_round_trip(client):
    r = client.get("/api/alerts", headers=AUTH).json()
    assert r["wind_live_activity"] is True and r["wind_live_mph"] == 35.0
    assert client.put("/api/alerts", headers=AUTH,
                      json={"wind_live_activity": False,
                            "wind_live_mph": 45.0}).status_code == 200
    r = client.get("/api/alerts", headers=AUTH).json()
    assert r["wind_live_activity"] is False and r["wind_live_mph"] == 45.0
    assert client.put("/api/alerts", headers=AUTH,
                      json={"wind_live_mph": 500}).status_code == 422


def test_wind_a_creeping_peak_waits_for_the_cadence_beat(client, monkeypatch):
    """A gust rising a few tenths a tick is a new peak every minute; only a
    peak `_PEAK_STEP_MPH` over the last PUSHED one bypasses the gap (a
    probe counted 20 updates in 20 minutes before this rule; iOS drops
    updates that arrive that often). The cadence beat carries the creep."""
    calls = _fake_apns(monkeypatch)
    from app import wind_live as wl
    cfg = _cfg()

    def tick(m, gust):
        return wl.check(cfg, _dev(T0 + m * MIN, gust=gust, speed=gust / 2, wdir=270),
                        T0 + m * MIN)

    async def run():
        await _seed(cfg, 20.0)
        await tick(25, 40.0)                         # opens, pushed peak 40
        for m, g in ((26, 40.3), (27, 40.6), (28, 40.9), (29, 41.2)):
            await tick(m, g)                         # new peaks, all silent
        assert calls["update"] == []
        await tick(30, 41.5)                         # the cadence beat
        assert len(calls["update"]) == 1
        await tick(31, 42.8)                         # +1.3 over 41.5: silent
        assert len(calls["update"]) == 1
        await tick(32, 44.5)                         # +3.0 over 41.5: at once
        assert len(calls["update"]) == 2

    asyncio.run(run())
    ups = [p["aps"]["content-state"] for a, p in calls["update"]]
    assert ups[0]["peakMph"] == 41.5 and ups[0]["peakMs"] == T0 + 30 * MIN
    assert ups[1]["peakMph"] == 44.5 and ups[1]["peakMs"] == T0 + 32 * MIN
    assert _state()["pushedPeak"] == 44.5


def test_wind_an_episode_without_a_pushed_peak_pushes_its_next_peak_once(client, monkeypatch):
    """An episode open across the deploy that added pushedPeak: its first
    new peak pushes (nothing to judge against) and sets the anchor, so
    the creep is gated from then on."""
    calls = _fake_apns(monkeypatch)
    from app import live_state as ls
    from app import wind_live as wl
    cfg = _cfg()

    def tick(m, gust):
        return wl.check(cfg, _dev(T0 + m * MIN, gust=gust, speed=gust / 2, wdir=270),
                        T0 + m * MIN)

    async def run():
        await _seed(cfg, 20.0)
        await tick(25, 40.0)
        st = await ls.load(wl._KV_PREFIX + MAC)
        del st["pushedPeak"]
        await ls.save(wl._KV_PREFIX + MAC, st)
        await tick(26, 40.3)
        assert len(calls["update"]) == 1
        await tick(27, 40.6)
        assert len(calls["update"]) == 1

    asyncio.run(run())
    assert _state()["pushedPeak"] == 40.3
