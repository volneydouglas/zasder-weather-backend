"""Crawlspace freeze watch (2.8, Doren 10-27 and 10-31).

Pipes under a house freeze after the OUTSIDE has been cold for hours, so
the warning is a forecast, delivered while there is still time to act:
at sunset, when the hourly forecast for the coming night holds at or
below `threshold_f` (default 27 °F) for at least `hours` consecutive
hours (default 6), one push and email:

    Crawlspace Freeze Monitor
    At or below 27°F could reach 6 sustained hours by Saturday 6:00 AM.

One notice per qualifying night, never re-sent the same night. A cold
spell of three nights is three notices, one each evening, because each
night is its own decision about the heat.

An optional crawlspace sensor (any device: a Govee thermometer, a probe)
adds two live levels on its own temperature: at or below `sensor_watch_f`
(36 °F, "watch" tier, held by quiet hours) and at or below
`sensor_urgent_f` (32 °F, "warning" tier, breaks through quiet hours).
Each fires once and re-arms when the sensor has read 2 °F above its level.

Hourly temperatures come from Open-Meteo (TWC has no hourly product on a
free WU key, see forecast_hourly). When the owner's forecast source is
TWC, the night's hourly curve is shifted DOWN so its lowest hour matches
TWC's low for the next morning: Open-Meteo ran 4 to 9 °F warm on Doren's
October freeze nights where TWC was within a degree. Never shifted up.

Every constant is °F, the storage unit. Off unless enabled.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

from . import db
from . import live_state as ls

log = logging.getLogger("crawlspace-watch")

KV_CONFIG = "crawlspace_watch.config"
KV_STATE = "crawlspace_watch.state"
LOOKAHEAD_H = 36
HOUR_MS = 3_600_000
# The sunset check runs from sunset until this long after it (a restart
# at dusk must not skip the night), never before sunset.
SUNSET_WINDOW_MS = 3 * 3_600_000
# A sensor level re-arms after reading this far above it.
REARM_F = 2.0
# A sensor reading older than this is not acted on: an offline sensor's
# last value is not the crawlspace now (device-down covers the outage).
SENSOR_STALE_MS = 3_600_000
TITLE = "Crawlspace Freeze Monitor"


@dataclass
class Config:
    enabled: bool = False
    threshold_f: float = 27.0
    hours: int = 6
    sensor_mac: str | None = None
    sensor_watch_f: float = 36.0
    sensor_urgent_f: float = 32.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Config":
        c = cls()
        if not isinstance(d, dict):
            return c
        if isinstance(d.get("enabled"), bool):
            c.enabled = d["enabled"]
        for key, lo, hi in (("threshold_f", -40.0, 50.0),
                            ("sensor_watch_f", -40.0, 60.0),
                            ("sensor_urgent_f", -40.0, 60.0)):
            v = ls.num(d.get(key))
            if v is not None and lo <= v <= hi:
                setattr(c, key, float(v))
        h = d.get("hours")
        if isinstance(h, int) and not isinstance(h, bool) and 1 <= h <= 24:
            c.hours = h
        mac = d.get("sensor_mac")
        c.sensor_mac = mac.strip().upper() if isinstance(mac, str) and mac.strip() else None
        return c


async def load_config() -> Config:
    raw = await db.get_kv(KV_CONFIG)
    try:
        return Config.from_dict(json.loads(raw) if raw else None)
    except (ValueError, TypeError):
        return Config()


async def save_config(c: Config) -> None:
    await db.set_kv(KV_CONFIG, json.dumps(asdict(c)))


# ───────────────────────── pure decisions ─────────────────────────

def sustained_run(times_ms: list[int], temps: list[float | None],
                  threshold_f: float, hours: int) -> int | None:
    """The time of the hour at which the forecast has held at or below
    `threshold_f` for `hours` consecutive hours, or None. A missing hour
    breaks the run: absent is not cold. So does a skipped timestamp: the
    provider's hours are not filled in, and two cold stretches either side
    of a missing hour are not one sustained run."""
    run = 0
    prev: int | None = None
    for t, v in zip(times_ms, temps):
        if prev is not None and t - prev != HOUR_MS:
            run = 0
        prev = t
        if v is not None and v <= threshold_f:
            run += 1
            if run >= hours:
                return t
        else:
            run = 0
    return None


def complete_hours(times_ms: list[int], temps: list[float | None],
                   start_ms: int, hours: int) -> bool:
    """Every hour of the window present and known, from `start_ms` (the
    current hour, where forecast_hourly starts) for `hours`: only then can
    a forecast with no cold run close the night. A gap, a None, a skipped
    hour or a truncated response could hide one (CodeRabbit, PR #130); the
    cost of not closing is one more cached look inside the window."""
    return (hours >= 1 and len(times_ms) == hours
            and len(times_ms) == len(temps)
            and times_ms[0] == start_ms
            and all(v is not None for v in temps)
            and all(b - a == HOUR_MS for a, b in zip(times_ms, times_ms[1:])))


def anchored(temps: list[float | None], low_f: float | None,
             times_ms: list[int] | None = None,
             until_ms: int | None = None) -> list[float | None]:
    """Shift the curve down so its lowest hour equals `low_f` (TWC's low).
    Never up: a warmer second opinion must not hide a cold night.

    TWC's low describes one morning, so with `times_ms` and `until_ms` the
    curve's low is read only from the hours before `until_ms` (noon after
    the night): a colder day-3 dawn at the end of the 36 hours would
    otherwise shrink the shift, or cancel it, on exactly the night TWC
    calls colder (CodeRabbit, PR #146). The shift moves the whole curve."""
    if times_ms is not None and until_ms is not None:
        vals = [v for t, v in zip(times_ms, temps) if v is not None and t < until_ms]
    else:
        vals = [v for v in temps if v is not None]
    if low_f is None or not vals:
        return temps
    shift = low_f - min(vals)
    if shift >= 0:
        return temps
    return [None if v is None else round(v + shift, 1) for v in temps]


def message(threshold_f: float, hours: int, by_ms: int,
            tz: _dt.tzinfo) -> tuple[str, str]:
    when = _dt.datetime.fromtimestamp(by_ms / 1000, tz)
    clock = when.strftime("%-I:%M %p")
    return (TITLE,
            f"At or below {threshold_f:.0f}°F could reach {hours} sustained "
            f"hours by {when:%A} {clock}.")


def in_sunset_window(now_ms: int, sunset_ms: int | None) -> bool:
    if sunset_ms is None:
        return False
    return sunset_ms <= now_ms < sunset_ms + SUNSET_WINDOW_MS


def sensor_step(tempf: float | None, level_f: float, armed: bool) -> tuple[bool, bool]:
    """(fire now, armed after) for one live level."""
    if tempf is None:
        return False, armed
    if armed and tempf <= level_f:
        return True, False
    if not armed and tempf >= level_f + REARM_F:
        return False, True
    return False, armed


# ───────────────────────── the tick ─────────────────────────

async def _sunset_ms(coords: tuple[float, float], day: _dt.date) -> int | None:
    from . import almanac
    try:
        s = almanac.sunset(coords[0], coords[1], day, ls.tz())
    except Exception:
        return None
    return int(s.timestamp() * 1000) if s else None


async def _twc_low(cfg, coords: tuple[float, float], day: _dt.date) -> float | None:
    """TWC's forecast low for `day` when the owner chose TWC, else None."""
    from . import outlook as ol
    if getattr(cfg, "outlook_source", None) != ol.SOURCE_TWC:
        return None
    from .main import effective_wu_key
    key = await effective_wu_key()
    if not key:
        return None
    from .config import settings
    daily, _, used, _ = await ol.fetch_daily(
        coords[0], coords[1], source=ol.SOURCE_TWC, wu_key=key,
        tz_name=settings.timezone)
    if used != ol.SOURCE_TWC:
        return None
    times = daily.get("time") or []
    for i, t in enumerate(times):
        if isinstance(t, str) and t[:10] == day.isoformat():
            lows = daily.get("temperature_2m_min") or []
            return ls.num(lows[i]) if i < len(lows) else None
    return None


async def check(cfg, devices: list[dict[str, Any]], now_ms: int, deliver) -> None:
    """One monitor tick. `deliver` is alerts._deliver (injected so the
    tests drive it without a channel)."""
    c = await load_config()
    if not c.enabled:
        return
    raw = await db.get_kv(KV_STATE)
    try:
        st = json.loads(raw) if raw else {}
    except ValueError:
        st = {}
    before = dict(st)
    try:
        await _forecast_notice(c, cfg, st, devices, now_ms, deliver)
    except Exception:
        log.exception("crawlspace forecast check failed")
    try:
        await _sensor_levels(c, cfg, st, devices, now_ms, deliver)
    except Exception:
        log.exception("crawlspace sensor check failed")
    if st != before:
        await db.set_kv(KV_STATE, json.dumps(st))


async def _forecast_notice(c: Config, cfg, st: dict[str, Any],
                           devices: list[dict[str, Any]], now_ms: int, deliver) -> None:
    from . import forecast_hourly
    from . import forecast_snapshots as fs
    coords = fs.coords(devices)
    if coords is None:
        return
    today = ls.local_dt(now_ms).date()
    if st.get("noticeNight") == today.isoformat():
        return
    if not in_sunset_window(now_ms, await _sunset_ms(coords, today)):
        return
    fc = await forecast_hourly.fetch(coords[0], coords[1], LOOKAHEAD_H, now_ms=now_ms)
    times = fc.get("times_ms") or []
    temps = (fc.get("series") or {}).get("tempf") or []
    if not times or not any(v is not None for v in temps):
        # No usable hours is not a warm night: ask again next tick
        # (CodeRabbit, PR #130).
        return
    tomorrow = today + _dt.timedelta(days=1)
    low_known = True
    try:
        low = await _twc_low(cfg, coords, tomorrow)
    except Exception:
        # The hourly curve still decides tonight: a failed second opinion
        # must not cost the notice. A warm result stays open so a later
        # tick can ask TWC again (CodeRabbit, PR #146).
        log.warning("crawlspace: TWC low lookup failed; using the hourly curve",
                    exc_info=True)
        low, low_known = None, False
    noon = int(_dt.datetime.combine(tomorrow, _dt.time(12), tzinfo=ls.tz())
               .timestamp() * 1000)
    temps = anchored(temps, low, times, noon)
    by_ms = sustained_run(times, temps, c.threshold_f, c.hours)
    if by_ms is None:
        # Asked once per evening: a warm night is decided, but only when
        # every hour is known. A gap could hide the cold run, so the night
        # stays open for the rest of the window (CodeRabbit, PR #130).
        if low_known and complete_hours(times, temps, now_ms - now_ms % HOUR_MS,
                                        LOOKAHEAD_H):
            st["noticeNight"] = today.isoformat()
        return
    title, body = message(c.threshold_f, c.hours, by_ms, ls.tz())
    handled = await deliver(cfg, f"[Zasder Weather] {title}", body, title, body,
                            email_ok=getattr(cfg, "email_scope", "all") == "all",
                            kind="crawlspace", severity="watch")
    if handled:
        # A failed send is retried on the next tick inside the window.
        st["noticeNight"] = today.isoformat()
        log.info("crawlspace notice sent: %s", body)


async def _sensor_levels(c: Config, cfg, st: dict[str, Any],
                         devices: list[dict[str, Any]], now_ms: int, deliver) -> None:
    if not c.sensor_mac:
        return
    dev = next((d for d in devices if str(d.get("mac", "")).upper() == c.sensor_mac), None)
    if dev is None:
        return
    last = dev.get("lastData") or {}
    obs_ms = ls.num(last.get("dateutc"))
    if obs_ms is None or now_ms - obs_ms > SENSOR_STALE_MS:
        return
    tempf = ls.num(last.get("tempinf"))
    if tempf is None:
        tempf = ls.num(last.get("tempf"))
    name = dev.get("name") or c.sensor_mac
    for key, level, severity in (("urgentArmed", c.sensor_urgent_f, "warning"),
                                 ("watchArmed", c.sensor_watch_f, "watch")):
        fire, armed = sensor_step(tempf, level, bool(st.get(key, True)))
        if fire:
            title = f"{name}: {tempf:.0f}°F"
            body = (f"The crawlspace sensor reads {tempf:.1f}°F, at or below "
                    f"{level:.0f}°F. Check the pipes and the heat.")
            handled = await deliver(cfg, f"[Zasder Weather] {title}", body, title, body,
                                    email_ok=getattr(cfg, "email_scope", "all") == "all",
                                    kind="crawlspace", mac=dev.get("mac"), severity=severity)
            if not handled:
                # Still armed: the next tick tries again.
                st[key] = True
                break
        st[key] = armed
        if fire:
            if severity == "warning":
                # The colder level said it; the milder one stays quiet.
                st["watchArmed"] = False
                break
