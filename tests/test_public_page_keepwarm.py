"""2.7: the public page keeps itself fresh, logs a slow build, and hides
fields a station has never reported.

The keep-warm task exists because stale-while-revalidate alone served the
first visitor after a quiet spell the copy from the last visit, and a
sharing save left the cache empty for a visitor to rebuild (Doren's empty
frame, 2026-10-12)."""
import asyncio
import importlib
import logging
import time

import pytest


class _Mod:
    """The live module, looked up per use: the `client` fixture re-imports
    app.main, and a module-level import would build Settings before
    conftest sets the environment."""
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(importlib.import_module(self._name), attr)

    def __setattr__(self, attr, value):
        if attr == "_name":
            object.__setattr__(self, attr, value)
        else:
            setattr(importlib.import_module(self._name), attr, value)


_m = _Mod("app.main")
pd = _Mod("app.public_dashboard")
_pp = _Mod("app.publish_policy")


@pytest.fixture(autouse=True)
def _env(temp_env):
    """Every test runs in conftest's environment, client or not."""


def _arm(monkeypatch, builds, enabled=True):
    settings = importlib.import_module("app.config").settings
    monkeypatch.setattr(settings, "public_dashboard", enabled)

    async def fake_build(devices, now_ms):
        builds.append(now_ms)
        return f"<html>build-{len(builds)}</html>"

    async def one_device():
        return [{"mac": "AA:BB:CC:DD:EE:FF", "name": "Yard"}]

    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_build_public_dashboard", fake_build)
    monkeypatch.setattr(main.db, "list_devices", one_device)


def _tick() -> bool:
    async def go():
        started = await _m._public_dashboard_keepwarm_once()
        if started:
            await _m._PUBLIC_DASH_REFRESH_TASK
        return started
    return asyncio.run(go())


def test_keepwarm_rebuilds_before_the_ttl(client, monkeypatch):
    """A copy still fresh to a visitor but inside the last tick before the
    TTL is rebuilt now, so no visitor ever lands on a stale one."""
    builds = []
    _arm(monkeypatch, builds)
    age = _m._PUBLIC_DASH_TTL_S - _m._PUBLIC_DASH_TICK_S + 1
    _m._PUBLIC_DASH_CACHE = (time.time() - age, "<html>old</html>", _pp.generation())
    assert _tick() is True
    assert builds and _m._PUBLIC_DASH_CACHE[1] == "<html>build-1</html>"
    assert _m._PUBLIC_DASH_REFRESHING is False


def test_keepwarm_leaves_a_young_copy_alone(client, monkeypatch):
    builds = []
    _arm(monkeypatch, builds)
    _m._PUBLIC_DASH_CACHE = (time.time() - 5, "<html>young</html>", _pp.generation())
    assert _tick() is False
    assert builds == []


def test_keepwarm_refills_a_cache_a_save_emptied(client, monkeypatch):
    """The Doren case: a sharing save bumps the policy and clears the cache.
    The next tick rebuilds it, instead of the next visitor waiting."""
    builds = []
    _arm(monkeypatch, builds)
    _m._PUBLIC_DASH_CACHE = (time.time(), "<html>before</html>", _pp.generation())
    _pp.bump()                          # the old copy is no longer servable
    assert _tick() is True
    assert _m._PUBLIC_DASH_CACHE[2] == _pp.generation()
    assert _m._PUBLIC_DASH_CACHE[1] == "<html>build-1</html>"


def test_keepwarm_does_nothing_while_the_page_is_off(client, monkeypatch):
    builds = []
    _arm(monkeypatch, builds, enabled=False)
    _m._PUBLIC_DASH_CACHE = None
    assert _tick() is False
    assert builds == []


def test_keepwarm_backs_off_on_a_slow_box(client, monkeypatch):
    """A 40 s build is rebuilt no more often than every 160 s, so a small
    box spends at most a quarter of its time on the page; visitors in
    between get the stale copy at once, as before."""
    builds = []
    _arm(monkeypatch, builds)
    _m._PUBLIC_DASH_LAST_BUILD_S = 40.0
    assert _m._public_dash_refresh_age_s() == 160.0
    _m._PUBLIC_DASH_CACHE = (time.time() - 120, "<html>old</html>", _pp.generation())
    assert _tick() is False
    _m._PUBLIC_DASH_CACHE = (time.time() - 161, "<html>old</html>", _pp.generation())
    assert _tick() is True


def test_a_slow_build_is_logged_with_its_duration(client, monkeypatch, caplog):
    builds = []
    _arm(monkeypatch, builds)
    monkeypatch.setattr(importlib.import_module("app.main"), "_PUBLIC_DASH_SLOW_S", 0.0)
    _m._PUBLIC_DASH_CACHE = None
    with caplog.at_level(logging.WARNING):
        assert _tick() is True
    assert any("public dashboard build took" in r.getMessage()
               and "keep-warm" in r.getMessage() for r in caplog.records)


def test_a_fast_build_is_not_logged(client, monkeypatch, caplog):
    builds = []
    _arm(monkeypatch, builds)
    _m._PUBLIC_DASH_CACHE = None
    with caplog.at_level(logging.WARNING):
        assert _tick() is True
    assert not any("took" in r.getMessage() for r in caplog.records)


def test_the_server_starts_the_keepwarm_task(client):
    task = _m._PUBLIC_DASH_KEEPWARM_TASK
    assert task is not None and not task.done()


# ── fields a station has never reported ────────────────────────────────

_FIELDS = ["tempf", "humidity", "windspeedmph", "baromrelin", "hourlyrainin"]


def _records(*fields):
    return {"periods": {"all": {"fields": {f: {"min": 1, "max": 2} for f in fields}}}}


def test_a_barometerless_station_loses_its_pressure_card():
    """Mirror #8: an AcuRite with no barometer showed an empty Pressure
    card. Rain is judged by the day total the records keep."""
    recs = _records("tempf", "humidity", "windspeedmph", "dailyrainin")
    out = pd.shown_fields(_FIELDS, {"tempf": 70}, {}, recs)
    assert out == ["tempf", "humidity", "windspeedmph", "hourlyrainin"]


def test_unknown_records_hide_nothing():
    """A cold records cache is unknown, not never."""
    assert pd.shown_fields(_FIELDS, {}, {}, None) == _FIELDS
    assert pd.shown_fields(_FIELDS, {}, {}, {"periods": {}}) == _FIELDS


def test_a_shared_barometer_keeps_the_pressure_card():
    """SHARED_BAROMETER_SOURCE_MAC fills the current reading only, so the
    records never see it; the current value keeps the card."""
    recs = _records("tempf")
    out = pd.shown_fields(_FIELDS, {"baromrelin": 29.9}, {}, recs)
    assert "baromrelin" in out


def test_a_field_with_readings_in_the_last_day_keeps_its_card():
    """Records that do not list UV yet (a sensor added today) and no
    current UV value: the 24-hour series alone keeps the card."""
    recs = _records("tempf")
    row = {"dateutc": 1_790_000_000_000, "uv": 3.0}
    assert pd.shown_fields(["tempf", "uv"], {"tempf": 70}, {"uv": [row]}, recs) == [
        "tempf", "uv"]
    assert pd.shown_fields(["tempf", "uv"], {"tempf": 70}, {"uv": []}, recs) == ["tempf"]


def test_temperature_always_stays():
    assert pd.shown_fields(_FIELDS, {}, {}, _records())[0] == "tempf"
    assert pd.shown_fields(_FIELDS, {}, {}, _records()) == ["tempf"]


def test_the_rendered_station_has_no_pressure_card_or_chip():
    recs = _records("tempf", "humidity", "windspeedmph", "dailyrainin")
    html = pd.render_station("Yard", {"tempf": 70, "humidity": 40},
                             {"tempf": [(1, 70.0), (2, 71.0)]}, _FIELDS,
                             records=recs)
    assert "Pressure" not in html
    assert "Humidity" in html and "Rain" in html
    # Without records the card is still drawn (empty), exactly as before.
    html = pd.render_station("Yard", {"tempf": 70}, {}, _FIELDS, records=None)
    assert "Pressure" in html



def test_a_restore_between_lookup_and_build_never_stores_old_stations(client, monkeypatch):
    """CodeRabbit on PR #92: the keep-warm tick listed devices, then the
    refresh captured the policy generation. A restore in between stored the
    old database's stations under the new generation. The build now reads
    the station list after the generation, so it sees the restored one."""
    main = importlib.import_module("app.main")
    settings = importlib.import_module("app.config").settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    seen = []
    current = {"devices": [{"mac": "OLD", "name": "Old station"}]}

    async def list_devices():
        return list(current["devices"])

    async def fake_build(devices, now_ms):
        seen.append([d["mac"] for d in devices])
        return "<html>" + ",".join(d["mac"] for d in devices) + "</html>"

    monkeypatch.setattr(main.db, "list_devices", list_devices)
    monkeypatch.setattr(main, "_build_public_dashboard", fake_build)
    main._PUBLIC_DASH_CACHE = None

    async def go():
        stale = await main.db.list_devices()          # the caller's lookup
        current["devices"] = [{"mac": "NEW", "name": "Restored"}]
        importlib.import_module("app.publish_policy").bump()   # the restore
        main._refresh_public_dashboard_soon(stale, int(time.time() * 1000))
        await main._PUBLIC_DASH_REFRESH_TASK

    asyncio.run(go())
    assert seen == [["NEW"]]
    assert "OLD" not in main._PUBLIC_DASH_CACHE[1]
