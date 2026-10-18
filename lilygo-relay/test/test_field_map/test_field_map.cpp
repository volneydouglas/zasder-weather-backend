// The board's rtl_433 -> /ingest/custom mapping (src/field_map.h), run on
// real decoder shapes. The Atlas sends UV and light in their own message
// (type 0x27, no temperature): mirror #8 and Volney's own Atlas lost both
// from 2026-08-11, the day the Pi's SDR relay stopped feeding that row.
#include <unity.h>
#include <ArduinoJson.h>
#include "field_map.h"
#include <math.h>

static JsonDocument parse(const char *json) {
  JsonDocument d;
  deserializeJson(d, json);
  return d;
}

static JsonDocument mapped(const char *json) {
  JsonDocument in = parse(json);
  JsonDocument out;
  FieldMap::mapReadings(in, out);
  return out;
}

void setUp() {}
void tearDown() {}

// rtl_433 Acurite-Atlas, message type 0x27: wind speed + uv + lux (both
// DATA_INT), no temperature or humidity.
static const char *ATLAS_UV_LUX =
    "{\"model\":\"Acurite-Atlas\",\"id\":711,\"channel\":\"A\","
    "\"message_type\":39,\"wind_avg_mi_h\":3.0,\"uv\":6,\"lux\":63350,"
    "\"exception\":0}";

void test_atlas_uv_message_carries_uv_and_light() {
  JsonDocument out = mapped(ATLAS_UV_LUX);
  TEST_ASSERT_TRUE(out["outdoor"].is<JsonObject>());
  TEST_ASSERT_TRUE(out["outdoor"]["uv"].is<float>());
  TEST_ASSERT_EQUAL_FLOAT(6.0f, out["outdoor"]["uv"].as<float>());
  TEST_ASSERT_TRUE(out["outdoor"]["solar_wm2"].is<float>());
  TEST_ASSERT_FLOAT_WITHIN(0.5f, 63350.0f / 126.7f,
                           out["outdoor"]["solar_wm2"].as<float>());
  // No temperature in this message, so none is invented.
  TEST_ASSERT_FALSE(out["outdoor"]["tempf"].is<float>());
  TEST_ASSERT_FALSE(out["outdoor"]["dew_point_f"].is<float>());
  TEST_ASSERT_EQUAL_FLOAT(3.0f, out["wind"]["speed_mph"].as<float>());
}

void test_atlas_temperature_message_is_unchanged() {
  // Type 0x25: temperature (double) + humidity (int).
  JsonDocument out = mapped(
      "{\"model\":\"Acurite-Atlas\",\"id\":711,\"message_type\":37,"
      "\"wind_avg_mi_h\":2.0,\"temperature_F\":71.5,\"humidity\":40}");
  TEST_ASSERT_EQUAL_FLOAT(71.5f, out["outdoor"]["tempf"].as<float>());
  TEST_ASSERT_EQUAL_FLOAT(40.0f, out["outdoor"]["humidity"].as<float>());
  TEST_ASSERT_TRUE(out["outdoor"]["dew_point_f"].is<float>());
  TEST_ASSERT_FALSE(out["outdoor"]["uv"].is<float>());
}

void test_atlas_rain_message_has_no_outdoor_block() {
  // Type 0x26: wind direction + rain only.
  JsonDocument out = mapped(
      "{\"model\":\"Acurite-Atlas\",\"id\":711,\"message_type\":38,"
      "\"wind_avg_mi_h\":1.0,\"wind_dir_deg\":225.0,\"rain_in\":1.23}");
  TEST_ASSERT_FALSE(out["outdoor"].is<JsonObject>());
  TEST_ASSERT_EQUAL_FLOAT(225.0f, out["wind"]["direction"].as<float>());
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 1.23f, out["rain"]["yearly_in"].as<float>());
}

void test_integer_light_lux_is_converted() {
  // Fineoffset-style "light_lux" as an integer still becomes solar_wm2.
  JsonDocument out = mapped(
      "{\"model\":\"Fineoffset-WH65B\",\"id\":125,\"temperature_C\":20.0,"
      "\"humidity\":50,\"light_lux\":12670,\"uvi\":3}");
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 100.0f, out["outdoor"]["solar_wm2"].as<float>());
  TEST_ASSERT_EQUAL_FLOAT(3.0f, out["outdoor"]["uv"].as<float>());
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 68.0f, out["outdoor"]["tempf"].as<float>());
}

void test_absent_readings_stay_absent() {
  JsonDocument out = mapped("{\"model\":\"Acurite-Atlas\",\"id\":711}");
  TEST_ASSERT_FALSE(out["outdoor"].is<JsonObject>());
  TEST_ASSERT_FALSE(out["wind"].is<JsonObject>());
  TEST_ASSERT_FALSE(out["rain"].is<JsonObject>());
  TEST_ASSERT_FALSE(out["pressure"].is<JsonObject>());
}

void test_non_finite_readings_are_dropped() {
  JsonDocument in;
  in["model"] = "Acurite-Atlas";
  in["temperature_F"] = INFINITY;
  in["uv"] = NAN;
  in["wind_avg_mi_h"] = 2.0;
  JsonDocument out;
  FieldMap::mapReadings(in, out);
  TEST_ASSERT_FALSE(out["outdoor"].is<JsonObject>());
  TEST_ASSERT_EQUAL_FLOAT(2.0f, out["wind"]["speed_mph"].as<float>());
}

void test_the_uv_light_mask_drops_only_uv_and_light() {
  JsonDocument in = parse(ATLAS_UV_LUX);
  JsonDocument out;
  FieldMap::mapReadings(in, out, true);
  TEST_ASSERT_FALSE(out["outdoor"].is<JsonObject>());
  TEST_ASSERT_EQUAL_FLOAT(3.0f, out["wind"]["speed_mph"].as<float>());
  JsonDocument t = parse("{\"model\":\"Acurite-Atlas\",\"temperature_F\":70.0,"
                         "\"humidity\":40,\"uv\":4,\"lux\":79550}");
  JsonDocument out2;
  FieldMap::mapReadings(t, out2, true);
  TEST_ASSERT_EQUAL_FLOAT(70.0f, out2["outdoor"]["tempf"].as<float>());
  TEST_ASSERT_FALSE(out2["outdoor"]["uv"].is<float>());
  TEST_ASSERT_FALSE(out2["outdoor"]["solar_wm2"].is<float>());
}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_atlas_uv_message_carries_uv_and_light);
  RUN_TEST(test_atlas_temperature_message_is_unchanged);
  RUN_TEST(test_atlas_rain_message_has_no_outdoor_block);
  RUN_TEST(test_integer_light_lux_is_converted);
  RUN_TEST(test_absent_readings_stay_absent);
  RUN_TEST(test_non_finite_readings_are_dropped);
  RUN_TEST(test_the_uv_light_mask_drops_only_uv_and_light);
  return UNITY_END();
}
