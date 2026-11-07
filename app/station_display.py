"""Station display (2.8): the data behind /display.

A 1280x800 room display (a Raspberry Pi on a 10.1" screen, a TV, an old
tablet) that shows one station at a time: the current reading, a wind
compass, eight tiles, four 24-hour charts and the 7-day forecast. The page
itself is a static shell (static/display/); everything it shows comes from
`build()`, served two ways:

  /display/data   anonymous, only while the public dashboard is on, and only
                  the stations the public pages already show (by public id,
                  never the MAC; no coordinates, no indoor readings).
  /api/display    any read token, every weather station on the server.

Values are API-native (°F, mph, inHg, in) and finite or null: a station
without a sensor gets null and the page draws no tile, never a zero.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import almanac, db


def _settings():
    """The CURRENT settings object. The test suite reloads app.config per
    test while this module (and the fetch stubs installed on it) stays
    loaded, so a module-level binding would read the previous test's
    values (CodeRabbit, PR #129)."""
    from . import config
    return config.settings

log = logging.getLogger("station_display")

BUCKET_MS = 600_000                 # 10-minute chart points
WINDOW_MS = 24 * 3_600_000
TTL_S = 20.0                        # one build serves every viewer for 20 s
FORECAST_TTL_S = 1800.0
FORECAST_RETRY_S = 300.0           # after a failed fetch, wait this long to try again
ALERTS_TTL_S = 300.0

SERIES_FIELDS = ("tempf", "feelsLike", "windspeedmph", "windgustmph",
                 "baromrelin")
CURRENT_FIELDS = ("tempf", "feelsLike", "dewPoint", "humidity",
                  "windspeedmph", "windgustmph", "winddir", "baromrelin",
                  "hourlyrainin", "dailyrainin", "uv", "solarradiation",
                  "lightning_distance_mi", "lightning_last_3hr",
                  "lightning_last_1hr")
INDOOR_FIELDS = ("tempinf", "humidityin")
AIR_FIELDS = ("pm25", "pm10", "co2")

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_FORECAST: dict[tuple[float, float], tuple[float, list[dict[str, Any]]]] = {}
_ALERTS: dict[tuple[float, float], tuple[float, list[dict[str, Any]]]] = {}
_LOCK: asyncio.Lock | None = None


def _reset_for_tests() -> None:
    global _LOCK
    _CACHE.clear()
    _FORECAST.clear()
    _ALERTS.clear()
    _LOCK = None


def _num(v: Any) -> float | None:
    """Finite float or None: NaN and infinity never reach the page."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _tz() -> Any:
    try:
        return ZoneInfo(_settings().timezone)
    except Exception:                                # noqa: BLE001
        return timezone.utc


def _coords(device: dict[str, Any]) -> tuple[float, float] | None:
    info = device.get("info") or {}
    c = (info.get("coords") or {}).get("coords") or {}
    lat, lon = _num(c.get("lat")), _num(c.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    return lat, lon


def place_coords(devices: list[dict[str, Any]]) -> tuple[float, float] | None:
    """Where the forecast, sun times and warnings are asked for: the
    operator's forecast location when set, else the first station with
    coordinates."""
    cfg = _settings()
    lat, lon = _num(cfg.forecast_lat), _num(cfg.forecast_lon)
    if lat is not None and lon is not None:
        return lat, lon
    for d in devices:
        c = _coords(d)
        if c is not None:
            return c
    return None


# ── per-station series ────────────────────────────────────────────────

def downsample(rows: list[dict[str, Any]], start_ms: int,
               end_ms: int) -> tuple[list[int], dict[str, list[float | None]]]:
    """24 h of history as 10-minute points. Peaks (gust) keep the bucket
    maximum; everything else the mean. A bucket without a value is None,
    so the chart shows a gap instead of inventing a line."""
    n = max(1, (end_ms - start_ms) // BUCKET_MS)
    sums: dict[str, list[list[float]]] = {f: [[] for _ in range(n)] for f in SERIES_FIELDS}
    for r in rows:
        t = r.get("dateutc")
        if not isinstance(t, (int, float)) or t < start_ms or t >= end_ms:
            continue
        i = int((t - start_ms) // BUCKET_MS)
        if i >= n:
            continue
        for f in SERIES_FIELDS:
            v = _num(r.get(f + "_max")) if f == "windgustmph" else None
            if v is None:
                v = _num(r.get(f))
            if v is not None:
                sums[f][i].append(v)
    times = [start_ms + i * BUCKET_MS + BUCKET_MS // 2 for i in range(n)]
    series: dict[str, list[float | None]] = {}
    for f in SERIES_FIELDS:
        out = []
        for vals in sums[f]:
            if not vals:
                out.append(None)
            elif f == "windgustmph":
                out.append(round(max(vals), 1))
            else:
                out.append(round(sum(vals) / len(vals), 2))
        series[f] = out
    return times, series


def rain_by_hour(rows: list[dict[str, Any]], start_ms: int,
                 end_ms: int, tz: Any) -> list[float | None]:
    """Rain per clock hour, 24 bars. A gauge that reports a day total is
    read by the rise of that total within the hour (reset at local
    midnight); one that reports only a lifetime counter, by the counter's
    rise. A station with no rain fields at all gets all None."""
    hour0 = start_ms - (start_ms % 3_600_000)
    n = int((end_ms - hour0) // 3_600_000) + 1
    n = min(n, 25)
    per_hour: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    key = None
    for k in ("dailyrainin", "yearlyrainin"):
        if any(_num(r.get(k)) is not None for r in rows):
            key = k
            break
    if key is None:
        return [None] * n
    ordered = sorted((r for r in rows if isinstance(r.get("dateutc"), (int, float))),
                     key=lambda r: r["dateutc"])
    prev: float | None = None
    prev_day = None
    for r in ordered:
        v = _num(r.get(key))
        if v is None:
            continue
        t = int(r["dateutc"])
        day = datetime.fromtimestamp(t / 1000, tz).date()
        if prev is not None and (key == "yearlyrainin" or day == prev_day):
            rise = v - prev
            if 0 < rise < 5 and hour0 <= t < end_ms:
                i = int((t - hour0) // 3_600_000)
                if 0 <= i < n:
                    per_hour[i].append((t, rise))
        prev, prev_day = v, day
    return [round(sum(x for _, x in h), 3) for h in per_hour]


def day_extremes(rows: list[dict[str, Any]], since_ms: int
                 ) -> tuple[list[float] | None, list[float] | None]:
    """Today's high and low as [time_ms, °F], from the bucket extremes."""
    hi: list[float] | None = None
    lo: list[float] | None = None
    for r in rows:
        t = r.get("dateutc")
        if not isinstance(t, (int, float)) or t < since_ms:
            continue
        mx = _num(r.get("tempf_max"))
        mn = _num(r.get("tempf_min"))
        v = _num(r.get("tempf"))
        mx = mx if mx is not None else v
        mn = mn if mn is not None else v
        if mx is not None and (hi is None or mx > hi[1]):
            hi = [int(t), mx]
        if mn is not None and (lo is None or mn < lo[1]):
            lo = [int(t), mn]
    return hi, lo


def pressure_trend(times: list[int], press: list[float | None]) -> float | None:
    """Change over the last three hours, inHg; None without both ends."""
    pts = [(t, v) for t, v in zip(times, press) if v is not None]
    if len(pts) < 2:
        return None
    t_end, v_end = pts[-1]
    earlier = [v for t, v in pts if t <= t_end - 3 * 3_600_000]
    if not earlier:
        return None
    return round(v_end - earlier[-1], 3)


async def _fill_rain(mac: str, obs: dict[str, Any]) -> None:
    """A gauge that posts only a running counter (an SDR or LilyGO station)
    has no day or hour total on its reading, so the display's Rain today
    tile and headline went missing for it. Read them from the rain ledger
    the way /current does (main._fill_rain_periods), only for a station
    that has a gauge at all: no counter, no rain, never a zero."""
    if not any(obs.get(k) is not None for k in
               ("yearlyrainin", "monthlyrainin", "dailyrainin")):
        return
    if obs.get("dailyrainin") is not None and obs.get("hourlyrainin") is not None:
        return
    try:
        r = await db.rain_rollups(mac, _settings().timezone)
    except Exception as e:
        log.warning("rain_rollups failed for %s: %s", mac, e)
        return
    for k, src in (("dailyrainin", "daily_in"), ("hourlyrainin", "hourly_in")):
        if obs.get(k) is None and r.get(src) is not None:
            obs[k] = r[src]


async def station_block(dev: dict[str, Any], sid: str, *, public: bool,
                        now_ms: int, tz: Any) -> dict[str, Any] | None:
    mac = dev["mac"]
    cur = dict(await db.latest_observation(mac) or {})
    await _fill_rain(mac, cur)
    start = now_ms - WINDOW_MS
    rows = await db.history(mac, start, now_ms, limit=5000)
    times, series = downsample(rows, start, now_ms)
    midnight = int(almanac.local_midnight(datetime.fromtimestamp(now_ms / 1000, tz).date(), tz)
                   .timestamp() * 1000)
    hi, lo = day_extremes(rows, midnight)
    fields = CURRENT_FIELDS + (() if public else INDOOR_FIELDS)
    current = {f: _num(cur.get(f)) for f in fields}
    current["dateutc"] = _num(cur.get("dateutc"))
    gusts = [v for v in series["windgustmph"] if v is not None]
    return {
        "id": sid,
        "name": dev.get("name") or "Station",
        "current": current,
        "t": times,
        "series": series,
        "rain_hours": rain_by_hour(rows, start, now_ms, tz),
        "hi": hi,
        "lo": lo,
        "gust24": max(gusts) if gusts else None,
        "press3h": pressure_trend(times, series["baromrelin"]),
    }


# ── place: forecast, sun, warnings ────────────────────────────────────

async def _fetch_forecast(lat: float, lon: float) -> list[dict[str, Any]] | None:
    params = {
        "latitude": round(lat, 2), "longitude": round(lon, 2),
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
        "current": "weather_code",
        "temperature_unit": "fahrenheit", "timezone": "auto",
        "forecast_days": 7,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError):
        log.info("display forecast fetch failed")
        return None
    d = body.get("daily") or {}
    days = d.get("time") or []
    out = []
    for i, day in enumerate(days[:7]):
        def pick(k: str) -> Any:
            vals = d.get(k) or []
            return vals[i] if i < len(vals) else None
        code = pick("weather_code")
        pop = _num(pick("precipitation_probability_max"))
        out.append({"day": str(day),
                    "code": int(code) if _num(code) is not None else None,
                    "hi": _num(pick("temperature_2m_max")),
                    "lo": _num(pick("temperature_2m_min")),
                    "pop": round(pop) if pop is not None else None})
    now_code = (body.get("current") or {}).get("weather_code")
    if out and _num(now_code) is not None:
        out[0]["now_code"] = int(now_code)
    return out


async def _fetch_alerts(lat: float, lon: float) -> list[dict[str, Any]] | None:
    """The weather service's active alerts at a point (the same call the
    alert monitor makes). Its own name so the test suite can stub it."""
    from . import nws_watch
    return await nws_watch._fetch_active(lat, lon)


async def forecast(coords: tuple[float, float] | None) -> list[dict[str, Any]]:
    if coords is None:
        return []
    key = (round(coords[0], 2), round(coords[1], 2))
    hit = _FORECAST.get(key)
    if hit:
        # A good strip holds for the TTL; a remembered failure (an empty
        # entry) only until the retry delay, so a recovered upstream shows
        # up within minutes, not half an hour.
        ttl = FORECAST_TTL_S if hit[1] else FORECAST_RETRY_S
        if time.time() - hit[0] < ttl:
            return hit[1]
    got = await _fetch_forecast(*key)
    if got is None:
        # Remember the failure so an outage costs one 15 s timeout per
        # retry window, not one per build under the payload lock
        # (CodeRabbit, PR #129). A stale strip beats an empty one. It is
        # re-stamped so the next fetch is one retry delay away: left at its
        # old age it stayed expired, and every build retried the 15 s fetch
        # under the payload lock (CodeRabbit, PR #146).
        if hit and hit[1]:
            _FORECAST[key] = (time.time() - FORECAST_TTL_S + FORECAST_RETRY_S, hit[1])
            return hit[1]
        _FORECAST[key] = (time.time(), [])
        return []
    _FORECAST[key] = (time.time(), got)
    return got


async def active_alerts(coords: tuple[float, float] | None, *, public: bool,
                        now_ms: int) -> list[dict[str, Any]]:
    """Active weather-service alerts at the place, warnings first. Public
    mode carries the event and its end only: the area text names
    counties, which is more of the place than the public pages show."""
    if coords is None:
        return []
    from . import nws_watch
    key = (round(coords[0], 3), round(coords[1], 3))
    hit = _ALERTS.get(key)
    if hit and time.time() - hit[0] < ALERTS_TTL_S:
        raw = hit[1]
    else:
        got = await _fetch_alerts(*key)
        raw = got if got is not None else (hit[1] if hit else [])
        _ALERTS[key] = (time.time(), raw)
    out = []
    for a in raw:
        event = a.get("event")
        if not event or (a.get("status") or "Actual") != "Actual":
            continue
        ends = a.get("ends") or a.get("expires")
        ends_ms = None
        if isinstance(ends, str):
            try:
                ends_ms = int(datetime.fromisoformat(ends).timestamp() * 1000)
            except ValueError:
                ends_ms = None
        if ends_ms is not None and ends_ms < now_ms:
            continue
        item = {"event": str(event)[:80], "ends_ms": ends_ms,
                "warning": nws_watch.is_warning(event),
                "rank": nws_watch.severity_rank(a.get("severity"))}
        if not public and a.get("headline"):
            item["headline"] = str(a["headline"])[:200]
        out.append(item)
    out.sort(key=lambda x: (not x["warning"], -x["rank"]))
    for x in out:
        x.pop("rank", None)
    return out[:3]


def sun_times(coords: tuple[float, float] | None, now_ms: int, tz: Any) -> list[int] | None:
    if coords is None:
        return None
    day = datetime.fromtimestamp(now_ms / 1000, tz).date()
    rise = almanac.sunrise(coords[0], coords[1], day, tz)
    sets = almanac.sunset(coords[0], coords[1], day, tz)
    if rise is None or sets is None:
        return None
    if sets <= rise:
        # A server clock far from the station's own zone (UTC for an
        # Arizona yard) puts the evening's sunset at the start of its
        # day. The page needs the sunset that FOLLOWS this sunrise.
        nxt = almanac.sunset(coords[0], coords[1], day + timedelta(days=1), tz)
        if nxt is None:
            return None
        sets = nxt
    return [int(rise.timestamp() * 1000), int(sets.timestamp() * 1000)]


async def sunset_outlook(coords: tuple[float, float] | None, now_ms: int,
                         tz: Any) -> dict[str, Any] | None:
    """The sunset chip: pct, verdict and line for the next sunset, or None
    (no coordinates, no cloud forecast, or the model unreachable)."""
    if coords is None:
        return None
    from . import sunset as ss
    now = datetime.fromtimestamp(now_ms / 1000, tz)
    sets = ss.next_sunset(coords[0], coords[1], now, tz)
    if sets is None:
        return None
    try:
        out = await asyncio.wait_for(
            ss.outlook_at(coords[0], coords[1], sets, tz, _settings().timezone),
            timeout=10)
    except Exception:
        return None
    if not out:
        return None
    return {"pct": out["pct"], "verdict": out["verdict"],
            "line": out["line"], "sunset_ms": out["sunset_ms"]}


# ── the payload ───────────────────────────────────────────────────────

async def build(weather: list[tuple[str, dict[str, Any]]],
                air: dict[str, Any] | None, *, public: bool, place: str,
                coords: tuple[float, float] | None) -> dict[str, Any]:
    """Everything the page draws. `weather` is (id, device) per station
    in page order; `air` an air monitor's device or None."""
    now_ms = int(time.time() * 1000)
    tz = _tz()
    stations = []
    for sid, dev in weather:
        block = await station_block(dev, sid, public=public, now_ms=now_ms, tz=tz)
        if block is not None:
            stations.append(block)
    air_block = None
    if air is not None:
        a = await db.latest_observation(air["mac"]) or {}
        vals = {f: _num(a.get(f)) for f in AIR_FIELDS}
        if any(v is not None for v in vals.values()):
            air_block = {"name": air.get("name") or "Air", **vals}
    return {
        "v": 1,
        "generated": now_ms,
        "tz": _settings().timezone,
        "place": place,
        "public": public,
        "stations": stations,
        "air": air_block,
        "forecast": await forecast(coords),
        "sun": sun_times(coords, now_ms, tz),
        # 2.8: how good the next sunset looks (today's until it sets).
        "sunset_outlook": await sunset_outlook(coords, now_ms, tz),
        "alerts": await active_alerts(coords, public=public, now_ms=now_ms),
    }


async def cached(key: str, make) -> dict[str, Any]:
    """One build per TTL for every viewer: a wall display polls every 30 s
    and a public one may have many."""
    global _LOCK
    if _LOCK is None:                    # no await between test and assign
        _LOCK = asyncio.Lock()
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL_S:
        return hit[1]
    async with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < TTL_S:
            return hit[1]
        payload = await make()
        now = time.time()
        # Keys follow the public station set, so drop expired ones instead
        # of letting every sharing change leave an entry behind.
        for k in [k for k, (at, _) in _CACHE.items() if now - at >= TTL_S]:
            _CACHE.pop(k, None)
        _CACHE[key] = (now, payload)
        return payload
