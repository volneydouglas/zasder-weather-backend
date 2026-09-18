"""Freeze Night Live Activity (2.3, item 5): a Lock Screen / Dynamic
Island card through a night that may freeze — the current temperature,
the forecast low, the next sunrise, whether it has frozen yet, and the
lowest reading so far.

Opens in the evening (local 18:00 until dawn) when either the forecast
low for the night is at or below 32 °F (the newest Open-Meteo call in the
forecast_snapshots archive, the same archive the scorecard reads) or the
station itself reads 36 °F or colder AND falling over the last hour
(judged against the tick history this module keeps in server_kv). Never
on a stale station, never without a temperature — absent is not zero.

`froze` latches once the station reads 32 °F or below; `minF` is the
lowest reading of the episode. Pushes go out every 15 minutes, or at
once when `froze` flips. The card ends an hour after sunrise (at 07:00
local when no coordinates give a sunrise — a card with no daytime end
once ran 17 hours), or once the station has read above 36 °F for an
hour, or when it goes stale.

Quiet hours: same footing as storm_watch — never held. Switch:
`freeze_live_activity` (NULL = on). Every constant here is °F, the
storage unit.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from . import db
from . import live_state as ls

log = logging.getLogger("freeze-live")

ATTRS_TYPE = "FreezeActivityAttributes"
ACTIVITY = "freeze"
_KV_PREFIX = "freeze_live."
_MIN_PUSH_GAP_MS = 15 * 60_000
_FREEZE_F = 32.0
_WATCH_F = 36.0
_FORECAST_LOW_F = 32.0
_HISTORY_MS = 60 * 60_000
# The hour-ago anchor must be at least this old before "falling" is judged.
_MIN_HISTORY_MS = 45 * 60_000
# The reading must sit this far under the anchor to count as falling.
_FALL_F = 1.0
_ABOVE_MS = 60 * 60_000
_AFTER_SUNRISE_MS = 60 * 60_000
_EVENING_HOUR = 18
_DAWN_HOUR = 6
_END_LINGER_MS = 30 * 60_000
_FORECAST_PROVIDER = "open-meteo"


def _content_state(*, tempf: float, low_f: float | None,
                   sunrise_ms: int | None, opened_ms: int, froze: bool,
                   min_f: float, ended: bool) -> dict[str, Any]:
    # `ended` is the storm_watch/heat_watch end marker: False on every
    # live beat, True on the end beat only, so the card can say the
    # night is over for the linger instead of "Freeze tonight".
    return {
        "tempf": round(float(tempf), 1),
        "lowF": round(float(low_f), 1) if low_f is not None else None,
        "sunriseMs": int(sunrise_ms) if sunrise_ms is not None else None,
        "openedMs": int(opened_ms),
        "froze": bool(froze),
        "minF": round(float(min_f), 1),
        "ended": bool(ended),
    }


def _local_hour(now_ms: int) -> int:
    return ls.local_dt(now_ms).hour


def _is_night(now_ms: int) -> bool:
    h = _local_hour(now_ms)
    return h >= _EVENING_HOUR or h < _DAWN_HOUR


def _is_morning(now_ms: int) -> bool:
    """The sunrise-less end: an hour past nominal dawn, through the day.
    The card can only open at night, so any daytime hour from 07:00 on
    means the night it was opened for is over."""
    h = _local_hour(now_ms)
    return _DAWN_HOUR + 1 <= h < _EVENING_HOUR


def _night_date(now_ms: int) -> _dt.date:
    """The calendar date whose early hours this night runs into — the
    date the forecast archive files the night's low under."""
    local = ls.local_dt(now_ms)
    if local.hour >= _EVENING_HOUR:
        return local.date() + _dt.timedelta(days=1)
    return local.date()


def _coords(device: dict[str, Any],
            devices: list[dict[str, Any]]) -> tuple[float, float] | None:
    """The station's own coordinates, else the server's forecast station
    (one sky per server)."""
    from . import forecast_snapshots as fs
    own = fs._coords([device])
    if own is not None:
        return own
    return fs._coords(devices)


def _next_sunrise_ms(coords: tuple[float, float] | None,
                     now_ms: int) -> int | None:
    if coords is None:
        return None
    from . import almanac
    tz = ls.tz()
    local = ls.local_dt(now_ms)
    for offset in (0, 1, 2):
        day = local.date() + _dt.timedelta(days=offset)
        try:
            rise = almanac.sunrise(coords[0], coords[1], day, tz)
        except Exception:
            return None
        if rise is None:
            continue
        rise_ms = int(rise.timestamp() * 1000)
        if rise_ms > now_ms:
            return rise_ms
    return None


def _falling(hist: list, tempf: float, now_ms: int) -> bool:
    if not hist:
        return False
    oldest_ms, oldest = hist[0][0], ls.num(hist[0][1])
    if oldest is None or now_ms - oldest_ms < _MIN_HISTORY_MS:
        return False
    return tempf <= oldest - _FALL_F


async def _end(st: dict[str, Any], name: str, now_ms: int, *,
               tempf: float | None, reason: str) -> None:
    from . import apns
    min_f = ls.num(st.get("minF"))
    cur = tempf if tempf is not None else ls.num(st.get("lastTempf"))
    if cur is None:
        cur = min_f if min_f is not None else _WATCH_F
    if min_f is None:
        min_f = cur
    state = _content_state(
        tempf=cur, low_f=ls.num(st.get("lowF")),
        sunrise_ms=st.get("sunriseMs"), opened_ms=int(st["openedMs"]),
        froze=bool(st.get("froze")), min_f=min_f, ended=True)
    payload = apns.build_live_activity_update(
        state, now_s=now_ms // 1000, event="end",
        dismiss_s=(now_ms + _END_LINGER_MS) // 1000)
    verdict = ("it froze" if st.get("froze")
               else f"the low was {min_f:.0f}°F")
    await apns.send_live_activity_update(
        ACTIVITY, payload, "Freeze night over", f"{name}: {verdict}")
    log.info("freeze card closed for %s (%s, min %.1fF)", name, reason, min_f)


async def check(cfg, devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point, heat_watch's sibling."""
    if not getattr(cfg, "freeze_live_activity", True):
        return
    from . import apns
    from . import forecast_snapshots as fs
    for d in devices:
        if db.is_air_monitor_device(d):
            continue
        last = d.get("lastData") or {}
        tempf = ls.num(last.get("tempf"))
        mac = d["mac"]
        name = d.get("name") or mac
        key = _KV_PREFIX + mac
        st = await ls.load(key)
        if tempf is None and not st:
            continue                      # no thermometer — no opinion
        st_before = dict(st)
        stale = ls.is_stale(d, now_ms)

        hist = ls.prune(st.get("hist"), now_ms, _HISTORY_MS)
        falling = (tempf is not None and not stale
                   and _falling(hist, tempf, now_ms))
        if tempf is not None and not stale:
            hist.append([now_ms, tempf])
        st["hist"] = hist

        try:
            opened = st.get("openedMs")
            if opened is None:
                if stale or tempf is None or not _is_night(now_ms):
                    if st != st_before:
                        await ls.save(key, st)
                    continue
                low = await fs.latest_low_f(
                    _FORECAST_PROVIDER, _night_date(now_ms).isoformat())
                by_forecast = low is not None and low <= _FORECAST_LOW_F
                by_station = tempf <= _WATCH_F and falling
                if not (by_forecast or by_station):
                    if st != st_before:
                        await ls.save(key, st)
                    continue
                sunrise_ms = _next_sunrise_ms(_coords(d, devices), now_ms)
                froze = tempf <= _FREEZE_F
                state = _content_state(
                    tempf=tempf, low_f=low, sunrise_ms=sunrise_ms,
                    opened_ms=now_ms, froze=froze, min_f=tempf, ended=False)
                title = "Freeze tonight" if by_forecast else "Freeze watch"
                body = (f"{name}: {tempf:.0f}°F now"
                        + (f", low of {low:.0f}°F forecast" if low is not None
                           else ", falling"))
                payload = apns.build_live_activity_start(
                    ATTRS_TYPE, {"station": name, "mac": mac}, state,
                    title, body, now_s=now_ms // 1000,
                    stale_s=(now_ms + 45 * 60_000) // 1000,
                    dismiss_s=(now_ms + 16 * 3_600_000) // 1000)
                res = await apns.send_live_activity_start(
                    payload, title, body, activity=ACTIVITY)
                if res.get("sent"):
                    st.update({"openedMs": now_ms, "lastPushMs": now_ms,
                               "lowF": low, "sunriseMs": sunrise_ms,
                               "froze": froze, "minF": tempf,
                               "lastTempf": tempf, "aboveSinceMs": None})
                    log.info("freeze card opened for %s at %.1fF", name, tempf)
                await ls.save(key, st)
                continue

            # ── open episode ──
            opened = int(opened)
            if stale:
                await _end(st, name, now_ms, tempf=tempf, reason="stale")
                await ls.save(key, {"hist": hist})
                continue
            sunrise_ms = st.get("sunriseMs")
            if sunrise_ms is not None:
                day_over = now_ms >= int(sunrise_ms) + _AFTER_SUNRISE_MS
            else:
                # No coordinates anywhere: no sunrise on the wire, so the
                # clock ends the night instead.
                day_over = _is_morning(now_ms)
            if day_over:
                await _end(st, name, now_ms, tempf=tempf,
                           reason="sunrise" if sunrise_ms is not None else "morning")
                await ls.save(key, {"hist": hist})
                continue
            if tempf is None:
                if st != st_before:
                    await ls.save(key, st)
                continue
            if tempf > _WATCH_F:
                if st.get("aboveSinceMs") is None:
                    st["aboveSinceMs"] = now_ms
            else:
                st["aboveSinceMs"] = None
            above_since = st.get("aboveSinceMs")
            if above_since is not None and now_ms - int(above_since) >= _ABOVE_MS:
                await _end(st, name, now_ms, tempf=tempf, reason="warmed")
                await ls.save(key, {"hist": hist})
                continue
            was_frozen = bool(st.get("froze"))
            froze = was_frozen or tempf <= _FREEZE_F
            st["froze"] = froze
            min_f = ls.num(st.get("minF"))
            min_f = tempf if min_f is None else min(min_f, tempf)
            st["minF"] = min_f
            st["lastTempf"] = tempf
            last_push = int(st.get("lastPushMs") or 0)
            if now_ms - last_push >= _MIN_PUSH_GAP_MS or froze != was_frozen:
                state = _content_state(
                    tempf=tempf, low_f=ls.num(st.get("lowF")),
                    sunrise_ms=sunrise_ms, opened_ms=opened,
                    froze=froze, min_f=min_f, ended=False)
                payload = apns.build_live_activity_update(
                    state, now_s=now_ms // 1000,
                    stale_s=(now_ms + 45 * 60_000) // 1000)
                await apns.send_live_activity_update(
                    ACTIVITY, payload, "Freeze watch", name)
                st["lastPushMs"] = now_ms
            if st != st_before:
                await ls.save(key, st)
        except Exception:
            log.exception("freeze live activity failed for %s", mac)
