"""Sun, moon and season math — the almanac the sky stories are built on.

PORTED, NOT REINVENTED. Every formula in this file already existed in this
project and shipped in 1.9, on the app side, in Swift:

  · `SolarMath`  — the standard NOAA sunrise equation, plus the civil
    twilight (−6°) variant the almanac card labels "first light" and
    "last light";
  · `MoonMath`   — mean-synodic phase and illumination, and the truncated
    Meeus lunar position that the moonrise/moonset rows scan;
  · `SeasonMath` — Meeus's mean-equinox polynomials for the solstice and
    equinox instants.

They are transcribed here CONSTANT FOR CONSTANT — same epochs, same
coefficients, same horizon angles, same 10-minute-scan-then-bisect method
for the moon — because the app and the server must never print two
different sunsets for one backyard. A share card generated on the server
sits beside the almanac card in the same app; a second, subtly different
sun-position implementation would show up as a one-minute disagreement that
nobody could explain and everybody would notice. If either side is ever
improved, the other has to move with it, and that is the point of saying so
here rather than quietly deriving it again.

WHY THE SERVER NEEDS IT AT ALL: the story engine writes every string a card
shows (ImageRenderer cannot take env objects off-tree), so "sunset 7:12 pm,
last light 7:37 pm" has to be composed here. The app cannot be asked to fill
it in afterwards — that is the same rule that put unit conversion in
`stories.Units`.

Accuracy is the almanac's, not an ephemeris's: sun events land within about
a minute, moon events within five to ten, and the mean synodic phase drifts
up to ±0.6 days from the true lunation. That is the honesty class of a
printed calendar, which is what these cards are.

PURITY: nothing in this module reads a clock, a database or a setting. Every
entry point takes the instant or the local day it is asked about, so the
story engine stays reproducible under its pinned `climate.local_today`
anchor — a producer that called `datetime.now()` here would be the one thing
in the engine that moved between two calls in the same second.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, tzinfo

UTC = timezone.utc

# The sun's centre 0.83° BELOW the geometric horizon: half the disc's
# apparent diameter plus standard refraction. This is the definition of
# "sunrise" every almanac uses.
SUNRISE_ANGLE = -0.83
# Civil twilight. The sky is still usefully bright, which is why the app
# calls these two "first light" and "last light" rather than dawn and dusk.
CIVIL_ANGLE = -6.0

# The moon's parallax slightly beats its refraction, so it is already up
# when its centre is a hair ABOVE the geometric horizon. Same number the
# app scans with.
MOON_HORIZON_DEG = 0.125

# The scan step for every moon crossing, in seconds. Ten minutes is coarse
# enough to be cheap over a whole night and fine enough that the moon (which
# moves ~0.5° an hour against the horizon at worst) cannot rise and set
# again between two samples at any latitude a weather station lives at.
MOON_STEP_S = 600.0
# Halvings after a bracket is found. Twenty takes a ten-minute bracket below
# a millisecond, which is far past the honesty of the position model but
# costs nothing and keeps the result stable under re-runs.
_BISECT_STEPS = 20

# A mean synodic month and a well-known new moon (2000-01-06 18:14 UTC) —
# MoonMath's epoch, to the second.
SYNODIC_DAYS = 29.530588853
_MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=UTC)
# J2000.0 proper (2000-01-01 12:00 UTC), the lunar position elements' epoch.
_J2000 = datetime(2000, 1, 1, 12, 0, tzinfo=UTC)

# Sun states for a given local day. "always_up" and "always_down" are
# MEASURED astronomical facts about a place, not missing data — a polar-night
# day genuinely has zero daylight, and that distinction is why this returns a
# state rather than None (absent is not zero, and here zero is not absent).
SUN_NORMAL = "normal"
SUN_ALWAYS_UP = "always_up"
SUN_ALWAYS_DOWN = "always_down"

MARCH_EQUINOX = "march_equinox"
JUNE_SOLSTICE = "june_solstice"
SEPTEMBER_EQUINOX = "september_equinox"
DECEMBER_SOLSTICE = "december_solstice"
SEASON_EVENTS = (MARCH_EQUINOX, JUNE_SOLSTICE, SEPTEMBER_EQUINOX,
                 DECEMBER_SOLSTICE)
SOLSTICES = (JUNE_SOLSTICE, DECEMBER_SOLSTICE)
EQUINOXES = (MARCH_EQUINOX, SEPTEMBER_EQUINOX)

# Northern-hemisphere names, matching the app's `SeasonMath.Event.name`. The
# INSTANTS are hemisphere-free; only these words assume a hemisphere, and
# they are the words already on the almanac card.
SEASON_NAMES = {
    MARCH_EQUINOX: "Spring Equinox",
    JUNE_SOLSTICE: "Summer Solstice",
    SEPTEMBER_EQUINOX: "Fall Equinox",
    DECEMBER_SOLSTICE: "Winter Solstice",
}


# ───────────────────────── local-day helpers ─────────────────────────

def local_midnight(day: date, tz: tzinfo) -> datetime:
    """The instant the local day `day` begins.

    In the handful of zones that shift the clock AT midnight, the nominal
    local midnight can be a time that does not exist; `astimezone` resolves
    it by the fold rules and the answer is off by an hour on one day a year,
    which is smaller than the moon-position error this module already
    admits to.
    """
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def local_day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """(start, end) of a local day as absolute instants.

    Local days run 23–25 hours across a DST transition. A fixed 24-hour
    window misses a late event on the long day and steals the next day's on
    the short one — the same defect the app's moon scan was corrected for.
    """
    start = local_midnight(day, tz)
    return start, local_midnight(day + timedelta(days=1), tz)


# ───────────────────────── the sun ─────────────────────────

def julian_day(year: int, month: int, day: int) -> float:
    """Julian Day Number at 0h UTC for a Gregorian calendar date."""
    y, m = year, month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716))
            + math.floor(30.6001 * (m + 1))
            + day + b - 1524.5)


def _solar_elements(lat: float, lon: float, on: date,
                    angle_deg: float) -> tuple[float, float] | None:
    """(julian transit, cos of the hour angle) for the UTC day of `on`.

    Split out of `sun_event` because the polar check needs the cosine and
    the event needs both — computing them twice would be two places for the
    convention (longitude positive EAST) to drift apart.
    """
    jd0h = julian_day(on.year, on.month, on.day)
    # ceil so the midnight-UTC JD maps to the calendar day in question
    # rather than the previous noon-anchored half-day; 0.0008 for leap
    # seconds. See the sunrise-equation reference the app cites.
    n = math.ceil(jd0h - 2451545.0 + 0.0008)
    # Longitude positive east, negative west. A western longitude means the
    # sun is "behind" Greenwich, so noon-UTC happens later.
    j_star = n - lon / 360.0
    m_deg = math.fmod(357.5291 + 0.98560028 * j_star, 360.0)
    m_rad = math.radians(m_deg)
    c = (1.9148 * math.sin(m_rad)
         + 0.0200 * math.sin(2 * m_rad)
         + 0.0003 * math.sin(3 * m_rad))
    lam = math.fmod(m_deg + c + 180 + 102.9372, 360.0)
    lam_rad = math.radians(lam)
    j_transit = (2451545.0 + j_star
                 + 0.0053 * math.sin(m_rad)
                 - 0.0069 * math.sin(2 * lam_rad))
    delta = math.asin(math.sin(lam_rad) * math.sin(math.radians(23.4397)))
    lat_rad = math.radians(lat)
    numer = (math.sin(math.radians(angle_deg))
             - math.sin(lat_rad) * math.sin(delta))
    denom = math.cos(lat_rad) * math.cos(delta)
    if denom == 0:
        # Exactly at a pole: the hour angle is undefined because the sun
        # never crosses the horizon there at all. The sign of the numerator
        # still says WHICH side it is stuck on, so this degenerates into the
        # polar-day / polar-night answer rather than into a missing value.
        return j_transit, math.copysign(math.inf, numer) if numer else 0.0
    return j_transit, numer / denom


def _from_julian(j: float) -> datetime:
    return datetime.fromtimestamp((j - 2440587.5) * 86400.0, tz=UTC)


def sun_event(lat: float, lon: float, on: date, *, rising: bool,
              angle_deg: float = SUNRISE_ANGLE) -> datetime | None:
    """The UTC instant of one solar event for the UTC calendar day of `on`.

    None when the sun does not cross that altitude on that day at that
    latitude. The caller picks the right day — see `sun_event_local`.
    """
    el = _solar_elements(lat, lon, on, angle_deg)
    if el is None:
        return None
    j_transit, cos_omega = el
    if not -1.0 <= cos_omega <= 1.0:
        return None
    omega = math.acos(cos_omega)
    frac = omega / (2 * math.pi)
    return _from_julian(j_transit - frac if rising else j_transit + frac)


def sun_state(lat: float, lon: float, on: date, *,
              angle_deg: float = SUNRISE_ANGLE) -> str:
    """Whether the sun rises and sets at all on this day here.

    The polar cases are the reason this exists: a day on which the sun never
    sets has 24 hours of daylight and a day on which it never rises has
    zero, and both are things this station's sky ACTUALLY DID. Reporting
    either as a missing value would be the mirror of the bug this repo keeps
    re-shipping — here, absent really is different from zero, in the other
    direction.
    """
    el = _solar_elements(lat, lon, on, angle_deg)
    if el is None:                                  # unreachable; guarded
        return SUN_NORMAL
    _, cos_omega = el
    if cos_omega < -1.0:
        return SUN_ALWAYS_UP
    if cos_omega > 1.0:
        return SUN_ALWAYS_DOWN
    return SUN_NORMAL


def sun_event_local(lat: float, lon: float, day: date, *, rising: bool,
                    angle_deg: float = SUNRISE_ANGLE,
                    tz: tzinfo = UTC) -> datetime | None:
    """The solar event that lands inside the LOCAL day `day`.

    Probing yesterday/today/tomorrow in UTC handles negative-offset zones,
    where a local day straddles two UTC days. When none of the three probes
    lands inside the local window — reachable near the polar day/night
    transitions — the UTC-day event is returned anyway, deliberately: rise
    and set then stay all-or-nothing, where returning None for only one of
    them would produce a day with a sunrise and no sunset.
    """
    start, end = local_day_bounds(day, tz)
    for offset in (-1, 0, 1):
        got = sun_event(lat, lon, day + timedelta(days=offset),
                        rising=rising, angle_deg=angle_deg)
        if got is not None and start <= got < end:
            return got
    return sun_event(lat, lon, day, rising=rising, angle_deg=angle_deg)


def sunrise(lat: float, lon: float, day: date,
            tz: tzinfo = UTC) -> datetime | None:
    return sun_event_local(lat, lon, day, rising=True, tz=tz)


def sunset(lat: float, lon: float, day: date,
           tz: tzinfo = UTC) -> datetime | None:
    return sun_event_local(lat, lon, day, rising=False, tz=tz)


def first_light(lat: float, lon: float, day: date,
                tz: tzinfo = UTC) -> datetime | None:
    """Civil dawn — the sky starts to brighten about half an hour before
    the sun itself appears."""
    return sun_event_local(lat, lon, day, rising=True,
                           angle_deg=CIVIL_ANGLE, tz=tz)


def last_light(lat: float, lon: float, day: date,
               tz: tzinfo = UTC) -> datetime | None:
    """Civil dusk. The start of a usable night, which is what the dark-sky
    window is measured between."""
    return sun_event_local(lat, lon, day, rising=False,
                           angle_deg=CIVIL_ANGLE, tz=tz)


def solar_noon(lat: float, lon: float, day: date,
               tz: tzinfo = UTC) -> datetime | None:
    rise = sunrise(lat, lon, day, tz)
    fall = sunset(lat, lon, day, tz)
    if rise is None or fall is None:
        return None
    return datetime.fromtimestamp(
        (rise.timestamp() + fall.timestamp()) / 2, tz=UTC)


def daylight_seconds(lat: float, lon: float, day: date,
                     tz: tzinfo = UTC) -> float | None:
    """How long the sun is above the horizon on this local day.

    86400 under a midnight sun and 0.0 through a polar night — both are
    measurements, not gaps. None only when the geometry produced no usable
    pair, which is the one case a caller must treat as "we do not know".
    """
    state = sun_state(lat, lon, day)
    if state == SUN_ALWAYS_UP:
        return 86400.0
    if state == SUN_ALWAYS_DOWN:
        return 0.0
    rise = sunrise(lat, lon, day, tz)
    fall = sunset(lat, lon, day, tz)
    if rise is None or fall is None:
        return None
    span = (fall - rise).total_seconds()
    return span if span > 0 else None


# ───────────────────────── the moon ─────────────────────────

def moon_phase_fraction(when: datetime) -> float:
    """Position in the mean synodic cycle, [0, 1): 0 = new, 0.5 = full.

    Dates before the epoch fold back into range, so the historical end of a
    long record is as well behaved as the running end.
    """
    days = (when - _MOON_EPOCH).total_seconds() / 86400.0
    f = math.fmod(days / SYNODIC_DAYS, 1.0)
    return f + 1.0 if f < 0 else f


def moon_illumination(when: datetime) -> float:
    """Lit fraction of the disc, [0, 1] — the cosine curve, not the phase
    fraction. A first-quarter moon is HALF lit at phase 0.25; a linear map
    would call it 25% and the number would be wrong on the card."""
    return (1 - math.cos(2 * math.pi * moon_phase_fraction(when))) / 2


def moon_age_days(when: datetime) -> float:
    return moon_phase_fraction(when) * SYNODIC_DAYS


def days_to_full(when: datetime) -> float:
    """Days until the next full moon, (0, 29.53]."""
    delta = 0.5 - moon_phase_fraction(when)
    return (delta if delta > 0 else delta + 1) * SYNODIC_DAYS


# The eight traditional phases, bucketed by rounding to the nearest eighth
# so each cardinal name spans the days the disc actually looks that way —
# "Full Moon" covers about 3.7 days, which is what calendars print.
_PHASE_NAMES = ("New Moon", "Waxing Crescent", "First Quarter",
                "Waxing Gibbous", "Full Moon", "Waning Gibbous",
                "Last Quarter", "Waning Crescent")


def moon_phase_name(when: datetime) -> str:
    # floor(x + 0.5), not round(): Python rounds halves to EVEN and Swift
    # rounds them away from zero, so `round()` here would put the app and the
    # server in different phase buckets on the exact boundaries — one glyph
    # apart, once in a while, for no reason a reader could ever work out.
    return _PHASE_NAMES[int(math.floor(moon_phase_fraction(when) * 8 + 0.5)) % 8]


def moon_altitude_deg(lat: float, lon: float, when: datetime) -> float:
    """Geocentric moon altitude for an observer, degrees.

    The low-precision lunar position: mean elements plus the dominant
    evection term. Good to about a degree, which lands a rise or set within
    five to ten minutes — the almanac prints minutes, so that bar is met.
    """
    d = (when - _J2000).total_seconds() / 86400.0
    lon_mean = 218.316 + 13.176396 * d          # mean longitude
    anomaly = 134.963 + 13.064993 * d           # mean anomaly
    arg_lat = 93.272 + 13.229350 * d            # argument of latitude
    ecl_lon = math.radians(lon_mean + 6.289 * math.sin(math.radians(anomaly)))
    ecl_lat = math.radians(5.128 * math.sin(math.radians(arg_lat)))
    e = math.radians(23.4397)
    ra = math.atan2(math.sin(ecl_lon) * math.cos(e)
                    - math.tan(ecl_lat) * math.sin(e), math.cos(ecl_lon))
    dec = math.asin(math.sin(ecl_lat) * math.cos(e)
                    + math.cos(ecl_lat) * math.sin(e) * math.sin(ecl_lon))
    lst = math.radians(280.16 + 360.9856235 * d + lon)
    h = lst - ra
    phi = math.radians(lat)
    return math.degrees(math.asin(
        math.sin(phi) * math.sin(dec)
        + math.cos(phi) * math.cos(dec) * math.cos(h)))


def _bisect(lo: datetime, hi: datetime, *, rising: bool,
            alt) -> datetime:
    for _ in range(_BISECT_STEPS):
        mid = datetime.fromtimestamp(
            (lo.timestamp() + hi.timestamp()) / 2, tz=UTC)
        if (alt(mid) > 0) == rising:
            hi = mid
        else:
            lo = mid
    return hi


def moon_rise_set(lat: float, lon: float, day: date,
                  tz: tzinfo = UTC) -> tuple[datetime | None, datetime | None]:
    """Moonrise and moonset inside the LOCAL day `day`.

    Found by scanning altitude and bisecting each horizon crossing rather
    than solving for it, which is why the awkward days degrade honestly
    instead of lying: the moon rises roughly fifty minutes later each day,
    so a local day with NO moonrise (or no moonset) in it is ordinary,
    happens about once a month, and returns None for that half of the pair.
    """
    start, end = local_day_bounds(day, tz)

    def alt(t: datetime) -> float:
        return moon_altitude_deg(lat, lon, t) - MOON_HORIZON_DEG

    rise: datetime | None = None
    fall: datetime | None = None
    steps = max(1, round((end - start).total_seconds() / MOON_STEP_S))
    prev_t, prev_a = start, alt(start)
    for i in range(1, steps + 1):
        t = min(start + timedelta(seconds=MOON_STEP_S * i), end)
        a = alt(t)
        if prev_a <= 0 < a and rise is None:
            rise = _bisect(prev_t, t, rising=True, alt=alt)
        elif a <= 0 < prev_a and fall is None:
            fall = _bisect(prev_t, t, rising=False, alt=alt)
        prev_t, prev_a = t, a
    return rise, fall


def moon_below_intervals(lat: float, lon: float, start: datetime,
                         end: datetime) -> list[tuple[datetime, datetime]]:
    """Every stretch between `start` and `end` with the moon below the
    horizon, in order.

    THE EDGE CASES ARE THE WHOLE REASON THIS EXISTS. Deriving a moon-free
    window from a moonrise and a moonset means reasoning about a moon that
    never rises, never sets, is up the whole time, rises before the window
    and sets after it, or crosses midnight — five branches, each of which
    has to know which calendar day it is talking about. Scanning the
    altitude over the ACTUAL INTERVAL has none of those branches: a night
    the moon spends underground returns one interval covering all of it, a
    night the moon spends up returns an empty list, and a window that
    straddles midnight is just an interval between two instants. The clock
    never enters into it.

    Intervals are clamped to the requested bounds, so a moon that set before
    the window opened yields an interval starting exactly at `start`.
    """
    if end <= start:
        return []

    def alt(t: datetime) -> float:
        return moon_altitude_deg(lat, lon, t) - MOON_HORIZON_DEG

    out: list[tuple[datetime, datetime]] = []
    prev_t, prev_a = start, alt(start)
    open_at: datetime | None = start if prev_a <= 0 else None
    steps = max(1, math.ceil((end - start).total_seconds() / MOON_STEP_S))
    for i in range(1, steps + 1):
        t = min(start + timedelta(seconds=MOON_STEP_S * i), end)
        a = alt(t)
        if a <= 0 < prev_a:                       # the moon set
            open_at = _bisect(prev_t, t, rising=False, alt=alt)
        elif prev_a <= 0 < a:                     # the moon rose
            if open_at is not None:
                out.append((open_at, _bisect(prev_t, t, rising=True, alt=alt)))
                open_at = None
        prev_t, prev_a = t, a
    if open_at is not None:
        out.append((open_at, end))
    return out


def darkest_window(lat: float, lon: float, start: datetime,
                   end: datetime) -> tuple[datetime, datetime] | None:
    """The LONGEST moon-free stretch of the night, or None when the moon is
    up for all of it.

    Longest rather than first: on the rare night the moon sets and rises
    again inside one window, the piece worth naming is the big one.
    """
    intervals = moon_below_intervals(lat, lon, start, end)
    if not intervals:
        return None
    return max(intervals, key=lambda iv: (iv[1] - iv[0]).total_seconds())


# ───────────────────────── the seasons ─────────────────────────

# Meeus's mean-equinox polynomials, good to about half an hour. The almanac
# counts days, so that is two orders of magnitude of headroom.
_SEASON_TERMS = {
    MARCH_EQUINOX:     (2451623.80984, 365242.37404, 0.05169),
    JUNE_SOLSTICE:     (2451716.56767, 365241.62603, 0.00325),
    SEPTEMBER_EQUINOX: (2451810.21715, 365242.01767, -0.11575),
    DECEMBER_SOLSTICE: (2451900.05952, 365242.74049, -0.06223),
}


def season_instant(event: str, year: int) -> datetime:
    """The UTC instant of one solstice or equinox."""
    a, b, c = _SEASON_TERMS[event]
    y = (year - 2000) / 1000.0
    return _from_julian(a + b * y + c * y * y)


def next_season(after: datetime) -> tuple[str, datetime]:
    """The first solstice or equinox strictly after `after`."""
    best: tuple[str, datetime] | None = None
    for y in (after.year - 1, after.year, after.year + 1):
        for event in SEASON_EVENTS:
            when = season_instant(event, y)
            if when > after and (best is None or when < best[1]):
                best = (event, when)
    assert best is not None      # three years scanned; one always qualifies
    return best


def previous_season(before: datetime,
                    events: tuple[str, ...] = SEASON_EVENTS
                    ) -> tuple[str, datetime]:
    """The last of `events` at or before `before`.

    `events` narrows it to the solstices for the story that measures how
    much daylight has come or gone since the sun last turned around.
    """
    best: tuple[str, datetime] | None = None
    for y in (before.year - 1, before.year, before.year + 1):
        for event in events:
            when = season_instant(event, y)
            if when <= before and (best is None or when > best[1]):
                best = (event, when)
    assert best is not None
    return best


__all__ = [
    "CIVIL_ANGLE", "DECEMBER_SOLSTICE", "EQUINOXES", "JUNE_SOLSTICE",
    "MARCH_EQUINOX", "MOON_HORIZON_DEG", "SEASON_EVENTS", "SEASON_NAMES",
    "SEPTEMBER_EQUINOX", "SOLSTICES", "SUNRISE_ANGLE", "SUN_ALWAYS_DOWN",
    "SUN_ALWAYS_UP", "SUN_NORMAL", "SYNODIC_DAYS", "darkest_window",
    "daylight_seconds", "days_to_full", "first_light", "julian_day",
    "last_light", "local_day_bounds", "local_midnight",
    "moon_altitude_deg", "moon_age_days", "moon_below_intervals",
    "moon_illumination", "moon_phase_fraction", "moon_phase_name",
    "moon_rise_set", "next_season", "previous_season", "season_instant",
    "solar_noon", "sun_event", "sun_event_local", "sun_state", "sunrise",
    "sunset",
]
