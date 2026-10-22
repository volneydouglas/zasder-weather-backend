"""Freeze Night Live Activity (2.3, item 5): a Lock Screen / Dynamic
Island card through a night that may freeze — the current temperature,
the forecast low, the next sunrise, whether it has frozen yet, and the
lowest reading so far.

Opens in the evening (local 18:00 until dawn) when either the forecast
low for the night is at or below 34 °F, or a station at the place reads
36 °F or colder AND falling over the last hour (judged against the tick
history this module keeps in server_kv). Never on a stale station, never
without a temperature — absent is not zero.

2.7.1 (Doren, 2026-10-19: "if I get the alert at 3am that it might frost
at 6am, I'm not getting out of bed to protect our plants"):
  · The low is the OWNER'S forecast (`outlook_source`, the one the
    outlook and the morning report read), from the forecast_snapshots
    archive. Open-Meteo answers only when that source filed no call for
    the night in the last day. 2.7.0 read Open-Meteo whatever the
    setting, and on his two freeze nights it said 36.2 and 41.3 while
    TWC, his source, said 31 and 32 (the station read 30.6 and 30.8).
  · Frost forms on plants with the screen-level air a few degrees above
    freezing on a clear calm night, so a low of 34 °F or under opens the
    card ("Frost possible tonight" on the opening alert), and 32 °F or
    under keeps "Freeze tonight".
  · One card per PLACE (app/places.py), not per station. The place's
    first fresh station leads it (the smart alerts' rule); the station
    trigger hears every member, and the member that crossed leads. If
    the lead goes quiet a fresh sibling carries the card; it ends as
    stale only when the whole place has.

`froze` latches once the station reads 32 °F or below; `minF` is the
lowest reading of the episode. Pushes go out every 15 minutes, or at
once when `froze` flips. The card ends an hour after sunrise (at 07:00
local when no coordinates give a sunrise — a card with no daytime end
once ran 17 hours), or once the station has read above 36 °F for an
hour after reading 36 °F or under, or when every station at the place
has gone stale.

Quiet hours: same footing as storm_watch — never held. Switch:
`freeze_live_activity` (NULL = on). Every constant here is °F, the
storage unit.
"""
from __future__ import annotations

import datetime as _dt
import json
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
# The fallback forecast, and how old the owner's own source's newest call
# for tonight may be before it no longer counts (2.7.1). The archive files
# every six hours, so a day is four missed beats: a key that stopped
# working, not a slow upstream.
_FORECAST_FALLBACK = "open-meteo"
_FORECAST_MAX_AGE_MS = 24 * 3_600_000
# The episode's own keys: what a 2.7.0 per-station state carries into its
# place on the upgrade (`_fold`).
_EPISODE_KEYS = ("openedMs", "lastPushMs", "lowF", "lowSource",
                 "sunriseMs", "froze", "minF", "lastTempf", "aboveSinceMs")


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
    own = fs.coords([device])
    if own is not None:
        return own
    return fs.coords(devices)


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


async def _tonight_low(cfg, night_iso: str,
                       now_ms: int) -> tuple[float | None, str | None]:
    """(tonight's forecast low °F, the source it came from), or
    (None, None). The owner's outlook source first: its newest call for
    the night, if one was issued within _FORECAST_MAX_AGE_MS. Open-Meteo
    otherwise, any age, as 2.7.0 read it."""
    from . import forecast_snapshots as fs
    from . import outlook as ol
    chosen = getattr(cfg, "outlook_source", None)
    if chosen not in ol.SOURCES:
        chosen = _FORECAST_FALLBACK
    if chosen != _FORECAST_FALLBACK:
        low = await fs.latest_low_f(
            chosen, night_iso, issued_since_ms=now_ms - _FORECAST_MAX_AGE_MS)
        if low is not None:
            return low, chosen
    low = await fs.latest_low_f(_FORECAST_FALLBACK, night_iso)
    return (low, _FORECAST_FALLBACK) if low is not None else (None, None)


def _band_title(low: float | None) -> str | None:
    """The opening alert's title for a forecast low, or None when the low
    opens nothing: the report line's bands (outlook.frost_band, 34 °F and
    32 °F on the whole degree the body prints), so the card and the
    evening outlook agree about the same night. The card's own headline
    is the app's ("Freeze tonight" until it freezes): the shipped apps
    have no frost stage, so only this server-written alert can say
    "Frost possible" today (a stage for the card itself is 2.8 work)."""
    from . import outlook as ol
    return {"freeze": "Freeze tonight",
            "frost": "Frost possible tonight"}.get(ol.frost_band(low))


async def _fold(st: dict[str, Any], members: list[dict[str, Any]]
                ) -> tuple[dict[str, Any], list[str]]:
    """The 2.7.1 upgrade, run every tick so a regrouping is handled the
    same way: 2.7.0 kept one state per station (`freeze_live.<mac>`, with
    its history under "hist"); a place keeps one under its first
    station's key, with each member's history under "hists".

    The first station's own "hist" moves into "hists". Each other
    member's state is folded in: its history, and its open episode when
    the place has none yet (the first such member in place order, the
    lead rule; it keeps leading, since the card on the phone names it).
    A second open episode is dropped without an end beat: the phone's
    cards all take every freeze update (the note in `_check_place`), so it
    follows the place's episode and ends with it. Returns the state and
    the member keys to clear once it is saved."""
    anchor = members[0]["mac"]
    hists = st.get("hists") if isinstance(st.get("hists"), dict) else {}
    old = st.pop("hist", None)
    if isinstance(old, list):
        hists.setdefault(anchor, old)
    if st.get("openedMs") is not None and not st.get("leadMac"):
        st["leadMac"] = anchor
    folded: list[str] = []
    for m in members[1:]:
        key = _KV_PREFIX + m["mac"]
        other = await ls.load(key)
        if not other:
            continue
        folded.append(key)
        if isinstance(other.get("hist"), list):
            hists.setdefault(m["mac"], other["hist"])
        if isinstance(other.get("hists"), dict):
            for mac, h in other["hists"].items():
                hists.setdefault(mac, h)
        # A former first station's "warmed" mark (the newest night wins;
        # ISO dates compare as strings).
        ended = other.get("endedNight")
        if isinstance(ended, str) and ended > str(st.get("endedNight") or ""):
            st["endedNight"] = ended
        if other.get("openedMs") is None:
            continue
        if st.get("openedMs") is None:
            for k in _EPISODE_KEYS:
                st[k] = other.get(k)
            st["leadMac"] = other.get("leadMac") or m["mac"]
            log.info("freeze card of %s carries on as its place's card",
                     m.get("name") or m["mac"])
        else:
            log.info("freeze card of %s folds into its place's card",
                     m.get("name") or m["mac"])
    st["hists"] = hists
    return st, folded


def _name(d: dict[str, Any]) -> str:
    return d.get("name") or d["mac"]


async def check(cfg, devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point, heat_watch's sibling. One card per
    place (2.7.1)."""
    if not getattr(cfg, "freeze_live_activity", True):
        return
    from . import places
    weather = [d for d in devices if not db.is_air_monitor_device(d)]
    for members in places.group(weather):
        try:
            await _check_place(cfg, members, devices, now_ms)
        except Exception:
            log.exception("freeze live activity failed for %s",
                          members[0]["mac"])


async def _check_place(cfg, members: list[dict[str, Any]],
                       devices: list[dict[str, Any]], now_ms: int) -> None:
    from . import apns
    key = _KV_PREFIX + members[0]["mac"]
    st = await ls.load(key)
    before = json.dumps(st, sort_keys=True)
    st, folded = await _fold(st, members)

    async def save(state: dict[str, Any]) -> None:
        # The place first, then the folded keys: a crash between the two
        # folds the same states again next tick, which changes nothing.
        if json.dumps(state, sort_keys=True) != before:
            await ls.save(key, state)
        for k in folded:
            await ls.save(k, None)

    # Each member's hour of readings; `fresh` is every member with a
    # temperature on a live station, in place order, and whether it is
    # falling on its own history.
    hists: dict[str, list] = {}
    fresh: list[tuple[dict[str, Any], float, bool]] = []
    for m in members:
        tempf = ls.num((m.get("lastData") or {}).get("tempf"))
        live = not ls.is_stale(m, now_ms)
        hist = ls.prune(st["hists"].get(m["mac"]), now_ms, _HISTORY_MS)
        if tempf is not None and live:
            fresh.append((m, tempf, _falling(hist, tempf, now_ms)))
            hist.append([now_ms, tempf])
        if hist:
            hists[m["mac"]] = hist
    st["hists"] = hists
    if not fresh and st.get("openedMs") is None and not hists:
        # No thermometer anywhere here: no opinion, and nothing to keep.
        if folded:
            await save({})
        return

    opened = st.get("openedMs")
    if opened is None:
        if not fresh or not _is_night(now_ms):
            await save(st)
            return
        night = _night_date(now_ms).isoformat()
        low, source = await _tonight_low(cfg, night, now_ms)
        # A night whose card already ended as "warmed" is not reopened by
        # the same forecast (CodeRabbit, PR #117): the next tick did, with a
        # new alert. The station trigger can still open it if the air cools
        # and falls again. A stale end leaves no mark, so a place back from
        # a network drop gets its card back.
        title = _band_title(low) if st.get("endedNight") != night else None
        if title is not None:
            lead, tempf = fresh[0][0], fresh[0][1]
        else:
            falling = [(m, t) for m, t, f in fresh if t <= _WATCH_F and f]
            if not falling:
                await save(st)
                return
            lead, tempf = falling[0]
            title = "Freeze watch"
        name = _name(lead)
        sunrise_ms = _next_sunrise_ms(_coords(lead, devices), now_ms)
        froze = tempf <= _FREEZE_F
        state = _content_state(
            tempf=tempf, low_f=low, sunrise_ms=sunrise_ms,
            opened_ms=now_ms, froze=froze, min_f=tempf, ended=False)
        body = (f"{name}: {tempf:.0f}°F now"
                + (f", low of {low:.0f}°F forecast" if low is not None
                   else ", falling"))
        payload = apns.build_live_activity_start(
            ATTRS_TYPE, {"station": name, "mac": lead["mac"]}, state,
            title, body, now_s=now_ms // 1000,
            stale_s=(now_ms + 45 * 60_000) // 1000,
            dismiss_s=(now_ms + 16 * 3_600_000) // 1000)
        res = await apns.send_live_activity_start(
            payload, title, body, activity=ACTIVITY)
        if res.get("sent"):
            st.update({"openedMs": now_ms, "lastPushMs": now_ms,
                       "lowF": low, "lowSource": source,
                       "sunriseMs": sunrise_ms, "froze": froze,
                       "minF": tempf, "lastTempf": tempf,
                       "aboveSinceMs": None, "leadMac": lead["mac"]})
            log.info("freeze card opened for %s at %.1fF (%s; forecast low %s)",
                     name, tempf, title,
                     f"{low:.1f}F from {source}" if low is not None
                     else "none on file")
        await save(st)
        return

    # ── open episode ──
    opened = int(opened)
    by_mac = {m["mac"]: m for m in members}
    lead = by_mac.get(st.get("leadMac") or members[0]["mac"])
    if all(ls.is_stale(m, now_ms) for m in members):
        name = _name(lead) if lead else _name(members[0])
        await _end(st, name, now_ms, tempf=None, reason="stale")
        await save({"hists": hists})
        return
    if lead is None or ls.is_stale(lead, now_ms):
        if not fresh:
            await save(st)
            return
        # The lead went quiet (or left the place); a sibling in the same
        # yard carries the card on rather than ending it at the coldest
        # hour. The card keeps the name it opened with.
        lead = fresh[0][0]
        log.info("freeze card hands over to %s", _name(lead))
        st["leadMac"] = lead["mac"]
    name = _name(lead)
    tempf = next((t for m, t, _f in fresh if m is lead), None)
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
        await save({"hists": hists})
        return
    if tempf is None:
        await save(st)
        return
    # "Warmed back up" needs the night to have been cold first (2.7.1).
    # A card the forecast opens at 18:00 starts well above 36 °F on most
    # freeze nights; counting that hour as "warmed" ended it at 19:00 and
    # the next tick opened it again, an alert every 70 minutes until the
    # air cooled. Until the episode reads 36 °F the forecast stands, and
    # sunrise ends a night that never came.
    min_so_far = ls.num(st.get("minF"))
    cold_yet = min_so_far is not None and min_so_far <= _WATCH_F
    if tempf > _WATCH_F and cold_yet:
        if st.get("aboveSinceMs") is None:
            st["aboveSinceMs"] = now_ms
    else:
        st["aboveSinceMs"] = None
    above_since = st.get("aboveSinceMs")
    if above_since is not None and now_ms - int(above_since) >= _ABOVE_MS:
        await _end(st, name, now_ms, tempf=tempf, reason="warmed")
        await save({"hists": hists,
                    "endedNight": _night_date(now_ms).isoformat()})
        return
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
        # Every freeze card on a phone takes this update: the token table
        # (db.py, live_activity_tokens) has no station column, so a card
        # per station showed whichever station pushed last (Doren's Davis
        # and Tempest, 10-18). One card per place keeps that honest.
        # Scoping update tokens to a station is 2.8 work: the app has to
        # register its card's mac with the token.
        await apns.send_live_activity_update(
            ACTIVITY, payload, "Freeze watch", name)
        st["lastPushMs"] = now_ms
    await save(st)
