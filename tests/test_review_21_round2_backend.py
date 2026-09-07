"""2.1 pre-release review, round two: backend rain, ingest and pollers
(§4.2 BE-N1..N8 and Doren's Govee flap of 2026-09-06).

Each test names the finding it pins and failed against the code before
the fix.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}


# ───────── BE-N1: the morning report reads the one rain rule ─────────

def test_the_morning_report_quotes_a_yearly_counter_stations_rain(client, monkeypatch):
    """alerts.build_morning_report read rain_total raw, so a LilyGO station's
    one-inch day produced a morning report that never mentioned rain; the
    digest tile and the Live Activity rode the same value."""
    from zoneinfo import ZoneInfo
    import app.alerts as al
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    tz = ZoneInfo("UTC")
    now = dt.datetime.now(tz).replace(hour=8, minute=0, second=0, microsecond=0)
    yday = (now.date() - dt.timedelta(days=1)).isoformat()
    now_ms = int(now.timestamp() * 1000)
    counter, gaugeless = "AA:BB:CC:00:00:D1", "AA:BB:CC:00:00:D2"

    async def run():
        for mac in (counter, gaugeless):
            await db.upsert_device(mac, {"lastData": {"dateutc": now_ms, "tempf": 90.0}})
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO daily_rollups (mac, day, tempf_min, tempf_max, tempf_sum, "
                "tempf_n, rain_total, yearly_min, yearly_max) "
                "VALUES (?, ?, 60, 80, 140, 2, NULL, 16.76, 17.76)", (counter, yday))
            await conn.execute(
                "INSERT INTO daily_rollups (mac, day, tempf_min, tempf_max, tempf_sum, "
                "tempf_n) VALUES (?, ?, 60, 80, 140, 2)", (gaugeless, yday))
            await conn.commit()
        devices = await db.list_devices()
        return await al.build_morning_report(devices, now_ms, now_ms - 86_400_000, tz, now)

    _report, stations, _alerts = asyncio.run(run())
    by_mac = {s.name: s for s in stations}
    assert by_mac[counter].rain_in == pytest.approx(1.0), "the counter's rise is the day's rain"
    assert by_mac[gaugeless].rain_in is None, "no gauge is None, never 0.00"


# (The gauge-less dry-streak pin lives beside its siblings in
# test_insights.py: test_a_station_with_no_rain_gauge_has_no_dry_streak.)


# ───────── BE-N2: a gauge reset is not a year of 0.00 ─────────

def test_a_counter_reset_re_differences_from_the_first_reading_after_it(client):
    """Rows at 16.00 and 18.47 in, then a replacement gauge posts 0.20 and
    0.35: every bucket used to clamp to 0.0 (cur − prior negative) until
    the new counter climbed past 18.47 — the rest of the year. Now the
    period's rain is the rise since the reset."""
    from app import db
    mac = "5D:5D:02:00:00:7E"
    HOUR, DAY = 3_600_000, 86_400_000
    now = int(time.time() * 1000)

    async def run():
        await db.insert_observations(mac, [
            {"dateutc": now - 400 * DAY, "yearlyrainin": 16.00},
            {"dateutc": now - 3 * DAY,   "yearlyrainin": 18.47},
            {"dateutc": now - 30 * HOUR, "yearlyrainin": 0.20},     # the reset
            {"dateutc": now,             "yearlyrainin": 0.35},
        ])
        r = await db.rain_rollups(mac, "America/Phoenix")
        # A calibration wobble is not a reset: a hundredth down still clamps.
        mac2 = "5D:5D:02:00:00:7F"
        await db.insert_observations(mac2, [
            {"dateutc": now - 3 * DAY, "yearlyrainin": 18.47},
            {"dateutc": now,           "yearlyrainin": 18.46},
        ])
        return r, await db.rain_rollups(mac2, "America/Phoenix")

    r, r2 = asyncio.run(run())
    assert r["yearly_in"] == pytest.approx(0.15)
    assert r["monthly_in"] == pytest.approx(0.15)
    assert r["weekly_in"] == pytest.approx(0.15)
    assert r2["weekly_in"] == 0.0
    # And the served reading agrees: the year bucket is the rise since the
    # reset, the raw counter rides beside it.
    cur = client.get(f"/api/devices/{mac}/current", headers=H).json()
    assert cur["yearlyrainin"] == pytest.approx(0.15)
    assert cur["totalrainin"] == pytest.approx(0.35)


# ───────── BE-N4: January 1 is a day like any other for a lifetime counter ─────────

def test_day_rain_keeps_january_first_and_refuses_an_implausible_rise():
    from app.day_rain import DAY_RAIN_MAX_IN, day_rain_in
    # A LilyGO counter does not reset on Jan 1; its rise that day is rain.
    assert day_rain_in({"rain_total": None, "yearly_min": 16.76, "yearly_max": 16.80,
                        "day": "2026-01-01"}) == pytest.approx(0.04)
    # A counter swapped mid-day files the whole counter as one day: refused.
    assert day_rain_in({"rain_total": None, "yearly_min": 0.5,
                        "yearly_max": DAY_RAIN_MAX_IN + 1.5, "day": "2026-06-01"}) is None
    assert day_rain_in({"rain_total": None, "yearly_min": 0.5,
                        "yearly_max": DAY_RAIN_MAX_IN + 0.5, "day": "2026-06-01"}) == DAY_RAIN_MAX_IN


def test_the_reset_signature_fires_on_any_day(client):
    """Round-three review BE-F5: a UTC-midnight reset on a UTC-7 station
    lands on December 31 local, and a replaced gauge resets on any day;
    the signature (low near zero, high in the inches) is the reset, not
    the calendar. A lifetime counter's Jan 1 has both in the tens and is
    kept; a genuine rise from a low base is kept."""
    from app.day_rain import day_rain_in
    assert day_rain_in({"rain_total": None, "yearly_min": 0.0,
                        "yearly_max": 41.3, "day": "2025-12-31"}) is None
    assert day_rain_in({"rain_total": None, "yearly_min": 0.02,
                        "yearly_max": 18.47, "day": "2026-06-14"}) is None
    assert day_rain_in({"rain_total": None, "yearly_min": 16.76,
                        "yearly_max": 16.80, "day": "2026-01-01"}) == pytest.approx(0.04)
    assert day_rain_in({"rain_total": None, "yearly_min": 0.12,
                        "yearly_max": 0.40, "day": "2026-03-03"}) == pytest.approx(0.28)


def test_a_silent_lifetime_counter_still_serves_year_to_date(client):
    """Round-three review BE-F1, on the wire: a yearly-only station silent
    nine days serves the year bucket and the lifetime total, not the
    lifetime total as the year."""
    from app import db
    mac = "5D:5D:02:00:00:7A"
    DAY = 86_400_000
    now = int(time.time() * 1000)
    asyncio.run(db.insert_observations(mac, [
        {"dateutc": now - 400 * DAY, "yearlyrainin": 16.00},
        {"dateutc": now - 9 * DAY,   "yearlyrainin": 18.47},
    ]))
    cur = client.get(f"/api/devices/{mac}/current", headers=H).json()
    assert cur["yearlyrainin"] == pytest.approx(2.47)
    assert cur["totalrainin"] == pytest.approx(18.47)


# ───────── BE-N3: the composed name and the location are cleaned ─────────

def test_a_control_character_in_model_or_location_never_reaches_the_device_row(client):
    r = client.post("/ingest/custom", headers=IH, json={
        "device": {"id": "AABBCC0000E1", "model": "Atlas\r\nX-Subject: pwned",
                   "location": "Back\rfence"},
        "timestamp_utc": "2026-05-17T15:00:00Z",
        "outdoor": {"tempf": 80.0}, "source": "test"})
    assert r.status_code == 200
    dev = [d for d in client.get("/api/devices", headers=H).json()
           if d["mac"] == "AA:BB:CC:00:00:E1"][0]
    assert dev["name"] == "Test", dev["name"]
    assert "\r" not in (dev.get("location") or "")
    assert dev.get("location") in (None, "")


# ───────── BE-N6: WU-protocol credentials are redacted from captures ─────────

def test_capture_redacts_the_wu_protocol_id_and_password(client, temp_env):
    from pathlib import Path
    r = client.get("/ingest/capture/wu-test?ID=KAZCHAND1&PASSWORD=hunter2&tempf=71.2",
                   headers={"Authorization": "Bearer test-capture-token"})
    assert r.status_code == 200, r.text
    logged = (Path(temp_env).parent / "captures" / "wu-test.jsonl").read_text()
    assert "hunter2" not in logged and "KAZCHAND1" not in logged
    assert "<redacted>" in logged and "71.2" in logged


# ───────── BE-N7: the resolver's threads are bounded ─────────

def test_the_webhook_resolver_runs_on_its_own_small_pool():
    from app import webhooks as wh
    assert wh._RESOLVER_POOL._max_workers == wh.RESOLVER_THREADS == 4

    async def run():
        return await wh._to_thread(lambda a, b: a + b, 2, 3)
    assert asyncio.run(run()) == 5


# ───────── BE-N8: Govee suffixes and the probe's kv write ─────────

def test_a_short_govee_suffix_is_refused():
    from app.govee_cloud_poller import resolve_device_ids
    listed = ["04:B5:DC:B4:D9:F2:DE:20"]
    assert resolve_device_ids(["20"], listed) == ([], ["20"])
    assert resolve_device_ids(["E:20"], listed) == ([], ["E:20"])
    assert resolve_device_ids(["DE:20"], listed) == (["04:B5:DC:B4:D9:F2:DE:20"], [])


def test_the_probe_does_not_promote_an_env_only_device_list_into_kv(client, monkeypatch):
    from app import db, integrations
    from app.config import settings

    class FakeGovee:
        def __init__(self, key): pass
        async def list_devices(self):
            from app.govee_cloud_poller import AIR_TYPE
            return [{"device": "04:B5:DC:B4:D9:F2:DE:20", "sku": "H5140",
                     "deviceName": "Living Room", "type": AIR_TYPE, "capabilities": []}]
    import app.govee_cloud_client as gc
    monkeypatch.setattr(gc, "GoveeCloudClient", FakeGovee)
    monkeypatch.setattr(settings, "govee_api_key", "k" * 36)
    monkeypatch.setattr(settings, "govee_devices", "DC:B4:D9:F2:DE:20")

    async def run():
        err = await integrations.probe("govee")
        return err, await db.get_kv(integrations._kv_key("govee", "devices"))
    err, stored = asyncio.run(run())
    assert err is None, err
    assert stored is None, "an env-only list must not become an app override"


# ───────── Doren 2026-09-06: the Govee must not flap against a 5-minute threshold ─────────

def test_an_unchanged_govee_reading_is_reposted_inside_two_minutes():
    """REPOST_AFTER_S was ten minutes; against Doren's five-minute stale
    threshold his Govee CO2 went "stopped reporting" / "reporting again"
    every five minutes. The repost window must sit well under any sane
    threshold."""
    from app.govee_cloud_poller import REPOST_AFTER_S
    assert REPOST_AFTER_S <= 120
