"""Shared plumbing for the server-started Live Activities (2.3, item 5):
lightning, wind ramp and freeze night — the three siblings heat_watch
gained this cycle, each with per-station state in server_kv.

Kept deliberately small: a JSON kv load/save pair, the float coercion
every watcher repeats, the local-clock helpers, and ONE definition of
"the station is stale" so the three cards agree about when a silent
station ends its episode (health_watch's 30-minute rule — a station that
quiet belongs to the device-down alert, not to a live card that would
keep showing numbers nobody is measuring).
"""
from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import settings

# A station whose newest observation is older than this has gone quiet:
# no card opens on it, and an open card ends (health_watch's threshold).
STALE_MS = 30 * 60_000


def num(v) -> float | None:
    """A finite float, or None — absent is not zero, and a garbled
    reading stored as text is absent too."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


async def load(key: str) -> dict[str, Any]:
    raw = await db.get_kv(key)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def save(key: str, state: dict[str, Any] | None) -> None:
    await db.set_kv(key, json.dumps(state) if state else None)


def tz() -> _dt.tzinfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        return _dt.timezone.utc


def local_dt(now_ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(now_ms / 1000, tz())


def observed_ms(device: dict[str, Any], now_ms: int) -> int | None:
    """When the station last reported: the observation's own stamp, else
    the device row's last-seen. None when neither is known."""
    last = device.get("lastData") or {}
    obs = last.get("dateutc") if isinstance(last, dict) else None
    if isinstance(obs, (int, float)) and not isinstance(obs, bool) \
            and math.isfinite(obs):
        return int(obs)
    seen = device.get("lastSeen")
    if isinstance(seen, (int, float)) and not isinstance(seen, bool) \
            and math.isfinite(seen):
        return int(seen)
    return None


def is_stale(device: dict[str, Any] | None, now_ms: int) -> bool:
    """A missing device, a device with no observation at all, or one whose
    newest observation is older than STALE_MS."""
    if device is None:
        return True
    obs = observed_ms(device, now_ms)
    if obs is None:
        return True
    return now_ms - obs > STALE_MS


def prune(hist: list, now_ms: int, window_ms: int, cap: int = 240) -> list:
    """Keep the [ms, value] pairs inside the window, newest last, bounded
    so a fast poller can never grow the kv row without limit."""
    out = []
    for e in hist or []:
        if not (isinstance(e, (list, tuple)) and len(e) == 2):
            continue
        ms, v = e
        if not isinstance(ms, (int, float)) or isinstance(ms, bool):
            continue
        if now_ms - ms > window_ms:
            continue
        out.append([int(ms), v])
    return out[-cap:]
