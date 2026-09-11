"""Sky notes (2.2, Doren): the stargazing verdict and its note."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

H = {"Authorization": "Bearer test-api-token"}


def test_score_and_verdict(client):
    from app import sky
    clear = sky.SkyInputs(cloud_pct=5, humidity_pct=30, dew_spread_f=25, wind_mph=3,
                          moon_illumination=0.1, moon_up=True)
    s, reasons = sky.score(clear)
    assert s == 1.0 and reasons == [] and sky.verdict(s) == sky.GOOD
    # A full moon below the horizon costs nothing; above it, plenty.
    moon_down = sky.SkyInputs(5, 30, 25, 3, 0.98, False)
    moon_up = sky.SkyInputs(5, 30, 25, 3, 0.98, True)
    assert sky.score(moon_down)[0] == 1.0
    s_up, r_up = sky.score(moon_up)
    assert sky.verdict(s_up) != sky.GOOD and r_up == ["moon 98% lit"]
    # Overcast is poor whatever else is true; the reason leads with cloud.
    over = sky.SkyInputs(90, 30, 25, 3, 0.1, True)
    s_o, r_o = sky.score(over)
    assert sky.verdict(s_o) == sky.POOR and r_o[0] == "90% cloud"
    # Damp and windy: fair at best, reasons name both.
    damp = sky.SkyInputs(10, 85, 2, 16, 0.1, True)
    s_d, r_d = sky.score(damp)
    assert sky.verdict(s_d) != sky.GOOD
    assert any("dew" in r for r in r_d) and any("wind" in r for r in r_d)
    # Nothing known: no verdict of "good" from silence.
    s_n, r_n = sky.score(sky.SkyInputs(None, None, None, None, None, None))
    assert s_n == 0.0 and r_n == ["no readings to judge by"]


def test_note_words(client):
    from app import sky
    tz = ZoneInfo("America/New_York")
    sunset = datetime(2026, 9, 10, 19, 28, tzinfo=tz)
    sunrise = datetime(2026, 9, 11, 6, 52, tzinfo=tz)
    good = sky.SkyInputs(5, 40, 20, 2, 0.05, False)
    title, body, v = sky.note(sunset_local=sunset, sunrise_next_local=sunrise,
                              moon_phase="new moon", moon_illumination=0.05, inputs=good)
    assert v == sky.GOOD and title == "Good night for the scope"
    assert body == ("Sunset 7:28 PM. sunrise 6:52 AM. new moon, 5% lit, below the horizon "
                    "this evening. Clear, dry and calm.")
    poor = sky.SkyInputs(85, 90, 1, 3, 0.6, True)
    title, body, v = sky.note(sunset_local=sunset, sunrise_next_local=None,
                              moon_phase="waxing gibbous", moon_illumination=0.6, inputs=poor)
    assert v == sky.POOR and title == "Not a night for the scope"
    assert body.startswith("Sunset 7:28 PM. waxing gibbous, 60% lit. 85% cloud")


def test_evening_cloud_mean(client):
    from app import sky
    now = datetime(2026, 9, 10, 18, 40)
    hourly = {"time": [f"2026-09-10T{h:02d}:00" for h in range(24)] + [f"2026-09-11T{h:02d}:00" for h in range(6)],
              "cloud_cover": [100] * 20 + [10, 20, 30, 40] + [50, 60, 70, 100, 100, 100]}
    # 20..23 tonight (10,20,30,40) + 00..02 tomorrow (50,60,70) = 280/7
    assert sky.mean_evening_cloud(hourly, now) == 40.0
    assert sky.mean_evening_cloud({}, now) is None


def test_prefs_round_trip(client):
    r = client.put("/api/alerts", headers=H, json={"sky_notes": True, "sky_good_only": True})
    assert r.status_code == 200, r.text
    g = client.get("/api/alerts", headers=H).json()
    assert g["sky_notes"] is True and g["sky_good_only"] is True
    client.put("/api/alerts", headers=H, json={"sky_good_only": False})
    assert client.get("/api/alerts", headers=H).json()["sky_good_only"] is False


def test_note_goes_out_once_after_sunset_and_good_only_holds(client, monkeypatch):
    from app import alerts, db, config, sky
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:33", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": "2026-09-11T00:30:00Z",
                      "outdoor": {"tempf": 95, "humidity": 20, "dew_point_f": 50},
                      "wind": {"speed_mph": 2}})
    client.put("/api/alerts", headers=H, json={"sky_notes": True, "sky_good_only": False})
    sent = []

    async def fake_deliver(cfg, subject, text, title, body, **kw):
        sent.append((title, body, kw.get("kind")))
        return True

    async def fake_clouds(self, lat, lon, tz_name, now_local):
        return 8.0
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_clouds", fake_clouds)
    tz = ZoneInfo("America/Phoenix")
    noon = int(datetime(2026, 9, 10, 12, 0, tzinfo=tz).timestamp() * 1000)
    dusk = int(datetime(2026, 9, 10, 18, 30, tzinfo=tz).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        mon = alerts.AlertMonitor()
        await mon._maybe_send_sky(cfg, devs, noon)        # long before sunset
        n_noon = len(sent)
        await mon._maybe_send_sky(cfg, devs, dusk)
        await mon._maybe_send_sky(cfg, devs, dusk + 60_000)
        return n_noon, await db.get_kv("alerts.sky.day")
    n_noon, stamp = asyncio.run(run())
    assert n_noon == 0 and len(sent) == 1 and stamp == "2026-09-10"
    title, body, kind = sent[0]
    assert kind == "sky" and "Sunset" in body and title.endswith("for the scope")

    # Good-only: a poor night is held and the day still stamped.
    sent.clear()
    client.put("/api/alerts", headers=H, json={"sky_good_only": True})

    async def cloudy(self, lat, lon, tz_name, now_local):
        return 95.0
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_clouds", cloudy)

    async def run2():
        await db.set_kv("alerts.sky.day", "")
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        await alerts.AlertMonitor()._maybe_send_sky(cfg, devs, dusk)
        return await db.get_kv("alerts.sky.day")
    assert asyncio.run(run2()) == "2026-09-10" and sent == []
