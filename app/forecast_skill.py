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
import itertools as _it
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


async def _calls(provider: str, since: _dt.date, lead: int,
                 near: tuple[float, float] | None = None
                 ) -> dict[str, dict[str, Any]]:
    """The newest run's call per valid date at this lead, for the sky at
    `near` (2.6: runs archived for another sky are left out)."""
    from . import forecast_snapshots as fs
    return await fs.day_ahead_calls(provider, since, lead_days=lead, near=near)


async def _sky_coords() -> tuple[float, float] | None:
    """Where this server's forecasts are fetched for, today."""
    from .forecast_snapshots import coords
    return coords(await db.list_devices())


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


class _Ledger:
    """What every source's grade reads in common, read once: the window,
    the station's measured days and the sky being scored. The per-source
    scorecard (2.7) grades five providers against ONE ledger, so they are
    graded on the same days by the same rules and the station's rollups
    are read once, not five times."""

    def __init__(self, mac: str, days: int, today: _dt.date | None) -> None:
        self.mac = mac
        self.days = max(7, min(int(days), MAX_DAYS))
        self.today = today
        self.since: _dt.date | None = None
        self.measured: dict[str, dict[str, Any]] = {}
        self.near: tuple[float, float] | None = None
        self._station: tuple[str | None] | None = None
        # provider -> (card, scored days): a source graded once per ledger.
        self.cards: dict[str, tuple[dict[str, Any], dict[int, dict[str, tuple[float, float]]]]] = {}

    async def load(self) -> "_Ledger":
        if self.today is None:
            from .climate import local_today
            self.today = local_today()
        self.since = self.today - _dt.timedelta(days=self.days)
        self.measured = await _measured(self.mac, self.since)
        if self.measured:
            self.near = await _sky_coords()
        return self

    async def coords_station(self) -> str | None:
        if self._station is None:
            self._station = (await _coords_station(),)
        return self._station[0]


async def _grade(ledger: _Ledger, provider: str
                 ) -> tuple[list[dict[str, Any]], dict[int, dict[str, tuple[float, float]]]]:
    """THE grader: one provider's per-lead rows against the ledger, plus
    each lead's scored days as {day: (high error, low error)} for the
    head-to-head (`best_source`). Every source goes through here; there is
    no second copy of the matching rules."""
    if ledger.today is None or ledger.since is None:
        raise RuntimeError("_grade before _Ledger.load()")
    today, since, measured = ledger.today, ledger.since, ledger.measured
    leads: list[dict[str, Any]] = []
    scored: dict[int, dict[str, tuple[float, float]]] = {}
    for lead in LEADS:
        calls = await _calls(provider, since, lead, ledger.near)
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
        # hi_err and lo_err are filled together, a pair per scored day.
        scored[lead] = {d: (h, lo) for (d, h), (_, lo) in zip(hi_err, lo_err)}
        n = len(both)
        reasons = [{"reason": k, "n": v} for k, v in excluded.items()]
        n_excluded = sum(excluded.values())
        if n == 0:
            leads.append({"lead_days": lead, "n": 0, "enough": False,
                          "scored": 0, "excluded": n_excluded,
                          "excluded_reasons": reasons})
            continue
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
    return leads, scored


async def _card(ledger: _Ledger, provider: str
                ) -> tuple[dict[str, Any], dict[int, dict[str, tuple[float, float]]]]:
    """One provider's scorecard envelope around `_grade` (graded once per
    ledger: the requested provider is also one of the sources)."""
    if provider not in ledger.cards:
        ledger.cards[provider] = await _envelope(ledger, provider)
    return ledger.cards[provider]


async def _envelope(ledger: _Ledger, provider: str
                    ) -> tuple[dict[str, Any], dict[int, dict[str, tuple[float, float]]]]:
    days = ledger.days
    if not ledger.measured:
        return ({"available": False, "reason": "no measured days yet",
                 "provider": provider, "window_days": days, "leads": []}, {})
    leads, scored = await _grade(ledger, provider)
    if not any(row["n"] for row in leads):
        return ({"available": False,
                 "reason": "the forecast archive has nothing to score yet",
                 "provider": provider, "window_days": days, "leads": leads,
                 "min_days": MIN_DAYS}, scored)
    return ({"available": True, "provider": provider, "window_days": days,
             "min_days": MIN_DAYS, "close_f": CLOSE_F, "rain_pop": RAIN_POP,
             "min_cover_hours": MIN_COVER_HOURS,
             "leads": leads,
             "coords_station": await ledger.coords_station()}, scored)


async def scorecard(mac: str, *, provider: str = "open-meteo",
                    days: int = DEFAULT_DAYS,
                    today: _dt.date | None = None) -> dict[str, Any]:
    """The per-lead scorecard for one station over the last `days` days."""
    ledger = await _Ledger(mac, days, today).load()
    card, _ = await _card(ledger, provider)
    return card


# ── Every source, graded against the yard (2.7) ─────────────────────────
# The archive has held more than one forecast since 2.6 (TWC beside
# Open-Meteo) and three of Open-Meteo's models since 2.7. `sources` grades
# each of them through the same `_grade` against the same ledger, and
# names the one that has done best by a rule simple enough to state in a
# sentence. The wire contract is docs/2.7/FORECAST_SOURCES.md.

# The leads the "best" rule reads: tomorrow and the day after, the calls
# people plan by, and the ones every source files (TWC stops at lead 4).
BEST_LEADS = (1, 2)

BEST_RULE = (
    "Among the sources with at least min_days scored days at leads 1 and 2, "
    "compared on the days every one of them was scored at each lead (when "
    "those shared days are fewer than min_days at either lead, the largest "
    "group of sources whose shared days reach min_days at both leads is "
    "compared instead; between groups that size, the one with more shared "
    "days, then the one listed first), the lowest mean of the high and low "
    "typical miss (mae_f) at leads 1 and 2. It takes two sources to name a "
    "best; a tie at two decimals goes to the source listed first."
)

# Why there is no best, on the wire.
BEST_TOO_FEW_SOURCES = "too_few_sources"
BEST_TOO_FEW_SHARED_DAYS = "too_few_shared_days"

# Not an archive status: the server has no station with a location, so
# nothing is fetched for any source.
STATUS_NO_LOCATION = "no_location"


def _local_date(ms: int) -> _dt.date:
    from zoneinfo import ZoneInfo
    from .config import settings
    try:
        tz: _dt.tzinfo = ZoneInfo(settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    return _dt.datetime.fromtimestamp(ms / 1000, tz).date()


def _enough(leads: list[dict[str, Any]]) -> bool:
    by = {row["lead_days"]: row for row in leads}
    return all((by.get(lead) or {}).get("n", 0) >= MIN_DAYS for lead in BEST_LEADS)


def days_until_enough(leads: list[dict[str, Any]], first_ms: int | None,
                      today: _dt.date, window_days: int,
                      status: str) -> int | None:
    """At the earliest, how many days until this source has MIN_DAYS
    scored days at every BEST_LEADS lead: 0 when it has them, None when it
    cannot get there (not archiving, or a window shorter than MIN_DAYS).

    Two lower bounds, the larger wins. The calendar one: a lead-L call
    issued on day C is first gradable on C + L + 1, so MIN_DAYS of them
    take until C + L + MIN_DAYS. The count one: the days still missing.
    Both assume every day from here is scored, which a station that drops
    out will not meet; the apps say "at the earliest" for that reason.
    """
    if _enough(leads):
        return 0
    from . import forecast_snapshots as fs
    if status != fs.STATUS_ARCHIVING or window_days < MIN_DAYS:
        return None
    by = {row["lead_days"]: row for row in leads}
    missing = MIN_DAYS - min((by.get(lead) or {}).get("n", 0) for lead in BEST_LEADS)
    lag = max(BEST_LEADS)
    if first_ms is None:
        calendar = lag + MIN_DAYS        # collection starts with the next beat
    else:
        ready = _local_date(first_ms) + _dt.timedelta(days=lag + MIN_DAYS)
        calendar = max(0, (ready - today).days)
    return max(missing, calendar)


def best_source(graded: list[tuple[str, dict[int, dict[str, tuple[float, float]]]]]
                ) -> tuple[dict[str, Any] | None, str | None]:
    """BEST_RULE over (source id, scored days per lead) pairs, in list
    order, each already past MIN_DAYS at every BEST_LEADS lead. Returns
    (best, None) or (None, reason).

    Head to head on shared days because the sources started collecting at
    different times: ninety days of one model against twelve of another
    would compare a summer with an autumn, not two forecasts.
    """
    pool = list(graded)
    if len(pool) < 2:
        return None, BEST_TOO_FEW_SOURCES
    # The largest group of sources that shares MIN_DAYS at every best lead;
    # among groups that size, the most shared days (both leads summed), then
    # the group listed first. Every subset is tried, biggest first: there
    # are at most five sources, so 26 groups at most. Dropping the source
    # with the fewest days first (the 2.7 draft) could break up the only
    # viable pair for a longer-running third that overlapped neither
    # (CodeRabbit, PR #98).
    days = [{lead: set(sc.get(lead) or {}) for lead in BEST_LEADS} for _, sc in pool]
    for size in range(len(pool), 1, -1):
        chosen: tuple[int, ...] | None = None
        chosen_shared: dict[int, set] = {}
        chosen_total = -1
        # combinations() yields in list order, and only a strictly larger
        # total replaces the pick, so a tie keeps the group listed first.
        for group in _it.combinations(range(len(pool)), size):
            shared = {lead: set.intersection(*(days[i][lead] for i in group))
                      for lead in BEST_LEADS}
            if any(len(shared[lead]) < MIN_DAYS for lead in BEST_LEADS):
                continue
            total = sum(len(shared[lead]) for lead in BEST_LEADS)
            if total > chosen_total:
                chosen, chosen_shared, chosen_total = group, shared, total
        if chosen is None:
            continue
        compared = []
        for i in chosen:
            sid, sc = pool[i]
            hi = [sum(abs(sc[lead][d][0]) for d in chosen_shared[lead])
                  / len(chosen_shared[lead]) for lead in BEST_LEADS]
            lo = [sum(abs(sc[lead][d][1]) for d in chosen_shared[lead])
                  / len(chosen_shared[lead]) for lead in BEST_LEADS]
            compared.append({
                "source": sid,
                "score_f": round((sum(hi) + sum(lo)) / (2 * len(BEST_LEADS)), 2),
                "high_mae_f": round(sum(hi) / len(hi), 2),
                "low_mae_f": round(sum(lo) / len(lo), 2),
            })
        # min() keeps the FIRST minimum: a tie goes to the earlier source.
        win = min(compared, key=lambda c: c["score_f"])
        return ({"source": win["source"], "score_f": win["score_f"],
                 "shared_days": [{"lead_days": lead, "n": len(chosen_shared[lead])}
                                 for lead in BEST_LEADS],
                 "compared": compared}, None)
    return None, BEST_TOO_FEW_SHARED_DAYS


async def _sources(ledger: _Ledger, selected: str | None) -> dict[str, Any]:
    """The `sources` block: every archived source's scorecard, its
    collection state, and the best of them by BEST_RULE."""
    from . import forecast_snapshots as fs
    near = ledger.near if ledger.measured else await _sky_coords()
    twc_status = await fs.twc_status()
    items: list[dict[str, Any]] = []
    eligible: list[tuple[str, dict[int, dict[str, tuple[float, float]]]]] = []
    today = ledger.today or _dt.date.today()
    for src in fs.SOURCES:
        sid = src["id"]
        card, scored = await _card(ledger, sid)
        span = await fs.archive_span(sid, near)
        if near is None:
            status = STATUS_NO_LOCATION
        elif sid == "twc":
            status = twc_status
        else:
            status = fs.STATUS_ARCHIVING
        leads = card.get("leads") or []
        enough = bool(card.get("available")) and _enough(leads)
        if enough:
            eligible.append((sid, scored))
        first = span["first_ms"]
        items.append({
            "id": sid,
            "name": src["name"],
            "provider": src["provider"],
            "model": src["model"],
            "max_lead_days": src["max_lead_days"],
            "selectable": src["selectable"],
            "selected": sid == selected,
            "status": status,
            "collecting_since": None if first is None else _local_date(first).isoformat(),
            "collecting_since_ms": first,
            "last_archived_ms": span["last_ms"],
            "runs": span["runs"],
            "available": bool(card.get("available")),
            "reason": card.get("reason"),
            "enough": enough,
            "days_until_enough": days_until_enough(leads, first, today,
                                                   ledger.days, status),
            "leads": leads,
        })
    best, why = best_source(eligible)
    return {"selected": selected, "min_days": MIN_DAYS,
            "best_leads": list(BEST_LEADS), "best_rule": BEST_RULE,
            "best": best, "best_reason": why, "items": items}


async def accuracy(mac: str, *, provider: str = "open-meteo",
                   days: int = DEFAULT_DAYS, selected: str | None = None,
                   today: _dt.date | None = None) -> dict[str, Any]:
    """The forecast-accuracy payload: `scorecard(provider)` exactly as it
    has always been, plus the 2.7 `sources` block, graded on one ledger."""
    ledger = await _Ledger(mac, days, today).load()
    card, _ = await _card(ledger, provider)
    out = dict(card)
    out["sources"] = await _sources(ledger, selected)
    return out


async def _coords_station() -> str | None:
    """The station whose coordinates the archive's forecasts were fetched
    for — the one-sky-per-server rule. Named so the app can say whose sky
    it is scoring when a server holds stations in two places."""
    from .forecast_snapshots import coords_device
    d = coords_device(await db.list_devices())
    return None if d is None else (d.get("name") or d.get("mac"))


# ── Backyard correction (2.5, C1) ────────────────────────────────────────
# The scorecard says how far the forecast tends to miss THIS yard, per
# lead. The correction is that bias turned around: forecast minus bias is
# what the station has tended to measure. Shown BESIDE the forecast, never
# instead of it (the forecast is somebody's model; this is what your yard
# has done to it). A lead gets a correction only with MIN_DAYS scored days
# and a bias of at least MIN_OFFSET_F; below that, a nudge is noise.

CORRECTION_DAYS = 45
MIN_OFFSET_F = 1.0


def corrections_from(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: the scorecard's leads → the offsets to ADD to a forecast."""
    out = []
    for lead in card.get("leads") or []:
        if not lead.get("enough"):
            continue
        hi = (lead.get("high") or {}).get("bias_f")
        lo = (lead.get("low") or {}).get("bias_f")
        row: dict[str, Any] = {"lead_days": lead["lead_days"], "n": lead["n"]}
        row["high_offset_f"] = (round(-hi, 1) if isinstance(hi, (int, float))
                                and abs(hi) >= MIN_OFFSET_F else None)
        row["low_offset_f"] = (round(-lo, 1) if isinstance(lo, (int, float))
                               and abs(lo) >= MIN_OFFSET_F else None)
        if row["high_offset_f"] is not None or row["low_offset_f"] is not None:
            out.append(row)
    return out
