"""The one rule for "how much rain fell on this rollup day" (2.1).

Three consumers used to answer it three ways: the story engine had the
right rule, the Zambretti ledger copied it, and the climate reports
wrote `rain_total or 0.0`, which printed 0.00 in for every station that
reports only a yearly counter (the repo's own LilyGO relay sets just
`yearly_in`) — the "absent is not zero" bug the 1.6 review named as the
repo's recurring one, in a new place (2.1 pre-release review BE-1).

Provenance: `rain_total` is the day's high-water mark of `dailyrainin`,
and the tipping-gauge-over-haptic preference is applied at ingest, once,
so reading it IS honouring that rule. `hourlyrainin` is the trailing
60-minute accumulation (see the definition beside FIELD_LABELS in
alerts.py) and is never read here. The yearly-counter delta is the fallback for sources
that carry no daily total. Those are lifetime counters (an SDR or
LilyGO sensor posts nothing else) and they do NOT reset on January 1,
so the old skip-Jan-1 rule dropped a real day from every such station
(round-two review BE-N4). What CAN go wrong is a counter swap or reset
inside the day, which files the whole counter as one day's rain; the
guard for that is the same 80 in/day plausibility ceiling ingest
applies to a daily total.

None, never 0.0, when neither counter reported: a station with no rain
gauge must drop the dimension, print a dash, and stay out of the sums.
"""
from __future__ import annotations

import math
from typing import Any


# The most rain a day can plausibly hold (ingest's dailyrainin band). A
# yearly-counter rise above it is a reset or a replaced gauge, not weather.
DAY_RAIN_MAX_IN = 80.0
# The most one STEP of a lifetime counter can plausibly be. Between two
# readings — five minutes apart on a live station, a day apart on an
# imported history — ten inches is past anything measured anywhere on
# earth, so a step that large is a corrupt counter and not weather. It is
# dropped rather than allowed to poison the day (2.3, the yearly_rise
# clause in insights._UPSERT_DAILY).
RISE_MAX_IN = 10.0
# The RATE gate on one step (2.3, R23): a step is rain only if it fits in
# the time since the previous reading at ingest's plausible rate
# (`ingest_max_rain_rate_in_per_hr`, default 2.0) plus a fixed slack for
# tip-counter jitter. These are the SAME constants ingest's spike guard
# uses (`_is_rain_glitch`, `_confirms_rejected_level`), so the two gates
# agree on what a plausible step is. What this catches that RISE_MAX_IN
# cannot: a manual console set. The Davis console's yearly counter went
# 0 -> 0.34 in one 60 s step on 2026-08-10 and 14.6 -> 0 -> 0.73 in nine
# minutes on 2026-05-24, both with the daily counter at 0 all day;
# ingest accepts such a step as a "level shift" once the next reading
# confirms it (a manual set always confirms), and the fold then credited
# it as rain. A rejected step advances the counter's last reading and
# adds nothing, so the readings after it are measured from the new level.
RATE_SLACK_IN = 0.25
RATE_MAX_IN_PER_HR_DEFAULT = 2.0

# The pre-2.2 reset signature (the last arm of day_rain_in): a day whose
# counter low sits under the floor while its high is over the total was a
# reset, and the min/max pair cannot say how much fell either side of it.
RESET_FLOOR_IN = 0.1
RESET_TOTAL_IN = 1.0


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def day_rain_in(row: Any) -> float | None:
    """The day's rainfall in inches, or None when the station never
    measured it. `row` is a daily_rollups row: a dict or a sqlite Row
    carrying rain_total, yearly_min, yearly_max and day."""
    if row is None:
        return None
    try:
        get = row.get                      # dict
    except AttributeError:
        keys = set(row.keys())             # sqlite3.Row / aiosqlite.Row

        def get(k, default=None):
            return row[k] if k in keys else default
    total = _num(get("rain_total"))
    if total is not None:
        return max(0.0, total)
    # 2.3: the sum of every RISE the counter took that day. This is the
    # only form that is right for a day with more than one reset, or a
    # reset followed by more rain than the restarted counter had reached
    # — both of which read short as last - first. It ranks above the span
    # below and under the station's own daily counter, which needs no
    # arithmetic at all. A rise is counted only when it fits the time it
    # took (RATE_SLACK_IN above): a manual counter set is not rain.
    #
    # Zero is a real answer here (a dry day on a working gauge), so this
    # tests for None and not for truth.
    rise = _num(get("yearly_rise"))
    if rise is not None:
        rise = max(0.0, rise)
        return None if rise > DAY_RAIN_MAX_IN else rise
    # 2.2: the counter's first and last reading of the day, when the
    # rollup has them. last >= first is the day's rain, full stop; last
    # < first is a reset inside the day, and what the restarted counter
    # holds is what fell since. Kept for rows folded before yearly_rise
    # existed and not yet rebuilt (a live fold that ran out of order also
    # abandons the rise for that day and lands here).
    first, last = _num(get("yearly_first")), _num(get("yearly_last"))
    if first is not None and last is not None:
        rain = last - first if last >= first else last
        rain = max(0.0, rain)
        return None if rain > DAY_RAIN_MAX_IN else rain
    # LAST fallback: the pre-2.2 min/max "reset signature". A row reaches
    # this arm only if it was folded before 2.2 (no first/last) and no
    # rebuild has touched it since — the rebuild fills `yearly_rise` for
    # every day, after which this arm is dead code for that server. It is
    # not retired because a guest server upgraded straight from 2.1 reads
    # its history through it until its first rebuild finishes, and a row
    # that answers "reset, unknown" here is better than one that answers
    # "42 inches". `day_rain_provenance` reports it as "signature" so a
    # caller can tell a figure from this arm apart from a measured one.
    lo, hi = _num(get("yearly_min")), _num(get("yearly_max"))
    if lo is not None and hi is not None:
        delta = max(0.0, hi - lo)
        if delta > DAY_RAIN_MAX_IN:
            return None
        # A counter that RESETS leaves a signature the rollup can see: its
        # low touches zero while its high is the old total. Not gated on
        # the calendar (round-three review BE-F5): a UTC-midnight reset on
        # a UTC-7 station lands on December 31 local, and a replaced gauge
        # resets on any day. A lifetime counter's Jan 1 has both in the
        # tens and is kept; a fresh sensor's first inches are lost for one
        # day, which is the cheaper mistake.
        if lo < RESET_FLOOR_IN and hi > RESET_TOTAL_IN:
            return None
        return delta
    return None


def sum_or_none(values) -> float | None:
    """Sum of the values that exist; None when none do, so a period with
    no gauge reads as unknown rather than dry."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


# How a day's rain was arrived at, best first. The app shows the number;
# this says what is behind it, so "0.00 in" from a working gauge and
# "0.00 in" from a span that only saw one reading are not the same claim.
PROVENANCE_DAILY = "daily"        # the station's own daily counter
PROVENANCE_RISE = "rise"          # every rise the counter took that day
PROVENANCE_SPAN = "span"          # last - first, right unless it reset twice
PROVENANCE_SIGNATURE = "signature"  # min/max, pre-2.2 rows a rebuild has not reached
PROVENANCE_NONE = "none"          # the station never measured it


def day_rain_provenance(row: Any) -> str:
    """Which rule `day_rain_in` used for this row.

    PARTIAL days are the reason this exists: a row folded before 2.3 has
    only first and last, and that span is short whenever the counter reset
    more than once. Naming it lets a caller mark the figure as provisional
    until the rebuild fills `yearly_rise` in, rather than presenting two
    different measurements as the same fact.
    """
    if row is None:
        return PROVENANCE_NONE
    try:
        get = row.get
    except AttributeError:
        keys = set(row.keys())

        def get(k, default=None):
            return row[k] if k in keys else default
    if _num(get("rain_total")) is not None:
        return PROVENANCE_DAILY
    if _num(get("yearly_rise")) is not None:
        return PROVENANCE_RISE
    if _num(get("yearly_first")) is not None and _num(get("yearly_last")) is not None:
        return PROVENANCE_SPAN
    if _num(get("yearly_min")) is not None and _num(get("yearly_max")) is not None:
        return PROVENANCE_SIGNATURE
    return PROVENANCE_NONE
