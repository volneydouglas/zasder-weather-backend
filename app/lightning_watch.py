"""Lightning proximity + timed all-clear (1.8, Pillar A).

The NWS rule this encodes: shelter at the first strike, and the danger
is not over until 30 minutes after the LAST one — more than half of
lightning deaths happen after the storm has "passed". So this watcher
does two rarely-implemented things: it re-alerts only when a strike is
CLOSER than the one already announced (the fatigue rule Tempest's own
app proved out — never a ping per strike), and it sends an explicit
ALL CLEAR exactly once, 30 minutes after the last detected strike,
with the timer resetting on every new one.

Since 2.6 the owner can set a distance (`lightning_alert_mi`): only a
strike at or inside it opens or escalates an alert, while every strike,
near or far, still restarts the all-clear clock.

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


def within(dist: float | None, limit: float | None) -> bool:
    """Does a strike at `dist` count for an alert set to `limit` miles?
    No limit: every strike counts. A limit: only a strike with a known
    distance at or inside it."""
    if limit is None:
        return True
    return dist is not None and dist <= limit


async def check(cfg, devices: list[dict[str, Any]], now_ms: int,
                deliver) -> None:
    """One monitor-tick entry point. `deliver` is alerts._deliver, passed
    in to stay import-cycle-free (the nowcast pattern).

    Per place since 2.7 (app/places.py): detectors at one place share one
    episode. The place is "active" while any of its stations is; the
    first in-range strike at a quiet place opens it with one alert (naming
    every station that heard strikes on that tick), a station whose place
    is already active joins without a second one on ANY strike, near, far
    or with no distance (the owner's distance gates alerts, not the all
    clear's clock), "closing" measures against the nearest distance the
    PLACE announced, and the all clear goes out once, when the last active
    station's 30 minutes are up (a station whose clock runs out while a
    sibling still hears strikes goes quiet without telling anyone). A
    station alone is its own place, so this is the 1.8 behaviour exactly."""
    from . import places
    limit = getattr(cfg, "lightning_alert_mi", None)
    sts: dict[str, _Station] = {}
    detectors: list[dict[str, Any]] = []
    for d in devices:
        last = d.get("lastData") or {}
        count = _f(last.get("lightningcount"))
        if count is None:
            continue                      # no detector — no opinion
        mac = d["mac"]
        st = await _state(mac)
        # Snapshot for the dirty check at the tail: an idle lightning-capable
        # station used to rewrite server_kv EVERY tick forever (R7 R9 — the
        # WAL-churn class R6 fixed in the storm checker).
        s = _Station(mac=mac, name=d.get("name") or mac,
                     dist=_f(last.get("lightning_distance_mi")),
                     st=st, before=dict(st),
                     was_active=bool(st.get("active")))
        s.baseline = _f(st.get("baseline"))
        if s.baseline is None or count < s.baseline:
            # First sight, or a counter reset: baseline silently. A reset
            # that lands already >0 still isn't provably NEW lightning —
            # never alert off a baseline guess.
            st["baseline"] = count
        elif count > s.baseline:
            s.new_strikes = True
            st["baseline"] = count
            # Every strike restarts the all-clear clock, near or far: the
            # NWS 30 minutes run from the last strike, not the last one
            # inside the owner's distance.
            st["last_strike_ms"] = now_ms
        sts[mac] = s
        detectors.append(d)

    grouped = ("lightning" in places.GROUPED_KINDS
               and "lightning_clear" in places.GROUPED_KINDS)
    for members in places.group(detectors):
        units = [members] if grouped else [[m] for m in members]
        for unit in units:
            await _check_place(cfg, [sts[m["mac"]] for m in unit], now_ms,
                               limit, deliver)

    for s in sts.values():
        # Persist baseline moves — but only when something actually moved.
        if s.st != s.before:
            await _save(s.mac, s.st)


class _Station:
    """One detector's tick: its kv state (mutated in place) and what it
    heard."""

    def __init__(self, *, mac: str, name: str, dist: float | None,
                 st: dict[str, Any], before: dict[str, Any],
                 was_active: bool) -> None:
        self.mac = mac
        self.name = name
        self.dist = dist
        self.st = st
        self.before = before
        self.was_active = was_active
        self.baseline: float | None = None
        self.new_strikes = False


async def _check_place(cfg, unit: list[_Station], now_ms: int,
                       limit: float | None, deliver) -> None:
    from . import places
    active = [s for s in unit if s.was_active]
    # What the place has already told the owner: the nearest distance an
    # active station announced, and when the last alert went out.
    announced_all = [a for a in (_f(s.st.get("announced_mi")) for s in active)
                     if a is not None]
    announced = min(announced_all) if announced_all else None
    last_alert = max((int(s.st.get("last_alert_ms") or 0) for s in active),
                     default=0)

    # 2.6: the owner's distance. A strike farther out, or one the detector
    # gave no distance for, is not "within N miles" and never opens an
    # alert. Without a distance set, any strike does (1.8 to 2.5).
    heard = [s for s in unit if s.new_strikes and within(s.dist, limit)]
    if active:
        # The place's episode is open, so a strike at ANY of its stations
        # holds the all clear: the distance gates opening and escalating,
        # never the quiet period. A sibling that heard only far strikes,
        # or strikes with no distance, joins the episode (silently, with
        # nothing announced) so the all clear below waits for its 30
        # minutes too, and so does the next tick's, from server_kv. Before
        # 2.7's review (R27-01) only an in-range sibling joined, and the
        # place said "all clear" a minute after a far strike next door.
        for s in unit:
            if s.new_strikes and not s.was_active and s not in heard:
                s.st["active"] = True
    if heard:
        if not active:
            speakers, closer = heard, False
        else:
            speakers = [s for s in heard
                        if s.dist is not None
                        and (announced is None
                             or s.dist <= announced - _CLOSER_MI)
                        and now_ms - last_alert >= _MIN_REALERT_MS]
            closer = True
            for s in heard:
                if s not in speakers and not s.was_active:
                    # The place's episode is already open: this station
                    # joins it, so the all clear waits for its strikes
                    # too. No announced distance: nothing was said.
                    s.st["active"] = True
        if speakers:
            if closer:
                # Closest first: the alert says how near it got.
                speakers.sort(key=lambda s: s.dist)
            lead = speakers[0]
            where = f" within {limit:g} mi" if limit is not None else ""
            title = (f"{lead.name}: Lightning closing" if closer
                     else f"{lead.name}: Lightning detected{where}")
            body = (f"New strikes{_fmt_mi(lead.dist)}. Danger continues "
                    f"until 30 minutes after the last strike.")
            body += places.also_line("detected",
                                     [s.name for s in speakers[1:]])
            if await deliver(cfg, f"[Zasder Weather] {title}", body,
                             title, body,
                             email_ok=cfg.email_scope == "all",
                             kind="lightning", mac=lead.mac):
                for s in speakers:
                    s.st["active"] = True
                    s.st["last_alert_ms"] = now_ms
                    if s.dist is not None:
                        s.st["announced_mi"] = s.dist
                log.info("lightning %s on %s%s",
                         "closing" if closer else "detected",
                         "+".join(s.name for s in speakers),
                         _fmt_mi(lead.dist))
            else:
                # Delivery failed on an attempted channel: rewind the
                # baseline so the next tick still sees count > baseline
                # and retries (the release's persist-after-deliver
                # invariant).
                for s in speakers:
                    s.st["baseline"] = s.baseline
        return

    # All clear: a station's 30 minutes are up when it heard nothing this
    # tick and its last strike is that old. The place's all clear goes out
    # only when every active station's are.
    def expired(s: _Station) -> bool:
        last_strike = int(s.st.get("last_strike_ms") or 0)
        return (not s.new_strikes and bool(last_strike)
                and now_ms - last_strike >= _ALL_CLEAR_MS)

    # Every station in the episode, including one that joined this tick.
    active = [s for s in unit if s.st.get("active")]
    done = [s for s in active if expired(s)]
    if not done:
        return
    if len(done) < len(active):
        # A sibling still has strikes inside its 30 minutes: these go quiet
        # and leave the all clear to it.
        for s in done:
            s.st["active"] = False
            s.st.pop("announced_mi", None)
        return
    lead = done[0]
    title = f"{lead.name}: Lightning all clear"
    body = ("30 minutes since the last detected strike — the "
            "NWS all-clear window has passed.")
    body += places.also_line("clear", [s.name for s in done[1:]])
    if await deliver(cfg, f"[Zasder Weather] {title}", body,
                     title, body,
                     email_ok=cfg.email_scope == "all",
                     kind="lightning_clear", mac=lead.mac):
        for s in done:
            s.st["active"] = False
            s.st.pop("announced_mi", None)
        log.info("lightning all clear on %s",
                 "+".join(s.name for s in done))
