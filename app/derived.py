"""Derived weather metrics (1.8, Pillar C) — pure functions, unit-tested.

Every function takes API-native inputs (°F, mph, inHg — CLAUDE.md) and
keeps each formula's constants in the units of its source paper,
converting at the boundary. Missing/garbage inputs return None, never a
fabricated zero (absent is not zero).

Sources, per function:
- Wet bulb: Stull 2011 (J. Appl. Meteor. Climatol.) closed form, °C/RH%,
  valid RH 5–99, T −20…50 °C, assumes ~1013 mb (fine for a tile).
- Dew/frost point: Magnus (water) and Buck (ice) saturation forms —
  below freezing the frost point sits ABOVE the dew point.
- Fosberg FWI: Fosberg 1978 — instantaneous fire-WEATHER index from
  T °F, RH %, wind mph. Not an official fire-danger rating (no fuels).
- Chandler Burning Index: T °C + RH % weather-only index; hobbyist
  standard (Cumulus/WeeWX both carry it). Same no-fuels caveat.
- Delta-T: spray-conditions wet-bulb depression, conventionally
  expressed in °C even in US ag literature.
- Density altitude: the NWS El Paso calculator chain (vapor pressure →
  virtual temperature → DA in feet) from STATION pressure, not SLP.
- Degree days: NWS convention — (Tmax+Tmin)/2 vs base 65 °F.
- Pressure tendency: WMO code-0200 simplified to the net 3 h change.
- Zambretti: the 1920 Negretti & Zambra forecaster; honest skill is
  ~70 % rain/no-rain at 24–48 h, so present it as "what your barometer
  thinks", never as a forecast product.
"""
from __future__ import annotations

import math

__all__ = [
    "wet_bulb_f", "dew_point_f", "frost_point_f", "delta_t_c",
    "fosberg_fwi", "chandler_burning_index", "density_altitude_ft",
    "heating_degree_days", "cooling_degree_days",
    "pressure_tendency_code", "zambretti",
]


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _f2c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _c2f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ── moisture ────────────────────────────────────────────────────────────

def dew_point_f(temp_f, humidity) -> float | None:
    """Magnus dew point. Matches the ingest-time computation's family;
    kept here so the derived module is self-contained for callers that
    only hold T/RH."""
    t, rh = _f(temp_f), _f(humidity)
    if t is None or rh is None or not (0 < rh <= 100):
        return None
    tc = _f2c(t)
    a, b = 17.62, 243.12          # Magnus over water, °C
    gamma = math.log(rh / 100.0) + a * tc / (b + tc)
    if gamma >= a:                # rh>100-ish numeric guard
        return None
    td = b * gamma / (a - gamma)
    return _c2f(td)


def frost_point_f(temp_f, humidity) -> float | None:
    """Frost point — meaningful when the dew point is below freezing,
    where saturation over ICE gives a higher deposit temperature than
    the water-phase dew point. Returns None when Td ≥ 32 °F (no frost
    story to tell). Buck ice constants: es = 6.1115·exp(22.452·T/(272.55+T))."""
    td_f = dew_point_f(temp_f, humidity)
    if td_f is None or td_f >= 32.0:
        return None
    t, rh = _f(temp_f), _f(humidity)
    tc = _f2c(t)
    # Actual vapor pressure from the water-phase Buck form at T:
    e = 6.1121 * math.exp(17.502 * tc / (240.97 + tc)) * (rh / 100.0)
    if e <= 0:
        return None
    # Invert the ice form for the frost point:
    ln_ratio = math.log(e / 6.1115)
    tf = 272.55 * ln_ratio / (22.452 - ln_ratio)
    return _c2f(tf)


def wet_bulb_f(temp_f, humidity) -> float | None:
    """Stull (2011) psychrometric wet bulb. Validity window enforced —
    outside it the closed form degrades and None beats a wrong number."""
    t, rh = _f(temp_f), _f(humidity)
    if t is None or rh is None:
        return None
    tc = _f2c(t)
    if not (-20.0 <= tc <= 50.0):
        return None
    # At saturation the exact answer is Tw = T — fog and active rain are
    # precisely when this tile is interesting, and the Stull window used to
    # blank it there (R7 finding 4).
    if rh >= 100.0:
        return t
    if not (5.0 <= rh <= 99.0):
        return None
    tw = (tc * math.atan(0.151977 * math.sqrt(rh + 8.313659))
          + math.atan(tc + rh) - math.atan(rh - 1.676331)
          + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
          - 4.686035)
    return _c2f(tw)


def delta_t_c(temp_f, humidity) -> float | None:
    """Spray-conditions ΔT = Tdry − Twet, in °C — the guidance bands
    (2–8 ideal, avoid >10) are defined in °C even in US ag literature;
    a °F display must convert the BANDS, not this value's unit."""
    t = _f(temp_f)
    tw_f = wet_bulb_f(temp_f, humidity)
    if t is None or tw_f is None:
        return None
    return _f2c(t) - _f2c(tw_f)


# ── fire weather (weather-only indices, NOT official danger ratings) ────

def fosberg_fwi(temp_f, humidity, wind_mph) -> float | None:
    """Fosberg Fire Weather Index. Piecewise equilibrium moisture m from
    T °F / RH %, damping η, then FFWI = η·√(1+U²)/0.3002 with U in mph.
    ~100 ≈ 30 mph wind over bone-dry fuels."""
    t, rh, u = _f(temp_f), _f(humidity), _f(wind_mph)
    if t is None or rh is None or u is None or rh < 0 or u < 0:
        return None
    if rh < 10:
        m = 0.03229 + 0.281073 * rh - 0.000578 * rh * t
    elif rh <= 50:
        m = 2.22749 + 0.160107 * rh - 0.01478 * t
    else:
        m = 21.0606 + 0.005565 * rh * rh - 0.00035 * rh * t - 0.483199 * rh
    m = max(m, 0.0)
    r = m / 30.0
    eta = 1.0 - 2.0 * r + 1.5 * r * r - 0.5 * r ** 3
    eta = max(eta, 0.0)
    return eta * math.sqrt(1.0 + u * u) / 0.3002


def chandler_burning_index(temp_f, humidity) -> float | None:
    """CBI, T in °C per the source formula. Bands: <50 low, 50–75
    moderate, 75–90 high, 90–97.5 very high, >97.5 extreme."""
    t, rh = _f(temp_f), _f(humidity)
    if t is None or rh is None or rh < 0:
        return None
    tc = _f2c(t)
    cbi = (((110.0 - 1.373 * rh) - 0.54 * (10.20 - tc))
           * (124.0 * 10.0 ** (-0.0142 * rh))) / 60.0
    return max(cbi, 0.0)


# ── aviation / density ──────────────────────────────────────────────────

def density_altitude_ft(temp_f, dew_point_f_val, station_pressure_inhg
                        ) -> float | None:
    """NWS chain: vapor pressure (mb) from dew point, virtual temperature
    (Rankine), DA = 145366·[1 − (17.326·P/Tv)^0.235]. STATION pressure
    (baromabsin), never sea-level-corrected — feeding SLP here is the
    classic wrong-answer path."""
    t, td, p = _f(temp_f), _f(dew_point_f_val), _f(station_pressure_inhg)
    if t is None or td is None or p is None or p <= 0:
        return None
    tdc = _f2c(td)
    e_mb = 6.11 * 10.0 ** (7.5 * tdc / (237.3 + tdc))
    p_mb = p * 33.8639
    if e_mb >= p_mb:
        return None
    t_k = (_f2c(t)) + 273.15
    tv_k = t_k / (1.0 - (e_mb / p_mb) * (1.0 - 0.622))
    tv_r = tv_k * 1.8
    return 145366.0 * (1.0 - (17.326 * p / tv_r) ** 0.235)


# ── degree days ─────────────────────────────────────────────────────────

def heating_degree_days(tmax_f, tmin_f, base_f: float = 65.0) -> float | None:
    """NWS min/max-mean convention (matches utility bills and climate
    reports; NOT the integrated 24 h mean)."""
    hi, lo = _f(tmax_f), _f(tmin_f)
    if hi is None or lo is None or hi < lo:
        return None
    return max(0.0, base_f - (hi + lo) / 2.0)


def cooling_degree_days(tmax_f, tmin_f, base_f: float = 65.0) -> float | None:
    hi, lo = _f(tmax_f), _f(tmin_f)
    if hi is None or lo is None or hi < lo:
        return None
    return max(0.0, (hi + lo) / 2.0 - base_f)


# ── barometer ───────────────────────────────────────────────────────────

# ±1 hPa over 3 h is the conventional steady band, in inHg.
_TENDENCY_STEADY_INHG = 0.0295

def pressure_tendency_code(delta_3h_inhg) -> tuple[int, str] | None:
    """WMO code-0200 simplified to the net change: 2 rising, 4 steady,
    7 falling. (The full 0–8 characteristic needs the curve's shape;
    the net-change triple is what a tile can honestly claim.)"""
    d = _f(delta_3h_inhg)
    if d is None:
        return None
    if d > _TENDENCY_STEADY_INHG:
        return (2, "rising")
    if d < -_TENDENCY_STEADY_INHG:
        return (7, "falling")
    return (4, "steady")


# Z 1–32, the canonical Negretti & Zambra list (verified against the
# published algorithm 2026-08-25 — the falling/steady/rising formulas each
# index their own slice: falling 1–9, steady 10–19, rising 20–32).
_ZAMBRETTI_TEXT = {
    1: "Settled fine", 2: "Fine weather", 3: "Fine, becoming less settled",
    4: "Fairly fine, showery later", 5: "Showery, becoming more unsettled",
    6: "Unsettled, rain later", 7: "Rain at times, worse later",
    8: "Rain at times, becoming very unsettled", 9: "Very unsettled, rain",
    10: "Settled fine", 11: "Fine weather", 12: "Fine, possibly showers",
    13: "Fairly fine, showers likely", 14: "Showery, bright intervals",
    15: "Changeable, some rain", 16: "Unsettled, rain at times",
    17: "Rain at frequent intervals", 18: "Very unsettled, rain",
    19: "Stormy, much rain",
    20: "Settled fine", 21: "Fine weather", 22: "Becoming fine",
    23: "Fairly fine, improving", 24: "Fairly fine, possibly showers early",
    25: "Showery early, improving", 26: "Changeable, mending",
    27: "Rather unsettled, clearing later", 28: "Unsettled, probably improving",
    29: "Unsettled, short fine intervals", 30: "Very unsettled, finer at times",
    31: "Stormy, possibly improving", 32: "Stormy, much rain",
}


def zambretti(slp_hpa, trend: str) -> str | None:
    """The Negretti & Zambra slide rule, canonical algorithmic form:
    falling Z = 127 − 0.12·P (clamped 1–9), steady Z = 144 − 0.13·P
    (10–19), rising Z = 185 − 0.16·P (20–32), P = sea-level hPa. The
    optional wind-direction nudge is deliberately omitted — an honest
    "what your barometer thinks" from pressure alone, to be scored
    against reality by the forecast verification work."""
    p = _f(slp_hpa)
    if p is None or trend not in ("rising", "steady", "falling"):
        return None
    if trend == "falling":
        z = round(127.0 - 0.12 * p)
        z = min(max(z, 1), 9)
    elif trend == "steady":
        z = round(144.0 - 0.13 * p)
        z = min(max(z, 10), 19)
    else:
        z = round(185.0 - 0.16 * p)
        z = min(max(z, 20), 32)
    return _ZAMBRETTI_TEXT[z]
