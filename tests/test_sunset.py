"""Sunset outlook (2.8, Doren 10-17): the score, its words, the report
line, the good-sunset heads-up, its settings and where it is shown."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

H = {"Authorization": "Bearer test-api-token"}
PHX = ZoneInfo("America/Phoenix")


# ── the formula ────────────────────────────────────────────────────────

def test_clear_sky_is_a_plain_sunset_not_no_sunset():
    from app import sunset as ss
    s = ss.score(ss.SunsetInputs(low_pct=0, mid_pct=0, high_pct=0,
                                 visibility_km=30, humidity_pct=25, aod=0.15))
    assert s is not None and s.pct == 30 and s.verdict == "fair"
    assert s.reasons == ["a clear sky, so plain colors", "a clear horizon"]


def test_broken_high_cloud_over_a_clear_horizon_is_the_best_case():
    from app import sunset as ss
    s = ss.score(ss.SunsetInputs(low_pct=5, mid_pct=10, high_pct=50,
                                 visibility_km=40, humidity_pct=30, aod=0.2))
    assert s.pct >= 90 and s.verdict == "looks good"
    assert s.reasons[:2] == ["broken high cloud", "a clear horizon"]


def test_low_overcast_closes_the_horizon_whatever_is_above_it():
    from app import sunset as ss
    s = ss.score(ss.SunsetInputs(low_pct=95, mid_pct=40, high_pct=50))
    assert s.pct <= 5 and s.verdict == "looks dull"
    assert "low cloud blocking the horizon" in s.reasons


def test_smoke_dims_an_otherwise_good_sky():
    from app import sunset as ss
    clean = ss.score(ss.SunsetInputs(low_pct=5, high_pct=50, aod=0.2))
    smoky = ss.score(ss.SunsetInputs(low_pct=5, high_pct=50, aod=1.5))
    assert smoky.pct < clean.pct - 30
    assert "smoke or dust in the air" in smoky.reasons


def test_rain_clearing_before_sunset_earns_the_bonus_only_once_it_stops():
    from app import sunset as ss
    base = dict(low_pct=20, mid_pct=40, high_pct=20)
    dry = ss.score(ss.SunsetInputs(**base))
    clearing = ss.score(ss.SunsetInputs(**base, rain_before_in=0.12, rain_at_in=0.0))
    still_raining = ss.score(ss.SunsetInputs(**base, rain_before_in=0.12, rain_at_in=0.05))
    assert clearing.pct == dry.pct + 8 and "rain clearing just before" in clearing.reasons
    assert still_raining.pct == dry.pct


def test_no_cloud_forecast_is_no_score_and_a_missing_reading_is_neutral():
    from app import sunset as ss
    assert ss.score(ss.SunsetInputs(visibility_km=30, humidity_pct=20)) is None
    with_air = ss.score(ss.SunsetInputs(low_pct=0, high_pct=40, humidity_pct=50,
                                        visibility_km=30, aod=0.2))
    without = ss.score(ss.SunsetInputs(low_pct=0, high_pct=40))
    assert with_air.pct == without.pct


def test_the_report_line():
    from app import sunset as ss
    s = ss.score(ss.SunsetInputs(low_pct=5, high_pct=50))
    when = datetime(2026, 11, 3, 17, 37, tzinfo=PHX)
    assert ss.line(when, s) == (f"Sunset 5:37 PM, looks good: {s.pct}%, "
                                "broken high cloud and a clear horizon")


def test_inputs_are_read_at_the_sunset_minute_and_rain_summed_before():
    from app import sunset as ss
    times = [f"2026-11-03T{h:02d}:00" for h in range(24)]
    hourly = {"time": times,
              "cloud_cover_low": [0] * 17 + [10, 30] + [0] * 5,
              "cloud_cover_mid": [0] * 24,
              "cloud_cover_high": [0] * 17 + [40, 60] + [0] * 5,
              "visibility": [24140.0] * 24,
              "relative_humidity_2m": [30] * 24,
              "precipitation": [0] * 14 + [0.05, 0.02, 0.01, 0.0] + [0] * 6}
    aq = {"time": times, "aerosol_optical_depth": [0.1] * 17 + [0.3] + [0.1] * 6}
    i = ss.inputs_at(hourly, aq, datetime(2026, 11, 3, 17, 30, tzinfo=PHX))
    assert i.low_pct == pytest.approx(20.0) and i.high_pct == pytest.approx(50.0)
    assert i.visibility_km == pytest.approx(24.14)
    assert i.rain_before_in == pytest.approx(0.08)      # 14:00 + 15:00 + 16:00
    assert i.rain_at_in == 0.0 and i.aod == pytest.approx(0.3)


# ── settings, backup, endpoint ─────────────────────────────────────────

def test_settings_round_trip_and_bounds(client):
    g = client.get("/api/alerts", headers=H).json()
    assert g["sunset_alert"] is False and g["sunset_threshold"] == 70
    r = client.put("/api/alerts", headers=H, json={"sunset_alert": True, "sunset_threshold": 80})
    assert r.status_code == 200
    g = client.get("/api/alerts", headers=H).json()
    assert g["sunset_alert"] is True and g["sunset_threshold"] == 80
    for bad in (0, 101):
        assert client.put("/api/alerts", headers=H,
                          json={"sunset_threshold": bad}).status_code == 422
    from app import config_backup as cb
    assert cb._coerce_alert_pref("sunset_threshold", 85) == 85
    assert cb._coerce_alert_pref("sunset_threshold", 150) is cb._INVALID
    assert cb._coerce_alert_pref("sunset_alert", True) == 1


def test_the_endpoint_answers_null_without_a_station(client):
    r = client.get("/api/sunset-outlook", headers=H)
    assert r.status_code == 200 and r.json() == {"outlook": None}
    assert client.get("/api/sunset-outlook").status_code == 401


def test_the_endpoint_serves_the_next_sunset(client, monkeypatch):
    from app import sunset as ss
    _yard(client)
    seen = {}

    async def fake(lat, lon, sunset_at, tz, tz_name):
        seen["day"], seen["at"] = sunset_at, (lat, lon)
        return {"date": sunset_at.date().isoformat(), "sunset": "17:35", "sunset_ms": 1, "pct": 81,
                "verdict": "looks good", "reasons": ["broken high cloud"],
                "line": "Sunset 5:35 PM, looks good: 81%, broken high cloud"}
    monkeypatch.setattr(ss, "outlook_at", fake)
    body = client.get("/api/sunset-outlook", headers=H).json()
    assert body["outlook"]["pct"] == 81 and isinstance(seen["day"], datetime)
    # The dashboard asks for the station it is showing.
    client.get("/api/sunset-outlook", headers=H, params={"lat": 40.3, "lon": -79.7})
    assert seen["at"] == (40.3, -79.7)
    assert client.get("/api/sunset-outlook", headers=H,
                      params={"lat": 91, "lon": 0}).status_code == 422


# ── reports ────────────────────────────────────────────────────────────

SAMPLE = {"date": "2026-11-03", "sunset": "17:34", "sunset_ms": 1, "pct": 90,
          "verdict": "looks good", "reasons": ["broken high cloud", "a clear horizon"],
          "line": "Sunset 5:34 PM, looks good: 90%, broken high cloud and a clear horizon"}


def test_the_reports_carry_the_line():
    from app import digest as dg
    from app import outlook as ol
    from app import reports as rp
    rep = dg.Report(date_label="Tuesday, November 3", sunset_outlook=SAMPLE)
    payload = rp.morning_payload(rep)
    assert payload["sunset_outlook"]["pct"] == 90 and "sunset_ms" not in payload["sunset_outlook"]
    assert SAMPLE["line"] + "." in dg.build_text(rep)
    assert "looks good: 90%" in dg.build_html(rep)
    assert "Sunset looks good: 90%" in dg.push_text(rep)[1]
    o = ol.OutlookReport(date_label="Tuesday, November 3", for_date="2026-11-03",
                         when="tomorrow", hi_f=80, lo_f=55, precip_pct=0,
                         wind_max_mph=8, sky="clear", narrative=None, sunrise="06:46",
                         sunset="17:34", source="open-meteo", sunset_outlook=SAMPLE)
    assert rp.outlook_payload(o)["sunset_outlook"]["line"] == SAMPLE["line"]
    assert SAMPLE["line"] + "." in ol.text(o)
    assert "sunset looks good (90%)" in ol.push_text(o)[1]
    assert "looks good: 90%" in ol.build_html(o)
    # A dull evening stays out of the push but in the report.
    dull = dict(SAMPLE, pct=10, verdict="looks dull", line="Sunset 5:34 PM, looks dull: 10%")
    rep2 = dg.Report(date_label="x", sunset_outlook=dull)
    assert "Sunset" not in dg.push_text(rep2)[1] and "looks dull: 10%" in dg.build_text(rep2)
    # Absent stays absent.
    assert rp.morning_payload(dg.Report(date_label="x"))["sunset_outlook"] is None
    assert rp.sunset_wire({"pct": None}) is None


# ── the heads-up ───────────────────────────────────────────────────────

def _yard(client, mac="AA:BB:CC:DD:EE:61", name="Yard", lat=33.3, lon=-111.9):
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": mac, "name": name, "coords": {"lat": lat, "lon": lon}},
                      "timestamp_utc": "2026-11-03T20:00:00Z",
                      "outdoor": {"tempf": 75, "humidity": 20}})


def test_heads_up_once_a_day_inside_the_window_and_over_the_threshold(client, monkeypatch):
    from app import alerts, config, db
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    _yard(client)
    client.put("/api/alerts", headers=H, json={"sunset_alert": True, "sunset_threshold": 70})
    sent = []

    async def fake_deliver(cfg, subject, text, title, body, **kw):
        sent.append((title, body, kw.get("kind"), kw.get("severity")))
        return True
    score = {"pct": 85}

    async def fake_sunset(self, coords, day, tz):
        return {"pct": score["pct"], "verdict": "looks good",
                "line": f"Sunset 5:34 PM, looks good: {score['pct']}%, broken high cloud"}
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_sunset", fake_sunset)
    noon = int(datetime(2026, 11, 3, 12, 0, tzinfo=PHX).timestamp() * 1000)
    window = int(datetime(2026, 11, 3, 17, 5, tzinfo=PHX).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        mon = alerts.AlertMonitor()
        await mon._maybe_send_sunset(cfg, devs, noon)          # too early
        early = len(sent)
        await mon._maybe_send_sunset(cfg, devs, window)
        await mon._maybe_send_sunset(cfg, devs, window + 60_000)  # once a day
        return early
    assert asyncio.run(run()) == 0
    assert len(sent) == 1
    title, body, kind, severity = sent[0]
    assert title == "Good sunset tonight" and body.startswith("Sunset 5:34 PM, looks good: 85%")
    assert kind == "sunset" and severity == "watch"

    # Below the threshold: held, and the day stamped so it is not retried.
    sent.clear()
    score["pct"] = 50

    async def run2():
        await db.set_kv("alerts.sunset.day:AA:BB:CC:DD:EE:61", "")
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        await alerts.AlertMonitor()._maybe_send_sunset(cfg, devs, window)
        return await db.get_kv("alerts.sunset.day:AA:BB:CC:DD:EE:61")
    assert asyncio.run(run2()).startswith("2026-11-03T17:") and sent == []


def test_heads_up_is_never_sent_late_and_off_by_default(client, monkeypatch):
    from app import alerts, config, db
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    _yard(client)
    sent = []

    async def fake_deliver(cfg, subject, text, title, body, **kw):
        sent.append(title)
        return True

    async def fake_sunset(self, coords, day, tz):
        return {"pct": 95, "verdict": "looks good", "line": "Sunset 5:34 PM, looks good: 95%"}
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_sunset", fake_sunset)
    window = int(datetime(2026, 11, 3, 17, 5, tzinfo=PHX).timestamp() * 1000)
    after = int(datetime(2026, 11, 3, 18, 30, tzinfo=PHX).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        await alerts.AlertMonitor()._maybe_send_sunset(cfg, devs, window)   # off
        client.put("/api/alerts", headers=H, json={"sunset_alert": True})
        cfg = await alerts.effective_config()
        await alerts.AlertMonitor()._maybe_send_sunset(cfg, devs, after)    # too late
    asyncio.run(run())
    assert sent == []


def test_quiet_hours_hold_the_heads_up(client, monkeypatch):
    """Watch tier: a sunset inside quiet hours is not pushed."""
    from app import alerts
    assert alerts.severity_of("sunset") == "watch"
    assert "watch" not in alerts._QUIET_HOURS_EXEMPT


def test_the_display_carries_the_next_sunset(monkeypatch):
    from app import station_display as sd
    from app import sunset as ss

    async def fake(lat, lon, sunset_at, tz, tz_name):
        return dict(SAMPLE)
    monkeypatch.setattr(ss, "outlook_at", fake)
    now = int(datetime(2026, 11, 3, 12, 0, tzinfo=PHX).timestamp() * 1000)
    chip = asyncio.run(sd.sunset_outlook((33.3, -111.9), now, PHX))
    assert chip == {"pct": 90, "verdict": "looks good", "line": SAMPLE["line"], "sunset_ms": 1}
    assert asyncio.run(sd.sunset_outlook(None, now, PHX)) is None

    async def none(lat, lon, sunset_at, tz, tz_name):
        return None
    monkeypatch.setattr(ss, "outlook_at", none)
    assert asyncio.run(sd.sunset_outlook((33.3, -111.9), now, PHX)) is None


def test_one_heads_up_per_place(client, monkeypatch):
    from app import alerts, config, db
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    _yard(client, "AA:BB:CC:DD:EE:61", "Davis", 33.3000, -111.9000)
    _yard(client, "AA:BB:CC:DD:EE:62", "Tempest", 33.3001, -111.9001)   # same yard
    client.put("/api/alerts", headers=H, json={"sunset_alert": True})
    sent = []

    async def fake_deliver(cfg, subject, text, title, body, **kw):
        sent.append((title, kw.get("mac")))
        return True

    async def fake_sunset(self, coords, day, tz):
        return {"pct": 90, "verdict": "looks good", "line": "Sunset 5:34 PM, looks good: 90%"}
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_sunset", fake_sunset)
    window = int(datetime(2026, 11, 3, 17, 5, tzinfo=PHX).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        await alerts.AlertMonitor()._maybe_send_sunset(cfg, devs, window)
    asyncio.run(run())
    assert len(sent) == 1 and sent[0][0] == "Good sunset tonight"


# ── review follow-ups (PR #136) ────────────────────────────────────────

def test_a_utc_server_scores_the_station_evening_not_the_one_before():
    """A UTC clock puts an Arizona yard's previous sunset at 00:35 on the
    date, before that date's sunrise; the outlook wants the evening after
    it, as the display's sun times do."""
    from app import almanac
    from app import sunset as ss
    utc = ZoneInfo("UTC")
    d = date(2026, 11, 2)
    raw = almanac.sunset(33.3, -111.9, d, utc)
    assert raw < almanac.sunrise(33.3, -111.9, d, utc)        # the trap
    ev = ss.evening_sunset(33.3, -111.9, d, utc)
    assert ev.astimezone(PHX).date() == d and ev.astimezone(PHX).hour == 17
    # A station-zone server is unchanged.
    assert ss.evening_sunset(33.3, -111.9, d, PHX) == almanac.sunset(33.3, -111.9, d, PHX)


def test_a_failed_forecast_is_not_refetched_on_every_call(monkeypatch):
    from app import sunset as ss
    calls = []

    async def down(url, params):
        calls.append(url)
        return None
    monkeypatch.setattr(ss, "_fetch_json", down)
    ss.reset_cache()
    assert asyncio.run(ss._hourly(33.3, -111.9, "America/Phoenix")) is None
    assert asyncio.run(ss._hourly(33.3, -111.9, "America/Phoenix")) is None
    assert len(calls) == 1
    # The cooldown ends, and a good answer clears it.
    key = next(iter(ss._FAILED))
    ss._FAILED[key] -= ss.FAIL_RETRY_S + 1

    async def up(url, params):
        calls.append(url)
        return {"hourly": {"time": []}}
    monkeypatch.setattr(ss, "_fetch_json", up)
    assert asyncio.run(ss._hourly(33.3, -111.9, "America/Phoenix")) is not None
    assert key not in ss._FAILED
    ss.reset_cache()


def test_the_next_sunset_is_never_skipped_on_a_utc_server():
    from datetime import timezone
    from app import sunset as ss
    utc = ZoneInfo("UTC")
    at_0010 = datetime(2026, 11, 3, 0, 10, tzinfo=timezone.utc)
    nxt = ss.next_sunset(33.3, -111.9, at_0010, utc)
    # Still 25 minutes to go on the Arizona evening of Nov 2.
    assert nxt.astimezone(PHX).date() == date(2026, 11, 2)
    assert 0 < (nxt - at_0010).total_seconds() < 3600


def test_the_heads_up_fires_late_in_the_window_on_a_utc_server(client, monkeypatch):
    from datetime import timezone
    from app import alerts, config, db
    monkeypatch.setattr(config.settings, "timezone", "UTC")
    _yard(client)
    client.put("/api/alerts", headers=H, json={"sunset_alert": True})
    sent = []

    async def fake_deliver(cfg, subject, text, title, body, **kw):
        sent.append(title)
        return True

    async def fake_sunset(self, coords, sunset_at, tz):
        return {"pct": 90, "verdict": "looks good", "line": "Sunset 5:34 PM, looks good: 90%"}
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_sunset", fake_sunset)
    at_0010 = int(datetime(2026, 11, 3, 0, 10, tzinfo=timezone.utc).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        mon = alerts.AlertMonitor()
        await mon._maybe_send_sunset(cfg, devs, at_0010)
        await mon._maybe_send_sunset(cfg, devs, at_0010 + 60_000)
    asyncio.run(run())
    assert sent == ["Good sunset tonight"]


def test_a_slow_air_quality_model_costs_only_its_factor(monkeypatch):
    from app import sunset as ss
    monkeypatch.setattr(ss, "AQ_TIMEOUT_S", 0.05)

    async def fetch(url, params):
        if "air-quality" in url:
            await asyncio.sleep(5)
        return {"hourly": {"time": []}}
    monkeypatch.setattr(ss, "_fetch_json", fetch)
    ss.reset_cache()
    out = asyncio.run(asyncio.wait_for(ss._hourly(33.3, -111.9, "America/Phoenix"), 1))
    assert out == {"hourly": {"time": []}, "aq": None}
    ss.reset_cache()
