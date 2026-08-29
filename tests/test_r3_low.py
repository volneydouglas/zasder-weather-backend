"""Round-3 Low-severity findings — regression tests (R3-36…R3-92 batch).

Each test names the finding it pins and fails against the pre-fix code.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone

import pytest

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:DD:EE:FF"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_obs(client, extra_rain=None, ts=None):
    body = {"device": {"id": "AABBCCDDEEFF", "name": "Yard"},
            "timestamp_utc": ts or _now_iso(),
            "outdoor": {"tempf": 75.0, "humidity": 40},
            "wind": {}, "rain": extra_rain or {},
            "pressure": {"relative_inhg": 29.9}, "source": "test"}
    return client.post("/ingest/custom", headers=IH, json=body)


# ───────────── R3-36: POST /api/alerts/test is throttled ─────────────

def test_alert_test_email_is_throttled(client, monkeypatch):
    """A write-token holder must not be able to pump unlimited email through
    the operator's SMTP account — one attempt per minute, 429 on repeats."""
    from app import alerts as A
    sent = []
    monkeypatch.setattr(A, "_send_sync", lambda *a, **k: sent.append(1))
    cfg = A.EffectiveAlertConfig(
        True, True, ["op@example.com"], 15.0, 0.0,
        "smtp.example.com", 465, None, None, None, False, True, "all")
    async def fake_cfg():
        return cfg
    monkeypatch.setattr(A, "effective_config", fake_cfg)
    assert client.post("/api/alerts/test", headers=H).status_code == 200
    assert len(sent) == 1
    r = client.post("/api/alerts/test", headers=H)
    assert r.status_code == 429, "second test email within a minute must 429"
    assert len(sent) == 1, "the throttled attempt must not reach SMTP"


# ───────────── R3-46 / R3-129: throttle-lock eviction on cancelled acquire ─────────────

def test_throttle_lock_evicts_entry_when_acquire_cancelled(client):
    """A task cancelled while BLOCKED on the per-MAC lock never runs __aexit__
    (async with skips it when __aenter__ raises), so the holder count leaked
    and the registry entry for that MAC was never evicted."""
    from app import ingest

    async def run():
        loop = asyncio.get_running_loop()
        mac = "CC:CC:CC:CC:CC:CC"
        release = asyncio.Event()

        async def holder():
            async with ingest._throttle_lock(mac):
                await release.wait()

        h = asyncio.create_task(holder())
        await asyncio.sleep(0)                 # holder acquires
        blocked_entered = asyncio.Event()

        async def blocked():
            blocked_entered.set()
            async with ingest._throttle_lock(mac):
                pass

        b = asyncio.create_task(blocked())
        await blocked_entered.wait()
        await asyncio.sleep(0)                 # b increments holders, blocks
        b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await b
        release.set()
        await h
        per_loop = ingest._THROTTLE_LOCKS.get(loop, {})
        assert per_loop == {}, f"cancelled waiter leaked registry entry: {per_loop}"

    asyncio.run(run())


# ───────────── R3-47: bucketed wind direction keeps fractional degrees ─────────────

def test_bucketed_winddir_keeps_fractional_degrees(client):
    """SQLite's % casts to INTEGER (224.7 % 360.0 → 224.0) — the circular
    mean was truncated to whole degrees on every bucketed window."""
    from app import db
    now_ms = int(time.time() * 1000)
    rows = [{"dateutc": now_ms - i * 3_600_000, "winddir": 224.7,
             "windspeedmph": 5.0} for i in range(1, 4)]
    asyncio.run(db.insert_observations(MAC, rows))
    # 8h window → bucketed path (raw path is ≤ 6h).
    out = asyncio.run(db.history(MAC, now_ms - 8 * 3_600_000, now_ms, limit=100))
    winds = [r["winddir"] for r in out if r.get("winddir") is not None]
    assert winds, "expected bucketed rows with winddir"
    for w in winds:
        assert abs(w - 224.7) < 0.01, f"winddir truncated: {w}"


# ───────────── R3-48 / R3-130: TEXT-in-REAL yearlyrainin read paths ─────────────

def _inject_text_yearly(temp_env: str, ts_ms: int, value: str = "abc") -> None:
    con = sqlite3.connect(temp_env)
    con.execute(
        "INSERT INTO observations (mac, dateutc_ms, yearlyrainin, data_json) "
        "VALUES (?, ?, ?, ?)",
        (MAC, ts_ms, value, '{"yearlyrainin": "abc"}'))
    con.commit()
    con.close()


def test_last_yearly_rain_tolerates_text_column_value(client, temp_env):
    """One TEXT row in the REAL column (the poller path stores upstream JSON
    verbatim) made last_yearly_rain raise ValueError inside _do_ingest —
    every subsequent /ingest/custom for that MAC 500'd."""
    from app import db
    assert _post_obs(client, extra_rain={"yearly_in": 1.0}).status_code == 200
    _inject_text_yearly(temp_env, int(time.time() * 1000) + 1000)
    assert asyncio.run(db.last_yearly_rain(MAC)) is None
    rollups = asyncio.run(db.rain_rollups(MAC))
    assert rollups == {"hourly_in": None, "daily_in": None,
                       "weekly_in": None, "monthly_in": None}
    # THE incident: the next ingest for this MAC must be a 200, not a 500.
    r = _post_obs(client, extra_rain={"yearly_in": 1.1})
    assert r.status_code == 200, r.text


# ───────────── R3-49 / R3-50 / R3-131: "nothing to deliver" ≠ failure ─────────────

def _cfg(scope="all", enabled=True):
    from app.alerts import EffectiveAlertConfig
    return EffectiveAlertConfig(
        enabled, enabled, ["x@example.com"] if enabled else [], 15.0, 0.0,
        "smtp.example.com" if enabled else None, 587, None, None,
        "from@example.com", True, False, scope)


def test_deliver_muted_by_scope_with_no_push_is_handled(client, monkeypatch):
    """email_scope='device_down' + push unconfigured: a threshold alert has
    NO willing channel. Pre-fix _deliver returned False → the caller
    retry-warned every tick forever and never persisted state."""
    from app import alerts as A
    from app import apns
    sent = []
    monkeypatch.setattr(A, "_send_sync", lambda *a, **k: sent.append(a))
    async def push_off():
        return False
    monkeypatch.setattr(apns, "push_configured", push_off)
    ok = asyncio.run(A._deliver(_cfg("device_down"), "s", "b", "t", "p",
                                email_ok=False))
    assert ok is True, "muted-by-scope must be handled, not a transient failure"
    assert sent == []


def test_deliver_zero_push_recipients_is_noop_success(client, monkeypatch):
    """Push configured but no registered token matches a live channel:
    send_to_all reports sent=0 with nothing failed — that is 'nothing to
    deliver', not a delivery failure to retry forever."""
    from app import alerts as A
    from app import apns
    async def push_on():
        return True
    async def no_recipients(title, body, interruption_level=None):
        return {"sent": 0, "skipped": "no push channel for the registered tokens"}
    monkeypatch.setattr(apns, "push_configured", push_on)
    monkeypatch.setattr(apns, "send_to_all", no_recipients)
    ok = asyncio.run(A._deliver(_cfg(enabled=False), "s", "b", "t", "p",
                                email_ok=False))
    assert ok is True


def test_deliver_real_push_failure_still_retries(client, monkeypatch):
    """An ATTEMPTED send that failed (failed > 0, sent == 0) must still count
    as undelivered so the caller retries next tick."""
    from app import alerts as A
    from app import apns
    async def push_on():
        return True
    async def all_failed(title, body):
        return {"sent": 0, "pruned": 0, "failed": 2, "total": 2}
    monkeypatch.setattr(apns, "push_configured", push_on)
    monkeypatch.setattr(apns, "send_to_all", all_failed)
    ok = asyncio.run(A._deliver(_cfg(enabled=False), "s", "b", "t", "p",
                                email_ok=False))
    assert ok is False


def test_deliver_email_failure_still_retries(client, monkeypatch):
    from app import alerts as A
    from app import apns
    def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(A, "_send_sync", boom)
    async def push_off():
        return False
    monkeypatch.setattr(apns, "push_configured", push_off)
    ok = asyncio.run(A._deliver(_cfg(), "s", "b", "t", "p", email_ok=True))
    assert ok is False


# ───────────── R3-52 / R3-133: never-alive devices don't "repeat" ─────────────

def test_baseline_stale_device_never_repeats():
    """The baseline promise: never alert for a device already dead when
    monitoring started. The repeat branch broke it via the `or changed_ms`
    fallback — 'still not reporting' fired for a device never seen alive."""
    from app import alerts
    TH = 600_000
    RPT = 3_600_000
    now = 10_000_000_000
    d0 = alerts.decide(None, now - 5 * TH, now, TH, RPT)
    assert d0.state == "stale" and d0.event is None      # baselined, no alert
    prior = {"state": "stale", "changed_ms": d0.changed_ms, "notified_ms": None}
    d1 = alerts.decide(prior, now - 5 * TH, now + 3 * RPT, TH, RPT)
    assert d1.event is None, "never-notified baseline device must not 'repeat'"
    # A device whose initial stale alert DID deliver still repeats.
    prior2 = {"state": "stale", "changed_ms": d0.changed_ms, "notified_ms": now}
    d2 = alerts.decide(prior2, now - 5 * TH, now + 3 * RPT, TH, RPT)
    assert d2.event == "repeat"


# ───────────── R3-53: duplicate Prometheus label sets don't kill the scrape ─────────────

def test_metrics_dedupes_colliding_label_sets():
    """Masked MACs keep only the last two bytes and names are free-form, so
    two devices CAN produce identical label sets — a duplicate sample fails
    the ENTIRE Prometheus scrape."""
    from app import metrics
    devs = [
        {"mac": "AA:BB:CC:DD:EE:FF", "name": "Same",
         "lastData": {"tempf": 70.0}, "lastSeen": 1000},
        {"mac": "11:22:33:DD:EE:FF", "name": "Same",   # same last 2 bytes
         "lastData": {"tempf": 80.0}, "lastSeen": 1000},
    ]
    out = metrics.render_prometheus(devs, now_ms=2000)
    samples = [line.split(" ")[0] for line in out.splitlines()
               if line and not line.startswith("#")]
    assert len(samples) == len(set(samples)), \
        f"duplicate label sets in scrape output:\n{out}"


# ───────────── R3-58: negative wind speed isn't the strongest petal ─────────────

def test_wind_rose_negative_speed_not_binned_as_strongest():
    from app import public_dashboard as pd
    samples = [(0.0, 1.0), (10.0, 2.0), (20.0, 3.0), (90.0, -5.0)]
    svg = pd.svg_wind_rose(samples)
    assert f'fill="{pd._ROSE_SPEED_COLORS[-1]}"' not in svg, \
        "a negative speed sample was plotted in the 20+ mph bin"


# ───────────── R3-86: read-side MAC normalization ─────────────

def test_format_mac_normalizes_colonized_lowercase():
    from app.ingest import _format_mac
    assert _format_mac("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"
    assert _format_mac("AABBCCDDEEFF") == "AA:BB:CC:DD:EE:FF"
    assert _format_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"
    assert _format_mac("not-a-mac") == "not-a-mac"     # passthrough unchanged
    assert _format_mac("") == ""


def test_read_endpoints_accept_lowercase_colonized_mac(client):
    """Write endpoints normalize the path MAC; reads passed it raw, so
    `aa:bb:...` 404'd while `AA:BB:...` worked — a script-user footgun."""
    assert _post_obs(client).status_code == 200
    low = "aa:bb:cc:dd:ee:ff"
    r = client.get(f"/api/devices/{low}/current", headers=H)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/devices/{low}/records", headers=H)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/devices/{low}/history?hours=24", headers=H)
    assert r.status_code == 200 and r.json()["count"] >= 1
    r = client.get(f"/api/devices/{low}/summary?field=tempf", headers=H)
    assert r.status_code == 200 and r.json().get("avg") is not None


# ───────────── R3-87: list_discoveries scrubs non-finite literals ─────────────

def test_discovery_sample_scrubs_nonfinite_literals(client, temp_env):
    """upsert_discovery stores with json.dumps(allow_nan=True): a stored
    `Infinity` literal round-tripped to inf via the unguarded json.loads —
    surviving only by Pydantic's serializer. Scrub on the way out."""
    from app import db
    r = client.post("/ingest/discovery", headers=IH,
                    json={"model": "Acurite-Atlas", "id": 711})
    assert r.status_code == 200, r.text
    con = sqlite3.connect(temp_env)
    con.execute("UPDATE discoveries SET sample_json = ?",
                ('{"rain_mm": Infinity, "temp_C": 21.5}',))
    con.commit()
    con.close()
    rows = asyncio.run(db.list_discoveries())
    assert rows and rows[0]["sample"] == {"rain_mm": None, "temp_C": 21.5}
    assert client.get("/api/discoveries?since_hours=0",
                      headers=H).status_code == 200


# ───────────── R3-90: a crashed rebuild doesn't leave a truncated ledger ─────────────

def test_failed_rebuild_clears_partial_rollups(client, monkeypatch):
    """A rebuild commits per batch, so a mid-scan crash left plausible-looking
    partial rollups with the endpoint's 'run rebuild' hint never firing
    (it needs day_count == 0). The failure path must clear the tables."""
    from app import insights
    assert _post_obs(client).status_code == 200
    asyncio.run(insights.rebuild(MAC))
    assert asyncio.run(insights.assemble(MAC))["day_count"] >= 1

    async def partial_then_crash(dbmod, mac):
        # Simulate "some batches committed, then the process died".
        async with dbmod.connect() as d:
            await d.execute(
                "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_n) "
                "VALUES (?, '2020-01-01', 1)", (mac,))
            await d.commit()
        raise RuntimeError("interrupted mid-scan")

    monkeypatch.setattr(insights, "_rebuild_scan", partial_then_crash)
    insights._REBUILD_LOCK = None          # new event loop per asyncio.run
    with pytest.raises(RuntimeError):
        asyncio.run(insights.rebuild(MAC))
    insights._REBUILD_LOCK = None
    assert asyncio.run(insights.assemble(MAC))["day_count"] == 0, \
        "partial rollups survived a failed rebuild (silently truncated ledger)"


# ───────────── R3-91: retained MQTT topics retracted on device delete ─────────────

def test_mqtt_retraction_topics_cover_everything_published():
    from app import mqtt_publish as mp
    topics = mp.retraction_topics("aabbccddeeff", "zasder", "homeassistant")
    assert len(topics) == len(mp._SENSORS) + 2
    assert "zasder/aabbccddeeff/status" in topics
    assert "zasder/aabbccddeeff/state" in topics
    assert "homeassistant/sensor/zasder_aabbccddeeff/tempf/config" in topics
    # Every discovery config topic published for a device must be retracted.
    cfg_topics = {t for t, _ in mp.discovery_messages(
        {"mac": "AA:BB:CC:DD:EE:FF", "name": "X"}, "zasder", "homeassistant")}
    assert cfg_topics <= set(topics)


def test_mqtt_publish_retracts_deleted_devices(client, monkeypatch):
    from app import mqtt_publish as mp
    pub = mp.MqttPublisher()
    pub._announced = {"aabbccddeeff"}
    published: list[tuple] = []

    class FakeClient:
        def publish(self, topic, payload=None, retain=False):
            published.append((topic, payload, retain))

    async def no_devices():
        return []
    monkeypatch.setattr(mp.db, "list_devices", no_devices)
    asyncio.run(pub._publish(FakeClient()))
    assert {t for t, _, _ in published} == set(
        mp.retraction_topics("aabbccddeeff"))
    assert all(payload == "" and retain for _, payload, retain in published), \
        "retraction must be EMPTY retained publishes (deletes on the broker)"
    assert pub._announced == set()


# ───────────── R3-143: the busy_timeout lock fix has a floor test ─────────────

def test_connect_sets_busy_timeout(client):
    """The 1.4 'database is locked' fix — writers wait up to 10s instead of
    failing instantly during an insights rebuild. A refactor dropping the
    PRAGMA reintroduces production 500s and passed the suite."""
    from app import db

    async def check():
        async with db.connect() as conn:
            row = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
            return row[0]

    assert asyncio.run(check()) == 10000
