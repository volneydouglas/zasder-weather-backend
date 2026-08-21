"""Custom-source ingest endpoint.

Accepts a normalized observation from any external source (the acurite-relay
container, a custom SDR pipeline, an Ecowitt receiver, etc.). The shape is
the one acurite-relay's `parse_observation` produces:

  {
    "device": {
      "id": "24C86E0A66F5",   # MAC, raw or colonized
      "model": "Atlas",
      "sensor_id": "00000711",
      "rssi": 4,
      "battery_outdoor": "normal",
      "battery_hub": "low"
    },
    "timestamp_utc": "2026-05-14T01:09:47",
    "outdoor": { "tempf": 98.3, "humidity": 8, "feels_like": 94, ... },
    "wind": { "speed_mph": 7, "gust_mph": 7, "direction": 224, ... },
    "rain": { "hourly_in": 0, "daily_in": 0, ... },
    "pressure": { "relative_inhg": 29.9 },
    "lightning": { ... },
    "source": "acurite-atlas",
    "received_iso": "..."
  }

We flatten it into the existing observations table columns the iOS app
already reads, plus persist the full normalized record as data_json so we
don't lose source-specific bonus fields (lightning, hub battery, etc).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import math
import re
import time
import weakref
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request

from . import db
from . import device_probation
from . import source_status
from . import wu_upload
from .config import settings, tokens_match

log = logging.getLogger("ingest")

router = APIRouter()

# Timestamp sanity bounds (see _flatten). 15 min of forward skew tolerates a
# sloppy source clock; 400 days back is generous for any live sensor while
# still keeping garbage out of the records/aggregate windows.
_FUTURE_SKEW_MS = 15 * 60 * 1000
_PAST_HORIZON_MS = 400 * 24 * 3600 * 1000


def _finite(v: Any) -> Any:
    """Return v if it's a finite number, None if it's NaN/inf, pass-through
    for anything that isn't a number. Stops upstream decoders from poisoning
    stored observations with non-finite floats that crash JSONResponse on
    the read path (`/api/devices/{mac}/current` raises ValueError otherwise)."""
    if isinstance(v, bool):  # bools are int subclass — leave alone
        return v
    if isinstance(v, (int, float)):
        try:
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None
    return v


def _scrub_numbers(block: Any) -> dict[str, Any]:
    """Filter all numeric values in a sub-block (outdoor/wind/etc.) through
    _finite so non-finite values never reach the DB.

    Non-dict blocks are dropped rather than trusted: a buggy encoder posting
    `"outdoor": [1, 2]` used to raise AttributeError here, i.e. a 500 on every
    reading from that source instead of a 400 it could act on."""
    if not isinstance(block, dict):
        return {}
    return {k: _finite(v) for k, v in block.items()}


def _coerce_num(v: Any) -> Any:
    """Final numeric coercion for the flat metric fields. Real numbers pass
    through (already scrubbed by _scrub_numbers); a numeric STRING coerces
    ("75.5" → 75.5, a buggy decoder's stringified reading); anything else —
    including "1e999", which float()s to inf — becomes None. Without this,
    an overflow string bound into a REAL column is coerced to Inf by
    SQLite's affinity rules at insert, and AVG()/MIN()/MAX() over it 500
    /records, /summary and bucketed /history permanently (JSONResponse
    serializes with allow_nan=False). Same bug class the yearly_in path
    fixed for itself; this closes it for every metric field."""
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _battery_flag(v: Any) -> int | None:
    """Relay battery state → AWN's battout/battin convention (1 = ok,
    0 = low). Anything unrecognized stays None rather than guessing."""
    if v == "normal":
        return 1
    if v == "low":
        return 0
    return None


def _flatten(normalized: dict[str, Any]) -> dict[str, Any] | None:
    """Map a normalized observation → the flat-field shape db.insert_observations
    expects (same keys as AmbientWeather's REST response)."""
    dev = _dev_block(normalized)
    # Filter NaN/inf out of every numeric sub-block at the boundary so non-
    # finite values never reach the DB or downstream JSON serialization.
    out = _scrub_numbers(normalized.get("outdoor"))
    ind = _scrub_numbers(normalized.get("indoor"))
    wind = _scrub_numbers(normalized.get("wind"))
    rain = _scrub_numbers(normalized.get("rain"))
    press = _scrub_numbers(normalized.get("pressure"))
    # Solar comes in two shapes: LilyGO/cloud pollers put solar_wm2 + uv
    # inside `outdoor`; the WLL local poller posts a dedicated `solar`
    # block ({radiation_wm2, uv}). Accept both — the block was silently
    # dropped before, so WLL-sourced devices never stored solar at all.
    solar = _scrub_numbers(normalized.get("solar"))
    # Lightning: Tempest reports it, AcuRite Atlas can too. This block was
    # being DROPPED — the same silent loss the `solar` note above records,
    # and the module docstring's claim that bonus fields survive in data_json
    # was false, because data_json stores this flattened dict rather than the
    # original payload. Carried through as flat lightning_* keys so it lands
    # in data_json today; dedicated columns can follow.
    lightning = _scrub_numbers(normalized.get("lightning"))

    ts_iso = normalized.get("timestamp_utc")
    # A non-string timestamp (an epoch int, a dict) hit .endswith() below and
    # raised AttributeError, which the except clause didn't catch — a 500
    # instead of the 400 the caller needs.
    if not ts_iso or not isinstance(ts_iso, str):
        return None
    # "2026-05-14T01:09:47" or "2026-05-14T01:09:47Z" → epoch ms
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(ts_iso[:-1] if ts_iso.endswith("Z") else ts_iso)
        # An offset-bearing timestamp ("...+02:00") parses AWARE, and
        # .replace(tzinfo=utc) would re-LABEL the wall clock instead of
        # converting it — a silent multi-hour error stored as fact.
        t = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
        dateutc_ms = int(t.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    # Sanity-bound the timestamp. A far-FUTURE value would be stored as fact:
    # devices.last_seen_ms jumps ahead (silencing the staleness monitor) and
    # the write-throttle then rejects every real reading until that moment
    # arrives. Clamp to server time (>15 min skew is a broken clock, not data)
    # so the reading itself survives. A far-PAST value (>~400 days) pollutes
    # records/aggregate windows and can't be a live reading — reject it.
    now_ms = int(time.time() * 1000)
    raw_dateutc_ms = dateutc_ms      # pre-clamp, for rejection-confirmation identity
    if dateutc_ms > now_ms + _FUTURE_SKEW_MS:
        log.warning("timestamp %s is %.1f min in the future — clamping to "
                    "server time", ts_iso, (dateutc_ms - now_ms) / 60000)
        dateutc_ms = now_ms
    elif dateutc_ms < now_ms - _PAST_HORIZON_MS:
        return None

    # Indoor block is optional — historically the AcuRite hub-relay never
    # had access to indoor data (the hub sensor was elsewhere). The SDR path
    # using a paired indoor sensor (Fineoffset-WH32B, etc.) sends it here.
    # Pressure is unusual — it physically comes from indoors (the console)
    # but is logically reported as "outdoor barometric"; accept it from
    # either place.
    rel_inhg = press.get("relative_inhg") or ind.get("pressure_inhg")

    # Per-MAC yearly-rain calibration. Cumulative sensor counters (Atlas
    # rain_in, Fineoffset rain_mm) report lifetime totals, so we subtract
    # an operator-configured offset to get actual YTD inches. Clamp to
    # zero so a decoder glitch posting below the offset doesn't yield
    # a negative stored value. Non-numeric input (e.g. decoder posted
    # the literal string "abc") coerces to None — same defensive pattern
    # as _scrub_numbers; never 500 the request.
    yearly_in = rain.get("yearly_in")
    if yearly_in is not None:
        try:
            yearly_in = float(yearly_in)
        except (TypeError, ValueError):
            yearly_in = None
        else:
            # _scrub_numbers only filters values that arrive AS numbers — a
            # STRING like "1e999" passes through and float() here turns it
            # into inf, which then 500s every JSON response containing the
            # row (JSONResponse serializes with allow_nan=False). Re-check
            # finiteness after the coercion.
            if not math.isfinite(yearly_in):
                yearly_in = None
    # Yearly-rain offsets REMOVED (2026-08-11). The per-MAC calibration
    # subtracted an operator-set constant so lifetime counters read as YTD —
    # but it broke more than it fixed in production: applied to Davis (whose
    # yearly is already true YTD) it clamped the real total to 0.0, and for
    # offset sensors every reading stored before the offset was configured
    # used the unshifted scale, so year-over-history deltas (records,
    # rollups) went negative and the yearly rain records vanished. Raw
    # values now pass through untouched; INGEST_YEARLY_RAIN_OFFSETS is inert.

    # Feels-like: pass through the source's own value if present (AWN +
    # Davis provide it); otherwise derive it so SDR/custom sources that only
    # post raw temp/humidity still get the tile. Matches AWN's method.
    tempf = out.get("tempf")
    feels_like = out.get("feels_like")
    feels_derived = False
    if feels_like is None:
        feels_like = _compute_feels_like(tempf, out.get("humidity"),
                                         wind.get("speed_mph"))
        feels_derived = feels_like is not None

    flat = {
        "dateutc":        dateutc_ms,
        # Popped by _do_ingest before storage. The clamp above rewrites a
        # future dateutc to server "now", so each RETRY of the same broken-
        # clock packet got a fresh, later timestamp — and the second retry
        # "confirmed" the first's rejected spike. Confirmation ordering must
        # use the device's own claimed time, which a retry repeats verbatim.
        "_raw_dateutc":   raw_dateutc_ms,
        # Popped by _do_ingest (never stored): marks feelsLike as backend-
        # derived, so the plausibility bands can null it when they null the
        # inputs it was computed from.
        "_feels_derived": feels_derived,
        "tempf":          tempf,
        "feelsLike":      feels_like,
        "dewPoint":       out.get("dew_point_f"),
        "humidity":       out.get("humidity"),
        "tempinf":        ind.get("tempf"),
        "humidityin":     ind.get("humidity"),
        "baromrelin":     rel_inhg,
        "baromabsin":     rel_inhg,  # ingest sources rarely split
        "windspeedmph":   wind.get("speed_mph"),
        "windgustmph":    wind.get("gust_mph"),
        "maxdailygust":   wind.get("gust_mph"),  # best-effort; relays don't track daily peak
        # Accept both key shapes: docs/AGENTS say "direction", but the WLL
        # poller has always posted "dir_deg" — reading only one silently
        # dropped Davis wind direction. Explicit None check so 0° (north)
        # survives.
        "winddir":        wind.get("direction") if wind.get("direction") is not None
                          else wind.get("dir_deg"),
        "hourlyrainin":   rain.get("hourly_in"),
        "eventrainin":    rain.get("event_in"),
        "dailyrainin":    rain.get("daily_in"),
        "weeklyrainin":   rain.get("weekly_in"),
        "monthlyrainin":  rain.get("monthly_in"),
        "yearlyrainin":   yearly_in,
        # Explicit None checks, not `or` — 0 is a legitimate reading
        # (UV/solar at night) and must not fall through.
        "uv":             out.get("uv") if out.get("uv") is not None
                          else solar.get("uv"),
        "solarradiation": out.get("solar_wm2") if out.get("solar_wm2") is not None
                          else solar.get("radiation_wm2"),
        # Relay battery state (sdr-relay posts device.battery_outdoor, davis-
        # relay maps battery_low → battery_outdoor, the AcuRite hub path sent
        # battery_hub). Mapped to the battout/battin keys the iOS Observation
        # decodes — without this the relay-side battery reporting was inert
        # end to end (no header battery icon, battery-low notable never fired).
        "battout":        _battery_flag(dev.get("battery_outdoor")),
        "battin":         _battery_flag(dev.get("battery_hub")),
    }
    # Only the per-interval count is safe to accumulate; the trailing windows
    # keep their names so nothing mistakes them for additive.
    for src, dest in (("strike_count", "lightningcount"),
                      ("strike_count_last_1hr", "lightning_last_1hr"),
                      ("strike_count_last_3hr", "lightning_last_3hr"),
                      ("last_distance_mi", "lightning_distance_mi"),
                      ("last_strike_ms", "lightning_last_strike_ms")):
        v = lightning.get(src)
        if v is not None:
            flat[dest] = v
    # One choke point for every metric field (timestamps excluded — they were
    # validated above and must stay ints).
    for k, v in flat.items():
        if k not in ("dateutc", "_raw_dateutc", "lightning_last_strike_ms"):
            flat[k] = _coerce_num(v)
    return flat


def _compute_feels_like(tempf: Any, humidity: Any, wind_mph: Any) -> float | None:
    """NWS 'feels like', matching what AmbientWeather reports: the Rothfusz
    heat-index regression at ≥80°F (which legitimately dips below air temp in
    dry heat — e.g. 99.3°F/15% → 95.09°F), wind chill at ≤50°F with wind
    >3 mph, else the air temp. Returns None only when temperature is unknown.
    Fallback for sources (SDR/custom) that don't post their own feels_like."""
    try:
        t = float(tempf)
    except (TypeError, ValueError):
        return None
    # Same string-bypass as yearly_in: a STRING "1e999" sails past
    # _scrub_numbers, float()s to inf here, and the arithmetic below would
    # store a non-finite feels-like that breaks JSON serialization.
    if not math.isfinite(t):
        return None
    try:
        rh = float(humidity)
    except (TypeError, ValueError):
        rh = None
    if rh is not None and not math.isfinite(rh):
        rh = None
    if rh is not None and t >= 80.0:
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh
              - 0.22475541 * t * rh - 6.83783e-3 * t * t
              - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh
              + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh)
        return round(hi, 2)
    try:
        v = float(wind_mph)
    except (TypeError, ValueError):
        v = None
    if v is not None and not math.isfinite(v):
        v = None
    if v is not None and t <= 50.0 and v > 3.0:
        wc = 35.74 + 0.6215 * t - 35.75 * v**0.16 + 0.4275 * t * v**0.16
        return round(wc, 2)
    return round(t, 2)


# Absolute plausibility bands (records QC): min/max per metric field, set
# BEYOND world-record extremes so they can never clip a real reading — they
# exist for decode garbage (bit-flips like 3276.7 °F, negative rain, 3000 mph
# wind), which otherwise lands in records, rollups and alert evaluation as
# fact. Units are API-native (°F, mph, inHg, inches) — the storage convention.
# For reference: world temp extremes −128.6/134.1 °F; strongest measured
# surface gust 253 mph; sea-level pressure extremes 25.69/32.06 inHg (the
# absolute-pressure floor is lower for high-elevation stations); 24 h rain
# record ~71 in; hourly ~12 in; yearly ~1,042 in (Meghalaya).
#
# The wind ceiling is 254, NOT a round 260, and the exact number matters.
# 255 is 0xFF — the single-byte "no reading" sentinel — and Weather
# Underground serves it as a literal wind speed when a station's anemometer
# drops out. At a 260 ceiling those sentinels sail through and become
# all-time records (found 2026-08-15 on an imported archive: ~1,200 rows of
# 255 mph, against a real peak gust of 51). 254 still sits above the 253 mph
# world record, so the "never clip a real reading" property holds exactly.
_PLAUSIBLE_BANDS: dict[str, tuple[float, float]] = {
    "tempf":          (-90.0, 140.0),
    "feelsLike":      (-110.0, 160.0),
    "dewPoint":       (-90.0, 100.0),
    "humidity":       (0.0, 100.0),
    "tempinf":        (-40.0, 150.0),
    "humidityin":     (0.0, 100.0),
    "baromrelin":     (24.0, 33.0),
    "baromabsin":     (15.0, 33.0),
    "windspeedmph":   (0.0, 254.0),
    "windgustmph":    (0.0, 254.0),
    "maxdailygust":   (0.0, 254.0),
    "winddir":        (0.0, 360.0),
    "hourlyrainin":   (0.0, 15.0),
    "eventrainin":    (0.0, 100.0),
    "dailyrainin":    (0.0, 80.0),
    "weeklyrainin":   (0.0, 150.0),
    "monthlyrainin":  (0.0, 400.0),
    "yearlyrainin":   (0.0, 1500.0),
    "uv":             (0.0, 20.0),
    "solarradiation": (0.0, 1800.0),
    # Lightning. Local detectors (Tempest's AS3935-class sensor) top out in
    # the low thousands of strikes/hr even inside a violent storm — the live
    # 2026-08-19 monsoon cell peaked at ~1,200 — so these ceilings only ever
    # catch decode garbage. Distance: the sensor's own range limit is ~25 mi
    # (40 km); 100 keeps the never-clip property with a wide margin.
    "lightningcount":        (0.0, 5000.0),
    "lightning_last_1hr":    (0.0, 20000.0),
    "lightning_last_3hr":    (0.0, 60000.0),
    "lightning_distance_mi": (0.0, 100.0),
}


# The anemometer's speed channels. They come from ONE sensor, so a band
# rejection on any of them condemns the others on that same reading — see
# _apply_plausibility_bands. winddir is deliberately excluded: it is a
# separate vane channel and every value it can report is in-band anyway.
_ANEMOMETER_FIELDS = ("windspeedmph", "windgustmph", "maxdailygust")


def _apply_plausibility_bands(flat: dict[str, Any]) -> list[str]:
    """Null every metric field whose value falls outside its physical band.
    Field-level on purpose: one garbage field must not cost the reading's
    good fields. Returns the names of the fields dropped (for the log)."""
    dropped: list[str] = []
    for k, (lo, hi) in _PLAUSIBLE_BANDS.items():
        v = flat.get(k)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v < lo or v > hi:
            flat[k] = None
            dropped.append(f"{k}={v:g}")

    # A banded wind value means the anemometer was faulting at this instant,
    # and the sibling speed channels from that same sensor are not evidence of
    # anything either. Dropping only the out-of-band one leaves an orphan that
    # the bands cannot catch on a later pass, because it is physically
    # in-range: the 2026-08-15 archive left 89.7-213.3 mph "sustained" winds
    # sitting on rows whose 255 mph gust had just been nulled, on a station
    # whose 99.9th percentile wind is 13.9. Same reasoning as the derived
    # feelsLike rule at the call site.
    if any(d.split("=")[0] in _ANEMOMETER_FIELDS for d in dropped):
        for k in _ANEMOMETER_FIELDS:
            v = flat.get(k)
            if v is None or not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            flat[k] = None
            dropped.append(f"{k}={v:g}(anemometer)")

    dropped.extend(_apply_wind_consistency(flat))
    return dropped


# Internal-consistency (relational) check on the anemometer. The bands above
# are a *plausible value* check — they only see one field at a time, so garbage
# that lands inside every band walks straight through. Standard station QC
# pairs that with an internal-consistency check, and the canonical one for wind
# is the gust/speed relation (Doren, 2026-08-16: "it's impossible to have a
# steady wind be more powerful than a gust, right?" — correct, and it is true
# by definition, because the gust is the peak INSIDE the window the sustained
# average is taken over, and an average cannot exceed its own largest sample).
#
# The tolerance is deliberately loose, because a violation is only *definitely*
# impossible when both numbers describe the same window, and live stations
# often do not report them that way — an instantaneous speed can legitimately
# sit above a gust field that is a stale 10-minute maximum. Sized against the
# real archive that started this: 863 of Doren's 1.09 M readings have sustained
# above gust, every one by ≤6.4 mph, and they are overwhelmingly a gust channel
# pinned at 2.0 mph while the sustained value varies (harmless, and nulling
# them would throw away good data). Requiring BOTH a 25% overshoot and a 5 mph
# absolute gap cuts those 863 down to 6 flagged readings in eleven years — the
# handful where the gust channel is so far under the sustained wind that one of
# the two is certainly wrong.
_WIND_CONSISTENCY_RATIO = 1.25
_WIND_CONSISTENCY_ABS_MPH = 5.0


def _apply_wind_consistency(flat: dict[str, Any]) -> list[str]:
    """Condemn the anemometer set when sustained wind contradicts the gust on
    the same reading. Returns the names of the fields dropped (for the log).

    Whole-set, not just the offending field, for the reason the band sibling
    rule gives: these channels come from one sensor, and when it contradicts
    itself there is no way to tell which half is lying — the 8.4 mph sustained
    /2.0 mph gust pattern blames the gust, a dropout blames both."""
    def _num(key: str) -> float | None:
        v = flat.get(key)
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v) if math.isfinite(v) else None

    speed = _num("windspeedmph")
    # maxdailygust is a running daily peak, so sustained legitimately sits
    # under it all day and above it early — only the instantaneous gust is a
    # same-window comparison.
    gust = _num("windgustmph")
    if speed is None or gust is None:
        return []
    if speed <= gust * _WIND_CONSISTENCY_RATIO:
        return []
    if speed - gust <= _WIND_CONSISTENCY_ABS_MPH:
        return []

    dropped: list[str] = []
    for k in _ANEMOMETER_FIELDS:
        v = _num(k)
        if v is None:
            continue
        flat[k] = None
        dropped.append(f"{k}={v:g}(wind-inconsistent)")
    return dropped


def _is_rain_glitch(jump_in: float, elapsed_h: float,
                    max_rate_in_per_hr: float) -> bool:
    """True if a positive jump in cumulative yearly rain is implausible for the
    elapsed time (an SDR decode glitch). Allowance = max-rate × hours + a 0.25"
    floor for tip-counter jitter. Only positive spikes are flagged (counter
    resets / negative deltas are handled by the offset/clamp path)."""
    if max_rate_in_per_hr <= 0 or jump_in <= 0:
        return False
    allowance = max_rate_in_per_hr * max(elapsed_h, 1.0 / 3600.0) + 0.25
    return jump_in > allowance


# Pending level-shift rejections, keyed by mac (the original yearly-rain
# guard) or "mac|field" (the 1.5 daily-rain and temperature guards) ->
# (rejected_value, dateutc_ms).
# A true decode glitch is one-shot — the next reading returns to the old level.
# A LEVEL SHIFT (counter swap, calibration change, the 2026-08-11 removal of
# the yearly-rain offsets) persists: every subsequent reading agrees with the
# "glitch". Without this, the guard nulls rain forever after a shift, because
# rejected rows store NULL and the comparison baseline never advances — the
# exact lockout that re-broke Crestview. One confirming reading at the new
# level rebaselines. Process-global like main.py's caches; single event loop,
# lost on restart (costs one extra confirming reading — fine).
_rain_reject: dict[str, tuple[float, float]] = {}
# Bounded like main.py's limiter maps: every rejected reading adds a MAC entry
# and entries are only removed when that MAC later takes a non-glitch path, so
# an ingest-token holder spraying synthetic device ids grows it without limit.
_RAIN_REJECT_MAX = 512
# A confirming reading must be at least this much later than the rejection it
# corroborates. rtl_433 frequently decodes the SAME radio transmission two or
# three times within a few seconds, and a burst of identical decodes is one
# observation, not two: in production a neighboring sensor on a colliding ID
# (constant 20.22 counter vs Crestview's 17.12) got its duplicate decode
# accepted as "confirmation" of itself.
#
# 300s for RAIN, not the original 90s. The 90s figure was calibrated against
# the ~60s STORAGE cadence — but this guard runs before the write-throttle,
# so it evaluates every ~16s relay POST, and the relay serves rain from a
# per-field cache. On 2026-08-19 the colliding neighbor monopolized that
# cache for ~2-3 minutes (twice in one day): a throttled post primed the
# pending rejection and a post 90s+ later "confirmed" it, storing the
# neighbor's 20.27 counter as fact on a station at 17.16. Each episode ran
# well under 5 minutes, so requiring the new level to hold for 300s of
# posts rejects them outright. A genuine level shift never reverts, so the
# only cost is ~5 minutes of nulled rain rows before a real shift confirms
# — and a steady climb never enters this path at all (in-allowance jumps
# aren't rejected in the first place).
_RAIN_CONFIRM_MIN_GAP_MS = 300_000
# Temperature keeps the shorter gap: temp jitters 0.1-0.3°F between posts,
# its guard uses a ±5°F same-level tolerance, and a swapped sensor should
# rebaseline quickly (R4-02 pinned 108s-after-first-rejection confirming).
# The rain failure mode above doesn't apply — a colliding temp reading is
# re-nulled each episode rather than permanently corrupting a counter that
# feeds rollups.
_TEMP_CONFIRM_MIN_GAP_MS = 90_000

# Levels that PROVED to be glitches. When a pending level-shift candidate is
# followed by a reading back at the old baseline, the candidate "fell back" —
# something a real level shift never does — so that value is remembered here
# (key -> (value_in, last_seen_ms)) and refused as a confirmation while
# fresh. This is what ends the RECURRING colliding-neighbor pattern: its
# counter barely moves between episodes, so after the first fall-back the
# same 20.27 can reappear all day (or monopolize the relay cache for any
# length of time) and never rebaseline the station. TTL runs from CREATION
# and is deliberately NOT refreshed by blocked confirmations — refreshing
# would let one stale reading during a genuine level shift shadow the new
# level forever (the full reasoning sits at the check in
# _confirms_rejected_level). Expiry means a station whose counter genuinely
# reaches that level someday isn't shadowed beyond 24h. Process-global like
# _rain_reject: lost on restart, costing one rejection/fall-back cycle —
# worst case a repeat colliding-neighbor episode within the restart's 300 s
# confirmation gap rebaselines once more (R5-26: accepted; persisting guard
# state to disk isn't worth the coupling for a once-per-restart cost).
_rain_tombstone: dict[str, tuple[float, float]] = {}
_RAIN_TOMBSTONE_TTL_MS = 24 * 3_600_000
_RAIN_TOMBSTONE_MAX = 512


def _record_rain_rejection(mac: str, value_in: float, ts_ms: float,
                           same_tol: float = 0.05) -> None:
    """Register a rejected reading as a pending level-shift candidate,
    evicting the oldest entry at the cap (losing one pending confirmation
    costs that device a single extra confirming reading).

    `same_tol` is the per-guard "same level" tolerance: 0.05 in for rain
    counters (stable between tips), ~5 °F for temperature — which jitters
    0.1–0.3 °F between posts, so the rain tolerance would advance the
    pending timestamp on EVERY reading and a sensor posting faster than the
    90 s gap could never rebaseline (temp nulled forever after a swap)."""
    prev = _rain_reject.get(mac)
    if prev is not None and ts_ms <= prev[1]:
        # An out-of-order (or replayed) packet must not roll the pending
        # rejection's timestamp backwards: lowering rej_ts would let a replay
        # of the NEWER original packet pass the strictly-later check and
        # self-confirm the spike it belongs to.
        return
    if prev is not None and abs(value_in - prev[0]) <= same_tol:
        # Same level seen again but not yet confirmable (e.g. within the
        # burst-dedup gap): keep the FIRST-SEEN timestamp. Advancing it on
        # every repeat would push the confirmation window ahead of a sensor
        # whose cadence is shorter than the gap, so a real level shift could
        # never confirm.
        return
    if len(_rain_reject) >= _RAIN_REJECT_MAX and mac not in _rain_reject:
        oldest = min(_rain_reject, key=lambda k: _rain_reject[k][1])
        del _rain_reject[oldest]
    _rain_reject[mac] = (value_in, ts_ms)


def _note_rain_fallback(key: str, ts_ms: float) -> None:
    """A reading back at the old baseline arrived while a level-shift
    candidate was pending: the candidate fell back, which a real level shift
    never does, so it was a glitch — remember its value so it can't confirm
    a later pending candidate (the recurring colliding-neighbor pattern).
    Call in place of the bare `_rain_reject.pop(key)` on the non-glitch
    path."""
    prev = _rain_reject.pop(key, None)
    if prev is None:
        return
    if len(_rain_tombstone) >= _RAIN_TOMBSTONE_MAX and key not in _rain_tombstone:
        oldest = min(_rain_tombstone, key=lambda k: _rain_tombstone[k][1])
        del _rain_tombstone[oldest]
    _rain_tombstone[key] = (prev[0], ts_ms)


def _confirms_rejected_level(mac: str, value_in: float, ts_ms: float,
                             max_rate_in_per_hr: float) -> bool:
    """True when `value_in` corroborates the previously REJECTED reading for
    this mac: at or above that level, the increment since the rejection is
    itself plausible rain for the elapsed time, and the level has not already
    proven itself a glitch by falling back (see _rain_tombstone). A one-shot
    spike fails this (the next reading falls back below the rejected level)."""
    tomb = _rain_tombstone.get(mac)
    if tomb is not None:
        tomb_val, tomb_ts = tomb
        if ts_ms - tomb_ts >= _RAIN_TOMBSTONE_TTL_MS:
            del _rain_tombstone[mac]
        elif abs(value_in - tomb_val) <= 0.05:
            # Fixed TTL from CREATION — deliberately not refreshed on
            # sightings. Refreshing looked attractive (the shadow would
            # outlive a neighbor that keeps transmitting) but inverts the
            # failure: one stale old-baseline reading arriving during a
            # GENUINE level shift tombstones the new level, and since every
            # subsequent real reading sits at that level, a refreshed
            # tombstone never expires — months of nulled rain in a dry
            # season (2026-08-20 review). Bounded at 24h, the worst case
            # either way is one day; a recurring neighbor beyond the TTL
            # still has to re-earn a rejection AND hold the cache for the
            # full 300s confirmation gap.
            return False
    prev = _rain_reject.get(mac)
    if prev is None:
        return False
    rej_val, rej_ts = prev
    # Strictly LATER observation required: a sender retrying the identical
    # rejected packet (same dateutc) has an increment of zero, which trivially
    # passes the plausibility check — the duplicate would confirm itself.
    # Corroboration means a NEW reading at the new level, not the same one
    # delivered twice.
    if ts_ms <= rej_ts:
        return False
    if ts_ms - rej_ts < _RAIN_CONFIRM_MIN_GAP_MS:
        # Too soon to be a distinct observation: duplicate decodes of one
        # radio transmission arrive seconds apart and must not corroborate
        # each other (see _RAIN_CONFIRM_MIN_GAP_MS).
        return False
    if value_in < rej_val - 0.05:          # fell back — it WAS a glitch
        return False
    elapsed_h = max((ts_ms - rej_ts) / 3_600_000.0, 1.0 / 3600.0)
    return (value_in - rej_val) <= max_rate_in_per_hr * elapsed_h + 0.25


def _confirms_temp_level(key: str, value_f: float, ts_ms: float,
                         tolerance_f: float = 5.0) -> bool:
    """Temperature flavor of `_confirms_rejected_level`: a strictly-later,
    distinct (past the burst-dedup gap) reading within ±tolerance of the
    rejected value means the new level is real — a swapped/colliding sensor,
    not a one-shot decode glitch — and should rebaseline."""
    prev = _rain_reject.get(key)
    if prev is None:
        return False
    rej_val, rej_ts = prev
    if ts_ms <= rej_ts or ts_ms - rej_ts < _TEMP_CONFIRM_MIN_GAP_MS:
        return False
    return abs(value_f - rej_val) <= tolerance_f


def _format_mac(raw: str) -> str:
    """Normalize a hub identifier to AA:BB:CC:DD:EE:FF if it looks like a
    12-hex MAC — compact OR already-colonized in any letter case (storage
    keys are the uppercase colonized form, so `aa:bb:...` must not slip
    through as a distinct device key). Pass through anything else
    unchanged."""
    if raw and re.fullmatch(r"[0-9A-Fa-f]{12}", raw):
        return ":".join(raw[i:i+2].upper() for i in range(0, 12, 2))
    if raw and re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", raw):
        return raw.upper()
    return raw or ""


def _dev_block(normalized: dict[str, Any]) -> dict[str, Any]:
    """The `device` block, or {} when a source sent something that isn't an
    object. Every caller here does .get() on it, so a bare string or list used
    to raise AttributeError → 500 instead of a 400."""
    dev = normalized.get("device")
    return dev if isinstance(dev, dict) else {}


def _device_label(normalized: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pick a friendly name + location for the devices table.

    Returns (name, location) where `name` is:
      - the operator-supplied `device.name` if present (explicit POST field), OR
      - None — meaning "I have no explicit name; preserve whatever the row
        already has, and only fall back to an auto-derived name on first INSERT".

    The auto-derived fallback ("AcuRite Atlas" etc.) is built in upsert_device
    when row is brand new, NOT here, so a secondary source posting to an
    existing row (e.g. LilyGO posting to a row the Pi already named
    "AcuRite Atlas (SDR)") doesn't flip the name on every UPSERT."""
    dev = _dev_block(normalized)
    explicit_name = dev.get("name")
    location = dev.get("location")
    return explicit_name, location


def _payload_coords(normalized: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a source-provided lat/lon in the shape the iOS Device model reads
    (info.coords.coords.{lat,lon}), or None if the source didn't include one.
    Accepts either a flat {lat,lon} or a nested {coords:{lat,lon}} under the
    top-level or device block."""
    dev = _dev_block(normalized)
    raw = normalized.get("coords") or dev.get("coords") or {}
    if not isinstance(raw, dict):
        return None
    inner = raw.get("coords") if isinstance(raw.get("coords"), dict) else raw
    if not isinstance(inner, dict):
        return None
    try:
        lat = float(inner["lat"])
        lon = float(inner["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"location": raw.get("location"), "coords": {"lat": lat, "lon": lon}}


def _auto_device_name(normalized: dict[str, Any]) -> str:
    """Auto-generated name used ONLY on first INSERT of a device row."""
    dev = _dev_block(normalized)
    src = normalized.get("source") or "custom"
    if not isinstance(src, str):        # a non-string `source` broke .replace()
        src = "custom"
    model = dev.get("model")
    if not isinstance(model, str):      # ...and a non-string `model` broke .lower()
        model = None
    pretty = {
        "acurite-atlas": "AcuRite Atlas",
        "acurite-access": "AcuRite Access",
        "ecowitt": "Ecowitt",
        "tempest": "Tempest",
        # The Mac app posts WITHOUT device.name unless the user typed one
        # (posting its default clobbered server-side names), so first inserts
        # from it land here — and "Davis Wll Local" is not a name to ship.
        "davis-wll-local": "Davis WeatherLink Live",
    }.get(src, src.replace("-", " ").title())
    return f"{pretty}{f' ({model})' if model and model.lower() not in pretty.lower() else ''}"


def _sea_level_pressure(abs_inhg: float, elevation_ft: float) -> float | None:
    """Absolute (station) pressure → sea-level equivalent via the standard-
    atmosphere barometric formula: slp = p * (1 - 0.0065*h/288.15)^-5.257,
    h in meters. Physics from the operator's actual elevation, not a raw
    additive offset — see settings.station_elevation_ft (the 1.3.2 rain-
    offset incident is why hand-tuned per-MAC constants are off the table).
    None when the result isn't a finite positive number (absurd elevation or
    a garbage reading must not store a poisoned value)."""
    try:
        base = 1.0 - 0.0065 * (elevation_ft * 0.3048) / 288.15
        if base <= 0:           # ~145k ft — not a weather station
            return None
        slp = abs_inhg * base ** -5.257
    except (TypeError, ValueError, OverflowError):
        return None
    return round(slp, 3) if math.isfinite(slp) and slp > 0 else None


def _pressure_absolute_macs() -> set[str]:
    """PRESSURE_ABSOLUTE_MACS (csv) → normalized colonized MACs. Parsed per
    call like main.py's PUBLIC_DASHBOARD_MACS handling — a non-MAC entry
    passes through _format_mac unchanged and simply never matches a
    normalized device key."""
    raw = settings.pressure_absolute_macs or ""
    return {_format_mac(p.strip()) for p in raw.split(",") if p.strip()}


def _is_gust_glitch(gust: float | None, speed: float | None,
                    min_mph: float, max_factor: float) -> bool:
    """A gust is a glitch if it's above `min_mph` AND exceeds `max_factor` ×
    the concurrent sustained wind speed — an implausible gust factor. Pure so
    it's unit-testable. Never flags a gust at/below the floor, or when the
    sustained speed is unknown."""
    if max_factor <= 0 or gust is None or gust <= min_mph:
        return False
    # Unknown sustained wind → can't judge the ratio, so never flag.
    # speed == 0 is the same situation, NOT a 100%-confidence glitch: a squall
    # front hitting a calm station legitimately reads 0 sustained with a real
    # 45 mph gust. Multiplying by zero would discard every gust above the floor
    # — exactly when gusts matter most — so treat 0 as unknown too.
    if speed is None or speed <= 0:
        return False
    return gust > speed * max_factor


def _require_ingest_token(token: str, client_host: str | None = None) -> None:
    if not tokens_match(token, settings.ingest_token):
        # Log the rejection (rain/gust drops already log; auth failures were
        # the one silent denial). One line per failed request is bounded by
        # the body-size middleware + normal request handling, and gives the
        # operator the "why is my board's data missing?" answer.
        log.warning("ingest auth failed from %s (token %s)",
                    client_host or "?", "present" if token else "missing")
        raise HTTPException(status_code=401, detail="invalid ingest token")


def _token_from_header(authorization: str | None,
                       x_ingest_token: str | None) -> str:
    """Pull the ingest token from either Authorization: Bearer or
    X-Ingest-Token. Missing both = empty string (will fail validation)."""
    if x_ingest_token: return x_ingest_token.strip()
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return ""


# Per-(event-loop, MAC) locks for the write-throttle's check-then-insert.
# WeakKeyDictionary on the loop so locks never outlive the loop they bound to
# (each test's asyncio.run() gets fresh locks; nothing to reset in conftest).
# Entries are [lock, holders]: the production loop lives for the process
# lifetime, so without eviction every MAC an ingest-token holder ever posts
# leaves a Lock behind forever — an unbounded cache fed by request input. The
# holder count makes eviction safe: an entry is dropped only when the LAST
# task through it leaves, so concurrent tasks always share one lock object
# and mutual exclusion holds (all registry mutations happen on the loop with
# no await between read and write).
_THROTTLE_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, list]]" = (
    weakref.WeakKeyDictionary())


class _throttle_lock:
    """`async with _throttle_lock(mac):` — per-(loop, MAC) mutex whose
    registry entry is removed once no task holds or awaits it."""

    def __init__(self, mac: str) -> None:
        self._mac = mac
        self._entry: list | None = None

    async def __aenter__(self) -> None:
        loop = asyncio.get_running_loop()
        per_loop = _THROTTLE_LOCKS.setdefault(loop, {})
        entry = per_loop.get(self._mac)
        if entry is None:
            entry = per_loop[self._mac] = [asyncio.Lock(), 0]
        entry[1] += 1
        self._entry = entry
        self._per_loop = per_loop
        try:
            await entry[0].acquire()
        except BaseException:
            # The awaiting task can be CANCELLED while blocked on acquire()
            # (client disconnect, shutdown mid-ingest). `async with` never
            # runs __aexit__ when __aenter__ raises, so without this the
            # holder count incremented above leaks and the registry entry
            # for this MAC is never evicted — an unbounded, request-fed
            # leak on the process-lifetime production loop.
            entry[1] -= 1
            if entry[1] == 0 and per_loop.get(self._mac) is entry:
                del per_loop[self._mac]
            self._entry = None
            raise

    async def __aexit__(self, *exc: object) -> None:
        entry = self._entry  # always set: `async with` pairs __aexit__ with __aenter__
        entry[0].release()
        entry[1] -= 1
        if entry[1] == 0 and self._per_loop.get(self._mac) is entry:
            del self._per_loop[self._mac]


# Metadata keys ignored when deciding whether a throttled-window reading
# carries genuinely new data: timestamps, the source tag, and the free-text
# lastRain marker all churn without representing a sensor field.
_THROTTLE_IGNORE = frozenset({"_source", "dateutc", "date", "tz", "lastRain"})


def _adds_field(new: dict[str, Any], prev: dict[str, Any]) -> bool:
    """True if `new` provides a non-null value for a sensor field that `prev`
    lacks (null or absent). Such a reading is a multi-source composite
    contribution and must be stored even inside the throttle window; a reading
    that only changes existing values can be safely coalesced."""
    for k, v in new.items():
        if k in _THROTTLE_IGNORE or v is None:
            continue
        if prev.get(k) is None:
            return True
    return False


async def _do_ingest(payload_obj: Any) -> dict[str, Any]:
    if not isinstance(payload_obj, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    dev = _dev_block(payload_obj)
    raw_id = dev.get("id") or ""
    mac = _format_mac(str(raw_id))
    if not mac:
        raise HTTPException(status_code=400, detail="device.id required")
    if "|" in mac:
        # '|' is the guard-state key separator (mac + "|tempf"): a device id
        # containing it would share glitch-guard state with another device
        # (CODE_REVIEW_R5 R5-24). No legitimate hub id carries one.
        raise HTTPException(status_code=400,
                            detail="device.id must not contain '|'")
    flat = _flatten(payload_obj)
    if not flat:
        raise HTTPException(status_code=400, detail="missing or invalid timestamp_utc")
    raw_dateutc = flat.pop("_raw_dateutc", flat.get("dateutc"))

    # Elevation-based sea-level correction BEFORE the plausibility bands
    # (CodeRabbit, round 2): a configured absolute-pressure station above
    # ~6,000 ft legitimately reads under the 24 inHg sea-level-relative
    # floor, so banding first would null the reading and the correction
    # would never see it. Corrected values are then judged by the relative
    # band; baromabsin keeps the true absolute reading (15 inHg floor).
    elev_ft = settings.station_elevation_ft
    if (elev_ft != 0 and flat.get("baromrelin") is not None
            and mac in _pressure_absolute_macs()):
        slp = _sea_level_pressure(flat["baromrelin"], elev_ft)
        if slp is not None:
            flat["baromabsin"] = flat["baromrelin"]
            flat["baromrelin"] = slp

    # Physical plausibility bands next (records QC): decode garbage — values
    # beyond world-record extremes — is nulled field-by-field before it can
    # reach records, rollups, alerts or the relative guards below (whose
    # baselines it would poison).
    feels_derived = bool(flat.pop("_feels_derived", False))
    if settings.ingest_plausibility_bands:
        dropped = _apply_plausibility_bands(flat)
        if dropped:
            log.warning("implausible values dropped for %s: %s",
                        mac, ", ".join(dropped))
            # A feels-like the backend derived from an input the bands just
            # nulled is garbage that happened to land in-band (tempf=85 with
            # humidity=-5 → a plausible-looking 92.9 °F) — never store a
            # derivation whose inputs didn't survive. Source-provided
            # feels-like answers to its own band only.
            # windspeedmph too: wind chill derives from wind speed on cold
            # readings, so a banded wind input poisons the derivation the
            # same way banded temp/humidity do.
            if feels_derived and any(
                    d.startswith(("tempf=", "humidity=", "windspeedmph="))
                    for d in dropped):
                flat["feelsLike"] = None

    # Reject SDR rain-decode glitches: a sudden spike in cumulative yearly
    # rain that's physically impossible for the elapsed time. Real rain ramps
    # gradually; a glitch jumps for one reading then the counter returns to its
    # true value. We compare to the last *good* reading ("before" corroboration)
    # — since a dropped glitch is stored NULL, the next real reading resumes from
    # the good value, so isolated spikes drop cleanly without an "after" lookup.
    max_rate = settings.ingest_max_rain_rate_in_per_hr
    if flat.get("yearlyrainin") is not None and max_rate > 0:
        prev = await db.last_yearly_rain(mac)
        if prev is not None:
            last_val, last_ts = prev
            jump = flat["yearlyrainin"] - last_val
            elapsed_h = (flat["dateutc"] - last_ts) / 3_600_000.0
            if _is_rain_glitch(jump, elapsed_h, max_rate):
                if _confirms_rejected_level(mac, flat["yearlyrainin"],
                                            raw_dateutc, max_rate):
                    # Second consecutive reading at the new level: this is a
                    # level shift, not a spike. Accept and rebaseline.
                    log.warning(
                        "rain level shift ACCEPTED for %s: %.2f→%.2f in "
                        "(confirmed by consecutive readings)", mac, last_val,
                        flat["yearlyrainin"])
                    _rain_reject.pop(mac, None)
                else:
                    _record_rain_rejection(mac, flat["yearlyrainin"],
                                           raw_dateutc)
                    log.warning(
                        "rain glitch dropped for %s: +%.2f in over %.3f h "
                        "— %.2f→%.2f (will accept if the next reading "
                        "confirms)", mac, jump, elapsed_h, last_val,
                        flat["yearlyrainin"])
                    for k in ("yearlyrainin", "hourlyrainin", "eventrainin",
                              "dailyrainin", "weeklyrainin", "monthlyrainin"):
                        flat[k] = None
            else:
                # In-band reading at (or plausibly above) the old baseline:
                # any pending candidate just fell back — tombstone it so the
                # same value can't confirm a later episode.
                _note_rain_fallback(mac, raw_dateutc)

    # Same spike guard for the DAILY rain bucket (1.5, records QC): a decode
    # glitch can spike daily_in while the yearly counter stays sane — the
    # "3.58-inch day" class that reaches the daily-rain record untouched by
    # the yearly guard above. Midnight resets are negative jumps, which
    # _is_rain_glitch ignores. Same level-shift rebaseline: one confirming
    # reading at the new level accepts it (a real cloudburst confirms on the
    # next reading and costs a single nulled row).
    if flat.get("dailyrainin") is not None and max_rate > 0:
        daily_key = mac + "|dailyrainin"
        prev = await db.last_metric_value(mac, "dailyrainin")
        if prev is not None and flat["dateutc"] <= prev[1]:
            # Out-of-order/replayed packet: judging it against the NEWER
            # baseline reads a legitimate old value as a spike — worst case
            # a delayed pre-midnight rain row arriving after the post-reset
            # 0.0. Store as-is (the plausibility bands already vetted it)
            # and leave any pending rejection untouched.
            prev = None
        if prev is not None:
            last_val, last_ts = prev
            jump = flat["dailyrainin"] - last_val
            elapsed_h = (flat["dateutc"] - last_ts) / 3_600_000.0
            if _is_rain_glitch(jump, elapsed_h, max_rate):
                if _confirms_rejected_level(daily_key, flat["dailyrainin"],
                                            raw_dateutc, max_rate):
                    log.warning(
                        "daily-rain level shift ACCEPTED for %s: %.2f→%.2f in "
                        "(confirmed by consecutive readings)", mac, last_val,
                        flat["dailyrainin"])
                    _rain_reject.pop(daily_key, None)
                else:
                    _record_rain_rejection(daily_key, flat["dailyrainin"],
                                           raw_dateutc)
                    log.warning(
                        "daily-rain glitch dropped for %s: +%.2f in over "
                        "%.3f h — %.2f→%.2f (will accept if the next reading "
                        "confirms)", mac, jump, elapsed_h, last_val,
                        flat["dailyrainin"])
                    # Null the sub-yearly buckets from the same decode; the
                    # yearly counter answered its own guard above.
                    for k in ("dailyrainin", "hourlyrainin", "eventrainin",
                              "weeklyrainin", "monthlyrainin"):
                        flat[k] = None
            else:
                _note_rain_fallback(daily_key, raw_dateutc)

    # Temperature-jump guard (1.5, records QC — see ingest_max_temp_jump_f):
    # a reading impossibly far from the device's last stored temperature is a
    # decode glitch or a colliding transmitter. Same rebaseline contract as
    # the rain guards: a persistent new level (swapped sensor) is accepted on
    # the second consecutive sighting.
    max_jump = settings.ingest_max_temp_jump_f
    if flat.get("tempf") is not None and max_jump > 0:
        temp_key = mac + "|tempf"
        prev = await db.last_metric_value(mac, "tempf")
        if prev is not None and flat["dateutc"] <= prev[1]:
            # Same out-of-order rule as the daily-rain guard above.
            prev = None
        if prev is not None:
            last_val, last_ts = prev
            delta = abs(flat["tempf"] - last_val)
            elapsed_h = max((flat["dateutc"] - last_ts) / 3_600_000.0,
                            1.0 / 3600.0)
            if delta > max_jump + 60.0 * elapsed_h:
                if _confirms_temp_level(temp_key, flat["tempf"], raw_dateutc):
                    log.warning(
                        "temperature level shift ACCEPTED for %s: "
                        "%.1f→%.1f °F (confirmed by consecutive readings)",
                        mac, last_val, flat["tempf"])
                    _rain_reject.pop(temp_key, None)
                else:
                    _record_rain_rejection(temp_key, flat["tempf"], raw_dateutc,
                                           same_tol=5.0)
                    log.warning(
                        "temperature glitch dropped for %s: %.1f→%.1f °F over "
                        "%.3f h (will accept if the next reading confirms)",
                        mac, last_val, flat["tempf"], elapsed_h)
                    # feels-like and dew point derive from the same decode.
                    for k in ("tempf", "feelsLike", "dewPoint"):
                        flat[k] = None
            else:
                _rain_reject.pop(temp_key, None)

    # Drop a spurious wind gust (see settings.ingest_gust_*). A gust wildly
    # higher than the concurrent sustained wind is a sensor glitch — nulling it
    # stops false high-wind alerts and keeps it out of the peak-gust record.
    if _is_gust_glitch(flat.get("windgustmph"), flat.get("windspeedmph"),
                       settings.ingest_gust_min_mph, settings.ingest_gust_max_factor):
        log.warning("gust glitch dropped for %s: %.1f mph gust vs %.1f mph "
                    "sustained", mac, flat["windgustmph"], flat.get("windspeedmph") or 0.0)
        flat["windgustmph"] = None

    # (Elevation-based sea-level correction moved ABOVE the plausibility
    # bands — see the top of this function.)

    explicit_name, location = _device_label(payload_obj)
    auto_name = _auto_device_name(payload_obj)
    inner_info: dict[str, Any] = {
        "name": explicit_name or auto_name,
        "location": location,
        "source": payload_obj.get("source"),
    }
    # Stamp the operator's configured home location onto devices whose source
    # doesn't report coordinates (SDR relays, the local WLL poller). Without a
    # location the iOS sunrise/sunset + forecast + UV features have nothing to
    # work from and fall back to (0,0). forecast_lat/lon is the single source
    # of "where am I", so it stays correct no matter which poller is active.
    coords_from_payload = _payload_coords(payload_obj)
    if coords_from_payload is not None:
        inner_info["coords"] = coords_from_payload
    elif settings.forecast_lat is not None and settings.forecast_lon is not None:
        inner_info["coords"] = {
            "location": location,
            "coords": {"lat": settings.forecast_lat, "lon": settings.forecast_lon},
        }
    info = {
        # `name` here is the operator-explicit value (None if not provided).
        # `info.auto_name` is the fallback used only on first INSERT.
        "name": explicit_name,
        "auto_name": auto_name,
        "info": inner_info,
        "lastData": flat,
    }
    # New-device probation. A MAC that looks like a bit-flipped twin of a
    # device we already know has to be seen repeatedly before it earns a row
    # — one corrupt 433 MHz packet once minted a phantom Atlas that then
    # emailed a device-down alert about a station that never existed. A
    # genuinely new MAC is admitted immediately, so ordinary setup is
    # unaffected. See app/device_probation.py.
    if not await _admit_device(mac):
        # 200, not an error: the sender is behaving correctly and a 4xx would
        # make a LilyGO count it toward its 401-wipe heuristic. `quarantined`
        # is how a caller can tell this reading was dropped on purpose.
        source_status.record_success("custom-ingest", rows=0)
        return {"ok": True, "mac": mac, "inserted": 0,
                "ts_ms": flat["dateutc"], "throttled": False,
                "quarantined": True}

    # Always refresh the device row (lastData / live view) so throttling the
    # history write below never staleness the dashboard.
    await db.upsert_device(mac, info)

    # History-write throttle: drop a reading that lands within
    # ingest_min_interval_seconds of the last STORED row for this device,
    # unless it contributes a field that row was missing (multi-source
    # composite). See settings.ingest_min_interval_seconds.
    row = {**flat, "_source": _truncate_source(payload_obj)}
    min_interval_ms = settings.ingest_min_interval_seconds * 1000
    if min_interval_ms > 0:
        # Check-then-insert across two connections is a race: two concurrent
        # posts for the same MAC both see the same "last stored" row, both
        # pass the check, and both insert. Serialize per MAC. The lock map is
        # keyed on the running event loop (an asyncio.Lock binds to the first
        # loop that awaits it, and the test suite runs asyncio.run() per test
        # — see the _PUBLIC_DASH_LOCK note in main.py).
        async with _throttle_lock(mac):
            store = True
            last = await db.last_stored_observation(mac)
            if last is not None:
                last_ts, last_data = last
                recent = 0 <= flat["dateutc"] - last_ts < min_interval_ms
                if recent and not _adds_field(flat, last_data):
                    store = False
            inserted = await db.insert_observations(mac, [row]) if store else 0
    else:
        store = True
        inserted = await db.insert_observations(mac, [row])
    # Custom ingest is a push path — nothing polls it, so without this the
    # source reports unhealthy forever even while boards and pollers are
    # posting successfully. A throttled write still counts as the source
    # working; `rows` distinguishes stored from merely received.
    source_status.record_success("custom-ingest", rows=inserted)
    # Forward to Weather Underground (1.5) — scheduled only AFTER every DB
    # write above has committed, as a fire-and-forget task, so a slow or
    # failing WU call can never block or fail this request. No-ops unless the
    # device has upload enabled + station + key configured, and wu_upload
    # throttles per mac (so even history-throttled readings may schedule —
    # they're still fresh data). Copy `flat`: the task runs after we return.
    wu_upload.schedule(mac, dict(flat))
    return {"ok": True, "mac": mac, "inserted": inserted,
            "ts_ms": flat["dateutc"], "throttled": not store}


# Per-request body size cap. A normal observation is ~500 bytes; even a
# rich Atlas message with lightning + battery + RSSI tops out around 2 KB.
# 64 KiB is generous headroom while making it impossible for a misbehaving
# source to OOM the worker by streaming megabytes into a single ingest.
INGEST_BODY_MAX_BYTES = 64 * 1024
# Trim the persisted source-object copy to this so a single fat _source
# can't bloat the observations table indefinitely. Loses bonus diagnostic
# fields but never drops the flat data we actually render in iOS.
INGEST_SOURCE_MAX_BYTES = 16 * 1024


async def _parse_json_body(request: Request) -> Any:
    """Parse a request body as JSON, returning a 400 on malformed input
    instead of letting FastAPI surface it as a 500. Also rejects Python's
    non-standard NaN/Infinity literals that some decoders emit, since
    they'd serialize back to non-JSON-compliant numbers downstream.
    Enforces a size cap to bound worker memory."""
    # Cheap early-reject via Content-Length so we don't read the body at
    # all when a misbehaving client claims an absurd size.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > INGEST_BODY_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"body too large; max {INGEST_BODY_MAX_BYTES} bytes")
        except ValueError:
            pass  # Malformed Content-Length — fall through to the read check.
    body = await request.body()
    if len(body) > INGEST_BODY_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"body too large; max {INGEST_BODY_MAX_BYTES} bytes")
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        return _json.loads(body, parse_constant=lambda _: None)
    except _json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e.msg}")
    except RecursionError:
        # "[[[[…" raises RecursionError, not JSONDecodeError — still a 400.
        raise HTTPException(status_code=400,
                            detail="invalid JSON: too deeply nested")


def _probation_now_ms() -> int:
    """SERVER clock for probation sighting spacing — patchable in tests.
    The device-claimed timestamp must play no role here: a replayed backfill
    with crafted dateutc values could space its "sightings" perfectly and
    mint a phantom device with zero real-time presence (CODE_REVIEW_R5
    R5-25). Real-time evidence is the entire point of probation."""
    return int(time.time() * 1000)


async def _admit_device(mac: str) -> bool:
    """False when this reading is from a MAC still serving probation.

    Returns True for every device we already know (the overwhelmingly common
    path, one indexed lookup) and for any unknown MAC that is not a near
    neighbour of an existing one.
    """
    needed = settings.device_confirm_suspect_hits
    if needed <= 0:
        return True
    known = await db.list_device_macs()
    if any(k.upper() == mac.upper() for k in known):
        return True

    suspect = device_probation.suspect_of(
        mac, known, settings.device_confirm_max_bits)
    if suspect is None:
        log.info("new device %s admitted (no similar device known)", mac)
        return True

    now_ms = _probation_now_ms()
    prior = await db.get_pending_device(mac) or {}
    # A pending row older than the TTL is history, not evidence: the read
    # path had no staleness check and bump's global prune runs AFTER decide()
    # already computed hits, re-inserting the carried count fresh — so the
    # same recurring bit-flip arriving every couple of weeks still summed to
    # admission over months, minting the phantom device (and its eventual
    # false device-down alert) that probation exists to stop (2026-08-20
    # review). Counting restarts from zero once the trail has gone cold.
    ttl_ms = int(settings.device_confirm_ttl_hours * 3_600_000)
    last_ms = prior.get("last_ms")
    if isinstance(last_ms, (int, float)) and now_ms - last_ms >= ttl_ms:
        prior = {}
    verdict = device_probation.decide(
        prior_hits=int(prior.get("hits") or 0),
        prior_ms=prior.get("last_ms"),
        now_ms=now_ms,
        suspect=suspect,
        needed=needed,
        min_gap_ms=int(settings.device_confirm_min_gap_seconds * 1000),
    )
    if verdict.admit:
        await db.clear_pending_device(mac)
        log.warning(
            "admitting %s after %d confirmed sightings — it closely resembles "
            "%s, so if these are the same station the extra row is a decoder "
            "problem worth investigating", mac, verdict.hits, suspect)
        return True

    await db.bump_pending_device(
        mac, now_ms, verdict.hits, suspect, verdict.counted,
        ttl_ms=int(settings.device_confirm_ttl_hours * 3_600_000))
    log.warning(
        "quarantining %s: looks like a corrupted %s (%d/%d confirmed "
        "sightings). Reading dropped.",
        mac, suspect, verdict.hits, verdict.needed)
    return False


def _truncate_source(payload_obj: dict[str, Any]) -> dict[str, Any]:
    """Drop the _source copy down to INGEST_SOURCE_MAX_BYTES of JSON.
    Strategy: serialize once, check size; if over, replace with a small
    marker dict that retains key identifying fields (source tag, device
    block, timestamp) so we can still trace the row's provenance."""
    raw = _json.dumps(payload_obj, separators=(",", ":"))
    if len(raw) <= INGEST_SOURCE_MAX_BYTES:
        return payload_obj
    return {
        "_truncated": True,
        "_original_bytes": len(raw),
        "source": payload_obj.get("source"),
        "device": payload_obj.get("device"),
        "timestamp_utc": payload_obj.get("timestamp_utc"),
    }


# Token in header so it never appears in proxy/access logs.
@router.post("/ingest/custom")
async def ingest_custom_header(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ingest_token: Annotated[str | None, Header(alias="X-Ingest-Token")] = None,
) -> dict[str, Any]:
    _require_ingest_token(_token_from_header(authorization, x_ingest_token),
                          request.client.host if request.client else None)
    return await _do_ingest(await _parse_json_body(request))

# (Legacy path-form `/ingest/custom/{token}` was removed 2026-05-21. The
# only consumer was the retired hub-relay container; tokens in URLs leak
# to proxy/access logs. Use the header form above for all new ingest.)
