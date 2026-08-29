"""R7 review regression coverage: the egress gate, freshness floors,
front-group severity, idle-tick WAL churn, coords hardening, the widgets
quota bucket, and the small correctness fixes."""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

H = {"Authorization": "Bearer test-api-token"}


def _ingest(client, mac, name, tempf=90.0, extra=None):
    outdoor = {"tempf": tempf}
    if extra:
        outdoor.update(extra)
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": mac, "name": name},
              "timestamp_utc": "2026-06-01T12:00:00Z",
              "outdoor": outdoor})


def test_webhooks_count_as_an_alert_channel(client, monkeypatch):
    """R1: with email AND push unconfigured but a webhook registered, the
    tick must still run and a fired rule must reach the webhook."""
    import json as _json
    import app.alerts as al
    from app import webhooks, db

    _ingest(client, "AA:BB:CC:00:00:80", "Gated", tempf=105.0)
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above",
                      "threshold": 100})
    sent = []

    async def fake_send(hook, payload):
        sent.append(_json.loads(payload))
    monkeypatch.setattr(webhooks, "_send", fake_send)

    async def run():
        await db.create_webhook("https://example.com/hook")
        await al.AlertMonitor()._tick()
        if al._WEBHOOK_TASKS:
            await asyncio.gather(*al._WEBHOOK_TASKS)
    asyncio.run(run())
    assert any(p.get("kind") == "rule" for p in sent), \
        "webhook-only setups must still evaluate and dispatch alerts"


def test_share_fanout_runs_without_alert_transport(client, monkeypatch):
    """R1: community uploads are their own delivery surface."""
    import app.alerts as al
    from app import share_targets as st

    _ingest(client, "AA:BB:CC:00:00:81", "Uploader")
    called = []

    async def fake_check(devices, now_ms):
        called.append(len(devices))
    monkeypatch.setattr(st, "check", fake_check)
    asyncio.run(al.AlertMonitor()._tick())
    assert called, "share fan-out must run with email+push unconfigured"


def test_anchor_freshness_floor(client):
    """R4: an anchor older than max_age_ms returns None instead of letting
    a '1h' delta span a multi-hour gap."""
    from app import db

    mac = "AA:BB:CC:00:00:82"
    now = int(time.time() * 1000)

    async def run():
        await db.insert_observations(mac, [
            {"dateutc": now - 5 * 3_600_000, "tempf": 92.0}])
        floored = await db.value_at_or_before(mac, "tempf",
                                              now - 3_600_000,
                                              max_age_ms=3_600_000)
        unfloored = await db.value_at_or_before(mac, "tempf",
                                                now - 3_600_000)
        return floored, unfloored
    floored, unfloored = asyncio.run(run())
    assert unfloored == 92.0
    assert floored is None, "stale anchor must not fabricate the window"


def test_front_group_keeps_outflow_warning_tier(client, monkeypatch):
    """R5: a grouped front containing outflow inherits the warning tier
    and breaks quiet hours."""
    import app.alerts as al
    from app import db, apns

    monkeypatch.setattr(al, "in_quiet_hours", lambda *a, **kw: True)
    pushes = []

    async def fake_push_configured():
        return True

    async def fake_send_to_all(title, body, interruption_level=None):
        pushes.append(title)
        return {"sent": 1}
    monkeypatch.setattr(apns, "push_configured", fake_push_configured)
    monkeypatch.setattr(apns, "send_to_all", fake_send_to_all)

    mac = "AA:BB:CC:00:00:83"
    now = int(time.time() * 1000)

    async def run():
        # History ~62 min and 10 min ago: warm, calm, higher pressure → current reading
        # computes an outflow signature (Δp≥0.02 up, ΔT≤−3, gust≥25) plus a
        # 1h temp drop, so the two group into a front.
        await db.insert_observations(mac, [
            {"dateutc": now - 3_700_000, "tempf": 95.0,
             "windspeedmph": 3.0, "baromrelin": 29.80},
            {"dateutc": now - 600_000, "tempf": 94.0,
             "windspeedmph": 3.0, "baromrelin": 29.80}])
        dev = {"mac": mac, "name": "Fronty", "lastData": {
            "tempf": 80.0, "windspeedmph": 20.0, "windgustmph": 30.0,
            "baromrelin": 29.84, "dateutc": now}}
        cfg = await al.effective_config()
        await al.AlertMonitor()._check_smart_alerts(cfg, [dev], now)
        return await db.recent_alerts(5)
    hist = asyncio.run(run())
    front = [h for h in hist if h["kind"] == "front"]
    assert front, "expected a grouped front delivery"
    assert front[0]["severity"] == "warning"
    assert pushes, "warning tier must break quiet hours"


def test_quiet_hours_must_be_set_as_a_pair(client):
    r = client.put("/api/alerts", headers=H, json={"quiet_start_min": 1320})
    assert r.status_code == 400
    r = client.put("/api/alerts", headers=H,
                   json={"quiet_start_min": 1320, "quiet_end_min": 420})
    assert r.status_code == 200
    r = client.put("/api/alerts", headers=H,
                   json={"quiet_start_min": -1, "quiet_end_min": -1})
    assert r.status_code == 200


def test_manual_storm_start_needs_global_toggle(client):
    _ingest(client, "AA:BB:CC:00:00:84", "Stormless")
    client.put("/api/alerts", headers=H, json={"storm_summary": False})
    r = client.post("/api/storm/watch/start", headers=H,
                    json={"mac": "AA:BB:CC:00:00:84"})
    assert r.status_code == 409
    client.put("/api/alerts", headers=H, json={"storm_summary": True})
    r = client.post("/api/storm/watch/start", headers=H,
                    json={"mac": "AA:BB:CC:00:00:84"})
    assert r.status_code == 200


def test_lightning_idle_tick_writes_nothing(client, monkeypatch):
    """R9: an idle lightning-capable station must not rewrite server_kv
    every tick."""
    from app import lightning_watch as lw, db

    writes = []
    real_set_kv = db.set_kv

    async def counting_set_kv(key, value):
        if key.startswith("lightning."):
            writes.append(key)
        return await real_set_kv(key, value)
    monkeypatch.setattr(db, "set_kv", counting_set_kv)

    async def deliver(*a, **kw):
        return True

    now = int(time.time() * 1000)
    dev = [{"mac": "AA:BB:CC:00:00:85", "name": "Zap",
            "lastData": {"lightningcount": 7}}]

    async def run():
        cfg = await __import__("app.alerts", fromlist=["x"]).effective_config()
        await lw.check(cfg, dev, now, deliver)          # baseline sight
        baseline_writes = len(writes)
        for i in range(5):                              # idle ticks
            await lw.check(cfg, dev, now + (i + 1) * 60_000, deliver)
        return baseline_writes
    baseline_writes = asyncio.run(run())
    assert len(writes) == baseline_writes, \
        "idle ticks must not churn server_kv"


def test_nws_bad_coords_skips_station_not_pass(client, monkeypatch):
    """R10: one malformed coords record skips that station only."""
    from app import nws_watch as nw

    polled = []

    async def fake_fetch(lat, lon):
        polled.append((lat, lon))
        return []
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    monkeypatch.setattr(nw, "_last_poll_ms", {})

    devs = [
        {"mac": "BAD", "name": "Broken",
         "info": {"coords": {"coords": {"lat": "not-a-number", "lon": 1.0}}}},
        {"mac": "OK", "name": "Fine",
         "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}},
    ]

    async def run():
        cfg = await __import__("app.alerts", fromlist=["x"]).effective_config()

        async def deliver(*a, **kw):
            return True
        await nw.check(cfg, devs, int(time.time() * 1000), deliver)
    asyncio.run(run())
    assert polled == [(33.3, -111.9)], \
        "the good station must still be polled"


def test_battery_recovery_notifies_and_persists_after_deliver(client):
    from app import health_watch as hw, db

    mac = "AA:BB:CC:00:00:86"
    now = int(time.time() * 1000)
    delivered = []

    def dev(flag):
        return [{"mac": mac, "name": "Batty",
                 "lastData": {"tempf": 70.0, "dateutc": now,
                              "battout": flag}}]

    async def ok_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append(kw.get("kind"))
        return True

    async def run():
        from app.alerts import effective_config
        cfg = await effective_config()
        await hw.check(cfg, dev(0), now, ok_deliver)        # low fires
        await hw.check(cfg, dev(1), now + 60_000, ok_deliver)  # recovery
    asyncio.run(run())
    assert "battery" in delivered
    assert "battery_recovered" in delivered


def test_wet_bulb_at_saturation_is_dry_bulb():
    from app.derived import wet_bulb_f, delta_t_c
    assert wet_bulb_f(60.0, 100.0) == 60.0
    assert delta_t_c(60.0, 100.0) == 0.0
    # The Stull window still applies below saturation.
    assert wet_bulb_f(60.0, 50.0) is not None
    assert wet_bulb_f(60.0, 3.0) is None


def test_bucketed_history_carries_air_fields(client):
    """CodeRabbit round 2: air fields must survive auto-bucketing (>6h) —
    they existed in raw history but vanished when downsampling kicked in."""
    import asyncio, time
    from app import db

    mac = "5D:5D:07:00:AA:01"
    now = int(time.time() * 1000)

    async def run():
        rows = [{"dateutc": now - h * 3_600_000, "tempf": 70.0 + h,
                 "pm25": 2.0 + h, "co2": 400.0 + 10 * h}
                for h in range(8)]
        await db.insert_observations(mac, rows)
        return await db.history(mac, now - 8 * 3_600_000, now)
    hist = asyncio.run(run())
    assert hist, "bucketed rows expected"
    r = hist[0]
    assert r.get("pm25") is not None
    assert r.get("co2") is not None
    assert r.get("pm25_max") is not None
    assert r.get("co2_max") is not None


def test_webhooks_deliver_even_when_email_fails(client, monkeypatch):
    """CodeRabbit round 2: webhooks are a CHANNEL — an alert must reach
    them (and count as handled) even when SMTP fails every attempt."""
    import asyncio
    import json as _json
    import app.alerts as al
    from app import webhooks, db

    def failing_send(*a, **kw):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(al, "_send_sync", failing_send)

    sent = []
    async def fake_send(hook, payload):
        sent.append(_json.loads(payload))
    monkeypatch.setattr(webhooks, "_send", fake_send)

    async def run():
        await db.create_webhook("https://example.com/hook")
        await db.set_alert_prefs(enabled=1, recipients="a@b.co")
        cfg = await al.effective_config()
        handled = await al._deliver(cfg, "s", "b", "Hooked despite smtp",
                                    "b", kind="rule", mac="AA")
        if al._WEBHOOK_TASKS:
            await asyncio.gather(*al._WEBHOOK_TASKS)
        return handled
    handled = asyncio.run(run())
    assert handled, "webhook channel must count as delivery"
    assert any(p["title"] == "Hooked despite smtp" for p in sent)


def test_nws_same_alert_across_stations_pushes_once(client, monkeypatch):
    """Three stations in one backyard share one sky — the same NWS alert
    id must push exactly once, not once per station (Volney, 2026-08-26)."""
    import asyncio, time
    from app import nws_watch as nw

    calls = []

    async def deliver(cfg, subject, body, pt, pb, **kw):
        calls.append(pt)
        return True

    alert = [{"id": "urn:heat:1", "severity": "Extreme",
              "event": "Extreme Heat Warning", "headline": "Stay inside"}]

    async def fake_fetch(lat, lon):
        return list(alert)
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    monkeypatch.setattr(nw, "_last_poll_ms", {})

    def station(mac, name):
        return {"mac": mac, "name": name,
                "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}
    devs = [station("AA:00", "Davis"), station("AA:01", "Tempest"),
            station("AA:02", "Atlas")]

    async def run():
        from app.alerts import effective_config
        cfg = await effective_config()
        now = int(time.time() * 1000)
        await nw.check(cfg, devs, now, deliver)
        # Staggered cadences: a later tick where only station 3 is due
        # must ALSO stay quiet on the globally-seen id.
        nw._last_poll_ms["AA:02"] = 0
        await nw.check(cfg, devs, now + 60_000, deliver)
    asyncio.run(run())
    assert len(calls) == 1, calls
    assert "Extreme Heat Warning" in calls[0]


def test_nws_legacy_per_station_seen_seeds_global(client, monkeypatch):
    """Upgrade path: ids already pushed under the old per-station keys
    must not re-push once as 'new' when the global set first builds."""
    import asyncio, json, time
    from app import nws_watch as nw, db

    calls = []

    async def deliver(cfg, subject, body, pt, pb, **kw):
        calls.append(pt)
        return True

    async def fake_fetch(lat, lon):
        return [{"id": "urn:old:1", "severity": "Severe",
                 "event": "Severe Thunderstorm Warning"}]
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    monkeypatch.setattr(nw, "_last_poll_ms", {})

    dev = [{"mac": "AA:10", "name": "Davis",
            "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]

    async def run():
        await db.set_kv("nws_watch.seen.AA:10", json.dumps(["urn:old:1"]))
        from app.alerts import effective_config
        cfg = await effective_config()
        await nw.check(cfg, dev, int(time.time() * 1000), deliver)
    asyncio.run(run())
    assert calls == [], "legacy-seen id must not re-push after the upgrade"
