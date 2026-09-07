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

# How much older than obs−3h the trend anchor may be. The call says "the
# three-hour change", so the span measured has to BE three hours: one
# missed poll of a five-minute station, or one of a fifteen-minute one, is
# slack; a second missing hour is not. Until 2.1 the ledger accepted
# anchors up to six hours old (a three-hour freshness floor on a lookup
# already three hours back) while the card declined past this slack, so
# after an outage the season's record could hold a call the card would
# have refused to print. One constant, both readers.
ANCHOR_SLACK_MS = 30 * 60_000

PROVIDER = "zambretti"

# The table's DDL lives in db.SCHEMA since 2.1 (it was here, created on
# first use, which is why 2.0 databases have it without a migration).
# _ensure_table keeps the create-on-first-use behaviour from the same
# text so a table dropped by hand comes back, and the two cannot drift.
def _ddl() -> str:
    from . import db as dbmod
    start = dbmod.SCHEMA.index("CREATE TABLE IF NOT EXISTS zambretti_calls")
    end = dbmod.SCHEMA.index(";", start) + 1
    return dbmod.SCHEMA[start:end]


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
    three hours back (within ANCHOR_SLACK_MS of it) to take a trend from
    (Zambretti is a function OF the trend; defaulting an unknown trend to
    "steady" would invent the input), or when the tendency cannot be coded.
    """
    from . import db as dbmod, derived
    if not obs:
        return None
    slp = _num(obs.get("baromrelin"))
    obs_ms = _num(obs.get("dateutc"))
    if slp is None or obs_ms is None:
        return None
    past = await dbmod.value_at_or_before(
        mac, "baromrelin", int(obs_ms) - TREND_MS, max_age_ms=ANCHOR_SLACK_MS)
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
    await conn.execute(_ddl())


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


# ───────────────────────── the scorecard (2.1) ─────────────────────────
#
# "1920 vs 2026", the promissory note in barometer_says's footnote. A
# season of calls matched to what happened, scored on the one question a
# slide rule and a numerical model both answer plainly: did it rain today?

# The slide rule's 32 sentences, sorted by whether the HEADLINE promises
# rain. The hedged ones ("Fine, possibly showers", "Fairly fine, possibly
# showers early") are filed as dry: the instrument led with "fine", and a
# scorer that read every hedge as a rain call would hand it credit for
# every shower it merely allowed for. Explicit, reviewable, and exactly
# the derived._ZAMBRETTI_TEXT vocabulary — an unknown sentence scores as
# nothing rather than as either side.
DRY_CALLS = frozenset({
    "Settled fine", "Fine weather", "Fine, becoming less settled",
    "Fine, possibly showers", "Becoming fine", "Fairly fine, improving",
    "Fairly fine, possibly showers early",
})
WET_CALLS = frozenset({
    "Fairly fine, showery later", "Showery, becoming more unsettled",
    "Unsettled, rain later", "Rain at times, worse later",
    "Rain at times, becoming very unsettled", "Very unsettled, rain",
    "Fairly fine, showers likely", "Showery, bright intervals",
    "Changeable, some rain", "Unsettled, rain at times",
    "Rain at frequent intervals", "Stormy, much rain",
    "Showery early, improving", "Changeable, mending",
    "Rather unsettled, clearing later", "Unsettled, probably improving",
    "Unsettled, short fine intervals", "Very unsettled, finer at times",
    "Stormy, possibly improving",
})

# A day counts as a rain day at or above this (inches, storage units): one
# tip of a 0.01-inch gauge. A model's chance of rain is read as a rain
# call at or above MODERN_RAIN_POP percent.
RAIN_DAY_IN = 0.01
MODERN_RAIN_POP = 50.0
# A "season": the fewest scored days the card will print a hit rate on.
# Thirty is where a coin-flip instrument stops looking like a good one
# by luck alone often enough to be embarrassing.
SCORECARD_MIN_DAYS = 30
SCORECARD_LOOKBACK_DAYS = 366


def call_expects_rain(sentence: str | None) -> bool | None:
    """True for a rain call, False for a dry one, None for a sentence the
    slide rule never says (absent is absent, not dry)."""
    if sentence in WET_CALLS:
        return True
    if sentence in DRY_CALLS:
        return False
    return None


def _day_rain_in(row: dict[str, Any]) -> float | None:
    """The day's rainfall or None: app/day_rain.py, the one rule."""
    from .day_rain import day_rain_in
    return day_rain_in(row)


@dataclass(frozen=True)
class Scorecard:
    """One season, scored. Every count is a DAY; `modern_*` is None when
    no stored forecast was issued before the morning call on any day."""
    days: int                       # days with a call AND a rain outcome
    rain_days: int
    zambretti_hits: int
    zambretti_hit_rate: float
    modern_days: int                # days that ALSO had a forecast to score
    modern_hits: int | None
    modern_hit_rate: float | None
    zambretti_hits_on_modern_days: int | None
    first_day: str
    last_day: str


async def scorecard(mac: str, forecast_provider: str,
                    days: int = SCORECARD_LOOKBACK_DAYS) -> Scorecard | None:
    """Match the season's calls to what happened, and to what the numerical
    model said that morning.

    Outcome: the station's own gauge for the SAME station-local day the
    call was filed on (daily_rollups). A day the gauge never reported is
    unscorable and dropped, never counted as dry. The model's call is the
    newest forecast for that day issued at or BEFORE the ledger's call, so
    both instruments are judged on what they knew at 09:00; a forecast
    filed later that morning would be a second look the barometer did not
    get. Days with no such forecast score the slide rule alone, and the
    model's rate is reported over ITS days only, beside the slide rule's
    rate over the same days, so the two rates are comparable.
    """
    from . import db as dbmod
    calls = await list_calls(mac, days)
    if not calls:
        return None
    day_lo, day_hi = calls[0]["day"], calls[-1]["day"]
    async with dbmod.connect() as conn:
        rain_rows = await (await conn.execute(
            "SELECT day, rain_total, yearly_min, yearly_max FROM daily_rollups "
            "WHERE mac = ? AND day BETWEEN ? AND ?",
            (mac, day_lo, day_hi))).fetchall()
        fc_rows = await (await conn.execute(
            "SELECT valid_date, issued_ms, pop FROM forecast_snapshots "
            "WHERE provider = ? AND valid_date BETWEEN ? AND ? "
            "ORDER BY issued_ms",
            (forecast_provider, day_lo, day_hi))).fetchall()
    rain_by_day = {r["day"]: _day_rain_in(dict(r)) for r in rain_rows}
    forecasts: dict[str, list[tuple[int, float | None]]] = {}
    for r in fc_rows:
        forecasts.setdefault(r["valid_date"], []).append(
            (int(r["issued_ms"]), _num(r["pop"])))

    scored = 0
    rain_days = 0
    z_hits = 0
    m_days = 0
    m_hits = 0
    z_hits_m = 0
    first = last = None
    for c in calls:
        expects = call_expects_rain(c.get("call"))
        rain = rain_by_day.get(c["day"])
        if expects is None or rain is None:
            continue
        rained = rain >= RAIN_DAY_IN
        scored += 1
        rain_days += rained
        z_right = expects == rained
        z_hits += z_right
        first = first or c["day"]
        last = c["day"]
        # The model's newest word BEFORE the call, if any.
        pop = None
        for issued, p in forecasts.get(c["day"], ()):
            if issued <= int(c["issued_ms"]) and p is not None:
                pop = p
        if pop is not None:
            m_days += 1
            m_hits += (pop >= MODERN_RAIN_POP) == rained
            z_hits_m += z_right
    if scored == 0:
        return None
    return Scorecard(
        days=scored, rain_days=rain_days, zambretti_hits=z_hits,
        zambretti_hit_rate=z_hits / scored,
        modern_days=m_days,
        modern_hits=m_hits if m_days else None,
        modern_hit_rate=(m_hits / m_days) if m_days else None,
        zambretti_hits_on_modern_days=z_hits_m if m_days else None,
        first_day=first or day_lo, last_day=last or day_hi)
