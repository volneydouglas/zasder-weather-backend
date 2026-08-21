"""The Weather Company (TWC) forecast source.

The free WU PWS-owner API key unlocks exactly one forecast product —
`v3/wx/forecast/daily/5day` (verified live: 3day/7day/10day/hourly all
401 on a PWS key). This module fetches it and transforms the response
into the OPEN-METEO shape the app already parses, so choosing TWC in the
app changes the data source without touching the client's decoder.

Design rules (user decisions, 2026-08-12):
- 5 honest TWC days — never splice Open-Meteo days 6-7 onto the end;
  mixed-forecaster seams read as breakage.
- The caller (main.get_forecast) falls back to Open-Meteo on ANY failure
  here — WU deactivates keys when a station stops uploading, and WU has
  outages; the forecast strip must always work. The stored preference is
  never auto-flipped; TWC is retried on the next refresh.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("zasder.forecast_twc")

TWC_URL = "https://api.weather.com/v3/wx/forecast/daily/5day"

# TWC iconCode (0..47) → WMO weather_code (what the app's icon mapper
# reads). Approximate by weather family; unknown codes fall back to 3
# (overcast) — a bland icon beats a crash or a blank.
_ICON_TO_WMO = {
    0: 95, 1: 95, 2: 95, 3: 95, 4: 95,            # tornado/storms
    5: 68, 6: 68, 7: 71,                          # rain+snow mixes
    8: 56, 9: 51, 10: 66,                         # freezing drizzle/drizzle/freezing rain
    11: 80, 12: 63,                               # showers / rain
    13: 71, 14: 73, 15: 73, 16: 75,               # snow flurries → heavy snow
    17: 96, 18: 67, 35: 96,                       # hail / sleet / rain+hail
    19: 45, 20: 45, 21: 45, 22: 45,               # dust/fog/haze/smoke
    23: 2, 24: 2, 25: 2,                          # windy/frigid → fair-ish
    26: 3, 27: 3, 28: 3,                          # cloudy
    29: 2, 30: 2,                                 # partly cloudy
    31: 0, 32: 0,                                 # clear / sunny
    33: 1, 34: 1,                                 # fair
    36: 0,                                        # hot → clear sky
    37: 95, 38: 95, 47: 95,                       # thunderstorms
    39: 80, 40: 63, 45: 80,                       # scattered showers / rain
    41: 71, 42: 73, 43: 75, 46: 71,               # snow showers → heavy
    44: 3,                                        # not available
}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def transform(twc: dict[str, Any]) -> dict[str, Any]:
    """TWC 5-day JSON → Open-Meteo-shaped response (+ source marker)."""
    times = twc.get("validTimeLocal") or []
    n = len(times)
    cal_max = twc.get("calendarDayTemperatureMax") or []
    cal_min = twc.get("calendarDayTemperatureMin") or []
    alt_max = twc.get("temperatureMax") or []
    alt_min = twc.get("temperatureMin") or []
    dp = twc.get("daypart") or [{}]
    dp0 = dp[0] if isinstance(dp, list) and dp else {}
    dp_precip = dp0.get("precipChance") or []
    dp_wind = dp0.get("windSpeed") or []
    dp_dir = dp0.get("windDirection") or []
    dp_icon = dp0.get("iconCode") or []
    # The written forecast, which we already download and used to discard
    # (Doren's request, 2026-08-16). `narrative` is the prose and
    # `daypartName` is its own heading — and TWC advances that heading itself
    # ("Today"/"Tonight"/"Tomorrow"/"Tomorrow night"), so the app does not
    # need sunset arithmetic to know which half it is showing.
    dp_narrative = dp0.get("narrative") or []
    dp_name = dp0.get("daypartName") or []

    def at(arr: list, i: int) -> Any:
        return arr[i] if i < len(arr) else None

    days: dict[str, list] = {k: [] for k in (
        "time", "weather_code", "temperature_2m_max", "temperature_2m_min",
        "precipitation_probability_max", "wind_speed_10m_max",
        "wind_direction_10m_dominant")}

    for i in range(n):
        t = times[i]
        days["time"].append(t[:10] if isinstance(t, str) else None)
        # Day-0's calendar max goes null after mid-afternoon (the "day" part
        # is over); the plain temperatureMax still carries a value.
        hi = _num(at(cal_max, i))
        if hi is None:
            hi = _num(at(alt_max, i))
        lo = _num(at(cal_min, i))
        if lo is None:
            lo = _num(at(alt_min, i))
        days["temperature_2m_max"].append(hi)
        days["temperature_2m_min"].append(lo)
        # Dayparts interleave day/night (2 per calendar day); either half can
        # be null (day-0 evening). Take the worst precip chance, the peak
        # wind, and prefer the daytime icon/direction.
        d_idx, n_idx = 2 * i, 2 * i + 1
        chances = [c for c in (_num(at(dp_precip, d_idx)), _num(at(dp_precip, n_idx)))
                   if c is not None]
        days["precipitation_probability_max"].append(
            int(max(chances)) if chances else None)
        winds = [w for w in (_num(at(dp_wind, d_idx)), _num(at(dp_wind, n_idx)))
                 if w is not None]
        days["wind_speed_10m_max"].append(max(winds) if winds else None)
        wdir = _num(at(dp_dir, d_idx))
        if wdir is None:
            wdir = _num(at(dp_dir, n_idx))
        days["wind_direction_10m_dominant"].append(wdir)
        icon = at(dp_icon, d_idx)
        if icon is None:
            icon = at(dp_icon, n_idx)
        days["weather_code"].append(
            _ICON_TO_WMO.get(int(icon), 3) if isinstance(icon, (int, float)) else 3)

    if not days["time"]:
        # A 200 with an empty/malformed payload ({} transforms into
        # valid-shaped all-empty arrays) must not count as a successful TWC
        # forecast — the iOS strip would render blank instead of falling
        # back. Raising here routes the caller's blanket except to the
        # marked Open-Meteo fallback ("the strip must always work").
        raise ValueError("TWC returned no daily forecast")

    # Dayparts, in TWC's own order, as a flat sequence rather than folded into
    # `days` — they are twice as many as the days and the first one expires at
    # mid-afternoon, so index 0 is simply "the half we are in now". Nulls are
    # dropped rather than kept as placeholders, which is what makes that true.
    narrative = [
        {"name": name, "text": text}
        for name, text in zip(dp_name, dp_narrative)
        if isinstance(name, str) and isinstance(text, str) and text.strip()
    ]
    return {"daily": days, "timezone": None, "source": "twc",
            "narrative": narrative}


async def fetch(lat: float, lon: float, api_key: str) -> dict[str, Any]:
    """Fetch + transform. Raises on any upstream problem — the caller owns
    the Open-Meteo fallback. The key travels as a query parameter, so error
    messages must never include the URL."""
    params = {"geocode": f"{lat},{lon}", "format": "json", "units": "e",
              "language": "en-US", "apiKey": api_key}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(TWC_URL, params=params)
        r.raise_for_status()
        return transform(r.json())
