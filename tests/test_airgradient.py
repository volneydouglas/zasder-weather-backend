"""AirGradient integration (1.8): payload mapping, indoor/outdoor split,
corrected-series preference, and end-to-end ingest into the air columns."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app.airgradient_poller import build_payload, synth_mac  # noqa: E402


def _outdoor(now_iso: str) -> dict:
    """Shape captured live from Volney's account, 2026-08-25."""
    return {"locationId": 191562, "locationName": "Home Chandler",
            "latitude": 33.2997772, "longitude": -111.9375404,
            "pm01": 0.0, "pm02": 0.0, "pm10": 0.0,
            "pm01_corrected": 0.0, "pm02_corrected": 2.4,
            "pm10_corrected": 0.0, "pm003Count": 131,
            "atmp": 37.7, "rhum": 16, "rco2": 425,
            "atmp_corrected": 39.4, "rhum_corrected": 27,
            "rco2_corrected": 425, "wifi": -48, "timestamp": now_iso,
            "serialno": "d83bda1bbeb8", "model": "O-1PST",
            "firmwareVersion": "3.6.2", "tvoc": 124.7, "tvocIndex": 131,
            "noxIndex": 1, "locationType": "outdoor"}


def _indoor(now_iso: str) -> dict:
    return {"locationId": 191839, "locationName": "Chandler Home Inside",
            "latitude": None, "longitude": None,
            "pm02_corrected": 0.1, "atmp": 21.6, "rhum": 49, "rco2": 607,
            "atmp_corrected": 21.6, "rhum_corrected": 49,
            "rco2_corrected": 607, "timestamp": now_iso,
            "serialno": "3cdc75b579b0", "model": "I-9PSL-DE",
            "tvocIndex": 29, "noxIndex": 1, "locationType": "indoor"}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat()


def test_synth_mac_shape():
    assert synth_mac(191562) == "5D5D0702EC4A"


def test_outdoor_maps_to_outdoor_block_with_corrected_values():
    p = build_payload(_outdoor(_now_iso()))
    assert p is not None
    assert p["device"]["id"] == "5D5D0702EC4A"
    assert p["device"]["name"] == "Home Chandler"
    assert p["device"]["coords"]["lat"] == 33.2997772
    # Corrected series preferred: 39.4 °C → 102.9 °F, humidity 27 not 16.
    assert p["outdoor"]["tempf"] == 102.9
    assert p["outdoor"]["humidity"] == 27
    assert "indoor" not in p
    assert p["air"]["pm25"] == 2.4          # pm02_corrected, not raw 0.0
    assert p["air"]["co2"] == 425
    assert p["air"]["tvoc_index"] == 131
    assert p["air"]["nox_index"] == 1
    assert p["source"] == "airgradient"


def test_indoor_maps_to_indoor_block():
    p = build_payload(_indoor(_now_iso()))
    assert p is not None
    # An indoor monitor must never impersonate an outdoor station.
    assert "outdoor" not in p
    assert p["indoor"]["tempf"] == 70.9      # 21.6 °C
    assert p["indoor"]["humidity"] == 49
    assert "coords" not in p["device"]       # nulls dropped, not stored


def test_unusable_locations_are_skipped():
    assert build_payload({"locationName": "no id"}) is None
    assert build_payload({"locationId": 5, "timestamp": None}) is None
    assert build_payload({"locationId": 5, "timestamp": "not-a-time"}) is None


def test_ingest_stores_air_columns(client):
    """End to end: the payload the poller builds lands in the dedicated
    air columns (not just data_json), for both unit types."""
    from app import db, ingest

    async def run():
        for loc in (_outdoor(_now_iso()), _indoor(_now_iso())):
            payload = build_payload(loc)
            assert payload is not None
            await ingest._do_ingest(payload)
        rows = await db.observation_rows("5D:5D:07:02:EC:4A", 0,
                                         int(time.time() * 1000) + 1000,
                                         limit=5)
        return rows
    rows = asyncio.run(run())
    assert rows, "outdoor observation stored"
    r = rows[-1]
    assert r["pm25"] == 2.4
    assert r["co2"] == 425
    assert r["tvoc_index"] == 131
    assert r["nox_index"] == 1
    assert r["tempf"] == 102.9

    H = {"Authorization": "Bearer test-api-token"}
    devs = client.get("/api/devices", headers=H).json()
    names = {d.get("name") for d in devs}
    assert "Home Chandler" in names and "Chandler Home Inside" in names
    inside = next(d for d in devs if d.get("name") == "Chandler Home Inside")
    last = inside.get("lastData") or {}
    assert last.get("tempinf") == 70.9
    assert last.get("co2") == 607
    assert last.get("tempf") is None, "indoor unit stores no outdoor temp"


def test_error_messages_never_carry_the_token():
    import httpx
    from app.airgradient_client import AirGradientError
    req = httpx.Request("GET", "https://api.airgradient.com/public/api/v1/"
                        "locations/measures/current?token=SECRET-TOKEN")
    resp = httpx.Response(401, request=req)
    err = AirGradientError.from_http(
        httpx.HTTPStatusError("boom", request=req, response=resp))
    assert "SECRET-TOKEN" not in str(err)
    assert "401" in str(err)


# ── LAN path (1.9): build_local_payload + the local client ──────────────

def _local_measures(**over):
    """A live-verified I-9PSL local /measures/current shape."""
    d = {"wifi": -48, "serialno": "84fce612345c", "rco2": 612,
         "pm01": 3.0, "pm02": 5.0, "pm10": 6.0,
         "atmp": 27.3, "atmpCompensated": 26.8,
         "rhum": 33.0, "rhumCompensated": 35.0,
         "tvocIndex": 61.0, "noxIndex": 1.0,
         "boot": 4, "firmware": "3.1.1", "model": "I-9PSL"}
    d.update(over)
    return d


def test_local_payload_prefers_compensated_and_maps_indoor():
    from app.airgradient_poller import build_local_payload
    p = build_local_payload(_local_measures(), "2026-08-27T01:00:00+00:00")
    assert p["device"]["id"] == "84fce612345c"     # the monitor's own MAC
    assert p["device"]["model"] == "I-9PSL"
    assert p["timestamp_utc"] == "2026-08-27T01:00:00+00:00"
    # atmpCompensated 26.8 °C → 80.2 °F; rhumCompensated wins over rhum.
    assert p["indoor"]["tempf"] == 80.2
    assert p["indoor"]["humidity"] == 35
    assert "outdoor" not in p
    assert p["air"] == {"pm1": 3.0, "pm25": 5.0, "pm10": 6.0, "co2": 612.0,
                        "tvoc_index": 61.0, "nox_index": 1.0}


def test_local_payload_o_series_maps_outdoor_unknown_maps_indoor():
    from app.airgradient_poller import build_local_payload
    p = build_local_payload(_local_measures(model="O-1PST"), "2026-08-27T01:00:00+00:00")
    assert "outdoor" in p and "indoor" not in p
    # Unknown model errs indoor — never impersonate an outdoor station.
    p = build_local_payload(_local_measures(model=None), "2026-08-27T01:00:00+00:00")
    assert "indoor" in p and "outdoor" not in p


def test_local_payload_missing_readings_stay_omitted():
    """The local API OMITS missing readings (verified live) — absent is
    not zero, and a CO2-less model must not invent one."""
    from app.airgradient_poller import build_local_payload
    m = {"serialno": "84fce612345c", "atmp": 20.0, "model": "I-9PSL"}
    p = build_local_payload(m, "2026-08-27T01:00:00+00:00")
    assert "air" not in p
    assert p["indoor"] == {"tempf": 68.0}
    # No serial → no device row to key — skip, don't guess.
    assert build_local_payload({"atmp": 20.0}, "x") is None
    # Nothing usable at all → None (wifi/boot alone is not a reading).
    assert build_local_payload({"serialno": "84fce612345c",
                                "boot": 9}, "x") is None


def test_local_client_errors_name_host_and_class_only(monkeypatch):
    import asyncio
    import httpx
    from app.airgradient_client import AirGradientError, AirGradientLocalClient

    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(AirGradientError) as e:
        asyncio.run(AirGradientLocalClient().measures_current("192.168.1.40"))
    assert "192.168.1.40" in str(e.value) and "500" in str(e.value)


def test_local_poll_tick_stores_a_row(client, monkeypatch):
    """One real tick end to end: LAN fetch (mocked transport) → transform
    → ingest → stored row + airgradient-local success accounting."""
    import asyncio
    import httpx
    from app import db, source_status
    from app.airgradient_client import AirGradientLocalClient
    from app.airgradient_poller import AirGradientLocalPoller

    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "192.168.1.40"
        return httpx.Response(200, json=_local_measures())

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def run():
        poller = AirGradientLocalPoller(AirGradientLocalClient(),
                                        ["192.168.1.40"], 3600)
        await poller.start()
        await asyncio.sleep(0.05)
        await poller.stop()
        return await db.latest_observation("84:FC:E6:12:34:5C")

    obs = asyncio.run(run())
    assert obs is not None and obs["co2"] == 612.0
    row = next(r for r in source_status.snapshot()
               if r["name"] == "airgradient-local")
    assert row["last_error"] is None and row["last_rows"] == 1
