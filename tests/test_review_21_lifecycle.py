"""2.1 pre-release review: BE-3 (units on the reports list), BE-4 (every
cloud poller warms up inside its task), BE-5 (a dead station is not
published as current)."""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:D1"


# ───────────── BE-3 ─────────────

def _store_reports(client):
    from app import db, reports as rp

    async def run():
        await db.insert_report(
            kind=rp.KIND_MORNING, mac=None, ts_ms=1_780_000_000_000, for_date="2026-06-01",
            title="Morning report", summary=rp.morning_summary({
                "stations": [{"tmax_f": 93.0, "tmin_f": 70.0, "rain_in": 0.25}],
                "outlook": {"hi_f": 100.0, "precip_pct": 20}, "alerts": []}),
            payload={"stations": [{"tmax_f": 93.0, "tmin_f": 70.0, "rain_in": 0.25}],
                     "outlook": {"hi_f": 100.0, "precip_pct": 20}, "alerts": []},
            dedupe="morning:2026-06-01")
        storm = {"total_in": 1.5, "peak_rate_in_hr": 2.0, "max_gust_mph": 40.0,
                 "temp_drop_f": 21.6}
        await db.insert_report(
            kind=rp.KIND_STORM, mac=MAC, ts_ms=1_780_000_100_000, for_date="2026-06-01",
            title="Storm", summary=rp.storm_summary_line(storm), payload=storm,
            dedupe="storm:x:1")
        noaa = {"high_f": 104.0, "low_f": 62.0, "mean_f": 84.5, "rain_in": 0.5, "days": 30}
        await db.insert_report(
            kind=rp.KIND_NOAA_MONTH, mac=MAC, ts_ms=1_780_000_200_000, for_date="2026-06-01",
            title="June", summary=rp.noaa_summary_line(noaa), payload=noaa,
            dedupe="noaa_month:x:2026-06")
    asyncio.run(run())


def test_the_list_summary_follows_the_readers_units(client):
    """The list row was a server-rendered imperial string; a Celsius reader
    saw "93/70 · 0.25 in rain" beside a 34/21 °C detail page (BE-3)."""
    _store_reports(client)
    native = client.get("/api/reports", headers=H).json()["reports"]
    by_kind = {r["kind"]: r["summary"] for r in native}
    assert by_kind["morning"] == "Yesterday 93/70 · 0.25 in rain · today near 100, 20% rain"
    assert by_kind["storm"] == "1.50 in · peak 2.00 in/hr · gust 40 mph · cooled 22°F"
    assert by_kind["noaa_month"].startswith("High 104, low 62 · mean 84.5 · 0.50 in rain")
    assert all("payload" not in r for r in native)

    metric = client.get("/api/reports?temp_unit=celsius&rain_unit=mm&wind_unit=kph",
                        headers=H).json()["reports"]
    by_kind = {r["kind"]: r["summary"] for r in metric}
    assert by_kind["morning"] == "Yesterday 34/21 · 6.3 mm rain · today near 38, 20% rain"
    # A temperature DROP converts by scale alone: 21.6°F is 12°C, not
    # −5.8°C (round-two BE-N5; the payload carries it now, §5 row 5).
    assert by_kind["storm"] == "38.1 mm · peak 50.8 mm/hr · gust 64 km/h · cooled 12°C"
    assert by_kind["noaa_month"].startswith("High 40, low 17 · mean 29.2 · 12.7 mm rain")
    assert all("payload" not in r for r in metric), "payloads never ride the list"
    assert client.get("/api/reports?temp_unit=kelvin", headers=H).status_code == 400


# ───────────── BE-5 ─────────────

def test_the_upload_carries_the_readings_time_not_the_monitors(client):
    from app import share_targets as st
    now = int(time.time() * 1000)
    obs = {"tempf": 80.0, "dateutc": now - 4 * 60_000}
    when = st._reading_time(obs, now)
    assert when == dt.datetime.fromtimestamp((now - 4 * 60_000) / 1000, dt.timezone.utc)
    assert st._reading_time({"tempf": 80.0}, now).timestamp() * 1000 == pytest.approx(now, abs=1)
    assert st.reading_too_old({"dateutc": now - 4 * 60_000}, now, "pwsweather") is None
    assert st.reading_too_old({"dateutc": now - 11 * 60_000}, now, "pwsweather") == 11
    assert st.reading_too_old({"dateutc": now - 19 * 60_000}, now, "cwop") is None


def test_a_dead_station_is_not_published_as_current(client, monkeypatch):
    """A station that died at 02:00 had that reading uploaded every five
    minutes, indefinitely, with a fresh timestamp (BE-5)."""
    from app import share_targets as st
    calls = []

    async def fake_pws(cfg, obs, now_ms):
        calls.append(obs.get("dateutc"))
        return None
    monkeypatch.setattr(st, "_send_pwsweather", fake_pws)
    now = int(time.time() * 1000)
    asyncio.run(st.set_config("pwsweather", {"enabled": True, "station_id": "X",
                                             "api_key": "Y"}))
    st._reset_for_tests()

    def devices(age_ms, at):
        return [{"mac": "AA", "lastData": {"tempf": 80.0, "dateutc": at - age_ms},
                 "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]

    asyncio.run(st.check(devices(2 * 60_000, now), now))
    assert len(calls) == 1, "a fresh reading publishes"
    t2 = now + 6 * 60_000
    asyncio.run(st.check(devices(30 * 60_000, t2), t2))
    assert len(calls) == 1, "a half-hour-old reading is a dead station, not weather"
    status = asyncio.run(st.get_status("pwsweather"))
    assert "old" in (status["last_error"] or "") and "not published" in status["last_error"]
    t3 = now + 12 * 60_000
    asyncio.run(st.check(devices(60_000, t3), t3))
    assert len(calls) == 2, "the next fresh reading publishes again"


# ───────────── BE-4 ─────────────

class _Hang:
    """A vendor client whose every call blocks until cancelled. `reached`
    is set on the first call, so a test can wait for the poller to be
    inside the vendor instead of sleeping and hoping (round two, I4)."""
    def __init__(self):
        self.calls = 0
        self.reached = asyncio.Event()

    async def _hang(self, *a, **k):
        self.calls += 1
        self.reached.set()
        await asyncio.Event().wait()

    list_devices = device_history = stations = list_stations = _hang
    station_observation = current = device_state = _hang


def _reload_for(*mods):
    import importlib, sys
    for mod in ("app.config", "app.db", "app.ingest", *mods):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


@pytest.mark.parametrize("make", [
    lambda v: __import__("app.poller", fromlist=["Poller"]).Poller(v),
    lambda v: __import__("app.tempest_poller", fromlist=["TempestPoller"]).TempestPoller(v, 1, 60, None),
    lambda v: __import__("app.weatherlink_poller", fromlist=["WeatherlinkPoller"]).WeatherlinkPoller(v, 1, 60),
    lambda v: __import__("app.govee_cloud_poller", fromlist=["GoveeCloudPoller"]).GoveeCloudPoller(v, 60, None, None),
    lambda v: __import__("app.ecowitt_cloud_poller", fromlist=["EcowittCloudPoller"]).EcowittCloudPoller(v, 60, None),
], ids=["ambientweather", "tempest", "weatherlink", "govee", "ecowitt"])
def test_every_cloud_poller_starts_and_stops_without_waiting_on_the_vendor(temp_env, make):
    """One provider had this shape (R18 #6); the other four did vendor I/O
    in start() with a 15 s timeout per call, on the boot path and inside
    the settings PUT (BE-4)."""
    _reload_for()
    from app import db
    asyncio.run(db.init_db())

    async def run():
        vendor = _Hang()                         # built on THIS loop: its
        poller = make(vendor)                    # Event binds to the loop
        t0 = time.monotonic()
        await poller.start()
        started = time.monotonic() - t0
        # A barrier, not a 50 ms sleep a slow runner could outlast: the
        # task is inside the vendor call before stop() is timed.
        await asyncio.wait_for(vendor.reached.wait(), timeout=5)
        t1 = time.monotonic()
        await poller.stop()
        return started, time.monotonic() - t1, poller
    started, stopped, poller = asyncio.run(run())
    assert started < 0.5, f"start waited on the vendor: {started:.2f}s"
    assert stopped < 1.0, f"stop waited on the vendor: {stopped:.2f}s"
    assert poller._task is None


def test_teardown_is_bounded_even_when_a_poller_will_not_stop(client, monkeypatch):
    from app import integrations, poller_lifecycle

    class Stuck:
        async def stop(self):
            await asyncio.Event().wait()
    monkeypatch.setattr(poller_lifecycle, "STOP_TIMEOUT_S", 0.05)
    # A fresh manager: the old hasattr fallback to the process-global one
    # would have parked a Stuck poller in it for every later test.
    mgr = integrations.IntegrationManager()
    mgr._pollers["awn"] = Stuck()

    async def run():
        t0 = time.monotonic()
        # Bounded so a lost teardown timeout fails this test instead of
        # wedging the worker.
        await asyncio.wait_for(mgr._teardown("awn"), timeout=2)
        return time.monotonic() - t0
    assert asyncio.run(run()) < 2.0
    assert "awn" not in mgr._pollers


async def test_reap_keeps_the_callers_own_cancellation():
    """reap() swallows the POLLER's CancelledError. The caller being
    cancelled while it waits must still come out cancelled, or a cancelled
    shutdown carries on as if nothing happened (CodeRabbit, PR #36)."""
    import asyncio
    from app import poller_lifecycle

    async def stubborn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)      # ignores the cancel: never dies

    poller = asyncio.get_running_loop().create_task(stubborn())
    caller = asyncio.get_running_loop().create_task(
        poller_lifecycle.reap(poller, "stubborn"))
    await asyncio.sleep(0.01)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    poller.cancel()
    try:
        await asyncio.wait_for(poller, timeout=0.1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


async def test_reap_abandons_a_task_that_ignores_cancellation(monkeypatch, caplog):
    """§5 row 6: the timeout branch. A poller that swallows its cancel is
    abandoned after STOP_TIMEOUT_S with a warning, and reap() returns."""
    import asyncio
    import logging
    from app import poller_lifecycle

    really_stop = asyncio.Event()

    async def stubborn():
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if really_stop.is_set():
                    raise
                continue                     # swallows the reaper's cancel
    monkeypatch.setattr(poller_lifecycle, "STOP_TIMEOUT_S", 0.05)
    task = asyncio.get_running_loop().create_task(stubborn())
    await asyncio.sleep(0)
    with caplog.at_level(logging.WARNING):
        # Bounded: with wait_for inside reap() this hung the whole suite,
        # which is how the branch was found never to run.
        await asyncio.wait_for(poller_lifecycle.reap(task, "stubborn"), timeout=2)
    assert any("abandoned" in r.getMessage() for r in caplog.records)
    assert not task.done()
    # Tidy: let it die for real.
    really_stop.set()
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
