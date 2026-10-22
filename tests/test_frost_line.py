"""2.7.1: a frost line in the morning report and the outlook.

When the owner's forecast (the one the report already reads, with its
Open-Meteo fallback) puts the coming night's low at 34 °F or under, the
report says so in one plain sentence: "Frost possible tonight, low 33F."
or, at 32 °F and under, "Freeze tonight, low 30F.". It rides in the
`narrative` text every shipped app, email and push already shows, so no
app needs a change, and it is °F as stored, like the provider prose it
sits beside.

"The coming night" is the night after the report is sent: for an evening
outlook about tomorrow, tonight's low is TOMORROW's calendar low (the
small hours); for a morning report or a morning outlook about today, it
is also tomorrow's row, never today's, whose low was this morning.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

H = {"Authorization": "Bearer test-api-token"}
TZ = ZoneInfo("America/New_York")
EVENING = datetime(2026, 10, 17, 20, 0, tzinfo=TZ)
MORNING = datetime(2026, 10, 17, 7, 0, tzinfo=TZ)


def _daily(today_lo, tomorrow_lo):
    return {"time": ["2026-10-17", "2026-10-18"],
            "weather_code": [0, 0],
            "temperature_2m_max": [55.0, 57.0],
            "temperature_2m_min": [today_lo, tomorrow_lo],
            "precipitation_probability_max": [0, 0],
            "wind_speed_10m_max": [5.0, 4.0]}


# ── the sentence ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("low, line", [
    (None, None),
    (float("nan"), None),
    (40.0, None),
    (34.6, None),
    (34.5, "Frost possible tonight, low 34F."),
    (34.0, "Frost possible tonight, low 34F."),
    (33.2, "Frost possible tonight, low 33F."),
    (32.6, "Frost possible tonight, low 33F."),
    (32.4, "Freeze tonight, low 32F."),
    (32.0, "Freeze tonight, low 32F."),
    (30.4, "Freeze tonight, low 30F."),
])
def test_the_frost_line_bands(low, line):
    """Banded on the whole degree the sentence prints, so the words and
    the number always agree: never "Frost possible tonight, low 32F"."""
    from app import outlook as ol
    assert ol.frost_line(low) == line


# ── the outlook ──────────────────────────────────────────────────────────

def test_the_evening_outlook_reads_tonights_low_from_tomorrows_row():
    from app import outlook as ol
    r = ol.build(_daily(45.0, 33.0), narrative=None, when="tomorrow",
                 now_local=EVENING, source=ol.SOURCE_OPEN_METEO)
    assert r.frost_line == "Frost possible tonight, low 33F."
    assert r.narrative == "Frost possible tonight, low 33F."
    assert "Frost possible tonight, low 33F." in ol.text(r)
    assert "Frost possible tonight, low 33F." in ol.push_text(r)[1]
    assert "Frost possible tonight, low 33F." in ol.build_html(r)
    # The stored payload carries it in the field the apps already show.
    from app import reports as rp
    assert rp.outlook_payload(r)["narrative"] == "Frost possible tonight, low 33F."


def test_the_line_leads_the_providers_own_prose():
    from app import outlook as ol
    r = ol.build(_daily(45.0, 30.0),
                 narrative=[{"name": "Tomorrow", "text": "Sunny. High 57F."}],
                 when="tomorrow", now_local=EVENING, source=ol.SOURCE_TWC)
    assert r.narrative == "Freeze tonight, low 30F. Sunny. High 57F."


@pytest.mark.parametrize("tomorrow_lo", [34.6, 41.3])
def test_a_mild_night_gets_no_line(tomorrow_lo):
    from app import outlook as ol
    r = ol.build(_daily(28.0, tomorrow_lo), narrative=None, when="tomorrow",
                 now_local=EVENING, source=ol.SOURCE_OPEN_METEO)
    assert r.frost_line is None and r.narrative is None
    assert "tonight" not in ol.text(r)


def test_a_morning_outlook_reads_the_night_after_today():
    """This morning's 28 °F is over; tonight's 33 °F is the one to act on."""
    from app import outlook as ol
    r = ol.build(_daily(28.0, 33.0), narrative=None, when="today",
                 now_local=MORNING, source=ol.SOURCE_OPEN_METEO)
    assert r.frost_line == "Frost possible tonight, low 33F."
    mild = ol.build(_daily(28.0, 40.0), narrative=None, when="today",
                    now_local=MORNING, source=ol.SOURCE_OPEN_METEO)
    assert mild.frost_line is None and mild.narrative is None


def test_no_forecast_no_line():
    from app import outlook as ol
    for daily in ({}, {"time": ["2026-10-17", "2026-10-18"]},
                  {"time": ["2026-10-17"], "temperature_2m_min": [20.0]}):
        r = ol.build(daily, narrative=None, when="today", now_local=MORNING,
                     source=ol.SOURCE_OPEN_METEO)
        assert r.frost_line is None and r.narrative is None


def test_a_provider_without_dates_uses_day_order():
    from app import outlook as ol
    daily = {"temperature_2m_min": [28.0, 32.0]}
    r = ol.build(daily, narrative=None, when="today", now_local=MORNING,
                 source=ol.SOURCE_OPEN_METEO)
    assert r.frost_line == "Freeze tonight, low 32F."


def test_the_evening_outlook_delivers_the_line(client, monkeypatch):
    """End to end through the 22:00 slot: the stored report, the push and
    the email all carry it, from the owner's chosen source."""
    from app import alerts, apns, db
    from app import config
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:71", "name": "Davis",
                                 "coords": {"lat": 40.32, "lon": -79.70}},
                      "timestamp_utc": "2026-10-18T01:00:00Z",
                      "outdoor": {"tempf": 44}})
    client.put("/api/alerts", headers=H,
               json={"outlook_hour": 22, "outlook_minute": 0,
                     "outlook_source": "twc"})
    pushes, mails, asked = [], [], []

    async def fake_configured():
        return True

    async def fake_send_to_all(title, body, route=None, **kw):
        pushes.append(body)
        return {"sent": 1, "total": 1}

    async def fake_fetch(self, coords, source, wu_key, tz_name):
        asked.append(source)
        return (_daily(45.0, 31.0),
                [{"name": "Tomorrow", "text": "Sunny. High 57F."}],
                "twc", None)

    def fake_send(subject, text, recipients, cfg, html=None):
        mails.append((text, html))
    monkeypatch.setattr(apns, "push_configured", fake_configured)
    monkeypatch.setattr(apns, "send_to_all", fake_send_to_all)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fake_fetch)
    monkeypatch.setattr(alerts, "_send_sync", fake_send)
    monkeypatch.setattr(config.settings, "timezone", "America/New_York")
    at_2200 = int(datetime(2026, 10, 17, 22, 0, tzinfo=TZ).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        cfg.enabled, cfg.recipients = True, ["doren@example.com"]
        await alerts.AlertMonitor()._maybe_send_outlook(
            cfg, await db.list_devices(), at_2200)
        rows = await db.list_reports(kind="outlook")
        return await db.get_report(rows[0]["id"])
    row = asyncio.run(run())
    line = "Freeze tonight, low 31F."
    assert asked == ["twc"]
    assert row["payload"]["narrative"] == line + " Sunny. High 57F."
    assert len(pushes) == 1 and line in pushes[0]
    assert len(mails) == 1 and line in mails[0][0] and line in mails[0][1]


# ── the morning report ───────────────────────────────────────────────────

def _morning(monkeypatch, daily, narrative=None, source="twc"):
    from app import alerts as al
    from app import db
    from app import outlook as ol

    async def fake_fetch(lat, lon, *, source, wu_key, tz_name):
        return daily, narrative or [], source, None
    monkeypatch.setattr(ol, "fetch_daily", fake_fetch)

    async def fake_key():
        return "k"
    import app.main as main
    monkeypatch.setattr(main, "effective_wu_key", fake_key)

    async def go():
        await db.set_alert_prefs(outlook_source=source)
        return await al._morning_outlook((40.32, -79.70), MORNING)
    return asyncio.run(go())


def test_the_morning_report_says_tonight_in_every_twin(client, monkeypatch):
    from app import digest as dg
    from app import reports as rp
    o = _morning(monkeypatch, _daily(41.0, 33.0),
                 [{"name": "Today", "text": "Sunny. High 55F."}])
    line = "Frost possible tonight, low 33F."
    assert o.frost_line == line
    assert o.narrative == line + " Sunny. High 55F."
    r = dg.Report(date_label="Saturday, October 17", stations=[
        dg.StationDay(name="Davis", tmax_f=52.0, tmin_f=30.6, feels_max_f=None,
                      rain_in=0.0, gust_mph=8.0, humidity_lo=40.0,
                      humidity_hi=95.0, uv_max=3.0)], alerts=[], outlook=o)
    assert line in dg.build_text(r)
    assert line in dg.build_html(r)
    assert line in dg.push_text(r)[1].splitlines()
    assert rp.morning_payload(r)["outlook"]["narrative"].startswith(line)


def test_the_morning_report_reads_tonight_not_this_morning(client, monkeypatch):
    from app import digest as dg
    o = _morning(monkeypatch, _daily(29.0, 38.0), source="open-meteo")
    assert o.frost_line is None and o.narrative is None
    r = dg.Report(date_label="Saturday, October 17", stations=[], alerts=[], outlook=o)
    assert "tonight" not in dg.push_text(r)[1]
    assert "tonight" not in dg.build_text(r)


def test_a_morning_report_without_a_forecast_has_no_line():
    from app import digest as dg
    r = dg.Report(date_label="Saturday, October 17", stations=[], alerts=[],
                  outlook=None)
    assert "tonight" not in dg.push_text(r)[1]
    assert "tonight" not in dg.build_text(r)
