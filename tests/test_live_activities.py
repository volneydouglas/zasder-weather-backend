"""1.8 Live Activity infrastructure: per-activity update tokens, the Storm
Watch lifecycle (start once per episode, throttled updates, end beat), the
heat-day watcher, and the manual storm-watch trigger."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

AUTH = {"Authorization": "Bearer test-api-token"}
INGEST = {"Authorization": "Bearer test-ingest-token"}


def _cfg(**kw):
    base = dict(storm_summary=True, heat_day=True, heat_day_threshold_f=100.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_apns(monkeypatch):
    from app import apns
    calls = {"start": [], "update": []}

    async def fake_start(payload, title, body, **kw):
        calls["start"].append(payload)
        return {"sent": 1, "dead": [], "failed": 0}

    async def fake_update(activity, payload, title, body, **kw):
        calls["update"].append((activity, payload))
        return {"sent": 1, "dead": [], "failed": 0}

    monkeypatch.setattr(apns, "send_live_activity_start", fake_start)
    monkeypatch.setattr(apns, "send_live_activity_update", fake_update)
    return calls


def test_update_token_kind_and_activity_scoping(client):
    from app import db
    r = client.post("/api/push/live-activity-token", headers=AUTH,
                    json={"token": "a" * 16, "env": "production",
                          "kind": "start"})
    assert r.status_code == 200
    # update without an activity is a caller bug, not a silent NULL row
    r = client.post("/api/push/live-activity-token", headers=AUTH,
                    json={"token": "b" * 16, "kind": "update"})
    assert r.status_code == 400
    r = client.post("/api/push/live-activity-token", headers=AUTH,
                    json={"token": "b" * 16, "env": "production",
                          "kind": "update", "activity": "storm"})
    assert r.status_code == 200
    r = client.post("/api/push/live-activity-token", headers=AUTH,
                    json={"token": "c" * 16, "env": "production",
                          "kind": "update", "activity": "heat"})
    assert r.status_code == 200
    r = client.post("/api/push/live-activity-token", headers=AUTH,
                    json={"token": "d" * 16, "kind": "update",
                          "activity": "disco"})
    assert r.status_code == 400

    storm = asyncio.run(db.list_live_activity_tokens("update",
                                                     activity="storm"))
    assert [t["token"] for t in storm] == ["b" * 16]
    starts = asyncio.run(db.list_live_activity_tokens("start"))
    assert [t["token"] for t in starts] == ["a" * 16]


def test_storm_watch_lifecycle(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import db, storm_watch
    dev = {"mac": "AA:BB:CC:00:00:01", "name": "Test Station"}
    now = int(time.time() * 1000)
    started = now - 600_000
    cfg = _cfg()

    async def run():
        # Episode opens → exactly one Activity start.
        await storm_watch.on_open_tick(cfg, dev, started, now, "dailyrainin")
        # Same episode, inside the push gap → neither a second start nor an
        # update (a server restart must never duplicate the Activity).
        await storm_watch.on_open_tick(cfg, dev, started, now + 30_000,
                                       "dailyrainin")
        # Past the gap → one content-state update.
        await storm_watch.on_open_tick(cfg, dev, started,
                                       now + 4 * 60_000, "dailyrainin")
        # Close after a delivered summary → silent end beat + cleared row.
        await storm_watch.on_closed(cfg, dev, started, now + 5 * 60_000,
                                    now + 5 * 60_000, "dailyrainin",
                                    reported=True)

    asyncio.run(run())

    assert len(calls["start"]) == 1
    aps = calls["start"][0]["aps"]
    assert aps["event"] == "start"
    assert aps["attributes-type"] == "StormWatchActivityAttributes"
    assert aps["attributes"] == {"stationName": "Test Station"}
    assert aps["content-state"]["startMs"] == started
    assert aps["content-state"]["ended"] is False

    assert [a for a, _ in calls["update"]] == ["storm", "storm"]
    mid = calls["update"][0][1]["aps"]
    assert mid["event"] == "update" and "alert" not in mid
    end = calls["update"][1][1]["aps"]
    assert end["event"] == "end"
    assert end["content-state"]["ended"] is True
    # reported=True → the summary notification already rang; end is silent
    assert "alert" not in end
    assert asyncio.run(db.get_storm_watch_la(dev["mac"])) is None


def test_storm_watch_silent_close_is_audible(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import storm_watch
    dev = {"mac": "AA:BB:CC:00:00:02", "name": "Quiet"}
    now = int(time.time() * 1000)
    cfg = _cfg()

    async def run():
        await storm_watch.on_open_tick(cfg, dev, now - 300_000, now, None)
        await storm_watch.on_closed(cfg, dev, now - 300_000, now, now, None,
                                    reported=False)

    asyncio.run(run())
    end = calls["update"][-1][1]["aps"]
    # No summary rang, so the Activity's end carries the only word.
    assert end["event"] == "end" and "alert" in end


def test_storm_watch_disabled_pref_is_inert(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import storm_watch
    dev = {"mac": "AA:BB:CC:00:00:03", "name": "Off"}
    now = int(time.time() * 1000)
    asyncio.run(storm_watch.on_open_tick(_cfg(storm_summary=False), dev,
                                         now - 60_000, now, None))
    assert calls["start"] == [] and calls["update"] == []


def test_heat_day_lifecycle(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import db, heat_watch
    cfg = _cfg()
    now = int(time.time() * 1000)

    def dev(t):
        return [{"mac": "AA:BB:CC:00:00:04", "name": "Hot",
                 "lastData": {"tempf": t}}]

    async def run():
        # Below threshold: nothing opens.
        await heat_watch.check(cfg, dev(98.0), now)
        # Crossing opens the day.
        await heat_watch.check(cfg, dev(101.0), now + 60_000)
        # Inside the push gap: silent, but the high still tracks.
        await heat_watch.check(cfg, dev(103.0), now + 5 * 60_000)
        # Past the gap: an update carrying the running high.
        await heat_watch.check(cfg, dev(104.0), now + 21 * 60_000)
        # The heat breaks (threshold − 8): the day ends.
        await heat_watch.check(cfg, dev(91.0), now + 40 * 60_000)

    asyncio.run(run())

    assert len(calls["start"]) == 1
    aps = calls["start"][0]["aps"]
    assert aps["attributes-type"] == "HeatDayActivityAttributes"
    assert aps["content-state"]["currentF"] == 101.0

    heats = [p for a, p in calls["update"] if a == "heat"]
    assert len(heats) == 2
    assert heats[0]["aps"]["content-state"]["hiF"] == 104.0
    assert heats[-1]["aps"]["event"] == "end"
    # The running high survived the silent tick between pushes.
    assert heats[-1]["aps"]["content-state"]["hiF"] == 104.0
    assert asyncio.run(heat_watch._get_state()) == {}


def test_heat_day_opt_in(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    from app import heat_watch
    now = int(time.time() * 1000)
    devices = [{"mac": "AA:BB:CC:00:00:05", "name": "N",
                "lastData": {"tempf": 110.0}}]
    asyncio.run(heat_watch.check(_cfg(heat_day=False), devices, now))
    assert calls["start"] == []


def test_storm_watch_manual_start(client):
    from app import db
    r = client.post("/api/storm/watch/start", headers=AUTH,
                    json={"mac": "DE:AD:BE:EF:00:00"})
    assert r.status_code == 404

    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.post("/ingest/custom", headers=INGEST,
                    json={"device": {"id": "AABBCC000006", "name": "Manual"},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": 88.0}, "source": "test"})
    assert r.status_code == 200
    mac = "AA:BB:CC:00:00:06"
    r = client.post("/api/storm/watch/start", headers=AUTH,
                    json={"mac": mac})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["already_open"] is False
    state = asyncio.run(db.get_storm_state(mac))
    assert state and state["started_ms"] == j["started_ms"]

    # Idempotent: a nervous double-tap reports the open episode.
    r2 = client.post("/api/storm/watch/start", headers=AUTH,
                     json={"mac": mac})
    assert r2.json()["already_open"] is True
    assert r2.json()["started_ms"] == j["started_ms"]


def test_heat_prefs_roundtrip(client):
    r = client.put("/api/alerts", headers=AUTH,
                   json={"heat_day": True, "heat_day_threshold_f": 105})
    assert r.status_code == 200
    g = client.get("/api/alerts", headers=AUTH).json()
    assert g["heat_day"] is True
    assert g["heat_day_threshold_f"] == 105.0


def test_widget_push_throttle_and_freshness(client, monkeypatch):
    from app import apns, widget_push
    sent = []

    async def fake_refresh():
        sent.append(1)
        return {"sent": 1, "dead": [], "failed": 0}

    monkeypatch.setattr(apns, "send_widget_refresh", fake_refresh)
    now = int(time.time() * 1000)

    def dev(ts):
        return [{"mac": "AA", "lastData": {"dateutc": ts}}]

    gap = widget_push._MIN_GAP_MS
    async def run():
        await widget_push.check(dev(now), now)                 # fresh → push
        await widget_push.check(dev(now + 1000), now + gap // 2)  # in gap → no
        await widget_push.check(dev(now + 1000), now + gap + 60_000)  # fresh
    asyncio.run(run())
    assert len(sent) == 2, "first push + one post-gap fresh-data push"

    async def run2():
        # Past the gap again with the SAME newest reading → idle, no push.
        await widget_push.check(dev(now + 1000), now + 2 * gap + 120_000)
    asyncio.run(run2())
    assert len(sent) == 2

