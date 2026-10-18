// rtl_433 decoder JSON → the /ingest/custom reading blocks (outdoor, wind,
// rain, pressure). Header-only and Arduino-free (ArduinoJson is header-only
// too) so the native test env runs the exact mapping the board runs.
//
// Field names MUST match backend/app/ingest._flatten() schema:
//   outdoor:  tempf, feels_like, dew_point_f, humidity, uv, solar_wm2
//   wind:     speed_mph, gust_mph, direction
//   rain:     yearly_in
//   pressure: relative_inhg
// Mis-naming silently drops the field; the app card then renders without
// that tile.
#pragma once

#include <ArduinoJson.h>
#include <math.h>

namespace FieldMap {

// A decoder field that holds a number, integer or floating. rtl_433 types
// fields per decoder (the Atlas emits humidity, uv and lux as DATA_INT).
// ArduinoJson 7's is<float>() already accepts integers (test_field_map
// pins that with an integer light_lux); this spells the intent out.
// Finite only: an overflowing decode can hold infinity, which serializes
// as null and reaches the server as a reading that says nothing.
inline bool isNum(JsonVariantConst v) {
  if (!(v.is<float>() || v.is<int>() || v.is<long>() || v.is<double>())) return false;
  return isfinite(v.as<double>());
}

inline void copyIf(JsonVariantConst in, const char *inKey,
                   JsonObject out, const char *outKey) {
  if (isNum(in[inKey])) out[outKey] = in[inKey].as<float>();
}

inline float c_to_f(float c)      { return c * 9.0f / 5.0f + 32.0f; }
inline float ms_to_mph(float ms)  { return ms * 2.23694f; }
inline float hpa_to_inhg(float h) { return h * 0.0295299830714f; }

// Magnus-Tetens dew point in °F from tempf + humidity %. Matches the
// formula sdr-relay uses so two sources posting to the same device row
// don't disagree on dew point by a fraction. NaN for a non-positive
// humidity (rare sensor glitch); callers check isnan().
inline float dew_point_f(float temp_f, float humidity_pct) {
  if (humidity_pct <= 0.0f) return NAN;
  float t_c = (temp_f - 32.0f) * 5.0f / 9.0f;
  const float a = 17.625f, b = 243.04f;
  float gamma = logf(humidity_pct / 100.0f) + (a * t_c) / (b + t_c);
  float dp_c = (b * gamma) / (a - gamma);
  return dp_c * 9.0f / 5.0f + 32.0f;
}

// Lux per W/m² for sunlight; the same divisor sdr-relay/sdr_relay.py uses
// (the two relays share a device row, so they must share the number).
constexpr float LUX_PER_WM2 = 126.7f;

// Fills out["outdoor"/"wind"/"rain"/"pressure"] from one decoded packet.
//
// Each block is emitted when the packet carries ANY of its fields, not
// only its headline one. The Atlas sends its readings in three rotating
// messages (0x25 temp/humidity, 0x26 wind direction/rain, 0x27 UV/lux,
// wind speed in all three), so a UV/lux message has no temperature. Gating
// the outdoor block on temperature dropped every Atlas UV and lux reading
// for as long as the LilyGO was the only source (2026-10-15, mirror #8).
// `dropUvLight`: leave uv and light out (a dead Atlas photodiode; the
// board's atlas_uv_lux_broken setting). Absent, never a stuck number.
inline void mapReadings(JsonVariantConst in, JsonDocument &out, bool dropUvLight = false) {
  // ── outdoor ──
  const bool hasTemp = isNum(in["temperature_C"]) || isNum(in["temperature_F"]);
  const bool hasLight = !dropUvLight &&
      (isNum(in["solar_radiation"]) || isNum(in["light_lux"]) || isNum(in["lux"]));
  const bool hasUv = !dropUvLight && (isNum(in["uv"]) || isNum(in["uvi"]));
  if (hasTemp || isNum(in["humidity"]) || hasUv || hasLight) {
    JsonObject outdoor = out["outdoor"].to<JsonObject>();
    if (isNum(in["temperature_C"]))
      outdoor["tempf"] = c_to_f(in["temperature_C"].as<float>());
    else if (isNum(in["temperature_F"]))
      outdoor["tempf"] = in["temperature_F"].as<float>();
    copyIf(in, "humidity", outdoor, "humidity");
    if (hasUv) {
      copyIf(in, "uv",  outdoor, "uv");
      copyIf(in, "uvi", outdoor, "uv");
    }
    // Solar irradiance: the backend takes outdoor.solar_wm2 only. Use W/m²
    // when the decoder emits it (some Ecowitt variants), else derive it
    // from lux. rtl_433 names differ: the Atlas emits "lux", Fineoffset
    // WH65B/WH24 emit "light_lux".
    if (!hasLight) {
      // No light reading, or the mask is on.
    } else if (isNum(in["solar_radiation"])) {
      outdoor["solar_wm2"] = in["solar_radiation"].as<float>();
    } else {
      float lux = NAN;
      if (isNum(in["light_lux"]))  lux = in["light_lux"].as<float>();
      else if (isNum(in["lux"]))   lux = in["lux"].as<float>();
      if (!isnan(lux)) outdoor["solar_wm2"] = lux / LUX_PER_WM2;
    }
    // Dew point when the same packet carries both temperature and humidity.
    if (outdoor["tempf"].is<float>() && outdoor["humidity"].is<float>()) {
      float dp = dew_point_f(outdoor["tempf"].as<float>(),
                             outdoor["humidity"].as<float>());
      if (!isnan(dp)) outdoor["dew_point_f"] = dp;
    }
  }

  // ── wind ── decoders emit m/s, mi/h or km/h by protocol; accept all three.
  if (isNum(in["wind_avg_m_s"]) || isNum(in["wind_max_m_s"]) ||
      isNum(in["wind_avg_mi_h"]) || isNum(in["wind_avg_km_h"]) ||
      isNum(in["wind_max_mi_h"]) || isNum(in["wind_max_km_h"]) ||
      isNum(in["wind_dir_deg"])) {
    JsonObject wind = out["wind"].to<JsonObject>();
    if (isNum(in["wind_avg_m_s"]))
      wind["speed_mph"] = ms_to_mph(in["wind_avg_m_s"].as<float>());
    else if (isNum(in["wind_avg_mi_h"]))
      wind["speed_mph"] = in["wind_avg_mi_h"].as<float>();
    else if (isNum(in["wind_avg_km_h"]))
      wind["speed_mph"] = in["wind_avg_km_h"].as<float>() * 0.621371f;
    if (isNum(in["wind_max_m_s"]))
      wind["gust_mph"] = ms_to_mph(in["wind_max_m_s"].as<float>());
    else if (isNum(in["wind_max_mi_h"]))
      wind["gust_mph"] = in["wind_max_mi_h"].as<float>();
    else if (isNum(in["wind_max_km_h"]))
      wind["gust_mph"] = in["wind_max_km_h"].as<float>() * 0.621371f;
    copyIf(in, "wind_dir_deg", wind, "direction");
  }

  // ── rain ── the raw cumulative counter as yearly_in; the backend's
  // rain_rollups() differences it at local period boundaries, so daily,
  // hourly, weekly and monthly come out right after the first ~24 h. It
  // looks big on first deploy (the sensor's lifetime total, not since
  // Jan 1); the rollups are differences, so they are still correct. The
  // Atlas emits rain_in (inches), Fineoffset rain_mm.
  float rain_in_total = NAN;
  if (isNum(in["rain_in"]))      rain_in_total = in["rain_in"].as<float>();
  else if (isNum(in["rain_mm"])) rain_in_total = in["rain_mm"].as<float>() * 0.0393701f;
  if (!isnan(rain_in_total)) out["rain"]["yearly_in"] = rain_in_total;

  // ── pressure ── only from a sensor that has a barometer (hPa → inHg);
  // outdoor stations don't.
  if (isNum(in["pressure_hPa"]))
    out["pressure"]["relative_inhg"] = hpa_to_inhg(in["pressure_hPa"].as<float>());
}

}  // namespace FieldMap
