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


# ── 2.4 item 6 ────────────────────────────────────────────────────────
#
# Sources, same rule as above: each formula keeps its own paper's units
# and converts at the boundary, and missing input is None rather than a
# fabricated zero.
#
# - Vapour pressure deficit: Buck 1981 saturation vapour pressure, kPa.
#   The number greenhouse and vineyard people actually steer by.
# - Humidex: Masterton & Richardson 1979, Environment Canada. Reported
#   as a °C-scaled index, never as a temperature, which is why it has no
#   unit suffix in their own publications.
# - Apparent temperature: Steadman 1984 as used by the Australian BoM —
#   the one that takes WIND as well as humidity, so it reads below air
#   temperature in a breeze where heat index simply stops existing.
# - Cloud base: the standard 1000 ft per 4.4 °F spread rule for
#   convective cumulus. A rule of thumb with a real pedigree, and wrong
#   for every other cloud type, which the wording has to carry.
# - Wind run: distance the air moved past the station, which is what an
#   anemometer measures and what evaporation and spray drift care about.
# - Sunshine hours: the WMO threshold is 120 W/m² of DIRECT beam; a
#   pyranometer sees global radiation, so the common proxy compares
#   against a fraction of the clear-sky maximum for the sun's angle.
# - EPA AQI: the 2024 PM2.5 breakpoints (the reform that moved the
#   annual standard to 9 µg/m³ kept these 24 h breakpoints).
# - Chill hours: the Utah model's weighted bands, which unlike a simple
#   hours-below-45 count can go DOWN on a hot afternoon.


def vapour_pressure_deficit_kpa(temp_f, humidity) -> float | None:
    """How thirsty the air is, in kPa. Zero means saturated."""
    t, rh = _f(temp_f), _f(humidity)
    if t is None or rh is None or not (0 <= rh <= 100):
        return None
    tc = _f2c(t)
    # Buck 1981, over water, kPa.
    svp = 0.61121 * math.exp((18.678 - tc / 234.5) * (tc / (257.14 + tc)))
    return max(0.0, svp * (1.0 - rh / 100.0))


def humidex(temp_f, humidity) -> float | None:
    """Environment Canada's humidex. An INDEX on the Celsius scale, not
    a temperature: 40 is "great discomfort", not 40 degrees of
    anything."""
    t, rh = _f(temp_f), _f(humidity)
    if t is None or rh is None or not (0 <= rh <= 100):
        return None
    dew = dew_point_f(t, rh)
    if dew is None:
        return None
    dew_k = _f2c(dew) + 273.15
    if dew_k <= 0:
        return None
    vapour = 6.11 * math.exp(5417.7530 * (1 / 273.16 - 1 / dew_k))
    return _f2c(t) + 0.5555 * (vapour - 10.0)


def apparent_temperature_f(temp_f, humidity, wind_mph) -> float | None:
    """Steadman's apparent temperature, the one with wind in it.

    Heat index gives up below about 80 °F and wind chill above about
    50 °F, which leaves the middle of the year with no answer at all.
    This one is continuous, and it reads BELOW the air temperature in a
    breeze, which is the honest answer on a dry windy day.
    """
    t, rh, wind = _f(temp_f), _f(humidity), _f(wind_mph)
    if t is None or rh is None or not (0 <= rh <= 100):
        return None
    wind = 0.0 if wind is None or wind < 0 else wind
    tc = _f2c(t)
    ws = wind * 0.44704                      # m/s
    e = rh / 100.0 * 6.105 * math.exp(17.27 * tc / (237.7 + tc))
    return _c2f(tc + 0.33 * e - 0.70 * ws - 4.00)


def cloud_base_ft(temp_f, dew_point_f_val) -> float | None:
    """Height of the convective cumulus base above the STATION.

    The 1000 ft per 4.4 °F spread rule. It describes fair weather cumulus
    and nothing else, so anywhere it is shown has to say so.
    """
    t, dew = _f(temp_f), _f(dew_point_f_val)
    if t is None or dew is None:
        return None
    spread = t - dew
    if spread < 0:
        return 0.0
    return spread / 4.4 * 1000.0


def wind_run_mi(readings: list[tuple[int, float]]) -> float | None:
    """Miles of air past the station, from (epoch ms, mph) samples.

    Trapezoidal over the gaps actually present, so a poller that missed
    an hour reports the miles it can account for rather than inventing
    the hour. A gap longer than an hour is NOT bridged: at that point
    the average is a guess and a day's wind run that silently includes
    guesses is worse than one that says it is short.
    """
    if not readings or len(readings) < 2:
        return None
    ordered = sorted(readings, key=lambda r: r[0])
    total = 0.0
    for (t0, v0), (t1, v1) in zip(ordered, ordered[1:]):
        gap_h = (t1 - t0) / 3_600_000
        if gap_h <= 0 or gap_h > 1.0:
            continue
        total += (v0 + v1) / 2.0 * gap_h
    return total


# The share of the clear-sky maximum that counts as sunshine. The WMO
# threshold is 120 W/m² of direct beam; a pyranometer sees global, so
# the hobbyist convention (Cumulus, WeeWX) compares against a fraction
# of the theoretical clear-sky value for the sun's elevation.
SUNSHINE_FRACTION = 0.75
SUNSHINE_FLOOR_WM2 = 120.0


def is_sunshine(solar_wm2, clear_sky_wm2) -> bool | None:
    """Whether this instant counts as sunshine."""
    s, clear = _f(solar_wm2), _f(clear_sky_wm2)
    if s is None or clear is None or clear <= 0:
        return None
    return s >= max(SUNSHINE_FLOOR_WM2, clear * SUNSHINE_FRACTION)


# EPA AQI breakpoints for PM2.5, µg/m³, 24 hour average (the 2024
# reform kept these). (low, high, aqi_low, aqi_high, category).
_PM25_BANDS = (
    (0.0, 9.0, 0, 50, "Good"),
    (9.1, 35.4, 51, 100, "Moderate"),
    (35.5, 55.4, 101, 150, "Unhealthy for sensitive groups"),
    (55.5, 125.4, 151, 200, "Unhealthy"),
    (125.5, 225.4, 201, 300, "Very unhealthy"),
    (225.5, 325.4, 301, 500, "Hazardous"),
)


def aqi_pm25(pm25) -> tuple[int, str] | None:
    """US EPA AQI and its category from a PM2.5 concentration.

    The breakpoints are defined on a 24 HOUR average. Handing this an
    instantaneous reading gives an instantaneous AQI, which is what
    every consumer monitor shows and is not what the EPA publishes, so
    whatever displays it has to be honest about the window.
    """
    v = _f(pm25)
    if v is None or v < 0:
        return None
    v = math.floor(v * 10) / 10          # EPA truncates to 0.1 µg/m³
    for lo, hi, alo, ahi, label in _PM25_BANDS:
        if v <= hi:
            aqi = (ahi - alo) / (hi - lo) * (v - lo) + alo
            return int(round(aqi)), label
    return 500, "Hazardous"


def chill_hours_utah(temp_f) -> float | None:
    """One hour's contribution under the Utah model.

    Unlike a plain hours-below-45 count this one can go NEGATIVE, which
    is the whole reason fruit growers use it: a warm winter afternoon
    genuinely undoes chill the night before accumulated.
    """
    t = _f(temp_f)
    if t is None:
        return None
    if t <= 34.0:
        return 0.0
    if t <= 36.0:
        return 0.5
    if t <= 48.0:
        return 1.0
    if t <= 54.0:
        return 0.5
    if t <= 60.0:
        return 0.0
    if t <= 65.0:
        return -0.5
    return -1.0


def evapotranspiration_in(temp_f, humidity, wind_mph, solar_wm2,
                          pressure_inhg, hours: float = 1.0) -> float | None:
    """Reference evapotranspiration, inches, FAO-56 Penman-Monteith.

    The number irrigation scheduling runs on. A Davis console computes
    its own ET and that one is preferred where it exists; this is for
    every station that does not, which is most of them.

    Hourly form, with the standard reference crop (0.12 m grass,
    albedo 0.23). Soil heat flux is taken as zero, which is the FAO's
    own daytime simplification and is close enough at an hour.
    """
    t, rh = _f(temp_f), _f(humidity)
    wind, solar, press = _f(wind_mph), _f(solar_wm2), _f(pressure_inhg)
    if None in (t, rh, wind, solar, press) or not (0 <= rh <= 100):
        return None
    if hours <= 0:
        return None
    tc = _f2c(t)
    u2 = max(0.0, wind) * 0.44704                    # m/s at 2 m
    p_kpa = press * 3.386389
    # W/m² → MJ/m² per HOUR. A rate, like every other term in the
    # numerator; the one `* hours` at the end integrates the step. Scaling
    # this one by the step as well squared it, so the finer a station
    # posted the less its sun counted (2.4 review).
    rs_mj = max(0.0, solar) * 0.0036
    svp = 0.6108 * math.exp(17.27 * tc / (tc + 237.3))
    avp = svp * rh / 100.0
    delta = 4098 * svp / ((tc + 237.3) ** 2)
    gamma = 0.000665 * p_kpa
    rn = 0.77 * rs_mj                                # net radiation, albedo 0.23
    numerator = (0.408 * delta * rn
                 + gamma * (37.0 / (tc + 273.0)) * u2 * (svp - avp))
    denominator = delta + gamma * (1 + 0.34 * u2)
    if denominator <= 0:
        return None
    et_mm = numerator / denominator * hours
    return max(0.0, et_mm) / 25.4


def solar_elevation_deg(ts_ms: int, lat: float, lon: float) -> float | None:
    """How high the sun is, in degrees above the horizon.

    The standard NOAA short form: declination and the equation of time
    from the day angle, then the hour angle from apparent solar time.
    Good to a fraction of a degree, which is far more than a clear-sky
    envelope needs, and it keeps this module free of a dependency for
    one sine.

    Negative below the horizon on purpose, so a caller can tell night
    from an overcast noon.
    """
    try:
        lat = float(lat)
        lon = float(lon)
        ts = float(ts_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    day_of_year = when.timetuple().tm_yday
    gamma = 2 * math.pi / 365.0 * (day_of_year - 1 + (when.hour - 12) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma)
                       - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma)
                       - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    minutes = when.hour * 60 + when.minute + when.second / 60.0
    true_solar = (minutes + eqtime + 4 * lon) % 1440
    hour_angle = math.radians(true_solar / 4.0 - 180.0)
    phi = math.radians(lat)
    cos_zenith = (math.sin(phi) * math.sin(decl)
                  + math.cos(phi) * math.cos(decl) * math.cos(hour_angle))
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


def clear_sky_wm2(ts_ms: int, lat: float, lon: float) -> float:
    """Clear-sky global radiation for this instant, W/m².

    The elevation model the hobbyist sunshine proxies use. Zero below
    the horizon, which is what makes a night of zero solar read as "no
    sunshine to measure" rather than as an overcast day.
    """
    elevation = solar_elevation_deg(ts_ms, lat, lon)
    if elevation is None or elevation <= 0:
        return 0.0
    sin_h = math.sin(math.radians(elevation))
    if sin_h <= 0:
        return 0.0
    return 1098.0 * sin_h * math.exp(-0.057 / sin_h)


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
