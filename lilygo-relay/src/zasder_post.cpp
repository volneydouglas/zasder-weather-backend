#include "zasder_post.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFiClientSecure.h>

#include <time.h>

#include "config_server.h"
#include "root_ca.h"

// 5 consecutive 401s = the token is wrong. Wipe it from NVS; the board
// stays on Wi-Fi and LOCKED (provisioned flag kept), serving /provision
// so the user can re-enter a token via the setup key — no reflash, no
// anonymous re-provisioning window.
static constexpr int MAX_CONSECUTIVE_401 = 5;
static int consecutive401 = 0;

// Whitelist of rtl_433 models we POST to the backend. The 433 dongle
// hears everything in the band — TPMS, garage openers, neighbor weather
// stations, etc. — and POSTing all of it spams the backend with random
// device rows (e.g. "Secplus-v1" garage doors). Only the actual weather
// sensors are useful; everything else is dropped at the source.
//
// Fineoffset-WH32B is handled separately (NOT in this whitelist): its
// temp/humid/pressure are cached and merged into the outdoor WH24/WH65/
// WS80 post's `indoor` + `pressure` blocks instead of being POSTed as
// its own device row. Same trick the Pi's sdr-relay uses — gives the
// outdoor card pressure + indoor tiles without spawning a duplicate
// indoor-only device row in the iOS app.
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
  return 0;
}

// Cached WH32B reading — populated whenever the 915 dongle hears a
// WH32B packet, merged into every outdoor (WH24/WH65/WS80) post.
// `valid` flips true on first capture; outdoor posts include the
// indoor + pressure blocks only when this is true.
struct WH32BCache {
  bool   valid = false;
  float  tempf = NAN;
  float  humidity = NAN;
  float  pressure_inhg = NAN;
};
static WH32BCache wh32b;

// Generic type tag for models forwarded via the opt-in forward_all path
// (any decoded rtl_433 station that isn't on the whitelist). The synthetic
// MAC mixes a hash of the model string into the id bytes so two different
// models that happen to share an id don't collide on one device row.
static constexpr uint8_t GENERIC_TYPE_TAG = 0x0A;

// djb2, folded to one byte — stable across boots (pure function of the
// model string), so the same sensor always lands on the same device row.
static uint8_t modelHash8(const char *model) {
  uint32_t h = 5381;
  for (const char *p = model; *p; p++) h = ((h << 5) + h) ^ (uint8_t) *p;
  return (uint8_t) (h ^ (h >> 8) ^ (h >> 16) ^ (h >> 24));
}

// A packet is "weather-shaped" if it carries at least one field we can map.
// Gates the forward_all path so garage doors / TPMS / doorbells the 433
// dongle also hears don't become backend device rows.
static bool hasWeatherFields(const JsonDocument &in) {
  static const char *keys[] = {
    "temperature_C", "temperature_F", "humidity",
    "wind_avg_m_s", "wind_avg_mi_h", "wind_avg_km_h",
    "rain_mm", "rain_in", "uv", "uvi", "light_lux", "lux",
    "pressure_hPa",
  };
  for (const char *k : keys) {
    if (in[k].is<float>() || in[k].is<int>() || in[k].is<double>()) return true;
  }
  return false;
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

// Magnus-Tetens dew point in °F from tempf + humidity %. Matches the
// formula sdr-relay uses so two sources posting to the same device
// row don't disagree on dew point by a fraction. Returns NaN if
// humidity is non-positive (rare sensor glitch) — caller must check
// isnan() before using.
static float dew_point_f(float temp_f, float humidity_pct) {
  if (humidity_pct <= 0.0f) return NAN;
  float t_c = (temp_f - 32.0f) * 5.0f / 9.0f;
  const float a = 17.625f, b = 243.04f;
  float gamma = logf(humidity_pct / 100.0f) + (a * t_c) / (b + t_c);
  float dp_c = (b * gamma) / (a - gamma);
  return dp_c * 9.0f / 5.0f + 32.0f;
}

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

  // Fineoffset-WH32B: cache + return. We never POST the WH32B as its
  // own device (operator doesn't want an indoor-only station row);
  // instead, the next WH24/WH65/WS80 outdoor post merges these values
  // into its `indoor` + `pressure` blocks.
  if (strcmp(model, "Fineoffset-WH32B") == 0) {
    if (in["temperature_C"].is<float>())
      wh32b.tempf = c_to_f(in["temperature_C"].as<float>());
    if (in["humidity"].is<float>())
      wh32b.humidity = in["humidity"].as<float>();
    if (in["pressure_hPa"].is<float>())
      wh32b.pressure_inhg = hpa_to_inhg(in["pressure_hPa"].as<float>());
    wh32b.valid = true;
    Serial.printf("[wh32b-cache] tempf=%.1f hum=%.1f press_inhg=%.2f\n",
                  wh32b.tempf, wh32b.humidity, wh32b.pressure_inhg);
    return;
  }

  // Whitelist: drop anything that isn't one of the known weather
  // sensors before we POST. Otherwise the 433 dongle's broad RX
  // creates a "Secplus-v1" device row for the neighbor's garage door,
  // and similar junk for TPMS, smoke detectors, etc.
  //
  // forward_all (opt-in, POST /provision forward_all=1) relaxes this to
  // "any decoded model that carries weather fields" — the forward-compat
  // path for stations the bundled decoders learn later (e.g. AcuRite
  // Optimus once rtl_433 #3444 lands) and for oddballs like LaCrosse.
  // The generic mapping below already handles the standard rtl_433 field
  // names in both metric + imperial variants.
  bool generic = false;
  uint8_t typeTag = modelTypeTag(model);
  if (typeTag == 0) {
    if (!ZasderConfigServer::forwardAll || !hasWeatherFields(in)) {
      return;
    }
    generic = true;
    typeTag = GENERIC_TYPE_TAG;
    // Fold the model name into the id so distinct models with equal ids
    // get distinct device rows.
    id = ((uint32_t) modelHash8(model) << 16) ^ (id & 0xFFFFFF);
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
  // Generic (forward_all) rows get the rtl_433 model + id as their name so
  // multiple discovered stations are tellable-apart in the app. Whitelisted
  // models keep omitting the name (see comment above) so an sdr-relay or
  // operator-set friendly name survives the UPSERT.
  if (generic) {
    device["name"] = String(model) + " " + String((unsigned) in["id"].as<uint32_t>());
  }
  out["timestamp_utc"] = nowIsoUtc();
  out["source"]        = ZASDER_SOURCE_TAG;

  // ── outdoor block ──
  if (in["temperature_C"].is<float>() || in["temperature_F"].is<float>() ||
      in["humidity"].is<float>()) {
    auto outdoor = out["outdoor"].to<JsonObject>();
    if (in["temperature_C"].is<float>())
      outdoor["tempf"] = c_to_f(in["temperature_C"].as<float>());
    else if (in["temperature_F"].is<float>())
      outdoor["tempf"] = in["temperature_F"].as<float>();
    // Field names MUST match backend/app/ingest._flatten() schema:
    //   outdoor: tempf, feels_like, dew_point_f, humidity, uv, solar_wm2
    //   wind:    speed_mph, gust_mph, direction
    //   indoor:  tempf, humidity, pressure_inhg
    //   pressure: relative_inhg
    // Mis-naming silently drops the field; the iOS card then renders
    // without that tile.
    copyIf(in, "humidity",         outdoor, "humidity");
    copyIf(in, "uv",               outdoor, "uv");
    copyIf(in, "uvi",              outdoor, "uv");
    // Solar irradiance: backend schema only takes outdoor.solar_wm2. If
    // the decoder emits W/m² directly (rare; some Ecowitt variants),
    // use it; otherwise derive from lux via the standard 126.7 lux/(W·m⁻²)
    // sunlight approximation. rtl_433 names differ by decoder:
    //   Atlas emits "lux"; Fineoffset-WH65B/WH24 emits "light_lux".
    if (in["solar_radiation"].is<float>()) {
      outdoor["solar_wm2"] = in["solar_radiation"].as<float>();
    } else {
      float lux = NAN;
      if (in["light_lux"].is<float>()) lux = in["light_lux"].as<float>();
      else if (in["lux"].is<float>())  lux = in["lux"].as<float>();
      if (!isnan(lux)) {
        outdoor["solar_wm2"] = lux / 126.7f;
      }
    }
    // Computed dew point when we have both temp + humidity. Backend
    // accepts dew_point_f directly (no further derivation needed).
    if (outdoor["tempf"].is<float>() && outdoor["humidity"].is<float>()) {
      float dp = dew_point_f(outdoor["tempf"].as<float>(),
                             outdoor["humidity"].as<float>());
      if (!isnan(dp)) outdoor["dew_point_f"] = dp;
    }
  }

  // ── wind block ── rtl_433 decoders emit m/s, mi/h, or km/h depending
  // on the protocol's native unit; accept all three.
  if (in["wind_avg_m_s"].is<float>() || in["wind_max_m_s"].is<float>() ||
      in["wind_avg_mi_h"].is<float>() || in["wind_avg_km_h"].is<float>() ||
      in["wind_dir_deg"].is<int>()) {
    auto wind = out["wind"].to<JsonObject>();
    if (in["wind_avg_m_s"].is<float>())
      wind["speed_mph"] = ms_to_mph(in["wind_avg_m_s"].as<float>());
    else if (in["wind_avg_mi_h"].is<float>())
      wind["speed_mph"] = in["wind_avg_mi_h"].as<float>();
    else if (in["wind_avg_km_h"].is<float>())
      wind["speed_mph"] = in["wind_avg_km_h"].as<float>() * 0.621371f;
    if (in["wind_max_m_s"].is<float>())
      wind["gust_mph"] = ms_to_mph(in["wind_max_m_s"].as<float>());
    else if (in["wind_max_mi_h"].is<float>())
      wind["gust_mph"] = in["wind_max_mi_h"].as<float>();
    else if (in["wind_max_km_h"].is<float>())
      wind["gust_mph"] = in["wind_max_km_h"].as<float>() * 0.621371f;
    copyIf(in, "wind_dir_deg", wind, "direction");
  }

  // ── rain block ── Send the raw cumulative counter as yearly_in.
  // The backend's rain_rollups() differences yearlyrainin against
  // historical values at local-time period boundaries, so daily /
  // hourly / weekly / monthly come out correct after the first ~24h
  // of history (the very first reading produces None for daily, which
  // iOS handles by hiding the cell — subsequent readings populate
  // properly). Atlas emits rain_in (inches); Fineoffset emits
  // rain_mm (millimeters).
  //
  // CAVEAT: yearlyrainin will look big on first deploy because it's
  // the sensor's lifetime cumulative, not 0-since-Jan-1. The rollups
  // are still correct since they're differences.
  float rain_in_total = NAN;
  if (in["rain_in"].is<float>()) {
    rain_in_total = in["rain_in"].as<float>();
  } else if (in["rain_mm"].is<float>()) {
    rain_in_total = in["rain_mm"].as<float>() * 0.0393701f;
  }
  if (!isnan(rain_in_total)) {
    auto rain = out["rain"].to<JsonObject>();
    rain["yearly_in"] = rain_in_total;
  }

  // ── pressure block ── (only relevant if a paired indoor sensor
  // forwards barometer; outdoor stations don't have one). Convert
  // hPa → inHg.
  if (in["pressure_hPa"].is<float>()) {
    auto p = out["pressure"].to<JsonObject>();
    p["relative_inhg"] = hpa_to_inhg(in["pressure_hPa"].as<float>());
  }

  // ── WH32B merge ── Fineoffset outdoor stations don't carry indoor
  // temp/humidity or pressure on their own, but a paired WH32B does.
  // We cache the latest WH32B reading globally and attach it to each
  // outdoor post here — same pattern as sdr-relay/sdr_relay.py's
  // handle_wh24() does with WH32B_ID. typeTag 0x02 = Fineoffset
  // outdoor (WH24/WH65/WS80); Atlas (0x01) doesn't get the merge
  // because Atlas + WH32B aren't typically deployed together.
  if (typeTag == 0x02 && wh32b.valid) {
    auto indoor = out["indoor"].to<JsonObject>();
    if (!isnan(wh32b.tempf))         indoor["tempf"]         = wh32b.tempf;
    if (!isnan(wh32b.humidity))      indoor["humidity"]      = wh32b.humidity;
    if (!isnan(wh32b.pressure_inhg)) indoor["pressure_inhg"] = wh32b.pressure_inhg;
    // Also expose pressure at top-level so the iOS Pressure tile
    // renders. Backend stores both indoor.pressure_inhg and
    // pressure.relative_inhg into the same baromrelin column.
    if (!isnan(wh32b.pressure_inhg)) {
      auto p = out["pressure"].to<JsonObject>();
      p["relative_inhg"] = wh32b.pressure_inhg;
    }
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
#if TLS_INSECURE
    // Dev path: self-signed backend, internal CA, plain HTTP+TLS
    // termination upstream. Set -DTLS_INSECURE=1 in platformio.ini
    // only when you accept that anyone on the Wi-Fi path can read
    // the ingest token from the in-flight POST.
    tls.setInsecure();
#else
    // Production: pin Let's Encrypt's ISRG Root X1 (covers Fly.io
    // edge + any LE-issued custom domain like weather.zasder.com).
    tls.setCACert(ZASDER_ROOT_CA);
#endif
    tlsConfigured = true;
  }
  static HTTPClient http;
  String url = backendUrl + "/ingest/custom";
  if (!http.begin(tls, url)) {
    Serial.printf("[http-begin-fail] %s\n", url.c_str());
    return;
  }
  // Bound the POST so a stalled TLS connect/read (e.g. after a Wi-Fi flap)
  // can't block loop() indefinitely. Well under the 60s loop watchdog.
  http.setConnectTimeout(8000);
  http.setTimeout(8000);
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
                     "Board stays LOCKED; re-pair via POST /provision "
                     "using the setup key shown on the OLED "
                     "(setup_key=<...>), not anonymously.");
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
