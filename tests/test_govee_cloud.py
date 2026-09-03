"""Govee cloud source (2.0): the GoveeLife H5140 CO₂ monitor through
Govee's Platform API. The rules this file pins:

  · the key rides a header and never an error message;
  · the H5140 reports Fahrenheit without saying so and is stored as is;
    a listing that declares Celsius is converted;
  · an offline monitor's cached numbers are not air: skipped;
  · no timestamp on the wire, so an unchanged reading is the cache
    repeating itself and is skipped, but never for longer than
    REPOST_AFTER_S;
  · temperature and humidity are INDOOR (a CO₂ monitor is in a room);
  · the device is a `5D:5D:08:…` air monitor, stable per device id.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app.govee_cloud_client import GoveeCloudClient, GoveeCloudError  # noqa: E402
from app.govee_cloud_poller import (  # noqa: E402
    REPOST_AFTER_S, GoveeCloudPoller, build_payload, capability_values,
    is_air_monitor_listing, parse_device_ids, synth_mac,
    temperature_is_celsius)

DEV_ID = "12:BC:AC:27:6E:02:6C:7C"
LISTING = {"sku": "H5140", "device": DEV_ID, "deviceName": "Office CO2",
           "type": "devices.types.air_quality_monitor",
           "capabilities": [
               {"type": "devices.capabilities.property", "instance": "sensorTemperature"},
               {"type": "devices.capabilities.property", "instance": "sensorHumidity"},
               {"type": "devices.capabilities.property", "instance": "carbonDioxideConcentration"}]}


def state(co2=620, temp=69.26, hum=49.9, online=1, pm25=None):
    caps = [
        {"type": "devices.capabilities.online", "instance": "online", "state": {"value": online}},
        {"type": "devices.capabilities.property", "instance": "sensorTemperature", "state": {"value": temp}},
        {"type": "devices.capabilities.property", "instance": "sensorHumidity", "state": {"value": hum}},
        {"type": "devices.capabilities.property", "instance": "carbonDioxideConcentration", "state": {"value": co2}},
    ]
    if pm25 is not None:
        caps.append({"type": "devices.capabilities.property", "instance": "pm25", "state": {"value": pm25}})
    return {"sku": "H5140", "device": DEV_ID, "capabilities": caps}


# ───────────────────────── transform ─────────────────────────

def test_h5140_reading_lands_as_an_indoor_air_device():
    p = build_payload(DEV_ID, LISTING, state())
    assert p["source"] == "govee"
    assert p["device"] == {"id": synth_mac(DEV_ID), "name": "Office CO2", "model": "H5140"}
    assert p["air"] == {"co2": 620.0}
    assert p["indoor"] == {"tempf": 69.3, "humidity": 50}      # Fahrenheit as reported
    assert "outdoor" not in p
    assert p["timestamp_utc"].endswith("+00:00")


def test_synth_mac_is_the_air_family_and_stable():
    m = synth_mac(DEV_ID)
    assert m.startswith("5D5D08") and len(m) == 12
    assert synth_mac(DEV_ID.lower()) == m
    assert synth_mac("AA:BB:CC:DD:EE:FF:00:11") != m
    from app import db
    assert db.is_air_monitor(m)


def test_a_celsius_listing_is_converted_and_the_h5140_is_not():
    metric = dict(LISTING, sku="H5XXX", capabilities=[
        {"instance": "sensorTemperature", "parameters": {"unit": "unit.celsius"}}])
    assert temperature_is_celsius(metric)
    assert build_payload(DEV_ID, metric, state(temp=20.7))["indoor"]["tempf"] == 69.3
    assert not temperature_is_celsius(LISTING)
    assert build_payload(DEV_ID, LISTING, state(temp=69.26))["indoor"]["tempf"] == 69.3


def test_an_offline_monitor_reports_nothing():
    assert build_payload(DEV_ID, LISTING, state(online=0)) is None
    assert build_payload(DEV_ID, LISTING, {"capabilities": []}) is None


def test_pm25_rides_along_on_a_model_that_has_it():
    p = build_payload(DEV_ID, LISTING, state(pm25=3.4))
    assert p["air"] == {"co2": 620.0, "pm25": 3.4}


def test_capability_values_unwrap_the_shapes_govee_uses():
    caps = {"capabilities": [
        {"instance": "a", "state": {"value": 1}},
        {"instance": "b", "state": {"currentTemperature": 70.5}},
        {"instance": "c", "state": {"nested": {"value": 2}}},      # unknown key: dropped
        {"instance": "d", "state": None},
        {"state": {"value": 9}}]}                                  # no instance
    assert capability_values(caps) == {"a": 1, "b": 70.5}


def test_listing_filter_and_device_id_parse():
    assert is_air_monitor_listing(LISTING)
    assert is_air_monitor_listing({"capabilities": [{"instance": "pm25"}]})
    assert not is_air_monitor_listing({"type": "devices.types.light", "capabilities": []})
    assert parse_device_ids(" aa:bb, cc:dd ;aa:bb,") == ["AA:BB", "CC:DD"]
    assert parse_device_ids(None) == []


# ───────────────────────── client ─────────────────────────

def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return original(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", patched)


async def test_the_key_rides_a_header_and_never_an_error(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("Govee-API-Key")
        seen["path"] = request.url.path
        if request.url.path.endswith("/device/state"):
            seen["body"] = request.read().decode()
            return httpx.Response(401, json={"code": 401, "message": "unauthorized"})
        return httpx.Response(200, json={"code": 200, "message": "success",
                                         "data": [LISTING]})
    _patch_transport(monkeypatch, handler)
    client = GoveeCloudClient("secret-key-123")
    assert await client.list_devices() == [LISTING]
    assert seen["key"] == "secret-key-123"
    with pytest.raises(GoveeCloudError) as e:
        await client.device_state("H5140", DEV_ID)
    assert "secret-key-123" not in str(e.value)
    assert "401" in str(e.value) and "/device/state" in str(e.value)
    assert '"sku": "H5140"' in seen["body"] and DEV_ID in seen["body"]


async def test_a_200_with_an_error_code_is_an_error(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(
        200, json={"code": 429, "message": "too many requests"}))
    with pytest.raises(GoveeCloudError, match="429"):
        await GoveeCloudClient("k").list_devices()


# ───────────────────────── poller ─────────────────────────

class FakeClient:
    def __init__(self, listings, states):
        self.listings = listings
        self.states = states
        self.calls: list[str] = []

    async def list_devices(self):
        self.calls.append("list")
        return self.listings

    async def device_state(self, sku, device):
        self.calls.append(f"state {device}")
        return self.states[device]


def _fresh_db(temp_env):
    import importlib
    for mod in ("app.config", "app.db", "app.ingest", "app.govee_cloud_poller"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from app import db
    asyncio.run(db.init_db())
    return db


def test_poller_posts_a_change_and_skips_the_cache_repeating_itself(temp_env, monkeypatch):
    db = _fresh_db(temp_env)
    from app import govee_cloud_poller as gcp
    posted: list[dict] = []

    async def fake_ingest(payload):
        posted.append(payload)
        return {"ok": True, "inserted": 1}
    monkeypatch.setattr(gcp.ingest, "_do_ingest", fake_ingest)
    states = {DEV_ID: state()}
    fake = FakeClient([LISTING, {"sku": "H6001", "device": "LIGHT", "type": "devices.types.light"}],
                      states)
    poller = gcp.GoveeCloudPoller(fake, 60, None, "Study")

    async def run():
        await poller._discover()
        assert list(poller._devices) == [DEV_ID], "the light is not an air monitor"
        await poller._tick()
        await poller._tick()                        # identical → skipped
        states[DEV_ID] = state(co2=740)
        await poller._tick()                        # changed → posted
    asyncio.run(run())
    assert [p["air"]["co2"] for p in posted] == [620.0, 740.0]
    assert posted[0]["device"]["name"] == "Study"    # single device: the configured name
    assert fake.calls.count(f"state {DEV_ID}") == 3
    del db


def test_an_unchanged_reading_is_reposted_after_the_window(temp_env, monkeypatch):
    db = _fresh_db(temp_env)
    from app import govee_cloud_poller as gcp
    posted: list[dict] = []

    async def fake_ingest(payload):
        posted.append(payload)
        return {"ok": True, "inserted": 1}
    monkeypatch.setattr(gcp.ingest, "_do_ingest", fake_ingest)
    poller = gcp.GoveeCloudPoller(FakeClient([LISTING], {DEV_ID: state()}), 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(gcp.time, "monotonic", lambda: clock["t"])

    async def run():
        await poller._discover()
        await poller._tick()
        clock["t"] += REPOST_AFTER_S - 1
        await poller._tick()                        # still inside the window
        clock["t"] += 2
        await poller._tick()                        # window passed: re-posted
    asyncio.run(run())
    assert len(posted) == 2
    del db


def test_poller_filters_by_configured_ids_and_floors_the_interval(temp_env):
    db = _fresh_db(temp_env)
    from app import govee_cloud_poller as gcp
    other = "AA:BB:CC:DD:EE:FF:00:11"
    fake = FakeClient([LISTING, dict(LISTING, device=other, deviceName="Bedroom")],
                      {DEV_ID: state(), other: state(co2=900)})
    poller = gcp.GoveeCloudPoller(fake, 5, f"{other}", None)
    assert poller._interval_s == gcp.MIN_INTERVAL_S
    asyncio.run(poller._discover())
    assert list(poller._devices) == [other]
    del db


def test_poller_records_failure_only_when_nothing_was_stored(temp_env, monkeypatch):
    db = _fresh_db(temp_env)
    from app import govee_cloud_poller as gcp, source_status
    source_status.reset()

    class Boom(FakeClient):
        async def device_state(self, sku, device):
            raise RuntimeError("cloud down")
    source_status.declare("govee", True)
    poller = gcp.GoveeCloudPoller(Boom([LISTING], {}), 60)

    async def run():
        await poller._discover()
        await poller._tick()
    asyncio.run(run())
    src = next(s for s in source_status.snapshot() if s["name"] == "govee")
    assert src["last_error"] and "cloud down" in src["last_error"]
    assert src["healthy"] is False
    del db


# ───────────────────────── end to end ─────────────────────────

def test_a_govee_reading_stores_air_columns_and_classifies_as_a_monitor(client, monkeypatch):
    from app import db, govee_cloud_poller as gcp
    posted = {}

    async def run():
        p = gcp.build_payload(DEV_ID, LISTING, state(co2=655, temp=71.2, hum=44.4))
        from app import ingest
        posted["res"] = await ingest._do_ingest(p)
        compact = gcp.synth_mac(DEV_ID)
        mac = ":".join(compact[i:i + 2] for i in range(0, 12, 2))
        obs = await db.latest_observation(mac)
        devs = await db.list_devices()
        dev = next(d for d in devs if d["mac"].replace(":", "").upper() == compact)
        return obs, dev
    obs, dev = asyncio.run(run())
    assert obs["co2"] == 655.0 and obs["tempinf"] == 71.2 and obs["humidityin"] == 44
    assert obs.get("tempf") is None
    assert db.is_air_monitor_device(dev)
