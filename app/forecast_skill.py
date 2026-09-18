"""Forecast skill (2.3): how wrong the forecast tends to be, for THIS
backyard, at every lead time.

`forecast_snapshots` has been archiving Open-Meteo's 7-day run every six
hours since 1.8 — every forecast AS ISSUED, with the lead time it was
issued at. The 2.1 story `forecast_vs_backyard` reads one slice of it
(the day-ahead call, one month) and tells a story about it. This module
answers the question the archive was built for: across the window, at
lead 1 through 6, what is the bias, what is the typical miss, and how
often was the rain call right.

Signed errors are FORECAST MINUS MEASURED, so a positive bias means the
model promised more than the backyard delivered — it ran warm. Every
number is °F and inches, API-native; the app converts for display, and a
°C reader sees 5/9 of the same anomaly, which is correct.

Matching is strict in both directions, the same rule the story uses. A
day counts at a lead only when the model filed BOTH a high and a low for
it at that lead and the station measured both. Today never counts,
because today's high has not happened yet. Rain is scored only on days
where the model filed a probability AND the station measured rain at
all: a gauge-less station has no rain calls to grade, not a perfect
record. Absent is not zero.

The one caveat worth stating out loud: the archive holds the SERVER's
sky (the primary station's coordinates, the one-sky-per-server rule),
while the measurements come from the station asked about. On a server
whose stations share a backyard that is the point. On one whose stations
are a state apart, the comparison is against a forecast for somewhere
else, and `coords_station` in the payload says whose.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from . import db
from .day_rain import day_rain_in

# Leads worth scoring. The archive files 0..6; lead 0 is a nowcast for a
# day already underway and says more about the model's assimilation than
# its skill, so the scorecard starts at tomorrow. 6 is the last full lead
# a 7-day run can offer.
LEADS = (1, 2, 3, 4, 5, 6)

# A lead needs this many matched days before its row means anything. Ten
# days of a 90-day window is a thin month, not a verdict, but it is the
# same floor the story producer uses and it lets a fresh install see
# something after a fortnight.
MIN_DAYS = 10

# The model "called rain" at or above this probability, and the station
# "had rain" at or above insights.RAIN_DAY_MIN_IN. Both are the story
# engine's thresholds; a scorecard that graded on a different line from
# the story would report a different skill for the same days.
RAIN_POP = 50.0

# How close counts as a hit on a temperature. Three degrees is the step a
# reader feels: inside it the forecast was right for planning purposes,
# outside it the day did not go the way the model said.
CLOSE_F = 3.0

DEFAULT_DAYS = 90
MAX_DAYS = 400          # the archive's own prune horizon

# A day is scored only when the station COVERED it (F01): its first and
# last observation of the day at least this far apart. One noon reading
# has a minimum and a maximum, and ten of them used to satisfy MIN_DAYS
# and grade the model on highs and lows that were never observed. The
# same rule qualifies a rain day: a gauge that reported for an hour did
# not measure a dry day. A row folded before the span columns existed
# (2.3) has no span and is "coverage unknown", excluded rather than
# assumed; the fold-version rebuild fills it in from raw where raw is.
MIN_COVER_HOURS = 20.0

# Why a day was left out, in the order the checks run. Keys on the wire.
EXCL_NO_FORECAST = "no_forecast"        # the model filed no high/low
EXCL_NO_MEASUREMENT = "no_measurement"  # the station has no high/low
EXCL_UNKNOWN = "coverage_unknown"       # a row without a span (pre-2.3)
EXCL_PARTIAL = "partial_day"            # covered under MIN_COVER_HOURS


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


async def _calls(provider: str, since: _dt.date, lead: int
                 ) -> dict[str, dict[str, Any]]:
    """The newest run's call per valid date at this lead."""
    from . import forecast_snapshots as fs
    return await fs.day_ahead_calls(provider, since, lead_days=lead)


async def _measured(mac: str, since: _dt.date) -> dict[str, dict[str, Any]]:
    """Daily rollups from `since` onward, keyed by local day.

    One indexed range over a table with one row per day — never the
    observations table, which is where the same question would cost a
    million-row scan.
    """
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT day, tempf_min, tempf_max, rain_total, yearly_min, "
            "yearly_max, yearly_first, yearly_last, yearly_rise, "
            "obs_first_ms, obs_last_ms "
            "FROM daily_rollups WHERE mac = ? AND day >= ? "
            "ORDER BY day", (mac, since.isoformat()))).fetchall()
    return {r["day"]: dict(r) for r in rows}


def covered(row: dict[str, Any]) -> bool | None:
    """Did the station cover this day? True when its observations span
    MIN_COVER_HOURS, False when they do not, None when the row carries no
    span at all (folded before 2.3, or preserved behind a thin watermark
    from before it) and the question cannot be answered."""
    first, last = _num(row.get("obs_first_ms")), _num(row.get("obs_last_ms"))
    if first is None or last is None:
        return None
    return (last - first) >= MIN_COVER_HOURS * 3_600_000


def _side(errors: list[tuple[str, float]]) -> dict[str, Any]:
    """Bias, typical miss, how often it was close, and the worst day.

    `bias` is the mean SIGNED error and `mae` the mean of the absolute
    ones: a model that is 8° high half the time and 8° low the other half
    has no bias and is badly wrong, and only reporting both says so.
    """
    n = len(errors)
    bias = sum(e for _, e in errors) / n
    mae = sum(abs(e) for _, e in errors) / n
    close = sum(1 for _, e in errors if abs(e) <= CLOSE_F) / n
    # Ties go to the earlier day, which is the day the reader met first.
    # `errors` is in day order and max() keeps the FIRST maximum; keying
    # on the day as well picked the later one (R23).
    worst_day, worst_err = max(errors, key=lambda t: abs(t[1]),
                               default=("", 0.0))
    return {
        "bias_f": round(bias, 2),
        "mae_f": round(mae, 2),
        "within_3f": round(close, 4),
        "worst_day": worst_day or None,
        "worst_error_f": round(worst_err, 2),
    }


def _rain(days: list[str], calls: dict[str, dict[str, Any]],
          measured: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The rain calls as a four-way tally, on the days both sides spoke.

    A day where the station has no gauge reading is not a day the model
    got right, so it is not counted at all — the denominator is days the
    question could be answered.
    """
    from . import insights as _ins
    hits = false_alarms = misses = quiet = 0
    for iso in days:
        pop = _num(calls[iso].get("pop"))
        rain = day_rain_in(measured[iso])
        if pop is None or rain is None:
            continue
        called, rained = pop >= RAIN_POP, rain >= _ins.RAIN_DAY_MIN_IN
        if called and rained:
            hits += 1
        elif called:
            false_alarms += 1
        elif rained:
            misses += 1
        else:
            quiet += 1
    n = hits + false_alarms + misses + quiet
    if n == 0:
        return None
    return {"n": n, "hits": hits, "false_alarms": false_alarms,
            "misses": misses, "quiet": quiet,
            # Every day the call and the sky agreed, wet or dry. A desert's
            # number is high because most days are dry and the model says
            # so, which is true and worth saying plainly.
            "agreed": round((hits + quiet) / n, 4)}


async def scorecard(mac: str, *, provider: str = "open-meteo",
                    days: int = DEFAULT_DAYS,
                    today: _dt.date | None = None) -> dict[str, Any]:
    """The per-lead scorecard for one station over the last `days` days."""
    days = max(7, min(int(days), MAX_DAYS))
    if today is None:
        from .climate import local_today
        today = local_today()
    since = today - _dt.timedelta(days=days)

    measured = await _measured(mac, since)
    if not measured:
        return {"available": False, "reason": "no measured days yet",
                "provider": provider, "window_days": days, "leads": []}

    leads: list[dict[str, Any]] = []
    matched_any = False
    for lead in LEADS:
        calls = await _calls(provider, since, lead)
        hi_err: list[tuple[str, float]] = []
        lo_err: list[tuple[str, float]] = []
        both: list[str] = []
        # Days the archive and the ledger both have but that could not be
        # scored, by reason. A day is a candidate once it is in the past
        # and the station has a row for it; silence about the rest would
        # be right (nothing to grade), silence about these is not.
        excluded: dict[str, int] = {}
        for iso, fc in calls.items():
            row = measured.get(iso)
            if row is None:
                continue
            try:
                d = _dt.date.fromisoformat(iso)
            except ValueError:
                continue
            # Today's high has not happened, and a future valid date is a
            # forecast waiting to be graded, not a miss.
            if d >= today:
                continue
            f_hi, f_lo = _num(fc.get("tmax_f")), _num(fc.get("tmin_f"))
            a_hi, a_lo = _num(row.get("tempf_max")), _num(row.get("tempf_min"))
            if None in (f_hi, f_lo):
                excluded[EXCL_NO_FORECAST] = excluded.get(EXCL_NO_FORECAST, 0) + 1
                continue
            if None in (a_hi, a_lo):
                excluded[EXCL_NO_MEASUREMENT] = excluded.get(EXCL_NO_MEASUREMENT, 0) + 1
                continue
            # F01: a high and a low from a station that was up for an hour
            # are not the day's high and low. Both temperatures and the
            # rain call below are scored only on a covered day.
            cov = covered(row)
            if cov is None:
                excluded[EXCL_UNKNOWN] = excluded.get(EXCL_UNKNOWN, 0) + 1
                continue
            if not cov:
                excluded[EXCL_PARTIAL] = excluded.get(EXCL_PARTIAL, 0) + 1
                continue
            hi_err.append((iso, f_hi - a_hi))
            lo_err.append((iso, f_lo - a_lo))
            both.append(iso)
        # day_ahead_calls reads ORDER BY valid_date, so insertion order is
        # already chronological; sorting says so rather than relying on it.
        both.sort()
        n = len(both)
        reasons = [{"reason": k, "n": v} for k, v in excluded.items()]
        n_excluded = sum(excluded.values())
        if n == 0:
            leads.append({"lead_days": lead, "n": 0, "enough": False,
                          "scored": 0, "excluded": n_excluded,
                          "excluded_reasons": reasons})
            continue
        matched_any = True
        leads.append({
            "lead_days": lead,
            "n": n,
            "enough": n >= MIN_DAYS,
            "scored": n,
            "excluded": n_excluded,
            "excluded_reasons": reasons,
            "first_day": both[0],
            "last_day": both[-1],
            "high": _side(hi_err),
            "low": _side(lo_err),
            "rain": _rain(both, calls, measured),
        })

    if not matched_any:
        return {"available": False,
                "reason": "the forecast archive has nothing to score yet",
                "provider": provider, "window_days": days, "leads": leads,
                "min_days": MIN_DAYS}

    return {"available": True, "provider": provider, "window_days": days,
            "min_days": MIN_DAYS, "close_f": CLOSE_F, "rain_pop": RAIN_POP,
            "min_cover_hours": MIN_COVER_HOURS,
            "leads": leads,
            "coords_station": await _coords_station()}


async def _coords_station() -> str | None:
    """The station whose coordinates the archive's forecasts were fetched
    for — the one-sky-per-server rule. Named so the app can say whose sky
    it is scoring when a server holds stations in two places."""
    from .forecast_snapshots import coords_device
    d = coords_device(await db.list_devices())
    return None if d is None else (d.get("name") or d.get("mac"))
