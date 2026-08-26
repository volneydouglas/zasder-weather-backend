"""AirGradient integration (1.8): payload mapping, indoor/outdoor split,
corrected-series preference, and end-to-end ingest into the air columns."""
from __future__ import annotations

import asyncio
import os
import time

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
