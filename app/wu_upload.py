"""Weather Underground live upload — forward ingested readings to WU.

Built for the MyAcurite shutdown: AcuRite's cloud was many users' only path
from their station to Weather Underground, and when its forwarding stops, so
does the WU station (and eventually the free WU API key that a live station
keeps alive — the same key the History importer and TWC forecasts need). SDR
users already have every reading landing on /ingest/custom; this module
re-posts it to WU's classic PWS upload endpoint
(`GET updateweatherstation.php?action=updateraw`).

Design constraints, in order:
- **Must never block or fail ingest.** `schedule()` is called AFTER the
  ingest write has committed, fires an asyncio task, and `maybe_upload`
  swallows every failure into per-mac health counters (surfaced on
  /api/sources).
- **Per-mac throttle ≥ 60s.** WU's standard endpoint wants ~1/min and SDR
  sources post every ~16-60s; the throttle counts ATTEMPTS, so a failing
  station also can't hammer WU.
- **Units are API-native already** (°F / mph / inHg / inches — exactly WU's
  upload units). No conversion layer, per the project invariant.
- **The station key and the full request URL are never logged or stored.**
  httpx exception reprs embed the keyed URL (the trap wu_import.py
  documents), so failures record the exception TYPE or HTTP status only.

Config is app-managed per device (wu_station_map.upload_key /
upload_enabled, via PUT /api/devices/{mac}/wu-station) and read from the DB
at call time — no env vars, no restart needed to enable or disable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import settings

log = logging.getLogger("zasder.wu_upload")

WU_UPLOAD_URL = ("https://weatherstation.wunderground.com/weatherstation/"
                 "updateweatherstation.php")
UPLOAD_TIMEOUT_S = 8
# Attempt throttle per mac. WU's classic endpoint is designed for ~1/min;
# uploading a subset of readings is fine because each upload carries the
# station's full current state (`_current_state`), so dropping the ones in
# between loses nothing.
UPLOAD_MIN_INTERVAL_S = 60.0
# How far apart the incoming post and the station's composite may be for
# the composite to fill the post's gaps: db.latest_observation's own
# lookback, so WU sees what the dashboard shows and nothing older.
COMPOSITE_MAX_SKEW_MS = 5 * 60 * 1000

# Flat ingest row key → WU upload query parameter. The flat row's units are
# already WU's native upload units, so this is a pure rename. rainin is WU's
# past-1-hour rain — hourlyrainin's exact meaning (NOT an instantaneous
# rate; see wu_import.py's precipRate note for the inverse mapping).
_FIELD_MAP: dict[str, str] = {
    "tempf":          "tempf",
    "humidity":       "humidity",
    "dewPoint":       "dewptf",
    "baromrelin":     "baromin",
    "windspeedmph":   "windspeedmph",
    "windgustmph":    "windgustmph",
    "winddir":        "winddir",
    "hourlyrainin":   "rainin",
    "dailyrainin":    "dailyrainin",
    "solarradiation": "solarradiation",
    "uv":             "UV",
    "tempinf":        "indoortempf",
    "humidityin":     "indoorhumidity",
}

# Module-global state (single event loop; process-lifetime like main.py's
# caches). Reset per test via conftest's reload list. Both maps are bounded
# like ingest._rain_reject — they're fed by request input (device MACs).
_last_attempt: dict[str, float] = {}      # mac -> time.monotonic() of last attempt
_stats: dict[str, dict[str, Any]] = {}    # mac -> last_ok_ms / failures / last_error
_MAX_TRACKED = 512
# Strong refs to in-flight tasks: the event loop only keeps weak references,
# so a fire-and-forget task with no other referent can be GC'd mid-upload.
_TASKS: set[asyncio.Task[Any]] = set()


def stats(mac: str) -> dict[str, Any]:
    """Health snapshot for one mac (never contains the key)."""
    st = _stats.get(mac, {})
    return {"last_ok_ms": st.get("last_ok_ms"),
            "failures": st.get("failures", 0),
            "last_error": st.get("last_error")}


def _bound(d: dict[str, Any]) -> None:
    if len(d) >= _MAX_TRACKED:
        d.pop(next(iter(d)))


def _record_success(mac: str) -> None:
    _bound(_stats)
    st = _stats.setdefault(mac, {})
    st["last_ok_ms"] = int(time.time() * 1000)
    st["failures"] = 0
    st["last_error"] = None


def _record_failure(mac: str, error: str) -> None:
    """`error` must already be scrubbed: HTTP status or exception type only.
    Never pass str(exc) — httpx reprs embed the keyed request URL."""
    _bound(_stats)
    st = _stats.setdefault(mac, {})
    st["failures"] = st.get("failures", 0) + 1
    st["last_error"] = error
    log.warning("WU upload failed for %s (%s; %d consecutive)", mac, error,
                st["failures"])


def build_params(station_id: str, upload_key: str,
                 flat: dict[str, Any]) -> dict[str, Any]:
    """The updateweatherstation.php query for one flat ingest row. Only
    fields the row actually carries are sent — a station without a solar
    sensor must not upload zeros."""
    ts = datetime.fromtimestamp(flat["dateutc"] / 1000, tz=timezone.utc)
    params: dict[str, Any] = {
        "ID": station_id,
        "PASSWORD": upload_key,
        "action": "updateraw",
        "softwaretype": "ZasderWeather",
        "dateutc": ts.strftime("%Y-%m-%d %H:%M:%S"),
    }
    for src, dst in _FIELD_MAP.items():
        v = flat.get(src)
        if v is not None:
            params[dst] = v
    return params


async def _current_state(mac: str, flat: dict[str, Any]) -> dict[str, Any]:
    """The reading to upload: this post's own values, with the fields it
    lacks filled from the station's current composite.

    A partial poster sends one subset per post: the AcuRite Atlas rotates
    temperature + humidity, wind direction + rain, and UV + light messages
    about 10 s apart, and the LilyGO relay or an rtl_433 bridge posts each
    as it is heard. Uploading the post alone gave WU whichever subset held
    the 60 s slot, so its graphs were patchy while the dashboard (which
    composites) looked whole (mirror #8, 2026-10-15). The composite is the
    dashboard's: non-null values from the last few minutes, never older,
    never zero for a field the station lacks. Counter-only rain (SDR
    sources post a lifetime counter WU has no parameter for) gets the
    day's total and the hour's from that counter.

    Both are anchored to the post (CodeRabbit, PR #103): the composite
    window is measured back from the post's own time (never the newest
    stored row, which the history throttle can leave minutes behind, and
    never a reading taken after it). Rain is anchored harder (2.7
    follow-up F27-03): a day total is never carried across the post's
    local midnight (0.8 in at 23:59:30 is not the new day's), and the
    derived day and hour are the counter's rise up to the post's own time
    in the post's own local day and hour, not the rollups of NOW, which
    lent a delayed 12:20 post the rain measured at 12:30. What cannot be
    reconstructed is left out: absent, never wrong."""
    out = dict(flat)
    post_ms = int(flat["dateutc"])
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    day_start_ms = int(datetime.fromtimestamp(post_ms / 1000, tz=tz).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    try:
        latest = await db.latest_observation(mac, post_ms, day_start_ms=day_start_ms)
    except Exception as e:
        log.warning("WU upload composite read failed for %s (%s)", mac,
                    type(e).__name__)
        latest = None
    if latest and isinstance(latest.get("dateutc"), (int, float)) and \
            abs(latest["dateutc"] - post_ms) <= COMPOSITE_MAX_SKEW_MS:
        for k in _FIELD_MAP:
            if out.get(k) is None and latest.get(k) is not None:
                out[k] = latest[k]
        if out.get("yearlyrainin") is None:
            out["yearlyrainin"] = latest.get("yearlyrainin")
    if out.get("yearlyrainin") is not None and (
            out.get("dailyrainin") is None or out.get("hourlyrainin") is None):
        try:
            derived = await db.counter_rain_through(mac, post_ms, settings.timezone)
        except Exception as e:
            log.warning("WU upload rain derivation failed for %s (%s)", mac,
                        type(e).__name__)
            derived = {}
        for k, src in (("dailyrainin", "daily_in"), ("hourlyrainin", "hourly_in")):
            if out.get(k) is None and derived.get(src) is not None:
                out[k] = derived[src]
    return out


async def _send(params: dict[str, Any]) -> tuple[int, str]:
    """(status_code, body) for one upload GET. Split out so tests monkeypatch
    the transport without an httpx mock (the wu_import._fetch_day pattern)."""
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_S) as client:
        r = await client.get(WU_UPLOAD_URL, params=params)
        return r.status_code, r.text


async def maybe_upload(mac: str, flat: dict[str, Any]) -> bool:
    """Upload one reading if this mac has forwarding enabled + configured and
    is outside the throttle window. True only when WU acknowledged. Never
    raises — this runs as a fire-and-forget task off the ingest path."""
    try:
        if not isinstance(flat.get("dateutc"), (int, float)):
            return False
        # Throttle check BEFORE the SQLite read: at SDR cadence (~16-60 s)
        # roughly 3 of 4 ingested readings land inside the 60 s window, and
        # each was paying a DB read just to find that out (R4-53).
        now = time.monotonic()
        last = _last_attempt.get(mac)
        if last is not None and now - last < UPLOAD_MIN_INTERVAL_S:
            return False
        # Reserve the slot BEFORE the await: two overlapping tasks for the
        # same mac could otherwise both pass the check above, both read the
        # config, and both send — bypassing the 60 s limit (CodeRabbit).
        _bound(_last_attempt)
        _last_attempt[mac] = now
        row = await db.get_wu_station(mac)
        if (row is None or not row["upload_enabled"]
                or not row["station_id"] or not row["upload_key"]):
            # Not configured: give the reservation back so enabling
            # forwarding a moment later doesn't wait out a phantom attempt.
            _last_attempt.pop(mac, None)
            return False
        params = build_params(row["station_id"], row["upload_key"],
                              await _current_state(mac, flat))
        try:
            status_code, body = await _send(params)
        except Exception as e:
            # Type only — httpx exception text embeds the keyed URL.
            _record_failure(mac, type(e).__name__)
            return False
        # WU acks with a bare "success" body; anything else (INVALIDPASSWORDID,
        # an HTML error page) is a failure. Status/short-marker only — never
        # store or log the body wholesale, and never the URL.
        if status_code == 200 and "success" in body.lower():
            _record_success(mac)
            return True
        # Fixed-marker match only (never the body wholesale): WU answers
        # HTTP 200 + "INVALIDPASSWORDID" for bad credentials — the one
        # failure the user can actually fix, so name it (R4-49).
        if "invalidpasswordid" in body.lower():
            _record_failure(mac, "WU rejected the station ID/key")
        else:
            _record_failure(mac, f"HTTP {status_code}" if status_code != 200
                            else "HTTP 200 without success ack")
        return False
    except Exception as e:  # pragma: no cover — belt and braces for the task
        log.warning("WU upload internal error for %s (%s)", mac,
                    type(e).__name__)
        return False


def schedule(mac: str, flat: dict[str, Any]) -> None:
    """Fire-and-forget a maybe_upload task. Called by ingest AFTER its write
    committed; must never raise into the ingest path (and doesn't: no running
    loop degrades to a debug line, and maybe_upload catches its own errors)."""
    try:
        task = asyncio.get_running_loop().create_task(
            maybe_upload(mac, flat), name="wu-upload")
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except Exception:  # pragma: no cover
        log.debug("could not schedule WU upload for %s", mac, exc_info=True)
