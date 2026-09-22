"""Per sensor calibration offsets (2.4, item 7).

Every station in a real backyard reads a little wrong somewhere. The
outdoor AirGradient here reads hot by design; a rain gauge that is not
quite level collects a little short; a barometer that has never been set
against a nearby airport is out by a tenth for its whole life. Every
other weather system has a correction table for this, and until now this
one's answer was to live with it.

WHERE THE CORRECTION IS APPLIED, and why that is the interesting part.

It is applied at INGEST, and the corrected value is what gets stored.
The alternative, storing raw and correcting on read, sounds tidier and
is not: rollups, records, insights, alerts, exports and the rain ledger
all read stored values, and a correction that lived only in the display
layer would mean every one of those disagreed with the number on the
screen. Every other system that does this — WeeWX's StdCalibrate, the
Ecowitt gateway's own offsets, the Davis console — applies at ingest for
the same reason.

The cost is honest and stated: a correction changes the future, not the
past. What the row carries is the correction that was applied to IT, in
data_json, so a reading can always be turned back into what the sensor
actually said.

Offsets only, deliberately, plus a scale for the one case that needs it.
A rain gauge under-collects by a percentage, which is a multiplier; a
thermometer sits warm by a constant, which is an offset. Nothing here
fits a curve, because a curve fitted to a backyard is a way to make bad
data look convincing.

WHAT A SCALE MAY NOT TOUCH: a cumulative rain counter. The ledger reads
rain as the RISE of a counter, so the moment a scale on `yearlyrainin`
changes, the stored counter steps by the difference with no rain falling
and the day is credited that step. R24-01 (2.4 release review) showed it
with an unchanged 2.0 in counter and a scale of 1.1: 0.20 in of rain on
a dry day. That is an ingest offset in disguise, which this project has
banned for good (a manual counter set is not rain). The rate and the
trailing-hour total are not folded as counters and stay correctable; a
gauge that under-collects is corrected where every other system corrects
it, at the gauge, or lived with.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("zasder.calibration")

# Fields a correction may touch. The list is the readings a person can
# actually check against something else: a thermometer against a second
# thermometer, a gauge against a manual one, a barometer against the
# airport. Wind direction is not here because an offset on a compass is
# a mounting problem, and a derived field is not here because it is
# derived again from the corrected inputs (`rederive`) rather than
# corrected on its own.
CALIBRATABLE: dict[str, str] = {
    "tempf": "offset",
    "tempinf": "offset",
    "humidity": "offset",
    "humidityin": "offset",
    "baromrelin": "offset",
    "baromabsin": "offset",
    "windspeedmph": "scale",
    "windgustmph": "scale",
    "solarradiation": "scale",
    "uv": "scale",
    # Rain is a scale: a gauge under-collects by a proportion of what
    # fell, not by a fixed amount per reading. The counters are lifetime
    # totals, so a scale applies to the whole of each reading and stays
    # consistent with the rise-based reads ([[rain counters]]).
    # Never a cumulative counter (dailyrainin, eventrainin, weeklyrainin,
    # monthlyrainin, yearlyrainin, totalrainin): see the module docstring.
    "hourlyrainin": "scale",
    "rainratein": "scale",
    # Soil, because a probe in one bed reads differently from a probe in
    # the next and people do calibrate them against a hand meter.
    **{f"soilhum{i}": "offset" for i in range(1, 9)},
    **{f"soiltemp{i}f": "offset" for i in range(1, 9)},
}

# What a correction is allowed to be. Wide enough for a real sensor that
# is out, narrow enough that a typo cannot rewrite the weather: nobody's
# thermometer is fifty degrees wrong, and a gauge that needs doubling is
# broken rather than uncalibrated.
OFFSET_LIMIT = 25.0
SCALE_MIN, SCALE_MAX = 0.5, 2.0

# A percentage stays a percentage. A hygrometer that reads low is exactly
# the one being corrected upward, and saturation is when the correction
# matters: 98 + 3 stores as 100, not as a 101 the plausibility band
# nulls along with the dew point derived from it.
_PERCENT: frozenset[str] = frozenset(
    {"humidity", "humidityin", *(f"soilhum{i}" for i in range(1, 9))})

# What a console derives from its own inputs. AWN, Tempest and Davis all
# send a dew point and a feels-like computed from the RAW temperature,
# so after a correction those describe a reading that no longer exists.
# They are derived again from the corrected inputs, here for the pollers
# that write straight to the store and by the ingest path for the rest.
_DERIVED_FROM: dict[str, tuple[str, ...]] = {
    "tempf": ("dewPoint", "feelsLike"),
    "humidity": ("dewPoint", "feelsLike"),
    "windspeedmph": ("feelsLike",),
    "tempinf": ("dewPointin",),
    "humidityin": ("dewPointin",),
}


def clean(raw: Any) -> dict[str, float]:
    """A stored or submitted correction table, cleaned.

    A field nobody can correct, a correction out of bounds, or a value
    that is not a number is DROPPED rather than clamped. Clamping would
    store something the person did not ask for and then apply it to
    every reading forever.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for field, value in raw.items():
        kind = CALIBRATABLE.get(field)
        if kind is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        if kind == "offset":
            if abs(v) > OFFSET_LIMIT or v == 0.0:
                continue
        else:
            if not (SCALE_MIN <= v <= SCALE_MAX) or v == 1.0:
                continue
        out[field] = v
    return out


def describe(field: str, value: float) -> str:
    """One correction, in words, for an API that has to explain itself."""
    if CALIBRATABLE.get(field) == "scale":
        return f"{field} × {value:g}"
    return f"{field} {value:+g}"


def apply(flat: dict[str, Any], table: dict[str, float]) -> dict[str, float]:
    """Correct a reading in place. Returns what was actually applied.

    A field the reading does not carry is not corrected into existence:
    an offset on a sensor that reported nothing must not turn absence
    into a number ([[absent is not zero]]).
    """
    applied: dict[str, float] = {}
    for field, value in table.items():
        kind = CALIBRATABLE.get(field)
        if kind is None or field not in flat:
            continue
        current = flat.get(field)
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            continue
        corrected = (current + value) if kind == "offset" else (current * value)
        if corrected != corrected:          # NaN in, nothing out
            continue
        if field in _PERCENT:
            corrected = min(100.0, max(0.0, corrected))
        flat[field] = corrected
        applied[field] = value
    return applied


def rederived(applied: dict[str, float]) -> set[str]:
    """The source-derived fields a correction of these inputs replaces."""
    stale: set[str] = set()
    for field in applied:
        stale.update(_DERIVED_FROM.get(field, ()))
    return stale


def rederive(flat: dict[str, Any], applied: dict[str, float]) -> None:
    """Replace what the source derived from an input that just changed.

    A dew point or feels-like the console computed from the raw
    temperature is dropped and computed again from the corrected pair;
    one whose inputs the reading does not carry is dropped and stays
    absent, because a number nobody can recompute is not a reading.
    """
    stale = rederived(applied)
    if not stale:
        return
    from . import derived
    from .ingest import _compute_feels_like
    for field in stale:
        if field in flat:
            flat[field] = None
    if "dewPoint" in stale and flat.get("tempf") is not None \
            and flat.get("humidity") is not None:
        v = derived.dew_point_f(flat["tempf"], flat["humidity"])
        flat["dewPoint"] = None if v is None else round(v, 1)
    if "dewPointin" in stale and flat.get("tempinf") is not None \
            and flat.get("humidityin") is not None:
        v = derived.dew_point_f(flat["tempinf"], flat["humidityin"])
        flat["dewPointin"] = None if v is None else round(v, 1)
    if "feelsLike" in stale:
        flat["feelsLike"] = _compute_feels_like(
            flat.get("tempf"), flat.get("humidity"), flat.get("windspeedmph"))


def correct_with(flat: dict[str, Any], table: dict[str, float]) -> dict[str, float]:
    """Apply a station's table to one reading and stamp what was done.

    The one chokepoint. /ingest, the AmbientWeather poller and the
    Ecowitt bootstrap all go through here, because the first release of
    this feature corrected only /ingest and a cloud station's route
    answered 200 while every stored row stayed raw."""
    if not table:
        return {}
    applied = apply(flat, table)
    if applied:
        rederive(flat, applied)
        flat["calibration"] = applied
    return applied


async def correct(mac: str, flat: dict[str, Any]) -> dict[str, float]:
    """`correct_with`, reading the station's table first."""
    from . import db
    return correct_with(flat, await db.get_calibration(mac))
