#include "zasder_post.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFiClientSecure.h>

#include <time.h>

#include "config_server.h"

// 5 consecutive 401s = the token is wrong. Wipe it from NVS and reboot
// so the firmware's "ingest_token empty → AP portal" path takes over,
// letting the user re-enter it without a reflash.
static constexpr int MAX_CONSECUTIVE_401 = 5;
static int consecutive401 = 0;

// Whitelist of rtl_433 models we POST to the backend. The 433 dongle
// hears everything in the band — TPMS, garage openers, neighbor weather
// stations, etc. — and POSTing all of it spams the backend with random
// device rows (e.g. "Secplus-v1" garage doors). Only the actual weather
// sensors are useful; everything else is dropped at the source.
//
// Returns the synthetic-MAC type tag for `model`, or 0 if the model is
// not on the whitelist (caller skips the POST). Type tags match
// sdr-relay so a LilyGO-sourced sensor lands on the same device row
// as a Pi/SDR-sourced one (last-write-wins UPSERT).
static uint8_t modelTypeTag(const char *model) {
  if (!model) return 0;
  if (strcmp(model, "Acurite-Atlas")   == 0) return 0x01;
  if (strcmp(model, "Fineoffset-WH24") == 0) return 0x02;
  if (strcmp(model, "Fineoffset-WH65B")== 0) return 0x02;
  if (strcmp(model, "Fineoffset-WS80") == 0) return 0x02;
  if (strcmp(model, "Fineoffset-WH32B")== 0) return 0x03;
  return 0;
}

static String synthMac(uint8_t typeTag, uint32_t id) {
  char buf[13];
  snprintf(buf, sizeof(buf), "5D5D%02X%02X%02X%02X",
           typeTag,
           (unsigned) ((id >> 16) & 0xFF),
           (unsigned) ((id >>  8) & 0xFF),
           (unsigned) ( id        & 0xFF));
  return String(buf);
}

// Lowest plausible "real time" UNIX epoch — anything below this means
// SNTP hasn't synced yet and we should NOT POST (backend would record
// "1970-01-01" timestamps which show up as "56 years ago" everywhere).
// 1700000000 = 2023-11-14, comfortably before any real boot time.
static constexpr time_t MIN_VALID_EPOCH = 1700000000;

static bool clockSynced() {
  return time(nullptr) >= MIN_VALID_EPOCH;
}

static String nowIsoUtc() {
  time_t now = time(nullptr);
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
    // OLED disabled — would have shown "post: no config" here
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

  // Whitelist: drop anything that isn't one of the known weather
  // sensors before we POST. Otherwise the 433 dongle's broad RX
  // creates a "Secplus-v1" device row for the neighbor's garage door,
  // and similar junk for TPMS, smoke detectors, etc.
  uint8_t typeTag = modelTypeTag(model);
  if (typeTag == 0) {
    return;
  }

  // Don't POST until NTP has synced. The backend would otherwise store
  // 1970-01-01 timestamps for early-boot packets and they'd show up as
  // "56 years ago" in the iOS app.
  if (!clockSynced()) {
    Serial.printf("[skip-no-clock] %s %u\n", model, (unsigned) id);
    return;
  }

  // Build the outgoing observation. /ingest/custom accepts:
  //   {device:{id,name,location}, timestamp_utc, source,
  //    outdoor:{tempf, humidity},
  //    wind:{windspeedmph, windgustmph, winddir},
  //    rain:{yearly_in, hourly_in},
  //    pressure:{baromrelin},
  //    indoor:{tempf,humidity,pressure_inhg}}
  // Intentionally omitting device.name — the Pi's sdr-relay already
  // POSTs a friendly "AcuRite Atlas (SDR)" / "WS-2000 (SDR)" name on
  // first sight; leaving name unset here means the backend keeps that
  // friendly name on UPSERT instead of being overwritten with the raw
  // rtl_433 model string ("Acurite-Atlas", etc.).
  JsonDocument out;
  auto device       = out["device"].to<JsonObject>();
  device["id"]      = synthMac(typeTag, id);
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

  // Rain block intentionally NOT sent. rtl_433 emits a lifetime
  // cumulative rain counter; turning that into a useful yearly_in
  // requires a baseline + delta tracker (the Pi's sdr-relay has one,
  // calibrated against AWN's yearlyrainin at deploy time). The LilyGO
  // doesn't, and posting the raw counter as "yearly_in" overwrites the
  // Pi's correct value via last-write-wins UPSERT. If you're running
  // LilyGO-only (no Pi), wire up baselining here; otherwise let the Pi
  // own rain reporting for that device row.

  // ── pressure block ── (typically only WH32B indoor; outdoor sensors
  // rarely include it). Convert hPa → inHg.
  if (in["pressure_hPa"].is<float>()) {
    auto p = out["pressure"].to<JsonObject>();
    p["baromrelin"] = hpa_to_inhg(in["pressure_hPa"].as<float>());
  }

  String body;
  serializeJson(out, body);

  // Static TLS client + HTTP client: reused across POSTs so we don't
  // allocate fresh mbedTLS contexts every packet. Each fresh context
  // is ~16 KB and the heap fragments fast; we saw "SSL - Memory
  // allocation failed" → StoreProhibited panic within ~5 minutes of
  // per-call alloc.
  static WiFiClientSecure tls;
  static bool tlsConfigured = false;
  if (!tlsConfigured) {
    tls.setInsecure();  // public template default; pin a CA in build
                        // flags via setCACert() if you have one.
    tlsConfigured = true;
  }
  static HTTPClient http;
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
    consecutive401 = 0;
  } else if (rc == 401) {
    consecutive401++;
    Serial.printf("[post-fail 401] (consecutive=%d)\n", consecutive401);
    if (consecutive401 >= MAX_CONSECUTIVE_401) {
      Serial.println("Token rejected repeatedly — wiping NVS token. "
                     "Re-provision via POST /provision over HTTP.");
      http.end();
      ZasderConfigServer::wipeIngestToken();
      consecutive401 = 0;
      ZasderConfigServer::notePostResult(rc);
      return;
    }
  } else {
    Serial.printf("[post-fail %d] %s\n", rc, http.errorToString(rc).c_str());
  }
  ZasderConfigServer::notePostResult(rc);
  http.end();
}
