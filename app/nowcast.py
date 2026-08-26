"""Rain-START nowcast (1.7) — the front half of the storm story.

The storm summary reports the trailing edge; this warns about the leading
one: "rain expected around 7:40 PM". The prediction is NOT ours — Open-Meteo's
free, keyless `minutely_15` endpoint serves 15-minute precipitation from
HRRR-class models; this module just polls it for the primary station's
coordinates and turns the first wet bucket into one alert through the
existing channels. The station's own gauge then ground-truths the event
(and the storm summary closes it), which is the part no consumer app has.

Design guards:
- OFF unless cfg.rain_start (alert_prefs.rain_start DB-over-env, default
  env RAIN_START_ALERTS=0) — a self-hosted box must opt into new outbound
  polling.
- One nowcast per SERVER, for the first device with coordinates (multiple
  stations in one yard would otherwise alert once each for the same sky).
- Poll at most every POLL_INTERVAL_S regardless of the monitor's tick rate.
- Edge-triggered with a cooldown, state in server_kv so a restart doesn't
  re-alert mid-event.
- "Not already raining" gate uses hourlyrainin ONLY as a suppressor —
  NEVER as onset detection (the storm-summary lesson: it's a trailing-hour
  accumulation). Worst case a wet trailing hour suppresses a warning about
  rain the user can already hear.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import settings

log = logging.getLogger("nowcast")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
POLL_INTERVAL_S = 600           # ask Open-Meteo at most every 10 min
LEAD_MAX_MIN = 60               # alert only for rain starting within the hour
RAIN_MM_MIN = 0.15              # a 15-min bucket under this is drizzle-noise
COOLDOWN_S = 3 * 3600           # one alert per event, re-arm after 3h
DRY_GATE_IN = 0.02              # trailing-hour accumulation that counts as wet

_KV_STATE = "nowcast.state"     # {"alerted_at_ms": int, "start_ms": int}

# Module-scope throttle; per-process on purpose (a restart just polls once
# more, which is harmless — the kv state is what prevents duplicate alerts).
_next_poll_ms: int = 0


def _reset_for_tests() -> None:
    global _next_poll_ms
    _next_poll_ms = 0


async def fetch_minutely(lat: float, lon: float) -> list[tuple[int, float]] | None:
    """(epoch_ms, mm) per 15-min bucket for the next ~4h, or None on any
    failure — the nowcast is best-effort and must never take the alert
    monitor down with it."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(OPEN_METEO, params={
                "latitude": round(lat, 3), "longitude": round(lon, 3),
                "minutely_15": "precipitation",
                "forecast_minutely_15": 16,
                "timezone": "UTC",
            })
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        log.warning("nowcast fetch failed: %s", e)
        return None
    block = body.get("minutely_15") or {}
    times = block.get("time") or []
    precip = block.get("precipitation") or []
    out: list[tuple[int, float]] = []
    for t, p in zip(times, precip):
        if p is None:
            continue
        try:
            ms = int(datetime.fromisoformat(t).replace(
                tzinfo=timezone.utc).timestamp() * 1000)
            out.append((ms, float(p)))
        except (ValueError, TypeError):
            continue
    return out


def first_wet_bucket(series: list[tuple[int, float]], now_ms: int,
                     lead_max_min: int = LEAD_MAX_MIN,
                     rain_mm_min: float = RAIN_MM_MIN) -> tuple[int, float] | None:
    """(start_ms, total_mm_next_2h) for the first meaningful wet bucket
    within the lead window, else None. Buckets already in the past are
    ignored (the API returns the current quarter-hour too)."""
    horizon = now_ms + lead_max_min * 60_000
    for i, (ms, mm) in enumerate(series):
        if ms + 15 * 60_000 <= now_ms:      # bucket fully in the past
            continue
        if ms > horizon:
            break
        if mm >= rain_mm_min:
            total = sum(p for t, p in series[i:] if t < ms + 2 * 3600_000)
            return ms, total
    return None


def build_message(name: str, start_ms: int, total_mm_2h: float,
                  tz_name: str) -> tuple[str, str]:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    local = datetime.fromtimestamp(start_ms / 1000, tz)
    when = local.strftime("%-I:%M %p") if hasattr(local, "strftime") else str(local)
    inches = total_mm_2h / 25.4
    title = f"Rain expected around {when}"
    body = (f"{name}: the forecast shows rain starting around {when} "
            f"(~{inches:.2f} in over the following two hours, Open-Meteo "
            f"15-minute model). Your station will confirm when it arrives.")
    return title, body


async def _get_state() -> dict[str, Any]:
    raw = await db.get_kv(_KV_STATE)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


async def check(cfg, devices: list[dict[str, Any]], now_ms: int,
                deliver) -> None:
    """One monitor-tick entry point. `deliver` is alerts._deliver, passed in
    to keep this module import-cycle-free and trivially testable."""
    global _next_poll_ms
    if not getattr(cfg, "rain_start", False):
        return
    if now_ms < _next_poll_ms:
        return
    # First device with coordinates = the nowcast location (one sky per
    # server). Stations without coords never nowcast.
    target = None
    for d in devices:
        if db.is_air_monitor_device(d):
            continue
        info = d.get("info") or {}
        coords = (info.get("coords") or {}).get("coords") or {}
        lat, lon = coords.get("lat"), coords.get("lon")
        if lat is not None and lon is not None:
            target = (d, float(lat), float(lon))
            break
    if target is None:
        return
    _next_poll_ms = now_ms + POLL_INTERVAL_S * 1000
    d, lat, lon = target

    # Suppressor only, never detection: a wet trailing hour means the user
    # doesn't need a forecast to know it's raining.
    last = d.get("lastData") or {}
    try:
        hourly = float(last.get("hourlyrainin") or 0.0)
    except (TypeError, ValueError):
        hourly = 0.0
    if hourly > DRY_GATE_IN:
        return

    series = await fetch_minutely(lat, lon)
    if not series:
        return
    hit = first_wet_bucket(series, now_ms)
    if hit is None:
        return
    start_ms, total_mm = hit

    state = await _get_state()
    alerted_at = int(state.get("alerted_at_ms") or 0)
    prev_start = int(state.get("start_ms") or 0)
    # One alert per event: inside the cooldown, or the same predicted onset
    # sliding around by a bucket or two, stays silent.
    if now_ms - alerted_at < COOLDOWN_S * 1000:
        return
    if prev_start and abs(start_ms - prev_start) < 45 * 60_000:
        return

    name = d.get("name") or d.get("mac") or "Your station"
    title, body = build_message(name, start_ms, total_mm, settings.timezone)
    if await deliver(cfg, f"[Zasder Weather] {title}", body, title, body,
                     email_ok=cfg.email_scope == "all"):
        await db.set_kv(_KV_STATE, json.dumps(
            {"alerted_at_ms": now_ms, "start_ms": start_ms}))
        log.info("rain-start alert sent: %s (+%.1f min)",
                 name, (start_ms - now_ms) / 60000)
        # Phase 2 (1.7): the same event also starts a Live Activity on any
        # phone that registered a push-to-start token — a lock-screen
        # countdown to the onset. One start push runs the whole lifecycle:
        # the countdown is client-rendered from startMs, staleness greys it
        # out shortly after onset, and the dismissal date clears it. Rides
        # INSIDE the delivered-branch on purpose — the plain push is the
        # authoritative alert, and best-effort means a Live Activity
        # failure must neither retry-loop nor block the kv stamp.
        try:
            from . import apns
            payload = apns.build_live_activity_start(
                "RainStartActivityAttributes",
                {"stationName": name},
                {"startMs": start_ms,
                 "totalIn": round(total_mm / 25.4, 2)},
                title, body,
                now_s=now_ms // 1000,
                stale_s=(start_ms + 30 * 60_000) // 1000,
                dismiss_s=(start_ms + 60 * 60_000) // 1000)
            res = await apns.send_live_activity_start(payload, title, body)
            if res.get("sent"):
                log.info("rain-start live activity started on %d device(s)",
                         res["sent"])
        except Exception as e:  # noqa: BLE001 — best-effort by contract
            log.warning("rain-start live activity failed: %s", e)
    # A failed delivery retries naturally: state stays unset and the next
    # poll re-detects the same bucket.
