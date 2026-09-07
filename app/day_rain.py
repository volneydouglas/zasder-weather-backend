"""The one rule for "how much rain fell on this rollup day" (2.1).

Three consumers used to answer it three ways: the story engine had the
right rule, the Zambretti ledger copied it, and the climate reports
wrote `rain_total or 0.0`, which printed 0.00 in for every station that
reports only a yearly counter (the repo's own LilyGO relay sets just
`yearly_in`) — the "absent is not zero" bug the 1.6 review named as the
repo's recurring one, in a new place (2.1 pre-release review BE-1).

Provenance: `rain_total` is the day's high-water mark of `dailyrainin`,
and the tipping-gauge-over-haptic preference is applied at ingest, once,
so reading it IS honouring that rule. `hourlyrainin` is a RATE and is
never read here. The yearly-counter delta is the fallback for sources
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
# The New-Year reset signature (see day_rain_in).
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
