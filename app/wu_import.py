"""Weather Underground historical importer.

Pulls a station's archive from The Weather Company's PWS history API
(`GET /v2/pws/history/all`, one date per call) and inserts it into
`observations` under an existing device MAC. Built for the 1.4 History
feature: users with years of WU history (station owners get the API key
free) backfill it into their own backend.

Design constraints, in order:
- **Idempotent**: rows insert via `db.insert_observations` (INSERT OR
  IGNORE on the (mac, dateutc_ms) primary key), so re-running an import —
  or overlapping one with live ingest — never duplicates.
- **Units are API-native already**: `units=e` returns °F / mph / inHg /
  inches, the backend's storage units. No conversion layer — the bug
  class that has shipped more than once stays structurally impossible.
- **Rain**: WU's `precipTotal` is cumulative SINCE MIDNIGHT → maps to
  `dailyrainin`. `precipRate` is an INSTANTANEOUS rate (in/hr) and is kept
  only in data_json (its own `precipRate` key) — it must NOT map to
  `hourlyrainin`, which everywhere else means trailing-1-hour ACCUMULATION
  (`_derive_hourly_rain`); mapping it charted rate spikes as "hourly rain"
  that never reconciled with `dailyrainin`. `yearlyrainin` is
  deliberately never written: the rain rollups judge yearly counters over
  all history, and a synthesized yearly series could retroactively change
  live rollups (see project rain invariants).
- **Rate-limited**: the free PWS key allows ~30 calls/min, 1500/day. One
  call every WU_CALL_GAP_S keeps a multi-year import inside both.
- **The API key never persists**: it lives in the running task's closure,
  is never logged, and never appears in the status snapshot.

One import runs at a time (module-global state, like main.py's caches);
progress is polled via GET /api/import/wu/status.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Any

import httpx

from . import db
from .config import settings
from .ingest import _apply_plausibility_bands

log = logging.getLogger("zasder.wu_import")

WU_BASE = "https://api.weather.com/v2/pws/history/all"
WU_CALL_GAP_S = 2.5          # ~24/min, under the 30/min free-key cap
_RETRY_429_SLEEP_S = 65      # one polite retry after a rate-limit response
# One retry of the SAME day after a transient transport error (ConnectError,
# ReadTimeout, a dropped connection). A single network blip mid-way through a
# multi-year import otherwise parks the whole run with resume state and waits
# for a manual resume; one short-fuse retry rides it out. A second failure
# still takes the resume-state path.
_RETRY_TRANSPORT_SLEEP_S = 5
# The free PWS key allows ~1500 calls/day, one call per day of history.
# Stop cleanly a bit under the cap (probes and other tools share the quota)
# and record where to resume — a multi-year import is a multi-day job by
# WU's rules, not ours.
WU_DAILY_CALL_BUDGET = 1400

# Module-global import state (single event loop; reset in tests' conftest is
# unnecessary — every test drives its own run to completion or cancels it).
_state: dict[str, Any] = {"running": False}
_task: asyncio.Task[None] | None = None


def status() -> dict[str, Any]:
    """Public snapshot — everything except the API key (which is never in
    the dict to begin with; this copy keeps callers from mutating state)."""
    return dict(_state)


def _feels_like(temp: float | None, heat_index: float | None,
                wind_chill: float | None) -> float | None:
    """Match the relays' convention: heat index only at/above 80 °F, wind
    chill only at/below 50 °F, otherwise the air temperature itself."""
    if temp is None:
        return None
    if temp >= 80 and heat_index is not None:
        return heat_index
    if temp <= 50 and wind_chill is not None:
        return wind_chill
    return temp


def transform_observation(o: dict[str, Any], station_id: str) -> dict[str, Any] | None:
    """One WU history observation → one API-native observation row (the dict
    shape `db.insert_observations` stores). Returns None when the row has no
    usable timestamp. Missing readings stay None — a WU station without a
    solar sensor must not import as zeros."""
    epoch = o.get("epoch")
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    # WU's pre-~2019 archive returns epoch in MILLISECONDS (13 digits;
    # seen live on 2015 data) while the modern API returns seconds.
    # Unnormalized, the seconds path lands in year ~47000 and every day
    # of an old import dies with ValueError. 1e11 seconds is year 5138 —
    # anything above it can only be milliseconds.
    if epoch > 1e11:
        epoch /= 1000.0
    imp = o.get("imperial") or {}

    def num(src: dict[str, Any], key: str) -> float | None:
        v = src.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    temp = num(imp, "tempAvg")
    press_hi, press_lo = num(imp, "pressureMax"), num(imp, "pressureMin")
    if press_hi is not None and press_lo is not None:
        pressure = (press_hi + press_lo) / 2.0
    else:
        pressure = press_hi if press_hi is not None else press_lo

    row: dict[str, Any] = {
        "dateutc": int(epoch * 1000),
        "tempf": temp,
        "humidity": num(o, "humidityAvg"),
        "windspeedmph": num(imp, "windspeedAvg"),
        "windgustmph": num(imp, "windgustHigh"),
        "winddir": num(o, "winddirAvg"),
        "baromrelin": pressure,
        "dewPoint": num(imp, "dewptAvg"),
        "feelsLike": _feels_like(temp, num(imp, "heatindexAvg"),
                                 num(imp, "windchillAvg")),
        "solarradiation": num(o, "solarRadiationHigh"),
        "uv": num(o, "uvHigh"),
        # NOT hourlyrainin: precipRate is an instantaneous rate, and
        # hourlyrainin means trailing-1h accumulation (see module docstring).
        # Kept under its own key so it survives in data_json for provenance
        # while the column stays NULL rather than lying.
        "precipRate": num(imp, "precipRate"),
        "dailyrainin": num(imp, "precipTotal"),
        # Provenance marker, stored in data_json alongside the values.
        "source": "wu-import",
        "wu_station": station_id,
    }
    # Same physical plausibility bands the live ingest path applies. Without
    # this the importer was the one write path into `observations` with no QC
    # at all, so whatever WU's archive held became fact — including 0xFF (255)
    # anemometer-dropout sentinels that then owned the all-time wind records.
    # Field-level, so one bad reading never costs the row its good fields.
    if settings.ingest_plausibility_bands:
        dropped = _apply_plausibility_bands(row)
        if dropped:
            log.warning("wu-import: implausible values dropped for %s at %s: %s",
                        station_id, row["dateutc"], ", ".join(dropped))
    return row


class _QuotaExhausted(Exception):
    """WU answered 429 twice in a row — the key's daily quota is spent
    regardless of our own call counting."""


async def _fetch_day(client: httpx.AsyncClient, station_id: str, day: str,
                     api_key: str) -> list[dict[str, Any]]:
    """All observations for one YYYYMMDD date. Empty list for a day WU has
    nothing for (204/no-body/empty observations). Raises for auth errors."""
    params = {"stationId": station_id, "date": day, "format": "json",
              "units": "e", "numericPrecision": "decimal", "apiKey": api_key}
    r = await client.get(WU_BASE, params=params)
    if r.status_code == 429:
        log.warning("WU rate limit hit on %s; sleeping %ss", day, _RETRY_429_SLEEP_S)
        await asyncio.sleep(_RETRY_429_SLEEP_S)
        r = await client.get(WU_BASE, params=params)
        if r.status_code == 429:
            raise _QuotaExhausted()
    if r.status_code in (204, 404):
        return []
    if r.status_code == 401:
        raise PermissionError("WU API key rejected (401)")
    r.raise_for_status()
    if not r.content:
        return []
    body = r.json()
    obs = body.get("observations")
    return obs if isinstance(obs, list) else []


def _init_state(mac: str, station_id: str, start: date, end: date,
                dry_run: bool) -> None:
    """Full state snapshot for a new run. Called from start_import (so the
    snapshot is coherent from the moment the slot is claimed — a status poll
    landing before the task first runs must not see running=True merged with
    the PREVIOUS import's station/progress/terminal fields) and again from
    _run (idempotent; keeps direct _run callers, incl. tests, working)."""
    _state.update({
        "running": True, "mac": mac, "wu_station_id": station_id,
        "dry_run": dry_run, "start_date": start.isoformat(),
        "end_date": end.isoformat(), "total_days": (end - start).days + 1,
        "done_days": 0, "rows_seen": 0, "rows_inserted": 0,
        "empty_days": 0, "current_day": None, "error": None,
        "started_ms": int(time.time() * 1000), "finished_ms": None,
        "cancelled": False, "calls_made": 0,
        "call_budget": WU_DAILY_CALL_BUDGET, "resume_from": None,
    })


async def _run(mac: str, station_id: str, api_key: str,
               start: date, end: date, dry_run: bool) -> None:
    _init_state(mac, station_id, start, end, dry_run)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            day = start
            while day <= end:
                if _state.get("cancelled"):
                    # Record the first unprocessed day so a cancelled run is
                    # resumable exactly like a quota-paused one.
                    _state["resume_from"] = day.isoformat()
                    break
                if _state["calls_made"] >= WU_DAILY_CALL_BUDGET:
                    # Out of quota for today: stop cleanly and record where
                    # to pick up. Resuming re-runs are duplicate-safe, so an
                    # overlapping restart costs only calls.
                    _state["resume_from"] = day.isoformat()
                    break
                _state["current_day"] = day.isoformat()
                _state["calls_made"] += 1
                try:
                    try:
                        obs = await _fetch_day(client, station_id,
                                               day.strftime("%Y%m%d"), api_key)
                    except httpx.TransportError as e:
                        # The retry is a real upstream call and must respect
                        # the same budget as the first attempt: when the
                        # failed call consumed the final slot, park the run
                        # (this day stays unprocessed → resume_from) instead
                        # of exceeding WU_DAILY_CALL_BUDGET by one.
                        if _state["calls_made"] >= WU_DAILY_CALL_BUDGET:
                            raise _QuotaExhausted() from None
                        # Type-and-day only, like the outer handler — httpx
                        # exception reprs embed the request URL, which carries
                        # the API key as a query parameter.
                        log.warning("WU transient %s on %s; retrying once in "
                                    "%ss", type(e).__name__, day.isoformat(),
                                    _RETRY_TRANSPORT_SLEEP_S)
                        await asyncio.sleep(_RETRY_TRANSPORT_SLEEP_S)
                        _state["calls_made"] += 1
                        # A second failure propagates to the outer handler,
                        # which parks the run with resume_from as before.
                        obs = await _fetch_day(client, station_id,
                                               day.strftime("%Y%m%d"), api_key)
                except _QuotaExhausted:
                    _state["resume_from"] = day.isoformat()
                    log.warning("WU quota exhausted at %s; import paused for "
                                "the day", day.isoformat())
                    break
                rows = [r for o in obs
                        if (r := transform_observation(o, station_id))]
                _state["rows_seen"] += len(rows)
                if not rows:
                    _state["empty_days"] += 1
                elif not dry_run:
                    _state["rows_inserted"] += await db.insert_observations(mac, rows)
                _state["done_days"] += 1
                day += timedelta(days=1)
                if day <= end:
                    await asyncio.sleep(WU_CALL_GAP_S)
    except PermissionError as e:
        _state["error"] = str(e)
    except Exception as e:
        # A transient failure (ConnectError, ReadTimeout, WU 5xx, malformed
        # JSON) must leave a resume point like quota exhaustion does: on a
        # multi-year import re-running the whole range re-burns hundreds of
        # quota calls just to reach the day that failed.
        _state["resume_from"] = _state.get("current_day")
        # Keep the failure diagnosable without leaking the request URL,
        # which carries the API key as a query parameter.
        _state["error"] = f"{type(e).__name__}: day {_state.get('current_day')}"
        # Type-and-day only — httpx exception reprs embed the request URL,
        # which carries the API key as a query parameter. Never full details.
        log.error("WU import failed on %s (%s)", _state.get("current_day"),
                  type(e).__name__)
    finally:
        _state["running"] = False
        _state["current_day"] = None
        _state["finished_ms"] = int(time.time() * 1000)


def start_import(mac: str, station_id: str, api_key: str,
                 start: date, end: date, dry_run: bool) -> bool:
    """Kick off a background import. False if one is already running."""
    global _task
    if _state.get("running"):
        return False
    # Claim the slot SYNCHRONOUSLY: two rapid POSTs interleave before the
    # created task first runs, so checking alone is a check-then-act race —
    # both would pass and two imports would fight over the quota. The claim
    # is the FULL state snapshot, not just running=True: a status poll in
    # the window before the task first runs must not see running=True mixed
    # with the previous import's fields. _run()'s own _init_state keeps
    # running=True, so this claim is never dropped.
    _init_state(mac, station_id, start, end, dry_run)
    _task = asyncio.create_task(
        _run(mac, station_id, api_key, start, end, dry_run),
        name="wu-import")
    return True


def cancel() -> bool:
    """Request a graceful stop after the in-flight day. False if idle."""
    if not _state.get("running"):
        return False
    _state["cancelled"] = True
    return True
