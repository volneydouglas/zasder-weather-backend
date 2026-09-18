"""Sky notes (2.2, Doren, Celestron NexStar 8SE): a push around sunset
with tonight's sun and moon, and a stargazing verdict from what the
station itself measures (humidity, dew spread, wind) plus tonight's
cloud cover from the forecast and the moon's light. "Good night for the
scope" only when it is; the owner can ask for the note only on those
nights.

Pure scoring here; the alert monitor gathers the inputs and delivers.
The thresholds are a first cut Doren can tune; each reason is named in
the note so a wrong call is legible.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger("zasder.sky")


@dataclass(frozen=True)
class SkyInputs:
    cloud_pct: float | None          # mean cloud cover tonight, 0..100
    humidity_pct: float | None       # station, now
    dew_spread_f: float | None       # tempf - dewPoint, station, now
    wind_mph: float | None           # station, now
    moon_illumination: float | None  # 0..1
    moon_up: bool | None             # above the horizon mid-evening


GOOD = "good"
FAIR = "fair"
POOR = "poor"
# No cloud forecast and no station reading: the moon alone is not a sky
# (2.2 release review R22-08). The note then carries the sun and moon
# and says it cannot judge the night.
UNKNOWN = "unknown"


def has_weather_evidence(i: SkyInputs) -> bool:
    return any(v is not None for v in (i.cloud_pct, i.humidity_pct, i.dew_spread_f, i.wind_mph))


def score(i: SkyInputs) -> tuple[float, list[str]]:
    """0..1 and the reasons that cost points, worst first. A missing
    input is neither a point for nor against: the verdict is made from
    what is known and says so when little is."""
    parts: list[tuple[float, float, str | None]] = []   # (score, weight, reason)
    if i.cloud_pct is not None:
        c = max(0.0, min(100.0, i.cloud_pct))
        s = 1.0 if c <= 15 else 0.6 if c <= 35 else 0.25 if c <= 60 else 0.0
        parts.append((s, 3.0, None if s == 1.0 else f"{c:.0f}% cloud"))
    if i.moon_illumination is not None:
        m = max(0.0, min(1.0, i.moon_illumination))
        if i.moon_up is False:
            s = 1.0           # a bright moon below the horizon costs nothing
        else:
            # Weighted like cloud: a full moon up washes out the deep sky
            # the way an overcast does, whatever the rest of the night.
            s = 1.0 if m <= 0.25 else 0.7 if m <= 0.55 else 0.35 if m <= 0.85 else 0.0
        # The note's lead already says "First Quarter, 38% lit"; a reason
        # that repeated the number read twice in one push (Doren, 09-17:
        # "First Quarter, 38% lit ... moon 38% lit"). The reason names the
        # effect, the lead keeps the figure.
        parts.append((s, 3.0, None if s == 1.0
                      else "a bright moon up" if m > 0.85 else "moonlight"))
    if i.dew_spread_f is not None:
        d = i.dew_spread_f
        s = 1.0 if d >= 8 else 0.6 if d >= 4 else 0.2
        parts.append((s, 1.5, None if s == 1.0 else f"dew point {d:.0f}° below the air, dew or fog likely"))
    if i.humidity_pct is not None:
        h = i.humidity_pct
        s = 1.0 if h <= 65 else 0.6 if h <= 80 else 0.3
        parts.append((s, 1.0, None if s == 1.0 else f"humidity {h:.0f}%"))
    if i.wind_mph is not None:
        w = i.wind_mph
        s = 1.0 if w <= 8 else 0.6 if w <= 14 else 0.2
        parts.append((s, 1.0, None if s == 1.0 else f"wind {w:.0f} mph"))
    if not parts:
        return 0.0, ["no readings to judge by"]
    total = sum(s * w for s, w, _ in parts) / sum(w for _, w, _ in parts)
    # An overcast is poor on its own: the other inputs cannot buy it back.
    if i.cloud_pct is not None and i.cloud_pct > 60:
        total = min(total, 0.3)
    reasons = [r for s, w, r in sorted(parts, key=lambda p: p[0] * p[1]) if r]
    return round(total, 3), reasons


def verdict(s: float) -> str:
    return GOOD if s >= 0.75 else FAIR if s >= 0.45 else POOR


def note(*, sunset_local: datetime | None, sunrise_next_local: datetime | None,
         moon_phase: str | None, moon_illumination: float | None,
         inputs: SkyInputs) -> tuple[str, str, str]:
    """(title, body, verdict). Plain words; the sun and moon first, the
    scope's verdict last with its reasons."""
    s, reasons = score(inputs)
    v = verdict(s) if has_weather_evidence(inputs) else UNKNOWN
    bits: list[str] = []
    if sunset_local is not None:
        bits.append(f"Sunset {sunset_local.strftime('%-I:%M %p')}")
    if sunrise_next_local is not None:
        bits.append(f"sunrise {sunrise_next_local.strftime('%-I:%M %p')}")
    if moon_phase:
        m = moon_phase
        if moon_illumination is not None:
            m += f", {moon_illumination * 100:.0f}% lit"
        if inputs.moon_up is False:
            m += ", below the horizon this evening"
        bits.append(m)
    lead = ". ".join(bits) + "." if bits else ""
    if v == UNKNOWN:
        head = "Sky tonight"
        tail = "No station reading or cloud forecast to judge the night by."
    elif v == GOOD:
        head = "Good night for the scope"
        tail = "Clear, dry and calm." if not reasons else "Mostly clear; " + ", ".join(reasons) + "."
    elif v == FAIR:
        head = "A fair night for the scope"
        tail = ", ".join(reasons).capitalize() + "." if reasons else ""
    else:
        head = "Not a night for the scope"
        tail = ", ".join(reasons).capitalize() + "." if reasons else ""
    body = (lead + " " if lead else "") + tail
    return head, body.strip(), v


async def tonight_cloud_pct(lat: float, lon: float, tz_name: str,
                            now_local: datetime) -> float | None:
    """Mean hourly cloud cover from 20:00 tonight to 02:00, Open-Meteo.
    None on any failure: the verdict then leans on the station alone."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "hourly": "cloud_cover",
                "timezone": tz_name, "forecast_days": 2})
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        log.warning("cloud cover fetch failed (%s)", type(e).__name__)
        return None
    return mean_evening_cloud(body.get("hourly") or {}, now_local)


def mean_evening_cloud(hourly: dict[str, Any], now_local: datetime) -> float | None:
    times = hourly.get("time") or []
    covers = hourly.get("cloud_cover") or []
    day = now_local.date().isoformat()
    next_day = (now_local.replace(hour=0) + __import__("datetime").timedelta(days=1)).date().isoformat()
    picks: list[float] = []
    for t, c in zip(times, covers):
        if not isinstance(t, str) or not isinstance(c, (int, float)):
            continue
        d, _, hm = t.partition("T")
        h = int(hm[:2]) if len(hm) >= 2 and hm[:2].isdigit() else -1
        if (d == day and h >= 20) or (d == next_day and h <= 2):
            picks.append(float(c))
    return round(sum(picks) / len(picks), 1) if picks else None
