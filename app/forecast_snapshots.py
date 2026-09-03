"""Forecast snapshotter (1.8, Pillar C — the silent collector).

Verification is impossible retroactively: to ever answer "how accurate
is the forecast for MY backyard", the forecasts must be stored AS
ISSUED, with their lead time, and scored later against the station's own
readings. This module stores them; the scorecard UI comes later (1.9)
once months of snapshots exist — which is exactly why the collector
ships first.

Every ~6 h it fetches Open-Meteo's 7-day daily forecast for the primary
station's coordinates (the one-sky-per-server rule, same as the nowcast)
and writes one row per (issued run, valid day): predicted high/low,
precipitation probability, precipitation sum. Rows older than 400 days
prune on write. Cheap: one HTTP call and ≤7 rows per run, four runs a
day.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import settings

log = logging.getLogger("forecast-snap")

_INTERVAL_MS = 6 * 3_600_000
_KV_LAST = "forecast_snapshots.last_ms"
_KEEP_DAYS = 400


def _coords(devices: list[dict[str, Any]]) -> tuple[float, float] | None:
    for d in devices:
        if db.is_air_monitor_device(d):
            continue
        info = d.get("info") or {}
        coords = (info.get("coords") or {}).get("coords") or {}
        lat, lon = coords.get("lat"), coords.get("lon")
        if lat is not None and lon is not None:
            # Stored coords are operator data — one non-numeric record must
            # skip to the next station, not raise (the nws_watch lesson).
            try:
                return float(lat), float(lon)
            except (TypeError, ValueError):
                continue
    return None


async def _fetch_daily(lat: float, lon: float) -> dict[str, Any] | None:
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum",
        "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
        "forecast_days": 7, "timezone": settings.timezone,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast",
                                 params=params)
            if r.status_code != 200:
                return None
            return r.json().get("daily")
    except Exception:
        log.debug("open-meteo snapshot fetch failed", exc_info=True)
        return None


async def check(devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point; internally throttled to ~6 h."""
    raw = await db.get_kv(_KV_LAST)
    try:
        last = int(raw) if raw else 0
    except ValueError:
        last = 0
    if now_ms - last < _INTERVAL_MS:
        return
    target = _coords(devices)
    if target is None:
        return
    # Stamp BEFORE fetching (the nws_watch rule, R7): an Open-Meteo outage
    # otherwise costs an inline 15s HTTP attempt on EVERY alert tick until
    # it recovers. Worst case for a snapshot archive: one ~6h beat missed.
    await db.set_kv(_KV_LAST, str(now_ms))
    daily = await _fetch_daily(*target)
    if not daily or not daily.get("time"):
        return
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    today = _dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
    rows = []
    for i, day in enumerate(daily["time"]):
        try:
            valid = _dt.date.fromisoformat(day)
        except ValueError:
            continue
        def col(name):
            vals = daily.get(name) or []
            v = vals[i] if i < len(vals) else None
            return float(v) if isinstance(v, (int, float)) else None
        rows.append({
            "valid_date": day,
            "lead_days": (valid - today).days,
            "tmax_f": col("temperature_2m_max"),
            "tmin_f": col("temperature_2m_min"),
            "pop": col("precipitation_probability_max"),
            "precip_in": col("precipitation_sum"),
        })
    if rows:
        await db.insert_forecast_snapshots("open-meteo", now_ms, rows,
                                           keep_days=_KEEP_DAYS)
        log.debug("snapshotted %d forecast days", len(rows))


async def day_ahead_calls(provider: str, since: _dt.date, *,
                          lead_days: int = 1) -> dict[str, dict[str, Any]]:
    """The newest `lead_days` call per valid date, from `since` onward,
    keyed by valid date (local YYYY-MM-DD).

    The story engine's read of this archive: `forecast_snapshots` keeps every
    issue run, and scoring a forecast means scoring the LAST word the model
    had before the day began, so per valid date the greatest `issued_ms`
    wins. Rows without a high are skipped at the SQL — a provider that
    carries no temperatures (the Zambretti ledger) has nothing to score
    here, and a row that lost its high in transit is not a forecast of 0°F.

    One range read on (provider, valid_date); the per-date reduction runs
    in Python over at most a couple of months of rows.
    """
    from . import db as dbmod
    async with dbmod.connect() as conn:
        rows = await (await conn.execute(
            "SELECT valid_date, issued_ms, tmax_f, tmin_f, pop, precip_in "
            "FROM forecast_snapshots "
            "WHERE provider = ? AND lead_days = ? AND tmax_f IS NOT NULL "
            "AND valid_date >= ? ORDER BY valid_date, issued_ms",
            (provider, lead_days, since.isoformat()))).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        # Ordered by issued_ms within a date, so the last one seen is the
        # freshest issue.
        out[r["valid_date"]] = dict(r)
    return out
