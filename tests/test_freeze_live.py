"""2.3 item 5: the Freeze Night Live Activity — the two openers (forecast
low, station falling), the froze latch, the 15-minute cadence, the three
ends (sunrise + 1 h, warmed for an hour, stale), with the wire contract
pinned as exact content-state dicts."""
from __future__ import annotations

import asyncio
import datetime as _dt
from types import SimpleNamespace

AUTH = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:61"
# 2026-01-15 20:00 UTC — the suite's timezone is UTC, so this is a real
# evening on the station's clock with no monkeypatching of the hour.
T0 = int(_dt.datetime(2026, 1, 15, 20, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)
NOON = int(_dt.datetime(2026, 1, 15, 12, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)
NIGHT_DATE = "2026-01-16"
MIN = 60_000
SUNRISE = T0 + 11 * 60 * MIN


def _cfg(**kw):
    base = dict(freeze_live_activity=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_apns(monkeypatch, sent: int = 1):
    from app import apns
    calls = {"start": [], "update": []}

    async def fake_start(payload, title, body, **kw):
        calls["start"].append((kw.get("activity"), payload))
        return {"sent": sent, "dead": [], "failed": 0}

    async def fake_update(activity, payload, title, body, **kw):
        calls["update"].append((activity, payload))
        return {"sent": 1, "dead": [], "failed": 0}

    monkeypatch.setattr(apns, "send_live_activity_start", fake_start)
    monkeypatch.setattr(apns, "send_live_activity_update", fake_update)
    return calls


def _pin_sunrise(monkeypatch, ms=SUNRISE):
    from app import freeze_live as fl
    monkeypatch.setattr(fl, "_next_sunrise_ms", lambda coords, now_ms: ms)


def _dev(now_ms, tempf=None, obs_age_ms=0):
    last = {"dateutc": now_ms - obs_age_ms, "baromrelin": 29.9,
            "windspeedmph": 2.0}
    if tempf is not None:
        last["tempf"] = tempf
    return [{"mac": MAC, "name": "Yard", "lastData": last}]


def _state():
    from app import freeze_live as fl
    from app import live_state as ls
    return asyncio.run(ls.load(fl._KV_PREFIX + MAC))


def _forecast_low(low_f: float, valid_date: str = NIGHT_DATE, issued_ms: int = T0 - 3 * 60 * MIN):
    from app import db
    asyncio.run(db.insert_forecast_snapshots(
        "open-meteo", issued_ms,
        [{"valid_date": valid_date, "lead_days": 1, "tmax_f": 50.0,
          "tmin_f": low_f, "pop": 0.0, "precip_in": 0.0}]))


def test_freeze_forecast_lifecycle_pins_the_wire_contract(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    cfg = _cfg()
    # Two calls in the archive: the newest low wins.
    _forecast_low(30.0, issued_ms=T0 - 9 * 60 * MIN)
    _forecast_low(28.0)

    def tick(m, tempf):
        return fl.check(cfg, _dev(T0 + m * MIN, tempf=tempf), T0 + m * MIN)

    async def run():
        # Evening, 40°F, a 28°F low on the books: opens.
        await tick(0, 40.0)
        # Inside the 15-minute gap: silent, the low still tracks.
        await tick(5, 38.0)
        # Past the gap: a cadence beat.
        await tick(16, 34.0)
        # It froze: the flip pushes at once.
        await tick(20, 31.0)
        # Colder still, inside the gap: silent; the minimum tracks.
        await tick(25, 30.0)
        # An hour past sunrise: the night is over. froze stays latched.
        await tick(11 * 60 + 60, 33.0)

    asyncio.run(run())

    assert len(calls["start"]) == 1
    activity, payload = calls["start"][0]
    assert activity == "freeze"
    aps = payload["aps"]
    assert aps["attributes-type"] == "FreezeActivityAttributes"
    assert aps["attributes"] == {"station": "Yard", "mac": MAC}
    assert aps["content-state"] == {
        "tempf": 40.0, "lowF": 28.0, "sunriseMs": SUNRISE, "openedMs": T0,
        "froze": False, "minF": 40.0, "ended": False}

    ups = [p["aps"] for a, p in calls["update"] if a == "freeze"]
    assert [u["event"] for u in ups] == ["update", "update", "end"]
    assert ups[0]["content-state"] == {
        "tempf": 34.0, "lowF": 28.0, "sunriseMs": SUNRISE, "openedMs": T0,
        "froze": False, "minF": 34.0, "ended": False}
    assert ups[1]["content-state"] == {
        "tempf": 31.0, "lowF": 28.0, "sunriseMs": SUNRISE, "openedMs": T0,
        "froze": True, "minF": 31.0, "ended": False}
    # The end beat is the only one that says so: the card reads "night
    # over" for the linger, not "Freeze tonight".
    assert ups[2]["content-state"] == {
        "tempf": 33.0, "lowF": 28.0, "sunriseMs": SUNRISE, "openedMs": T0,
        "froze": True, "minF": 30.0, "ended": True}
    assert ups[2]["dismissal-date"] == (T0 + (11 * 60 + 90) * MIN) // 1000
    assert "openedMs" not in _state()


def test_freeze_station_path_opens_on_a_fall_and_ends_when_warm(client, monkeypatch):
    """No forecast on the books: the station's own reading, 36°F or colder
    and falling over the hour, opens the card with a null forecast low.
    An hour above 36°F ends it."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    cfg = _cfg()

    def tick(m, tempf):
        return fl.check(cfg, _dev(T0 + m * MIN, tempf=tempf), T0 + m * MIN)

    async def run():
        await tick(0, 40.0)
        await tick(15, 39.0)
        await tick(30, 38.0)
        # 36 and falling, but the anchor is only 30 minutes old: not yet.
        await tick(30, 36.0)
        assert calls["start"] == []
        # 50 minutes of history, 40 → 36: opens.
        await tick(50, 36.0)
        assert len(calls["start"]) == 1
        # Warms past 36 and stays there for an hour: ends.
        await tick(60, 37.0)
        await tick(90, 38.0)
        await tick(119, 38.0)
        assert not [1 for a, p in calls["update"] if p["aps"]["event"] == "end"]
        await tick(120, 38.5)

    asyncio.run(run())
    assert calls["start"][0][1]["aps"]["content-state"] == {
        "tempf": 36.0, "lowF": None, "sunriseMs": SUNRISE, "openedMs": T0 + 50 * MIN,
        "froze": False, "minF": 36.0, "ended": False}
    ups = [p["aps"] for a, p in calls["update"]]
    assert ups[-1]["event"] == "end"
    assert ups[-1]["content-state"] == {
        "tempf": 38.5, "lowF": None, "sunriseMs": SUNRISE,
        "openedMs": T0 + 50 * MIN, "froze": False, "minF": 36.0, "ended": True}
    assert "openedMs" not in _state()


def test_freeze_only_opens_at_night(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _forecast_low(20.0, valid_date="2026-01-15", issued_ms=NOON - 60 * MIN)
    _forecast_low(20.0, valid_date="2026-01-16", issued_ms=NOON - 60 * MIN)
    asyncio.run(fl.check(_cfg(), _dev(NOON, tempf=30.0), NOON))
    assert calls["start"] == []


def test_freeze_a_mild_forecast_and_a_steady_reading_open_nothing(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    cfg = _cfg()
    _forecast_low(38.0)

    async def run():
        for m in (0, 15, 30, 50, 60):
            await fl.check(cfg, _dev(T0 + m * MIN, tempf=35.0), T0 + m * MIN)

    asyncio.run(run())
    assert calls["start"] == []


def test_freeze_refuses_absent_temperature(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _forecast_low(20.0)
    asyncio.run(fl.check(_cfg(), _dev(T0), T0))
    assert calls["start"] == [] and _state() == {}


def test_freeze_pref_gate(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _forecast_low(20.0)
    asyncio.run(fl.check(_cfg(freeze_live_activity=False), _dev(T0, tempf=30.0), T0))
    assert calls["start"] == [] and _state() == {}


def test_freeze_never_opens_stale_and_ends_when_stale(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    from app import live_state as ls
    cfg = _cfg()
    _forecast_low(20.0)
    old = ls.STALE_MS + MIN

    async def run():
        await fl.check(cfg, _dev(T0, tempf=30.0, obs_age_ms=old), T0)
        assert calls["start"] == []
        await fl.check(cfg, _dev(T0 + MIN, tempf=30.0), T0 + MIN)
        assert len(calls["start"]) == 1
        await fl.check(cfg, _dev(T0 + 3 * MIN, tempf=30.0, obs_age_ms=old), T0 + 3 * MIN)

    asyncio.run(run())
    ups = [p["aps"] for a, p in calls["update"]]
    assert len(ups) == 1 and ups[0]["event"] == "end"
    assert ups[0]["content-state"]["froze"] is True
    assert "openedMs" not in _state()


def test_freeze_push_gap_holds_unless_froze_flips(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    cfg = _cfg()
    _forecast_low(25.0)

    async def run():
        await fl.check(cfg, _dev(T0, tempf=33.0), T0)
        for m in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14):
            await fl.check(cfg, _dev(T0 + m * MIN, tempf=33.0 - m * 0.05), T0 + m * MIN)
        assert calls["update"] == []
        await fl.check(cfg, _dev(T0 + 14 * MIN + 1, tempf=32.0), T0 + 14 * MIN + 1)

    asyncio.run(run())
    assert len(calls["update"]) == 1
    assert calls["update"][0][1]["aps"]["content-state"]["froze"] is True


def test_freeze_sunrise_helper_uses_the_almanac(client):
    """The real helper: a future sunrise for a real place, None without
    coordinates, and the archive read that backs the forecast opener."""
    from app import forecast_snapshots as fs
    from app import freeze_live as fl
    rise = fl._next_sunrise_ms((33.3, -111.8), T0)
    assert rise is not None and T0 < rise < T0 + 36 * 60 * MIN
    assert fl._next_sunrise_ms(None, T0) is None
    assert asyncio.run(fs.latest_low_f("open-meteo", NIGHT_DATE)) is None
    _forecast_low(29.5)
    assert asyncio.run(fs.latest_low_f("open-meteo", NIGHT_DATE)) == 29.5
    assert fl._night_date(T0).isoformat() == NIGHT_DATE
    assert fl._night_date(T0 + 6 * 60 * MIN).isoformat() == NIGHT_DATE


def test_freeze_pref_round_trip(client):
    r = client.get("/api/alerts", headers=AUTH).json()
    assert r["freeze_live_activity"] is True
    assert client.put("/api/alerts", headers=AUTH,
                      json={"freeze_live_activity": False}).status_code == 200
    assert client.get("/api/alerts", headers=AUTH).json()["freeze_live_activity"] is False


def test_freeze_without_a_sunrise_ends_in_the_morning(client, monkeypatch):
    """No coordinates anywhere: `sunriseMs` is null on the wire and there
    is no sunrise to end on. The clock ends the night at 07:00 local
    instead — a probe found such a card still open 17 hours later."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch, None)
    from app import freeze_live as fl
    _forecast_low(28.0)
    cfg = _cfg()

    def tick(m, tempf):
        return fl.check(cfg, _dev(T0 + m * MIN, tempf=tempf), T0 + m * MIN)

    async def run():
        await tick(0, 34.0)                          # 20:00, opens on the forecast
        assert len(calls["start"]) == 1
        # Through the night and past nominal dawn: 06:59 is still open.
        for m in (6 * 60, 10 * 60, 10 * 60 + 59):
            await tick(m, 30.0)
        assert not [1 for a, p in calls["update"] if p["aps"]["event"] == "end"]
        await tick(11 * 60, 31.0)                    # 07:00: the night is over
        ends = [p["aps"] for a, p in calls["update"] if p["aps"]["event"] == "end"]
        assert len(ends) == 1
        assert ends[0]["content-state"] == {
            "tempf": 31.0, "lowF": 28.0, "sunriseMs": None, "openedMs": T0,
            "froze": True, "minF": 30.0, "ended": True}

    asyncio.run(run())
    assert calls["start"][0][1]["aps"]["content-state"]["sunriseMs"] is None
    assert "openedMs" not in _state()


def test_freeze_with_a_sunrise_does_not_end_on_the_clock(client, monkeypatch):
    """The sunrise rule still governs when there is one: a late winter
    sunrise past 07:00 keeps the card until an hour after it."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch, T0 + (11 * 60 + 30) * MIN)   # 07:30
    from app import freeze_live as fl
    _forecast_low(28.0)
    cfg = _cfg()

    def tick(m, tempf):
        return fl.check(cfg, _dev(T0 + m * MIN, tempf=tempf), T0 + m * MIN)

    async def run():
        await tick(0, 34.0)
        await tick(11 * 60, 30.0)                    # 07:00: still open
        await tick(12 * 60 + 15, 31.0)               # 08:15: still open
        assert not [1 for a, p in calls["update"] if p["aps"]["event"] == "end"]
        await tick(12 * 60 + 30, 32.0)               # 08:30: sunrise + 1 h
        assert [1 for a, p in calls["update"] if p["aps"]["event"] == "end"]

    asyncio.run(run())
