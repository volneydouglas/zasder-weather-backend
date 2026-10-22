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

2.6 added TWC on the same beat (when the server holds a Weather
Underground key). 2.7 adds three of Open-Meteo's individual models, so
the scorecard can say which forecast has done best for this yard rather
than only how the one in use has done. Every source is a `provider` value
in the same table, graded by the same rules (`forecast_skill`):

  source              upstream                       leads  fields
  open-meteo          Open-Meteo best match           0-6    high, low, pop, sum
  twc                 TWC v3 daily/5day (WU key)      0-4    high, low, pop, sum
  open-meteo-gfs      Open-Meteo gfs_seamless         0-6    high, low, pop, sum
  open-meteo-ecmwf    Open-Meteo ecmwf_ifs025         0-6    high, low, pop, sum
  open-meteo-icon     Open-Meteo icon_seamless        0-6    high, low, pop, sum

The three models come from ONE request (`models=` with a comma list; the
response suffixes each daily key with the model id). A model's
probability is ensemble-derived and Open-Meteo may file it as null for a
model or a place; a null is archived as NULL and that day's rain call is
simply not graded, the rule every source already follows. A model that
filed no temperature at all for the run is not archived for that beat.

Request volume per server: one Open-Meteo call (best match), one
Open-Meteo call (the three models) and, with a key, one TWC call per
six hours. Twelve a day at most. A TWC key the upstream refuses (401 or
403: WU deactivates keys when a station stops uploading) is not asked
again for TWC_REFUSED_BACKOFF_MS or until the key changes.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import time
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
# 2.7: three of Open-Meteo's individual models beside its best match,
# source id -> Open-Meteo `models=` id. The ids are wire values (the
# scorecard's `sources` block, ?provider=) and fit the routes' provider
# pattern, ^[a-z0-9-]{1,32}$.
MODEL_SOURCES: dict[str, str] = {
    "open-meteo-gfs": "gfs_seamless",
    "open-meteo-ecmwf": "ecmwf_ifs025",
    "open-meteo-icon": "icon_seamless",
}

# Every archived source, in the order the scorecard lists them (and the
# order a tie for best is broken in). `selectable` is whether the apps'
# forecast picker offers it today; `max_lead_days` is the last lead the
# upstream files (TWC's free product is five days, leads 0-4).
SOURCES: tuple[dict[str, Any], ...] = (
    {"id": "open-meteo", "name": "Open-Meteo", "provider": "Open-Meteo",
     "model": None, "max_lead_days": 6, "selectable": True},
    {"id": "twc", "name": "The Weather Company", "provider": "The Weather Company",
     "model": None, "max_lead_days": 4, "selectable": True},
    {"id": "open-meteo-gfs", "name": "GFS", "provider": "Open-Meteo",
     "model": "gfs_seamless", "max_lead_days": 6, "selectable": False},
    {"id": "open-meteo-ecmwf", "name": "ECMWF IFS", "provider": "Open-Meteo",
     "model": "ecmwf_ifs025", "max_lead_days": 6, "selectable": False},
    {"id": "open-meteo-icon", "name": "ICON", "provider": "Open-Meteo",
     "model": "icon_seamless", "max_lead_days": 6, "selectable": False},
)
ARCHIVED_PROVIDERS = frozenset(s["id"] for s in SOURCES)

# The daily fields every source is archived from, Open-Meteo's names.
_DAILY_FIELDS = ("temperature_2m_max", "temperature_2m_min",
                 "precipitation_probability_max", "precipitation_sum")

# A refused TWC key (401/403) is not asked again for a day, or until the
# key changes. Four refused calls a day is not a load problem; it is a
# warning in the log every six hours about a key the operator already
# knows is dead, and an inline 15 s timeout risk on the alert tick.
_KV_TWC_REFUSED = "forecast_snapshots.twc_refused"
TWC_REFUSED_BACKOFF_MS = 24 * 3_600_000

# Source status, on the wire in the scorecard's `sources` block.
STATUS_ARCHIVING = "archiving"
STATUS_NO_KEY = "no_key"
STATUS_KEY_REFUSED = "key_refused"


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


def _om_params(lat: float, lon: float) -> dict[str, Any]:
    return {
        "latitude": lat, "longitude": lon,
        "daily": ",".join(_DAILY_FIELDS),
        "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
        "forecast_days": 7, "timezone": settings.timezone,
    }


async def _fetch_daily(lat: float, lon: float) -> dict[str, Any] | None:
    params = _om_params(lat, lon)
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


async def _fetch_models(lat: float, lon: float) -> dict[str, dict[str, Any]] | None:
    """Every MODEL_SOURCES model's 7-day daily, from ONE request, each in
    the plain Open-Meteo shape `_rows` reads, keyed by source id. None on
    any upstream failure; a model missing from the answer is missing from
    the dict (see `_split_models`)."""
    params = _om_params(lat, lon)
    params["models"] = ",".join(MODEL_SOURCES.values())
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast",
                                 params=params)
            if r.status_code != 200:
                log.debug("open-meteo models snapshot: HTTP %d", r.status_code)
                return None
            return _split_models(r.json().get("daily"))
    except Exception:
        log.debug("open-meteo models snapshot fetch failed", exc_info=True)
        return None


def _split_models(daily: Any) -> dict[str, dict[str, Any]]:
    """A multi-model `daily` block -> one plain block per source.

    Open-Meteo suffixes every key with the model id when more than one
    model is asked for (`temperature_2m_max_gfs_seamless`). A field the
    model did not file is left out, which `_rows` reads as None; a model
    with no temperature at all is left out entirely, because a run of
    NULL highs is not a forecast (day_ahead_calls would skip every row).
    """
    if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for source, model in MODEL_SOURCES.items():
        block: dict[str, Any] = {"time": daily["time"]}
        for field in _DAILY_FIELDS:
            vals = daily.get(f"{field}_{model}")
            if isinstance(vals, list):
                block[field] = vals
        temps = (block.get("temperature_2m_max") or []) + (block.get("temperature_2m_min") or [])
        if not any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in temps):
            continue
        out[source] = block
    return out


def _key_mark(key: str) -> str:
    """A refused key's fingerprint, so a NEW key is tried at once. A hash,
    never the key: the KV row is readable by anything that reads the
    database, and the key itself already lives in one place."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def _twc_refused(key: str, now_ms: int) -> bool:
    """Is this key inside its refusal back-off?"""
    raw = await db.get_kv(_KV_TWC_REFUSED)
    if not raw:
        return False
    try:
        mark = json.loads(raw)
        return (mark.get("key") == _key_mark(key)
                and now_ms - int(mark.get("ms")) < TWC_REFUSED_BACKOFF_MS)
    except (ValueError, TypeError, AttributeError):
        return False


async def twc_status(now_ms: int | None = None) -> str:
    """Whether TWC is being archived: STATUS_ARCHIVING, STATUS_NO_KEY or
    STATUS_KEY_REFUSED (the key was refused within the back-off)."""
    key = await twc_key()
    if not key:
        return STATUS_NO_KEY
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if await _twc_refused(key, now_ms):
        return STATUS_KEY_REFUSED
    return STATUS_ARCHIVING


async def _fetch_twc(lat: float, lon: float) -> dict[str, Any] | None:
    """TWC's 5-day daily in the Open-Meteo shape, or None without a key or
    on any upstream failure (the forecast route owns the fallback story;
    the archive simply skips the beat). A key TWC refuses is backed off
    (2.7): not asked again for TWC_REFUSED_BACKOFF_MS unless it changes."""
    key = await twc_key()
    if not key:
        return None
    now_ms = int(time.time() * 1000)
    if await _twc_refused(key, now_ms):
        return None
    try:
        from . import forecast_twc
        daily = (await forecast_twc.fetch(lat, lon, key)).get("daily")
    except httpx.HTTPStatusError as e:
        code = e.response.status_code if e.response is not None else 0
        if code in (401, 403):
            await db.set_kv(_KV_TWC_REFUSED, json.dumps(
                {"ms": now_ms, "key": _key_mark(key), "status": code}))
            # The status only, never the exception: its URL carries the key.
            log.warning("forecast archive: TWC refused the Weather Underground "
                        "key (HTTP %d); not asking again for %d h or until "
                        "the key changes", code, TWC_REFUSED_BACKOFF_MS // 3_600_000)
        else:
            log.debug("twc snapshot fetch failed: HTTP %d", code)
        return None
    except Exception as e:
        # The type only: the request URL carries the key as a query
        # parameter, and a traceback would print it (Greptile, PR #57; the
        # forecast route keeps the same rule).
        log.debug("twc snapshot fetch failed: %s", type(e).__name__)
        return None
    # A key that works again (WU reactivates a station's key) clears the mark.
    if await db.get_kv(_KV_TWC_REFUSED):
        await db.set_kv(_KV_TWC_REFUSED, None)
    return daily


# The live gate, by a name the test fixture does not stub (conftest points
# `_fetch_twc` at a no-op so no test can reach TWC by accident; the gate's
# own test puts this back).
_fetch_twc_live = _fetch_twc
_fetch_models_live = _fetch_models      # 2.7, the same arrangement


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
    filed: dict[str, int] = {}
    for provider, fetch in (("open-meteo", _fetch_daily), ("twc", _fetch_twc)):
        filed[provider] = await _archive(provider, await fetch(*target),
                                         today, now_ms, target)
    models = await _fetch_models(*target) or {}
    for provider in MODEL_SOURCES:
        filed[provider] = await _archive(provider, models.get(provider),
                                         today, now_ms, target)
    # One line a beat, every source, so the log says which archived and
    # why one did not (2.7). TWC's reason is the one an operator can act on.
    parts = []
    for provider, n in filed.items():
        if n:
            parts.append(f"{provider}={n}")
        elif provider == "twc":
            st = await twc_status(now_ms)
            parts.append("twc=skipped(" + {STATUS_NO_KEY: "no key",
                                           STATUS_KEY_REFUSED: "key refused"}.get(st, "failed") + ")")
        else:
            parts.append(f"{provider}=skipped")
    log.info("forecast archive: %s", " ".join(parts))


async def _archive(provider: str, daily: dict[str, Any] | None,
                   today: _dt.date, now_ms: int,
                   target: tuple[float, float]) -> int:
    """File one source's run; the number of days filed (0 for nothing)."""
    if not daily or not daily.get("time"):
        return 0
    rows = _rows(daily, today)
    if not rows:
        return 0
    await db.insert_forecast_snapshots(provider, now_ms, rows,  # in ARCHIVED_PROVIDERS
                                       keep_days=_KEEP_DAYS,
                                       lat=target[0], lon=target[1])
    return len(rows)


async def archive_span(provider: str, near: tuple[float, float] | None
                       ) -> dict[str, Any]:
    """When this source's archive for the sky at `near` began and last
    filed, and how many runs it holds: `first_ms`, `last_ms`, `runs`
    (None/None/0 for a source never archived here). Runs for another sky
    are left out by the scorer's own rule (`_same_sky`), so "collecting
    since" means since the archive could grade THIS sky. One indexed
    read on (provider, issued_ms)."""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT issued_ms, MIN(lat) AS lat, MIN(lon) AS lon "
            "FROM forecast_snapshots WHERE provider = ? AND tmax_f IS NOT NULL "
            "GROUP BY issued_ms ORDER BY issued_ms",
            (provider,))).fetchall()
    runs = [r["issued_ms"] for r in rows
            if near is None or _same_sky(r["lat"], r["lon"], near)]
    return {"first_ms": runs[0] if runs else None,
            "last_ms": runs[-1] if runs else None,
            "runs": len(runs)}


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


async def latest_low_f(provider: str, valid_date: str, *,
                       issued_since_ms: int | None = None) -> float | None:
    """The newest issued low for one local date (YYYY-MM-DD), or None when
    the archive holds no call for it. The freeze-night card's read of the
    archive: one indexed lookup on (provider, valid_date), newest issue
    first, and rows without a low are skipped at the SQL — a call that
    lost its low in transit is not a forecast of 0°F. `issued_since_ms`
    (2.7.1) leaves out calls issued before it: a source whose key stopped
    working keeps its last calls in the archive, and those are not a
    forecast for tonight."""
    from . import db as dbmod
    since = int(issued_since_ms) if issued_since_ms is not None else 0
    async with dbmod.connect() as conn:
        row = await (await conn.execute(
            "SELECT tmin_f FROM forecast_snapshots "
            "WHERE provider = ? AND valid_date = ? AND tmin_f IS NOT NULL "
            "AND issued_ms >= ? "
            "ORDER BY issued_ms DESC LIMIT 1",
            (provider, valid_date, since))).fetchone()
    if row is None:
        return None
    try:
        v = float(row[0])
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None
