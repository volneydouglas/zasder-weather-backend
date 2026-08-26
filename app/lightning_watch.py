"""Lightning proximity + timed all-clear (1.8, Pillar A).

The NWS rule this encodes: shelter at the first strike, and the danger
is not over until 30 minutes after the LAST one — more than half of
lightning deaths happen after the storm has "passed". So this watcher
does two rarely-implemented things: it re-alerts only when a strike is
CLOSER than the one already announced (the fatigue rule Tempest's own
app proved out — never a ping per strike), and it sends an explicit
ALL CLEAR exactly once, 30 minutes after the last detected strike,
with the timer resetting on every new one.

Strike detection rides the station's own detector (the Tempest's
lightning columns): `lightningcount` is the cumulative counter — a rise
means new strikes; a fall is a counter reset (day rollover / reboot)
and re-baselines silently. `lightning_distance_mi` is the nearest
recent strike. Stations without a detector never appear here — absent
is not zero, and no detector is not "no lightning".

State per station lives in server_kv: {baseline count, last_strike_ms,
active episode, last announced distance}.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

from . import db

log = logging.getLogger("lightning")

_ALL_CLEAR_MS = 30 * 60_000
# Re-announce when the nearest strike is at least this much closer (mi).
_CLOSER_MI = 2.0
# And never more often than this, even for closing storms.
_MIN_REALERT_MS = 5 * 60_000


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


async def _state(mac: str) -> dict[str, Any]:
    raw = await db.get_kv(f"lightning_watch.{mac}")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def _save(mac: str, st: dict[str, Any]) -> None:
    await db.set_kv(f"lightning_watch.{mac}", json.dumps(st))


def _fmt_mi(d: float | None) -> str:
    if d is None:
        return ""
    return f", nearest ~{d:.0f} mi" if d >= 1.5 else ", very close"


async def check(cfg, devices: list[dict[str, Any]], now_ms: int,
                deliver) -> None:
    """One monitor-tick entry point. `deliver` is alerts._deliver, passed
    in to stay import-cycle-free (the nowcast pattern)."""
    for d in devices:
        last = d.get("lastData") or {}
        count = _f(last.get("lightningcount"))
        if count is None:
            continue                      # no detector — no opinion
        mac = d["mac"]
        name = d.get("name") or mac
        dist = _f(last.get("lightning_distance_mi"))
        st = await _state(mac)
        # Snapshot for the dirty check at the tail: an idle lightning-capable
        # station used to rewrite server_kv EVERY tick forever (R7 R9 — the
        # WAL-churn class R6 fixed in the storm checker).
        st_before = dict(st)
        baseline = _f(st.get("baseline"))

        new_strikes = False
        if baseline is None or count < baseline:
            # First sight, or a counter reset: baseline silently. A reset
            # that lands already >0 still isn't provably NEW lightning —
            # never alert off a baseline guess.
            st["baseline"] = count
        elif count > baseline:
            new_strikes = True
            st["baseline"] = count

        active = bool(st.get("active"))

        if new_strikes:
            st["last_strike_ms"] = now_ms
            announced = _f(st.get("announced_mi"))
            last_alert = int(st.get("last_alert_ms") or 0)
            should_alert = (
                not active
                or (dist is not None
                    and (announced is None or dist <= announced - _CLOSER_MI)
                    and now_ms - last_alert >= _MIN_REALERT_MS))
            if should_alert:
                closer = active
                title = (f"{name}: Lightning closing" if closer
                         else f"{name}: Lightning detected")
                body = (f"New strikes{_fmt_mi(dist)}. Danger continues "
                        f"until 30 minutes after the last strike.")
                if await deliver(cfg, f"[Zasder Weather] {title}", body,
                                 title, body,
                                 email_ok=cfg.email_scope == "all",
                                 kind="lightning", mac=mac):
                    st["active"] = True
                    st["last_alert_ms"] = now_ms
                    if dist is not None:
                        st["announced_mi"] = dist
                    log.info("lightning %s on %s%s",
                             "closing" if closer else "detected", name,
                             _fmt_mi(dist))
                else:
                    # Delivery failed on an attempted channel: rewind the
                    # baseline so the next tick still sees count > baseline
                    # and retries (the release's persist-after-deliver
                    # invariant).
                    st["baseline"] = baseline
            await _save(mac, st)
            continue

        if active:
            last_strike = int(st.get("last_strike_ms") or 0)
            if last_strike and now_ms - last_strike >= _ALL_CLEAR_MS:
                title = f"{name}: Lightning all clear"
                body = ("30 minutes since the last detected strike — the "
                        "NWS all-clear window has passed.")
                if await deliver(cfg, f"[Zasder Weather] {title}", body,
                                 title, body,
                                 email_ok=cfg.email_scope == "all",
                                 kind="lightning_clear", mac=mac):
                    st["active"] = False
                    st.pop("announced_mi", None)
                    log.info("lightning all clear on %s", name)
                await _save(mac, st)
                continue
        # Persist baseline moves — but only when something actually moved.
        if st != st_before:
            await _save(mac, st)
