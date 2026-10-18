"""2.7: one smart alert per place (Doren, 2026-10-13).

A Davis and a Tempest in one yard both saw the same 0.06 inHg fall and
"Pressure falling fast" arrived twice a minute apart. Stations whose
coordinates sit within places.PLACE_RADIUS_KM of each other are one place;
the weather watches (places.GROUPED_KINDS) speak once per place, naming the
stations that agreed. Hardware alerts (battery, sensor quiet, leak) stay
per station, and a station with no location never groups."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

HOME = (33.3062, -111.8413)
NEXT_DOOR = (33.3066, -111.8413)      # ~45 m north: the same yard
ACROSS_TOWN = (33.3562, -111.8413)    # ~5.6 km north: its own place

SID = "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b"
_RealClient = httpx.AsyncClient


def _cfg(**kw):
    base = dict(enabled=False, email_scope="all", recipients=[],
                storm_summary=False, lightning_alert_mi=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _dev(mac, name, where, **last):
    d = {"mac": mac, "name": name, "lastData": dict(last)}
    if where is not None:
        d["info"] = {"coords": {"location": None,
                                "coords": {"lat": where[0], "lon": where[1]}}}
    return d


DAVIS = "AA:BB:CC:00:07:01"
TEMPEST = "AA:BB:CC:00:07:02"
ATLAS = "AA:BB:CC:00:07:03"


def _recorder(monkeypatch, ok=True):
    import app.alerts as al
    sent = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        sent.append({"kind": kw.get("kind"), "mac": kw.get("mac"),
                     "title": pt, "body": pb})
        return ok
    monkeypatch.setattr(al, "_deliver", fake_deliver)
    return sent


async def _seed_pressure(now, macs, then=29.96):
    from app import db
    for mac in macs:
        await db.insert_observations(mac, [{
            "dateutc": now - 3 * 3_600_000 - 60_000, "baromrelin": then}])


def _falling(mac, name, where, now, p=29.89):
    return _dev(mac, name, where, baromrelin=p, dateutc=now)


def _smart(devices, now, cfg=None):
    from app.alerts import AlertMonitor
    return AlertMonitor()._check_smart_alerts(cfg or _cfg(), devices, now)


async def _states():
    from app import db
    return await db.get_smart_alert_states()


# ── the pure place rule ──────────────────────────────────────────────────

def test_places_group_by_distance_and_never_group_the_unlocated():
    from app import places
    devs = [_dev("A", "A", HOME), _dev("B", "B", NEXT_DOOR),
            _dev("C", "C", ACROSS_TOWN), _dev("D", "D", None),
            _dev("E", "E", None)]
    assert [[d["mac"] for d in p] for p in places.group(devs)] == [
        ["A", "B"], ["C"], ["D"], ["E"]]
    assert places.distance_km(*HOME, *NEXT_DOOR) < 0.1
    assert places.distance_km(*HOME, *ACROSS_TOWN) > 5


def test_places_are_anchored_not_chained_and_refuse_junk_coords():
    from app import places
    step = 0.0081    # ~0.9 km of latitude
    line = [_dev(m, m, (HOME[0] + i * step, HOME[1]))
            for i, m in enumerate("ABC")]
    assert [[d["mac"] for d in p] for p in places.group(line)] == [
        ["A", "B"], ["C"]]
    for bad in ((True, False), ("nan", "nan"), (999, 0), ("x", 1)):
        d = {"mac": "Z", "info": {"coords": {"coords":
                                             {"lat": bad[0], "lon": bad[1]}}}}
        assert places.device_coords(d) is None


def test_also_line_copy():
    from app import places
    assert places.also_line("seen", []) == ""
    assert places.also_line("seen", ["Tempest"]) == " Also seen at Tempest."
    assert (places.also_line("seen", ["Tempest", "Atlas", "Shed"])
            == " Also seen at Tempest, Atlas and Shed.")
    for kind in places.GROUPED_KINDS:
        assert kind not in {"battery", "sensor_quiet", "flatline", "leak",
                            "rule", "device_stale"}


# ── Doren's pressure drop ────────────────────────────────────────────────

def test_two_stations_in_one_yard_send_one_pressure_alert_naming_both(
        client, monkeypatch):
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)

    async def run():
        await _seed_pressure(now, [DAVIS, TEMPEST])
        devs = [_falling(DAVIS, "Davis", HOME, now),
                _falling(TEMPEST, "Tempest", NEXT_DOOR, now)]
        await _smart(devs, now)
        # The next evaluation, still falling at both: nothing new.
        await _smart(devs, now + 60_000)
        return await _states()

    st = asyncio.run(run())
    assert len(sent) == 1, sent
    assert sent[0]["kind"] == "pressure_drop"
    assert sent[0]["mac"] == DAVIS
    assert sent[0]["title"] == "Davis: Pressure falling fast"
    assert sent[0]["body"].endswith(" Also seen at Tempest."), sent[0]
    assert "—" not in sent[0]["body"].split("approaching.")[1]
    assert st[(DAVIS, "pressure_drop")] == 1
    assert st[(TEMPEST, "pressure_drop")] == 1


def test_a_minute_apart_is_still_one_alert(client, monkeypatch):
    """Doren's actual case: the Davis crosses on one tick, the Tempest on
    the next. The Tempest joins the alert already sent and is marked
    triggered, so it does not fire on any later evaluation either."""
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)

    async def run():
        await _seed_pressure(now, [DAVIS, TEMPEST])
        await _smart([_falling(DAVIS, "Davis", HOME, now),
                      _falling(TEMPEST, "Tempest", NEXT_DOOR, now, p=29.93)],
                     now)
        for minute in (1, 2, 10):
            t = now + minute * 60_000
            await _smart([_falling(DAVIS, "Davis", HOME, t),
                          _falling(TEMPEST, "Tempest", NEXT_DOOR, t)], t)
        return await _states()

    st = asyncio.run(run())
    assert [s["title"] for s in sent] == ["Davis: Pressure falling fast"]
    assert st[(TEMPEST, "pressure_drop")] == 1


def test_stations_across_town_each_send_their_own(client, monkeypatch):
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)

    async def run():
        await _seed_pressure(now, [DAVIS, ATLAS])
        await _smart([_falling(DAVIS, "Davis", HOME, now),
                      _falling(ATLAS, "Atlas", ACROSS_TOWN, now)], now)

    asyncio.run(run())
    assert sorted(s["title"] for s in sent) == [
        "Atlas: Pressure falling fast", "Davis: Pressure falling fast"]
    assert not any("Also" in s["body"] for s in sent)


def test_a_station_with_no_location_never_groups(client, monkeypatch):
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)

    async def run():
        await _seed_pressure(now, [DAVIS, TEMPEST, ATLAS])
        await _smart([_falling(DAVIS, "Davis", None, now),
                      _falling(TEMPEST, "Tempest", None, now),
                      _falling(ATLAS, "Atlas", HOME, now)], now)
        # A minute later: still nobody joins anybody.
        await _smart([_falling(DAVIS, "Davis", None, now + 60_000)],
                     now + 60_000)

    asyncio.run(run())
    assert sorted(s["mac"] for s in sent) == sorted([DAVIS, TEMPEST, ATLAS])


def test_after_a_grouped_alert_both_rearm_and_the_next_crossing_is_one_again(
        client, monkeypatch):
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)
    from app import db

    async def run():
        await _seed_pressure(now, [DAVIS, TEMPEST])
        await _smart([_falling(DAVIS, "Davis", HOME, now),
                      _falling(TEMPEST, "Tempest", NEXT_DOOR, now)], now)
        # The fall eases at both (inside the deadband): both re-arm, no
        # "cleared" notice exists for this kind, so nothing is sent.
        t = now + 20 * 60_000
        await _smart([_falling(DAVIS, "Davis", HOME, t, p=29.96),
                      _falling(TEMPEST, "Tempest", NEXT_DOOR, t, p=29.96)], t)
        mid = await _states()
        # A new fall two hours on: one alert again.
        t2 = now + 2 * 3_600_000
        for mac in (DAVIS, TEMPEST):
            await db.insert_observations(mac, [{
                "dateutc": t2 - 3 * 3_600_000 - 60_000, "baromrelin": 30.00}])
        await _smart([_falling(DAVIS, "Davis", HOME, t2, p=29.90),
                      _falling(TEMPEST, "Tempest", NEXT_DOOR, t2, p=29.90)], t2)
        return mid

    mid = asyncio.run(run())
    assert mid[(DAVIS, "pressure_drop")] == 0
    assert mid[(TEMPEST, "pressure_drop")] == 0
    assert len(sent) == 2, sent
    assert all(s["body"].endswith("Also seen at Tempest.") for s in sent)


def test_a_failed_delivery_leaves_both_free_to_retry(client, monkeypatch):
    sent = _recorder(monkeypatch, ok=False)
    now = int(time.time() * 1000)

    async def run():
        await _seed_pressure(now, [DAVIS, TEMPEST])
        await _smart([_falling(DAVIS, "Davis", HOME, now),
                      _falling(TEMPEST, "Tempest", NEXT_DOOR, now)], now)
        return await _states()

    st = asyncio.run(run())
    assert len(sent) == 1
    assert not st.get((DAVIS, "pressure_drop"))
    assert not st.get((TEMPEST, "pressure_drop"))


def test_a_dead_sibling_does_not_speak_for_the_place_forever(
        client, monkeypatch):
    """The Davis fired hours ago and then died still over the line: its
    triggered row would never clear. Past the window and with no fresh
    reading, it no longer covers the Tempest."""
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)
    from app import db

    async def run():
        # Both have the history, so the dead Davis's stale lastData still
        # reads as over the line: only its age keeps it from covering.
        await _seed_pressure(now, [DAVIS, TEMPEST])
        await db.upsert_smart_alert_state(DAVIS, "pressure_drop", 1,
                                          now - 3 * 3_600_000)
        stale = _falling(DAVIS, "Davis", HOME, now - 3 * 3_600_000)
        await _smart([stale, _falling(TEMPEST, "Tempest", NEXT_DOOR, now)],
                     now)

    asyncio.run(run())
    assert [s["title"] for s in sent] == ["Tempest: Pressure falling fast"]


def test_a_sibling_still_over_the_line_covers_past_the_window(
        client, monkeypatch):
    """The Davis announced the fall an hour ago and is still over the line
    on fresh data: the Tempest crossing now is the same event."""
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)
    from app import db

    async def run():
        await _seed_pressure(now, [DAVIS, TEMPEST])
        await db.upsert_smart_alert_state(DAVIS, "pressure_drop", 1,
                                          now - 3_600_000)
        await _smart([_falling(DAVIS, "Davis", HOME, now),
                      _falling(TEMPEST, "Tempest", NEXT_DOOR, now)], now)
        return await _states()

    st = asyncio.run(run())
    assert sent == []
    assert st[(TEMPEST, "pressure_drop")] == 1


@pytest.mark.parametrize("davis_now", [
    # Back above the line but inside the re-arm deadband: still triggered,
    # no longer in the condition.
    {"tempf": 35.5, "age_min": 0},
    # Still over the line, but its reading has gone stale since it fired.
    {"tempf": 31.0, "age_min": 31},
], ids=["left_the_condition", "stale_data"])
def test_a_sibling_that_fired_inside_the_window_covers_by_the_window_alone(
        client, monkeypatch, davis_now):
    """The Davis announced frost ten minutes ago and cannot cover by being
    over the line on fresh data now: only PLACE_WINDOW_MS (30 minutes)
    makes the Tempest crossing now the same event, and it joins silently."""
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)
    from app import db

    async def run():
        await db.upsert_smart_alert_state(DAVIS, "frost", 1, now - 10 * 60_000)
        davis = _dev(DAVIS, "Davis", HOME, tempf=davis_now["tempf"],
                     dateutc=now - davis_now["age_min"] * 60_000)
        await _smart([davis, _dev(TEMPEST, "Tempest", NEXT_DOOR, tempf=34.0,
                                  dateutc=now)], now)
        return await _states()

    st = asyncio.run(run())
    assert sent == [], sent
    assert st[(DAVIS, "frost")] == 1
    assert st[(TEMPEST, "frost")] == 1


def test_a_front_across_two_stations_is_one_front(client, monkeypatch):
    """The Davis's pressure fall and the Tempest's temperature break on one
    tick in one yard: one front, not a pressure alert plus a temp alert."""
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)
    from app import db

    async def run():
        await _seed_pressure(now, [DAVIS])
        await db.insert_observations(TEMPEST, [{
            "dateutc": now - 3_700_000, "tempf": 92.0}])
        await _smart([_falling(DAVIS, "Davis", HOME, now),
                      _dev(TEMPEST, "Tempest", NEXT_DOOR, tempf=76.0,
                           dateutc=now)], now)
        return await _states()

    st = asyncio.run(run())
    assert [s["kind"] for s in sent] == ["front"], sent
    assert sent[0]["title"] == "Davis: Front passage"
    assert sent[0]["body"].endswith(" Also seen at Tempest.")
    assert st[(DAVIS, "pressure_drop")] == 1
    assert st[(TEMPEST, "temp_drop")] == 1


def test_frost_in_one_yard_is_one_alert(client, monkeypatch):
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)

    async def run():
        await _smart([_dev(DAVIS, "Davis", HOME, tempf=31.0, dateutc=now),
                      _dev(TEMPEST, "Tempest", NEXT_DOOR, tempf=31.5,
                           dateutc=now)], now)

    asyncio.run(run())
    assert [(s["kind"], s["title"]) for s in sent] == [
        ("frost", "Davis: Frost/freeze risk")]
    assert "31°F" in sent[0]["body"]


# ── every channel, once ──────────────────────────────────────────────────

@pytest.fixture
def channels(client, monkeypatch):
    """The real _deliver with push (FCM, the 0fbb5a3a data-only path),
    email and a webhook all open, each recorded."""
    import app.alerts as al
    from app import apns, db, fcm, webhooks
    rec = {"fcm": [], "email": [], "webhook": []}

    def handler(request):
        rec["fcm"].append(json.loads(request.content))
        return httpx.Response(200, json={"name": "projects/x/messages/1"})

    def http_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _RealClient(*a, **kw)

    monkeypatch.setattr(fcm, "_service_account",
                        lambda: {"project_id": "test-project",
                                 "client_email": "x@y", "private_key": "k"})

    async def _tok(sa):
        return "fake-bearer"

    async def _tokens():
        return [{"token": "fcm-token-abcdef123456", "platform": "android"}]

    async def _no_relay():
        return None, None

    async def _sid():
        return SID

    async def _hooks(enabled_only=False):
        return [{"id": 1, "url": "https://hook.example/x", "enabled": 1}]

    def _dispatch(kind, mac, title, body, ts_ms, severity=None):
        rec["webhook"].append((kind, mac, title, body))
        return asyncio.sleep(0)

    def _send_sync(subject, body, to, cfg, html=None):
        rec["email"].append((subject, body))

    monkeypatch.setattr(fcm, "_access_token", _tok)
    monkeypatch.setattr(fcm.httpx, "AsyncClient", http_client)
    monkeypatch.setattr(apns.db, "list_push_tokens", _tokens)
    monkeypatch.setattr(apns, "effective_relay", _no_relay)
    monkeypatch.setattr(apns, "current_server_id", _sid)
    monkeypatch.setattr(db, "list_webhooks", _hooks)
    monkeypatch.setattr(webhooks, "dispatch_alert", _dispatch)
    monkeypatch.setattr(al, "_send_sync", _send_sync)
    monkeypatch.setattr(al, "apns", apns)
    return rec


def test_one_delivery_per_channel_naming_both(channels):
    from app import db
    now = int(time.time() * 1000)
    cfg = _cfg(enabled=True, recipients=["owner@example.com"],
               quiet_start_min=None, quiet_end_min=None)

    async def run():
        await _seed_pressure(now, [DAVIS, TEMPEST])
        devs = [_falling(DAVIS, "Davis", HOME, now),
                _falling(TEMPEST, "Tempest", NEXT_DOOR, now)]
        await _smart(devs, cfg=cfg, now=now)
        await _smart(devs, cfg=cfg, now=now + 60_000)
        return await db.recent_alerts()

    history = asyncio.run(run())
    assert len(channels["fcm"]) == 1, channels["fcm"]
    data = channels["fcm"][0]["message"]["data"]
    assert data["title"] == "Davis: Pressure falling fast"
    assert data["body"].endswith(" Also seen at Tempest.")
    assert data["tier"] == "watch" and data["server_id"] == SID
    assert "notification" not in channels["fcm"][0]["message"]
    assert len(channels["email"]) == 1
    assert channels["email"][0][0] == "[Zasder Weather] Davis: Pressure falling fast"
    assert "Also seen at Tempest." in channels["email"][0][1]
    assert len(channels["webhook"]) == 1
    assert channels["webhook"][0][:2] == ("pressure_drop", DAVIS)
    assert len(history) == 1
    assert history[0]["mac"] == DAVIS
    assert history[0]["body"].endswith(" Also seen at Tempest.")


# ── per-sensor alerts stay per station ───────────────────────────────────

def test_battery_and_hardware_alerts_stay_per_station(client, monkeypatch):
    from app import health_watch
    sent = []

    async def deliver(cfg, subject, body, pt, pb, **kw):
        sent.append((kw.get("kind"), kw.get("mac")))
        return True
    now = int(time.time() * 1000)
    devs = [_dev(DAVIS, "Davis", HOME, battout=0, tempf=70.0, dateutc=now),
            _dev(TEMPEST, "Tempest", NEXT_DOOR, battout=0, tempf=70.0,
                 dateutc=now)]
    asyncio.run(health_watch.check(_cfg(), devs, now, deliver))
    assert sorted(m for k, m in sent if k == "battery") == sorted([DAVIS, TEMPEST])


# ── first frost of the season ────────────────────────────────────────────

def _seasonal(devices, now):
    from app.alerts import AlertMonitor
    return AlertMonitor()._check_seasonal_events(_cfg(), devices, now)


def test_first_frost_once_per_place(client, monkeypatch):
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)

    async def run():
        # Only the Davis is freezing yet.
        await _seasonal([_dev(DAVIS, "Davis", HOME, tempf=32.0, dateutc=now),
                         _dev(TEMPEST, "Tempest", NEXT_DOOR, tempf=34.0,
                              dateutc=now)], now)
        # Twenty minutes on, the Tempest freezes too: the place already
        # had its first frost.
        t = now + 20 * 60_000
        await _seasonal([_dev(DAVIS, "Davis", HOME, tempf=31.0, dateutc=t),
                         _dev(TEMPEST, "Tempest", NEXT_DOOR, tempf=32.0,
                              dateutc=t)], t)
        # Across town is its own place.
        await _seasonal([_dev(ATLAS, "Atlas", ACROSS_TOWN, tempf=30.0,
                              dateutc=t)], t)

    asyncio.run(run())
    assert [s["title"] for s in sent] == [
        "Davis: First frost of the season",
        "Atlas: First frost of the season"]


def test_first_frost_same_tick_names_both(client, monkeypatch):
    sent = _recorder(monkeypatch)
    now = int(time.time() * 1000)
    asyncio.run(_seasonal(
        [_dev(DAVIS, "Davis", HOME, tempf=32.0, dateutc=now),
         _dev(TEMPEST, "Tempest", NEXT_DOOR, tempf=31.0, dateutc=now)], now))
    asyncio.run(_seasonal(
        [_dev(DAVIS, "Davis", HOME, tempf=32.0, dateutc=now),
         _dev(TEMPEST, "Tempest", NEXT_DOOR, tempf=31.0, dateutc=now)],
        now + 60_000))
    assert len(sent) == 1
    assert sent[0]["body"].endswith(" Also seen at Tempest.")


# ── lightning: one episode per place ─────────────────────────────────────

def _zap(mac, name, where, count, dist):
    return _dev(mac, name, where, lightningcount=count,
                lightning_distance_mi=dist)


def _lightning(steps, where_b=NEXT_DOOR, *, limit=None, ok=None, now=None):
    """steps: (minute, (countA, distA), (countB, distB)). Each send is
    (kind, mac, title, body, minute). `limit` is the owner's alert
    distance; `ok(kind, minute)` says whether a delivery lands (default
    all do); `now` pins the clock so a second call continues the first."""
    from app import lightning_watch as lw
    sent = []
    minute_now = [0]

    async def deliver(cfg, subject, body, pt, pb, **kw):
        sent.append((kw.get("kind"), kw.get("mac"), pt, pb, minute_now[0]))
        return True if ok is None else ok(kw.get("kind"), minute_now[0])
    now = int(time.time() * 1000) if now is None else now

    async def go():
        for minute, a, b in steps:
            minute_now[0] = minute
            devs = [_zap(TEMPEST, "Tempest", HOME, *a),
                    _zap(ATLAS, "Shed Tempest", where_b, *b)]
            await lw.check(_cfg(lightning_alert_mi=limit), devs,
                           now + minute * 60_000, deliver)
    asyncio.run(go())
    return sent


def test_two_detectors_in_one_yard_one_alert_and_one_all_clear(client):
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),     # baselines
        (1, (12, 14.0), (7, 15.0)),     # both hear strikes: one alert
        (4, (14, 13.0), (9, 13.5)),     # more at the same range: quiet
        (10, (18, 6.0), (11, 6.5)),     # closer at both: one "closing"
        (39, (18, 6.0), (11, 6.5)),     # 29 quiet minutes
        (41, (18, 6.0), (11, 6.5)),     # 31: one all clear
        (45, (18, 6.0), (11, 6.5)),
    ])
    kinds = [k for k, *_ in sent]
    assert kinds == ["lightning", "lightning", "lightning_clear"], sent
    assert sent[0][2] == "Tempest: Lightning detected"
    assert sent[0][3].endswith(" Also detected at Shed Tempest.")
    assert "closing" in sent[1][2]
    assert sent[2][2] == "Tempest: Lightning all clear"
    assert sent[2][3].endswith(" Also clear at Shed Tempest.")


def test_a_late_detector_joins_and_the_all_clear_waits_for_it(client):
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),
        (1, (12, 14.0), (5, 20.0)),     # only the first hears: the alert
        (3, (12, 14.0), (8, 15.0)),     # the second hears: joins, quiet
        (20, (12, 14.0), (10, 16.0)),   # the second keeps hearing
        (35, (12, 14.0), (10, 16.0)),   # first's 30 min up: quiet
        (49, (12, 14.0), (10, 16.0)),   # second's 29: nothing yet
        (51, (12, 14.0), (10, 16.0)),   # second's 31: the all clear
        (60, (12, 14.0), (10, 16.0)),
    ])
    assert [(k, m) for k, m, *_ in sent] == [
        ("lightning", TEMPEST), ("lightning_clear", ATLAS)], sent
    assert sent[1][3].endswith("has passed.")


def test_closing_is_measured_from_the_nearest_distance_the_place_announced(client):
    """The opening alert announced 8 mi at one detector and 3 mi at the
    other. A strike at 5 mi is farther than the place already said, so it
    is not "closing"; one at 1 mi is."""
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),     # baselines
        (1, (11, 8.0), (6, 3.0)),       # both open: 8 mi and 3 mi announced
        (10, (12, 5.0), (6, 3.0)),      # 5 mi: nearer than 8, not than 3
        (16, (13, 1.0), (6, 3.0)),      # 1 mi: closing
    ])
    assert [(k, m) for k, m, *_ in sent] == [
        ("lightning", TEMPEST), ("lightning", TEMPEST)], sent
    assert "detected" in sent[0][2]
    assert sent[1][2] == "Tempest: Lightning closing" and sent[1][4] == 16


def test_detectors_across_town_keep_their_own_episodes(client):
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),
        (1, (12, 14.0), (7, 15.0)),
        (41, (12, 14.0), (7, 15.0)),
    ], where_b=ACROSS_TOWN)
    assert sorted((k, m) for k, m, *_ in sent) == sorted([
        ("lightning", TEMPEST), ("lightning", ATLAS),
        ("lightning_clear", TEMPEST), ("lightning_clear", ATLAS)])


# ── R27-01 (2.7 review): the all clear runs from the PLACE's last strike ──
# A strike outside the owner's distance never opens or escalates an alert,
# but it is still lightning at the place: once the place's episode is open
# it holds the all clear, whichever station heard it, near, far or with no
# distance. Before the fix, a sibling that had not opened the warning kept
# its strike to itself and the place said "all clear" a minute after it.

def _kinds_at(sent):
    return [(k, m, minute) for k, m, _t, _b, minute in sent]


def test_a_siblings_far_strike_holds_the_places_all_clear(client):
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),     # baselines
        (1, (11, 5.0), (5, 20.0)),      # A at 5 mi inside 15: the alert
        (30, (11, 5.0), (6, 20.0)),     # B at 20 mi: no alert, but lightning
        (31, (11, 5.0), (6, 20.0)),     # A's 30 minutes: B's strike is 1 old
        (59, (11, 5.0), (6, 20.0)),     # B's 29: nothing yet
        (60, (11, 5.0), (6, 20.0)),     # B's 30: the all clear
        (70, (11, 5.0), (6, 20.0)),
    ], limit=15.0)
    assert _kinds_at(sent) == [("lightning", TEMPEST, 1),
                               ("lightning_clear", ATLAS, 60)], sent
    assert sent[1][3].endswith("has passed.")


def test_a_siblings_strike_with_no_distance_holds_the_all_clear(client):
    sent = _lightning([
        (0, (10, 20.0), (5, None)),
        (1, (11, 5.0), (5, None)),
        (20, (11, 5.0), (6, None)),     # B heard one, no distance given
        (31, (11, 5.0), (6, None)),
        (50, (11, 5.0), (6, None)),
    ], limit=15.0)
    assert _kinds_at(sent) == [("lightning", TEMPEST, 1),
                               ("lightning_clear", ATLAS, 50)], sent


def test_a_far_strike_on_the_tick_the_clock_runs_out_holds_it(client):
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),
        (1, (11, 5.0), (5, 20.0)),
        (31, (11, 5.0), (6, 20.0)),     # A's 30 up AND B hears a far strike
        (60, (11, 5.0), (6, 20.0)),
        (61, (11, 5.0), (6, 20.0)),
    ], limit=15.0)
    assert _kinds_at(sent) == [("lightning", TEMPEST, 1),
                               ("lightning_clear", ATLAS, 61)], sent


def test_a_sibling_that_went_quiet_rejoins_on_a_far_strike(client):
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),
        (1, (11, 5.0), (6, 5.0)),       # both open the episode
        (20, (12, 6.0), (6, 5.0)),      # A keeps hearing (not closer)
        (32, (12, 6.0), (6, 5.0)),      # B's 31: B goes quiet, A holds
        (45, (12, 6.0), (7, 25.0)),     # B: a far strike
        (51, (12, 6.0), (7, 25.0)),     # A's 31: B's strike is 6 old
        (74, (12, 6.0), (7, 25.0)),
        (75, (12, 6.0), (7, 25.0)),     # B's 30: the all clear
    ], limit=15.0)
    assert _kinds_at(sent) == [("lightning", TEMPEST, 1),
                               ("lightning_clear", ATLAS, 75)], sent


def test_the_far_joiner_is_stored_so_a_restart_keeps_waiting(client):
    """Every tick reads the episode from server_kv, so a process restart
    between ticks is a call with no memory: the joined sibling must be in
    the stored state, not in a local of the last call."""
    from app import db
    now = int(time.time() * 1000)
    first = _lightning([
        (0, (10, 20.0), (5, 20.0)),
        (1, (11, 5.0), (5, 20.0)),
        (30, (11, 5.0), (6, 20.0)),
    ], limit=15.0, now=now)
    st = json.loads(asyncio.run(db.get_kv(f"lightning_watch.{ATLAS}")))
    assert st["active"] is True and st["last_strike_ms"] == now + 30 * 60_000
    assert "announced_mi" not in st      # it joined; it announced nothing
    second = _lightning([
        (31, (11, 5.0), (6, 20.0)),
        (60, (11, 5.0), (6, 20.0)),
    ], limit=15.0, now=now)
    assert _kinds_at(first) == [("lightning", TEMPEST, 1)]
    assert _kinds_at(second) == [("lightning_clear", ATLAS, 60)], second


def test_a_failed_all_clear_after_a_far_joiner_is_retried_once(client):
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),
        (1, (11, 5.0), (5, 20.0)),
        (30, (11, 5.0), (6, 20.0)),
        (60, (11, 5.0), (6, 20.0)),     # the all clear fails to deliver
        (61, (11, 5.0), (6, 20.0)),     # and goes on the next tick
        (70, (11, 5.0), (6, 20.0)),
    ], limit=15.0, ok=lambda kind, minute: not (kind == "lightning_clear"
                                               and minute == 60))
    # No tick between 30 and 60, so both stations' clocks are up together
    # and the all clear names both, at the first attempt and the retry.
    assert _kinds_at(sent) == [("lightning", TEMPEST, 1),
                               ("lightning_clear", TEMPEST, 60),
                               ("lightning_clear", TEMPEST, 61)], sent
    assert sent[2][3].endswith(" Also clear at Shed Tempest.")


def test_a_far_strike_at_a_quiet_place_still_opens_nothing(client):
    """The distance still gates opening: a sibling's far strike while
    nothing is open is no alert and starts no episode to clear."""
    sent = _lightning([
        (0, (10, 20.0), (5, 20.0)),
        (1, (10, 20.0), (6, 20.0)),
        (40, (10, 20.0), (6, 20.0)),
    ], limit=15.0)
    assert sent == []
