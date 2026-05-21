"""Custom-source ingest endpoint.

Accepts a normalized observation from any external source (the acurite-relay
container, a custom SDR pipeline, an Ecowitt receiver, etc.). The shape is
the one acurite-relay's `parse_observation` produces:

  {
    "device": {
      "id": "24C86E0A66F5",   # MAC, raw or colonized
      "model": "Atlas",
      "sensor_id": "00000711",
      "rssi": 4,
      "battery_outdoor": "normal",
      "battery_hub": "low"
    },
    "timestamp_utc": "2026-05-14T01:09:47",
    "outdoor": { "tempf": 98.3, "humidity": 8, "feels_like": 94, ... },
    "wind": { "speed_mph": 7, "gust_mph": 7, "direction": 224, ... },
    "rain": { "hourly_in": 0, "daily_in": 0, ... },
    "pressure": { "relative_inhg": 29.9 },
    "lightning": { ... },
    "source": "acurite-atlas",
    "received_iso": "..."
  }

We flatten it into the existing observations table columns the iOS app
already reads, plus persist the full normalized record as data_json so we
don't lose source-specific bonus fields (lightning, hub battery, etc).
"""

from __future__ import annotations

import json as _json
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Path, Request

from . import db
from .config import settings

router = APIRouter()


def _flatten(normalized: dict[str, Any]) -> dict[str, Any] | None:
    """Map a normalized observation → the flat-field shape db.insert_observations
    expects (same keys as AmbientWeather's REST response)."""
    dev = normalized.get("device") or {}
    out = normalized.get("outdoor") or {}
    ind = normalized.get("indoor") or {}
    wind = normalized.get("wind") or {}
    rain = normalized.get("rain") or {}
    press = normalized.get("pressure") or {}

    ts_iso = normalized.get("timestamp_utc")
    if not ts_iso:
        return None
    # "2026-05-14T01:09:47" or "2026-05-14T01:09:47Z" → epoch ms
    try:
        from datetime import datetime, timezone
        if ts_iso.endswith("Z"):
            t = datetime.fromisoformat(ts_iso[:-1]).replace(tzinfo=timezone.utc)
        else:
            t = datetime.fromisoformat(ts_iso).replace(tzinfo=timezone.utc)
        dateutc_ms = int(t.timestamp() * 1000)
    except (ValueError, TypeError):
        return None

    # Indoor block is optional — historically the AcuRite hub-relay never
    # had access to indoor data (the hub sensor was elsewhere). The SDR path
    # using a paired indoor sensor (Fineoffset-WH32B, etc.) sends it here.
    # Pressure is unusual — it physically comes from indoors (the console)
    # but is logically reported as "outdoor barometric"; accept it from
    # either place.
    rel_inhg = press.get("relative_inhg") or ind.get("pressure_inhg")
    return {
        "dateutc":        dateutc_ms,
        "tempf":          out.get("tempf"),
        "feelsLike":      out.get("feels_like"),
        "dewPoint":       out.get("dew_point_f"),
        "humidity":       out.get("humidity"),
        "tempinf":        ind.get("tempf"),
        "humidityin":     ind.get("humidity"),
        "baromrelin":     rel_inhg,
        "baromabsin":     rel_inhg,  # ingest sources rarely split
        "windspeedmph":   wind.get("speed_mph"),
        "windgustmph":    wind.get("gust_mph"),
        "maxdailygust":   wind.get("gust_mph"),  # best-effort; relays don't track daily peak
        "winddir":        wind.get("direction"),
        "hourlyrainin":   rain.get("hourly_in"),
        "eventrainin":    rain.get("event_in"),
        "dailyrainin":    rain.get("daily_in"),
        "weeklyrainin":   rain.get("weekly_in"),
        "monthlyrainin":  rain.get("monthly_in"),
        "yearlyrainin":   rain.get("yearly_in"),
        "uv":             out.get("uv"),
        "solarradiation": out.get("solar_wm2"),
    }


def _format_mac(raw: str) -> str:
    """Normalize a hub identifier to AA:BB:CC:DD:EE:FF if it looks like a
    12-hex MAC. Pass through anything else unchanged."""
    if raw and re.fullmatch(r"[0-9A-Fa-f]{12}", raw):
        return ":".join(raw[i:i+2].upper() for i in range(0, 12, 2))
    return raw or ""


def _device_label(normalized: dict[str, Any]) -> tuple[str, str | None]:
    """Pick a friendly name + location for the devices table from the source.
    Operator-set device.name / device.location override the auto-generated
    pretty name so a "STATION_LOCATION=Chandler" env var on the relay flows
    through to the iOS app."""
    dev = normalized.get("device") or {}
    src = normalized.get("source") or "custom"
    model = dev.get("model")
    pretty = {
        "acurite-atlas": "AcuRite Atlas",
        "acurite-access": "AcuRite Access",
        "ecowitt": "Ecowitt",
        "tempest": "Tempest",
    }.get(src, src.replace("-", " ").title())
    auto_name = f"{pretty}{f' ({model})' if model and model.lower() not in pretty.lower() else ''}"
    name = dev.get("name") or auto_name
    location = dev.get("location")
    return name, location


def _require_ingest_token(token: str) -> None:
    expected = settings.ingest_token
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="invalid ingest token")


def _token_from_header(authorization: str | None,
                       x_ingest_token: str | None) -> str:
    """Pull the ingest token from either Authorization: Bearer or
    X-Ingest-Token. Missing both = empty string (will fail validation)."""
    if x_ingest_token: return x_ingest_token.strip()
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return ""


async def _do_ingest(payload_obj: Any) -> dict[str, Any]:
    if not isinstance(payload_obj, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    dev = payload_obj.get("device") or {}
    raw_id = dev.get("id") or ""
    mac = _format_mac(str(raw_id))
    if not mac:
        raise HTTPException(status_code=400, detail="device.id required")
    flat = _flatten(payload_obj)
    if not flat:
        raise HTTPException(status_code=400, detail="missing or invalid timestamp_utc")
    name, location = _device_label(payload_obj)
    info = {
        "name": name,
        "info": {"name": name, "location": location, "source": payload_obj.get("source")},
        "lastData": flat,
    }
    await db.upsert_device(mac, info)
    inserted = await db.insert_observations(mac, [{**flat, "_source": payload_obj}])
    return {"ok": True, "mac": mac, "inserted": inserted, "ts_ms": flat["dateutc"]}


# Preferred: token in header so it never appears in proxy/access logs.
@router.post("/ingest/custom")
async def ingest_custom_header(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ingest_token: Annotated[str | None, Header(alias="X-Ingest-Token")] = None,
) -> dict[str, Any]:
    _require_ingest_token(_token_from_header(authorization, x_ingest_token))
    return await _do_ingest(await request.json())


# Backwards-compat: token in path. Still accepted for older relay deploys
# but the token will leak into access logs. Migrate clients to the header
# form when convenient.
@router.post("/ingest/custom/{token}")
async def ingest_custom(token: Annotated[str, Path()], request: Request) -> dict[str, Any]:
    _require_ingest_token(token)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    dev = payload.get("device") or {}
    raw_id = dev.get("id") or ""
    mac = _format_mac(str(raw_id))
    if not mac:
        raise HTTPException(status_code=400, detail="device.id required")

    flat = _flatten(payload)
    if not flat:
        raise HTTPException(status_code=400, detail="missing or invalid timestamp_utc")

    # Upsert the device row so /api/devices includes this source.
    name, location = _device_label(payload)
    info = {
        "name": name,
        "info": {"name": name, "location": location, "source": payload.get("source")},
        "lastData": flat,
    }
    await db.upsert_device(mac, info)

    # Insert the observation. Stash the full normalized payload as data_json
    # so we don't lose source-specific bonus fields (lightning, hub battery).
    inserted = await db.insert_observations(mac, [{**flat, "_source": payload}])

    return {"ok": True, "mac": mac, "inserted": inserted, "ts_ms": flat["dateutc"]}
