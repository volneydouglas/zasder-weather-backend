"""Stored reports (2.1): the payload shapes, idempotent storage, the list
and detail routes, the preview, and the push route that deep-links into
one. The hosted relay SERVER's half of that route is pinned in
tests/test_relay.py, which is stripped from the public mirror alongside
app/relay.py. The wire these tests pin is the same wire the Swift decoders read
(bin/tests/test_report_wire_parity.py)."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import reports as rp  # noqa: E402

AUTH = {"Authorization": "Bearer test-api-token"}


class _Station:
    def __init__(self, **kw):
        base = dict(name="Crestview", tmax_f=104.2, tmin_f=78.9,
                    feels_max_f=112.4, rain_in=0.12, gust_mph=34.0,
                    humidity_lo=12.0, humidity_hi=55.0, uv_max=9.0)
        base.update(kw)
        for k, v in base.items():
            setattr(self, k, v)


class _Outlook:
    def __init__(self, hi_f=101.0, lo_f=80.0, precip_pct=30):
        self.hi_f, self.lo_f, self.precip_pct = hi_f, lo_f, precip_pct


class _Alert:
    def __init__(self, when="Wed 14:02", title="Wind Gust alert",
                 severity="watch"):
        self.when, self.title, self.severity = when, title, severity


class _Report:
    def __init__(self, stations=None, alerts=None, outlook=None):
        self.date_label = "Thursday, September 3"
        self.stations = stations if stations is not None else [_Station()]
        self.alerts = alerts or []
        self.outlook = outlook


class _Summary:
    def __init__(self, **kw):
        base = dict(started_ms=1_788_400_000_000, ended_ms=1_788_403_600_000,
                    total_in=0.97, peak_rate_in_hr=0.53, max_gust_mph=45.0,
                    min_tempf=69.0, max_tempf=90.0)
        base.update(kw)
        for k, v in base.items():
            setattr(self, k, v)


# ── payload shapes ──────────────────────────────────────────────────────

def test_morning_payload_is_json_and_keeps_absent_sensors_absent():
    """[[absent is not zero]] all the way to the wire: a station with no
    UV sensor sends null, never 0, and the payload must survive
    json.dumps with allow_nan=False (NaN is not JSON)."""
    p = rp.morning_payload(_Report(
        stations=[_Station(uv_max=None, rain_in=None)],
        outlook=_Outlook()))
    assert p["stations"][0]["uv_max"] is None
    assert p["stations"][0]["rain_in"] is None
    assert p["stations"][0]["feels_max_f"] == 112.4
    assert p["outlook"]["precip_pct"] == 30
    json.dumps(p, allow_nan=False)


def test_payload_scrubs_nan_and_infinity():
    """A sensor that produced NaN must not reach the phone as garbage."""
    p = rp.morning_payload(_Report(
        stations=[_Station(tmax_f=float("nan"), gust_mph=float("inf"))]))
    assert p["stations"][0]["tmax_f"] is None
    assert p["stations"][0]["gust_mph"] is None
    json.dumps(p, allow_nan=False)


def test_morning_payload_without_a_forecast_still_stands():
    p = rp.morning_payload(_Report(outlook=None))
    assert p["outlook"] is None
    json.dumps(p, allow_nan=False)


def test_morning_summary_reads_like_a_list_row():
    p = rp.morning_payload(_Report(outlook=_Outlook(),
                                   alerts=[_Alert(), _Alert()]))
    line = rp.morning_summary(p)
    assert "Yesterday 104/79" in line
    assert "today near 101" in line and "30% rain" in line
    assert "2 alerts" in line
    assert "—" not in line, "no em-dashes in copy (house rule)"


def test_morning_summary_survives_an_empty_report():
    assert rp.morning_summary({"stations": [], "alerts": []})


def test_storm_payload_carries_the_close_capture():
    p = rp.storm_payload("Chaucer Drive", _Summary(),
                         {"pre_tempf": 100.4, "post_tempf": 79.5,
                          "temp_drop_f": 20.9, "pressure_change_inhg": 0.021,
                          "dew_change_f": -1.0})
    assert p["station"] == "Chaucer Drive"
    assert p["temp_drop_f"] == 20.9
    assert rp.storm_duration_minutes(p) == 60
    line = rp.storm_summary_line(p)
    assert "0.97 in" in line and "gust 45 mph" in line and "cooled 21" in line
    json.dumps(p, allow_nan=False)


def test_storm_payload_without_a_capture_is_all_nulls_not_zeros():
    p = rp.storm_payload("Yard", _Summary(), None)
    assert p["pre_tempf"] is None and p["temp_drop_f"] is None
    assert rp.storm_summary_line(p)


def test_dedupe_keys_are_stable_and_distinct():
    assert rp.morning_key("2026-09-04") == rp.morning_key("2026-09-04")
    assert rp.morning_key("2026-09-04") != rp.morning_key("2026-09-05")
    assert rp.storm_key("AA:BB", 5) != rp.storm_key("AA:BB", 6)
    assert rp.storm_key("AA:BB", 5) != rp.storm_key("CC:DD", 5)


# ── storage ─────────────────────────────────────────────────────────────

def test_insert_is_idempotent_on_the_dedupe_key(client):
    """The morning report's phone half retries on its own stamp (R15) and
    a failed storm summary re-attempts next tick. Neither may leave a
    second row — and the id must not move, or a push already delivered
    would deep-link to a report that no longer exists."""
    from app import db

    async def run():
        first = await db.insert_report(
            kind="morning", mac=None, ts_ms=1000, for_date="2026-09-03",
            title="Morning report", summary="one", payload={"v": 1},
            dedupe="morning:2026-09-03")
        second = await db.insert_report(
            kind="morning", mac=None, ts_ms=2000, for_date="2026-09-03",
            title="Morning report", summary="two", payload={"v": 2},
            dedupe="morning:2026-09-03")
        assert first == second, "the id a push already carries must not move"
        assert await db.count_reports() == 1
        row = await db.get_report(first)
        assert row["summary"] == "two" and row["payload"] == {"v": 2}

    asyncio.run(run())


def test_list_filters_by_kind_and_pages_by_time(client):
    from app import db

    async def run():
        for i in range(5):
            await db.insert_report(
                kind="storm" if i % 2 else "morning", mac="AA" if i % 2 else None,
                ts_ms=1000 + i, for_date="2026-09-03", title=f"r{i}",
                summary=None, payload={"i": i}, dedupe=f"k{i}")
        allr = await db.list_reports()
        assert [r["title"] for r in allr] == ["r4", "r3", "r2", "r1", "r0"]
        assert [r["title"] for r in await db.list_reports(kind="storm")] \
            == ["r3", "r1"]
        page = await db.list_reports(before_ms=1003)
        assert [r["title"] for r in page] == ["r2", "r1", "r0"]

    asyncio.run(run())


def test_storage_is_bounded(client, monkeypatch):
    """A self-hosted box must not grow this table forever."""
    from app import db
    monkeypatch.setattr(db, "_REPORT_MAX_ROWS", 3)

    async def run():
        for i in range(6):
            await db.insert_report(
                kind="morning", mac=None, ts_ms=1000 + i, for_date=None,
                title=f"r{i}", summary=None, payload={}, dedupe=f"k{i}")
        assert await db.count_reports() == 3
        assert [r["title"] for r in await db.list_reports()] \
            == ["r5", "r4", "r3"]

    asyncio.run(run())


def test_an_unparseable_payload_does_not_break_the_detail_route(client):
    """A row we cannot decode still answers with its title; it must not
    500 the Reports pane."""
    from app import db

    async def run():
        rid = await db.insert_report(
            kind="morning", mac=None, ts_ms=1, for_date=None, title="t",
            summary="s", payload={}, dedupe="k")
        async with db.connect() as conn:
            await conn.execute(
                "UPDATE reports SET payload_json = ? WHERE id = ?",
                ("{not json", rid))
            await conn.commit()
        row = await db.get_report(rid)
        assert row["payload"] is None and row["title"] == "t"

    asyncio.run(run())


# ── routes ──────────────────────────────────────────────────────────────

def test_routes_are_token_gated(client):
    for path in ("/api/reports", "/api/reports/1",
                 "/api/reports/morning/preview"):
        assert client.get(path).status_code == 401, path


def test_list_and_detail_round_trip(client):
    from app import db

    asyncio.run(db.insert_report(
        kind="storm", mac="5D:5D:05:00:00:01", ts_ms=1788400000000,
        for_date="2026-09-03", title="Chaucer Drive Storm Summary",
        summary="0.97 in", payload={"station": "Chaucer Drive",
                                    "total_in": 0.97},
        dedupe="storm:x:1"))
    body = client.get("/api/reports", headers=AUTH).json()
    assert len(body["reports"]) == 1
    row = body["reports"][0]
    assert row["kind"] == "storm" and row["summary"] == "0.97 in"
    assert "payload" not in row, "the list must not carry payloads"
    assert set(body["kinds"]) == set(rp.KINDS)

    detail = client.get(f"/api/reports/{row['id']}", headers=AUTH).json()
    assert detail["report"]["payload"]["total_in"] == 0.97


def test_detail_404s_for_an_unknown_report(client):
    assert client.get("/api/reports/9999", headers=AUTH).status_code == 404


def test_list_rejects_an_unknown_kind(client):
    r = client.get("/api/reports?kind=banana", headers=AUTH)
    assert r.status_code == 400


def test_preview_404s_before_there_is_anything_to_report(client):
    """A brand-new server with no rollups says so, rather than serving an
    empty report that looks like a broken one."""
    assert client.get("/api/reports/morning/preview",
                      headers=AUTH).status_code == 404


# ── the push route that deep-links into a report ────────────────────────

def test_route_validation_refuses_anything_but_a_route():
    """A push may say WHERE to land, never WHAT to render. Only a short
    verb and one id segment survive; everything else degrades to a push
    that opens the app normally."""
    from app import apns
    assert apns.valid_route("report/12") == "report/12"
    assert apns.valid_route("reports") == "reports"
    for bad in ("../etc/passwd", "REPORT/1", "report/12?x=1", "http://x",
                "report/" + "a" * 100, "a" * 40, "", None, 12,
                "report/1 2", "report//1"):
        assert apns.valid_route(bad) is None, bad


def test_build_payload_puts_the_route_beside_aps_not_inside_it():
    """Custom keys ride alongside `aps`, which is where iOS hands them
    back as userInfo. Inside aps they would be ignored, or worse."""
    from app import apns
    p = apns.build_payload("t", "b", route="report/9")
    assert p["route"] == "report/9"
    assert "route" not in p["aps"]
    assert "route" not in apns.build_payload("t", "b")
    assert "route" not in apns.build_payload("t", "b", route="../nope")


def test_relay_client_omits_the_route_when_there_is_none(monkeypatch):
    """A pre-2.1 relay uses extra=forbid, so a push with no route must be
    byte-identical to what 2.0 sent."""
    import httpx
    from app import apns
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"sent": 1, "dead": [], "failed": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            seen.setdefault("bodies", []).append(json)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(apns.settings, "apns_env", "production")

    asyncio.run(apns._push_via_relay(["t"], "ti", "bo", "http://r", "k"))
    assert "route" not in seen["bodies"][0]

    seen.clear()
    asyncio.run(apns._push_via_relay(["t"], "ti", "bo", "http://r", "k",
                                     route="report/3"))
    assert seen["bodies"][0]["route"] == "report/3"


def test_an_older_relay_that_rejects_the_route_still_delivers(monkeypatch):
    """422 from a relay that has never heard of `route` must cost the deep
    link, not the notification."""
    import httpx
    from app import apns
    bodies = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "unknown field"

        def json(self):
            return {"sent": 1, "dead": [], "failed": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            bodies.append(dict(json))
            return _Resp(422 if "route" in json else 200)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(apns.settings, "apns_env", "production")
    res = asyncio.run(apns._push_via_relay(["t"], "ti", "bo", "http://r", "k",
                                           route="report/3"))
    assert len(bodies) == 2, "one retry, without the field it rejected"
    assert "route" not in bodies[1]
    assert res["sent"] == 1


# ── end to end: a report that was SENT is a report you can open ─────────

def _digest_cfg():
    from types import SimpleNamespace
    return SimpleNamespace(enabled=True, recipients=["v@z.com"],
                           digest_hour=7, digest_minute=0, smtp_host="h",
                           smtp_port=465, smtp_username=None,
                           smtp_password=None, smtp_from=None,
                           smtp_tls=False, smtp_ssl=True, email_scope="all")


def test_the_morning_report_it_sent_is_the_report_you_can_open(client,
                                                               monkeypatch):
    """The whole point of 2.1: the 7am job stores the same object it
    rendered the mail from, the push carries that row's id, and the
    detail route serves it back. A retry must not stack a second row."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    import app.alerts as al
    from app import apns, db, insights
    from app.config import settings as _settings

    monkeypatch.setattr(al, "_send_sync",
                        lambda subject, body, to, cfg, html=None: None)
    la_sends, pushes = [], []

    async def fake_la(payload, title, body, activity="rain"):
        la_sends.append((activity, payload))
        return {"sent": 1, "dead": [], "failed": 0}

    async def fake_push(title, body, interruption_level=None, route=None, **kw):
        pushes.append({"title": title, "route": route})
        return {"sent": 1}

    async def configured():
        return True

    async def no_outlook(*a, **k):
        return None

    monkeypatch.setattr(apns, "send_live_activity_start", fake_la)
    monkeypatch.setattr(apns, "send_to_all", fake_push)
    monkeypatch.setattr(apns, "push_configured", configured)

    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)

    async def run():
        mac = "AA:BB:CC:DD:EE:99"
        await db.upsert_device(mac, {"lastData": {"dateutc": now_ms,
                                                  "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 104.0, "feelsLike": 112.0, "windgustmph": 34.0,
             "dailyrainin": 0.12},
            {"dateutc": int(yday.replace(hour=5).timestamp() * 1000),
             "tempf": 79.0, "feelsLike": 79.0, "windgustmph": 4.0,
             "dailyrainin": 0.0},
        ])
        await insights.rebuild(mac)
        devices = await db.list_devices()
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(_digest_cfg(), devices, now_ms)
        # Force the email half to retry; the row must not double.
        await db.set_kv("alerts.digest.last_ms", None)
        await mon._maybe_send_digest(_digest_cfg(), devices, now_ms + 60_000)
        return await db.list_reports()

    rows = asyncio.run(run())
    assert len(rows) == 1, "one report per morning, whatever retries"
    row = rows[0]
    assert row["kind"] == "morning"

    # The push says where to land, and it lands on the row that was stored.
    assert pushes and pushes[0]["route"] == f"report/{row['id']}"
    assert la_sends[0][1]["aps"]["content-state"]["reportId"] == row["id"]

    detail = client.get(f"/api/reports/{row['id']}", headers=AUTH).json()
    payload = detail["report"]["payload"]
    assert payload["stations"][0]["tmax_f"] == 104.0
    assert payload["stations"][0]["feels_max_f"] == 112.0, \
        "Doren's feels-like reaches the stored report"
    assert "104" in detail["report"]["summary"]


def test_a_server_that_cannot_store_still_sends_the_report(client,
                                                           monkeypatch):
    """Storage is best-effort by contract: losing the row costs the deep
    link, never the reader's morning report."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    import app.alerts as al
    from app import apns, db, insights
    from app.config import settings as _settings

    sent = []
    monkeypatch.setattr(
        al, "_send_sync",
        lambda subject, body, to, cfg, html=None: sent.append(subject))

    async def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "insert_report", boom)

    pushes = []

    async def fake_push(title, body, interruption_level=None, route=None, **kw):
        pushes.append(route)
        return {"sent": 1}

    async def configured():
        return True

    async def fake_la(payload, title, body, activity="rain"):
        return {"sent": 1, "dead": [], "failed": 0}
    monkeypatch.setattr(apns, "send_to_all", fake_push)
    monkeypatch.setattr(apns, "push_configured", configured)
    monkeypatch.setattr(apns, "send_live_activity_start", fake_la)

    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)

    async def run():
        mac = "AA:BB:CC:DD:EE:97"
        await db.upsert_device(mac, {"lastData": {"dateutc": now_ms,
                                                  "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 104.0},
            {"dateutc": int(yday.replace(hour=5).timestamp() * 1000),
             "tempf": 79.0},
        ])
        await insights.rebuild(mac)
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(_digest_cfg(), await db.list_devices(),
                                     now_ms)

    asyncio.run(run())
    assert sent, "the email went out even though storage failed"
    assert pushes == [None], "no id to link to, so no route"


def test_the_preview_is_the_same_report_the_job_would_send(client,
                                                           monkeypatch):
    """'Run it now' has to produce the report, not a lookalike."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from app import db, insights
    from app.config import settings as _settings

    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz)
    yday = now - _dt.timedelta(days=1)

    async def setup():
        mac = "AA:BB:CC:DD:EE:96"
        await db.upsert_device(mac, {"lastData": {
            "dateutc": int(now.timestamp() * 1000), "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 101.0, "windgustmph": 22.0},
            {"dateutc": int(yday.replace(hour=5).timestamp() * 1000),
             "tempf": 71.0, "windgustmph": 3.0},
        ])
        await insights.rebuild(mac)

    asyncio.run(setup())
    body = client.get("/api/reports/morning/preview", headers=AUTH).json()
    rep = body["report"]
    assert rep["preview"] is True and rep["id"] is None
    assert rep["payload"]["stations"][0]["tmax_f"] == 101.0
    assert asyncio.run(db.count_reports()) == 0, \
        "a preview must never store or send anything"


def test_a_storm_summary_becomes_a_report(client, monkeypatch):
    """Every storm the server reported is a report you can reopen, with
    the close-capture numbers the share card wants."""
    from app import db, storm

    async def run():
        mac = "5D:5D:05:00:00:01"
        summary = storm.StormSummary(
            started_ms=1_788_400_000_000, ended_ms=1_788_403_600_000,
            total_in=0.97, peak_rate_in_hr=0.53, max_gust_mph=45.0,
            min_tempf=69.0, max_tempf=90.0)
        payload = rp.storm_payload("Chaucer Drive", summary,
                                   {"pre_tempf": 100.4, "post_tempf": 79.5,
                                    "temp_drop_f": 20.9,
                                    "pressure_change_inhg": 0.021,
                                    "dew_change_f": -1.0})
        rid = await db.insert_report(
            kind=rp.KIND_STORM, mac=mac, ts_ms=1_788_403_600_000,
            for_date="2026-09-03", title="Chaucer Drive Storm Summary",
            summary=rp.storm_summary_line(payload), payload=payload,
            dedupe=rp.storm_key(mac, summary.started_ms))
        # The same storm re-sent (a retry) must not make a second row.
        await db.insert_report(
            kind=rp.KIND_STORM, mac=mac, ts_ms=1_788_403_600_001,
            for_date="2026-09-03", title="Chaucer Drive Storm Summary",
            summary="again", payload=payload,
            dedupe=rp.storm_key(mac, summary.started_ms))
        assert await db.count_reports() == 1
        return rid

    rid = asyncio.run(run())
    detail = client.get(f"/api/reports/{rid}", headers=AUTH).json()["report"]
    assert detail["payload"]["temp_drop_f"] == 20.9
    assert detail["mac"] == "5D:5D:05:00:00:01"
