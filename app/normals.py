"""Today-vs-normal (1.7) — NOAA 1991-2020 climate normals for the station's
location, so the app can say "3° above the normal high" instead of a bare
number.

Source: NCEI's free, keyless Access services (probed live 2026-08-21):
- search/v1/data?dataset=normals-daily&bbox=...   → nearby normals stations
- data/v1?dataset=normals-daily&units=standard    → real °F / inches strings
  keyed by DATE "MM-DD" (a full synthetic year; Feb 29 exists).

The killer property Volney approved this for: normals change once a DECADE,
so this is one fetch per station, cached in server_kv effectively forever
(re-fetched after ~180 days just to heal a bad cache). US-only by nature —
elsewhere the lookup finds no station and the endpoint answers 204-shaped
nothing; the app simply doesn't render the row. His explicit call: NEVER
fall back to computing "normals" from the user's own history.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import settings

log = logging.getLogger("normals")

NCEI_SEARCH = "https://www.ncei.noaa.gov/access/services/search/v1/data"
NCEI_DATA = "https://www.ncei.noaa.gov/access/services/data/v1"
_REFRESH_S = 180 * 24 * 3600            # cache heal interval, not a TTL
_BBOX_DEG = 0.35                        # ~25 mi half-side for station search

_KV_STATION = "normals.station"         # {"key": "lat,lon", "id", "name"}
_KV_DATA = "normals.data"               # {"station": id, "fetched_ms", "days": {...}}


def _coord_key(lat: float, lon: float) -> str:
    # Two decimals ≈ 1 km — enough to notice the station moving cities,
    # coarse enough that GPS jitter never busts the cache.
    return f"{lat:.2f},{lon:.2f}"


async def _find_station(lat: float, lon: float) -> tuple[str, str] | None:
    """(station_id, name) of a nearby normals station with daily temperature
    normals, or None (non-US, ocean, API down). "Nearby" not "nearest": the
    search result doesn't carry per-station coordinates, and a normals
    comparison against any station within ~25 mi is honest — the station
    NAME is shown to the user so nothing is hidden."""
    bbox = (f"{lat + _BBOX_DEG},{lon - _BBOX_DEG},"
            f"{lat - _BBOX_DEG},{lon + _BBOX_DEG}")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(NCEI_SEARCH, params={
                "dataset": "normals-daily", "bbox": bbox, "limit": 5})
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        log.warning("normals station search failed: %s", e)
        return None
    for result in body.get("results") or []:
        for st in result.get("stations") or []:
            covered = {t.get("id"): t.get("coverage")
                       for t in st.get("dataTypes") or []}
            if covered.get("DLY-TMAX-NORMAL"):
                return st.get("id"), st.get("name") or st.get("id")
    return None


async def _fetch_year(station_id: str) -> dict[str, dict[str, float]] | None:
    """Full synthetic year keyed "MM-DD" → {high, low, mtd_precip}. Values
    arrive as strings in real units under units=standard."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(NCEI_DATA, params={
                "dataset": "normals-daily",
                "stations": station_id,
                "dataTypes": "DLY-TMAX-NORMAL,DLY-TMIN-NORMAL,MTD-PRCP-NORMAL",
                "startDate": "2010-01-01", "endDate": "2010-12-31",
                "format": "json", "units": "standard",
            })
            r.raise_for_status()
            rows = r.json()
    except Exception as e:
        log.warning("normals data fetch failed: %s", e)
        return None

    def _f(row: dict, key: str) -> float | None:
        try:
            v = float(row.get(key))
        except (TypeError, ValueError):
            return None
        # NCEI's missing-value sentinels are large negative specials.
        return v if -900 < v < 20000 else None

    days: dict[str, dict[str, float]] = {}
    for row in rows if isinstance(rows, list) else []:
        date = row.get("DATE")
        if not date:
            continue
        entry = {}
        hi = _f(row, "DLY-TMAX-NORMAL")
        lo = _f(row, "DLY-TMIN-NORMAL")
        mtd = _f(row, "MTD-PRCP-NORMAL")
        if hi is not None:
            entry["high"] = hi
        if lo is not None:
            entry["low"] = lo
        if mtd is not None:
            entry["mtd_precip"] = mtd
        if entry:
            days[date] = entry
    return days or None


async def today(lat: float, lon: float) -> dict[str, Any] | None:
    """Today's normals for the location (server-local date), from cache when
    possible. None = no coverage — the caller renders nothing, never zero."""
    key = _coord_key(lat, lon)
    # Keys namespaced per location (CodeRabbit PR #27 + R6 finding 12): a
    # single global slot made two stations in different cities evict each
    # other on every alternating request — one NCEI station search plus a
    # full synthetic-year fetch per request, forever, on the request path.
    station_kv = f"{_KV_STATION}.{key}"
    st_raw = await db.get_kv(station_kv)
    station = None
    if st_raw:
        try:
            cached = json.loads(st_raw)
            if cached.get("key") == key:
                station = (cached["id"], cached.get("name") or cached["id"])
        except (ValueError, TypeError, KeyError):
            pass
    if station is None:
        station = await _find_station(lat, lon)
        if station is None:
            return None
        await db.set_kv(station_kv, json.dumps(
            {"key": key, "id": station[0], "name": station[1]}))

    station_id, station_name = station
    now_ms = int(time.time() * 1000)
    days = None
    data_kv = f"{_KV_DATA}.{station_id}"
    data_raw = await db.get_kv(data_kv)
    if data_raw:
        try:
            cached = json.loads(data_raw)
            if (cached.get("station") == station_id
                    and now_ms - int(cached.get("fetched_ms") or 0)
                        < _REFRESH_S * 1000):
                days = cached.get("days")
        except (ValueError, TypeError):
            pass
    if not days:
        days = await _fetch_year(station_id)
        if not days:
            return None
        await db.set_kv(data_kv, json.dumps(
            {"station": station_id, "fetched_ms": now_ms, "days": days}))

    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = None
    local = datetime.now(tz)
    date_key = local.strftime("%m-%d")
    entry = days.get(date_key)
    if not entry:
        return None
    return {"date": date_key, "station": station_name,
            "normal_high": entry.get("high"), "normal_low": entry.get("low"),
            "mtd_precip_normal": entry.get("mtd_precip"),
            "source": "NOAA/NCEI 1991-2020 U.S. Climate Normals"}
