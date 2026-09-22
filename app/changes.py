"""The weather-change timeline (2.4 item 9, carried from 2.3): the
moments the weather actually TURNED, read from the station's own
minute data.

`highlights.py` answers "how unusual is today" by ranking readings
against this station's own record. This answers the other question a
glance at a chart is really asking, which is "when did it change" — the
rain started at 4.10, the wind swung round at 4.25, the pressure had
already turned at 3.

Everything here is detection over a window of rows, never a forecast and
never a claim about weather the station did not measure. Absent is not
zero throughout: a missing barometer means no pressure turn, not a flat
one, and a station that reports no rain counter contributes no rain
events rather than a dry timeline.

Units are API-native, the way everything else in this repo stores them:
°F, mph, inHg, inches. The times are epoch ms and the app formats them
in the station's zone, so nothing here needs a timezone.

`detect` is pure and is where the tests live; `assemble` fetches.
"""
from __future__ import annotations

import bisect
import logging
import math
import time
from typing import Any

from . import derived

log = logging.getLogger("zasder.changes")

WINDOW_HOURS_DEFAULT = 24
WINDOW_HOURS_MAX = 72
MAX_CHANGES = 12          # a timeline, not a log
MIN_ROWS = 8              # under this a window cannot show a trend

# Rain. The instantaneous signal is the DAY counter moving, not the
# trailing hour total: hourlyrainin stays positive for an hour after the
# last drop, which would put "stopped" fifty minutes late. A counter that
# goes DOWN is midnight (or a manual set, ref_rain_counters) and is
# treated as no information rather than as negative rain.
RAIN_GAP_MIN = 30         # dry minutes that separate two showers
RAIN_STOP_MIN = 20        # dry minutes before a shower is over

# Pressure. Over three hours, which is the interval every barometer
# trend in this repo already uses. The turn has to have a real slope on
# BOTH sides or a barometer wobbling around flat produces a turn an hour.
PRESSURE_SPAN_MIN = 180
PRESSURE_SLOPE_INHG = 0.02

# Wind. Half-hour mean direction against the previous half hour, both
# halves needing a wind worth having a direction at all.
WIND_SPAN_MIN = 30
WIND_SHIFT_DEG = 60.0
WIND_MIN_MPH = 4.0
GUST_MIN_MPH = 15.0

# Temperature turning points. A window's high and low are only a change
# worth a line when the day actually swung.
TEMP_SWING_F = 8.0

# Sun. A sustained crossing of the clear-sky envelope, daylight only.
# The hold has to be OBSERVED to the end of the span: three agreeing
# points in the first two minutes are not twenty minutes (R24-07, 2.4
# release review), so the last mark inside the span must reach within
# SUN_HOLD_GAP_MIN of its end.
SUN_SPAN_MIN = 20
SUN_HOLD_GAP_MIN = 5

KINDS = (
    "rain_started", "rain_stopped", "wind_shift", "pressure_turn",
    "temp_peak", "temp_low", "gust_peak", "cleared", "clouded_over",
)


def _f(row: dict[str, Any], key: str) -> float | None:
    """A finite float, or None. `or 0.0` is the bug this exists to not
    have (ref_absent_is_not_zero)."""
    v = row.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _ms(row: dict[str, Any]) -> int | None:
    v = row.get("dateutc_ms")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _angle_gap(a: float, b: float) -> float:
    """Degrees between two bearings, 0..180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


COMPASS = ("north", "north east", "east", "south east",
           "south", "south west", "west", "north west")


def compass(deg: float) -> str:
    """The eight-point name of a bearing, spelled out. Eight points, not
    sixteen: "north north east" in a one-line timeline entry reads as
    noise, and a wind vane's own accuracy does not earn the extra step."""
    return COMPASS[int((deg % 360.0) / 45.0 + 0.5) % 8]


# What a change's `value` IS, so the app can say it in the reader's
# units instead of showing °F prose beside a °C card (the 2.3 F04 rule,
# highlights.FORMS). API-native, like everything else stored here.
UNITS = ("tempf", "mph", "inhg", "deg")


def _change(at_ms: int, kind: str, title: str, detail: str,
            value: float | None = None,
            unit: str | None = None) -> dict[str, Any]:
    """One entry. `title` and `detail` are the plain-English fallback an
    app that does not know this kind shows as sent, so neither carries a
    number in a unit the reader may not use — the number rides `value`
    with the `unit` it is in."""
    return {"at_ms": int(at_ms), "kind": kind, "title": title,
            "detail": detail, "value": value, "unit": unit}


def _rain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Showers, from the day counter's movement."""
    wet: list[int] = []       # timestamps where the counter advanced
    prev: float | None = None
    last_ms: int | None = None  # the newest row that CARRIED a rain reading
    for row in rows:
        at = _ms(row)
        total = _f(row, "dailyrainin")
        if at is None or total is None:
            continue
        last_ms = at
        if prev is not None and total > prev + 1e-6:
            wet.append(at)
        # A counter that fell is midnight or a manual set: the next
        # comparison starts from the new value, and nothing is claimed
        # about the step itself.
        prev = total
    if not wet:
        return []
    out: list[dict[str, Any]] = []
    gap_ms = RAIN_GAP_MIN * 60_000
    stop_ms = RAIN_STOP_MIN * 60_000
    # The stop clock runs on rain READINGS, not on rows of any kind: a
    # gauge that went quiet after the counter moved is a gauge that went
    # quiet, not dry weather (R24-06, 2.4 release review).
    spells: list[tuple[int, int]] = []
    start = wet[0]
    end = wet[0]
    for at in wet[1:]:
        if at - end > gap_ms:
            spells.append((start, end))
            start = at
        end = at
    spells.append((start, end))
    for begin, finish in spells:
        out.append(_change(begin, "rain_started", "Rain started",
                           "The day's rain counter started moving."))
        # Only call it over once the dry stretch has actually run: a
        # shower that is still going when the window ends has no stop.
        if last_ms is not None and last_ms - finish >= stop_ms:
            out.append(_change(finish, "rain_stopped", "Rain stopped",
                               "No more rain measured after this."))
    return out


def _pressure(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The moment a three-hour barometer trend changed sign."""
    pts = [(at, v) for at, v in
           ((_ms(r), _f(r, "baromrelin")) for r in rows)
           if at is not None and v is not None]
    if len(pts) < MIN_ROWS:
        return []
    span = PRESSURE_SPAN_MIN * 60_000

    # Two pointers, not a rescan per row. `tail` is the newest sample at
    # least three hours behind pts[i], and it only ever moves forward, so
    # the whole pass is linear. The straightforward inner loop is
    # quadratic, which at the 72 hour cap is tens of millions of steps
    # inside an async handler — the same shape as the /current rain scan
    # this release just deleted.
    slopes: list[float | None] = []
    tail = 0
    for at, v in pts:
        while tail + 1 < len(pts) and at - pts[tail + 1][0] >= span:
            tail += 1
        slopes.append(v - pts[tail][1] if at - pts[tail][0] >= span else None)

    best: tuple[float, int, float, float] | None = None
    prev_slope: float | None = None
    for i in range(len(pts)):
        s = slopes[i]
        if s is None:
            continue
        if (prev_slope is not None
                and abs(prev_slope) >= PRESSURE_SLOPE_INHG
                and abs(s) >= PRESSURE_SLOPE_INHG
                and (prev_slope > 0) != (s > 0)):
            strength = abs(prev_slope) + abs(s)
            if best is None or strength > best[0]:
                best = (strength, i, prev_slope, s)
        if s is not None and abs(s) >= PRESSURE_SLOPE_INHG:
            prev_slope = s
    if best is None:
        return []
    _, flip, before, after = best
    rising = after > 0
    # The flip is where the trailing delta changed sign, which is about
    # half a span after the barometer actually turned, and the threshold
    # puts it later still. The turn is the extremum inside the span that
    # ends at the flip: the low for a turn up, the high for a turn down.
    # One bounded scan for the one flip that won (2.4 review).
    at = pts[flip][0]
    j = flip
    while j > 0 and pts[flip][0] - pts[j - 1][0] <= span:
        j -= 1
    window = pts[j:flip + 1]
    at = (min(window, key=lambda p: p[1]) if rising
          else max(window, key=lambda p: p[1]))[0]
    return [_change(
        at, "pressure_turn",
        "Pressure turned " + ("up" if rising else "down"),
        "It had been " + ("falling" if before < 0 else "rising")
        + " for hours before this.",
        round(after, 3), "inhg")]


def _wind(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The biggest half-hour direction swing, and the day's strongest
    gust."""
    out: list[dict[str, Any]] = []
    span = WIND_SPAN_MIN * 60_000
    pts = [(at, d, s) for at, d, s in
           ((_ms(r), _f(r, "winddir"), _f(r, "windspeedmph")) for r in rows)
           if at is not None and d is not None and s is not None]
    # Prefix sums over the samples that HAVE a usable direction, so the
    # vector mean of any half hour is three subtractions and a bisect
    # rather than a scan. The scan version is quadratic and the window
    # can be three days long.
    usable = [(t, d, s) for t, d, s in pts if s >= WIND_MIN_MPH]
    times = [t for t, _d, _s in usable]
    sum_x = [0.0]
    sum_y = [0.0]
    for _t, d, sp in usable:
        sum_x.append(sum_x[-1] + sp * math.cos(math.radians(d)))
        sum_y.append(sum_y[-1] + sp * math.sin(math.radians(d)))

    def mean_over(lo_ms: int, hi_ms: int) -> tuple[float | None, int]:
        """Speed-weighted VECTOR mean direction over [lo, hi), and how
        many samples went into it.

        Vector, not scalar: the scalar mean of 350 and 10 is 180, which
        is the opposite direction, and a shift detector built on that
        reports a swing every time the wind sits on north.
        """
        lo = bisect.bisect_left(times, lo_ms)
        hi = bisect.bisect_left(times, hi_ms)
        n = hi - lo
        if n <= 0:
            return None, 0
        x = sum_x[hi] - sum_x[lo]
        y = sum_y[hi] - sum_y[lo]
        if abs(x) < 1e-9 and abs(y) < 1e-9:
            return None, n
        return math.degrees(math.atan2(y, x)) % 360.0, n

    best: tuple[float, int, float, float] | None = None
    for at, _d, _s in pts:
        a, n_after = mean_over(at, at + span)
        b, n_before = mean_over(at - span, at)
        if n_after < 3 or n_before < 3 or a is None or b is None:
            continue
        gap = _angle_gap(a, b)
        if gap >= WIND_SHIFT_DEG and (best is None or gap > best[0]):
            best = (gap, at, b, a)
    if best is not None:
        _, at, was, now = best
        out.append(_change(
            at, "wind_shift",
            f"Wind swung {compass(was)} to {compass(now)}",
            f"About {round(_angle_gap(was, now))} degrees in half an hour.",
            round(now, 1), "deg"))

    gusts = [(at, g) for at, g in
             ((_ms(r), _f(r, "windgustmph")) for r in rows)
             if at is not None and g is not None]
    if gusts:
        at, top = max(gusts, key=lambda p: p[1])
        if top >= GUST_MIN_MPH:
            out.append(_change(at, "gust_peak", "Strongest gust",
                               "The window's peak gust.", round(top, 1), "mph"))
    return out


def _temperature(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The window's turning points, when the window actually swung."""
    pts = [(at, v) for at, v in
           ((_ms(r), _f(r, "tempf")) for r in rows)
           if at is not None and v is not None]
    if len(pts) < MIN_ROWS:
        return []
    hi_at, hi = max(pts, key=lambda p: p[1])
    lo_at, lo = min(pts, key=lambda p: p[1])
    if hi - lo < TEMP_SWING_F:
        return []
    return [
        _change(hi_at, "temp_peak", "Warmest",
                "The window's high.", round(hi, 1), "tempf"),
        _change(lo_at, "temp_low", "Coolest",
                "The window's low.", round(lo, 1), "tempf"),
    ]


def _sun(rows: list[dict[str, Any]], lat: float | None,
         lon: float | None) -> list[dict[str, Any]]:
    """Sustained crossings of the clear-sky envelope, daylight only.

    Needs coordinates: without them there is no envelope to cross, and a
    raw radiation threshold would call every sunrise a clearing.
    """
    if lat is None or lon is None:
        return []
    marks: list[tuple[int, bool]] = []
    for row in rows:
        at = _ms(row)
        solar = _f(row, "solarradiation")
        if at is None or solar is None:
            continue
        clear = derived.clear_sky_wm2(at, lat, lon)
        sunny = derived.is_sunshine(solar, clear)
        if sunny is None:      # night, or no envelope: no claim
            continue
        marks.append((at, sunny))
    if len(marks) < MIN_ROWS:
        return []
    span = SUN_SPAN_MIN * 60_000
    out: list[dict[str, Any]] = []
    state = marks[0][1]
    times = [t for t, _ in marks]
    for i, (at, sunny) in enumerate(marks):
        if sunny == state:
            continue
        # The new state has to HOLD, or a single cloud crossing the sun
        # writes two lines. The slice ends where the span does — found by
        # bisection on the sorted times, not by walking to the end of the
        # window on every disagreeing sample (2.4 review: a partly cloudy
        # three days at minute density was millions of steps).
        end = bisect.bisect_left(times, at + span, i)
        held = [s for _, s in marks[i:end]]
        if len(held) < 3 or not all(s == sunny for s in held):
            continue
        # ...and held to the end of the span, or an unfinished right
        # edge would be reported as a twenty-minute event (R24-07).
        if times[end - 1] < at + span - SUN_HOLD_GAP_MIN * 60_000:
            continue
        out.append(_change(
            at, "cleared" if sunny else "clouded_over",
            "Cleared up" if sunny else "Clouded over",
            "Sunshine returned." if sunny
            else "The sun went behind cloud and stayed there.")
        )
        state = sunny
    return out


def detect(rows: list[dict[str, Any]], *, lat: float | None = None,
           lon: float | None = None) -> list[dict[str, Any]]:
    """Every change in the window, oldest first.

    `rows` are observation rows ascending by `dateutc_ms`. The result is
    capped: a timeline is something a person reads at a glance, and the
    cap keeps the MOST RECENT entries, which is the half anyone reading
    at breakfast is actually asking about.
    """
    rows = [r for r in rows if _ms(r) is not None]
    rows.sort(key=lambda r: _ms(r) or 0)
    if len(rows) < MIN_ROWS:
        return []
    out = (_rain(rows) + _pressure(rows) + _wind(rows)
           + _temperature(rows) + _sun(rows, lat, lon))
    out.sort(key=lambda c: c["at_ms"])
    return out[-MAX_CHANGES:]


async def _coords(mac: str) -> tuple[float | None, float | None]:
    """The station's coordinates, the same way every other consumer of
    them reads them. Only the sun detector needs them, and it simply
    stays quiet without them."""
    from . import db

    for device in await db.list_devices():
        if device.get("mac") != mac:
            continue
        coords = ((device.get("info") or {}).get("coords") or {}).get("coords") or {}
        lat, lon = coords.get("lat"), coords.get("lon")
        if lat is None or lon is None:
            return None, None
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None, None
    return None, None


async def assemble(mac: str, hours: int = WINDOW_HOURS_DEFAULT,
                   now_ms: int | None = None) -> dict[str, Any]:
    """The timeline for one station, ready to serialize."""
    from . import db

    hours = max(1, min(int(hours), WINDOW_HOURS_MAX))
    end = int(now_ms if now_ms is not None else time.time() * 1000)
    start = end - hours * 3_600_000
    # The whole window, paged (R24-04): one batch left the newest hours
    # of a high-cadence station out while the payload named the full end.
    rows = await db.observation_window(mac, start, end)
    lat, lon = await _coords(mac)
    return {
        "mac": mac,
        "hours": hours,
        "from_ms": start,
        "to_ms": end,
        "changes": detect(rows, lat=lat, lon=lon),
    }
