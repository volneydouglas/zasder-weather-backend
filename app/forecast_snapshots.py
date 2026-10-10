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
import math
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import settings

log = logging.getLogger("forecast-snap")

_INTERVAL_MS = 6 * 3_600_000
_KV_LAST = "forecast_snapshots.last_ms"
_KEEP_DAYS = 400
# The providers this module archives. The forecast correction can only be
# learned for these (R25-A07, the 2.5 additional review): until 2.6 TWC
# was fetched live but never archived, so a TWC user's correction was
# silently always empty. TWC is archived now, on the same beat, whenever
# the server holds a Weather Underground key (Doren keeps TWC for the
# written forecast; `twc_key` says whether his archive can fill).
ARCHIVED_PROVIDERS = frozenset({"open-meteo", "twc"})


async def twc_key() -> str | None:
    """The key /api/forecast uses for TWC: set from the app (server_kv),
    else the environment. None means TWC cannot be fetched OR archived."""
    return await db.get_kv("wu_api_key") or settings.wu_api_key


def coords_device(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The station whose sky this server forecasts — the first non-air
    station with usable coordinates, the one-sky-per-server rule.

    Public because the scorecard (`forecast_skill`) has to name it: the
    archive holds THIS station's sky whichever station the reader is
    scoring, and two modules picking it by two rules would eventually
    disagree about whose forecast was being graded.
    """
    for d in devices:
        if db.is_air_monitor_device(d):
            continue
        info = d.get("info") or {}
        coords = (info.get("coords") or {}).get("coords") or {}
        lat, lon = coords.get("lat"), coords.get("lon")
        if lat is None or lon is None or isinstance(lat, bool) or isinstance(lon, bool):
            continue
        # Stored coords are operator data — one non-numeric record must
        # skip to the next station, not raise (the nws_watch lesson), and
        # a stored "nan" or 999 (pre-R23 ingest let them through) is no
        # place to forecast for either.
        from .ingest import valid_coords
        try:
            if not valid_coords(float(lat), float(lon)):
                continue
        except (TypeError, ValueError):
            continue
        return d
    return None


def coords(devices: list[dict[str, Any]]) -> tuple[float, float] | None:
    """The sky's coordinates (see coords_device), or None."""
    d = coords_device(devices)
    if d is None:
        return None
    coords = ((d.get("info") or {}).get("coords") or {}).get("coords") or {}
    return float(coords["lat"]), float(coords["lon"])


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


async def _fetch_twc(lat: float, lon: float) -> dict[str, Any] | None:
    """TWC's 5-day daily in the Open-Meteo shape, or None without a key or
    on any upstream failure (the forecast route owns the fallback story;
    the archive simply skips the beat)."""
    key = await twc_key()
    if not key:
        return None
    try:
        from . import forecast_twc
        return (await forecast_twc.fetch(lat, lon, key)).get("daily")
    except Exception as e:
        # The type only: the request URL carries the key as a query
        # parameter, and a traceback would print it (Greptile, PR #57; the
        # forecast route keeps the same rule).
        log.debug("twc snapshot fetch failed: %s", type(e).__name__)
        return None


# The live gate, by a name the test fixture does not stub (conftest points
# `_fetch_twc` at a no-op so no test can reach TWC by accident; the gate's
# own test puts this back).
_fetch_twc_live = _fetch_twc


def _rows(daily: dict[str, Any], today: _dt.date) -> list[dict[str, Any]]:
    """One snapshot row per forecast day, lead counted from `today`."""
    rows = []
    for i, day in enumerate(daily["time"]):
        try:
            valid = _dt.date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        def col(name):
            vals = daily.get(name) or []
            v = vals[i] if i < len(vals) else None  # noqa: B023 (called in this iteration)
            return float(v) if isinstance(v, (int, float)) else None
        rows.append({
            "valid_date": day,
            "lead_days": (valid - today).days,
            "tmax_f": col("temperature_2m_max"),
            "tmin_f": col("temperature_2m_min"),
            "pop": col("precipitation_probability_max"),
            "precip_in": col("precipitation_sum"),
        })
    return rows


async def check(devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point; internally throttled to ~6 h. Every
    provider in ARCHIVED_PROVIDERS is fetched on the same beat so their
    scorecards grade the same days."""
    raw = await db.get_kv(_KV_LAST)
    try:
        last = int(raw) if raw else 0
    except ValueError:
        last = 0
    if now_ms - last < _INTERVAL_MS:
        return
    target = coords(devices)
    if target is None:
        return
    # Stamp BEFORE fetching (the nws_watch rule, R7): an Open-Meteo outage
    # otherwise costs an inline 15s HTTP attempt on EVERY alert tick until
    # it recovers. Worst case for a snapshot archive: one ~6h beat missed.
    await db.set_kv(_KV_LAST, str(now_ms))
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    today = _dt.datetime.fromtimestamp(now_ms / 1000, tz).date()
    # Looked up at call time, not bound at import: the test fixture swaps
    # the fetchers on the module.
    fetchers = (("open-meteo", _fetch_daily), ("twc", _fetch_twc))
    for provider, fetch in fetchers:
        daily = await fetch(*target)
        if not daily or not daily.get("time"):
            continue
        rows = _rows(daily, today)
        if rows:
            await db.insert_forecast_snapshots(provider, now_ms, rows,  # in ARCHIVED_PROVIDERS
                                               keep_days=_KEEP_DAYS,
                                               lat=target[0], lon=target[1])
            log.debug("snapshotted %d %s forecast days", len(rows), provider)


async def day_ahead_calls(provider: str, since: _dt.date, *,
                          lead_days: int = 1,
                          near: tuple[float, float] | None = None
                          ) -> dict[str, dict[str, Any]]:
    """The newest `lead_days` call per valid date, from `since` onward,
    keyed by valid date (local YYYY-MM-DD).

    `near` (2.6, plan item 5 / R25-A06) is the sky being scored: a run
    archived for a sky farther than neighbors.MAX_DISTANCE_KM from it is
    another place's forecast and is left out, so a server whose forecast
    station moved, or whose first station changed, does not grade the new
    sky against the old one's calls. Rows filed before 2.6 carry no sky
    and are taken as this one's: they were the server's sky when filed.

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
            "SELECT valid_date, issued_ms, tmax_f, tmin_f, pop, precip_in, lat, lon "
            "FROM forecast_snapshots "
            "WHERE provider = ? AND lead_days = ? AND tmax_f IS NOT NULL "
            "AND valid_date >= ? ORDER BY valid_date, issued_ms",
            (provider, lead_days, since.isoformat()))).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if near is not None and not _same_sky(r["lat"], r["lon"], near):
            continue
        # Ordered by issued_ms within a date, so the last one seen is the
        # freshest issue.
        out[r["valid_date"]] = dict(r)
    return out


def _same_sky(lat: Any, lon: Any, near: tuple[float, float]) -> bool:
    """A run filed for `lat`/`lon` grades the sky at `near` when it is the
    same place (the neighbour radius); a run with no sky on file is taken
    as this one's."""
    if lat is None or lon is None:
        return True
    try:
        from . import neighbors
        return neighbors.distance_km(float(lat), float(lon), near[0], near[1]) <= neighbors.MAX_DISTANCE_KM
    except (TypeError, ValueError):
        return True


async def latest_low_f(provider: str, valid_date: str) -> float | None:
    """The newest issued low for one local date (YYYY-MM-DD), or None when
    the archive holds no call for it. The freeze-night card's read of the
    archive: one indexed lookup on (provider, valid_date), newest issue
    first, and rows without a low are skipped at the SQL — a call that
    lost its low in transit is not a forecast of 0°F."""
    from . import db as dbmod
    async with dbmod.connect() as conn:
        row = await (await conn.execute(
            "SELECT tmin_f FROM forecast_snapshots "
            "WHERE provider = ? AND valid_date = ? AND tmin_f IS NOT NULL "
            "ORDER BY issued_ms DESC LIMIT 1",
            (provider, valid_date))).fetchone()
    if row is None:
        return None
    try:
        v = float(row[0])
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None
