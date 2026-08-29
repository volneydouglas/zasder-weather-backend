"""The morning weather report (1.9): the pure HTML/text builders in
app/digest.py plus the end-to-end shape through _maybe_send_digest —
station numbers off the rollups, the anchor's headline, the outlook,
and the quiet-day report."""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import digest as dg  # noqa: E402


def _station(**over):
    base = dict(name="Crestview", tmax_f=104.2, tmin_f=78.9, rain_in=0.12,
                gust_mph=34.0, humidity_lo=12.0, humidity_hi=55.0,
                uv_max=9.0)
    base.update(over)
    return dg.StationDay(**base)


def test_headline_reads_like_an_anchor():
    r = dg.Report(date_label="Thursday, August 28",
                  stations=[_station()],
                  alerts=[dg.AlertLine("Wed 14:02", "Wind Gust alert",
                                       "watch")])
    h = dg.headline(r)
    assert "triple-digit" in h and "104" in h
    assert '0.12" of rain' in h
    assert "gusts to 34 mph" in h
    assert "One alert" in h


def test_headline_quiet_day():
    r = dg.Report(date_label="Thu",
                  stations=[_station(tmax_f=None, tmin_f=None, rain_in=None,
                                     gust_mph=None)])
    assert dg.headline(r) == "A quiet day at Crestview."


def test_html_carries_the_numbers_and_escapes_hostile_names():
    # Station names are DEVICE input — one hostile name must not inject
    # markup into every recipient's inbox.
    evil = _station(name='<img src=x onerror=alert(1)>')
    r = dg.Report(date_label="Thursday, August 28", stations=[evil],
                  alerts=[dg.AlertLine("Wed 14:02",
                                       "<script>bad</script> alert",
                                       "warning")],
                  outlook=dg.Outlook(hi_f=106.0, lo_f=81.0, precip_pct=20))
    html_body = dg.build_html(r)
    assert "<script>" not in html_body
    assert "<img src=x" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "104" in html_body and "79" in html_body      # hero hi/lo
    assert "0.12" in html_body                            # rain tile
    assert "34 mph" in html_body                          # gust tile
    assert "106" in html_body and "20%" in html_body      # outlook
    # House copy rule: no em-dashes anywhere user-facing.
    assert "—" not in html_body
    # Email safety: no external resources.
    assert "http://" not in html_body and "https://" not in html_body


def test_quiet_day_is_a_good_report_too():
    r = dg.Report(date_label="Thu", stations=[_station()])
    html_body = dg.build_html(r)
    assert "Nothing fired" in html_body
    text = dg.build_text(r)
    assert "nothing fired" in text
    assert "high 104F" in text and 'rain 0.12"' in text


def test_absent_sensors_have_no_tiles():
    # Absent is not zero: a station with no rain gauge gets NO rain tile.
    r = dg.Report(date_label="Thu",
                  stations=[_station(rain_in=None, gust_mph=None,
                                     uv_max=None, humidity_lo=None,
                                     humidity_hi=None)])
    html_body = dg.build_html(r)
    assert "RAIN" not in html_body
    assert "PEAK GUST" not in html_body
    assert "PEAK UV" not in html_body


def test_report_pulls_rollups_and_sends_html(client, monkeypatch):
    """End to end: yesterday's rollup row becomes the station block even
    with ZERO alerts — the 1.8 skip-when-quiet behavior is gone for
    stations with data."""
    import datetime as _dt
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    import app.alerts as al
    from app import db, insights
    from app.config import settings as _settings

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
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)

    async def run():
        mac = "AA:BB:CC:DD:EE:99"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 104.0, "humidity": 12.0, "windgustmph": 34.0,
             "dailyrainin": 0.12},
            {"dateutc": int(yday.replace(hour=5).timestamp() * 1000),
             "tempf": 79.0, "humidity": 55.0, "windgustmph": 4.0,
             "dailyrainin": 0.0},
        ])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        await al.AlertMonitor()._maybe_send_digest(cfg, devices, now_ms)

    asyncio.run(run())
    assert len(sent) == 1, "a station with rollup data must report daily"
    subject, body, html_body = sent[0]
    assert "Morning report" in subject
    assert "104" in html_body and "79" in html_body
    assert "Nothing fired" in html_body


def test_push_text_two_lines():
    r = dg.Report(date_label="Thu", stations=[_station()],
                  alerts=[dg.AlertLine("Wed 14:02", "x", "watch")],
                  outlook=dg.Outlook(hi_f=106.0, lo_f=81.0, precip_pct=20))
    title, body = dg.push_text(r)
    assert title == "Morning report · Crestview"
    assert "Yesterday: Hi 104° · Lo 79°" in body
    assert '0.12" rain' in body and "gust 34 mph" in body
    assert "Today: near 106° · 20% rain chance" in body
    assert "1 alert in the log." in body


def test_phone_half_sends_live_activity_and_push_once(client, monkeypatch):
    """The lock-screen half (1.9): one Live Activity start + one compact
    push per local day — with its OWN stamp, so an email retry never
    stacks a second morning card."""
    import datetime as _dt
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    import app.alerts as al
    from app import apns, db, insights
    from app.config import settings as _settings

    monkeypatch.setattr(
        al, "_send_sync",
        lambda subject, body, to, cfg, html=None: None)
    la_sends, pushes = [], []

    async def fake_la(payload, title, body, activity="rain"):
        la_sends.append((activity, payload))
        return {"sent": 1, "dead": [], "failed": 0}

    async def fake_push(title, body, interruption_level=None):
        pushes.append((title, body))
        return {"sent": 1}

    async def configured():
        return True
    monkeypatch.setattr(apns, "send_live_activity_start", fake_la)
    monkeypatch.setattr(apns, "send_to_all", fake_push)
    monkeypatch.setattr(apns, "push_configured", configured)

    cfg = SimpleNamespace(enabled=True, recipients=["v@z.com"],
                          digest_hour=7, smtp_host="h", smtp_port=465,
                          smtp_username=None, smtp_password=None,
                          smtp_from=None, smtp_tls=False, smtp_ssl=True,
                          email_scope="all")
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)

    async def run():
        mac = "AA:BB:CC:DD:EE:98"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 104.0, "windgustmph": 34.0, "dailyrainin": 0.12},
            {"dateutc": int(yday.replace(hour=5).timestamp() * 1000),
             "tempf": 79.0, "windgustmph": 4.0, "dailyrainin": 0.0},
        ])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(cfg, devices, now_ms)
        # The email path's daily stamp blocks a second FULL run; wipe it
        # to prove the PHONE stamp alone prevents a duplicate card.
        await db.set_kv("alerts.digest.last_ms", None)
        await mon._maybe_send_digest(cfg, devices, now_ms + 60_000)

    asyncio.run(run())
    assert len(la_sends) == 1, "one morning card per day, ever"
    activity, payload = la_sends[0]
    assert activity == "morning"
    aps = payload["aps"]
    assert aps["attributes-type"] == "MorningReportActivityAttributes"
    assert aps["content-state"]["hiF"] == 104.0
    assert aps["content-state"]["loF"] == 79.0
    assert aps["dismissal-date"] > aps["timestamp"]
    assert len(pushes) == 1
    assert "Yesterday: Hi 104°" in pushes[0][1]


def test_push_only_install_still_gets_the_morning_report(client, monkeypatch):
    """digest_hour set + NO smtp: the phone half still runs (1.9 — the
    old gate required email and starved push-only installs)."""
    import datetime as _dt
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    import app.alerts as al
    from app import apns, db, insights
    from app.config import settings as _settings

    la_sends = []

    async def fake_la(payload, title, body, activity="rain"):
        la_sends.append(activity)
        return {"sent": 1, "dead": [], "failed": 0}

    async def not_configured():
        return False
    monkeypatch.setattr(apns, "send_live_activity_start", fake_la)
    monkeypatch.setattr(apns, "push_configured", not_configured)
    cfg = SimpleNamespace(enabled=False, recipients=[], digest_hour=7,
                          smtp_host=None, smtp_port=465, smtp_username=None,
                          smtp_password=None, smtp_from=None,
                          smtp_tls=False, smtp_ssl=True, email_scope="all")
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)

    async def run():
        mac = "AA:BB:CC:DD:EE:97"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 101.0}])
        await insights.rebuild(mac)
        await al.AlertMonitor()._maybe_send_digest(
            cfg, await db.list_devices(), now_ms)

    asyncio.run(run())
    assert la_sends == ["morning"]


# ── R14 additions ───────────────────────────────────────────────────────

def test_alert_dots_carry_their_own_severity():
    """R14 finding 2: alerts_since dropped the severity column, so every
    dot rendered watch-blue. Each tier must paint its own color, and an
    unknown future tier degrades to the dim grey, never a KeyError."""
    r = dg.Report(date_label="Thu", stations=[_station()], alerts=[
        dg.AlertLine("Wed 14:02", "Gust", "warning"),
        dg.AlertLine("Wed 15:02", "Heat", "major"),
        dg.AlertLine("Wed 16:02", "Wind", "watch"),
        dg.AlertLine("Wed 17:02", "Note", "info"),
        dg.AlertLine("Wed 18:02", "Future", "cataclysmic"),
    ])
    html_body = dg.build_html(r)
    for sev, color in dg._SEV.items():
        assert color in html_body, f"{sev} dot lost its color"
    # The unknown tier's dot is the dim fallback, and the count is intact.
    assert html_body.count("&#9679;") >= 5

def test_station_name_uppercases_before_escaping():
    """R14 finding 3: escape-then-upper turned "&amp;" into the literal
    entity-corrupting "&AMP;". Upper-first keeps the entity lowercase."""
    r = dg.Report(date_label="Thu",
                  stations=[_station(name="Bed & Breakfast <PWS>")])
    html_body = dg.build_html(r)
    assert "BED &amp; BREAKFAST" in html_body
    assert "&AMP;" not in html_body
    assert "<PWS>" not in html_body        # brackets still escaped


def test_text_report_matches_the_html_facts():
    """R14: build_text omitted humidity/UV and a lo_f-only outlook —
    text-only clients got a subset of the report."""
    r = dg.Report(date_label="Thu", stations=[_station()],
                  outlook=dg.Outlook(hi_f=None, lo_f=58.0, precip_pct=None))
    text = dg.build_text(r)
    assert "humidity 12-55%" in text
    assert "UV 9" in text
    # The lo-only outlook must survive the gate.
    assert "Today: low around 58F" in text


def test_outlook_fetch_failure_does_not_kill_the_report(client, monkeypatch):
    """R14: the Open-Meteo peek is best-effort — a network failure inside
    it must degrade to no-outlook, never abort the send."""
    import datetime as _dt
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    import httpx

    import app.alerts as al
    from app import db, insights
    from app.config import settings as _settings

    sent = []
    monkeypatch.setattr(
        al, "_send_sync",
        lambda subject, body, to, cfg, html=None: sent.append(
            (subject, body, html)))
    monkeypatch.setattr(_settings, "forecast_lat", 33.3, raising=False)
    monkeypatch.setattr(_settings, "forecast_lon", -111.9, raising=False)

    def boom(*a, **kw):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "AsyncClient", boom)

    cfg = SimpleNamespace(enabled=True, recipients=["v@z.com"],
                          digest_hour=7, smtp_host="h", smtp_port=465,
                          smtp_username=None, smtp_password=None,
                          smtp_from=None, smtp_tls=False, smtp_ssl=True,
                          email_scope="all")
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)

    async def run():
        mac = "AA:BB:CC:DD:EE:98"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 101.0}])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        await al.AlertMonitor()._maybe_send_digest(cfg, devices, now_ms)

    asyncio.run(run())
    assert len(sent) == 1, "an outlook failure must not abort the report"
    assert "101" in sent[0][2]
    assert "OUTLOOK" not in sent[0][2]


def _phone_cfg():
    from types import SimpleNamespace
    return SimpleNamespace(enabled=True, recipients=["v@z.com"],
                           digest_hour=7, smtp_host="h", smtp_port=465,
                           smtp_username=None, smtp_password=None,
                           smtp_from=None, smtp_tls=False, smtp_ssl=True,
                           email_scope="all")


def _digest_now():
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from app.config import settings as _settings
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    return now, int(now.timestamp() * 1000)


def test_severity_flows_from_alert_log_to_the_dot(client, monkeypatch):
    """R15 finding 1: the R14 fix's test pinned only the rendering half.
    This drives the WHOLE path — a logged warning-tier alert must come
    out of alerts_since with its severity and paint the warning-red dot,
    so dropping the column from the SELECT fails a test again."""
    import app.alerts as al
    from app import db, digest as dg, insights

    sent = []
    monkeypatch.setattr(
        al, "_send_sync",
        lambda subject, body, to, cfg, html=None: sent.append(html))
    now, now_ms = _digest_now()

    async def run():
        mac = "AA:BB:CC:DD:EE:97"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        import datetime as _dt
        yday = now - _dt.timedelta(days=1)
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 101.0}])
        await insights.rebuild(mac)
        await db.log_alert(now_ms - 3_600_000, "rule", mac,
                           "Gust warning", "gusts 60", True,
                           severity="warning")
        devices = await db.list_devices()
        await al.AlertMonitor()._maybe_send_digest(_phone_cfg(), devices,
                                                   now_ms)

    asyncio.run(run())
    assert len(sent) == 1
    html_body = sent[0]
    assert "Gust warning" in html_body
    assert dg._SEV["warning"] in html_body, \
        "the logged severity did not reach the dot — alerts_since " \
        "dropped the column again?"


def test_quiet_day_stamps_both_halves_and_sends_nothing(client, monkeypatch):
    """Ledger carry: no rollups + no alerts = stamp-and-skip. Both stamps
    (R15): an unstamped phone half would re-gather rollups every tick for
    the rest of the day."""
    import app.alerts as al
    from app import apns, db

    sent, pushes = [], []
    monkeypatch.setattr(
        al, "_send_sync",
        lambda *a, **kw: sent.append(a))

    async def fake_push(title, body, interruption_level=None):
        pushes.append(title)
        return {"sent": 1}
    monkeypatch.setattr(apns, "send_to_all", fake_push)
    now, now_ms = _digest_now()

    async def run():
        await al.AlertMonitor()._maybe_send_digest(_phone_cfg(), [], now_ms)
        return (await db.get_kv("alerts.digest.last_ms"),
                await db.get_kv("alerts.digest.phone_day"))

    last, phone = asyncio.run(run())
    assert not sent and not pushes
    assert last == str(now_ms)
    assert phone == now.date().isoformat()


def test_email_send_failure_retries_next_tick(client, monkeypatch):
    """Ledger carry: SMTP raising must leave the email stamp unset so the
    next tick retries — and the phone half must not double-send on that
    retry (its own stamp holds)."""
    import app.alerts as al
    from app import apns, db, insights

    attempts = []

    def flaky_send(subject, body, to, cfg, html=None):
        attempts.append(subject)
        if len(attempts) == 1:
            raise OSError("smtp down")
    monkeypatch.setattr(al, "_send_sync", flaky_send)

    la_sends = []

    async def fake_la(payload, title, body, activity="rain"):
        la_sends.append(activity)
        return {"sent": 1, "dead": [], "failed": 0}

    async def configured():
        return True

    async def fake_push(title, body, interruption_level=None):
        return {"sent": 1}
    monkeypatch.setattr(apns, "send_live_activity_start", fake_la)
    monkeypatch.setattr(apns, "send_to_all", fake_push)
    monkeypatch.setattr(apns, "push_configured", configured)
    now, now_ms = _digest_now()

    async def run():
        import datetime as _dt
        mac = "AA:BB:CC:DD:EE:96"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        yday = now - _dt.timedelta(days=1)
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 99.0}])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms)
        assert await db.get_kv("alerts.digest.last_ms") is None, \
            "a failed email must not stamp the day closed"
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms + 60_000)
        return await db.get_kv("alerts.digest.last_ms")

    last = asyncio.run(run())
    assert len(attempts) == 2, "the second tick must retry the email"
    assert last is not None
    assert len(la_sends) == 1, "the email retry must not re-send the card"


def test_phone_total_failure_retries_even_after_email_succeeds(client,
                                                               monkeypatch):
    """R15 finding 2: a transient APNs outage must NOT stamp the phone
    day — and the email's own success must not block the retry (the old
    gate keyed everything off the email stamp)."""
    import app.alerts as al
    from app import apns, db, insights

    monkeypatch.setattr(al, "_send_sync", lambda *a, **kw: None)
    la_calls, pushes = [], []

    async def flaky_la(payload, title, body, activity="rain"):
        la_calls.append(activity)
        if len(la_calls) == 1:
            raise OSError("apns down")
        return {"sent": 1, "dead": [], "failed": 0}

    async def flaky_push(title, body, interruption_level=None):
        if not la_calls or len(la_calls) == 1 and not pushes:
            # first tick: the push fails too — total phone outage
            pushes.append(None)
            raise OSError("apns down")
        pushes.append((title, body))
        return {"sent": 1}

    async def configured():
        return True
    monkeypatch.setattr(apns, "send_live_activity_start", flaky_la)
    monkeypatch.setattr(apns, "send_to_all", flaky_push)
    monkeypatch.setattr(apns, "push_configured", configured)
    now, now_ms = _digest_now()

    async def run():
        import datetime as _dt
        mac = "AA:BB:CC:DD:EE:95"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        yday = now - _dt.timedelta(days=1)
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 98.0}])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms)
        stamped_after_failure = await db.get_kv("alerts.digest.phone_day")
        email_stamp = await db.get_kv("alerts.digest.last_ms")
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms + 60_000)
        return (stamped_after_failure, email_stamp,
                await db.get_kv("alerts.digest.phone_day"))

    failed_stamp, email_stamp, retried_stamp = asyncio.run(run())
    assert failed_stamp is None, \
        "a total delivery failure must not stamp the phone day"
    assert email_stamp is not None, "the email half succeeded"
    assert retried_stamp == now.date().isoformat()
    assert len(la_calls) == 2, "the phone half must retry next tick"


def test_alerts_only_day_still_pushes_without_a_card(client, monkeypatch):
    """R15 finding 3: station down overnight but alerts in the log — the
    phone gets the compact push (push_text's no-station form), just no
    Live Activity (which needs a station's numbers)."""
    import app.alerts as al
    from app import apns, db

    monkeypatch.setattr(al, "_send_sync", lambda *a, **kw: None)
    la_calls, pushes = [], []

    async def fake_la(payload, title, body, activity="rain"):
        la_calls.append(activity)
        return {"sent": 1, "dead": [], "failed": 0}

    async def fake_push(title, body, interruption_level=None):
        pushes.append((title, body))
        return {"sent": 1}

    async def configured():
        return True
    monkeypatch.setattr(apns, "send_live_activity_start", fake_la)
    monkeypatch.setattr(apns, "send_to_all", fake_push)
    monkeypatch.setattr(apns, "push_configured", configured)
    now, now_ms = _digest_now()

    async def run():
        await db.log_alert(now_ms - 3_600_000, "stale", "AA:BB",
                           "Station offline", None, True, severity="watch")
        await al.AlertMonitor()._maybe_send_digest(_phone_cfg(), [], now_ms)

    asyncio.run(run())
    assert la_calls == [], "no station numbers, no card"
    assert len(pushes) == 1
    assert pushes[0][0] == "Morning report"


def test_channel_less_phone_half_stamps_instead_of_retry_storming(
        client, monkeypatch):
    """R16 finding 2, the caller half: the no-channel no-op shape must
    count as no-targets, stamp the day, and never re-enter — the old
    failed:N shape re-ran the rollup gather every monitor tick."""
    import app.alerts as al
    from app import apns, db, insights

    monkeypatch.setattr(al, "_send_sync", lambda *a, **kw: None)
    la_calls = []

    async def noop_la(payload, title, body, activity="rain"):
        la_calls.append(activity)
        return {"sent": 0, "dead": [], "failed": 0}

    async def not_configured():
        return False
    monkeypatch.setattr(apns, "send_live_activity_start", noop_la)
    monkeypatch.setattr(apns, "push_configured", not_configured)
    now, now_ms = _digest_now()

    async def run():
        import datetime as _dt
        mac = "AA:BB:CC:DD:EE:94"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        yday = now - _dt.timedelta(days=1)
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 97.0}])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms)
        stamped = await db.get_kv("alerts.digest.phone_day")
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms + 60_000)
        return stamped

    stamped = asyncio.run(run())
    assert stamped == now.date().isoformat(), \
        "no channel must stamp, not retry-storm"
    assert len(la_calls) == 1, "the second tick must not re-enter"


def test_partial_delivery_stamps_and_forfeits_the_failed_half(
        client, monkeypatch):
    """R16 test 4 — the load-bearing trade-off, pinned: the Live Activity
    lands but the banner push fails. sent_any stamps the day (no stacked
    cards tomorrow morning), and the banner is deliberately forfeited
    rather than risking a duplicate card on retry."""
    import app.alerts as al
    from app import apns, db, insights

    monkeypatch.setattr(al, "_send_sync", lambda *a, **kw: None)
    la_calls, push_calls = [], []

    async def good_la(payload, title, body, activity="rain"):
        la_calls.append(activity)
        return {"sent": 1, "dead": [], "failed": 0}

    async def bad_push(title, body, interruption_level=None):
        push_calls.append(title)
        raise OSError("apns down")

    async def configured():
        return True
    monkeypatch.setattr(apns, "send_live_activity_start", good_la)
    monkeypatch.setattr(apns, "send_to_all", bad_push)
    monkeypatch.setattr(apns, "push_configured", configured)
    now, now_ms = _digest_now()

    async def run():
        import datetime as _dt
        mac = "AA:BB:CC:DD:EE:93"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": now_ms, "tempf": 90.0}})
        yday = now - _dt.timedelta(days=1)
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 96.0}])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms)
        stamped = await db.get_kv("alerts.digest.phone_day")
        await mon._maybe_send_digest(_phone_cfg(), devices, now_ms + 60_000)
        return stamped

    stamped = asyncio.run(run())
    assert stamped == now.date().isoformat()
    assert len(la_calls) == 1 and len(push_calls) == 1, \
        "one delivery landing must close the day — no retry"
