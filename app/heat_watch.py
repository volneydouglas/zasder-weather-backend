"""Heat-day Live Activity (1.8, Pillar E): an all-day Lock Screen /
Dynamic Island presence on genuinely hot days — current temperature, the
running high, and when it ends, the day's verdict.

Deliberately observation-driven, not forecast-driven: the episode opens
when the station itself crosses the user's threshold (default 100 °F,
stored API-native), updates on a slow cadence as the numbers move, and
closes when the heat breaks — a sustained drop below threshold−8 °F, or
22:00 station-local, whichever comes first. iOS caps an Activity at 8
hours in the Island; the update path re-arrives via push-to-start when a
long desert day outlives that, which ActivityKit treats as a restart of
the same story.

State lives in server_kv (one sky per server, like the nowcast): the
primary station's open episode {mac, startMs, hiF, lastPushMs,
startedForMs}. Opt-in via alert_prefs.heat_day.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import settings

log = logging.getLogger("heat-watch")

ATTRS_TYPE = "HeatDayActivityAttributes"
_KV_STATE = "heat_watch.state"
_MIN_PUSH_GAP_MS = 15 * 60_000
# The heat has broken when the reading falls this far below the threshold.
_CLOSE_BELOW_F = 8.0
_LOCAL_CLOSE_HOUR = 22
# The final card lingers before self-dismissing.
_END_LINGER_MS = 30 * 60_000


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


async def _get_state() -> dict[str, Any]:
    raw = await db.get_kv(_KV_STATE)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def _set_state(state: dict[str, Any] | None) -> None:
    await db.set_kv(_KV_STATE, json.dumps(state) if state else None)


def _local_hour(now_ms: int) -> int:
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    return _dt.datetime.fromtimestamp(now_ms / 1000, tz).hour


def _content_state(start_ms: int, current: float, hi: float,
                   ended: bool) -> dict[str, Any]:
    return {"startMs": int(start_ms), "currentF": round(current, 1),
            "hiF": round(hi, 1), "ended": bool(ended)}


async def check(cfg, devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point, storm_watch's sibling."""
    if not getattr(cfg, "heat_day", False):
        return
    thr = float(getattr(cfg, "heat_day_threshold_f", 100.0))
    # State FIRST (CodeRabbit, PR #32): an open episode stays bound to the
    # station that opened it — device-order changes must not hand the card
    # another station's numbers, and the 22:00 close must run even when no
    # device currently reports a temperature.
    state = await _get_state()
    open_ms = state.get("startMs")

    if open_ms is None:
        # Opening: first device with a temperature = the day's subject
        # (one sky per server, the nowcast rule).
        target = None
        for d0 in devices:
            if db.is_air_monitor_device(d0):
                continue
            t = _f((d0.get("lastData") or {}).get("tempf"))
            if t is not None:
                target = (d0, t)
                break
        if target is None:
            return
        d, tempf = target
    else:
        d = next((x for x in devices if x.get("mac") == state.get("mac")),
                 None)
        tempf = _f((d.get("lastData") or {}).get("tempf")) if d else None

    name = (d.get("name") if d else None) or state.get("mac") or "Station"
    mac_for_log = (d or {}).get("mac", state.get("mac"))

    from . import apns
    try:
        if open_ms is None:
            if tempf < thr:
                return
            start_state = _content_state(now_ms, tempf, tempf, False)
            payload = apns.build_live_activity_start(
                ATTRS_TYPE, {"stationName": name}, start_state,
                "Heat day", f"{name} reached {tempf:.0f}°F",
                now_s=now_ms // 1000,
                stale_s=(now_ms + 45 * 60_000) // 1000,
                dismiss_s=(now_ms + 12 * 3_600_000) // 1000)
            res = await apns.send_live_activity_start(
                payload, "Heat day", f"{name} reached {tempf:.0f}°F",
                activity="heat")
            # Record the episode only when a token ACCEPTED the start —
            # otherwise the next tick retries (updates can never conjure a
            # missing Activity, and a token registered mid-episode would
            # miss the card entirely). Zero-token sends are cheap no-ops.
            if res.get("sent"):
                await _set_state({"mac": d["mac"], "startMs": now_ms,
                                  "hiF": tempf, "lastPushMs": now_ms})
                log.info("heat day opened for %s at %.1fF", name, tempf)
            return

        prev_hi = _f(state.get("hiF")) or (tempf if tempf is not None else thr)
        hi = max(prev_hi, tempf) if tempf is not None else prev_hi
        # No current reading: the temperature-based close can't be judged,
        # but the local-hour close still applies.
        closing = ((tempf is not None and tempf <= thr - _CLOSE_BELOW_F)
                   or _local_hour(now_ms) >= _LOCAL_CLOSE_HOUR)
        if tempf is None:
            tempf = hi
        if closing:
            payload = apns.build_live_activity_update(
                _content_state(open_ms, tempf, hi, True),
                now_s=now_ms // 1000, event="end",
                dismiss_s=(now_ms + _END_LINGER_MS) // 1000)
            await apns.send_live_activity_update(
                "heat", payload, "Heat day ended",
                f"{name} peaked at {hi:.0f}°F")
            await _set_state(None)
            log.info("heat day closed for %s (peak %.1fF)", name, hi)
            return

        # Pure cadence, no new-high bypass: a climbing morning sets a new
        # high every tick, and pushing each one would burn the ActivityKit
        # budget for zero information the next scheduled beat won't carry.
        last_push = int(state.get("lastPushMs") or 0)
        if now_ms - last_push >= _MIN_PUSH_GAP_MS:
            payload = apns.build_live_activity_update(
                _content_state(open_ms, tempf, hi, False),
                now_s=now_ms // 1000,
                stale_s=(now_ms + 45 * 60_000) // 1000)
            await apns.send_live_activity_update(
                "heat", payload, "Heat day", name)
            state.update({"hiF": hi, "lastPushMs": now_ms})
            await _set_state(state)
        elif hi != _f(state.get("hiF")):
            state["hiF"] = hi
            await _set_state(state)
    except Exception:
        log.exception("heat watch push failed for %s", mac_for_log)
