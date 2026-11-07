"""2.8 alerts and Live Activities (Doren, 10-17 to 10-29).

1. Live Activity update tokens carry their card's station, so one
   station's update or end no longer lands on every station's card.
2. The freeze card says "frost" for a 33-34 °F night (an optional key
   older apps ignore).
3. Tonight's low is corrected by the station's measured bias for the
   forecast source before it decides frost, on the card and in the reports.
4. Rules on one field that a single reading crosses together speak once.
5. The morning report, the outlook and the sky note each pick push,
   email or both.
6. Each smart watch can be muted on its own.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import sqlite3
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

H = {"Authorization": "Bearer test-api-token"}
MAC_A = "AA:BB:CC:00:28:01"
MAC_B = "AA:BB:CC:00:28:02"
MIN = 60_000


# ── 1. per-station update tokens ─────────────────────────────────────────

def _register(client, token, activity, mac=None, kind="update"):
    body = {"token": token, "kind": kind, "activity": activity, "env": "production"}
    if mac is not None:
        body["mac"] = mac
    r = client.post("/api/push/live-activity-token", json=body, headers=H)
    assert r.status_code == 200, r.text


def test_an_update_reaches_its_own_stations_cards_and_older_apps(client):
    from app import db
    _register(client, "tok-a" * 4, "freeze", MAC_A)
    _register(client, "tok-b" * 4, "freeze", MAC_B.lower())      # any spelling
    _register(client, "tok-old" * 4, "freeze")                    # a pre-2.8 app

    async def run():
        a = await db.list_live_activity_tokens("update", activity="freeze", mac=MAC_A)
        b = await db.list_live_activity_tokens("update", activity="freeze", mac=MAC_B)
        every = await db.list_live_activity_tokens("update", activity="freeze")
        return ({r["token"] for r in a}, {r["token"] for r in b},
                {r["token"] for r in every})
    a, b, every = asyncio.run(run())
    assert a == {"tok-a" * 4, "tok-old" * 4}
    assert b == {"tok-b" * 4, "tok-old" * 4}
    assert every == {"tok-a" * 4, "tok-b" * 4, "tok-old" * 4}


def test_a_start_token_ignores_a_mac(client):
    """Push-to-start tokens are app-wide: a mac on one is meaningless and
    must not narrow who a start reaches."""
    from app import db
    _register(client, "start-tok" * 3, "freeze", MAC_A, kind="start")

    async def run():
        return await db.list_live_activity_tokens("start", activity="freeze", mac=MAC_B)
    assert [r["token"] for r in asyncio.run(run())] == ["start-tok" * 3]


def test_a_bad_mac_is_refused(client):
    """A space, a '|' (ingest refuses it in a device id) or an overlong id."""
    for bad in ("not a mac!", "ST|01", "x" * 65, ""):
        r = client.post("/api/push/live-activity-token", headers=H, json={
            "token": "x" * 16, "kind": "update", "activity": "freeze",
            "mac": bad})
        assert r.status_code == 422, bad


def test_a_custom_station_id_scopes_its_card(client):
    """Custom ingest keeps a non-MAC id such as ST-00012345 as-is; its
    card's update token must register under it (CodeRabbit, PR #146)."""
    from app import db
    _register(client, "tok-st" * 4, "freeze", "ST-00012345")

    async def run():
        mine = await db.list_live_activity_tokens("update", activity="freeze",
                                                  mac="ST-00012345")
        other = await db.list_live_activity_tokens("update", activity="freeze",
                                                   mac=MAC_A)
        return {r["token"] for r in mine}, {r["token"] for r in other}
    mine, other = asyncio.run(run())
    assert "tok-st" * 4 in mine and "tok-st" * 4 not in other


def test_the_mac_column_reaches_a_database_that_already_exists(client):
    """The 1.8 table predates the column: CREATE IF NOT EXISTS never adds
    it, so only the ALTER path reaches an upgrading server, and a row
    written before the upgrade keeps fanning out to every card."""
    from app import db as dbmod
    from app.config import settings
    raw = sqlite3.connect(settings.database_path)
    # The table exactly as 2.7 created it, not 2.8's with a column dropped:
    # CI's SQLite cannot DROP the column that follows the comment above it
    # ("incomplete input"), and the old schema is what an upgrading server
    # really has.
    raw.execute("DROP TABLE live_activity_tokens")
    raw.execute("""
        CREATE TABLE live_activity_tokens (
            token        TEXT PRIMARY KEY,
            kind         TEXT NOT NULL DEFAULT 'start',
            env          TEXT,
            activity     TEXT,
            created_ms   INTEGER NOT NULL,
            last_seen_ms INTEGER NOT NULL
        )""")
    raw.execute("INSERT INTO live_activity_tokens (token, kind, env, activity, "
                "created_ms, last_seen_ms) VALUES ('legacy-token-1', 'update', "
                "'production', 'storm', 1, 1)")
    raw.commit()
    raw.close()

    asyncio.run(dbmod.init_db())
    asyncio.run(dbmod.init_db())               # idempotent on every boot

    async def run():
        return await dbmod.list_live_activity_tokens("update", activity="storm",
                                                     mac=MAC_A)
    assert [r["token"] for r in asyncio.run(run())] == ["legacy-token-1"]


def test_the_unchanged_skip_is_kept_per_station(client, monkeypatch):
    """Two stations' cards with identical numbers must not hide each
    other's update behind the 2.6.1 unchanged-skip."""
    from app import apns
    _register(client, "tok-a" * 4, "wind", MAC_A)
    _register(client, "tok-b" * 4, "wind", MAC_B)
    sent = []

    async def fake_relay(tokens, title, body, url, tok, la_payload=None, **kw):
        sent.append(tuple(sorted(tokens)))
        return {"sent": len(tokens), "dead": [], "failed": 0}

    async def relay():
        return "https://relay.example", "relay-token"
    monkeypatch.setattr(apns, "_push_via_relay", fake_relay)
    monkeypatch.setattr(apns, "effective_relay", relay)
    payload = apns.build_live_activity_update({"gustMph": 30.0},
                                              now_s=int(time.time()))

    async def run():
        await apns.send_live_activity_update("wind", payload, "Wind", "A", mac=MAC_A)
        await apns.send_live_activity_update("wind", payload, "Wind", "B", mac=MAC_B)
        await apns.send_live_activity_update("wind", payload, "Wind", "A", mac=MAC_A)
    asyncio.run(run())
    assert sent == [("tok-a" * 4,), ("tok-b" * 4,)]


def test_storm_cards_say_whose_storm_and_route_by_it(client, monkeypatch):
    from app import apns, db, storm_watch
    calls = {"start": [], "update": []}

    async def fake_start(payload, title, body, **kw):
        calls["start"].append(payload)
        return {"sent": 1, "dead": [], "failed": 0}

    async def fake_update(activity, payload, title, body, **kw):
        calls["update"].append(kw.get("mac"))
        return {"sent": 1, "dead": [], "failed": 0}
    monkeypatch.setattr(apns, "send_live_activity_start", fake_start)
    monkeypatch.setattr(apns, "send_live_activity_update", fake_update)
    cfg = SimpleNamespace(storm_summary=True, storm_live_activity=True)
    dev = {"mac": MAC_A, "name": "Davis"}
    now = int(time.time() * 1000)

    async def run():
        await storm_watch.on_open_tick(cfg, dev, now - 30 * MIN, now, "yearlyrainin")
        await storm_watch.on_open_tick(cfg, dev, now - 30 * MIN, now + 30 * MIN,
                                       "yearlyrainin")
        await storm_watch.on_closed(cfg, dev, now - 30 * MIN, now + 40 * MIN,
                                    now + 40 * MIN, "yearlyrainin", True)
        return await db.get_storm_watch_la(MAC_A)
    asyncio.run(run())
    assert calls["start"][0]["aps"]["attributes"]["mac"] == MAC_A
    assert calls["update"] == [MAC_A, MAC_A]


# ── 1-3. the freeze card: its station, frost, the corrected low ─────────

T0 = int(_dt.datetime(2026, 1, 15, 20, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)
NIGHT = "2026-01-16"


def _freeze_capture(monkeypatch):
    from app import apns
    from app import freeze_live as fl
    calls = {"start": [], "update": []}

    async def fake_start(payload, title, body, **kw):
        calls["start"].append(payload)
        return {"sent": 1, "dead": [], "failed": 0}

    async def fake_update(activity, payload, title, body, **kw):
        calls["update"].append((payload, kw.get("mac")))
        return {"sent": 1, "dead": [], "failed": 0}
    monkeypatch.setattr(apns, "send_live_activity_start", fake_start)
    monkeypatch.setattr(apns, "send_live_activity_update", fake_update)
    monkeypatch.setattr(fl, "_next_sunrise_ms", lambda c, n: T0 + 11 * 60 * MIN)
    return calls


def _low(low_f):
    from app import db
    asyncio.run(db.insert_forecast_snapshots(
        "open-meteo", T0 - 3 * 60 * MIN,
        [{"valid_date": NIGHT, "lead_days": 1, "tmax_f": 50.0,
          "tmin_f": low_f, "pop": 0.0, "precip_in": 0.0}]))


def _freeze_dev(now_ms, tempf):
    return [{"mac": MAC_A, "name": "Yard",
             "lastData": {"dateutc": now_ms, "tempf": tempf}}]


def test_the_freeze_card_routes_by_the_station_it_opened_for(client, monkeypatch):
    calls = _freeze_capture(monkeypatch)
    from app import freeze_live as fl
    cfg = SimpleNamespace(freeze_live_activity=True)
    _low(30.0)

    async def run():
        await fl.check(cfg, _freeze_dev(T0, 40.0), T0)
        await fl.check(cfg, _freeze_dev(T0 + 16 * MIN, 38.0), T0 + 16 * MIN)
    asyncio.run(run())
    assert calls["start"][0]["aps"]["attributes"]["mac"] == MAC_A
    assert [m for _p, m in calls["update"]] == [MAC_A]


def test_a_frost_night_says_frost_and_a_freeze_night_does_not(client, monkeypatch):
    calls = _freeze_capture(monkeypatch)
    from app import freeze_live as fl
    cfg = SimpleNamespace(freeze_live_activity=True)
    _low(33.6)                                  # prints "low 34F": frost band
    asyncio.run(fl.check(cfg, _freeze_dev(T0, 40.0), T0))
    aps = calls["start"][0]["aps"]
    assert aps["alert"]["title"] == "Frost possible tonight"
    assert aps["content-state"]["frost"] is True
    assert aps["content-state"]["lowAdjusted"] is False


def test_a_freeze_night_keeps_frost_off(client, monkeypatch):
    calls = _freeze_capture(monkeypatch)
    from app import freeze_live as fl
    _low(30.0)
    asyncio.run(fl.check(SimpleNamespace(freeze_live_activity=True),
                         _freeze_dev(T0, 40.0), T0))
    assert calls["start"][0]["aps"]["content-state"]["frost"] is False


def test_the_corrected_low_opens_the_card_and_says_so(client, monkeypatch):
    """Doren reads ~3 °F under the forecast. A 36 °F forecast opens nothing
    on its own; with the station's -3 °F correction it is 33 °F, a frost
    night, and the card says the low was adjusted."""
    calls = _freeze_capture(monkeypatch)
    from app import forecast_skill
    from app import freeze_live as fl
    _low(36.0)
    cfg = SimpleNamespace(freeze_live_activity=True)

    async def none(mac, provider):
        return None
    monkeypatch.setattr(forecast_skill, "low_offset_f", none)
    asyncio.run(fl.check(cfg, _freeze_dev(T0, 40.0), T0))
    assert calls["start"] == [], "uncorrected, 36 °F opens no card"

    async def colder(mac, provider):
        assert (mac, provider) == (MAC_A, "open-meteo")
        return -3.0
    monkeypatch.setattr(forecast_skill, "low_offset_f", colder)
    asyncio.run(fl.check(cfg, _freeze_dev(T0 + MIN, 40.0), T0 + MIN))
    aps = calls["start"][0]["aps"]
    assert aps["content-state"]["lowF"] == 33.0
    assert aps["content-state"]["lowAdjusted"] is True
    assert aps["content-state"]["frost"] is True
    assert "(adjusted for your station)" in aps["alert"]["body"]


# ── 3. the correction itself ─────────────────────────────────────────────

def _card(low_bias, lead=1, enough=True):
    return {"available": True, "leads": [
        {"lead_days": lead, "n": 30, "enough": enough,
         "high": {"bias_f": 0.2}, "low": {"bias_f": low_bias}}]}


def test_low_offset_turns_the_lead_one_bias_around(client, monkeypatch):
    from app import config, forecast_skill
    monkeypatch.setattr(config.settings, "insights", True)
    seen = []

    async def fake(mac, *, provider, days, today=None):
        seen.append((mac, provider, days))
        return _card(3.2)
    monkeypatch.setattr(forecast_skill, "scorecard", fake)

    async def run():
        a = await forecast_skill.low_offset_f(MAC_A, "open-meteo")
        b = await forecast_skill.low_offset_f(MAC_A, "open-meteo")   # cached
        return a, b
    assert asyncio.run(run()) == (-3.2, -3.2)
    assert seen == [(MAC_A, "open-meteo", forecast_skill.CORRECTION_DAYS)]


@pytest.mark.parametrize("why", ["insights_off", "unarchived", "no_mac",
                                 "thin", "other_lead"])
def test_low_offset_is_none_without_a_real_bias(client, monkeypatch, why):
    from app import config, forecast_skill
    monkeypatch.setattr(config.settings, "insights", why != "insights_off")
    card = {"thin": _card(3.2, enough=False), "other_lead": _card(3.2, lead=2)}

    async def fake(mac, *, provider, days, today=None):
        return card.get(why, _card(3.2))
    monkeypatch.setattr(forecast_skill, "scorecard", fake)
    provider = "accuweather" if why == "unarchived" else "open-meteo"
    mac = None if why == "no_mac" else MAC_A
    assert asyncio.run(forecast_skill.low_offset_f(mac, provider)) is None


def test_the_outlook_frost_line_reads_the_corrected_low(client):
    from app import outlook as ol
    evening = _dt.datetime(2026, 1, 15, 19, 0, tzinfo=ZoneInfo("America/Phoenix"))
    daily = {"time": ["2026-01-15", "2026-01-16"],
             "temperature_2m_max": [60.0, 58.0],
             "temperature_2m_min": [40.0, 36.0],
             "weather_code": [0, 0], "precipitation_probability_max": [0, 0]}
    plain = ol.build(daily, narrative=None, when="tomorrow", now_local=evening,
                     source=ol.SOURCE_OPEN_METEO)
    assert plain.frost_line is None
    fixed = ol.build(daily, narrative=None, when="tomorrow", now_local=evening,
                     source=ol.SOURCE_OPEN_METEO, low_offset_f=-3.0)
    assert fixed.frost_line == ("Frost possible tonight, low 33F "
                                "(adjusted for your station).")
    # The forecast's own numbers stay as issued.
    assert fixed.lo_f == plain.lo_f == 36.0


# ── 4. a ladder speaks once ──────────────────────────────────────────────

def _rule(rid, comp, thr, field="windgustmph", sev="minor"):
    return {"id": rid, "field": field, "comparator": comp, "threshold": thr,
            "severity": sev, "target_mac": None}


def test_collapse_ladder_picks_the_most_extreme_per_field_and_direction():
    from app.alerts import collapse_ladder
    up = [_rule(1, "above", 10), _rule(2, "above", 20), _rule(3, "above", 15)]
    down = [_rule(4, "below", 32, "tempf"), _rule(5, "below", 28, "tempf")]
    eq = [_rule(6, "equalTo", 0, "uv"), _rule(7, "equalTo", 0, "uv")]
    speak, quiet = collapse_ladder(up + down + eq)
    assert sorted(r["id"] for r in speak) == [2, 5, 6, 7]
    assert sorted(r["id"] for r in quiet) == [1, 3, 4]
    # A tie on the threshold goes to the more severe rule.
    tie = [_rule(8, "above", 20, sev="minor"), _rule(9, "above", 20, sev="urgent")]
    assert [r["id"] for r in collapse_ladder(tie)[0]] == [9]


def test_a_gust_jump_fires_the_top_rung_only_and_the_rest_stay_quiet(client, monkeypatch):
    """Doren 10-17: 6.7 to 20.7 mph in ten seconds fired 10, 15 and 20 at
    once. Now the 20 speaks, the others are marked as fired with it, and
    the next tick is silent."""
    from app import alerts, db
    sent = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        sent.append(kw.get("route"))
        return True
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    cfg = SimpleNamespace(email_scope="all")
    now = int(time.time() * 1000)

    async def run():
        ids = [(await db.create_alert_rule(MAC_A, "windgustmph", "above", t))["id"]
               for t in (10.0, 15.0, 20.0)]
        dev = [{"mac": MAC_A, "name": "Davis",
                "lastData": {"windgustmph": 20.7, "dateutc": now}}]
        mon = alerts.AlertMonitor()
        await mon._check_threshold_rules(cfg, dev, now)
        first = list(sent)
        await mon._check_threshold_rules(cfg, dev, now + MIN)
        states = await db.get_rule_states_full()
        return ids, first, [states[(i, MAC_A)][0] for i in ids]
    ids, first, states = asyncio.run(run())
    assert first == [f"rule/{ids[2]}"]
    assert sent == first, "the next tick is quiet"
    assert states == [1, 1, 1]


def test_a_failed_delivery_leaves_the_whole_ladder_armed(client, monkeypatch):
    from app import alerts, db

    async def fails(*a, **kw):
        return False
    monkeypatch.setattr(alerts, "_deliver", fails)
    now = int(time.time() * 1000)

    async def run():
        ids = [(await db.create_alert_rule(MAC_A, "windgustmph", "above", t))["id"]
               for t in (10.0, 20.0)]
        await alerts.AlertMonitor()._check_threshold_rules(
            SimpleNamespace(email_scope="all"),
            [{"mac": MAC_A, "lastData": {"windgustmph": 25.0}}], now)
        states = await db.get_rule_states_full()
        return [states.get((i, MAC_A), (0, None))[0] for i in ids]
    assert asyncio.run(run()) == [0, 0]


# ── 5. channels per report ───────────────────────────────────────────────

@pytest.mark.parametrize("field", ["digest_channels", "outlook_channels",
                                   "sky_channels"])
def test_report_channels_round_trip_and_validate(client, field):
    assert client.get("/api/alerts", headers=H).json()[field] is None
    r = client.put("/api/alerts", json={field: "push"}, headers=H)
    assert r.status_code == 200 and r.json()[field] == "push"
    backup = client.get("/api/config/backup", headers=H).json()
    assert backup["alert_prefs"][field] == "push"
    assert client.put("/api/alerts", json={field: "fax"}, headers=H).status_code == 400
    r = client.put("/api/alerts", json={field: ""}, headers=H)
    assert r.json()[field] is None
    r = client.post("/api/config/restore", json=backup, headers=H)
    assert r.status_code == 200, r.text
    assert client.get("/api/alerts", headers=H).json()[field] == "push"


def test_channel_flags():
    from app.alerts import channel_flags
    assert channel_flags(None, email_default=False) == (False, True)
    assert channel_flags("push", email_default=True) == (False, True)
    assert channel_flags("email", email_default=False) == (True, False)
    assert channel_flags("both", email_default=False) == (True, True)


def _sky_station(client):
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AA:BB:CC:DD:28:10", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": "2026-09-11T00:30:00Z",
                      "outdoor": {"tempf": 95, "humidity": 20, "dew_point_f": 50},
                      "wind": {"speed_mph": 2}})


@pytest.mark.parametrize("choice,email,push", [
    (None, True, True), ("push", False, True), ("email", True, False)])
def test_the_sky_note_follows_its_channels(client, monkeypatch, choice, email, push):
    from app import alerts, config, db
    _sky_station(client)
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    body = {"sky_notes": True, "sky_good_only": False, "email_scope": "all"}
    if choice:
        body["sky_channels"] = choice
    client.put("/api/alerts", headers=H, json=body)
    seen = []

    async def fake_deliver(cfg, subject, text, title, b, **kw):
        seen.append((kw.get("email_ok"), kw.get("push_ok", True)))
        return True

    async def clouds(self, lat, lon, tz_name, now_local):
        return 8.0
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_clouds", clouds)
    dusk = int(_dt.datetime(2026, 9, 10, 18, 30,
                            tzinfo=ZoneInfo("America/Phoenix")).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        await alerts.AlertMonitor()._maybe_send_sky(cfg, await db.list_devices(), dusk)
    asyncio.run(run())
    assert seen == [(email, push)]


@pytest.mark.parametrize("choice,mails,push", [
    (None, 1, True), ("push", 0, True), ("email", 1, False)])
def test_the_outlook_follows_its_channels(client, monkeypatch, choice, mails, push):
    from app import alerts, config, db
    _sky_station(client)
    body = {"outlook_hour": 19, "outlook_minute": 0, "enabled": True,
            "recipients": ["d@example.com"], "smtp_host": "smtp.example.com",
            "smtp_port": 587, "smtp_username": "u", "smtp_password": "p"}
    if choice:
        body["outlook_channels"] = choice
    client.put("/api/alerts", headers=H, json=body)
    sent_mail, pushed = [], []

    async def fake_deliver(cfg, subject, text, title, b, **kw):
        pushed.append(kw.get("push_ok", True))
        return True

    async def fetch(self, coords, source, wu_key, tz_name):
        daily = {"time": ["2026-09-10", "2026-09-11"],
                 "temperature_2m_max": [100.0, 99.0],
                 "temperature_2m_min": [80.0, 79.0],
                 "weather_code": [0, 0], "precipitation_probability_max": [0, 0]}
        return daily, None, "open-meteo", None
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    monkeypatch.setattr(alerts, "_send_sync",
                        lambda s, b, to, cfg, html=None: sent_mail.append(s))
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fetch)
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    at = int(_dt.datetime(2026, 9, 10, 19, 5,
                          tzinfo=ZoneInfo("America/Phoenix")).timestamp() * 1000)

    async def run():
        cfg = await alerts.effective_config()
        await alerts.AlertMonitor()._maybe_send_outlook(cfg, await db.list_devices(), at)
    asyncio.run(run())
    assert len(sent_mail) == mails
    assert pushed == [push]


@pytest.mark.parametrize("choice,mails,cards", [
    ("email", 1, []), ("push", 0, ["morning"]), ("both", 1, ["morning"])])
def test_the_morning_report_follows_its_channels(client, monkeypatch, choice,
                                                 mails, cards):
    import app.alerts as al
    from app import apns, db, insights
    from app.config import settings as _settings
    sent, la = [], []
    monkeypatch.setattr(al, "_send_sync",
                        lambda s, b, to, cfg, html=None: sent.append(s))

    async def fake_la(payload, title, body, activity="rain"):
        la.append(activity)
        return {"sent": 1, "dead": [], "failed": 0}

    async def no_push():
        return False
    monkeypatch.setattr(apns, "send_live_activity_start", fake_la)
    monkeypatch.setattr(apns, "push_configured", no_push)
    cfg = SimpleNamespace(enabled=True, recipients=["v@z.com"], digest_hour=7,
                          smtp_host="h", smtp_port=465, smtp_username=None,
                          smtp_password=None, smtp_from=None, smtp_tls=False,
                          smtp_ssl=True, email_scope="all",
                          digest_channels=choice)
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)

    async def run():
        mac = "AA:BB:CC:DD:28:20"
        await db.upsert_device(mac, {"lastData": {"dateutc": now_ms, "tempf": 90.0}})
        await db.insert_observations(mac, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000), "tempf": 101.0}])
        await insights.rebuild(mac)
        mon = al.AlertMonitor()
        await mon._maybe_send_digest(cfg, await db.list_devices(), now_ms)
        # A second tick sends nothing more: a switched-off half is done.
        await mon._maybe_send_digest(cfg, await db.list_devices(), now_ms + MIN)
    asyncio.run(run())
    assert len(sent) == mails
    assert la == cards


# ── 6. one smart watch at a time ─────────────────────────────────────────

def test_smart_muted_round_trips_cleaned_with_its_catalogue(client):
    state = client.get("/api/alerts", headers=H).json()
    assert state["smart_muted"] == []
    keys = [c["key"] for c in state["smart_kind_catalogue"]]
    from app.alerts import SMART_KINDS
    assert keys == list(SMART_KINDS)
    assert {c["label"] for c in state["smart_kind_catalogue"]} >= {"Pressure falling fast"}
    r = client.put("/api/alerts", headers=H,
                   json={"smart_muted": ["outflow", "pressure_drop", "bogus", "outflow"]})
    assert r.status_code == 200
    assert r.json()["smart_muted"] == ["pressure_drop", "outflow"]
    backup = client.get("/api/config/backup", headers=H).json()
    client.put("/api/alerts", headers=H, json={"smart_muted": []})
    assert client.get("/api/alerts", headers=H).json()["smart_muted"] == []
    assert client.post("/api/config/restore", json=backup, headers=H).status_code == 200
    assert client.get("/api/alerts", headers=H).json()["smart_muted"] == [
        "pressure_drop", "outflow"]


def test_a_muted_kind_is_silent_and_its_crossing_is_not_replayed(client, monkeypatch):
    from app import alerts, db
    sent = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        sent.append(kw.get("kind"))
        return True
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    now = int(time.time() * 1000)
    dev = [{"mac": MAC_A, "name": "Davis",
            "lastData": {"tempf": 31.0, "dateutc": now}}]
    base = dict(enabled=False, email_scope="all", recipients=[],
                storm_summary=False, lightning_alert_mi=None)

    async def run():
        mon = alerts.AlertMonitor()
        await mon._check_smart_alerts(SimpleNamespace(**base, smart_muted=["frost"]),
                                      dev, now)
        muted_states = await db.get_smart_alert_states()
        # Unmuted with the night still frosty: no replay of the old crossing.
        await mon._check_smart_alerts(SimpleNamespace(**base, smart_muted=[]),
                                      dev, now + MIN)
        return muted_states
    muted_states = asyncio.run(run())
    assert sent == []
    assert muted_states[(MAC_A, "frost")] == 1


def test_a_muted_kind_is_left_out_of_the_macs_banner_list(client):
    from app import db
    asyncio.run(db.upsert_smart_alert_state(MAC_A, "pressure_drop", 1, 1))
    asyncio.run(db.upsert_smart_alert_state(MAC_A, "frost", 1, 1))
    client.put("/api/alerts", headers=H, json={"smart_muted": ["pressure_drop"]})
    kinds = {r["kind"] for r in client.get("/api/alerts", headers=H).json()["smart_alerts"]}
    assert kinds == {"frost"}


def test_the_new_alert_columns_reach_a_database_that_already_exists(client):
    from app import db as dbmod
    from app.config import settings
    raw = sqlite3.connect(settings.database_path)
    for col in ("digest_channels", "outlook_channels", "sky_channels", "smart_muted"):
        raw.execute(f"ALTER TABLE alert_prefs DROP COLUMN {col}")
    raw.commit()
    raw.close()
    asyncio.run(dbmod.init_db())
    raw = sqlite3.connect(settings.database_path)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(alert_prefs)")}
    raw.close()
    assert {"digest_channels", "outlook_channels", "sky_channels",
            "smart_muted"} <= cols
    assert client.get("/api/alerts", headers=H).status_code == 200


# ── review fixes (CodeRabbit, PR #131) ───────────────────────────────────

def test_a_ladder_speaks_at_its_loudest_rungs_severity():
    from app.alerts import collapse_ladder, ladder_severity
    loud_low = _rule(1, "above", 10, sev="urgent")
    quiet_top = _rule(2, "above", 20, sev="minor")
    other_field = _rule(3, "above", 5, field="windspeedmph", sev="major")
    speak, quiet = collapse_ladder([loud_low, quiet_top, other_field])
    top = next(r for r in speak if r["id"] == 2)
    assert ladder_severity(top, quiet) == "urgent"
    # A rule on another field never lends its severity.
    lone = next(r for r in speak if r["id"] == 3)
    assert ladder_severity(lone, quiet) == "major"


def test_the_folded_ladders_delivery_rides_the_loudest_tier(client, monkeypatch):
    from app import alerts, db
    tiers = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        tiers.append(kw.get("severity"))
        return True
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    now = int(time.time() * 1000)

    async def run():
        await db.create_alert_rule(MAC_A, "windgustmph", "above", 10.0, severity="urgent")
        await db.create_alert_rule(MAC_A, "windgustmph", "above", 20.0, severity="minor")
        await alerts.AlertMonitor()._check_threshold_rules(
            SimpleNamespace(email_scope="all"),
            [{"mac": MAC_A, "lastData": {"windgustmph": 21.0}}], now)
    asyncio.run(run())
    assert tiers == [alerts.rule_tier("urgent")]


def test_the_folded_cards_end_reaches_only_its_own_stations_tokens(client):
    from app import db
    _register(client, "tok-a" * 4, "freeze", MAC_A)
    _register(client, "tok-old" * 4, "freeze")

    async def run():
        return await db.list_live_activity_tokens("update", activity="freeze",
                                                  mac=MAC_A, exact=True)
    assert [r["token"] for r in asyncio.run(run())] == ["tok-a" * 4]


def test_low_offset_degrades_to_none_on_an_unexpected_error_and_retries(
        client, monkeypatch):
    from app import config, forecast_skill
    monkeypatch.setattr(config.settings, "insights", True)
    calls = []

    async def broken(mac, *, provider, days, today=None):
        calls.append(1)
        raise RuntimeError("database is locked")
    monkeypatch.setattr(forecast_skill, "scorecard", broken)
    assert asyncio.run(forecast_skill.low_offset_f(MAC_A, "open-meteo")) is None
    assert asyncio.run(forecast_skill.low_offset_f(MAC_A, "open-meteo")) is None
    assert len(calls) == 2, "a failure is not cached"


def test_low_offset_cache_keeps_the_servers_day_only(client, monkeypatch):
    from app import config, forecast_skill
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")

    async def fake(mac, *, provider, days, today=None):
        return _card(2.0)
    monkeypatch.setattr(forecast_skill, "scorecard", fake)
    forecast_skill._LOW_OFFSET_CACHE[(MAC_A, "open-meteo", "2000-01-01")] = 9.0
    asyncio.run(forecast_skill.low_offset_f(MAC_A, "open-meteo"))
    days = {k[2] for k in forecast_skill._LOW_OFFSET_CACHE}
    today = forecast_skill._local_date(int(time.time() * 1000)).isoformat()
    assert days == {today}


def test_an_email_only_report_switched_on_late_waits_for_tomorrow(client, monkeypatch):
    """The grace window reads the real phone stamp: an email-only report
    switched on after its window is not 'given up on' (no abandoned
    stamp), it simply starts tomorrow, as before 2.8."""
    import app.alerts as al
    from app import db
    from app.config import settings as _settings
    sent = []
    monkeypatch.setattr(al, "_send_sync",
                        lambda s, b, to, cfg, html=None: sent.append(s))
    cfg = SimpleNamespace(enabled=True, recipients=["v@z.com"], digest_hour=7,
                          smtp_host="h", smtp_port=465, smtp_username=None,
                          smtp_password=None, smtp_from=None, smtp_tls=False,
                          smtp_ssl=True, email_scope="all",
                          digest_channels="email")
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    evening = _dt.datetime.now(tz).replace(hour=20, minute=0)
    ms = int(evening.timestamp() * 1000)

    async def run():
        await al.AlertMonitor()._maybe_send_digest(cfg, [], ms)
        return (await db.get_kv("alerts.digest.email_abandoned_day"),
                await db.get_kv("alerts.digest.phone_day"))
    abandoned, phone_day = asyncio.run(run())
    assert sent == []
    assert abandoned is None
    assert phone_day is None


def test_a_shared_legacy_card_never_hides_a_stations_repeat(client, monkeypatch):
    """A pre-2.8 card takes both stations' updates. A sends S_A, B sends
    S_B to the same card, then A repeats S_A: the card shows S_B, so A's
    repeat must go out rather than be skipped by A's memo."""
    from app import apns
    _register(client, "tok-a" * 4, "wind", MAC_A)
    _register(client, "tok-b" * 4, "wind", MAC_B)
    _register(client, "tok-old" * 4, "wind")
    sent = []

    async def fake_relay(tokens, title, body, url, tok, la_payload=None, **kw):
        sent.append(title)
        return {"sent": len(tokens), "dead": [], "failed": 0}

    async def relay():
        return "https://relay.example", "relay-token"
    monkeypatch.setattr(apns, "_push_via_relay", fake_relay)
    monkeypatch.setattr(apns, "effective_relay", relay)
    now_s = int(time.time())
    s_a = apns.build_live_activity_update({"gustMph": 30.0}, now_s=now_s)
    s_b = apns.build_live_activity_update({"gustMph": 12.0}, now_s=now_s)

    async def run():
        await apns.send_live_activity_update("wind", s_a, "A1", "", mac=MAC_A)
        await apns.send_live_activity_update("wind", s_b, "B1", "", mac=MAC_B)
        await apns.send_live_activity_update("wind", s_a, "A2", "", mac=MAC_A)
    asyncio.run(run())
    assert sent == ["A1", "B1", "A2"]


def test_station_only_cards_keep_their_own_skip(client, monkeypatch):
    """With no legacy card the stations share nothing, and each keeps its
    unchanged-skip: A's repeat after B's send is still skipped."""
    from app import apns
    _register(client, "tok-a" * 4, "wind", MAC_A)
    _register(client, "tok-b" * 4, "wind", MAC_B)
    sent = []

    async def fake_relay(tokens, title, body, url, tok, la_payload=None, **kw):
        sent.append(title)
        return {"sent": len(tokens), "dead": [], "failed": 0}

    async def relay():
        return "https://relay.example", "relay-token"
    monkeypatch.setattr(apns, "_push_via_relay", fake_relay)
    monkeypatch.setattr(apns, "effective_relay", relay)
    now_s = int(time.time())
    s_a = apns.build_live_activity_update({"gustMph": 30.0}, now_s=now_s)
    s_b = apns.build_live_activity_update({"gustMph": 12.0}, now_s=now_s)

    async def run():
        await apns.send_live_activity_update("wind", s_a, "A1", "", mac=MAC_A)
        await apns.send_live_activity_update("wind", s_b, "B1", "", mac=MAC_B)
        await apns.send_live_activity_update("wind", s_a, "A2", "", mac=MAC_A)
    asyncio.run(run())
    assert sent == ["A1", "B1"]


def test_an_email_only_outlook_logs_and_fans_out_once_it_is_sent(client, monkeypatch):
    """Email-only outlook with SMTP down (CodeRabbit, PR #131): the retry
    used to log a history row and fire the webhooks on EVERY tick before
    the email failed again, and the row never said the email went out.
    Now nothing is recorded until the email's outcome is known, and the
    row that lands says delivered."""
    from app import alerts, config, db, webhooks
    _sky_station(client)
    client.put("/api/alerts", headers=H, json={
        "outlook_hour": 19, "outlook_minute": 0, "enabled": True,
        "outlook_channels": "email",
        "recipients": ["d@example.com"], "smtp_host": "smtp.example.com",
        "smtp_port": 587, "smtp_username": "u", "smtp_password": "p"})
    hooks, attempts = [], []
    smtp_up = {"ok": False}

    def send(s, b, to, cfg, html=None):
        attempts.append(s)
        if not smtp_up["ok"]:
            raise OSError("smtp down")

    async def fetch(self, coords, source, wu_key, tz_name):
        daily = {"time": ["2026-09-10", "2026-09-11"],
                 "temperature_2m_max": [100.0, 99.0],
                 "temperature_2m_min": [80.0, 79.0],
                 "weather_code": [0, 0], "precipitation_probability_max": [0, 0]}
        return daily, None, "open-meteo", None

    async def fake_dispatch(kind, mac, title, body, ts_ms, **kw):
        hooks.append(kind)
    monkeypatch.setattr(alerts, "_send_sync", send)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fetch)
    monkeypatch.setattr(webhooks, "dispatch_alert", fake_dispatch)
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    at = int(_dt.datetime(2026, 9, 10, 19, 5,
                          tzinfo=ZoneInfo("America/Phoenix")).timestamp() * 1000)

    async def tick():
        cfg = await alerts.effective_config()
        await alerts.AlertMonitor()._maybe_send_outlook(cfg, await db.list_devices(), at)
        await asyncio.sleep(0)               # let a fire-and-forget dispatch run

    async def outlook_rows():
        return [r for r in await db.recent_alerts(50) if r["kind"] == "outlook"]

    async def run():
        await tick()
        await tick()
        assert len(attempts) == 2            # the email is retried
        assert await outlook_rows() == []    # but nothing is recorded yet
        assert hooks == []
        smtp_up["ok"] = True
        await tick()
        rows = await outlook_rows()
        assert len(rows) == 1 and rows[0]["delivered"]
        assert hooks == ["outlook"]
        await tick()                         # done: no fourth send
        assert len(attempts) == 3
    asyncio.run(run())


def test_a_both_channel_outlook_with_no_push_waits_for_its_email(client, monkeypatch):
    """Default channels, SMTP down, no push configured, a webhook
    registered (CodeRabbit, PR #131): _deliver found no channel, called it
    handled, and logged a row and fired the webhook on every retry; the
    webhook also counted as delivery, so the email was never retried.
    Nothing is recorded now until a real channel delivers."""
    from app import alerts, apns, config, db, webhooks
    _sky_station(client)
    client.put("/api/alerts", headers=H, json={
        "outlook_hour": 19, "outlook_minute": 0, "enabled": True,
        "recipients": ["d@example.com"], "smtp_host": "smtp.example.com",
        "smtp_port": 587, "smtp_username": "u", "smtp_password": "p"})
    hooks, attempts = [], []
    smtp_up = {"ok": False}

    def send(s, b, to, cfg, html=None):
        attempts.append(s)
        if not smtp_up["ok"]:
            raise OSError("smtp down")

    async def fetch(self, coords, source, wu_key, tz_name):
        daily = {"time": ["2026-09-10", "2026-09-11"],
                 "temperature_2m_max": [100.0, 99.0],
                 "temperature_2m_min": [80.0, 79.0],
                 "weather_code": [0, 0], "precipitation_probability_max": [0, 0]}
        return daily, None, "open-meteo", None

    async def fake_dispatch(kind, mac, title, body, ts_ms, **kw):
        hooks.append(kind)

    async def no_push():
        return False

    async def a_hook(enabled_only=False):
        return [{"id": 1}]
    monkeypatch.setattr(alerts, "_send_sync", send)
    monkeypatch.setattr(alerts.AlertMonitor, "_fetch_outlook", fetch)
    monkeypatch.setattr(webhooks, "dispatch_alert", fake_dispatch)
    monkeypatch.setattr(apns, "push_configured", no_push)
    monkeypatch.setattr(db, "list_webhooks", a_hook)
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    at = int(_dt.datetime(2026, 9, 10, 19, 5,
                          tzinfo=ZoneInfo("America/Phoenix")).timestamp() * 1000)

    async def tick():
        cfg = await alerts.effective_config()
        await alerts.AlertMonitor()._maybe_send_outlook(cfg, await db.list_devices(), at)
        await asyncio.sleep(0)

    async def outlook_rows():
        return [r for r in await db.recent_alerts(50) if r["kind"] == "outlook"]

    async def run():
        await tick()
        await tick()
        assert len(attempts) == 2
        assert await outlook_rows() == []
        assert hooks == []
        smtp_up["ok"] = True
        await tick()
        rows = await outlook_rows()
        assert len(rows) == 1 and rows[0]["delivered"]
        assert hooks == ["outlook"]
        await tick()
        assert len(attempts) == 3
    asyncio.run(run())
