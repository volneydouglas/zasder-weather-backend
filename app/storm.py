"""Storm summary alerts — the report you get once the rain has stopped.

Doren's ask, 2026-08-17: a notification a set time after the LAST reported
rain, summarising the whole event rather than pinging during it:

    Davis Storm Summary
    Time: 4:08pm - 9:58pm (5.8h)
    Total: 1.21" | Max Rate: 4.00"/h
    Temps: 70°F | 80°F | Gust: 25 mph

This is a different shape from every other alert here. Threshold and smart
alerts are edge-triggered on a value crossing a line; this one is an *event*
with accumulated statistics, delivered on the trailing edge.

Deliberately almost stateless. The tracker persists only three things — is a
storm open, when it started, when rain was last seen — and computes every
number in the summary from the `observations` table at the end. Carrying
running totals in a state row would drift across restarts, double-count a
replayed reading, and give the daily-rain repair tools a second place to be
wrong. The history is already the source of truth; ask it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("storm")


# Rain "is falling" is detected from an INCREASE in a cumulative counter, not
# from hourlyrainin. hourlyrainin is a trailing-hour accumulation, so it stays
# above zero for a full hour after the last drop — a 30-minute quiet window
# measured against it could never elapse before the hour did, and every
# summary would be an hour late with a wrong end time.
_COUNTER_FIELDS = ("yearlyrainin", "dailyrainin")

# A single reading may not add more than this. Guards against a counter reset
# read as a giant positive jump, and against decode garbage that slipped the
# ingest bands. Sized well above any real per-reading increment.
_MAX_SANE_INCREMENT_IN = 2.0


@dataclass(frozen=True)
class StormSummary:
    started_ms: int
    ended_ms: int
    total_in: float
    peak_rate_in_hr: float | None
    min_tempf: float | None
    max_tempf: float | None
    max_gust_mph: float | None

    @property
    def duration_hours(self) -> float:
        return max(0.0, (self.ended_ms - self.started_ms) / 3_600_000)


def counter_value(obs: dict) -> tuple[str, float] | None:
    """The best available cumulative rain counter on a reading.

    Yearly first because it is monotonic across a midnight boundary — a storm
    that runs past midnight would otherwise see `dailyrainin` reset to zero
    and read as "no rain", ending the event early and splitting one storm
    into two summaries.
    """
    for field in _COUNTER_FIELDS:
        v = obs.get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            f = float(v)
            if f == f and abs(f) != float("inf"):
                return field, f
    return None


def rain_increment(prev: float | None, curr: float | None) -> float:
    """Inches added between two consecutive counter readings.

    Zero for a missing reading, for a decrease (the counter reset), and for an
    implausibly large jump. Never negative — a reset must not subtract from a
    storm total, and must not be mistaken for rain either.
    """
    if prev is None or curr is None:
        return 0.0
    delta = curr - prev
    if delta <= 0 or delta > _MAX_SANE_INCREMENT_IN:
        return 0.0
    return delta


# A counter reading at or below this is a fresh period, not a revision. A
# tipping bucket resolves 0.01 in, so two counts of slack distinguishes
# "midnight rolled over" from "the source corrected itself".
_RESET_FLOOR_IN = 0.02


def counter_progress(peak: float | None, curr: float | None) -> tuple[float, float | None]:
    """(inches added, new high-water mark) for one counter reading.

    Tracks a PEAK rather than the previous value, because some sources revise
    the day's total DOWNWARD and then climb again. WeatherFlow does exactly
    this — on 2026-08-19 a Tempest went 0.104 -> 0.025 -> 0.123 inside half an
    hour. Comparing against the previous reading ignores the drop (correctly)
    and then counts the whole re-climb as new rain, reporting ~0.20 in for a
    storm that dropped ~0.12. Measuring against the peak counts each hundredth
    once, however many times the source walks back over it.

    A genuine period reset (daily at midnight, yearly at New Year) is a drop
    to ~zero, and must start a new mark instead — otherwise every drop after a
    reset would be swallowed until it re-passed the old peak.
    """
    if curr is None:
        return 0.0, peak
    if peak is None:
        return 0.0, curr
    if curr < peak:
        if curr <= _RESET_FLOOR_IN:
            return 0.0, curr          # period rolled over; re-baseline
        return 0.0, peak              # source revised downward; hold the mark
    delta = curr - peak
    if delta > _MAX_SANE_INCREMENT_IN:
        return 0.0, curr              # counter jumped implausibly; re-baseline
    return delta, curr


def should_close(last_rain_ms: int | None, now_ms: int, quiet_minutes: float) -> bool:
    """True once the quiet window since the last rain has fully elapsed."""
    if last_rain_ms is None:
        return False
    return (now_ms - last_rain_ms) >= quiet_minutes * 60_000


def worth_reporting(summary: StormSummary, min_total_in: float) -> bool:
    """Suppress the drizzle that is not a storm.

    Without this, a single tip of the bucket on an otherwise dry day sends a
    "storm summary" reporting 0.01 inches, which trains people to ignore the
    notification that matters.
    """
    return summary.total_in >= min_total_in


def _fmt_clock(ms: int, tz_name: str) -> str:
    try:
        zi = ZoneInfo(tz_name)
    except Exception:
        zi = ZoneInfo("UTC")
    dt = datetime.fromtimestamp(ms / 1000, zi)
    # "4:08pm" — lowercase meridiem and no leading zero, matching the format
    # Doren asked for rather than the 24h stamp the staleness alerts use.
    #
    # The meridiem is derived, NOT taken from strftime("%p"): that is
    # locale-dependent and returns an empty string under the C locale and a
    # non-ASCII string under several others, so the time would silently lose
    # its am/pm on a server whose locale is not English.
    hour = dt.hour % 12 or 12
    meridiem = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{meridiem}"


def _clean_name(name: str) -> str:
    """Sanitize a device name for message headers: EmailMessage raises
    ValueError on a control character (esp. '\\n') in a header, so one
    hostile/corrupt name would break EVERY alert for that device. Canonical
    here because alerts imports storm (the reverse import would be a cycle);
    alerts re-exports it. Harden it in one place or the storm-summary
    channel becomes the one alert kind that still breaks."""
    cleaned = "".join(c if ord(c) >= 32 else " " for c in str(name))
    return cleaned.strip() or "device"


def build_storm_message(device_name: str, s: StormSummary,
                        tz_name: str) -> tuple[str, str]:
    """(title, body). Pure — unit-testable, and the format Doren specified.

    Every optional line degrades rather than printing a placeholder: a station
    with no wind sensor should not be told its max gust was "—".
    """
    name = _clean_name(device_name)
    title = f"{name} Storm Summary"

    span = f"{_fmt_clock(s.started_ms, tz_name)} - {_fmt_clock(s.ended_ms, tz_name)}"
    lines = [f"Time: {span} ({s.duration_hours:.1f}h)"]

    totals = f"Total: {s.total_in:.2f}\""
    if s.peak_rate_in_hr is not None:
        totals += f" | Max Rate: {s.peak_rate_in_hr:.2f}\"/h"
    lines.append(totals)

    third: list[str] = []
    if s.min_tempf is not None and s.max_tempf is not None:
        third.append(f"Temps: {s.min_tempf:.0f}°F | {s.max_tempf:.0f}°F")
    if s.max_gust_mph is not None:
        third.append(f"Gust: {s.max_gust_mph:.0f} mph")
    if third:
        lines.append(" | ".join(third))

    return title, "\n".join(lines)
