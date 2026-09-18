"""Wind Ramp Live Activity (2.3, item 5): a Lock Screen / Dynamic Island
card while a wind event is on — the current gust, the event's peak and
when it hit, direction and sustained speed, and an "easing" flag once
the gusts have clearly backed off.

Observation-driven like heat_watch: the episode opens when the station's
gust exceeds `wind_live_mph` (default 35 mph, stored API-native) AND the
gust has RISEN over the last 30 minutes — a ramp, not a day that has
been blowing 40 since dawn. The rise is judged against the tick history
this module keeps in server_kv (the last half hour of gusts, one entry
per tick), never a history scan on every tick: the low point of that
window must sit at least `_RISE_MPH` under the current gust, and the
window must be at least 15 minutes deep so a fresh restart with one
entry cannot open a card on its own.

Updates go out at most every five minutes, or at once when a new peak
beats the last PUSHED peak by `_PEAK_STEP_MPH` (a gust creeping up a few
tenths a tick is a new peak every minute, and iOS drops Live Activity
updates that arrive that often — the lightning card's `_CLOSER_MI` rule).
`easing` turns on once gusts have stayed under 70% of the peak for
15 minutes (and off again if they come back). The card ends after 30
minutes under the threshold, or when the station goes stale.

Quiet hours: same footing as storm_watch — never held. Switch:
`wind_live_activity` (NULL = on).
"""
from __future__ import annotations

import logging
from typing import Any

from . import db
from . import live_state as ls

log = logging.getLogger("wind-live")

ATTRS_TYPE = "WindRampActivityAttributes"
ACTIVITY = "wind"
_KV_PREFIX = "wind_live."
_MIN_PUSH_GAP_MS = 5 * 60_000
_HISTORY_MS = 30 * 60_000
# The window must reach back at least this far before "risen" is a claim.
_MIN_HISTORY_MS = 15 * 60_000
# The gust must sit this far above the window's low point to count as a
# ramp rather than noise around a steady blow.
_RISE_MPH = 5.0
# A peak this much over the last PUSHED peak pushes at once; a smaller
# rise waits for the cadence beat, which carries it.
_PEAK_STEP_MPH = 2.0
_EASING_FRACTION = 0.7
_EASING_MS = 15 * 60_000
_UNDER_MS = 30 * 60_000
_END_LINGER_MS = 30 * 60_000


def _content_state(*, gust_mph: float, peak_mph: float, peak_ms: int,
                   direction_deg: float | None, speed_mph: float | None,
                   opened_ms: int, easing: bool) -> dict[str, Any]:
    return {
        "gustMph": round(float(gust_mph), 1),
        "peakMph": round(float(peak_mph), 1),
        "peakMs": int(peak_ms),
        "directionDeg": (int(round(direction_deg)) % 360
                         if direction_deg is not None else None),
        "speedMph": (round(float(speed_mph), 1)
                     if speed_mph is not None else None),
        "openedMs": int(opened_ms),
        "easing": bool(easing),
    }


def _risen(hist: list, gust: float, now_ms: int) -> bool:
    """A ramp: the window reaches back far enough, and its low point sits
    well under the current gust."""
    if not hist:
        return False
    oldest = hist[0][0]
    if now_ms - oldest < _MIN_HISTORY_MS:
        return False
    lows = [ls.num(v) for _ms, v in hist]
    lows = [v for v in lows if v is not None]
    if not lows:
        return False
    return gust >= min(lows) + _RISE_MPH


async def _end(st: dict[str, Any], name: str, now_ms: int, *,
               gust: float | None, direction: float | None,
               speed: float | None, reason: str) -> None:
    from . import apns
    peak = ls.num(st.get("peakMph")) or 0.0
    state = _content_state(
        gust_mph=gust if gust is not None else ls.num(st.get("lastGust")) or 0.0,
        peak_mph=peak, peak_ms=int(st.get("peakMs") or st["openedMs"]),
        direction_deg=direction, speed_mph=speed,
        opened_ms=int(st["openedMs"]), easing=True)
    payload = apns.build_live_activity_update(
        state, now_s=now_ms // 1000, event="end",
        dismiss_s=(now_ms + _END_LINGER_MS) // 1000)
    await apns.send_live_activity_update(
        ACTIVITY, payload, "Wind easing", f"{name} peaked at {peak:.0f} mph")
    log.info("wind card closed for %s (%s, peak %.1f mph)", name, reason, peak)


async def check(cfg, devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point, heat_watch's sibling."""
    if not getattr(cfg, "wind_live_activity", True):
        return
    thr = ls.num(getattr(cfg, "wind_live_mph", 35.0))
    if thr is None or thr <= 0:
        thr = 35.0
    from . import apns
    for d in devices:
        if db.is_air_monitor_device(d):
            continue
        last = d.get("lastData") or {}
        gust = ls.num(last.get("windgustmph"))
        mac = d["mac"]
        name = d.get("name") or mac
        key = _KV_PREFIX + mac
        st = await ls.load(key)
        if gust is None and not st:
            continue                      # no anemometer — no opinion
        st_before = dict(st)
        stale = ls.is_stale(d, now_ms)
        direction = ls.num(last.get("winddir"))
        speed = ls.num(last.get("windspeedmph"))

        hist = ls.prune(st.get("hist"), now_ms, _HISTORY_MS)
        # The rise is judged BEFORE this tick joins the window, so the
        # window's low point is the past, not the present reading.
        risen = gust is not None and not stale and _risen(hist, gust, now_ms)
        if gust is not None and not stale:
            hist.append([now_ms, gust])
        st["hist"] = hist

        try:
            opened = st.get("openedMs")
            if opened is None:
                if stale or gust is None or gust <= thr or not risen:
                    if st != st_before:
                        await ls.save(key, st)
                    continue
                state = _content_state(
                    gust_mph=gust, peak_mph=gust, peak_ms=now_ms,
                    direction_deg=direction, speed_mph=speed,
                    opened_ms=now_ms, easing=False)
                title = "Wind picking up"
                body = f"{name}: gusts to {gust:.0f} mph"
                payload = apns.build_live_activity_start(
                    ATTRS_TYPE, {"station": name, "mac": mac}, state,
                    title, body, now_s=now_ms // 1000,
                    stale_s=(now_ms + 20 * 60_000) // 1000,
                    dismiss_s=(now_ms + 8 * 3_600_000) // 1000)
                res = await apns.send_live_activity_start(
                    payload, title, body, activity=ACTIVITY)
                if res.get("sent"):
                    st.update({"openedMs": now_ms, "lastPushMs": now_ms,
                               "peakMph": gust, "peakMs": now_ms,
                               "pushedPeak": gust,
                               "lastGust": gust, "underSinceMs": None,
                               "calmSinceMs": None})
                    log.info("wind card opened for %s at %.1f mph", name, gust)
                await ls.save(key, st)
                continue

            # ── open episode ──
            opened = int(opened)
            if stale:
                await _end(st, name, now_ms, gust=gust, direction=direction,
                           speed=speed, reason="stale")
                await ls.save(key, {"hist": hist})
                continue
            if gust is None:
                # No reading this tick: nothing to judge; the stale rule
                # above is what ends a silent station.
                if st != st_before:
                    await ls.save(key, st)
                continue
            peak = ls.num(st.get("peakMph")) or gust
            new_peak = gust > peak
            if new_peak:
                peak = gust
                st["peakMph"] = gust
                st["peakMs"] = now_ms
                st["calmSinceMs"] = None
            st["lastGust"] = gust
            # Under the threshold for 30 minutes ends the event.
            if gust < thr:
                if st.get("underSinceMs") is None:
                    st["underSinceMs"] = now_ms
            else:
                st["underSinceMs"] = None
            under_since = st.get("underSinceMs")
            if under_since is not None and now_ms - int(under_since) >= _UNDER_MS:
                await _end(st, name, now_ms, gust=gust, direction=direction,
                           speed=speed, reason="under threshold")
                await ls.save(key, {"hist": hist})
                continue
            # Easing: under 70% of the peak for 15 minutes, and back off
            # again if the gusts return.
            if gust < peak * _EASING_FRACTION:
                if st.get("calmSinceMs") is None:
                    st["calmSinceMs"] = now_ms
            else:
                st["calmSinceMs"] = None
            calm_since = st.get("calmSinceMs")
            easing = (calm_since is not None
                      and now_ms - int(calm_since) >= _EASING_MS)
            last_push = int(st.get("lastPushMs") or 0)
            # The bypass is judged against the peak the phone last SAW,
            # not the previous tick's: +0.3 a minute is one push per
            # cadence beat, not one a minute. An episode from before this
            # rule has no pushedPeak; its first new peak pushes and sets it.
            pushed_peak = ls.num(st.get("pushedPeak"))
            peak_jump = new_peak and (pushed_peak is None
                                      or peak >= pushed_peak + _PEAK_STEP_MPH)
            if now_ms - last_push >= _MIN_PUSH_GAP_MS or peak_jump:
                state = _content_state(
                    gust_mph=gust, peak_mph=peak,
                    peak_ms=int(st.get("peakMs") or opened),
                    direction_deg=direction, speed_mph=speed,
                    opened_ms=opened, easing=easing)
                payload = apns.build_live_activity_update(
                    state, now_s=now_ms // 1000,
                    stale_s=(now_ms + 20 * 60_000) // 1000)
                await apns.send_live_activity_update(
                    ACTIVITY, payload, "Wind event", name)
                st["lastPushMs"] = now_ms
                st["pushedPeak"] = peak
            if st != st_before:
                await ls.save(key, st)
        except Exception:
            log.exception("wind live activity failed for %s", mac)
