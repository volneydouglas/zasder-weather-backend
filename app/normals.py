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
# Progressive search rings, ~5.5 / ~10 / ~25 mi half-sides. The old single
# 0.35° box took the FIRST station NCEI returned anywhere inside ~50 miles
# of span — which handed Chandler the Fountain Hills normals: 28 miles out
# in the McDowell foothills, a genuinely different desert microclimate
# (Volney, live 2026-08-27). Rings restore closeness bias without needing
# per-station coordinates the search result doesn't carry: the first hit
# at the SMALLEST radius wins.
_BBOX_RINGS_DEG = (0.08, 0.15, 0.35)

# ".v2": the ring fix must re-resolve every cached station choice — the
# old key would pin the far station forever (the cache is deliberately
# eternal). Old-key values are simply orphaned.
_KV_STATION = "normals.station.v2"      # {"key": "lat,lon", "id", "name"}
_KV_DATA = "normals.data"               # {"station": id, "fetched_ms", "days": {...}}


def _coord_key(lat: float, lon: float) -> str:
    # Two decimals ≈ 1 km — enough to notice the station moving cities,
    # coarse enough that GPS jitter never busts the cache.
    return f"{lat:.2f},{lon:.2f}"


async def _find_station(lat: float, lon: float) -> tuple[str, str] | None:
    """(station_id, name) of the CLOSEST-ring normals station with daily
    temperature normals, or None (non-US, ocean, API down). The search
    result carries no per-station coordinates, so closeness comes from
    expanding rings: a hit inside ~5 miles beats anything a wider box
    would return first. The station NAME is shown to the user either way,
    so the choice is always visible."""
    for ring in _BBOX_RINGS_DEG:
        bbox = (f"{lat + ring},{lon - ring},"
                f"{lat - ring},{lon + ring}")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(NCEI_SEARCH, params={
                    "dataset": "normals-daily", "bbox": bbox, "limit": 10})
                r.raise_for_status()
                body = r.json()
        except Exception as e:
            # continue, not return: a transient failure on an inner ring
            # must not abandon the wider rings — the whole point of the
            # progressive search is that SOME ring answers (R11).
            log.warning("normals station search failed (ring %s): %s",
                        ring, e)
            continue
        try:
            # Type-guarded walk (CodeRabbit): the whole response is remote
            # input — a valid-JSON scalar or malformed nested entry raised
            # OUTSIDE the fetch's except and 500'd the endpoint instead of
            # trying the wider rings.
            results = body.get("results") if isinstance(body, dict) else None
            for result in results or []:
                if not isinstance(result, dict):
                    continue
                stations = result.get("stations")
                for st in stations if isinstance(stations, list) else []:
                    if not isinstance(st, dict):
                        continue
                    types = st.get("dataTypes")
                    covered = {t.get("id"): t.get("coverage")
                               for t in (types if isinstance(types, list)
                                         else [])
                               if isinstance(t, dict)}
                    if covered.get("DLY-TMAX-NORMAL"):
                        # A covered entry with a junk id must not stop the
                        # wider-ring search (CodeRabbit).
                        sid = st.get("id")
                        if not isinstance(sid, str) or not sid.strip():
                            continue
                        sid = sid.strip()   # guard checked stripped; RETURN it (R14)
                        name = st.get("name")
                        return sid, (name.strip() if isinstance(name, str)
                                     and name.strip() else sid)
        except Exception as e:
            log.warning("normals station search: malformed NCEI response "
                        "on ring %s: %s", ring, e)
            continue
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


async def cached_year(lat: float, lon: float
                      ) -> tuple[str, dict[str, dict[str, float]]] | None:
    """(NCEI station name, the cached synthetic year keyed "MM-DD") for this
    location, from `server_kv` ONLY. Never touches the network.

    `today()` above is the request-path reader and it FETCHES on a cold
    cache: a station search plus a full-year download, on the request. A
    story producer runs inside a loop over every producer on every
    /stories call and cannot afford that, and a producer that blocks on
    NCEI is a producer that times out the whole feed when NCEI is slow.
    So the story engine reads whatever `today()` has already cached and
    declines its "vs normal" line when nothing is there. The cache fills
    the first time the app opens the Today-vs-normal row, which every
    install does.

    Age is deliberately not checked: normals change once a decade, the
    180-day heal interval exists to fix a BAD cache, and an old copy of a
    1991-2020 normal is the same number as a fresh one.
    """
    key = _coord_key(lat, lon)
    st_raw = await db.get_kv(f"{_KV_STATION}.{key}")
    if not st_raw:
        return None
    try:
        cached = json.loads(st_raw)
        if cached.get("key") != key:
            return None
        station_id = str(cached["id"])
        name = cached.get("name") or station_id
    except (ValueError, TypeError, KeyError):
        return None
    data_raw = await db.get_kv(f"{_KV_DATA}.{station_id}")
    if not data_raw:
        return None
    try:
        data = json.loads(data_raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("station") != station_id:
        return None
    days = data.get("days")
    if not isinstance(days, dict) or not days:
        return None
    return str(name), days
