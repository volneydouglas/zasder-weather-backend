"""The outlook report (2.2, Doren: "a forecast at a chosen time"): an
evening report carrying tomorrow's forecast, or a morning one carrying
today's, from the source the owner picked (Open-Meteo, or The Weather
Company via the WU key), delivered like the morning report and kept in
Reports. Plain words, one screen.

The fetch and the build are separate so the words can be pinned without
a network: `fetch_daily` talks to the provider, `build` turns one day of
the provider's daily arrays into the report.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("zasder.outlook")

SOURCE_OPEN_METEO = "open-meteo"
SOURCE_TWC = "twc"
SOURCES = (SOURCE_OPEN_METEO, SOURCE_TWC)

# WMO weather codes (Open-Meteo; forecast_twc maps TWC's icons onto the
# same scale) in the words a report uses.
SKY_WORDS: dict[int, str] = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


@dataclass(frozen=True)
class OutlookReport:
    date_label: str          # "Wednesday, September 10"
    for_date: str            # ISO day the forecast describes
    when: str                # "tomorrow" | "today"
    hi_f: float | None
    lo_f: float | None
    precip_pct: int | None
    wind_max_mph: float | None
    sky: str | None
    narrative: str | None    # the provider's own prose (TWC), when it has one
    sunrise: str | None      # "06:12" local, when the provider sends it
    sunset: str | None
    source: str
    fallback_from: str | None = None


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _at(daily: dict[str, Any], key: str, i: int) -> Any:
    arr = daily.get(key)
    if not isinstance(arr, list) or i >= len(arr):
        return None
    return arr[i]


def _clock(v: Any) -> str | None:
    """'2026-09-10T06:12' → '06:12'; anything else → None."""
    if not isinstance(v, str) or "T" not in v:
        return None
    hm = v.split("T", 1)[1][:5]
    return hm if len(hm) == 5 and hm[2] == ":" else None


def _narrative_for(narrative: list[Any] | None, when: str) -> str | None:
    """TWC's prose comes per DAYPART ("Today", "Tonight", "Tomorrow",
    "Tomorrow night", then weekday names), twice as many entries as days
    and the first one expired by evening, so the day's words are found by
    name, never by index. A plain list of strings (a test, another
    provider) falls back to day order."""
    if not narrative:
        return None
    wanted = ("tomorrow",) if when == "tomorrow" else ("today", "tonight")
    for entry in narrative:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip().lower()
            t = entry.get("text") or entry.get("narrative")
            if name in wanted and t:
                return str(t)
    strings = [e for e in narrative if isinstance(e, str)]
    if strings:
        i = 0 if when == "today" else 1
        return strings[i] if i < len(strings) else None
    return None


def slot_for(local_hour: int) -> str:
    """Before noon the report is about today; from noon on, tomorrow."""
    return "today" if local_hour < 12 else "tomorrow"


def build(daily: dict[str, Any], *, narrative: list[Any] | None, when: str,
          now_local: datetime, source: str,
          fallback_from: str | None = None) -> OutlookReport:
    i = 0 if when == "today" else 1
    target = (now_local + timedelta(days=i)).date()
    provider_day = _at(daily, "time", i)
    if isinstance(provider_day, str) and len(provider_day) >= 10:
        # Trust the provider's own date for the row; label from it too.
        try:
            target = datetime.strptime(provider_day[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    code = _at(daily, "weather_code", i)
    sky = SKY_WORDS.get(int(code)) if isinstance(code, (int, float)) else None
    pp = _num(_at(daily, "precipitation_probability_max", i))
    # Whitespace-only prose is no prose (CodeRabbit, PR #39): the push
    # and the HTML card key off truthiness.
    text = _narrative_for(narrative, when)
    text = " ".join(text.split()) or None if text else None
    return OutlookReport(
        date_label=target.strftime("%A, %B %-d"),
        for_date=target.isoformat(),
        when=when,
        hi_f=_num(_at(daily, "temperature_2m_max", i)),
        lo_f=_num(_at(daily, "temperature_2m_min", i)),
        precip_pct=int(round(pp)) if pp is not None else None,
        wind_max_mph=_num(_at(daily, "wind_speed_10m_max", i)),
        sky=sky,
        narrative=text,
        sunrise=_clock(_at(daily, "sunrise", i)),
        sunset=_clock(_at(daily, "sunset", i)),
        source=source,
        fallback_from=fallback_from,
    )


def title(r: OutlookReport) -> str:
    return ("Tomorrow's outlook · " if r.when == "tomorrow" else "Today's outlook · ") \
        + datetime.strptime(r.for_date, "%Y-%m-%d").strftime("%a %b %-d")


def text(r: OutlookReport) -> str:
    """The plain-text body: the email and the push share its sentences."""
    lines = [f"{'Tomorrow' if r.when == 'tomorrow' else 'Today'}, {r.date_label}"]
    bits: list[str] = []
    if r.sky:
        bits.append(r.sky.capitalize())
    if r.hi_f is not None:
        bits.append(f"high near {r.hi_f:.0f}F")
    if r.lo_f is not None:
        bits.append(f"low around {r.lo_f:.0f}F")
    if r.precip_pct is not None:
        bits.append(f"{r.precip_pct}% chance of precipitation")
    if r.wind_max_mph is not None:
        bits.append(f"wind up to {r.wind_max_mph:.0f} mph")
    if bits:
        lines.append(", ".join(bits) + ".")
    if r.sunrise or r.sunset:
        sun = []
        if r.sunrise:
            sun.append(f"sunrise {r.sunrise}")
        if r.sunset:
            sun.append(f"sunset {r.sunset}")
        lines.append(", ".join(sun).capitalize() + ".")
    if r.narrative:
        lines.append("")
        lines.append(r.narrative)
    lines.append("")
    src = "The Weather Company" if r.source == SOURCE_TWC else "Open-Meteo"
    if r.fallback_from:
        src += " (the Weather Company forecast was unavailable)"
    lines.append(f"Forecast by {src}.")
    return "\n".join(lines)


def push_text(r: OutlookReport) -> tuple[str, str]:
    bits: list[str] = []
    if r.sky:
        bits.append(r.sky.capitalize())
    if r.hi_f is not None and r.lo_f is not None:
        bits.append(f"{r.hi_f:.0f}/{r.lo_f:.0f}")
    elif r.hi_f is not None:
        bits.append(f"high {r.hi_f:.0f}")
    if r.precip_pct is not None:
        bits.append(f"{r.precip_pct}% precipitation")
    body = ", ".join(bits) if bits else "Open for the forecast."
    # 2.3 (Doren, 09-11): the push carries the forecast the email carries.
    # APNs caps the payload at 4 KB; a TWC narrative is a sentence or two,
    # and a long one is cut at a sentence end well inside that.
    if r.narrative:
        body = (body + ". " if bits else "") + _clip_narrative(r.narrative)
    return title(r), body


PUSH_NARRATIVE_MAX = 600


def _clip_narrative(text: str, limit: int = PUSH_NARRATIVE_MAX) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    return text[:cut + 1] if cut > limit // 2 else text[:limit].rstrip() + "…"


def build_html(r: OutlookReport, theme: str = "dark") -> str:
    """The outlook email in the morning report's dress (2.3): the day and
    its sky as the headline, the four numbers as tiles, sun times, the
    provider's own prose, and the credit. The plain text stays the
    alternative for clients that refuse HTML."""
    from . import email_card as ec
    import html as _h
    when = "Tomorrow" if r.when == "tomorrow" else "Today"
    inner = ec.section_label(when.upper(), padding="14px 0 6px")
    inner += (f'<div style="font:800 18px {ec.FONT};color:{ec.TEXT};">'
              f'{_h.escape(r.date_label)}</div>')
    if r.sky:
        inner += (f'<div style="font:400 14px {ec.FONT};color:{ec.DIM};'
                  f'padding-top:2px;">{_h.escape(r.sky.capitalize())}</div>')
    tiles: list[str] = []
    if r.hi_f is not None:
        tiles.append(ec.tile("HIGH", f"{r.hi_f:.0f}&deg;F", ec.WARM))
    if r.lo_f is not None:
        tiles.append(ec.tile("LOW", f"{r.lo_f:.0f}&deg;F", ec.ACCENT))
    if r.precip_pct is not None:
        tiles.append(ec.tile("PRECIP", f"{r.precip_pct}%",
                             ec.ACCENT if r.precip_pct >= 40 else ec.TEXT))
    if r.wind_max_mph is not None:
        tiles.append(ec.tile("WIND", f"{r.wind_max_mph:.0f} mph",
                             ec.WARM if r.wind_max_mph >= 30 else ec.TEXT))
    inner += ec.tile_row(tiles, margin_top=10)
    sun: list[str] = []
    if r.sunrise:
        sun.append(ec.tile("SUNRISE", _h.escape(r.sunrise), width="50%"))
    if r.sunset:
        sun.append(ec.tile("SUNSET", _h.escape(r.sunset), width="50%"))
    inner += ec.tile_row(sun, margin_top=6)
    if r.narrative:
        inner += ec.section_label("THE FORECAST")
        inner += ec.prose(r.narrative)
    src = "The Weather Company" if r.source == SOURCE_TWC else "Open-Meteo"
    if r.fallback_from:
        src += " (the Weather Company forecast was unavailable)"
    inner += (f'<div style="font:400 11px {ec.FONT};color:{ec.DIM};'
              f'padding-top:10px;">Forecast by {_h.escape(src)}.</div>')
    return ec.shell(title(r), f"{when}, {r.date_label}", inner,
                    "The outlook at the hour you chose. "
                    "Every report lives in the app's Reports pane.",
                    theme=theme)


async def fetch_daily(lat: float, lon: float, *, source: str, wu_key: str | None,
                      tz_name: str) -> tuple[dict[str, Any], list[Any], str, str | None]:
    """(daily arrays, narrative list, source used, fallback_from). A TWC
    request without a key or that fails falls back to Open-Meteo for
    this report only, marked, the way /api/forecast does."""
    import httpx
    fallback_from: str | None = None
    if source == SOURCE_TWC:
        if wu_key:
            try:
                from . import forecast_twc
                body = await forecast_twc.fetch(lat, lon, wu_key)
                return (body.get("daily") or {}, body.get("narrative") or [],
                        SOURCE_TWC, None)
            except Exception as e:
                log.warning("TWC outlook failed (%s); falling back to Open-Meteo",
                            type(e).__name__)
        fallback_from = SOURCE_TWC
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_speed_10m_max,sunrise,sunset",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "timezone": tz_name, "forecast_days": 2,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
        body = r.json()
    return body.get("daily") or {}, [], SOURCE_OPEN_METEO, fallback_from
