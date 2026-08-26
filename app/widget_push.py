"""iOS 26 push-updated widgets (1.8, Pillar E): the backend nudges every
registered widget push token when fresh data is worth showing, so the
Lock/Home Screen tracks the server's schedule instead of WidgetKit's
15-minute guesses.

Throttled hard: WidgetKit reload pushes carry a system budget, and a
station posting every two seconds must not spend it. One nudge per
_MIN_GAP_MS across the whole server (the reload is extension-wide), and
only when at least one device has data newer than the last nudge — an
idle station pushes nothing.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("widget-push")

# 15 min, not 5 (R7 R2): the system budgets widget refreshes anyway, and
# 96/day sits comfortably inside the relay's widgets bucket.
_MIN_GAP_MS = 15 * 60_000
_last_push_ms = 0
_last_data_ms = 0


def _reset_for_tests() -> None:
    global _last_push_ms, _last_data_ms
    _last_push_ms = 0
    _last_data_ms = 0


async def check(devices: list[dict[str, Any]], now_ms: int) -> None:
    global _last_push_ms, _last_data_ms
    if now_ms - _last_push_ms < _MIN_GAP_MS:
        return
    newest = 0
    for d in devices:
        ts = (d.get("lastData") or {}).get("dateutc")
        if isinstance(ts, (int, float)):
            newest = max(newest, int(ts))
    if newest <= _last_data_ms:
        return                      # nothing new since the last nudge
    from . import apns
    try:
        res = await apns.send_widget_refresh()
    except Exception:
        log.exception("widget refresh push failed")
        return
    _last_push_ms = now_ms
    if res.get("sent"):
        # Only a delivered nudge consumes the reading — zero sends (no
        # registered widget tokens yet, transport hiccup) must leave the
        # newest data eligible for the next attempt (CodeRabbit, PR #32).
        _last_data_ms = newest
        log.debug("widget refresh pushed to %d token(s)", res["sent"])
