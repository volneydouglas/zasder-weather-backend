"""Storm Watch Live Activity (1.8, Pillar E): live storm tracking on the
Lock Screen / Dynamic Island while an episode is open.

Rides the storm-summary tracker's state transitions — this module never
detects anything itself. Three entry points, called from
alerts._check_storm_summaries:

- `on_open_tick`: an episode is open this tick → push-to-start the
  Activity once per episode, then throttled content-state updates as the
  numbers move.
- `on_closed`: the episode closed (summary sent, or silent drizzle
  close) → one final audible update marking it ended, then bookkeeping
  cleared. The Activity self-dismisses via the dismissal date.
- `manual_start`: the app's "start storm watch" button (Volney: light
  onset sits below the rain threshold exactly when you're watching the
  sky). Opens a real episode in storm_state, so the eventual summary
  covers from the user's mark and the quiet-window machinery closes it
  the normal way — a false alarm self-cleans as a silent close.

Every push is best-effort: a transport failure logs and waits for the
next tick; nothing here may block or fail the summary pipeline.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import db

log = logging.getLogger("storm-watch")

ATTRS_TYPE = "StormWatchActivityAttributes"
# Content-state pushes at most this often while numbers are moving — well
# inside ActivityKit's budget and fast enough for a storm's rhythm.
_MIN_PUSH_GAP_MS = 3 * 60_000
# The final "ended" card lingers this long before self-dismissing.
_END_LINGER_MS = 30 * 60_000


async def _content_state(mac: str, started_ms: int, now_ms: int,
                         field: str | None, ended: bool) -> dict[str, Any]:
    stats = await db.storm_window_stats(
        mac, started_ms, now_ms, field or "yearlyrainin")
    def _num(v):
        return round(float(v), 2) if isinstance(v, (int, float)) else None
    return {
        "startMs": int(started_ms),
        "totalIn": _num(stats.get("total_in")) or 0.0,
        "rateInHr": _num(stats.get("peak_rate_in_hr")),
        "gustMph": _num(stats.get("max_gust_mph")),
        "ended": bool(ended),
    }


async def on_open_tick(cfg, device: dict, started_ms: int,
                       now_ms: int, field: str | None) -> None:
    """Called each monitor tick for a device whose episode is open."""
    if not cfg.storm_summary:
        return
    from . import apns
    mac = device["mac"]
    name = device.get("name") or mac
    la = await db.get_storm_watch_la(mac)
    try:
        if la is None or la["episode_started_ms"] != started_ms:
            # New episode (or a restart lost nothing: the row persists) —
            # start the Activity exactly once per episode.
            state = await _content_state(mac, started_ms, now_ms, field, False)
            payload = apns.build_live_activity_start(
                ATTRS_TYPE, {"stationName": name}, state,
                "Storm watch", f"{name} — storm in progress",
                now_s=now_ms // 1000,
                stale_s=(now_ms + 30 * 60_000) // 1000,
                dismiss_s=(now_ms + 12 * 3_600_000) // 1000)
            res = await apns.send_live_activity_start(
                payload, "Storm watch", f"{name} — storm in progress",
                activity="storm")
            # Record only when a token accepted the start — a failed or
            # tokenless start retries next tick, and a token registered
            # mid-storm still gets its card (CodeRabbit, PR #32).
            if res.get("sent"):
                await db.set_storm_watch_la(mac, started_ms, now_ms)
                log.info("storm watch started for %s", name)
            return
        if now_ms - la["last_push_ms"] < _MIN_PUSH_GAP_MS:
            return
        state = await _content_state(mac, started_ms, now_ms, field, False)
        payload = apns.build_live_activity_update(
            state, now_s=now_ms // 1000,
            stale_s=(now_ms + 30 * 60_000) // 1000)
        await apns.send_live_activity_update(
            "storm", payload, "Storm watch", f"{name} — updated")
        await db.set_storm_watch_la(mac, started_ms, now_ms)
    except Exception:
        log.exception("storm watch push failed for %s", mac)


async def on_closed(cfg, device: dict, started_ms: int | None,
                    ended_ms: int | None, now_ms: int,
                    field: str | None, reported: bool) -> None:
    """Episode closed. `reported` says a summary notification went out —
    the final Activity beat is audible only for silent closes, so a real
    storm never rings twice."""
    mac = device["mac"]
    la = await db.get_storm_watch_la(mac)
    if la is None:
        return
    from . import apns
    name = device.get("name") or mac
    try:
        state = await _content_state(
            mac, started_ms or la["episode_started_ms"],
            ended_ms or now_ms, field, True)
        alert = None if reported else ("Storm watch ended",
                                       f"{name} — the rain has stopped")
        payload = apns.build_live_activity_update(
            state, now_s=now_ms // 1000, event="end",
            dismiss_s=(now_ms + _END_LINGER_MS) // 1000, alert=alert)
        await apns.send_live_activity_update(
            "storm", payload, "Storm watch ended", name)
    except Exception:
        log.exception("storm watch end push failed for %s", mac)
    finally:
        await db.clear_storm_watch_la(mac)


async def manual_start(mac: str, device: dict) -> dict[str, Any]:
    """The app's button. Opens an episode at NOW (keeping the tracker's
    counter baseline so the next real tip counts), then lets the normal
    tick machinery drive the Activity. Idempotent: an already-open
    episode just reports itself."""
    now_ms = int(time.time() * 1000)
    state = await db.get_storm_state(mac) or {}
    if state.get("started_ms") is not None:
        return {"ok": True, "already_open": True,
                "started_ms": state["started_ms"]}
    await db.upsert_storm_state(
        mac, now_ms, now_ms,
        state.get("counter_field"), state.get("counter_value"),
        state.get("counter_ms") or now_ms)
    log.info("storm episode opened manually for %s", mac)
    return {"ok": True, "already_open": False, "started_ms": now_ms}
