// The paired AcuRite tower (src/tower_pair.h) and the /status heard list
// (src/heard_list.h), on real rtl_433 decoder shapes. Mirror #8: an Atlas
// owner's 592TXR tower never reached the server.
#include <unity.h>
#include <ArduinoJson.h>
#include "heard_list.h"
#include "tower_pair.h"
#include <math.h>
#include <string.h>

static JsonDocument parse(const char *json) {
  JsonDocument d;
  deserializeJson(d, json);
  return d;
}

static constexpr uint32_t PAIRED = 2310;

void setUp() {}
void tearDown() {}

// rtl_433 Acurite-Tower (acurite.c, acurite_txr_decode): temperature_C is
// DATA_DOUBLE, humidity and battery_ok DATA_INT, channel a letter.
static const char *TOWER =
    "{\"model\":\"Acurite-Tower\",\"id\":2310,\"channel\":\"A\","
    "\"battery_ok\":1,\"temperature_C\":22.5,\"humidity\":41,\"mic\":\"CHECKSUM\"}";

void test_the_paired_tower_is_captured() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TEST_ASSERT_TRUE(TowerPair::capture(in, 2310, c, 1000));
  TEST_ASSERT_TRUE(c.valid);
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 72.5f, c.tempf);
  TEST_ASSERT_EQUAL_FLOAT(41.0f, c.humidity);
  TEST_ASSERT_EQUAL_INT(1, c.batteryOk);
  TEST_ASSERT_EQUAL_UINT32(1000, c.ms);
}

void test_another_tower_or_no_pairing_is_not_captured() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  // A neighbor's tower (different id) and an unpaired board both leave the
  // packet to the normal path: dropped, or posted in discovery mode.
  TEST_ASSERT_FALSE(TowerPair::capture(in, 999, c, 1000));
  TEST_ASSERT_FALSE(TowerPair::capture(in, 0, c, 1000));
  TEST_ASSERT_FALSE(c.valid);
  // The Atlas is never a tower, even when its id matches.
  JsonDocument atlas = parse(
      "{\"model\":\"Acurite-Atlas\",\"id\":2310,\"temperature_F\":70.0,\"humidity\":30}");
  TEST_ASSERT_FALSE(TowerPair::capture(atlas, 2310, c, 1000));
  TEST_ASSERT_FALSE(c.valid);
}

void test_the_609txc_and_606tx_pair_too() {
  TowerPair::Cache c;
  JsonDocument txc = parse(
      "{\"model\":\"Acurite-609TXC\",\"id\":194,\"battery_ok\":0,"
      "\"temperature_C\":-3.0,\"humidity\":88,\"status\":2}");
  TEST_ASSERT_TRUE(TowerPair::capture(txc, 194, c, 5));
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 26.6f, c.tempf);
  TEST_ASSERT_EQUAL_INT(0, c.batteryOk);
  // The 606TX measures temperature only: humidity stays absent, not 0.
  TowerPair::Cache t;
  JsonDocument tx = parse(
      "{\"model\":\"Acurite-606TX\",\"id\":77,\"battery_ok\":1,\"temperature_C\":10.0}");
  TEST_ASSERT_TRUE(TowerPair::capture(tx, 77, t, 5));
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 50.0f, t.tempf);
  TEST_ASSERT_TRUE(isnan(t.humidity));
}

void test_a_reading_less_frame_keeps_the_last_reading() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TowerPair::capture(in, 2310, c, 1000);
  JsonDocument empty = parse("{\"model\":\"Acurite-Tower\",\"id\":2310,\"battery_ok\":1}");
  // Still "the paired tower" (the caller posts nothing for it) ...
  TEST_ASSERT_TRUE(TowerPair::capture(empty, 2310, c, 9000));
  // ... but the reading and its age are the previous frame's.
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 72.5f, c.tempf);
  TEST_ASSERT_EQUAL_UINT32(1000, c.ms);
}

void test_channel_merge_uses_the_awn_channel_names() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TowerPair::capture(in, 2310, c, 1000);
  JsonDocument out;
  TowerPair::merge(c, PAIRED, TowerPair::Role::Channel, 2000, out);
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 72.5f, out["extra"]["temp1f"].as<float>());
  TEST_ASSERT_EQUAL_FLOAT(41.0f, out["extra"]["humidity1"].as<float>());
  TEST_ASSERT_EQUAL_INT(1, out["batteries"]["batt1"].as<int>());
  TEST_ASSERT_FALSE(out["indoor"].is<JsonObject>());
}

void test_indoor_merge_fills_the_indoor_block() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TowerPair::capture(in, 2310, c, 1000);
  JsonDocument out;
  TowerPair::merge(c, PAIRED, TowerPair::Role::Indoor, 2000, out);
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 72.5f, out["indoor"]["tempf"].as<float>());
  TEST_ASSERT_EQUAL_FLOAT(41.0f, out["indoor"]["humidity"].as<float>());
  TEST_ASSERT_EQUAL_INT(1, out["batteries"]["battin"].as<int>());
  // No barometer in a tower: no pressure is invented.
  TEST_ASSERT_FALSE(out["indoor"]["pressure_inhg"].is<float>());
  TEST_ASSERT_FALSE(out["pressure"].is<JsonObject>());
  TEST_ASSERT_FALSE(out["extra"].is<JsonObject>());
}

void test_a_stale_or_absent_reading_is_not_merged() {
  TowerPair::Cache never;
  JsonDocument out;
  TowerPair::merge(never, PAIRED, TowerPair::Role::Channel, 1000, out);
  TEST_ASSERT_TRUE(out.isNull());

  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TowerPair::capture(in, 2310, c, 1000);
  JsonDocument fresh, stale;
  TowerPair::merge(c, PAIRED, TowerPair::Role::Channel, 1000 + TowerPair::MAX_AGE_MS, fresh);
  TEST_ASSERT_TRUE(fresh["extra"]["temp1f"].is<float>());
  TowerPair::merge(c, PAIRED, TowerPair::Role::Channel, 1001 + TowerPair::MAX_AGE_MS, stale);
  TEST_ASSERT_TRUE(stale.isNull());
}

void test_merge_age_survives_the_millis_wrap() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TowerPair::capture(in, 2310, c, 0xFFFFFF00UL);   // just before the wrap
  JsonDocument out;
  TowerPair::merge(c, PAIRED, TowerPair::Role::Channel, 0x00000100UL, out);   // 512 ms later
  TEST_ASSERT_TRUE(out["extra"]["temp1f"].is<float>());
}

void test_unreported_battery_is_left_out() {
  TowerPair::Cache c;
  JsonDocument in = parse(
      "{\"model\":\"Acurite-Tower\",\"id\":5,\"temperature_C\":20.0,\"humidity\":50}");
  TowerPair::capture(in, 5, c, 1);
  JsonDocument out;
  TowerPair::merge(c, 5, TowerPair::Role::Channel, 2, out);
  TEST_ASSERT_FALSE(out["batteries"].is<JsonObject>());
}

void test_parse_id_takes_only_a_clean_decimal() {
  uint32_t id = 7;
  TEST_ASSERT_TRUE(TowerPair::parseId("2310", &id));
  TEST_ASSERT_EQUAL_UINT32(2310, id);
  TEST_ASSERT_TRUE(TowerPair::parseId("0", &id));
  TEST_ASSERT_EQUAL_UINT32(0, id);
  TEST_ASSERT_TRUE(TowerPair::parseId("4294967295", &id));
  id = 7;
  TEST_ASSERT_FALSE(TowerPair::parseId("12ab", &id));
  TEST_ASSERT_FALSE(TowerPair::parseId("-1", &id));
  TEST_ASSERT_FALSE(TowerPair::parseId("4294967296", &id));
  TEST_ASSERT_FALSE(TowerPair::parseId("", &id));
  TEST_ASSERT_FALSE(TowerPair::parseId(" 12", &id));
  TEST_ASSERT_EQUAL_UINT32(7, id);   // a refused value never lands
}

void test_heard_list_keeps_distinct_sensors_newest_first() {
  HeardList::List h;
  h.note("Acurite-Atlas", 711, 100);
  h.note("Acurite-Tower", 2310, 200);
  h.note("Acurite-Atlas", 711, 300);   // repeats move to the front
  TEST_ASSERT_EQUAL_INT(2, h.count);
  TEST_ASSERT_EQUAL_STRING("Acurite-Atlas #711", h.label[0]);
  TEST_ASSERT_EQUAL_UINT32(300, h.ms[0]);
  TEST_ASSERT_EQUAL_STRING("Acurite-Tower #2310", h.label[1]);
  TEST_ASSERT_EQUAL_UINT32(200, h.ms[1]);
}

void test_heard_list_drops_the_oldest_when_full() {
  HeardList::List h;
  char model[16];
  for (int i = 0; i < HeardList::SLOTS + 2; i++) {
    snprintf(model, sizeof(model), "M%d", i);
    h.note(model, (uint32_t) i, (unsigned long) i);
  }
  TEST_ASSERT_EQUAL_INT(HeardList::SLOTS, h.count);
  char newest[24], oldest[24];
  snprintf(newest, sizeof(newest), "M%d #%d", HeardList::SLOTS + 1, HeardList::SLOTS + 1);
  snprintf(oldest, sizeof(oldest), "M2 #2");
  TEST_ASSERT_EQUAL_STRING(newest, h.label[0]);
  TEST_ASSERT_EQUAL_STRING(oldest, h.label[HeardList::SLOTS - 1]);
}

void test_unpairing_or_repairing_stops_the_old_reading_at_once() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TowerPair::capture(in, PAIRED, c, 1000);
  JsonDocument unpaired, repaired, same;
  TowerPair::merge(c, 0, TowerPair::Role::Channel, 1500, unpaired);
  TEST_ASSERT_TRUE(unpaired.isNull());
  TowerPair::merge(c, 999, TowerPair::Role::Channel, 1500, repaired);
  TEST_ASSERT_TRUE(repaired.isNull());
  TowerPair::merge(c, PAIRED, TowerPair::Role::Channel, 1500, same);
  TEST_ASSERT_TRUE(same["extra"]["temp1f"].is<float>());
}

void test_a_stale_reading_never_comes_back_after_the_wrap() {
  TowerPair::Cache c;
  JsonDocument in = parse(TOWER);
  TowerPair::capture(in, PAIRED, c, 1000);
  JsonDocument stale, wrapped;
  TowerPair::merge(c, PAIRED, TowerPair::Role::Channel, 1001 + TowerPair::MAX_AGE_MS, stale);
  TEST_ASSERT_TRUE(stale.isNull());
  TEST_ASSERT_FALSE(c.valid);
  // 2^32 ms later millis() reads the capture time again; the reading is gone.
  TowerPair::merge(c, PAIRED, TowerPair::Role::Channel, 1500, wrapped);
  TEST_ASSERT_TRUE(wrapped.isNull());
}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_the_paired_tower_is_captured);
  RUN_TEST(test_another_tower_or_no_pairing_is_not_captured);
  RUN_TEST(test_the_609txc_and_606tx_pair_too);
  RUN_TEST(test_a_reading_less_frame_keeps_the_last_reading);
  RUN_TEST(test_channel_merge_uses_the_awn_channel_names);
  RUN_TEST(test_indoor_merge_fills_the_indoor_block);
  RUN_TEST(test_a_stale_or_absent_reading_is_not_merged);
  RUN_TEST(test_merge_age_survives_the_millis_wrap);
  RUN_TEST(test_unreported_battery_is_left_out);
  RUN_TEST(test_unpairing_or_repairing_stops_the_old_reading_at_once);
  RUN_TEST(test_a_stale_reading_never_comes_back_after_the_wrap);
  RUN_TEST(test_parse_id_takes_only_a_clean_decimal);
  RUN_TEST(test_heard_list_keeps_distinct_sensors_newest_first);
  RUN_TEST(test_heard_list_drops_the_oldest_when_full);
  return UNITY_END();
}
