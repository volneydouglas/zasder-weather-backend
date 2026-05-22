#include "zasder_post.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

#include <time.h>

// Map an rtl_433 model + id to the synthetic MAC scheme the backend
// expects ("5D:5D:TT:HH:HH:HH" — see sdr-relay/README.md). Same
// type-tag bytes so a LilyGO-sourced Atlas lands on the same device
// row as an SDR-sourced one would.
static String synthMac(const char *model, uint32_t id) {
  uint8_t typeTag = 0xFF;
  String  m       = String(model);
  if (m == "Acurite-Atlas")          typeTag = 0x01;
  else if (m == "Fineoffset-WH24")   typeTag = 0x02;
  else if (m == "Fineoffset-WH65B")  typeTag = 0x02;
  else if (m == "Fineoffset-WS80")   typeTag = 0x02;
  else if (m == "Fineoffset-WH32B")  typeTag = 0x03;
  else                                typeTag = 0xFE;  // unknown — still POST

  char buf[13];
  snprintf(buf, sizeof(buf), "5D5D%02X%02X%02X%02X",
           typeTag,
           (unsigned) ((id >> 16) & 0xFF),
           (unsigned) ((id >>  8) & 0xFF),
           (unsigned) ( id        & 0xFF));
  return String(buf);
}

// ISO-8601 UTC string for the timestamp_utc field. rtl_433_ESP doesn't
// stamp packets itself; we use the ESP32's clock (kept fresh by SNTP
// after Wi-Fi connect — see configTzTime() in main if needed).
static String nowIsoUtc() {
  time_t now = time(nullptr);
  if (now < 1700000000) {
    // SNTP hasn't synced yet; fall back to a placeholder the backend
    // will replace with received_at if it sees this exact value.
    return "1970-01-01T00:00:00Z";
  }
  struct tm tm;
  gmtime_r(&now, &tm);
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
  return String(buf);
}

// Copy a numeric field from the rtl_433 doc into the appropriate
// sub-block of the outgoing observation, if present.
static void copyIf(const JsonDocument &in, const char *inKey,
                   JsonObject &out, const char *outKey) {
  if (in[inKey].is<float>() || in[inKey].is<int>() || in[inKey].is<double>()) {
    out[outKey] = in[inKey].as<float>();
  }
}

// Unit converters used inline where the source field's unit differs
// from what /ingest/custom expects (which mirrors AmbientWeather's
// imperial field names).
static inline float c_to_f(float c)     { return c * 9.0f / 5.0f + 32.0f; }
static inline float ms_to_mph(float ms) { return ms * 2.23694f; }
static inline float mm_to_in(float mm)  { return mm / 25.4f; }
static inline float hpa_to_inhg(float h){ return h * 0.0295299830714f; }

void zasder_post(const char *rtl433Json,
                 const String &backendUrl,
                 const String &ingestToken) {
  if (backendUrl.isEmpty() || ingestToken.isEmpty()) {
    Serial.printf("[skip] %s\n", rtl433Json);
    return;
  }

  // Parse the rtl_433 JSON. 1 KB is comfortable for the largest
  // weather packet shape (~300 bytes typical).
  JsonDocument in;
  DeserializationError derr = deserializeJson(in, rtl433Json);
  if (derr) {
    Serial.printf("[parse-err] %s — %s\n", derr.c_str(), rtl433Json);
    return;
  }

  const char *model = in["model"].as<const char *>();
  if (!model) return;
  uint32_t id = in["id"].as<uint32_t>();

  // Build the outgoing observation. /ingest/custom accepts:
  //   {device:{id,name,location}, timestamp_utc, source,
  //    outdoor:{tempf, humidity},
  //    wind:{windspeedmph, windgustmph, winddir},
  //    rain:{yearly_in, hourly_in},
  //    pressure:{baromrelin},
  //    indoor:{tempf,humidity,pressure_inhg}}
  JsonDocument out;
  auto device       = out["device"].to<JsonObject>();
  device["id"]      = synthMac(model, id);
  device["name"]    = model;            // backend keeps the first-seen name; can be edited via /api
  out["timestamp_utc"] = nowIsoUtc();
  out["source"]        = ZASDER_SOURCE_TAG;

  // ── outdoor block ──
  if (in["temperature_C"].is<float>() || in["humidity"].is<float>()) {
    auto outdoor = out["outdoor"].to<JsonObject>();
    if (in["temperature_C"].is<float>())
      outdoor["tempf"] = c_to_f(in["temperature_C"].as<float>());
    else if (in["temperature_F"].is<float>())
      outdoor["tempf"] = in["temperature_F"].as<float>();
    copyIf(in, "humidity",         outdoor, "humidity");
    copyIf(in, "uv",               outdoor, "uv");
    copyIf(in, "uvi",              outdoor, "uv");
    copyIf(in, "light_lux",        outdoor, "lux");
    copyIf(in, "solar_radiation",  outdoor, "solarradiation");
  }

  // ── wind block ──
  if (in["wind_avg_m_s"].is<float>() || in["wind_max_m_s"].is<float>() ||
      in["wind_avg_mi_h"].is<float>() || in["wind_dir_deg"].is<int>()) {
    auto wind = out["wind"].to<JsonObject>();
    if (in["wind_avg_m_s"].is<float>())
      wind["windspeedmph"] = ms_to_mph(in["wind_avg_m_s"].as<float>());
    else if (in["wind_avg_mi_h"].is<float>())
      wind["windspeedmph"] = in["wind_avg_mi_h"].as<float>();
    if (in["wind_max_m_s"].is<float>())
      wind["windgustmph"] = ms_to_mph(in["wind_max_m_s"].as<float>());
    else if (in["wind_max_mi_h"].is<float>())
      wind["windgustmph"] = in["wind_max_mi_h"].as<float>();
    copyIf(in, "wind_dir_deg", wind, "winddir");
  }

  // ── rain block ── rtl_433 emits a lifetime cumulative counter
  // ("rain_mm" or "rain_in"). Server-side rain calibration takes a
  // baseline once and tracks deltas from there — see backend docs.
  if (in["rain_mm"].is<float>() || in["rain_in"].is<float>()) {
    auto rain = out["rain"].to<JsonObject>();
    if (in["rain_in"].is<float>())
      rain["yearly_in"] = in["rain_in"].as<float>();
    else
      rain["yearly_in"] = mm_to_in(in["rain_mm"].as<float>());
  }

  // ── pressure block ── (typically only WH32B indoor; outdoor sensors
  // rarely include it). Convert hPa → inHg.
  if (in["pressure_hPa"].is<float>()) {
    auto p = out["pressure"].to<JsonObject>();
    p["baromrelin"] = hpa_to_inhg(in["pressure_hPa"].as<float>());
  }

  String body;
  serializeJson(out, body);

  WiFiClientSecure tls;
  tls.setInsecure();  // public template default; users on private CAs
                      // can drop in setCACert() during build.
  HTTPClient http;
  String url = backendUrl + "/ingest/custom";
  if (!http.begin(tls, url)) {
    Serial.printf("[http-begin-fail] %s\n", url.c_str());
    return;
  }
  http.addHeader("Content-Type",  "application/json");
  http.addHeader("Authorization", "Bearer " + ingestToken);
  int rc = http.POST(body);
  if (rc >= 200 && rc < 300) {
    Serial.printf("[posted %d] %s %u\n", rc, model, (unsigned) id);
  } else {
    Serial.printf("[post-fail %d] %s\n", rc, http.errorToString(rc).c_str());
  }
  http.end();
}
