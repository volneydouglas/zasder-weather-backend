"""Pillar A batch 1: the rate-of-change family, pipe-freeze, outflow
detection, the frost science refinement, front-passage grouping, and the
first-frost seasonal one-shot."""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app.alerts import (smart_condition, smart_cleared,  # noqa: E402
                        build_smart_message)

BASE = dict(frost_f=35.0, heat_f=105.0, drop_inhg=0.06)


def test_temp_drop_rate():
    assert smart_condition("temp_drop", temp_delta_1h=-14.0, **BASE)
    assert not smart_condition("temp_drop", temp_delta_1h=-8.0, **BASE)
    assert not smart_condition("temp_drop", temp_delta_1h=None, **BASE)
    assert smart_cleared("temp_drop", temp_delta_1h=-2.0, **BASE)
    assert not smart_cleared("temp_drop", temp_delta_1h=-10.0, **BASE)


def test_wind_ramp_needs_both_rise_and_absolute():
    assert smart_condition("wind_ramp", wind=20.0, wind_delta_1h=15.0, **BASE)
    # A rise to a still-light wind is not a ramp worth waking anyone for.
    assert not smart_condition("wind_ramp", wind=13.0, wind_delta_1h=13.0,
                               **BASE)
    assert not smart_condition("wind_ramp", wind=20.0, wind_delta_1h=5.0,
                               **BASE)
    assert smart_cleared("wind_ramp", wind=8.0, **BASE)


def test_pipe_freeze_is_sustained_not_instantaneous():
    assert smart_condition("pipe_freeze", tempf=18.0, temp_1h_ago=19.5, **BASE)
    # A fresh plunge (an hour ago it was mild) is not yet SUSTAINED.
    assert not smart_condition("pipe_freeze", tempf=18.0, temp_1h_ago=30.0,
                               **BASE)
    assert not smart_condition("pipe_freeze", tempf=18.0, temp_1h_ago=None,
                               **BASE)
    assert smart_cleared("pipe_freeze", tempf=25.0, **BASE)
    assert not smart_cleared("pipe_freeze", tempf=21.0, **BASE)


def test_outflow_signature_needs_all_three():
    ok = dict(pressure_delta_10m=0.02, temp_delta_10m=-4.0, gust=28.0)
    assert smart_condition("outflow", **ok, **BASE)
    assert not smart_condition("outflow", **{**ok, "gust": 12.0}, **BASE)
    assert not smart_condition("outflow",
                               **{**ok, "pressure_delta_10m": 0.005}, **BASE)
    assert not smart_condition("outflow",
                               **{**ok, "temp_delta_10m": -1.0}, **BASE)
    assert smart_cleared("outflow", pressure_delta_10m=0.0, **BASE)


def test_frost_science_suppressors_only_when_data_exists():
    # Classic radiative setup: cold, moist enough, calm → fires.
    assert smart_condition("frost", tempf=33.0, dew_point=30.0, wind=2.0,
                           **BASE)
    # Windy cold snap: real cold, not frost.
    assert not smart_condition("frost", tempf=33.0, dew_point=30.0,
                               wind=15.0, **BASE)
    # Bone-dry desert air: nothing to deposit.
    assert not smart_condition("frost", tempf=33.0, dew_point=45.0, wind=2.0,
                               **BASE)
    # Absent dew/wind readings are NOT suppressors (absent is not zero):
    assert smart_condition("frost", tempf=33.0, **BASE)


def test_front_message_lists_parts():
    title, body = build_smart_message(
        "front", "Davis", front_parts=["pressure falling fast",
                                       "wind ramping up"])
    assert "Front passage" in title
    assert "pressure falling fast" in body and "wind ramping up" in body


def _monitor():
    from app.alerts import AlertMonitor
    return AlertMonitor()


def _cfg():
    from types import SimpleNamespace
    return SimpleNamespace(enabled=False, email_scope="device_down",
                           recipients=[], storm_summary=False)


def test_front_grouping_delivers_once(client, monkeypatch):
    """Two rate-family kinds newly firing on one tick for one station →
    exactly one delivered alert, kind 'front', both states marked."""
    import app.alerts as al
    from app import db
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append(kw.get("kind"))
        return True

    monkeypatch.setattr(al, "_deliver", fake_deliver)
    mac = "AA:BB:CC:00:00:60"
    now = int(time.time() * 1000)

    async def run():
        # History: mild temps and steady wind an hour ago, so the current
        # reading computes big deltas. Write directly via ingest-shaped
        # observation rows using db.value_at_or_before's source table.
        import app.main  # ensure db initialized by client fixture
        await db.insert_observations(mac, [{
            "dateutc": now - 3_700_000, "tempf": 92.0,
            "windspeedmph": 4.0, "baromrelin": 29.90}])
        dev = {"mac": mac, "name": "Fronty", "lastData": {
            "tempf": 76.0, "windspeedmph": 22.0, "windgustmph": 30.0,
            "baromrelin": 29.86, "dateutc": now}}
        mon = _monitor()
        await mon._check_smart_alerts(_cfg(), [dev], now)

    asyncio.run(run())
    assert delivered == ["front"], delivered

    async def states():
        return await db.get_smart_alert_states()
    st = asyncio.run(states())
    assert st.get((mac, "temp_drop")) == 1
    assert st.get((mac, "wind_ramp")) == 1


def test_first_frost_fires_once_per_season(client, monkeypatch):
    import app.alerts as al
    from app import db
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append(kw.get("kind"))
        return True

    monkeypatch.setattr(al, "_deliver", fake_deliver)
    now = int(time.time() * 1000)
    # Fresh dateutc required since the staleness gate hardened (R7): a
    # blob with no timestamp is unverifiable and must not fire.
    dev = {"mac": "AA:BB:CC:00:00:61", "name": "Frosty",
           "lastData": {"tempf": 31.0, "dateutc": now}}
    mon = _monitor()
    asyncio.run(mon._check_seasonal_events(_cfg(), [dev], now))
    asyncio.run(mon._check_seasonal_events(_cfg(), [dev], now + 60_000))
    assert delivered == ["first_frost"], "one-shot per season"
    # A warm reading never fires.
    dev2 = {"mac": "AA:BB:CC:00:00:62", "name": "Warm",
            "lastData": {"tempf": 55.0, "dateutc": now}}
    asyncio.run(mon._check_seasonal_events(_cfg(), [dev2], now))
    assert delivered == ["first_frost"]


def test_lightning_episode_lifecycle(client, monkeypatch):
    """First strike alerts; same-distance strikes stay quiet; a CLOSER
    strike re-alerts; 30 quiet minutes send exactly one all-clear."""
    from app import lightning_watch as lw
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append((kw.get("kind"), pt))
        return True

    now = int(time.time() * 1000)

    def dev(count, dist):
        return [{"mac": "AA:BB:CC:00:00:63", "name": "Zappy",
                 "lastData": {"lightningcount": count,
                              "lightning_distance_mi": dist}}]

    async def run():
        # Baseline sight: count exists but is not provably NEW lightning.
        await lw.check(_cfg(), dev(10, 18.0), now, fake_deliver)
        # New strikes → first alert.
        await lw.check(_cfg(), dev(12, 14.0), now + 60_000, fake_deliver)
        # More strikes at similar distance → silent (fatigue rule).
        await lw.check(_cfg(), dev(15, 13.5), now + 4 * 60_000, fake_deliver)
        # Distinctly closer + past the re-alert floor → "closing".
        await lw.check(_cfg(), dev(18, 6.0), now + 10 * 60_000, fake_deliver)
        # Quiet 29 minutes: no all-clear yet.
        await lw.check(_cfg(), dev(18, 6.0), now + 39 * 60_000, fake_deliver)
        # Quiet ≥30 minutes since last strike: one all-clear.
        await lw.check(_cfg(), dev(18, 6.0), now + 41 * 60_000, fake_deliver)
        await lw.check(_cfg(), dev(18, 6.0), now + 45 * 60_000, fake_deliver)

    asyncio.run(run())
    kinds = [k for k, _ in delivered]
    assert kinds == ["lightning", "lightning", "lightning_clear"], delivered
    assert "detected" in delivered[0][1]
    assert "closing" in delivered[1][1]


def test_lightning_counter_reset_rebaselines_silently(client):
    from app import lightning_watch as lw
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append(kw.get("kind"))
        return True

    now = int(time.time() * 1000)

    def dev(count):
        return [{"mac": "AA:BB:CC:00:00:64", "name": "Resetty",
                 "lastData": {"lightningcount": count}}]

    async def run():
        await lw.check(_cfg(), dev(500), now, fake_deliver)
        # Midnight rollover: counter falls — silence, new baseline.
        await lw.check(_cfg(), dev(3), now + 60_000, fake_deliver)
        # And a rise from the NEW baseline is a real strike.
        await lw.check(_cfg(), dev(4), now + 120_000, fake_deliver)

    asyncio.run(run())
    assert delivered == ["lightning"]


def test_quiet_hours_wraparound():
    from app.alerts import in_quiet_hours
    import datetime as _dt
    from zoneinfo import ZoneInfo
    tz = "America/Phoenix"

    def ms_at(hour, minute=0):
        d = _dt.datetime(2026, 8, 25, hour, minute, tzinfo=ZoneInfo(tz))
        return int(d.timestamp() * 1000)

    # 22:00 → 07:00 window wraps midnight.
    assert in_quiet_hours(ms_at(23), tz, 22 * 60, 7 * 60)
    assert in_quiet_hours(ms_at(3), tz, 22 * 60, 7 * 60)
    assert not in_quiet_hours(ms_at(12), tz, 22 * 60, 7 * 60)
    assert not in_quiet_hours(ms_at(7), tz, 22 * 60, 7 * 60)   # end excl.
    # Non-wrapping window.
    assert in_quiet_hours(ms_at(13), tz, 12 * 60, 14 * 60)
    # Off states.
    assert not in_quiet_hours(ms_at(3), tz, None, 7 * 60)
    assert not in_quiet_hours(ms_at(3), tz, 300, 300)


def test_severity_defaults_and_warnings():
    from app.alerts import severity_of
    assert severity_of("lightning") == "warning"
    assert severity_of("outflow") == "warning"
    assert severity_of("lightning_clear") == "info"
    assert severity_of("first_frost") == "info"
    assert severity_of("rule") == "watch"
    assert severity_of("brand_new_kind") == "watch"


def test_digest_sends_once_per_day(client, monkeypatch):
    import app.alerts as al
    from app import db
    from types import SimpleNamespace
    import datetime as _dt
    from zoneinfo import ZoneInfo

    sent = []
    monkeypatch.setattr(
        al, "_send_sync",
        lambda subject, body, to, cfg, html=None: sent.append(
            (subject, body, html)))
    cfg = SimpleNamespace(enabled=True, recipients=["v@z.com"],
                          digest_hour=7, smtp_host="h", smtp_port=465,
                          smtp_username=None, smtp_password=None,
                          smtp_from=None, smtp_tls=False, smtp_ssl=True,
                          email_scope="all")
    # The digest computes "today" in the SERVER's timezone — the test
    # must build its clock in the same zone or the hour gate reads wrong.
    from app.config import settings as _settings
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc

    def ms_at(hour):
        return int(_dt.datetime(2026, 8, 25, hour, 5,
                                tzinfo=tz).timestamp() * 1000)

    async def run():
        await db.log_alert(ms_at(3), "rule", "AA", "Wind Gust alert",
                           "18 mph", 1)
        await db.log_alert(ms_at(4), "lightning_clear", "AA",
                           "Lightning all clear", "", 1)
        mon = al.AlertMonitor()
        # Before the digest hour: nothing.
        await mon._maybe_send_digest(cfg, [], ms_at(6))
        assert sent == []
        # At/after the hour: one report with both alert lines.
        await mon._maybe_send_digest(cfg, [], ms_at(8))
        # Same day again: no repeat.
        await mon._maybe_send_digest(cfg, [], ms_at(11))

    asyncio.run(run())
    assert len(sent) == 1
    subject, body, html = sent[0]
    assert "2 alerts" in subject
    assert "Wind Gust alert" in body and "Lightning all clear" in body
    # 1.9: the weather-report HTML rides as the alternative.
    assert html is not None and "Wind Gust alert" in html
    assert "zasder" in html


def test_nws_relay_pushes_severe_once(client, monkeypatch):
    from app import nws_watch as nw
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append((kw.get("kind"), pt))
        return True

    batches = [
        [{"id": "urn:a1", "severity": "Severe",
          "event": "Dust Storm Warning", "headline": "Wall of dust"}],
        # Second poll: same alert (no repeat) + a Minor one (no push).
        [{"id": "urn:a1", "severity": "Severe",
          "event": "Dust Storm Warning", "headline": "Wall of dust"},
         {"id": "urn:a2", "severity": "Minor",
          "event": "Heat Advisory", "headline": "Hot"}],
    ]

    async def fake_fetch(lat, lon):
        return batches.pop(0) if batches else []

    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    dev = [{"mac": "AA:BB:CC:00:00:65", "name": "Stormfront",
            "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]
    now = int(time.time() * 1000)

    async def run():
        await nw.check(_cfg(), dev, now, fake_deliver)
        await nw.check(_cfg(), dev, now + 11 * 60_000, fake_deliver)
        # Inside the cadence window: no fetch at all.
        await nw.check(_cfg(), dev, now + 12 * 60_000, fake_deliver)

    asyncio.run(run())
    assert [k for k, _ in delivered] == ["nws"]
    assert "Dust Storm Warning" in delivered[0][1]
    assert batches == []          # exactly two fetches happened


def test_battery_low_conventions_only():
    from app.health_watch import battery_low_fields
    assert battery_low_fields({"battout": 0}) == ["battout"]
    assert battery_low_fields({"battout": 1}) == []
    assert battery_low_fields({"battery_outdoor": "low"}) == ["battery_outdoor"]
    assert battery_low_fields({"battery_outdoor": "ok"}) == []
    # Unknown voltage-style values are silence, not a claim.
    assert battery_low_fields({"mystery_batt_v": 2.1}) == []
    assert battery_low_fields({}) == []


def test_sensor_quiet_and_recovery(client, monkeypatch):
    from app import health_watch as hw, db
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append((kw.get("kind"), pt))
        return True

    now = int(time.time() * 1000)
    mac = "AA:BB:CC:00:00:66"

    def dev(solar, ts):
        d = {"tempf": 95.0, "dateutc": ts}
        if solar is not None:
            d["solarradiation"] = solar
        return [{"mac": mac, "name": "Sunny", "lastData": d}]

    async def run():
        # Solar alive: last-seen stamps.
        await hw.check(_cfg(), dev(800.0, now), now, fake_deliver)
        # Solar null but only 1h quiet: nothing.
        await hw.check(_cfg(), dev(None, now + 3_600_000),
                       now + 3_600_000, fake_deliver)
        # 4h quiet while the device itself is fresh: sensor_quiet fires.
        t4 = now + 4 * 3_600_000
        await hw.check(_cfg(), dev(None, t4), t4, fake_deliver)
        # It returns: one recovery, state re-armed.
        t5 = now + 5 * 3_600_000
        await hw.check(_cfg(), dev(750.0, t5), t5, fake_deliver)

    asyncio.run(run())
    kinds = [k for k, _ in delivered]
    assert kinds == ["sensor_quiet", "sensor_recovered"], delivered
    assert "solarradiation" in delivered[0][1]


def test_sensor_never_seen_is_not_broken(client):
    """A station that never had a solar head must never be told its
    solar sensor died — absent is not broken."""
    from app import health_watch as hw
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append(kw.get("kind"))
        return True

    now = int(time.time() * 1000)
    dev = [{"mac": "AA:BB:CC:00:00:67", "name": "NoSolar",
            "lastData": {"tempf": 70.0, "dateutc": now}}]
    asyncio.run(hw.check(_cfg(), dev, now, fake_deliver))
    asyncio.run(hw.check(_cfg(), dev, now + 5 * 3_600_000, fake_deliver))
    assert delivered == []


def test_flatline_wind_and_humidity(client, monkeypatch):
    from app import health_watch as hw, db
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append((kw.get("kind"), pt))
        return True

    mac = "AA:BB:CC:00:00:68"
    now = int(time.time() * 1000)

    async def run():
        # 24h of observations: humidity pegged at 100, zero gusts.
        rows = [{"dateutc": now - h * 3_600_000, "tempf": 60.0,
                 "humidity": 100.0, "windgustmph": 0.0}
                for h in range(0, 25, 2)]
        await db.insert_observations(mac, rows)
        dev = [{"mac": mac, "name": "Stucky",
                "lastData": {"tempf": 60.0, "humidity": 100.0,
                             "windgustmph": 0.0, "dateutc": now}}]
        await hw.check(_cfg(), dev, now, fake_deliver)

    asyncio.run(run())
    kinds = [k for k, _ in delivered]
    assert kinds.count("flatline") == 2
    titles = " | ".join(t for _, t in delivered)
    assert "Humidity" in titles and "Anemometer" in titles


# ───────────────────────── per-rule severity (1.8) ─────────────────────────

def test_rule_severity_api_default_and_validation(client):
    H = {"Authorization": "Bearer test-api-token"}
    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "tempf", "comparator": "above",
                          "threshold": 100})
    assert r.status_code == 200
    rid = r.json()["id"]
    rows = client.get("/api/alerts/rules", headers=H).json()
    assert next(x for x in rows if x["id"] == rid)["severity"] == "minor"

    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "windspeedmph", "comparator": "above",
                          "threshold": 40, "severity": "urgent"})
    assert r.status_code == 200
    rid2 = r.json()["id"]
    rows = client.get("/api/alerts/rules", headers=H).json()
    assert next(x for x in rows if x["id"] == rid2)["severity"] == "urgent"

    assert client.post("/api/alerts/rules", headers=H,
                       json={"field": "tempf", "comparator": "above",
                             "threshold": 1,
                             "severity": "loud"}).status_code == 400

    r = client.patch(f"/api/alerts/rules/{rid}", headers=H,
                     json={"severity": "standard"})
    assert r.status_code == 200
    # The PATCH RESPONSE itself must carry severity — the app replaces its
    # cached rule with this body, and a missing field silently demoted an
    # urgent rule to minor on the next edit (CodeRabbit, PR #32).
    assert r.json()["severity"] == "standard"
    r = client.patch(f"/api/alerts/rules/{rid}", headers=H,
                     json={"enabled": False})
    assert r.json()["severity"] == "standard"
    rows = client.get("/api/alerts/rules", headers=H).json()
    assert next(x for x in rows if x["id"] == rid)["severity"] == "standard"
    assert client.patch(f"/api/alerts/rules/{rid}", headers=H,
                        json={"severity": "nope"}).status_code == 400


def test_rule_tier_mapping():
    from app.alerts import rule_tier
    assert rule_tier("minor") == "info"
    assert rule_tier("standard") == "watch"
    assert rule_tier("urgent") == "warning"
    # NULL rows (pre-1.8 rules) and junk both land on the quiet default.
    assert rule_tier(None) == "info"
    assert rule_tier("bogus") == "info"


def test_urgent_rule_breaks_quiet_hours_minor_does_not(client, monkeypatch):
    """The whole point of the feature: an urgent rule pushes at 3am, a
    minor one holds until morning (but still lands in history with its
    severity)."""
    import asyncio
    import app.alerts as al
    from app import db
    from app.alerts import effective_config
    from app import apns

    monkeypatch.setattr(al, "in_quiet_hours", lambda *a, **kw: True)
    pushes = []
    async def fake_push_configured():
        return True
    async def fake_send_to_all(title, body, interruption_level=None):
        pushes.append(title)
        return {"sent": 1}
    monkeypatch.setattr(apns, "push_configured", fake_push_configured)
    monkeypatch.setattr(apns, "send_to_all", fake_send_to_all)

    async def run():
        cfg = await effective_config()
        ok1 = await al._deliver(cfg, "s", "b", "Urgent wind", "b",
                                email_ok=False, kind="rule", mac="AA",
                                severity="warning")
        ok2 = await al._deliver(cfg, "s", "b", "Minor heat", "b",
                                email_ok=False, kind="rule", mac="AA",
                                severity="info")
        return ok1, ok2, await db.recent_alerts(10)
    ok1, ok2, hist = asyncio.run(run())
    assert ok1 and ok2
    assert pushes == ["Urgent wind"]      # minor muted overnight
    by_title = {h["title"]: h for h in hist}
    assert by_title["Urgent wind"]["severity"] == "warning"
    assert by_title["Minor heat"]["severity"] == "info"
    assert by_title["Minor heat"]["delivered"] in (0, False)


def test_threshold_checker_passes_rule_severity(client, monkeypatch):
    import asyncio
    import app.alerts as alerts
    from app.alerts import AlertMonitor, effective_config
    from app import db
    H = {"Authorization": "Bearer test-api-token"}
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": "AA:BB:CC:DD:EE:FF", "name": "Yard"},
              "timestamp_utc": "2026-06-01T12:00:00Z",
              "outdoor": {"tempf": 105}})
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above",
                      "threshold": 100, "severity": "urgent"})
    seen = []
    async def fake_deliver(*a, **kw):
        seen.append(kw.get("severity"))
        return True
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)

    async def tick():
        cfg = await effective_config()
        await AlertMonitor()._check_threshold_rules(
            cfg, await db.list_devices(), 1_000)
    asyncio.run(tick())
    assert seen == ["warning"]


def test_webhook_payload_carries_severity(client, monkeypatch):
    import asyncio
    import json as _json
    import app.alerts as al
    from app import webhooks
    from app.alerts import effective_config

    sent = []
    async def fake_send(hook, payload):
        sent.append(_json.loads(payload))
    monkeypatch.setattr(webhooks, "_send", fake_send)

    async def run():
        from app import db
        await db.create_webhook("https://example.com/hook")
        cfg = await effective_config()
        await al._deliver(cfg, "s", "b", "Hooked", "b", email_ok=False,
                          kind="rule", mac="AA", severity="warning")
        # Dispatch is fire-and-forget off the tick now (R7) — settle the
        # in-flight task before asserting.
        if al._WEBHOOK_TASKS:
            await asyncio.gather(*al._WEBHOOK_TASKS)
    asyncio.run(run())
    assert sent and sent[0]["severity"] == "warning"


# ─────────────── CodeRabbit PR #32 batch: regression coverage ───────────────

def test_flatline_needs_window_coverage(client, monkeypatch):
    """A station online for minutes in fog must NOT be called stuck — the
    pegged value has to span the window (CodeRabbit, PR #32)."""
    from app import health_watch as hw, db
    delivered = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append(kw.get("kind"))
        return True

    mac = "AA:BB:CC:00:00:69"
    now = int(time.time() * 1000)

    async def run():
        rows = [{"dateutc": now - m * 60_000, "tempf": 60.0,
                 "humidity": 100.0, "windgustmph": 0.0}
                for m in range(0, 10, 2)]      # ten minutes of history
        await db.insert_observations(mac, rows)
        dev = [{"mac": mac, "name": "Newbie",
                "lastData": {"tempf": 60.0, "humidity": 100.0,
                             "windgustmph": 0.0, "dateutc": now}}]
        await hw.check(_cfg(), dev, now, fake_deliver)

    asyncio.run(run())
    assert "flatline" not in delivered


def test_first_frost_ignores_stale_reading(client, monkeypatch):
    """A station that died on a cold night keeps a freezing lastData —
    firing off it days later would also burn the season key."""
    import app.alerts as al
    from app import db
    sent = []
    monkeypatch.setattr(al, "_send_sync",
                        lambda *a, **kw: sent.append(a))
    now = int(time.time() * 1000)
    stale = now - 2 * 86_400_000

    async def run():
        cfg = await al.effective_config()
        mon = al.AlertMonitor()
        devs = [{"mac": "AA:BB:CC:00:00:70", "name": "Dead",
                 "lastData": {"tempf": 28.0, "dateutc": stale}}]
        await mon._check_seasonal_events(cfg, devs, now)
        keys = [k for k in []]
        return await db.get_kv(
            f"seasonal.first_frost.AA:BB:CC:00:00:70.{2026}")

    burned = asyncio.run(run())
    assert not sent
    assert burned is None, "stale reading must not burn the season key"


def test_nws_failed_push_retries(client, monkeypatch):
    """Persist-after-deliver: a failed severe-alert push leaves the id
    unseen, so the next tick retries."""
    import asyncio
    from app import nws_watch as nw

    calls = {"n": 0}

    async def deliver(cfg, subject, body, pt, pb, **kw):
        calls["n"] += 1
        return calls["n"] > 1          # first attempt fails, second lands

    alerts = [{"id": "urn:x:1", "severity": "Severe",
               "event": "Severe Thunderstorm Warning",
               "headline": "Take cover"}]

    async def fake_fetch(lat, lon):
        return alerts
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    monkeypatch.setattr(nw, "_last_poll_ms", {})

    mac = "AA:BB:CC:00:00:71"
    dev = [{"mac": mac, "name": "Sky",
            "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]

    async def run():
        from app.alerts import effective_config
        cfg = await effective_config()
        now = int(time.time() * 1000)
        for tick in range(3):
            nw._last_poll_ms.clear()          # defeat the 10-min cadence
            await nw.check(cfg, dev, now + tick * 1000, deliver)
    asyncio.run(run())
    assert calls["n"] == 2, "failed push retries once, delivered push stops"


def test_widget_push_zero_sends_retries(client, monkeypatch):
    """A refresh nudge that reaches zero tokens must not consume the
    reading — the next tick retries once tokens exist."""
    import app.widget_push as wp
    from app import apns

    results = [{"sent": 0}, {"sent": 1}]
    sends = {"n": 0}

    async def fake_refresh():
        sends["n"] += 1
        return results[min(sends["n"] - 1, 1)]
    monkeypatch.setattr(apns, "send_widget_refresh", fake_refresh)
    monkeypatch.setattr(wp, "_last_push_ms", 0)
    monkeypatch.setattr(wp, "_last_data_ms", 0)

    ts = int(time.time() * 1000)
    devs = [{"mac": "AA", "lastData": {"dateutc": ts}}]

    async def run():
        await wp.check(devs, ts)
        await wp.check(devs, ts + wp._MIN_GAP_MS + 1)
        await wp.check(devs, ts + 2 * (wp._MIN_GAP_MS + 1))
    asyncio.run(run())
    assert sends["n"] == 2, "zero-send retried once, delivered send consumed it"


def test_export_filename_sanitized(client):
    """Junk in the mac path segment must not reach Content-Disposition
    (header injection). The route still 200s — data selection is by the
    normalized id, filename is just cosmetic."""
    H = {"Authorization": "Bearer test-api-token"}
    evil = 'x%22%0d%0aSet-Cookie:pwn'
    r = client.get(f"/api/devices/{evil}/export.csv", headers=H)
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert '\r' not in cd and '\n' not in cd
    assert cd.count('"') == 2            # only the framing quotes


def test_share_error_redacts_urls():
    from app.share_targets import _safe_err
    e = Exception("HTTP 500 for https://stations.windy.com/pws/update/SECRETKEY123")
    msg = _safe_err(e)
    assert "SECRETKEY123" not in msg
    assert "<url>" in msg and msg.startswith("Exception")


def test_manual_storm_start_respects_optout(client):
    H = {"Authorization": "Bearer test-api-token"}
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": "AA:BB:CC:00:00:72", "name": "Quiet"},
              "timestamp_utc": "2026-06-01T12:00:00Z",
              "outdoor": {"tempf": 90}})
    mac = "AA:BB:CC:00:00:72"
    r = client.put(f"/api/devices/{mac}/alert", headers=H,
                   json={"monitor": True, "storm_summary": False})
    assert r.status_code == 200
    r = client.post("/api/storm/watch/start", headers=H, json={"mac": mac})
    assert r.status_code == 409
    # flip it on: start goes through
    client.put(f"/api/devices/{mac}/alert", headers=H,
               json={"monitor": True, "storm_summary": True})
    r = client.post("/api/storm/watch/start", headers=H, json={"mac": mac})
    assert r.status_code == 200


def test_rule_severity_ladder_includes_major():
    """1.9: the middle ground. major maps to its OWN tier — quiet-hours
    exempt but not Time Sensitive."""
    from app.alerts import RULE_SEVERITIES, rule_tier
    assert RULE_SEVERITIES == ("minor", "standard", "major", "urgent")
    assert rule_tier("major") == "major"
    assert rule_tier("urgent") == "warning"
    assert rule_tier("standard") == "watch"
    assert rule_tier(None) == "info"


def test_quiet_hours_tier_ladder(client, monkeypatch):
    """1.9 delivery matrix during quiet hours: warning goes through AND
    rides Time Sensitive; major goes through as a NORMAL push; watch and
    info hold their tongue."""
    import asyncio
    from types import SimpleNamespace
    import app.alerts as al

    monkeypatch.setattr(al, "in_quiet_hours", lambda *a: True)
    calls: list = []

    async def fake_send(title, body, interruption_level=None):
        calls.append(interruption_level)
        return {"sent": 1}

    async def configured():
        return True
    monkeypatch.setattr(al.apns, "send_to_all", fake_send)
    monkeypatch.setattr(al.apns, "push_configured", configured)
    cfg = SimpleNamespace(enabled=False, recipients=[],
                          quiet_start_min=22 * 60, quiet_end_min=7 * 60)

    def run(sev):
        calls.clear()
        asyncio.run(al._deliver(cfg, "s", "b", "t", "pb",
                                email_ok=False, kind="rule", severity=sev))
        return list(calls)

    assert run("warning") == ["time-sensitive"]
    assert run("major") == [None]
    assert run("watch") == []
    assert run("info") == []


def test_outside_quiet_hours_only_warning_is_time_sensitive(client, monkeypatch):
    """By day everything pushes, but ONLY the warning tier is stamped
    Time Sensitive — major/standard/minor stay normal notifications."""
    import asyncio
    from types import SimpleNamespace
    import app.alerts as al

    monkeypatch.setattr(al, "in_quiet_hours", lambda *a: False)
    calls: list = []

    async def fake_send(title, body, interruption_level=None):
        calls.append(interruption_level)
        return {"sent": 1}

    async def configured():
        return True
    monkeypatch.setattr(al.apns, "send_to_all", fake_send)
    monkeypatch.setattr(al.apns, "push_configured", configured)
    cfg = SimpleNamespace(enabled=False, recipients=[],
                          quiet_start_min=None, quiet_end_min=None)

    def run(sev):
        calls.clear()
        asyncio.run(al._deliver(cfg, "s", "b", "t", "pb",
                                email_ok=False, kind="rule", severity=sev))
        return list(calls)

    assert run("warning") == ["time-sensitive"]
    assert run("major") == [None]
    assert run("watch") == [None]
    assert run("info") == [None]    # by day, even info delivers (CodeRabbit)
