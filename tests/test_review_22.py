"""The 2.2 release review's backend findings (docs/ZASDER_WEATHER_2_2_RELEASE_REVIEW_2026-09-10.md),
each reproduced then fixed: R22-06 backup allowlist, R22-07 the day's
window, R22-08 a moon-only sky verdict, R22-09 the outlook day stamp.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

H = {"Authorization": "Bearer test-api-token"}
I = {"Authorization": "Bearer test-ingest-token", "Content-Type": "application/json"}


def test_backup_round_trips_the_2_2_preferences(client):
    """R22-06: export carried none of the five, import ignored them."""
    from app import config_backup
    client.put("/api/alerts", headers=H, json={
        "outlook_hour": 19, "outlook_minute": 30, "outlook_source": "twc",
        "sky_notes": True, "sky_good_only": True})
    exported = asyncio.run(config_backup.export_config())
    prefs = exported["alert_prefs"] if "alert_prefs" in exported else exported
    flat = str(exported)
    for k in ("outlook_hour", "outlook_minute", "outlook_source", "sky_notes", "sky_good_only"):
        assert k in flat, k
    # Coercion: bounds and the enum hold on import.
    assert config_backup._coerce_alert_pref("outlook_hour", 24) is config_backup._INVALID
    assert config_backup._coerce_alert_pref("outlook_minute", 59) == 59
    assert config_backup._coerce_alert_pref("outlook_source", "bogus") is config_backup._INVALID
    assert config_backup._coerce_alert_pref("outlook_source", "twc") == "twc"
    assert config_backup._coerce_alert_pref("sky_notes", True) == 1
    assert config_backup._coerce_alert_pref("sky_good_only", "yes") is config_backup._INVALID


def test_day_report_stops_before_the_next_midnight(client, monkeypatch):
    """R22-07: the inclusive history window handed the next midnight's
    reading to hour 0 of the requested day."""
    from datetime import date
    from app import climate, config
    monkeypatch.setattr(config.settings, "timezone", "UTC")
    mac = "AA:BB:CC:DD:EE:70"
    for ts, t in (("2026-06-02T12:00:00Z", 60.0), ("2026-06-03T00:00:00Z", 100.0)):
        r = client.post("/ingest/custom", headers=I,
                        json={"device": {"id": mac, "name": "Edge"}, "timestamp_utc": ts,
                              "outdoor": {"tempf": t}})
        assert r.status_code == 200, r.text
    rows = asyncio.run(climate.day_hours(mac, date(2026, 6, 2)))
    with_data = {r["hour"]: r for r in rows if r.get("tmax") is not None}
    assert set(with_data) == {12}, sorted(with_data)
    assert with_data[12]["tmax"] == 60.0
    # And the next day owns its midnight reading.
    rows3 = asyncio.run(climate.day_hours(mac, date(2026, 6, 3)))
    assert {r["hour"]: r["tmax"] for r in rows3 if r.get("tmax") is not None} == {0: 100.0}


def test_moon_alone_is_no_verdict(client):
    """R22-08: no cloud forecast and no station reading used to score
    1.0 and say 'Clear, dry and calm.'"""
    from app import sky
    tz = ZoneInfo("America/Phoenix")
    sunset = datetime(2026, 9, 10, 18, 40, tzinfo=tz)
    moon_only = sky.SkyInputs(None, None, None, None, 0.05, False)
    title, body, v = sky.note(sunset_local=sunset, sunrise_next_local=None,
                              moon_phase="new moon", moon_illumination=0.05, inputs=moon_only)
    assert v == sky.UNKNOWN and title == "Sky tonight"
    assert "Clear" not in body and "judge the night" in body
    # One weather input is enough to judge again.
    some = sky.SkyInputs(10.0, None, None, None, 0.05, False)
    assert sky.note(sunset_local=sunset, sunrise_next_local=None, moon_phase="new moon",
                    moon_illumination=0.05, inputs=some)[2] == sky.GOOD


def test_outlook_day_is_not_stamped_until_the_row_exists(client, monkeypatch):
    """R22-09: a failed insert_report with no channels stamped the day and
    the report never existed. Now the row is retried next tick and the
    delivery is not repeated."""
    from app import alerts, db, config, apns
    from tests.test_outlook import DAILY
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    client.post("/ingest/custom", headers=I,
                json={"device": {"id": "AA:BB:CC:DD:EE:71", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": "2026-09-10T20:00:00Z", "outdoor": {"tempf": 100}})
    client.put("/api/alerts", headers=H, json={"outlook_hour": 19, "outlook_minute": 0})

    async def fake_fetch(self, coords, source, wu_key, tz_name):
        return DAILY, [], "open-meteo", None
    pushes = []

    async def yes():
        return True

    async def send(title, body, route=None, **kw):
        pushes.append(route)
        return {"sent": 1, "total": 1}
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fake_fetch)
    monkeypatch.setattr(apns, "push_configured", yes)
    monkeypatch.setattr(apns, "send_to_all", send)
    real_insert = db.insert_report
    fail = {"n": 1}

    async def flaky_insert(*a, **kw):
        if fail["n"]:
            fail["n"] -= 1
            raise RuntimeError("disk hiccup")
        return await real_insert(*a, **kw)
    monkeypatch.setattr(db, "insert_report", flaky_insert)
    tz = ZoneInfo("America/Phoenix")
    t1 = int(datetime(2026, 9, 10, 19, 35, tzinfo=tz).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        mon = alerts.AlertMonitor()
        await mon._maybe_send_outlook(cfg, devs, t1)
        after_fail = (await db.list_reports(kind="outlook"), await db.get_kv("alerts.outlook.day"))
        await mon._maybe_send_outlook(cfg, devs, t1 + 60_000)
        return after_fail, (await db.list_reports(kind="outlook"), await db.get_kv("alerts.outlook.day"))
    (rows1, stamp1), (rows2, stamp2) = asyncio.run(run())
    assert rows1 == [] and not stamp1, "a missing row must not stamp the day"
    assert len(rows2) == 1 and stamp2 == "2026-09-10"
    assert len(pushes) == 1, "the push went once, not again on the retry"
