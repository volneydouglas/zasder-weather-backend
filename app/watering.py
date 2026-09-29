"""The watering call (2.5, C6): water, go light, or skip.

A lawn or a bed needs roughly what the air took out of it minus what the
sky put back. The sky's half is the station's own gauge; the air's half is
reference evapotranspiration (ET0), estimated per day with FAO-56's
Hargreaves equation from the day's high and low and the station's
latitude. Hargreaves needs no solar sensor or anemometer, which most
backyard stations lack or site badly, and it reads the daily rollups
rather than a week of raw readings. It is an ESTIMATE and the verdict
says so; a Davis console's own ET is better where one exists.

The verdict looks at the last seven complete local days:

  skip   the rain covered the loss, or a real rain fell in the last two days
  light  a small deficit
  water  the air took more than half an inch the rain did not give back

A day the station did not measure rain on is not a dry day (absent is not
zero): it is left out of both sides, and the answer says how many days it
stands on. Fewer than four measured days is no verdict at all.

Every morning at 05:00 local the call goes to every enabled webhook as a
`watering` event, the hook an irrigation controller or a mower schedule
can act on. It never pushes to a phone.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

DAYS = 7
MIN_DAYS = 4
WATER_DEFICIT_IN = 0.5
LIGHT_DEFICIT_IN = 0.15
RECENT_RAIN_IN = 0.25
SEND_HOUR = 5


def extraterrestrial_mm(lat: float, day_of_year: int) -> float:
    """FAO-56 eq. 21: extraterrestrial radiation Ra as equivalent
    evaporation, mm/day (MJ m-2 day-1 x 0.408)."""
    phi = math.radians(lat)
    dr = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    delta = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta))))
    ra_mj = (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(phi) * math.sin(delta)
        + math.cos(phi) * math.cos(delta) * math.sin(ws))
    return max(0.0, ra_mj * 0.408)


def hargreaves_et0_in(tmin_f: float, tmax_f: float, lat: float,
                      day_of_year: int) -> float | None:
    """FAO-56 eq. 52, inches/day. Temperatures in °F (API-native),
    converted here and only here."""
    if not all(isinstance(v, (int, float)) and math.isfinite(v)
               for v in (tmin_f, tmax_f, lat)) or tmax_f < tmin_f:
        return None
    tmin, tmax = (tmin_f - 32) * 5 / 9, (tmax_f - 32) * 5 / 9
    mm = 0.0023 * extraterrestrial_mm(lat, day_of_year) \
        * ((tmax + tmin) / 2 + 17.8) * math.sqrt(tmax - tmin)
    return max(0.0, mm / 25.4)


def verdict(days: list[dict[str, Any]]) -> dict[str, Any]:
    """`days` oldest first: {"day", "rain_in" (None = not measured),
    "et_in" (None = no temperatures)}. Pure."""
    usable = [d for d in days if d.get("rain_in") is not None
              and d.get("et_in") is not None]
    out: dict[str, Any] = {"days": days, "days_used": len(usable),
                           "method": "hargreaves"}
    if len(usable) < MIN_DAYS:
        out.update(verdict=None, rain_in=None, et_in=None, balance_in=None)
        return out
    rain = sum(d["rain_in"] for d in usable)
    et = sum(d["et_in"] for d in usable)
    balance = rain - et
    recent = sum(d["rain_in"] for d in days[-2:] if d.get("rain_in") is not None)
    if balance >= 0 or recent >= RECENT_RAIN_IN:
        v = "skip"
    elif -balance >= WATER_DEFICIT_IN:
        v = "water"
    elif -balance >= LIGHT_DEFICIT_IN:
        v = "light"
    else:
        v = "skip"
    out.update(verdict=v, rain_in=round(rain, 2), et_in=round(et, 2),
               balance_in=round(balance, 2), recent_rain_in=round(recent, 2))
    return out


async def for_station(mac: str, today: date, lat: float | None) -> dict[str, Any]:
    from .climate import _rollup_rows
    from .day_rain import day_rain_in
    from .forecast_skill import covered
    first = today - timedelta(days=DAYS)
    last = today - timedelta(days=1)
    rows = {r["day"]: r for r in await _rollup_rows(mac, first.isoformat(),
                                                   last.isoformat())}
    days = []
    for k in range(DAYS):
        d = first + timedelta(days=k)
        r = rows.get(d.isoformat())
        # A day the station did not cover is not a measured day (R25-A03,
        # the 2.5 additional review): one reading makes tmin == tmax, so ET
        # read 0 and a morning of data became a confident "skip" that also
        # went to the webhooks. Rows without a span (folded before 2.3)
        # cannot be judged and are taken as they are.
        if r is not None and covered(dict(r)) is False:
            r = None
        et = None
        if r is not None and lat is not None:
            et = hargreaves_et0_in(r["tempf_min"], r["tempf_max"], lat,
                                   d.timetuple().tm_yday)
        days.append({"day": d.isoformat(),
                     "rain_in": day_rain_in(r) if r is not None else None,
                     "et_in": None if et is None else round(et, 3)})
    out = verdict(days)
    if lat is None:
        out["note"] = "ET needs the station's location."
    return out


async def send_if_due(devices: list[dict[str, Any]], now_ms: int) -> bool:
    """05:00 local, once a day, to every enabled webhook. Returns True when
    it sent (the tests read it)."""
    from zoneinfo import ZoneInfo
    from . import db, webhooks
    from .config import settings
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        return False
    local = datetime.fromtimestamp(now_ms / 1000, tz)
    if local.hour < SEND_HOUR:
        return False
    day = local.date().isoformat()
    if await db.get_kv("watering.sent_day") == day:
        return False
    if not await db.list_webhooks(enabled_only=True):
        return False
    station = next((d for d in devices if not db.is_air_monitor_device(d)), None)
    if station is None:
        return False
    c = (((station.get("info") or {}).get("coords") or {}).get("coords") or {})
    lat = c.get("lat") if isinstance(c.get("lat"), (int, float)) else None
    call = await for_station(station["mac"], local.date(), lat)
    if call.get("verdict") is None:
        # Not marked: a later tick the same day tries again, so a location
        # set or a gauge back after 05:00 still gets its call out (Greptile,
        # PR #48). The check is a seven-row rollup read.
        return False
    await db.set_kv("watering.sent_day", day)
    await webhooks.dispatch_event("watering", {
        "mac": station["mac"], "day": day, "verdict": call["verdict"],
        "rain_in": call["rain_in"], "et_in": call["et_in"],
        "balance_in": call["balance_in"], "days_used": call["days_used"],
        "method": call["method"], "ts_ms": now_ms})
    return True
