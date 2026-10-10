"""Storm summary alerts — the report you get once the rain has stopped.

Doren's ask, 2026-08-17: a notification a set time after the LAST reported
rain, summarising the whole event rather than pinging during it:

    Davis Storm Summary
    Time: 4:08pm - 9:58pm (5.8h)
    Total: 1.21" | Max Rate: 4.00"/h
    Hi 80°F | Lo 70°F | Gust: 25 mph

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


def stitched_rain(rows, cols) -> float:
    """Rain over a window from EVERY counter a station posted, counted once
    (R25-11, the 2.5 detailed review, the half the coverage rule left open).

    `rows` are the window's readings in time order, each a mapping of
    counter column to value (None when that row did not carry it); `cols`
    the counters in preference order. Each counter keeps its own
    high-water mark (counter_progress: resets re-baseline, downward
    revisions hold) and its own progress since it joined. The window's
    rain is the furthest any counter has carried the running total:
    a counter joins (first seen, or returning after a row without it)
    at the total so far, and from then on the total is the largest of
    joined-at plus progress across the counters present.

    So two counters measuring the same rain agree instead of adding: a
    daily counter that catches up a reading after the yearly one (or
    several readings after) reaches the total the yearly already set and
    adds nothing (R26-04, the 2.6 detailed review; the earlier rule took
    the largest increment PER ROW and counted a staggered 0.10 in twice).
    A counter that takes over midway still extends the total from where
    it joined. A counter back after a gap carries on from its own mark
    when that mark still measures it, so the gap cannot credit the same
    rain twice; only one back after a reset or an implausible jump
    rejoins at the total. At a switch from one counter to another
    the one increment no common scale can bridge is still the cost; a
    stray row no longer wins the window.
    """
    peaks: dict[str, float | None] = {c: None for c in cols}
    progress: dict[str, float] = {c: 0.0 for c in cols}
    joined_at: dict[str, float] = {c: 0.0 for c in cols}
    present_before: set[str] = set()
    total = 0.0
    for row in rows:
        present = {c for c in cols if row.get(c) is not None}
        if not present:
            continue
        for c in present:
            if c not in present_before and (peaks[c] is None
                                            or _rebaselines(peaks[c], row[c])):
                # First seen, or back after a reset or an implausible jump
                # its old mark cannot bridge: a fresh baseline, joining the
                # total as it stands before this row.
                peaks[c], progress[c], joined_at[c] = row[c], 0.0, total
                continue
            # Present last row, or back after a gap with a reading its own
            # mark still measures: it carries on from where it was. What it
            # gained while away is rain it measured too, and the total is
            # the furthest any counter reached, so a counter catching up
            # across a gap cannot count the same rain twice (follow-up
            # review, R26-04: 0.0, missing, 0.0, 0.1 used to rejoin at the
            # total and add the 0.1 the other counter already had).
            inc, peaks[c] = counter_progress(peaks[c], row[c])
            progress[c] += inc
        total = max([total] + [joined_at[c] + progress[c] for c in present])
        present_before = present
    return total


def _rebaselines(peak: float, curr: float) -> bool:
    """True when counter_progress would start a NEW mark at `curr` (a period
    reset, or a jump too large to be rain) rather than measure from `peak`."""
    if curr < peak:
        return curr <= _RESET_FLOOR_IN
    return curr - peak > _MAX_SANE_INCREMENT_IN


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
    # Hi first and LABELED, no "Temps:" prefix (Doren's three refinement
    # rounds, 2026-08-27): Hi/Lo label themselves, and the dropped prefix
    # keeps the full three-stat line under a lock-screen banner's width.
    # ONE-SIDED temps render too (R14): a station whose sensor came up
    # mid-storm has only a max — dropping BOTH silently was absent-is-
    # not-zero backwards.
    if s.max_tempf is not None and s.min_tempf is not None:
        third.append(f"Hi {s.max_tempf:.0f}°F | Lo {s.min_tempf:.0f}°F")
    elif s.max_tempf is not None:
        third.append(f"Hi {s.max_tempf:.0f}°F")
    elif s.min_tempf is not None:
        third.append(f"Lo {s.min_tempf:.0f}°F")
    if s.max_gust_mph is not None:
        third.append(f"Gust: {s.max_gust_mph:.0f} mph")
    if third:
        lines.append(" | ".join(third))

    return title, "\n".join(lines)


def build_storm_html(device_name: str, s: StormSummary, tz_name: str,
                     theme: str = "dark") -> str:
    """The storm summary in the morning report's dress (2.3, Doren). The
    same numbers as the text, as tiles; a line the text has no room for
    says when it rained. Absent sensors get no tile, not a placeholder."""
    from . import email_card as ec
    import html as _h
    name = _clean_name(device_name)
    span = f"{_fmt_clock(s.started_ms, tz_name)} to {_fmt_clock(s.ended_ms, tz_name)}"
    inner = ec.section_label(f"\u25cf {name.upper()}", padding="14px 0 6px")
    inner += (f'<div style="font:400 14px {ec.FONT};color:{ec.TEXT};">'
              f'Rain from {_h.escape(span)}, {s.duration_hours:.1f} hours.</div>')
    tiles = [ec.tile("TOTAL", f"{s.total_in:.2f}&quot;", ec.ACCENT)]
    if s.peak_rate_in_hr is not None:
        tiles.append(ec.tile("MAX RATE", f"{s.peak_rate_in_hr:.2f}&quot;/h",
                             ec.WARM if s.peak_rate_in_hr >= 1.0 else ec.TEXT))
    if s.max_gust_mph is not None:
        tiles.append(ec.tile("PEAK GUST", f"{s.max_gust_mph:.0f} mph",
                             ec.WARM if s.max_gust_mph >= 30 else ec.TEXT))
    if s.max_tempf is not None and s.min_tempf is not None:
        tiles.append(ec.tile("HI / LO", f"{s.max_tempf:.0f}&deg; / {s.min_tempf:.0f}&deg;F"))
    elif s.max_tempf is not None:
        tiles.append(ec.tile("HIGH", f"{s.max_tempf:.0f}&deg;F", ec.WARM))
    elif s.min_tempf is not None:
        tiles.append(ec.tile("LOW", f"{s.min_tempf:.0f}&deg;F", ec.ACCENT))
    inner += ec.tile_row(tiles, margin_top=10)
    day = _local_day_label(s.ended_ms, tz_name)
    return ec.shell(day, f"{name} Storm Summary", inner,
                    "Measured in your backyard, one report per storm. "
                    "Every storm lives in the app's Reports pane.",
                    theme=theme)


def _local_day_label(ms: int, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    dt = datetime.fromtimestamp(ms / 1000, tz)
    return dt.strftime("%A, %B ") + str(dt.day)
