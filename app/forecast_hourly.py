"""Hourly forecast for the charts (2.6, research item 12a).

The history charts draw a station's readings up to now; this is what
continues them, dashed, over the next day or two. Open-Meteo's hourly
model at the station's own coordinates, converted to the units the
station stores (degF, mph, inHg, inches, W/m2), keyed by the same API
field names the history uses, so a chart looks its series up by its own
field and nothing in the app converts twice.

TWC is not offered: a free WU key answers 401 on TWC's hourly product
(verified 2026-09-28), so every owner gets the same model here whatever
their daily source is. A value Open-Meteo did not send is None, never 0
(absent is not zero).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Open-Meteo hourly variable -> the station's API field name.
FIELDS: dict[str, str] = {
    "temperature_2m": "tempf",
    "apparent_temperature": "feelsLike",
    "dew_point_2m": "dewPoint",
    "relative_humidity_2m": "humidity",
    "wind_speed_10m": "windspeedmph",
    "wind_gusts_10m": "windgustmph",
    "pressure_msl": "baromrelin",
    "precipitation": "hourlyrainin",
    "uv_index": "uv",
    # The INSTANT value: plain shortwave_radiation is the mean of the hour
    # before its timestamp, which plotted beside a reading describes the
    # wrong interval on a rising or falling sun (CodeRabbit, PR #85).
    "shortwave_radiation_instant": "solarradiation",
}
HPA_TO_INHG = 0.0295299830714
MAX_HOURS = 72
DEFAULT_HOURS = 48
CACHE_TTL_S = 30 * 60

# (lat, lon) rounded to the model's grid -> (fetched_at, response).
_CACHE: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def transform(body: dict[str, Any], hours: int, now_ms: int) -> dict[str, Any]:
    """Open-Meteo's hourly block (unixtime) -> {"times_ms", "series"}, from
    the current hour on, at most `hours` points. Pressure becomes inHg,
    rounded the way a barometer reads; everything else is already in the
    station's units because the request asked for them."""
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    hour_start = now_ms - now_ms % 3_600_000
    keep = [i for i, t in enumerate(times)
            if isinstance(t, (int, float)) and int(t) * 1000 >= hour_start][:hours]
    series: dict[str, list[float | None]] = {}
    for om, field in FIELDS.items():
        raw = hourly.get(om)
        if not isinstance(raw, list):
            continue
        vals = []
        for i in keep:
            v = _finite(raw[i]) if i < len(raw) else None
            if v is not None and om == "pressure_msl":
                v = round(v * HPA_TO_INHG, 3)
            vals.append(v)
        if any(v is not None for v in vals):
            series[field] = vals
    return {"times_ms": [int(times[i]) * 1000 for i in keep],
            "series": series, "source": "open-meteo"}


async def fetch(lat: float, lon: float, hours: int = DEFAULT_HOURS,
                now_ms: int | None = None) -> dict[str, Any]:
    """The hourly forecast at (lat, lon), cached per grid point for half an
    hour so every chart a phone opens does not ask the model again."""
    hours = max(1, min(int(hours), MAX_HOURS))
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    key = (round(lat, 2), round(lon, 2))
    hit = _CACHE.get(key)
    if hit is None or time.monotonic() - hit[0] > CACHE_TTL_S:
        params = {
            "latitude": lat, "longitude": lon,
            "hourly": ",".join(FIELDS),
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timeformat": "unixtime",
            "timezone": "UTC", "forecast_hours": MAX_HOURS + 1,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
            body = r.json()
        _CACHE[key] = (time.monotonic(), body)
        if len(_CACHE) > 64:
            _CACHE.pop(next(iter(_CACHE)))
    else:
        body = hit[1]
    return transform(body, hours, now_ms)
