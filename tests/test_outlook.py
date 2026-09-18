"""The outlook report (2.2, Doren): tomorrow's forecast in the evening,
today's in the morning, from the owner's source, stored and delivered
like the morning report.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

H = {"Authorization": "Bearer test-api-token"}

DAILY = {
    "time": ["2026-09-10", "2026-09-11"],
    "weather_code": [1, 95],
    "temperature_2m_max": [104.2, 96.0],
    "temperature_2m_min": [81.0, 77.5],
    "precipitation_probability_max": [5, 60],
    "wind_speed_10m_max": [12.3, 24.0],
    "sunrise": ["2026-09-10T06:12", "2026-09-11T06:13"],
    "sunset": ["2026-09-10T18:41", "2026-09-11T18:40"],
}


def test_build_picks_the_slot_and_words(client):
    from app import outlook as ol
    tz = ZoneInfo("America/Phoenix")
    evening = datetime(2026, 9, 10, 19, 30, tzinfo=tz)
    r = ol.build(DAILY, narrative=None, when=ol.slot_for(evening.hour),
                 now_local=evening, source=ol.SOURCE_OPEN_METEO)
    assert r.when == "tomorrow" and r.for_date == "2026-09-11"
    assert r.sky == "thunderstorms" and r.hi_f == 96.0 and r.precip_pct == 60
    assert r.sunrise == "06:13" and r.sunset == "18:40"
    assert ol.title(r) == "Tomorrow's outlook · Fri Sep 11"
    body = ol.text(r)
    assert body.splitlines()[0] == "Tomorrow, Friday, September 11"
    assert "Thunderstorms, high near 96F, low around 78F, 60% chance of precipitation, wind up to 24 mph." in body
    assert "Sunrise 06:13, sunset 18:40." in body
    assert body.endswith("Forecast by Open-Meteo.")
    morning = datetime(2026, 9, 10, 6, 0, tzinfo=tz)
    t = ol.build(DAILY, narrative=None, when=ol.slot_for(morning.hour),
                 now_local=morning, source=ol.SOURCE_OPEN_METEO)
    assert t.when == "today" and t.for_date == "2026-09-10" and t.sky == "mostly clear"
    assert ol.push_text(t) == ("Today's outlook · Thu Sep 10", "Mostly clear, 104/81, 5% precipitation")


def test_twc_narrative_and_fallback_marking(client):
    from app import outlook as ol
    tz = ZoneInfo("America/New_York")
    evening = datetime(2026, 9, 10, 20, 0, tzinfo=tz)
    r = ol.build(DAILY, narrative=[{"name": "Today", "text": "Sunny."},
                                   {"name": "Tomorrow", "text": "Storms after 2pm."}],
                 when="tomorrow", now_local=evening, source=ol.SOURCE_TWC)
    assert r.narrative == "Storms after 2pm."
    assert "Storms after 2pm." in ol.text(r) and ol.text(r).endswith("Forecast by The Weather Company.")
    f = ol.build(DAILY, narrative=[], when="tomorrow", now_local=evening,
                 source=ol.SOURCE_OPEN_METEO, fallback_from=ol.SOURCE_TWC)
    assert "was unavailable" in ol.text(f)
    # Absent stays absent: an empty provider day has no numbers, no words.
    e = ol.build({}, narrative=None, when="today", now_local=evening, source=ol.SOURCE_OPEN_METEO)
    assert e.hi_f is None and e.sky is None and ol.push_text(e)[1] == "Open for the forecast."


def test_payload_summary_and_key(client):
    from app import outlook as ol, reports as rp
    tz = ZoneInfo("America/Phoenix")
    r = ol.build(DAILY, narrative=None, when="tomorrow",
                 now_local=datetime(2026, 9, 10, 19, 0, tzinfo=tz), source=ol.SOURCE_OPEN_METEO)
    p = rp.outlook_payload(r)
    assert p["for_date"] == "2026-09-11" and p["when"] == "tomorrow" and p["hi_f"] == 96.0
    assert rp.outlook_summary(p) == "Thunderstorms, 96/78, 60% precipitation"
    assert rp.outlook_key("2026-09-11", "tomorrow") == "outlook:2026-09-11:tomorrow"
    assert rp.KIND_OUTLOOK in rp.KINDS and rp.KIND_OUTLOOK not in rp.RUNNABLE_KINDS


def test_prefs_round_trip(client):
    r = client.put("/api/alerts", headers=H,
                   json={"outlook_hour": 19, "outlook_minute": 30, "outlook_source": "twc"})
    assert r.status_code == 200, r.text
    g = client.get("/api/alerts", headers=H).json()
    assert (g["outlook_hour"], g["outlook_minute"], g["outlook_source"]) == (19, 30, "twc")
    assert client.put("/api/alerts", headers=H,
                      json={"outlook_source": "bogus"}).status_code == 422
    r = client.put("/api/alerts", headers=H, json={"outlook_hour": -1})
    assert client.get("/api/alerts", headers=H).json()["outlook_hour"] is None


def test_a_2_2_server_always_names_its_outlook_source(client):
    """The app gates the Outlook controls on this field: a 2.2 server that
    has never been configured still answers open-meteo, never null."""
    g = client.get("/api/alerts", headers=H).json()
    assert g["outlook_source"] == "open-meteo"


def test_outlook_runs_with_every_channel_off(client, monkeypatch):
    """No email, no push, no webhook: the report is still stored and its
    day stamped, because it runs before the channel gate."""
    from app import alerts, db, config
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:23", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": "2026-09-10T20:00:00Z",
                      "outdoor": {"tempf": 100}})
    client.put("/api/alerts", headers=H, json={"outlook_hour": 19, "outlook_minute": 0})

    async def fake_fetch(self, coords, source, wu_key, tz_name):
        return DAILY, [], "open-meteo", None

    async def no_push():
        return False
    from app import apns
    monkeypatch.setattr(apns, "push_configured", no_push)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fake_fetch)
    monkeypatch.setattr(alerts, "time", __import__("time"))
    tz = ZoneInfo("America/Phoenix")
    at = datetime(2026, 9, 10, 19, 35, tzinfo=tz).timestamp()

    async def run():
        mon = alerts.AlertMonitor()
        monkeypatch.setattr(alerts.time, "time", lambda: at)
        await mon._tick()
        return await db.list_reports(kind="outlook"), await db.get_kv("alerts.outlook.day")
    rows, stamp = asyncio.run(run())
    assert len(rows) == 1 and stamp == "2026-09-10"


def test_the_evening_report_is_stored_once_and_pushed(client, monkeypatch):
    """At/after the hour: one stored row for tomorrow, a push carrying its
    deep link, and the day stamped so the next tick is quiet."""
    from app import alerts, db
    from app import apns
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:22", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": "2026-09-10T20:00:00Z",
                      "outdoor": {"tempf": 100}})
    client.put("/api/alerts", headers=H, json={"outlook_hour": 19, "outlook_minute": 0})
    pushes = []

    async def fake_configured():
        return True

    async def fake_send_to_all(title, body, route=None, **kw):
        pushes.append((title, body, route))
        return {"sent": 1, "total": 1}

    async def fake_fetch(self, coords, source, wu_key, tz_name):
        return DAILY, [], "open-meteo", None
    monkeypatch.setattr(apns, "push_configured", fake_configured)
    monkeypatch.setattr(apns, "send_to_all", fake_send_to_all)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fake_fetch)
    # The gate reads the SERVER clock (settings.timezone), not the host's.
    from app import config
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    tz = ZoneInfo("America/Phoenix")
    at_1935 = int(datetime(2026, 9, 10, 19, 35, tzinfo=tz).timestamp() * 1000)
    at_1800 = int(datetime(2026, 9, 10, 18, 0, tzinfo=tz).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        mon = alerts.AlertMonitor()
        await mon._maybe_send_outlook(cfg, devs, at_1800)      # too early
        early = await db.list_reports(kind="outlook")
        await mon._maybe_send_outlook(cfg, devs, at_1935)
        await mon._maybe_send_outlook(cfg, devs, at_1935 + 60_000)   # same day: quiet
        rows = await db.list_reports(kind="outlook")
        return early, rows, await db.get_kv("alerts.outlook.day")
    early, rows, stamp = asyncio.run(run())
    assert early == [] and len(rows) == 1
    assert rows[0]["for_date"] == "2026-09-11" and rows[0]["title"].startswith("Tomorrow's outlook")
    assert stamp == "2026-09-10"
    assert len(pushes) == 1 and pushes[0][2] == f"report/{rows[0]['id']}"


def test_the_push_and_the_html_carry_the_narrative(client):
    """2.3 (Doren, 09-11): the push carried only the one-line summary while
    the email carried the provider's prose. Now both do, the push cut at
    a sentence end well inside APNs' 4 KB, and the email has an HTML twin
    in the morning report's dress with the text as the alternative."""
    from app import outlook as ol
    narrative = ("Cloudy in the morning with scattered thunderstorms developing "
                 "later in the day. High 81F. Winds S at 5 to 10 mph. Chance of "
                 "rain 60%. Localized flooding is possible.")
    evening = datetime(2026, 9, 10, 23, 0, tzinfo=ZoneInfo("America/New_York"))
    nar = [{"name": "Tonight", "text": "x"}, {"name": "Tomorrow", "text": narrative}]
    r = ol.build(DAILY, narrative=nar, when="tomorrow", now_local=evening,
                 source=ol.SOURCE_TWC)
    title, body = ol.push_text(r)
    assert title.startswith("Tomorrow's outlook")
    assert body.startswith("Thunderstorms, 96/78, 60% precipitation. Cloudy in the morning")
    assert body.endswith("Localized flooding is possible.")
    long = ("A sentence that runs on. " * 60).strip()
    r2 = ol.build(DAILY, narrative=[{"name": "Tonight", "text": "x"},
                                    {"name": "Tomorrow", "text": long}],
                  when="tomorrow", now_local=evening, source=ol.SOURCE_TWC)
    _, body2 = ol.push_text(r2)
    assert len(body2) < 700 and body2.endswith("runs on.")
    page = ol.build_html(r)
    for needle in ("THE FORECAST", "Localized flooding is possible.", "HIGH", "96&deg;",
                   "LOW", "78&deg;", "PRECIP", "60%", "WIND", "24 mph", "SUNRISE",
                   "SUNSET", "Forecast by The Weather Company.", "<!DOCTYPE html>"):
        assert needle in page, needle
    assert "<script" not in page and "http" not in page.lower()
    # No narrative: no prose card, and the push is the summary alone.
    r3 = ol.build(DAILY, narrative=[], when="tomorrow", now_local=evening,
                  source=ol.SOURCE_OPEN_METEO)
    assert "THE FORECAST" not in ol.build_html(r3)
    assert ol.push_text(r3)[1] == "Thunderstorms, 96/78, 60% precipitation"


def test_the_outlook_email_rides_with_its_html_twin(client, monkeypatch):
    """The SMTP call gets the HTML as the alternative to the text."""
    from app import alerts, db, config
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:23", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": "2026-09-10T20:00:00Z",
                      "outdoor": {"tempf": 100}})
    client.put("/api/alerts", headers=H, json={
        "outlook_hour": 19, "outlook_minute": 0, "enabled": True,
        "recipients": ["d@example.com"], "smtp_host": "smtp.example.com",
        "smtp_port": 587, "smtp_username": "u", "smtp_password": "p"})
    mails = []

    def fake_send(subject, body, to_list, cfg, html=None):
        mails.append((subject, body, html))

    async def fake_fetch(self, coords, source, wu_key, tz_name):
        return DAILY, [{"name": "Tonight", "text": "x"},
                       {"name": "Tomorrow", "text": "Mostly clear tonight."}], "twc", None
    monkeypatch.setattr(alerts, "_send_sync", fake_send)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fake_fetch)
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    tz = ZoneInfo("America/Phoenix")
    at = int(datetime(2026, 9, 10, 19, 5, tzinfo=tz).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        await alerts.AlertMonitor()._maybe_send_outlook(cfg, devs, at)
    asyncio.run(run())
    assert len(mails) == 1
    subject, body, page = mails[0]
    assert subject.startswith("[Zasder Weather] Tomorrow's outlook")
    assert "Mostly clear tonight." in body and page is not None
    assert "Mostly clear tonight." in page and page.startswith("<!DOCTYPE html>")


def test_a_whitespace_narrative_is_no_narrative(client):
    from app import outlook as ol
    evening = datetime(2026, 9, 10, 23, 0, tzinfo=ZoneInfo("America/New_York"))
    r = ol.build(DAILY, narrative=[{"name": "Tomorrow", "text": "  \n\t "}],
                 when="tomorrow", now_local=evening, source=ol.SOURCE_TWC)
    assert r.narrative is None
    assert ol.push_text(r)[1] == "Thunderstorms, 96/78, 60% precipitation"
    assert "THE FORECAST" not in ol.build_html(r) and not ol.text(r).count("\n\n\n")
    r2 = ol.build(DAILY, narrative=[{"name": "Tomorrow", "text": " Clear.\n\nCalm. "}],
                  when="tomorrow", now_local=evening, source=ol.SOURCE_TWC)
    assert r2.narrative == "Clear. Calm."


def test_the_outlook_push_lands_in_the_history_and_reaches_webhooks(client, monkeypatch):
    """R23: the outlook (and the morning phone push) called apns directly,
    so neither wrote an alert_log row nor fired a webhook — the survey's
    item (a). Routed through _deliver now; the owner chose this hour, so
    quiet hours do not hold it either."""
    from app import alerts, apns, db, config, webhooks as wh
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:24", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": "2026-09-10T20:00:00Z",
                      "outdoor": {"tempf": 100}})
    # Quiet hours covering the whole day: an owner-scheduled push ignores them.
    client.put("/api/alerts", headers=H, json={"outlook_hour": 19, "outlook_minute": 0,
                                               "quiet_start_min": 0, "quiet_end_min": 1439})
    monkeypatch.setattr(wh, "validate_webhook_url", lambda url: None)
    assert client.post("/api/webhooks", headers=H,
                       json={"url": "https://hooks.example.com/outlook"}).status_code == 200
    hooked = []

    async def fake_send(hook, payload):
        hooked.append(payload)
    monkeypatch.setattr(wh, "_send", fake_send)
    pushes = []

    async def fake_configured():
        return True

    async def fake_send_to_all(title, body, route=None, **kw):
        pushes.append((title, body, route))
        return {"sent": 1, "total": 1}

    async def fake_fetch(self, coords, source, wu_key, tz_name):
        return DAILY, [], "open-meteo", None
    monkeypatch.setattr(apns, "push_configured", fake_configured)
    monkeypatch.setattr(apns, "send_to_all", fake_send_to_all)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fake_fetch)
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    tz = ZoneInfo("America/Phoenix")
    at_1935 = int(datetime(2026, 9, 10, 19, 35, tzinfo=tz).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        mon = alerts.AlertMonitor()
        await mon._maybe_send_outlook(cfg, devs, at_1935)
        # Let the fire-and-forget webhook task run.
        for t in list(alerts._WEBHOOK_TASKS):
            await t
        return await db.recent_alerts(10), await db.get_kv("alerts.outlook.day")
    rows, stamp = asyncio.run(run())
    assert len(pushes) == 1 and stamp == "2026-09-10"
    outlook_rows = [r for r in rows if r["kind"] == "outlook"]
    assert len(outlook_rows) == 1 and outlook_rows[0]["severity"] == "info"
    assert outlook_rows[0]["delivered"]
    assert len(hooked) == 1
    import json
    body = json.loads(hooked[0])
    assert body["kind"] == "outlook" and body["title"] == pushes[0][0]
