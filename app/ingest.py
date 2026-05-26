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
import logging
import math
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request

from . import db
from .config import settings

log = logging.getLogger("ingest")

router = APIRouter()


def _finite(v: Any) -> Any:
    """Return v if it's a finite number, None if it's NaN/inf, pass-through
    for anything that isn't a number. Stops upstream decoders from poisoning
    stored observations with non-finite floats that crash JSONResponse on
    the read path (`/api/devices/{mac}/current` raises ValueError otherwise)."""
    if isinstance(v, bool):  # bools are int subclass — leave alone
        return v
    if isinstance(v, (int, float)):
        try:
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None
    return v


def _scrub_numbers(block: dict[str, Any] | None) -> dict[str, Any]:
    """Filter all numeric values in a sub-block (outdoor/wind/etc.) through
    _finite so non-finite values never reach the DB."""
    if not block:
        return {}
    return {k: _finite(v) for k, v in block.items()}


def _flatten(normalized: dict[str, Any]) -> dict[str, Any] | None:
    """Map a normalized observation → the flat-field shape db.insert_observations
    expects (same keys as AmbientWeather's REST response)."""
    dev = normalized.get("device") or {}
    # Filter NaN/inf out of every numeric sub-block at the boundary so non-
    # finite values never reach the DB or downstream JSON serialization.
    out = _scrub_numbers(normalized.get("outdoor"))
    ind = _scrub_numbers(normalized.get("indoor"))
    wind = _scrub_numbers(normalized.get("wind"))
    rain = _scrub_numbers(normalized.get("rain"))
    press = _scrub_numbers(normalized.get("pressure"))

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

    # Per-MAC yearly-rain calibration. Cumulative sensor counters (Atlas
    # rain_in, Fineoffset rain_mm) report lifetime totals, so we subtract
    # an operator-configured offset to get actual YTD inches. Clamp to
    # zero so a decoder glitch posting below the offset doesn't yield
    # a negative stored value. Non-numeric input (e.g. decoder posted
    # the literal string "abc") coerces to None — same defensive pattern
    # as _scrub_numbers; never 500 the request.
    yearly_in = rain.get("yearly_in")
    if yearly_in is not None:
        try:
            yearly_in = float(yearly_in)
        except (TypeError, ValueError):
            yearly_in = None
    if yearly_in is not None:
        mac_for_offset = _format_mac(dev.get("id") or "").upper()
        offset = settings.ingest_yearly_rain_offsets.get(mac_for_offset)
        if offset is not None:
            yearly_in = max(0.0, yearly_in - offset)

    # Feels-like: pass through the source's own value if present (AWN +
    # Davis provide it); otherwise derive it so SDR/custom sources that only
    # post raw temp/humidity still get the tile. Matches AWN's method.
    tempf = out.get("tempf")
    feels_like = out.get("feels_like")
    if feels_like is None:
        feels_like = _compute_feels_like(tempf, out.get("humidity"),
                                         wind.get("speed_mph"))

    return {
        "dateutc":        dateutc_ms,
        "tempf":          tempf,
        "feelsLike":      feels_like,
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
        "yearlyrainin":   yearly_in,
        "uv":             out.get("uv"),
        "solarradiation": out.get("solar_wm2"),
    }


def _compute_feels_like(tempf: Any, humidity: Any, wind_mph: Any) -> float | None:
    """NWS 'feels like', matching what AmbientWeather reports: the Rothfusz
    heat-index regression at ≥80°F (which legitimately dips below air temp in
    dry heat — e.g. 99.3°F/15% → 95.09°F), wind chill at ≤50°F with wind
    >3 mph, else the air temp. Returns None only when temperature is unknown.
    Fallback for sources (SDR/custom) that don't post their own feels_like."""
    try:
        t = float(tempf)
    except (TypeError, ValueError):
        return None
    try:
        rh = float(humidity)
    except (TypeError, ValueError):
        rh = None
    if rh is not None and t >= 80.0:
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh
              - 0.22475541 * t * rh - 6.83783e-3 * t * t
              - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh
              + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh)
        return round(hi, 2)
    try:
        v = float(wind_mph)
    except (TypeError, ValueError):
        v = None
    if v is not None and t <= 50.0 and v > 3.0:
        wc = 35.74 + 0.6215 * t - 35.75 * v**0.16 + 0.4275 * t * v**0.16
        return round(wc, 2)
    return round(t, 2)


def _is_rain_glitch(jump_in: float, elapsed_h: float,
                    max_rate_in_per_hr: float) -> bool:
    """True if a positive jump in cumulative yearly rain is implausible for the
    elapsed time (an SDR decode glitch). Allowance = max-rate × hours + a 0.25"
    floor for tip-counter jitter. Only positive spikes are flagged (counter
    resets / negative deltas are handled by the offset/clamp path)."""
    if max_rate_in_per_hr <= 0 or jump_in <= 0:
        return False
    allowance = max_rate_in_per_hr * max(elapsed_h, 1.0 / 3600.0) + 0.25
    return jump_in > allowance


def _format_mac(raw: str) -> str:
    """Normalize a hub identifier to AA:BB:CC:DD:EE:FF if it looks like a
    12-hex MAC. Pass through anything else unchanged."""
    if raw and re.fullmatch(r"[0-9A-Fa-f]{12}", raw):
        return ":".join(raw[i:i+2].upper() for i in range(0, 12, 2))
    return raw or ""


def _device_label(normalized: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pick a friendly name + location for the devices table.

    Returns (name, location) where `name` is:
      - the operator-supplied `device.name` if present (explicit POST field), OR
      - None — meaning "I have no explicit name; preserve whatever the row
        already has, and only fall back to an auto-derived name on first INSERT".

    The auto-derived fallback ("AcuRite Atlas" etc.) is built in upsert_device
    when row is brand new, NOT here, so a secondary source posting to an
    existing row (e.g. LilyGO posting to a row the Pi already named
    "AcuRite Atlas (SDR)") doesn't flip the name on every UPSERT."""
    dev = normalized.get("device") or {}
    explicit_name = dev.get("name")
    location = dev.get("location")
    return explicit_name, location


def _auto_device_name(normalized: dict[str, Any]) -> str:
    """Auto-generated name used ONLY on first INSERT of a device row."""
    dev = normalized.get("device") or {}
    src = normalized.get("source") or "custom"
    model = dev.get("model")
    pretty = {
        "acurite-atlas": "AcuRite Atlas",
        "acurite-access": "AcuRite Access",
        "ecowitt": "Ecowitt",
        "tempest": "Tempest",
    }.get(src, src.replace("-", " ").title())
    return f"{pretty}{f' ({model})' if model and model.lower() not in pretty.lower() else ''}"


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

    # Reject SDR rain-decode glitches: a sudden spike in cumulative yearly
    # rain that's physically impossible for the elapsed time. Real rain ramps
    # gradually; a glitch jumps for one reading then the counter returns to its
    # true value. We compare to the last *good* reading ("before" corroboration)
    # — since a dropped glitch is stored NULL, the next real reading resumes from
    # the good value, so isolated spikes drop cleanly without an "after" lookup.
    max_rate = settings.ingest_max_rain_rate_in_per_hr
    if flat.get("yearlyrainin") is not None and max_rate > 0:
        prev = await db.last_yearly_rain(mac)
        if prev is not None:
            last_val, last_ts = prev
            jump = flat["yearlyrainin"] - last_val
            elapsed_h = (flat["dateutc"] - last_ts) / 3_600_000.0
            if _is_rain_glitch(jump, elapsed_h, max_rate):
                log.warning(
                    "rain glitch dropped for %s: +%.2f in over %.3f h "
                    "— %.2f→%.2f", mac, jump, elapsed_h, last_val,
                    flat["yearlyrainin"])
                for k in ("yearlyrainin", "hourlyrainin", "eventrainin",
                          "dailyrainin", "weeklyrainin", "monthlyrainin"):
                    flat[k] = None

    explicit_name, location = _device_label(payload_obj)
    auto_name = _auto_device_name(payload_obj)
    info = {
        # `name` here is the operator-explicit value (None if not provided).
        # `info.auto_name` is the fallback used only on first INSERT.
        "name": explicit_name,
        "auto_name": auto_name,
        "info": {"name": explicit_name or auto_name,
                 "location": location,
                 "source": payload_obj.get("source")},
        "lastData": flat,
    }
    await db.upsert_device(mac, info)
    inserted = await db.insert_observations(
        mac, [{**flat, "_source": _truncate_source(payload_obj)}])
    return {"ok": True, "mac": mac, "inserted": inserted, "ts_ms": flat["dateutc"]}


# Per-request body size cap. A normal observation is ~500 bytes; even a
# rich Atlas message with lightning + battery + RSSI tops out around 2 KB.
# 64 KiB is generous headroom while making it impossible for a misbehaving
# source to OOM the worker by streaming megabytes into a single ingest.
INGEST_BODY_MAX_BYTES = 64 * 1024
# Trim the persisted source-object copy to this so a single fat _source
# can't bloat the observations table indefinitely. Loses bonus diagnostic
# fields but never drops the flat data we actually render in iOS.
INGEST_SOURCE_MAX_BYTES = 16 * 1024


async def _parse_json_body(request: Request) -> Any:
    """Parse a request body as JSON, returning a 400 on malformed input
    instead of letting FastAPI surface it as a 500. Also rejects Python's
    non-standard NaN/Infinity literals that some decoders emit, since
    they'd serialize back to non-JSON-compliant numbers downstream.
    Enforces a size cap to bound worker memory."""
    # Cheap early-reject via Content-Length so we don't read the body at
    # all when a misbehaving client claims an absurd size.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > INGEST_BODY_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"body too large; max {INGEST_BODY_MAX_BYTES} bytes")
        except ValueError:
            pass  # Malformed Content-Length — fall through to the read check.
    body = await request.body()
    if len(body) > INGEST_BODY_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"body too large; max {INGEST_BODY_MAX_BYTES} bytes")
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        return _json.loads(body, parse_constant=lambda _: None)
    except _json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e.msg}")


def _truncate_source(payload_obj: dict[str, Any]) -> dict[str, Any]:
    """Drop the _source copy down to INGEST_SOURCE_MAX_BYTES of JSON.
    Strategy: serialize once, check size; if over, replace with a small
    marker dict that retains key identifying fields (source tag, device
    block, timestamp) so we can still trace the row's provenance."""
    raw = _json.dumps(payload_obj, separators=(",", ":"))
    if len(raw) <= INGEST_SOURCE_MAX_BYTES:
        return payload_obj
    return {
        "_truncated": True,
        "_original_bytes": len(raw),
        "source": payload_obj.get("source"),
        "device": payload_obj.get("device"),
        "timestamp_utc": payload_obj.get("timestamp_utc"),
    }


# Token in header so it never appears in proxy/access logs.
@router.post("/ingest/custom")
async def ingest_custom_header(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ingest_token: Annotated[str | None, Header(alias="X-Ingest-Token")] = None,
) -> dict[str, Any]:
    _require_ingest_token(_token_from_header(authorization, x_ingest_token))
    return await _do_ingest(await _parse_json_body(request))

# (Legacy path-form `/ingest/custom/{token}` was removed 2026-05-21. The
# only consumer was the retired hub-relay container; tokens in URLs leak
# to proxy/access logs. Use the header form above for all new ingest.)
