"""Lightning Live Activity (2.3, item 5): a Lock Screen / Dynamic Island
card while lightning is near — nearest strike, strikes in the last hour,
which way the storm is moving, and the NWS all-clear countdown once the
strikes stop.

Rides the same detector fields lightning_watch (the 1.8 proximity alert)
rides: `lightningcount` is the station's cumulative counter (a rise means
new strikes; a fall is a reset and re-baselines silently),
`lightning_distance_mi` the nearest recent strike, `lightning_last_1hr`
the detector's own hour tally when the station carries one (Tempest).
Stations without a detector never appear here — absent is not zero, and
no detector is not "no lightning".

The episode opens on a NEW strike within `lightning_live_mi` (default 10
mi, stored API-native) while the station is fresh, updates at most every
five minutes unless the nearest strike moved at least two miles closer,
sets the all-clear clock (last strike + 30 min) as soon as a tick passes
with no new strike, and ends when that clock runs out or the station
goes stale. State per station lives in server_kv.

Quiet hours: same footing as storm_watch — the card is a Live Activity,
not a notification, so it is never held. `lightning_live_activity`
(NULL = on) turns it off on its own.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db
from . import live_state as ls

log = logging.getLogger("lightning-live")

ATTRS_TYPE = "LightningWatchActivityAttributes"
ACTIVITY = "lightning"
_KV_PREFIX = "lightning_live."
_MIN_PUSH_GAP_MS = 5 * 60_000
# A strike this much nearer than the last PUSHED distance pushes at once.
_CLOSER_MI = 2.0
_ALL_CLEAR_MS = 30 * 60_000
_HOUR_MS = 60 * 60_000
_END_LINGER_MS = 30 * 60_000


def _content_state(*, distance_mi: float | None, strikes_1h: int,
                   last_strike_ms: int, opened_ms: int, trend: str,
                   all_clear_at_ms: int | None,
                   ended: bool) -> dict[str, Any]:
    # `ended` is the storm_watch/heat_watch end marker: False on every
    # live beat, True on the end beat only, so the card can say "all
    # clear" for the linger instead of the last live headline.
    return {
        "distanceMi": (round(float(distance_mi), 1)
                       if distance_mi is not None else None),
        "strikes1h": int(strikes_1h),
        "lastStrikeMs": int(last_strike_ms),
        "openedMs": int(opened_ms),
        "trend": trend,
        "allClearAtMs": (int(all_clear_at_ms)
                         if all_clear_at_ms is not None else None),
        "ended": bool(ended),
    }


def _trend(prev: float | None, cur: float | None) -> str:
    if prev is None or cur is None:
        return "steady"
    if cur < prev:
        return "closer"
    if cur > prev:
        return "farther"
    return "steady"


def _strikes_1h(st: dict[str, Any], last: dict[str, Any], now_ms: int) -> int:
    """The detector's own hour tally when it reports one, else the sum of
    the counter rises this module saw in the last hour."""
    tally = int(sum(n for _ms, n in st.get("strikes") or []))
    own = ls.num(last.get("lightning_last_1hr"))
    if own is not None and own >= 0:
        # The detector's tally can lag the counter by a beat, so the tick
        # that saw the rise never reads as "no strikes this hour".
        return max(int(own), tally)
    return tally


async def _end(st: dict[str, Any], name: str, now_ms: int,
               *, distance_mi: float | None, strikes_1h: int,
               reason: str) -> None:
    from . import apns
    last_strike = int(st.get("lastStrikeMs") or now_ms)
    state = _content_state(
        distance_mi=distance_mi, strikes_1h=strikes_1h,
        last_strike_ms=last_strike, opened_ms=int(st["openedMs"]),
        trend=str(st.get("trend") or "steady"),
        all_clear_at_ms=last_strike + _ALL_CLEAR_MS, ended=True)
    payload = apns.build_live_activity_update(
        state, now_s=now_ms // 1000, event="end",
        dismiss_s=(now_ms + _END_LINGER_MS) // 1000)
    await apns.send_live_activity_update(
        ACTIVITY, payload, "Lightning all clear", name)
    log.info("lightning card closed for %s (%s)", name, reason)


async def check(cfg, devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point, heat_watch's sibling."""
    if not getattr(cfg, "lightning_live_activity", True):
        return
    thr = ls.num(getattr(cfg, "lightning_live_mi", 10.0))
    if thr is None or thr <= 0:
        thr = 10.0
    from . import apns
    for d in devices:
        if db.is_air_monitor_device(d):
            continue
        last = d.get("lastData") or {}
        count = ls.num(last.get("lightningcount"))
        if count is None:
            continue                      # no detector — no opinion
        mac = d["mac"]
        name = d.get("name") or mac
        key = _KV_PREFIX + mac
        dist = ls.num(last.get("lightning_distance_mi"))
        st = await ls.load(key)
        st_before = dict(st)
        stale = ls.is_stale(d, now_ms)

        # Counter bookkeeping first, the lightning_watch way.
        baseline = ls.num(st.get("baseline"))
        new_strikes = 0.0
        if baseline is None or count < baseline:
            st["baseline"] = count
        elif count > baseline:
            new_strikes = count - baseline
            st["baseline"] = count
        strikes = ls.prune(st.get("strikes"), now_ms, _HOUR_MS)
        if new_strikes and not stale:
            strikes.append([now_ms, int(new_strikes)])
        st["strikes"] = strikes

        try:
            opened = st.get("openedMs")
            if opened is None:
                # Opening needs a NEW strike, a known distance inside the
                # threshold, a fresh station, and strikes in the hour.
                if (stale or not new_strikes or dist is None or dist > thr
                        or _strikes_1h(st, last, now_ms) <= 0):
                    if st != st_before:
                        await ls.save(key, st)
                    continue
                state = _content_state(
                    distance_mi=dist, strikes_1h=_strikes_1h(st, last, now_ms),
                    last_strike_ms=now_ms, opened_ms=now_ms,
                    trend="steady", all_clear_at_ms=None, ended=False)
                title = "Lightning nearby"
                body = f"{name}: strike about {dist:.0f} mi away"
                payload = apns.build_live_activity_start(
                    ATTRS_TYPE, {"station": name, "mac": mac}, state,
                    title, body, now_s=now_ms // 1000,
                    stale_s=(now_ms + 20 * 60_000) // 1000,
                    dismiss_s=(now_ms + 4 * 3_600_000) // 1000)
                res = await apns.send_live_activity_start(
                    payload, title, body, activity=ACTIVITY)
                # Record the episode only when a token ACCEPTED the start
                # (the heat_watch rule): a tokenless start retries on the
                # next strike, never on a stale count.
                if res.get("sent"):
                    st.update({"openedMs": now_ms, "lastPushMs": now_ms,
                               "lastStrikeMs": now_ms, "lastDist": dist,
                               "pushedDist": dist})
                    log.info("lightning card opened for %s at %.1f mi",
                             name, dist)
                await ls.save(key, st)
                continue

            # ── open episode ──
            opened = int(opened)
            last_strike = int(st.get("lastStrikeMs") or opened)
            strikes_1h = _strikes_1h(st, last, now_ms)
            prev_dist = ls.num(st.get("lastDist"))
            if new_strikes and not stale:
                last_strike = now_ms
                st["lastStrikeMs"] = now_ms
            if stale or now_ms - last_strike >= _ALL_CLEAR_MS:
                await _end(st, name, now_ms,
                           distance_mi=dist if dist is not None else prev_dist,
                           strikes_1h=strikes_1h,
                           reason="stale" if stale else "all clear")
                await ls.save(key, {"baseline": count, "strikes": strikes})
                continue
            cur_dist = dist if dist is not None else prev_dist
            # The trend is judged on strike ticks (the distance only moves
            # with a strike) and REMEMBERED, so a strike inside the push
            # gap still colours the next beat.
            if new_strikes and not stale and dist is not None:
                st["trend"] = _trend(prev_dist, dist)
                st["lastDist"] = dist
            trend = str(st.get("trend") or "steady")
            all_clear = None if new_strikes else last_strike + _ALL_CLEAR_MS
            last_push = int(st.get("lastPushMs") or 0)
            pushed_dist = ls.num(st.get("pushedDist"))
            closer = (dist is not None and pushed_dist is not None
                      and dist <= pushed_dist - _CLOSER_MI)
            if now_ms - last_push >= _MIN_PUSH_GAP_MS or closer:
                state = _content_state(
                    distance_mi=cur_dist, strikes_1h=strikes_1h,
                    last_strike_ms=last_strike, opened_ms=opened,
                    trend=trend, all_clear_at_ms=all_clear, ended=False)
                payload = apns.build_live_activity_update(
                    state, now_s=now_ms // 1000,
                    stale_s=(now_ms + 20 * 60_000) // 1000)
                await apns.send_live_activity_update(
                    ACTIVITY, payload, "Lightning nearby", name)
                st["lastPushMs"] = now_ms
                if cur_dist is not None:
                    st["pushedDist"] = cur_dist
            if st != st_before:
                await ls.save(key, st)
        except Exception:
            log.exception("lightning live activity failed for %s", mac)
