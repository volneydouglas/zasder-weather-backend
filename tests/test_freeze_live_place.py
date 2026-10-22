"""2.7.1: the freeze card arrives in the evening, once per place.

Doren, 2026-10-19, after two freeze nights (station lows 30.6 and
30.8 °F): the evening card never opened, because it read only the
Open-Meteo low (36.2 / 41.3) while his chosen source, TWC, said 31 / 32;
and his Davis and Tempest, 34 m apart, each opened their own card, both
showing the Tempest's numbers ("if I get the alert at 3am that it might
frost at 6am, I'm not getting out of bed to protect our plants").

Pinned here:
  · one card per place (app/places.py), led by the place's first fresh
    station, the smart alerts' rule; the station trigger hears every
    member;
  · tonight's low from the owner's outlook source, Open-Meteo only when
    that source has no usable call, and the card shows the low it used;
  · "Frost possible tonight" at 34 °F and under, "Freeze tonight" at 32;
  · the upgrade from 2.7.0's per-station keys, so a card running across
    the upgrade neither doubles nor orphans.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from types import SimpleNamespace

import pytest

MIN = 60_000
HOUR = 60 * MIN
# 2026-01-15 20:00 UTC: an evening on the suite's (UTC) clock.
T0 = int(_dt.datetime(2026, 1, 15, 20, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)
NIGHT_DATE = "2026-01-16"
SUNRISE = T0 + 11 * HOUR

HOME = (33.3062, -111.8413)
NEXT_DOOR = (33.3066, -111.8413)      # ~45 m: the same yard
ACROSS_TOWN = (33.3562, -111.8413)    # ~5.6 km: its own place

DAVIS = "AA:BB:CC:00:71:01"
TEMPEST = "AA:BB:CC:00:71:02"
FARM = "AA:BB:CC:00:71:03"


def _cfg(**kw):
    base = dict(freeze_live_activity=True, outlook_source=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_apns(monkeypatch):
    from app import apns
    calls = {"start": [], "update": []}

    async def fake_start(payload, title, body, **kw):
        calls["start"].append({"title": title, "body": body,
                               "aps": payload["aps"]})
        return {"sent": 1, "dead": [], "failed": 0}

    async def fake_update(activity, payload, title, body, **kw):
        calls["update"].append({"activity": activity, "aps": payload["aps"],
                                "title": title, "body": body})
        return {"sent": 1, "dead": [], "failed": 0}

    monkeypatch.setattr(apns, "send_live_activity_start", fake_start)
    monkeypatch.setattr(apns, "send_live_activity_update", fake_update)
    return calls


def _pin_sunrise(monkeypatch, ms=SUNRISE):
    from app import freeze_live as fl
    monkeypatch.setattr(fl, "_next_sunrise_ms", lambda coords, now_ms: ms)


def _dev(mac, name, where, now_ms, tempf=None, obs_age_ms=0):
    last = {"dateutc": now_ms - obs_age_ms, "baromrelin": 29.9,
            "windspeedmph": 2.0}
    if tempf is not None:
        last["tempf"] = tempf
    d = {"mac": mac, "name": name, "lastData": last}
    if where is not None:
        d["info"] = {"coords": {"location": None,
                                "coords": {"lat": where[0], "lon": where[1]}}}
    return d


def _yard(now_ms, davis=None, tempest=None, davis_age=0, tempest_age=0):
    """Doren's shape: a Davis and a Tempest in one yard, Davis first."""
    return [_dev(DAVIS, "Davis", HOME, now_ms, davis, davis_age),
            _dev(TEMPEST, "Tempest", NEXT_DOOR, now_ms, tempest, tempest_age)]


def _low(provider, low_f, valid_date=NIGHT_DATE, issued_ms=T0 - 3 * HOUR):
    from app import db
    asyncio.run(db.insert_forecast_snapshots(
        provider, issued_ms,
        [{"valid_date": valid_date, "lead_days": 1, "tmax_f": 50.0,
          "tmin_f": low_f, "pop": 0.0, "precip_in": 0.0}]))


def _load(mac):
    from app import freeze_live as fl
    from app import live_state as ls
    return asyncio.run(ls.load(fl._KV_PREFIX + mac))


def _save(mac, state):
    from app import freeze_live as fl
    from app import live_state as ls
    asyncio.run(ls.save(fl._KV_PREFIX + mac, state))


def _ends(calls):
    return [u for u in calls["update"] if u["aps"]["event"] == "end"]


# ── one card per place ───────────────────────────────────────────────────

def test_two_stations_in_one_yard_open_one_card_led_by_the_first(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 28.0)
    cfg = _cfg()

    async def run():
        await fl.check(cfg, _yard(T0, davis=34.0, tempest=33.4), T0)
        await fl.check(cfg, _yard(T0 + 16 * MIN, davis=33.0, tempest=32.6),
                       T0 + 16 * MIN)
        await fl.check(cfg, _yard(SUNRISE + HOUR, davis=33.5, tempest=33.1),
                       SUNRISE + HOUR)

    asyncio.run(run())
    assert len(calls["start"]) == 1
    aps = calls["start"][0]["aps"]
    assert aps["attributes"] == {"station": "Davis", "mac": DAVIS}
    assert aps["content-state"]["tempf"] == 34.0
    routine = [u for u in calls["update"] if u["aps"]["event"] == "update"]
    # One update per beat, carrying the lead's numbers only.
    assert [u["aps"]["content-state"]["tempf"] for u in routine] == [33.0]
    assert len(_ends(calls)) == 1
    assert _ends(calls)[0]["body"].startswith("Davis: ")
    # The place's state lives under its first station's key; the sibling
    # holds none.
    assert _load(TEMPEST) == {}


def test_stations_across_town_each_get_their_own_card(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 28.0)
    devs = [_dev(DAVIS, "Davis", HOME, T0, 34.0),
            _dev(FARM, "Farm", ACROSS_TOWN, T0, 31.0)]
    asyncio.run(fl.check(_cfg(), devs, T0))
    assert sorted(c["aps"]["attributes"]["station"] for c in calls["start"]) == [
        "Davis", "Farm"]


def test_the_station_trigger_hears_every_member_and_the_trigger_leads(
        client, monkeypatch):
    """No forecast on the books. The Davis (first) sits at a steady 37 °F;
    the Tempest falls from 40 to 35.5 over 50 minutes. The place opens on
    the Tempest, and the card is the Tempest's: the station that crossed
    is the one whose numbers explain why it opened (the smart alerts'
    rule: the first station that spoke leads)."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    cfg = _cfg()

    async def run():
        for m, t in ((0, 40.0), (15, 39.0), (30, 37.5)):
            await fl.check(cfg, _yard(T0 + m * MIN, davis=37.0, tempest=t),
                           T0 + m * MIN)
        assert calls["start"] == []
        await fl.check(cfg, _yard(T0 + 50 * MIN, davis=37.0, tempest=35.5),
                       T0 + 50 * MIN)

    asyncio.run(run())
    assert len(calls["start"]) == 1
    start = calls["start"][0]
    assert start["title"] == "Freeze watch"
    assert start["aps"]["attributes"] == {"station": "Tempest", "mac": TEMPEST}
    assert start["aps"]["content-state"]["tempf"] == 35.5
    assert start["aps"]["content-state"]["lowF"] is None


def test_a_lead_that_goes_quiet_hands_the_card_to_a_fresh_sibling(
        client, monkeypatch):
    """The Davis drops off the network at 2 AM; the Tempest is still
    reporting from the same yard. The card carries on with the Tempest's
    reading instead of ending at the coldest hour, and ends as stale
    only when the whole place has gone quiet."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    from app import live_state as ls
    _low("open-meteo", 28.0)
    cfg = _cfg()
    quiet = ls.STALE_MS + MIN

    async def run():
        await fl.check(cfg, _yard(T0, davis=34.0, tempest=33.8), T0)
        t1 = T0 + 6 * HOUR
        await fl.check(cfg, _yard(t1, davis=31.0, tempest=30.2,
                                  davis_age=quiet), t1)
        assert _ends(calls) == []
        t2 = t1 + 20 * MIN
        await fl.check(cfg, _yard(t2, davis=31.0, tempest=29.9,
                                  davis_age=quiet, tempest_age=quiet), t2)

    asyncio.run(run())
    routine = [u for u in calls["update"] if u["aps"]["event"] == "update"]
    assert [u["aps"]["content-state"]["tempf"] for u in routine] == [30.2]
    assert routine[0]["aps"]["content-state"]["froze"] is True
    assert len(_ends(calls)) == 1
    assert "openedMs" not in _load(DAVIS)


def test_an_air_monitor_never_joins_or_leads_the_place(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 28.0)
    # Listed first and in the yard: without the skip it would lead.
    air = _dev("5D:5D:07:00:00:01", "AirGradient", HOME, T0, 30.0)
    asyncio.run(fl.check(_cfg(), [air] + _yard(T0, davis=34.0, tempest=33.0), T0))
    assert len(calls["start"]) == 1
    assert calls["start"][0]["aps"]["attributes"]["station"] == "Davis"


# ── the owner's chosen forecast ──────────────────────────────────────────

def test_the_chosen_source_decides_and_its_low_is_on_the_card(client, monkeypatch, caplog):
    """Doren's 10-17 night in miniature: TWC (his source) 31, Open-Meteo
    36.2. The card opens in the evening on TWC's 31, and shows 31."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("twc", 31.0)
    _low("open-meteo", 36.2)
    with caplog.at_level(logging.INFO, logger="freeze-live"):
        asyncio.run(fl.check(_cfg(outlook_source="twc"),
                             _yard(T0, davis=44.0, tempest=44.3), T0))
    assert len(calls["start"]) == 1
    assert calls["start"][0]["title"] == "Freeze tonight"
    assert calls["start"][0]["aps"]["content-state"]["lowF"] == 31.0
    assert "low of 31°F forecast" in calls["start"][0]["body"]
    assert any("from twc" in r.getMessage() for r in caplog.records)


def test_the_chosen_source_wins_even_when_the_fallback_is_colder(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("twc", 38.0)
    _low("open-meteo", 30.0)
    asyncio.run(fl.check(_cfg(outlook_source="twc"),
                         _yard(T0, davis=44.0, tempest=44.0), T0))
    assert calls["start"] == []


def test_open_meteo_is_the_fallback_when_the_chosen_source_has_no_call(
        client, monkeypatch, caplog):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 33.0)
    with caplog.at_level(logging.INFO, logger="freeze-live"):
        asyncio.run(fl.check(_cfg(outlook_source="twc"),
                             _yard(T0, davis=44.0, tempest=44.0), T0))
    assert len(calls["start"]) == 1
    assert calls["start"][0]["aps"]["content-state"]["lowF"] == 33.0
    assert any("from open-meteo" in r.getMessage() for r in caplog.records)


def test_a_day_old_call_from_the_chosen_source_is_not_tonights(client, monkeypatch):
    """A TWC key that stopped working leaves its last calls in the archive.
    A call issued more than a day ago is not a usable forecast for tonight:
    Open-Meteo's current call speaks instead (here, a mild one)."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("twc", 30.0, issued_ms=T0 - 30 * HOUR)
    _low("open-meteo", 36.0)
    asyncio.run(fl.check(_cfg(outlook_source="twc"),
                         _yard(T0, davis=44.0, tempest=44.0), T0))
    assert calls["start"] == []
    # The same call inside the day is used.
    _low("twc", 30.0, issued_ms=T0 - 20 * HOUR)
    asyncio.run(fl.check(_cfg(outlook_source="twc"),
                         _yard(T0 + MIN, davis=44.0, tempest=44.0), T0 + MIN))
    assert len(calls["start"]) == 1
    assert calls["start"][0]["aps"]["content-state"]["lowF"] == 30.0


# ── the bands ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("low, title", [
    (34.0, "Frost possible tonight"),
    (33.0, "Frost possible tonight"),
    (32.6, "Frost possible tonight"),
    (32.4, "Freeze tonight"),
    (32.0, "Freeze tonight"),
    (27.0, "Freeze tonight"),
    (34.5, "Frost possible tonight"),
    (34.6, None),
    (38.0, None),
])
def test_the_frost_and_freeze_bands(client, monkeypatch, low, title):
    """The report line's bands (outlook.frost_band), on the whole degree
    the alert prints: a 32.4 °F low reads "low of 32°F", so it is a
    freeze."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", low)
    asyncio.run(fl.check(_cfg(), _yard(T0, davis=45.0, tempest=45.0), T0))
    if title is None:
        assert calls["start"] == []
        return
    assert [c["title"] for c in calls["start"]] == [title]
    assert calls["start"][0]["aps"]["content-state"]["lowF"] == low


# ── the upgrade from 2.7.0's per-station keys ────────────────────────────

def _old_episode(opened_ms, tempf, *, low=28.0, froze=False, hist=None):
    """A 2.7.0 per-station freeze state, exactly as that version wrote it."""
    return {"hist": hist or [[opened_ms, tempf]], "openedMs": opened_ms,
            "lastPushMs": opened_ms, "lowF": low, "sunriseMs": SUNRISE,
            "froze": froze, "minF": tempf, "lastTempf": tempf,
            "aboveSinceMs": None}


def test_upgrade_with_both_stations_carrying_a_card_keeps_one_stream(
        client, monkeypatch):
    """2.7.0 opened a card per station. After the upgrade the place keeps
    the first station's episode (its opening time, its low so far), the
    sibling's state is folded in and cleared, and no start goes out: the
    phone already has its cards, and every card takes every update."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 28.0)
    _save(DAVIS, _old_episode(T0 - 60 * MIN, 33.0))
    _save(TEMPEST, _old_episode(T0 - 58 * MIN, 32.5))
    cfg = _cfg()

    async def run():
        await fl.check(cfg, _yard(T0, davis=33.2, tempest=32.9), T0)
        await fl.check(cfg, _yard(T0 + 16 * MIN, davis=32.4, tempest=32.1),
                       T0 + 16 * MIN)
        await fl.check(cfg, _yard(SUNRISE + HOUR, davis=34.0, tempest=33.8),
                       SUNRISE + HOUR)

    asyncio.run(run())
    assert calls["start"] == []
    routine = [u for u in calls["update"] if u["aps"]["event"] == "update"]
    assert [u["aps"]["content-state"]["tempf"] for u in routine] == [33.2, 32.4]
    assert all(u["aps"]["content-state"]["openedMs"] == T0 - 60 * MIN
               for u in routine)
    assert len(_ends(calls)) == 1
    assert _load(TEMPEST) == {}


def test_upgrade_with_only_the_second_station_carrying_a_card_adopts_it(
        client, monkeypatch):
    """The Tempest's card is the one running. The place adopts it: no new
    start (that would put a second card on the phone), the Tempest keeps
    leading, and its old key is cleared."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 28.0)
    _save(DAVIS, {"hist": [[T0 - 5 * MIN, 35.0]]})
    _save(TEMPEST, _old_episode(T0 - 40 * MIN, 33.0, froze=False))
    asyncio.run(fl.check(_cfg(), _yard(T0, davis=34.0, tempest=31.5), T0))
    assert calls["start"] == []
    assert len(calls["update"]) == 1
    cs = calls["update"][0]["aps"]["content-state"]
    assert cs["tempf"] == 31.5 and cs["froze"] is True
    assert cs["openedMs"] == T0 - 40 * MIN
    assert _load(TEMPEST) == {}
    place = _load(DAVIS)
    assert place["openedMs"] == T0 - 40 * MIN and place["leadMac"] == TEMPEST
    assert "hist" not in place


def test_upgrade_carries_each_stations_hour_of_history(client, monkeypatch):
    """The falling test needs 45 minutes of history. A station's 2.7.0
    history comes across with it, so a place falling at the moment of the
    upgrade opens on the first tick, not 45 minutes later."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _save(DAVIS, {"hist": [[T0 - 50 * MIN, 37.0], [T0 - 20 * MIN, 37.0]]})
    _save(TEMPEST, {"hist": [[T0 - 50 * MIN, 40.0], [T0 - 20 * MIN, 37.5]]})
    asyncio.run(fl.check(_cfg(), _yard(T0, davis=37.0, tempest=35.5), T0))
    assert len(calls["start"]) == 1
    assert calls["start"][0]["aps"]["attributes"]["mac"] == TEMPEST
    assert _load(TEMPEST) == {}


def test_upgrade_reads_the_first_stations_own_old_history(client, monkeypatch):
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _save(DAVIS, {"hist": [[T0 - 50 * MIN, 40.0]]})
    asyncio.run(fl.check(_cfg(), _yard(T0, davis=35.5, tempest=37.0), T0))
    assert len(calls["start"]) == 1
    assert calls["start"][0]["aps"]["attributes"]["mac"] == DAVIS
    assert "hist" not in _load(DAVIS)


# ── Doren's two nights ───────────────────────────────────────────────────

IRWIN = (40.3245, -79.7012)
IRWIN_NEXT_DOOR = (40.3248, -79.7012)    # ~34 m


def _night(local_evening: _dt.datetime, low_f: float):
    """A synthetic clear, calm night: 50 °F at 16:00 falling steadily to the
    station's real low (`low_f`) at 06:30, then warming. Returns
    [(epoch ms, Davis °F, Tempest °F)] every 5 minutes from 16:00 to 09:00.
    The Tempest reads 0.3 °F warmer (the yard's real spread)."""
    start = local_evening.replace(hour=16, minute=0)
    floor_at = 14.5 * 60
    out = []
    for m in range(0, 17 * 60 + 1, 5):
        if m <= floor_at:
            t = 50.0 - (50.0 - low_f) * m / floor_at
        else:
            t = low_f + (m - floor_at) * 4.0 / 60
        ms = int((start + _dt.timedelta(minutes=m)).timestamp() * 1000)
        out.append((ms, round(t, 1), round(t + 0.3, 1)))
    return out


def _new_york(monkeypatch):
    """Doren's clock. live_state keeps the Settings object it imported
    (the client fixture reloads app.config, not app.live_state), so the
    one it reads is patched alongside the current one."""
    from app import config
    from app import live_state as ls
    monkeypatch.setattr(config.settings, "timezone", "America/New_York")
    monkeypatch.setattr(ls.settings, "timezone", "America/New_York")


def _run_doren_night(monkeypatch, *, evening, valid, twc, om, actual, source):
    from app import db
    from app import freeze_live as fl
    tz = evening.tzinfo
    issued = int(evening.replace(hour=12, minute=0).timestamp() * 1000)
    for prov, low in (("twc", twc), ("open-meteo", om)):
        asyncio.run(db.insert_forecast_snapshots(
            prov, issued, [{"valid_date": valid, "lead_days": 1,
                            "tmax_f": 55.0, "tmin_f": low, "pop": 0.0,
                            "precip_in": 0.0}]))
    rise = evening.replace(hour=7, minute=30) + _dt.timedelta(days=1)
    _pin_sunrise(monkeypatch, int(rise.timestamp() * 1000))
    calls = _fake_apns(monkeypatch)
    cfg = _cfg(outlook_source=source)

    async def run():
        for ms, davis, tempest in _night(evening, actual):
            devs = [_dev(DAVIS, "Davis", IRWIN, ms, davis),
                    _dev(TEMPEST, "Tempest", IRWIN_NEXT_DOOR, ms, tempest)]
            await fl.check(cfg, devs, ms)
    asyncio.run(run())

    def local(ms):
        return _dt.datetime.fromtimestamp(ms / 1000, tz).strftime("%H:%M")
    starts = [(local(c["aps"]["content-state"]["openedMs"]), c["title"],
               c["aps"]["content-state"]["lowF"],
               c["aps"]["attributes"]["station"]) for c in calls["start"]]
    return starts, calls


@pytest.mark.parametrize("evening, valid, twc, om, actual", [
    (_dt.datetime(2026, 10, 17, 16, 0), "2026-10-18", 31.0, 36.2, 30.6),
    (_dt.datetime(2026, 10, 18, 16, 0), "2026-10-19", 32.0, 41.3, 30.8),
])
def test_dorens_two_nights(client, monkeypatch, evening, valid, twc, om, actual):
    """Both of Doren's freeze nights, from the facts verified on his box:
    his source (TWC) said 31 / 32, Open-Meteo 36.2 / 41.3, the station hit
    30.6 / 30.8. With 2.7.1 one card opens at 18:00 on TWC's low. Read
    the way 2.7.0 read it (Open-Meteo), the evening passes with no card,
    and only the station's own fall opens one, deep in the night."""
    from zoneinfo import ZoneInfo
    _new_york(monkeypatch)
    evening = evening.replace(tzinfo=ZoneInfo("America/New_York"))
    starts, calls = _run_doren_night(monkeypatch, evening=evening, valid=valid,
                                     twc=twc, om=om, actual=actual, source="twc")
    assert starts == [("18:00", "Freeze tonight", twc, "Davis")]
    assert len(_ends(calls)) == 1


def test_dorens_first_night_read_the_old_way_opens_only_after_midnight(
        client, monkeypatch):
    from zoneinfo import ZoneInfo
    _new_york(monkeypatch)
    evening = _dt.datetime(2026, 10, 17, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    starts, _ = _run_doren_night(monkeypatch, evening=evening, valid="2026-10-18",
                                 twc=31.0, om=36.2, actual=30.6,
                                 source="open-meteo")
    assert len(starts) == 1
    opened, title, low, _station = starts[0]
    assert title == "Freeze watch" and low == 36.2
    assert "00:00" < opened < "06:00", opened


def test_a_forecast_card_opened_on_a_mild_evening_is_not_warmed_out(
        client, monkeypatch):
    """The forecast opens the card at 18:00 while the air is still in the
    40s, as it is on most freeze nights. "Above 36 for an hour" used to end
    it an hour later, and the next tick opened it again: an alert every 70
    minutes until the air cooled. The warm-up end counts only once the
    night has read 36 °F or under."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 30.0)
    cfg = _cfg()

    async def run():
        for m in range(0, 3 * 60 + 1, 10):
            await fl.check(cfg, _yard(T0 + m * MIN, davis=45.0 - m / 30,
                                      tempest=45.0), T0 + m * MIN)
        assert len(calls["start"]) == 1 and _ends(calls) == []
        t = T0 + 4 * HOUR
        await fl.check(cfg, _yard(t, davis=35.5, tempest=35.5), t)
        for m in (10, 40, 69):
            await fl.check(cfg, _yard(t + m * MIN, davis=38.0, tempest=38.0),
                           t + m * MIN)
        assert _ends(calls) == []
        await fl.check(cfg, _yard(t + 70 * MIN, davis=38.0, tempest=38.0),
                       t + 70 * MIN)

    asyncio.run(run())
    assert len(calls["start"]) == 1
    assert len(_ends(calls)) == 1
    assert _ends(calls)[0]["aps"]["content-state"]["minF"] == 35.5


def test_a_night_that_warmed_out_is_not_reopened_by_the_forecast(
        client, monkeypatch):
    """CodeRabbit, PR #117. The night cooled to 35, then clouds came in and
    it held 38 for an hour: the card ends as "warmed". The forecast still
    says 30, and the next tick opened it again with a new alert. The
    forecast has had its say for this night; a station that cools and
    falls again can still open it, and the place's key carries the mark
    when the place's first station changes."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    _low("open-meteo", 30.0)
    cfg = _cfg()

    async def run():
        await fl.check(cfg, _yard(T0, davis=40.0, tempest=40.0), T0)
        t = T0 + 2 * HOUR
        await fl.check(cfg, _yard(t, davis=35.0, tempest=35.0), t)
        for m in (10, 40, 70, 75):
            await fl.check(cfg, _yard(t + m * MIN, davis=38.0, tempest=38.0),
                           t + m * MIN)
        assert len(_ends(calls)) == 1 and len(calls["start"]) == 1
        # The place's first station changes (the Tempest now listed first):
        # the mark comes across with the fold.
        u = t + 80 * MIN
        await fl.check(cfg, list(reversed(_yard(u, davis=38.0, tempest=38.0))), u)
        assert len(calls["start"]) == 1
        # Clouds clear: 39 falling to 35.5 over 50 minutes opens it again.
        v = t + 3 * HOUR
        for m, temp in ((0, 39.0), (20, 37.5), (50, 35.5)):
            await fl.check(cfg, list(reversed(_yard(v + m * MIN, davis=temp,
                                                    tempest=temp))), v + m * MIN)

    asyncio.run(run())
    assert [c["title"] for c in calls["start"]] == ["Freeze tonight", "Freeze watch"]


def test_a_place_back_from_a_network_drop_gets_its_card_back(client, monkeypatch):
    """The other side of the warmed-out rule: a card that ended because
    every station went quiet leaves no mark, so when the yard reports
    again the forecast puts the card back for the rest of the night."""
    calls = _fake_apns(monkeypatch)
    _pin_sunrise(monkeypatch)
    from app import freeze_live as fl
    from app import live_state as ls
    _low("open-meteo", 30.0)
    cfg = _cfg()
    quiet = ls.STALE_MS + MIN

    async def run():
        await fl.check(cfg, _yard(T0, davis=34.0, tempest=34.0), T0)
        t = T0 + 3 * HOUR
        await fl.check(cfg, _yard(t, davis=33.0, tempest=33.0,
                                  davis_age=quiet, tempest_age=quiet), t)
        assert len(_ends(calls)) == 1
        await fl.check(cfg, _yard(t + 40 * MIN, davis=32.5, tempest=32.5),
                       t + 40 * MIN)

    asyncio.run(run())
    assert [c["title"] for c in calls["start"]] == ["Freeze tonight", "Freeze tonight"]
