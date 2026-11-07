"""Sunset outlook (2.8, Doren 10-17): how good tonight's sunset looks, 0-100%.

A score, its reasons in plain words, and one line for the reports:
"Sunset 5:37 PM, looks good: 72%, broken high cloud and a clear horizon".
Pure scoring here; the fetch at the bottom reads Open-Meteo's hourly model
at the sunset hour. The alert monitor and the reports gather and deliver.

Why this formula (simple, explainable, no learning):

  The colour in a sunset is sunlight passing UNDER the clouds after the sun
  has dropped and lighting their bases from below. That gives three
  questions, in order of how much they matter:

  1. Is the western horizon open? (LOW cloud, below ~2 km.) Low cloud sits
     between you and the sun, so the light never reaches the sky above it.
     This is a gate: an overcast of low cloud is a dull sunset whatever
     else is up there, so the horizon MULTIPLIES everything.
  2. Is there a canvas? (MID and HIGH cloud, ~2-12 km.) Broken altocumulus
     and cirrus are what catch the light; 30-70% cover is the sweet spot.
     A perfectly clear sky is a clean but plain sunset (still a sunset, so
     it keeps a floor of 30%); a solid high deck mutes it again.
  3. Is the air clear? Visibility and humidity wash colour out; a little
     aerosol (aerosol optical depth ~0.1-0.4) deepens the reds, heavy
     smoke or dust dims everything. A multiplier, 0.4-1.0.

  A small bonus when rain fell in the three hours before sunset and has
  stopped: the air is scrubbed and the clouds are breaking, the classic
  after-the-storm sky.

      score = horizon(low) x (0.3 + 0.7 x canvas(max(mid, high))) x air + bonus

  Each factor is a piecewise-linear curve through the points below, so a
  reader can see exactly why tonight scored what it did. Absent data is
  never a zero: without any cloud-layer forecast there is no score at all,
  and a missing visibility, humidity or aerosol reading is neutral (1.0).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

log = logging.getLogger("zasder.sunset")

# (value, factor) points; linear between, flat beyond the ends.
HORIZON_PTS = ((0, 1.0), (10, 1.0), (30, 0.75), (50, 0.45), (70, 0.2), (90, 0.05), (100, 0.0))
CANVAS_PTS = ((0, 0.0), (15, 0.35), (30, 0.85), (40, 1.0), (70, 1.0), (85, 0.7), (100, 0.4))
VIS_KM_PTS = ((0, 0.3), (2, 0.4), (5, 0.7), (10, 0.9), (20, 1.0))
HUMIDITY_PTS = ((0, 1.0), (70, 1.0), (85, 0.85), (95, 0.65), (100, 0.55))
AOD_PTS = ((0.0, 0.95), (0.1, 1.0), (0.4, 1.0), (0.6, 0.85), (1.0, 0.6), (2.0, 0.4))
CANVAS_FLOOR = 0.3          # a clear sky is a plain sunset, not no sunset
RAIN_CLEARING_BONUS = 0.08
RAIN_BEFORE_IN = 0.01       # rain that counts in the 3 h before sunset
RAIN_AT_SUNSET_IN = 0.004   # ...and has stopped by the sunset hour

GOOD_PCT = 70               # "looks good"; the alert's default threshold
NICE_PCT = 45
FAIR_PCT = 25


def _curve(pts: tuple[tuple[float, float], ...], x: float) -> float:
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


@dataclass(frozen=True)
class SunsetInputs:
    low_pct: float | None = None       # cloud below ~2 km
    mid_pct: float | None = None       # ~2-6 km
    high_pct: float | None = None      # above ~6 km
    visibility_km: float | None = None
    humidity_pct: float | None = None
    aod: float | None = None           # aerosol optical depth (550 nm)
    rain_before_in: float | None = None  # rain in the 3 h before sunset
    rain_at_in: float | None = None      # rain in the sunset hour

    def has_clouds(self) -> bool:
        return any(v is not None for v in (self.low_pct, self.mid_pct, self.high_pct))


@dataclass(frozen=True)
class SunsetScore:
    pct: int
    verdict: str
    reasons: list[str] = field(default_factory=list)


def verdict(pct: int) -> str:
    return ("looks good" if pct >= GOOD_PCT else "could be nice" if pct >= NICE_PCT
            else "fair" if pct >= FAIR_PCT else "looks dull")


def score(i: SunsetInputs) -> SunsetScore | None:
    """The 0-100 score and its reasons (most telling first), or None when
    there is no cloud forecast to judge by."""
    if not i.has_clouds():
        return None
    low = max(0.0, min(100.0, i.low_pct or 0.0))
    mid = max(0.0, min(100.0, i.mid_pct or 0.0))
    high = max(0.0, min(100.0, i.high_pct or 0.0))
    upper = max(mid, high)
    horizon = _curve(HORIZON_PTS, low)
    canvas = _curve(CANVAS_PTS, upper)
    air = 1.0
    haze: list[str] = []
    if i.visibility_km is not None:
        f = _curve(VIS_KM_PTS, max(0.0, i.visibility_km))
        air *= f
        if f < 0.85:
            haze.append("haze")
    if i.humidity_pct is not None:
        f = _curve(HUMIDITY_PTS, max(0.0, min(100.0, i.humidity_pct)))
        air *= f
        if f < 0.85:
            haze.append("very humid air")
    if i.aod is not None:
        f = _curve(AOD_PTS, max(0.0, i.aod))
        air *= f
        if f < 0.85:
            haze.append("smoke or dust in the air")
    clearing = (i.rain_before_in is not None and i.rain_before_in >= RAIN_BEFORE_IN
                and (i.rain_at_in or 0.0) < RAIN_AT_SUNSET_IN and low < 50)
    total = horizon * (CANVAS_FLOOR + (1 - CANVAS_FLOOR) * canvas) * air
    if clearing:
        total += RAIN_CLEARING_BONUS
    pct = int(round(max(0.0, min(1.0, total)) * 100))

    # The words, in the order a person would say them: the canvas, then the
    # horizon, then whatever is spoiling or helping it.
    reasons: list[str] = []
    layer = "high" if high >= mid else "mid-level"
    if upper < 10:
        reasons.append("a clear sky, so plain colors")
    elif upper < 30:
        reasons.append(f"a little {layer} cloud")
    elif upper <= 75:
        reasons.append(f"broken {layer} cloud")
    else:
        reasons.append(f"a thick {layer} cloud deck")
    if low <= 10:
        reasons.append("a clear horizon")
    elif low < 40:
        reasons.append("some low cloud on the horizon")
    else:
        reasons.append("low cloud blocking the horizon")
    reasons.extend(haze)
    if clearing:
        reasons.append("rain clearing just before")
    return SunsetScore(pct=pct, verdict=verdict(pct), reasons=reasons)


def join_reasons(reasons: list[str]) -> str:
    if not reasons:
        return ""
    if len(reasons) == 1:
        return reasons[0]
    return ", ".join(reasons[:-1]) + " and " + reasons[-1]


def line(sunset_local: datetime | None, s: SunsetScore) -> str:
    """"Sunset 5:37 PM, looks good: 72%, broken high cloud and a clear horizon"."""
    head = f"Sunset {sunset_local.strftime('%-I:%M %p')}, " if sunset_local else "Sunset "
    tail = join_reasons(s.reasons)
    return f"{head}{s.verdict}: {s.pct}%" + (f", {tail}" if tail else "")


# ── inputs from Open-Meteo's hourly model ──────────────────────────────

HOURLY = ("cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
          "visibility", "relative_humidity_2m", "precipitation")
CACHE_TTL_S = 30 * 60
_CACHE: dict[tuple[float, float, str], tuple[float, dict[str, Any]]] = {}
# A failed forecast fetch is remembered for this long, so an Open-Meteo
# outage costs one timeout per place every five minutes rather than one on
# every alert tick and every display refresh (CodeRabbit, PR #136).
FAIL_RETRY_S = 5 * 60
_FAILED: dict[tuple[float, float, str], float] = {}


def inputs_at(hourly: dict[str, Any], aq_hourly: dict[str, Any] | None,
              sunset_local: datetime) -> SunsetInputs:
    """The hourly series (Open-Meteo, station-local ISO times, inches for
    precipitation, metres for visibility) read at the sunset instant:
    linear between the two hours around it, rain summed over the three
    hours before."""
    times = hourly.get("time") or []
    base = sunset_local.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    frac = sunset_local.minute / 60.0
    idx = {t: n for n, t in enumerate(times) if isinstance(t, str)}

    def at(series: list[Any] | None, when: datetime) -> float | None:
        if not isinstance(series, list):
            return None
        n = idx.get(when.strftime("%Y-%m-%dT%H:%M"))
        return _finite(series[n]) if n is not None and n < len(series) else None

    def interp(key: str, src: dict[str, Any] | None = None) -> float | None:
        s = (src if src is not None else hourly).get(key)
        a, b = at(s, base), at(s, base + timedelta(hours=1))
        if a is None:
            return b
        if b is None:
            return a
        return a + (b - a) * frac

    vis_m = interp("visibility")
    rain_before = None
    hours_before = [at(hourly.get("precipitation"), base - timedelta(hours=k)) for k in (1, 2, 3)]
    if any(v is not None for v in hours_before):
        rain_before = sum(v for v in hours_before if v is not None)
    aod = None
    if aq_hourly:
        a_times = aq_hourly.get("time") or []
        a_idx = {t: n for n, t in enumerate(a_times) if isinstance(t, str)}
        series = aq_hourly.get("aerosol_optical_depth")
        n = a_idx.get(base.strftime("%Y-%m-%dT%H:%M"))
        if isinstance(series, list) and n is not None and n < len(series):
            aod = _finite(series[n])
    return SunsetInputs(
        low_pct=interp("cloud_cover_low"), mid_pct=interp("cloud_cover_mid"),
        high_pct=interp("cloud_cover_high"),
        visibility_km=(vis_m / 1000.0) if vis_m is not None else None,
        humidity_pct=interp("relative_humidity_2m"),
        aod=aod, rain_before_in=rain_before,
        rain_at_in=at(hourly.get("precipitation"), base))


async def _fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.warning("sunset fetch failed (%s)", type(e).__name__)
        return None


AQ_TIMEOUT_S = 6.0


async def _aq(lat: float, lon: float, tz_name: str) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(_fetch_json(
            "https://air-quality-api.open-meteo.com/v1/air-quality", {
                "latitude": lat, "longitude": lon, "hourly": "aerosol_optical_depth",
                "timezone": tz_name, "forecast_days": 3}), timeout=AQ_TIMEOUT_S)
    except Exception:
        return None


async def _hourly(lat: float, lon: float, tz_name: str) -> dict[str, Any] | None:
    key = (round(lat, 2), round(lon, 2), tz_name)
    hit = _CACHE.get(key)
    if hit is not None and time.monotonic() - hit[0] <= CACHE_TTL_S:
        return hit[1]
    failed_at = _FAILED.get(key)
    if failed_at is not None and time.monotonic() - failed_at < FAIL_RETRY_S:
        return None
    # Aerosols are a nicety: the air-quality model failing costs only that
    # factor (neutral), never the outlook. Fetched alongside the forecast and
    # capped at AQ_TIMEOUT_S, so a slow one adds nothing past the forecast's
    # own answer beyond that cap and cannot sink the display's 10-second
    # budget (CodeRabbit, PR #136).
    aq_task = asyncio.ensure_future(_aq(lat, lon, tz_name))
    try:
        body = await _fetch_json("https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon, "hourly": ",".join(HOURLY),
            "precipitation_unit": "inch", "timezone": tz_name, "forecast_days": 3})
    except BaseException:
        aq_task.cancel()
        raise
    if body is None:
        aq_task.cancel()
        _FAILED[key] = time.monotonic()
        if len(_FAILED) > 32:
            _FAILED.pop(next(iter(_FAILED)))
        return None
    _FAILED.pop(key, None)
    aq = await aq_task
    out = {"hourly": body.get("hourly") or {}, "aq": (aq or {}).get("hourly")}
    _CACHE[key] = (time.monotonic(), out)
    if len(_CACHE) > 32:
        _CACHE.pop(next(iter(_CACHE)))
    return out


def reset_cache() -> None:
    _CACHE.clear()
    _FAILED.clear()


def evening_sunset(lat: float, lon: float, day: date, tz: tzinfo) -> datetime | None:
    """The sunset that ends `day`'s daylight, aware. `almanac.sunset` gives
    the one inside the date in `tz`, and a server zone far from the
    station's (UTC for an Arizona yard) puts the previous evening's sunset
    at the start of that date, before the sunrise. Then the evening wanted
    is the next one, the correction `station_display.sun_times` makes
    (CodeRabbit, PR #136)."""
    from . import almanac
    sets = almanac.sunset(lat, lon, day, tz)
    rise = almanac.sunrise(lat, lon, day, tz)
    if sets is not None and rise is not None and sets <= rise:
        return almanac.sunset(lat, lon, day + timedelta(days=1), tz)
    return sets


def next_sunset(lat: float, lon: float, now: datetime, tz: tzinfo) -> datetime | None:
    """The first sunset after `now`, aware, or None (polar day or night).
    For the "next sunset" callers (the heads-up, /display, the app's row),
    which must not skip one still to come: on a UTC server an Arizona
    sunset at 00:35 is upcoming at 00:10, though `evening_sunset` for that
    date has moved on to the next evening (CodeRabbit, PR #136)."""
    from . import almanac
    day = now.astimezone(tz).date()
    for d in (day - timedelta(days=1), day, day + timedelta(days=1),
              day + timedelta(days=2)):
        s = almanac.sunset(lat, lon, d, tz)
        if s is not None and s > now:
            return s
    return None


async def outlook_for(lat: float, lon: float, day: date, tz: tzinfo,
                      tz_name: str) -> dict[str, Any] | None:
    """The sunset outlook for `day`'s evening at (lat, lon), as a plain
    dict, or None when there is no sunset (polar day/night) or no cloud
    forecast. The reports' form: a named day."""
    sunset_utc = evening_sunset(lat, lon, day, tz)
    if sunset_utc is None:
        return None
    out = await outlook_at(lat, lon, sunset_utc, tz, tz_name)
    if out is not None:
        out["date"] = day.isoformat()
    return out


async def outlook_at(lat: float, lon: float, sunset_utc: datetime, tz: tzinfo,
                     tz_name: str) -> dict[str, Any] | None:
    """The outlook for one sunset instant (`next_sunset`'s answer)."""
    sunset_local = sunset_utc.astimezone(tz)
    data = await _hourly(lat, lon, tz_name)
    if data is None:
        return None
    s = score(inputs_at(data["hourly"], data.get("aq"), sunset_local))
    if s is None:
        return None
    return {"date": sunset_local.date().isoformat(),
            "sunset": sunset_local.strftime("%H:%M"),
            "sunset_ms": int(sunset_utc.timestamp() * 1000),
            "pct": s.pct, "verdict": s.verdict, "reasons": list(s.reasons),
            "line": line(sunset_local, s)}
