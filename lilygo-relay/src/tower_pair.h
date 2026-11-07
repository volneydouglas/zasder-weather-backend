// A paired AcuRite temperature/humidity sensor (the 592TXR "tower", the
// 609TXC or the temperature-only 606TX) merged into the Atlas's post, the
// way the 915 board merges a WH32B into a Fineoffset outdoor post.
//
// Without this the 433 board drops every tower packet: the model is not on
// the whitelist, and discovery mode (forward_all) posts it as a separate
// "Acurite-Tower 1234" station rather than as part of the Atlas (mirror #8).
// The tower has no barometer; an AcuRite display's pressure is measured by
// the display itself and never transmitted.
//
// Header-only and Arduino-free so the native test env runs the exact logic
// the board runs. The caller passes millis() in. Times are uint32_t, the
// width of the ESP32's millis(), so the unsigned age arithmetic stays right
// across its 49.7-day wrap on the host tests too (a 64-bit unsigned long
// there would never wrap).
#pragma once

#include <ArduinoJson.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

#include "field_map.h"

namespace TowerPair {

// The /provision `tower_id` value: decimal digits only, at most UINT32_MAX.
// strtoul would read "12ab" as 12 and "-1" as 4294967295, silently pairing
// a different sensor than the one typed.
inline bool parseId(const char *s, uint32_t *out) {
  if (!s || !*s) return false;
  uint64_t v = 0;
  for (const char *p = s; *p; p++) {
    if (*p < '0' || *p > '9') return false;
    v = v * 10 + (uint64_t) (*p - '0');
    if (v > 0xFFFFFFFFull) return false;
  }
  *out = (uint32_t) v;
  return true;
}

// rtl_433 model strings for AcuRite T/H sensors an Atlas owner may pair.
inline bool isTowerModel(const char *model) {
  if (!model) return false;
  return strcmp(model, "Acurite-Tower") == 0 ||
         strcmp(model, "Acurite-609TXC") == 0 ||
         strcmp(model, "Acurite-606TX") == 0;
}

struct Cache {
  bool valid = false;
  float tempf = NAN;
  float humidity = NAN;
  int batteryOk = -1;            // 1 ok, 0 low, -1 not reported
  uint32_t id = 0;               // the paired id this reading came from
  uint32_t ms = 0;               // millis() of the last capture
};

// The tower transmits about every 16 s (the 609TXC about every 60 s);
// 10 minutes rides out missed packets but lets a dead sensor's last
// reading stop being posted as live.
constexpr uint32_t MAX_AGE_MS = 10UL * 60UL * 1000UL;

// Where the paired sensor's readings land on the Atlas's row.
enum class Role { Channel, Indoor };

// Captures `in` into `cache` when it is the paired tower: a tower model
// whose id equals `pairedId`. Returns true when the packet was the paired
// tower (the caller then posts nothing of its own for it), whether or not
// it carried a usable reading. `pairedId` 0 means nothing is paired.
inline bool capture(JsonVariantConst in, uint32_t pairedId, Cache &cache,
                    uint32_t nowMs) {
  if (pairedId == 0) return false;
  if (!isTowerModel(in["model"].as<const char *>())) return false;
  if (!FieldMap::isNum(in["id"]) || in["id"].as<uint32_t>() != pairedId)
    return false;
  float t = NAN, h = NAN;
  if (FieldMap::isNum(in["temperature_C"]))
    t = FieldMap::c_to_f(in["temperature_C"].as<float>());
  else if (FieldMap::isNum(in["temperature_F"]))
    t = in["temperature_F"].as<float>();
  if (FieldMap::isNum(in["humidity"])) h = in["humidity"].as<float>();
  // A frame with neither reading (a battery-only or partial decode) keeps
  // the previous reading and its age rather than arming an empty merge.
  if (isnan(t) && isnan(h)) return true;
  cache.tempf = t;
  cache.humidity = h;
  cache.batteryOk = in["battery_ok"].is<int>() ? (in["battery_ok"].as<int>() ? 1 : 0) : -1;
  cache.ms = nowMs;
  cache.id = pairedId;
  cache.valid = true;
  return true;
}

// Adds the cached reading to an Atlas post, only while it is from the
// sensor paired NOW (`pairedId`): unpairing or pairing another tower stops
// the old one's reading at once instead of after its ten-minute age
// (CodeRabbit, PR #141). Indoor: indoor.tempf/humidity +
// batteries.battin for Role::Indoor, extra.temp1f/humidity1 +
// batteries.batt1 for Role::Channel (the AWN channel-sensor names the
// backend's /ingest/custom passthrough accepts). Nothing once the reading
// is older than MAX_AGE_MS. Battery flags use the stored convention,
// 1 = ok, 0 = low.
//
// A stale reading is dropped from the cache, not just skipped: the age is
// wrapping uint32 arithmetic, so a tower silent for 2^32 ms (49.7 days,
// reachable with the nightly restart off) would otherwise look fresh again
// for ten minutes. Atlas posts arrive every ~10 s, so the drop happens
// within seconds of the age limit (CodeRabbit, PR #141).
inline void merge(Cache &cache, uint32_t pairedId, Role role,
                  uint32_t nowMs, JsonDocument &out) {
  if (!cache.valid || pairedId == 0 || cache.id != pairedId) return;
  if ((uint32_t) (nowMs - cache.ms) > MAX_AGE_MS) {
    cache.valid = false;
    return;
  }
  const bool indoor = role == Role::Indoor;
  JsonObject block = out[indoor ? "indoor" : "extra"].to<JsonObject>();
  if (!isnan(cache.tempf)) block[indoor ? "tempf" : "temp1f"] = cache.tempf;
  if (!isnan(cache.humidity)) block[indoor ? "humidity" : "humidity1"] = cache.humidity;
  if (cache.batteryOk >= 0)
    out["batteries"][indoor ? "battin" : "batt1"] = cache.batteryOk;
}

}  // namespace TowerPair
