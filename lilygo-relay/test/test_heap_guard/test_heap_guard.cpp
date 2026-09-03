// Host-side tests (`pio test -e native`) for HeapGuard — the state machine
// in main.cpp's loop() that stands between rtl_433_ESP's unchecked
// heap_caps_calloc(sizeof(pulse_data_t)) and a StoreProhibited panic.
// See src/heap_guard.h for the failure it closes.

#include <unity.h>

#include "heap_guard.h"

void setUp() {}
void tearDown() {}

static const size_t   NEED    = 13776;   // sizeof(pulse_data_t) + 4 KB margin
static const uint32_t RESTART = 30000;

using V = HeapGuard::Verdict;

static void test_healthy_heap_drains_every_time() {
  HeapGuard g(NEED, RESTART);
  TEST_ASSERT_EQUAL(V::Drain, g.step(81908, 1000));
  TEST_ASSERT_EQUAL(V::Drain, g.step(32756, 2000));
  TEST_ASSERT_EQUAL(V::Drain, g.step(NEED,  3000));   // exactly enough drains
  TEST_ASSERT_FALSE(g.holding());
  TEST_ASSERT_EQUAL_UINT32(0, g.trips());
}

static void test_transient_dip_holds_then_recovers() {
  HeapGuard g(NEED, RESTART);
  TEST_ASSERT_EQUAL(V::Drain,   g.step(32756,    1000));
  // A POST in flight with trains stacking up: the largest block shrinks
  // below one train. First low sample is the logged/counted trip.
  TEST_ASSERT_EQUAL(V::Hold,    g.step(NEED - 1, 1001));
  TEST_ASSERT_TRUE(g.holding());
  TEST_ASSERT_EQUAL_UINT32(1, g.trips());
  // Subsequent low samples inside the window: quiet holds, no new trip.
  TEST_ASSERT_EQUAL(V::Holding, g.step(9000,     1500));
  TEST_ASSERT_EQUAL(V::Holding, g.step(9000,     20000));
  TEST_ASSERT_EQUAL_UINT32(1, g.trips());
  // The decoder task frees its trains: one Recovered (so it gets logged),
  // then plain Drain.
  TEST_ASSERT_EQUAL(V::Recovered, g.step(30000,  2400));
  TEST_ASSERT_FALSE(g.holding());
  TEST_ASSERT_EQUAL_UINT32(1399, g.lastHoldMs());   // 2400 - 1001
  TEST_ASSERT_EQUAL(V::Drain,     g.step(30000,  2401));
}

static void test_persistent_shortage_restarts_after_window() {
  HeapGuard g(NEED, RESTART);
  TEST_ASSERT_EQUAL(V::Hold,    g.step(5000, 100000));
  // Boundary: exactly the window is still a hold; one ms past it restarts.
  TEST_ASSERT_EQUAL(V::Holding, g.step(5000, 100000 + RESTART));
  TEST_ASSERT_EQUAL(V::Restart, g.step(5000, 100000 + RESTART + 1));
}

static void test_each_episode_counts_one_trip() {
  HeapGuard g(NEED, RESTART);
  TEST_ASSERT_EQUAL(V::Hold,      g.step(1000,  10));
  TEST_ASSERT_EQUAL(V::Recovered, g.step(40000, 20));
  TEST_ASSERT_EQUAL(V::Hold,      g.step(1000,  30));
  TEST_ASSERT_EQUAL(V::Holding,   g.step(1000,  40));
  TEST_ASSERT_EQUAL(V::Recovered, g.step(40000, 50));
  TEST_ASSERT_EQUAL_UINT32(2, g.trips());
}

static void test_millis_wrap_inside_a_hold_keeps_the_window() {
  // millis() wraps every ~49.7 days. A hold that starts 10 s before the
  // wrap must still restart 30 s after it started, not immediately (a
  // signed compare would see "now < since") and not never.
  HeapGuard g(NEED, RESTART);
  const uint32_t since = 0xFFFFFFFFu - 10000u;
  TEST_ASSERT_EQUAL(V::Hold,    g.step(5000, since));
  TEST_ASSERT_EQUAL(V::Holding, g.step(5000, 0xFFFFFFFFu));      // 10 s in
  TEST_ASSERT_EQUAL(V::Holding, g.step(5000, 19999u));           // ~30 s in
  TEST_ASSERT_EQUAL(V::Restart, g.step(5000, 20001u));           // past it
}

static void test_recovery_at_wrap_reports_true_duration() {
  HeapGuard g(NEED, RESTART);
  TEST_ASSERT_EQUAL(V::Hold,      g.step(5000,  0xFFFFFF00u));
  TEST_ASSERT_EQUAL(V::Recovered, g.step(40000, 0x00000100u));
  TEST_ASSERT_EQUAL_UINT32(0x200, g.lastHoldMs());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_healthy_heap_drains_every_time);
  RUN_TEST(test_transient_dip_holds_then_recovers);
  RUN_TEST(test_persistent_shortage_restarts_after_window);
  RUN_TEST(test_each_episode_counts_one_trip);
  RUN_TEST(test_millis_wrap_inside_a_hold_keeps_the_window);
  RUN_TEST(test_recovery_at_wrap_reports_true_duration);
  return UNITY_END();
}
