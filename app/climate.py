"""Climate presentation set (1.9, WeeWX parity): monthly/yearly stats,
degree days, water-year rain, and the classic NOAA-style text report —
all served FROM THE DAILY ROLLUPS, never raw scans, so a decade answers
in milliseconds and history thinning changes nothing (the rollups are
the preserved source of truth for aged days).

Conventions, chosen for comparability with everyone else's numbers:
- Daily mean for degree days is (max + min) / 2 — the NOAA/NWS method,
  not the sample average. HDD/CDD base 65 °F; GDD base 50 cap 86 (the
  corn convention). The backend also computes GDD even though only
  HDD/CDD get UI this cycle (the agriculture pack rides later).
- Monthly mean temperature is the average of the daily means, weighted
  equally per day (a day with 200 samples counts once).
- Water year: rain accumulated since the configured start month's 1st
  (default October — the western-US hydrology convention).
- Missing days are missing — a month with 3 reporting days says so
  (`days` in every row) instead of passing 3 days off as a month.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from .config import settings

HDD_CDD_BASE_F = 65.0
GDD_BASE_F, GDD_CAP_F = 50.0, 86.0


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def degree_days(tmin: float | None, tmax: float | None) -> tuple[float, float, float] | None:
    """(hdd, cdd, gdd) for one day, or None without both extremes."""
    if tmin is None or tmax is None:
        return None
    mean = (tmin + tmax) / 2
    hdd = max(0.0, HDD_CDD_BASE_F - mean)
    cdd = max(0.0, mean - HDD_CDD_BASE_F)
    gdd = max(0.0, (min(tmax, GDD_CAP_F) + min(max(tmin, GDD_BASE_F), GDD_CAP_F)) / 2
              - GDD_BASE_F)
    return hdd, cdd, gdd


def local_today() -> date:
    """Today in the configured station timezone — the clock every
    daily_rollups.day was assigned by. Never the host's."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date()


def water_year_start(today: date, start_month: int) -> date:
    """The most recent 1st of `start_month` on or before `today`."""
    y = today.year if today.month >= start_month else today.year - 1
    return date(y, start_month, 1)


async def _rollup_rows(mac: str, first_day: str, last_day: str) -> list:
    from . import db as dbmod
    async with dbmod.connect() as db:
        return await (await db.execute(
            "SELECT * FROM daily_rollups WHERE mac = ? "
            "AND day >= ? AND day <= ? ORDER BY day",
            (mac, first_day, last_day))).fetchall()


def _day_stats(r) -> dict[str, Any]:
    """One rollup row → the per-day numbers every consumer here shares."""
    tmin, tmax = _f(r["tempf_min"]), _f(r["tempf_max"])
    dd = degree_days(tmin, tmax)
    return {
        "day": r["day"], "tmin": tmin, "tmax": tmax,
        "mean": (tmin + tmax) / 2 if dd else None,
        "rain": _f(r["rain_total"]) or 0.0,
        "gust": _f(r["windgustmph_max"]),
        "hdd": dd[0] if dd else None,
        "cdd": dd[1] if dd else None,
        "gdd": dd[2] if dd else None,
    }


async def year_summary(mac: str, year: int) -> dict[str, Any]:
    """Twelve month rows + annual totals + the running water year."""
    rows = await _rollup_rows(mac, f"{year}-01-01", f"{year}-12-31")
    months: list[dict[str, Any]] = []
    by_month: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        d = _day_stats(r)
        by_month.setdefault(int(d["day"][5:7]), []).append(d)
    for m in range(1, 13):
        days = by_month.get(m, [])
        temps = [d for d in days if d["mean"] is not None]
        lo = min((d["tmin"] for d in temps), default=None)
        hi = max((d["tmax"] for d in temps), default=None)
        months.append({
            "month": m,
            "days": len(days),
            "mean": round(sum(d["mean"] for d in temps) / len(temps), 1)
                    if temps else None,
            "min": lo, "max": hi,
            "min_day": next((d["day"] for d in temps if d["tmin"] == lo), None),
            "max_day": next((d["day"] for d in temps if d["tmax"] == hi), None),
            "rain": round(sum(d["rain"] for d in days), 2) if days else None,
            "hdd": round(sum(d["hdd"] for d in temps), 1) if temps else None,
            "cdd": round(sum(d["cdd"] for d in temps), 1) if temps else None,
            "gdd": round(sum(d["gdd"] for d in temps), 1) if temps else None,
        })
    # Annual totals from the UNROUNDED per-day set (CodeRabbit, PR #33):
    # averaging monthly means gave a 3-day month the weight of a full one,
    # and summing rounded monthly values compounds rounding.
    all_days = [d for days in by_month.values() for d in days]
    all_temps = [d for d in all_days if d["mean"] is not None]
    out: dict[str, Any] = {
        "mac": mac, "year": year, "months": months,
        # Absent is not zero (R11): a year with no rollup rows must report
        # None, not "0.00 in of rain" — indistinguishable from a real
        # drought. Same rule the per-month rows already follow.
        "totals": {
            "rain": round(sum(d["rain"] for d in all_days), 2)
                    if all_days else None,
            "hdd": round(sum(d["hdd"] for d in all_temps), 1)
                   if all_temps else None,
            "cdd": round(sum(d["cdd"] for d in all_temps), 1)
                   if all_temps else None,
            "gdd": round(sum(d["gdd"] for d in all_temps), 1)
                   if all_temps else None,
            "mean": round(sum(d["mean"] for d in all_temps) / len(all_temps), 1)
                    if all_temps else None,
        },
    }
    # Water year — computed against TODAY (it's a running total), not the
    # requested calendar year; the app shows it beside whatever year the
    # user is browsing. "Today" in the STATION's timezone — the same clock
    # that assigns daily_rollups.day — or a host on UTC flips the water
    # year at the wrong local moment near the boundary.
    today = local_today()
    start = water_year_start(today, settings.water_year_start_month)
    wy_rows = await _rollup_rows(mac, start.isoformat(), today.isoformat())
    out["water_year"] = {
        "start": start.isoformat(),
        "rain": round(sum(_f(r["rain_total"]) or 0 for r in wy_rows), 2),
    }
    return out


async def month_days(mac: str, year: int, month: int) -> list[dict[str, Any]]:
    last = (date(year + (month == 12), (month % 12) + 1, 1)
            - timedelta(days=1))
    rows = await _rollup_rows(mac, f"{year}-{month:02d}-01", last.isoformat())
    return [_day_stats(r) for r in rows]


# ── the NOAA-style text report ──────────────────────────────────────────

def _fmt(v: float | None, width: int, digits: int = 1) -> str:
    return " " * width if v is None else f"{v:{width}.{digits}f}"


async def noaa_month_report(mac: str, name: str, year: int,
                            month: int) -> str:
    """The classic fixed-width monthly climate summary every WeeWX skin
    ships — day rows of mean/high/low, degree days, rain."""
    days = await month_days(mac, year, month)
    lines = [
        f"   MONTHLY CLIMATOLOGICAL SUMMARY for {date(year, month, 1):%B %Y}",
        "",
        f"   Station: {name}",
        "",
        "        Temperature (F)          Deg Days     Rain      Wind",
        "   Day  Mean   High    Low    Heat   Cool     (in)    Gust(mph)",
        "   " + "-" * 60,
    ]
    for d in days:
        lines.append(
            f"   {int(d['day'][8:10]):3d}"
            f"{_fmt(d['mean'], 7)}{_fmt(d['tmax'], 7)}{_fmt(d['tmin'], 7)}"
            f"{_fmt(d['hdd'], 7)}{_fmt(d['cdd'], 7)}"
            f"{_fmt(d['rain'], 9, 2)}{_fmt(d['gust'], 9)}")
    temps = [d for d in days if d["mean"] is not None]
    lines.append("   " + "-" * 60)
    if temps:
        mean = sum(d["mean"] for d in temps) / len(temps)
        lines.append(
            f"       {_fmt(mean, 6)}"
            f"{_fmt(max(d['tmax'] for d in temps), 7)}"
            f"{_fmt(min(d['tmin'] for d in temps), 7)}"
            f"{_fmt(sum(d['hdd'] for d in temps), 7)}"
            f"{_fmt(sum(d['cdd'] for d in temps), 7)}"
            f"{_fmt(sum(d['rain'] for d in days), 9, 2)}"
            f"{_fmt(max((d['gust'] for d in days if d['gust'] is not None), default=None), 9)}")
    else:
        lines.append("   (no data for this month)")
    lines.append("")
    lines.append(f"   Days with data: {len(days)}")
    return "\n".join(lines) + "\n"


async def noaa_year_report(mac: str, name: str, year: int) -> str:
    """Month rows for one year — the NOAA yearly summary."""
    summary = await year_summary(mac, year)
    lines = [
        f"   YEARLY CLIMATOLOGICAL SUMMARY for {year}",
        "",
        f"   Station: {name}",
        "",
        "         Temperature (F)            Deg Days       Rain",
        "   Month  Mean   High    Low     Heat    Cool      (in)   Days",
        "   " + "-" * 60,
    ]
    for m in summary["months"]:
        lines.append(
            f"   {date(year, m['month'], 1):%b}  "
            f"{_fmt(m['mean'], 7)}{_fmt(m['max'], 7)}{_fmt(m['min'], 7)}"
            f"{_fmt(m['hdd'], 8)}{_fmt(m['cdd'], 8)}"
            f"{_fmt(m['rain'], 10, 2)}{m['days']:7d}")
    t = summary["totals"]
    lines.append("   " + "-" * 60)
    lines.append(
        f"        {_fmt(t['mean'], 7)}              "
        f"{_fmt(t['hdd'], 8)}{_fmt(t['cdd'], 8)}{_fmt(t['rain'], 10, 2)}")
    wy = summary["water_year"]
    lines.append("")
    lines.append(f"   Water year (from {wy['start']}): "
                 f"{wy['rain']:.2f} in")
    return "\n".join(lines) + "\n"
