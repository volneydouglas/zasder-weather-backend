"""Crawlspace freeze watch (2.8, Doren 10-27 / 10-31).

His spec: when the forecast holds at or below 27 °F for 6 or more
sustained hours, one notification at sunset of that night, "At or below
27°F could reach 6 sustained hours by Saturday 6:00 AM." Plus live
levels on an optional crawlspace sensor (36 °F watch, 32 °F urgent).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import crawlspace_watch as cw  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
UTC = dt.timezone.utc
HOUR = 3_600_000


def _hours(start: dt.datetime, temps: list[float | None]) -> tuple[list[int], list]:
    t0 = int(start.timestamp() * 1000)
    return [t0 + i * HOUR for i in range(len(temps))], temps


# ───────────────────────── pure decisions ─────────────────────────

def test_a_sustained_run_reports_the_hour_it_reaches_n():
    start = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)          # Friday 10 PM
    times, temps = _hours(start, [30, 28, 27, 26, 25, 25, 26, 27, 29, 33])
    by = cw.sustained_run(times, temps, 27.0, 6)
    # 27 at 00:00, then 26, 25, 25, 26, 27: six hours by 05:00.
    assert by == times[7]
    assert cw.sustained_run(times, temps, 27.0, 7) is None


def test_a_warm_or_missing_hour_breaks_the_run():
    start = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    times, temps = _hours(start, [26, 26, 26, 28, 26, 26, 26, 26, 26, 26])
    assert cw.sustained_run(times, temps, 27.0, 6) == times[9]   # counts again after 28
    assert cw.sustained_run(times[:9], temps[:9], 27.0, 6) is None
    times, temps = _hours(start, [26, 26, 26, None, 26, 26, 26, 26])
    assert cw.sustained_run(times, temps, 27.0, 6) is None   # absent is not cold


def test_a_skipped_hour_breaks_the_run_and_keeps_the_night_open():
    """The provider's hours are not filled in (forecast_hourly.transform),
    so a missing timestamp is a gap (CodeRabbit, PR #130)."""
    h = cw.HOUR_MS
    times = [i * h for i in (0, 1, 2, 4, 5, 6)]           # hour 3 missing
    assert cw.sustained_run(times, [25.0] * 6, 27.0, 6) is None
    assert cw.sustained_run([i * h for i in range(6)], [25.0] * 6, 27.0, 6) == 5 * h
    full = [i * h for i in range(6)]
    assert not cw.complete_hours(times, [35.0] * 6, 0, 6)
    assert not cw.complete_hours(full, [35.0, None] + [35.0] * 4, 0, 6)
    assert cw.complete_hours(full, [35.0] * 6, 0, 6)
    # A truncated or late-starting response does not cover the window.
    assert not cw.complete_hours(full[:4], [35.0] * 4, 0, 6)
    assert not cw.complete_hours([t + h for t in full], [35.0] * 6, 0, 6)


def test_a_warm_complete_forecast_closes_the_night(monkeypatch, temp_env):
    """The other side: a full, known, warm window is decided once, so the
    rest of the sunset window does not fetch again."""
    from app import db
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    assert _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=[35.0] * cw.LOOKAHEAD_H,
                      config={"enabled": True}) == []
    st = json.loads(asyncio.run(db.get_kv(cw.KV_STATE)))
    assert st["noticeNight"] == "2026-11-06"


def test_twc_shifts_the_curve_down_never_up():
    curve = [41.0, 38.0, 36.0, 37.0, None]
    assert cw.anchored(curve, 31.0) == [36.0, 33.0, 31.0, 32.0, None]
    assert cw.anchored(curve, 40.0) == curve        # a warmer opinion hides nothing
    assert cw.anchored(curve, None) == curve
    assert cw.anchored([None, None], 30.0) == [None, None]


def test_twc_anchors_to_the_morning_it_describes_not_a_colder_day_three():
    """TWC's low is tomorrow morning's. A colder dawn at the end of the 36
    hours must not set the shift (CodeRabbit, PR #146)."""
    times, _ = _hours(dt.datetime(2026, 11, 6, 22, tzinfo=UTC), [0] * 4)
    curve = [36.0, 34.0, 33.0, 22.0]                # the last hour is day 3
    cutoff = times[3]                               # noon falls before it
    assert cw.anchored(curve, 27.0, times, cutoff) == [30.0, 28.0, 27.0, 16.0]
    # Read over the whole curve, the cold day-3 hour cancels the shift.
    assert cw.anchored(curve, 27.0) == curve


def test_the_message_is_dorens_wording():
    by = int(dt.datetime(2026, 11, 7, 6, tzinfo=UTC).timestamp() * 1000)
    title, body = cw.message(27.0, 6, by, UTC)
    assert title == "Crawlspace Freeze Monitor"
    assert body == "At or below 27°F could reach 6 sustained hours by Saturday 6:00 AM."


def test_the_check_runs_only_in_the_sunset_window():
    sunset = 1_000_000_000_000
    assert not cw.in_sunset_window(sunset - 1, sunset)
    assert cw.in_sunset_window(sunset, sunset)
    assert cw.in_sunset_window(sunset + cw.SUNSET_WINDOW_MS - 1, sunset)
    assert not cw.in_sunset_window(sunset + cw.SUNSET_WINDOW_MS, sunset)
    assert not cw.in_sunset_window(sunset, None)


def test_a_sensor_level_fires_once_and_rearms_above_it():
    fire, armed = cw.sensor_step(35.0, 36.0, True)
    assert fire and not armed
    assert cw.sensor_step(34.0, 36.0, armed) == (False, False)      # still cold: quiet
    assert cw.sensor_step(37.0, 36.0, False) == (False, False)      # not 2 °F above yet
    assert cw.sensor_step(38.0, 36.0, False) == (False, True)       # re-armed
    assert cw.sensor_step(None, 36.0, True) == (False, True)        # no reading, no opinion


def test_config_cleaning():
    c = cw.Config.from_dict({"enabled": True, "threshold_f": 999, "hours": 0,
                             "sensor_mac": " 5d:5d:09:aa:bb:cc ", "sensor_watch_f": 35})
    assert c.enabled and c.threshold_f == 27.0 and c.hours == 6
    assert c.sensor_mac == "5D:5D:09:AA:BB:CC" and c.sensor_watch_f == 35.0
    assert cw.Config.from_dict("nonsense") == cw.Config()


# ───────────────────────── the tick ─────────────────────────

@pytest.fixture(autouse=True)
def _clean_watch_state(temp_env):
    """Each test starts with no saved config or state (server_kv outlives
    a test otherwise, and one test's sent night silenced the next)."""
    from app import db
    asyncio.run(db.init_db())
    asyncio.run(db.set_kv(cw.KV_STATE, None))
    asyncio.run(db.set_kv(cw.KV_CONFIG, None))
    yield


class _Cfg:
    outlook_source = None
    email_scope = "device_down"


def _run_check(monkeypatch, temp_env, *, now: dt.datetime, sunset: dt.datetime,
               temps: list, config: dict, devices=None, handled=True,
               twc_low=None) -> list[tuple]:
    from app import db, forecast_hourly
    from app import forecast_snapshots as fs
    asyncio.run(db.init_db())
    asyncio.run(db.set_kv(cw.KV_CONFIG, json.dumps(config)))
    start = now.replace(minute=0, second=0, microsecond=0)
    times, _ = _hours(start, temps)

    async def fake_fetch(lat, lon, hours, now_ms=None):
        return {"times_ms": times, "series": {"tempf": temps}, "source": "open-meteo"}

    async def fake_sunset(coords, day):
        return int(sunset.timestamp() * 1000)

    async def fake_twc(cfg, coords, day):
        if isinstance(twc_low, Exception):
            raise twc_low
        return twc_low

    monkeypatch.setattr(forecast_hourly, "fetch", fake_fetch)
    monkeypatch.setattr(fs, "coords", lambda devs: (40.3, -79.7))
    monkeypatch.setattr(cw, "_sunset_ms", fake_sunset)
    monkeypatch.setattr(cw, "_twc_low", fake_twc)
    sent: list[tuple] = []

    async def deliver(cfg, subject, body, push_title, push_body, email_ok=True, **kw):
        sent.append((push_title, push_body, kw.get("severity"), kw.get("kind"), email_ok))
        return handled

    asyncio.run(cw.check(_Cfg(), devices or [], int(now.timestamp() * 1000), deliver))
    return sent


COLD = [30, 28, 27, 26, 25, 25, 26, 27, 29, 33]


def _sensor(mac, tempf, at):
    return [{"mac": mac, "name": "Crawlspace",
             "lastData": {"dateutc": int(at.timestamp() * 1000), "tempinf": tempf}}]


def test_one_notice_at_sunset_then_quiet_the_rest_of_the_night(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    sent = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=COLD, config={"enabled": True})
    assert len(sent) == 1
    title, body, severity, kind, email_ok = sent[0]
    assert title == "Crawlspace Freeze Monitor" and severity == "watch" and kind == "crawlspace"
    assert body.startswith("At or below 27°F could reach 6 sustained hours by Saturday")
    assert email_ok is False                       # email_scope device_down
    again = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=30),
                       sunset=sunset, temps=COLD, config={"enabled": True})
    assert again == []


def test_nothing_before_sunset_or_when_off_or_warm(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    assert _run_check(monkeypatch, temp_env, now=sunset - dt.timedelta(minutes=5),
                      sunset=sunset, temps=COLD, config={"enabled": True}) == []
    assert _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=COLD, config={"enabled": False}) == []
    assert _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=[35] * 10, config={"enabled": True}) == []


def test_a_failed_send_is_retried_inside_the_window(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    first = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                       sunset=sunset, temps=COLD, config={"enabled": True}, handled=False)
    assert len(first) == 1
    retry = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=10),
                       sunset=sunset, temps=COLD, config={"enabled": True})
    assert len(retry) == 1


def test_twc_low_turns_a_mild_open_meteo_night_into_a_notice(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    mild = [36, 35, 34, 33, 33, 33, 34, 35, 37, 40]       # Open-Meteo, warm as on 10-17
    assert _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=mild, config={"enabled": True}) == []
    from app import db
    asyncio.run(db.set_kv(cw.KV_STATE, None))
    # TWC says 24 for the morning: the curve moves down 9 °F and holds at
    # or below 27 from the first hour.
    sent = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=mild, config={"enabled": True}, twc_low=24.0)
    assert len(sent) == 1


def test_the_sensor_levels_fire_once_each(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    noon = dt.datetime(2026, 11, 6, 12, tzinfo=UTC)          # outside the sunset window
    mac = "5D:5D:09:AA:BB:CC"
    cfg = {"enabled": True, "sensor_mac": mac}
    dev = lambda t: _sensor(mac, t, noon)  # noqa: E731
    s1 = _run_check(monkeypatch, temp_env, now=noon, sunset=sunset, temps=COLD,
                    config=cfg, devices=dev(35.0))
    assert [(x[0], x[2]) for x in s1] == [("Crawlspace: 35°F", "watch")]
    s2 = _run_check(monkeypatch, temp_env, now=noon, sunset=sunset, temps=COLD,
                    config=cfg, devices=dev(34.0))
    assert s2 == []                                         # still the same cold
    s3 = _run_check(monkeypatch, temp_env, now=noon, sunset=sunset, temps=COLD,
                    config=cfg, devices=dev(31.5))
    assert [(x[0], x[2]) for x in s3] == [("Crawlspace: 32°F", "warning")]


def test_straight_to_the_urgent_level_sends_only_the_urgent_one(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    noon = dt.datetime(2026, 11, 6, 12, tzinfo=UTC)
    mac = "5D:5D:09:AA:BB:CC"
    sent = _run_check(monkeypatch, temp_env, now=noon, sunset=sunset, temps=COLD,
                      config={"enabled": True, "sensor_mac": mac},
                      devices=_sensor(mac, 30.0, noon))
    assert [x[2] for x in sent] == ["warning"]


def test_a_sensor_level_re_arms_two_degrees_above_it(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    noon = dt.datetime(2026, 11, 6, 12, tzinfo=UTC)
    mac = "5D:5D:09:AA:BB:CC"
    cfg = {"enabled": True, "sensor_mac": mac}

    def tick(t):
        return [x[2] for x in _run_check(monkeypatch, temp_env, now=noon, sunset=sunset,
                                         temps=COLD, config=cfg, devices=_sensor(mac, t, noon))]
    assert tick(35.0) == ["watch"]
    assert tick(37.0) == []          # not yet 2 °F above 36
    assert tick(35.5) == []          # so this is the same cold spell
    assert tick(38.0) == []          # re-armed
    assert tick(35.0) == ["watch"]   # a new spell


def test_a_stale_sensor_reading_is_not_acted_on(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    noon = dt.datetime(2026, 11, 6, 12, tzinfo=UTC)
    mac = "5D:5D:09:AA:BB:CC"
    sent = _run_check(monkeypatch, temp_env, now=noon, sunset=sunset, temps=COLD,
                      config={"enabled": True, "sensor_mac": mac},
                      devices=_sensor(mac, 30.0, noon - dt.timedelta(hours=2)))
    assert sent == []


def test_a_failed_sensor_send_stays_armed(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    noon = dt.datetime(2026, 11, 6, 12, tzinfo=UTC)
    mac = "5D:5D:09:AA:BB:CC"
    cfg = {"enabled": True, "sensor_mac": mac}
    first = _run_check(monkeypatch, temp_env, now=noon, sunset=sunset, temps=COLD,
                       config=cfg, devices=_sensor(mac, 30.0, noon), handled=False)
    assert [x[2] for x in first] == ["warning"]
    retry = _run_check(monkeypatch, temp_env, now=noon, sunset=sunset, temps=COLD,
                       config=cfg, devices=_sensor(mac, 30.0, noon))
    assert [x[2] for x in retry] == ["warning"]


# ───────────────────────── API + backup ─────────────────────────

def test_api_is_owner_only_and_validates(client):
    assert client.get("/api/alerts/crawlspace").status_code == 401
    r = client.get("/api/alerts/crawlspace", headers=H)
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["threshold_f"] == 27.0
    guest = client.post("/api/guest-tokens", headers=H,
                        json={"label": "crawl"}).json()["token"]
    G = {"Authorization": f"Bearer {guest}"}
    assert client.put("/api/alerts/crawlspace", headers=G, json={"enabled": True}).status_code == 403
    r = client.put("/api/alerts/crawlspace", headers=H,
                   json={"enabled": True, "threshold_f": 25, "hours": 8})
    assert r.status_code == 200 and r.json()["hours"] == 8 and r.json()["threshold_f"] == 25.0
    assert client.put("/api/alerts/crawlspace", headers=H,
                      json={"sensor_mac": "AA:BB:CC:00:00:99"}).status_code == 404
    assert client.put("/api/alerts/crawlspace", headers=H,
                      json={"sensor_urgent_f": 40, "sensor_watch_f": 36}).status_code == 400
    assert client.put("/api/alerts/crawlspace", headers=H, json={"hours": 99}).status_code == 422
    # Partial update kept the rest.
    assert client.get("/api/alerts/crawlspace", headers=H).json()["threshold_f"] == 25.0


def test_the_config_backup_carries_the_watch(client):
    client.put("/api/alerts/crawlspace", headers=H,
               json={"enabled": True, "threshold_f": 24, "hours": 7})
    exported = client.get("/api/config/backup", headers=H).json()
    assert exported["crawlspace_watch"]["threshold_f"] == 24.0
    client.put("/api/alerts/crawlspace", headers=H, json={"enabled": False, "hours": 3})
    r = client.post("/api/config/restore", headers=H, json=exported)
    assert r.status_code == 200, r.text
    back = client.get("/api/alerts/crawlspace", headers=H).json()
    assert back["enabled"] is True and back["hours"] == 7 and back["threshold_f"] == 24.0


def test_a_failing_earlier_alert_check_does_not_skip_the_watch(client, monkeypatch):
    """The watch runs first after the alerts gate, so an exception in one
    of the unguarded checks after it cannot skip a freeze night
    (CodeRabbit, PR #130)."""
    from app import alerts
    ran: list[int] = []

    async def push_on():
        return True

    async def boom(self, *a, **k):
        raise RuntimeError("threshold rules down")

    async def watch(cfg, devices, now_ms, deliver):
        ran.append(now_ms)
    monkeypatch.setattr(alerts.apns, "push_configured", push_on)
    monkeypatch.setattr(alerts.AlertMonitor, "_check_threshold_rules", boom)
    monkeypatch.setattr(alerts.AlertMonitor, "_check_storm_summaries", boom)
    monkeypatch.setattr(cw, "check", watch)
    try:
        asyncio.run(alerts.AlertMonitor()._tick())
    except RuntimeError:
        pass
    assert len(ran) == 1


def test_an_empty_forecast_is_asked_again_not_called_warm(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    # A gap is unknown, not warm: the night stays open too.
    for empty in ([], [None] * 10, [35, 35, None, None, 35, 35, 35, 35, 35, 35]):
        assert _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                          sunset=sunset, temps=empty, config={"enabled": True}) == []
    sent = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=10),
                      sunset=sunset, temps=COLD, config={"enabled": True})
    assert len(sent) == 1


def test_a_failed_twc_lookup_still_sends_from_the_hourly_curve(monkeypatch, temp_env):
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    sent = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=COLD, config={"enabled": True},
                      twc_low=RuntimeError("upstream down"))
    assert len(sent) == 1


def test_a_failed_twc_lookup_on_a_warm_curve_keeps_the_night_open(monkeypatch, temp_env):
    """A warm Open-Meteo night is not decided while TWC could not be
    asked: a later tick inside the window tries it again."""
    from app import db
    sunset = dt.datetime(2026, 11, 6, 22, tzinfo=UTC)
    warm = [35] * cw.LOOKAHEAD_H
    assert _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=5),
                      sunset=sunset, temps=warm, config={"enabled": True},
                      twc_low=RuntimeError("upstream down")) == []
    st = json.loads(asyncio.run(db.get_kv(cw.KV_STATE)) or "{}")
    assert st.get("noticeNight") is None
    sent = _run_check(monkeypatch, temp_env, now=sunset + dt.timedelta(minutes=20),
                      sunset=sunset, temps=warm, config={"enabled": True}, twc_low=24.0)
    assert len(sent) == 1
