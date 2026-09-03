"""The Zambretti daily ledger (2.0): one slide-rule call per station per
local day, measured ONCE and never revised.

`barometer_says` (app.stories) sets the Negretti & Zambra verdict beside
the morning's numerical forecast and refuses to score them, because
scoring needs a SEASON of calls matched to what happened. That season can
only start accumulating once the call is snapshotted at the time it was
made: thinning erases intra-day pressure, so the reading and its
three-hour trend cannot be reconstructed later. This module is the
snapshot. The scorecard producer that pays the footnote's promissory note
("1920 vs 2026") reads `list_calls` when there is enough here to be honest.

Rules, each load-bearing:
- ONE call per (station, local day), taken at the first monitor tick at or
  after 09:00 station-local. INSERT OR IGNORE: a later tick the same day
  cannot revise it, matching the storm-close "measured once" pattern.
- The call is computed by `compute_call`, the SAME helper `barometer_says`
  renders from, so the ledger and the card cannot drift apart.
- Absent is absent: a day with no sea-level pressure, no reading three
  hours back, or a station that has not reported this morning writes
  NOTHING. There is no "steady by default".
- A fresh reading only. The 09:00 call must be about 09:00: a station that
  went quiet at 01:00 does not get its midnight barometer logged as a
  morning forecast.
- The window closes at noon. A server that was down all morning does not
  file a "09:00 call" from the afternoon barometer.

⚠️ TABLE DDL LIVES HERE FOR THIS RELEASE ONLY. `app/db.py` was frozen
under a concurrent edit when this shipped, so the table is created lazily
on first use through `db.connect()`. After this release the CREATE TABLE
below should move into db.py's schema block beside `forecast_snapshots`,
and `_ensure_table` should go away.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("zasder.zambretti")

# The barometer's own three-hour window, the one `pressure_tendency_code`
# and the whole WMO tendency convention are defined on. Not a tunable: a
# Zambretti read against a two-hour or six-hour trend is a different
# instrument giving a different answer.
TREND_MS = 3 * 3_600_000

# Station-local hour the daily call is taken at, and how long after it the
# ledger keeps trying before giving the day up. The first tick at or after
# 09:00 that has a fresh reading and a trend files the row; a station that
# is still catching up at 10:30 gets a slightly late call, a server that
# was dark until 14:00 gets no call for that day at all.
RECORD_HOUR = 9
RECORD_WINDOW_MS = 3 * 3_600_000

# How old the newest observation may be for the call to be about NOW. An
# hour is generous for a station on a five-minute cadence and still rules
# out a barometer that stopped reporting before dawn.
FRESH_MS = 60 * 60_000

PROVIDER = "zambretti"

_DDL = """
CREATE TABLE IF NOT EXISTS zambretti_calls (
    mac       TEXT NOT NULL,
    day       TEXT NOT NULL,      -- station-local YYYY-MM-DD
    issued_ms INTEGER NOT NULL,   -- the observation the call was read from
    slp_inhg  REAL NOT NULL,      -- sea-level pressure at issue
    trend     TEXT NOT NULL,      -- rising | steady | falling
    call      TEXT NOT NULL,      -- the slide rule's sentence
    PRIMARY KEY (mac, day)
)
"""


@dataclass(frozen=True)
class Call:
    """One slide-rule reading, with every input it was computed from.

    `slp_inhg` is the SEA-LEVEL pressure (Zambretti's constants are defined
    on it); `delta_inhg` is the three-hour change the trend word came from;
    `code` is the WMO tendency code that change maps to.
    """
    obs_ms: int
    slp_inhg: float
    past_inhg: float
    delta_inhg: float
    code: int
    trend: str
    says: str


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


async def compute_call(mac: str, obs: dict[str, Any] | None) -> Call | None:
    """The slide rule's verdict for one observation, or None.

    SHARED by `stories.barometer_says` and `record_today`: the card and the
    ledger must read the same barometer the same way. None when the row
    carries no sea-level pressure or timestamp, when there is no reading
    three hours back to take a trend from (Zambretti is a function OF the
    trend; defaulting an unknown trend to "steady" would invent the input),
    or when the tendency cannot be coded.
    """
    from . import db as dbmod, derived
    if not obs:
        return None
    slp = _num(obs.get("baromrelin"))
    obs_ms = _num(obs.get("dateutc"))
    if slp is None or obs_ms is None:
        return None
    past = await dbmod.value_at_or_before(
        mac, "baromrelin", int(obs_ms) - TREND_MS, max_age_ms=TREND_MS)
    if past is None:
        return None
    delta = slp - past
    tend = derived.pressure_tendency_code(delta)
    if tend is None:
        return None
    code, word = tend
    says = derived.zambretti(slp * 33.8639, word)
    if says is None:
        return None
    return Call(obs_ms=int(obs_ms), slp_inhg=slp, past_inhg=past,
                delta_inhg=delta, code=code, trend=word, says=says)


async def _ensure_table(conn) -> None:
    await conn.execute(_DDL)


def _local(ms: int, tz) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)


async def record_today(mac: str, now_ms: int, tz) -> dict[str, Any] | None:
    """File today's call if this is the first tick at or after 09:00
    station-local and nothing has been filed for today yet.

    Returns the row written, or None when nothing was (too early, window
    closed, already filed, station quiet, no trend). `tz` is the station's
    zone; `now_ms` is passed in rather than read so a test can put the
    clock exactly where it wants it and the rollover this exists to handle
    is reachable on purpose (the `db._now_local` lesson).
    """
    local_now = _local(now_ms, tz)
    open_at = local_now.replace(hour=RECORD_HOUR, minute=0, second=0,
                                microsecond=0)
    open_ms = int(open_at.timestamp() * 1000)
    if now_ms < open_ms or now_ms >= open_ms + RECORD_WINDOW_MS:
        return None
    day = local_now.date().isoformat()

    from . import db as dbmod
    async with dbmod.connect() as conn:
        await _ensure_table(conn)
        have = await (await conn.execute(
            "SELECT 1 FROM zambretti_calls WHERE mac = ? AND day = ?",
            (mac, day))).fetchone()
    if have:
        return None

    obs = await dbmod.latest_observation(mac)
    if not obs:
        return None
    obs_ms = _num(obs.get("dateutc"))
    if obs_ms is None or obs_ms < now_ms - FRESH_MS or obs_ms > now_ms + FRESH_MS:
        return None
    if _local(int(obs_ms), tz).date().isoformat() != day:
        return None
    call = await compute_call(mac, obs)
    if call is None:
        return None

    row = {"mac": mac, "day": day, "issued_ms": call.obs_ms,
           "slp_inhg": round(call.slp_inhg, 3), "trend": call.trend,
           "call": call.says}
    async with dbmod.connect() as conn:
        await _ensure_table(conn)
        cur = await conn.execute(
            "INSERT OR IGNORE INTO zambretti_calls "
            "(mac, day, issued_ms, slp_inhg, trend, call) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mac, day, row["issued_ms"], row["slp_inhg"], row["trend"],
             row["call"]))
        await conn.commit()
        if cur.rowcount == 0:
            # Lost a race with another tick: the first writer's row stands.
            return None
    log.debug("zambretti ledger %s %s: %s (%s)", mac, day, call.says,
              call.trend)
    return row


async def list_calls(mac: str, days: int) -> list[dict[str, Any]]:
    """The newest `days` calls for a station, oldest first. The reader the
    future scorecard producer joins against `forecast_snapshots` and the
    daily rollups; nothing renders from it yet."""
    from . import db as dbmod
    async with dbmod.connect() as conn:
        await _ensure_table(conn)
        rows = await (await conn.execute(
            "SELECT mac, day, issued_ms, slp_inhg, trend, call "
            "FROM zambretti_calls WHERE mac = ? "
            "ORDER BY day DESC LIMIT ?", (mac, max(0, int(days))))).fetchall()
    return [dict(r) for r in reversed(rows)]


def _station_tz():
    from zoneinfo import ZoneInfo
    from .config import settings
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        return timezone.utc


async def check(devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point. Outside the morning window this is a
    clock comparison and nothing else; inside it, one indexed SELECT per
    weather station until the day's row exists. A failure on one station
    is logged and never reaches the next, or the tick."""
    tz = _station_tz()
    from . import db as dbmod
    for d in devices:
        if dbmod.is_air_monitor_device(d):
            continue
        mac = d.get("mac")
        if not mac:
            continue
        try:
            await record_today(mac, now_ms, tz)
        except Exception:
            log.exception("zambretti ledger failed for %s", mac)
