// Native tests for the Improv frame bounds logic (R6): this parser faces
// raw bytes off the USB UART and shipped two memory-safety bugs before
// extraction — the uint8_t-truncation OOB read and the ssid off-by-one.
// Every case here is one of those bugs' exact triggers, plus the healthy
// shapes that must keep working.
#include <unity.h>
#include <string.h>

#include "improv_frame.h"

void setUp(void) {}
void tearDown(void) {}

// ── RPC envelope ─────────────────────────────────────────────────────────

static void test_envelope_accepts_exact_fit(void) {
  // [cmd, dlen, payload×3] in a 5-byte frame.
  TEST_ASSERT_TRUE(improv_rpc_envelope_ok(3, 5));
  TEST_ASSERT_TRUE(improv_rpc_envelope_ok(0, 2));     // empty payload
}

static void test_envelope_rejects_short_frames(void) {
  TEST_ASSERT_FALSE(improv_rpc_envelope_ok(3, 4));    // payload overruns
  TEST_ASSERT_FALSE(improv_rpc_envelope_ok(0, 1));    // no room for dlen
  TEST_ASSERT_FALSE(improv_rpc_envelope_ok(0, 0));
}

static void test_envelope_immune_to_uint8_wrap(void) {
  // The R6 bug: (uint8_t)(2 + 254) == 0 passed every check. dlen=254 in a
  // 2-byte frame must be rejected — in uint8_t math it was accepted.
  TEST_ASSERT_FALSE(improv_rpc_envelope_ok(254, 2));
  TEST_ASSERT_FALSE(improv_rpc_envelope_ok(255, 3));
  // ...and a genuinely large frame that really does fit still passes.
  TEST_ASSERT_TRUE(improv_rpc_envelope_ok(253, 255));
}

// ── WIFI_SETTINGS spans ──────────────────────────────────────────────────

static void test_spans_happy_path(void) {
  // ssid "ab", pass "xyz".
  const uint8_t d[] = {2, 'a', 'b', 3, 'x', 'y', 'z'};
  ImprovWifiSpans s = improv_wifi_spans(d, sizeof(d));
  TEST_ASSERT_TRUE(s.ok);
  TEST_ASSERT_EQUAL(1, s.ssid_off);
  TEST_ASSERT_EQUAL(2, s.ssid_len);
  TEST_ASSERT_EQUAL(4, s.pass_off);
  TEST_ASSERT_EQUAL(3, s.pass_len);
}

static void test_spans_open_network_empty_password(void) {
  const uint8_t d[] = {2, 'a', 'b', 0};
  ImprovWifiSpans s = improv_wifi_spans(d, sizeof(d));
  TEST_ASSERT_TRUE(s.ok);
  TEST_ASSERT_EQUAL(0, s.pass_len);
}

static void test_spans_reject_uint8_wrap_ssid(void) {
  // The R6 OOB read: sl=254 in a tiny payload wrapped (uint8_t)(1+254)
  // to 255→passed; the pass-length read then walked past the buffer.
  uint8_t d[4] = {254, 0, 0, 0};
  TEST_ASSERT_FALSE(improv_wifi_spans(d, sizeof(d)).ok);
  uint8_t d2[2] = {255, 0};
  TEST_ASSERT_FALSE(improv_wifi_spans(d2, sizeof(d2)).ok);
}

static void test_spans_reject_ssid_len_equal_to_payload(void) {
  // The off-by-one: 1 + sl == len means the password-length byte itself
  // sits one past the payload — must reject, the old `>` accepted it.
  const uint8_t d[] = {3, 'a', 'b', 'c'};   // len 4, sl 3 → 1+3 == 4
  TEST_ASSERT_FALSE(improv_wifi_spans(d, sizeof(d)).ok);
}

static void test_spans_reject_password_overrun(void) {
  const uint8_t d[] = {1, 'a', 5, 'x', 'y'};   // pass claims 5, has 2
  TEST_ASSERT_FALSE(improv_wifi_spans(d, sizeof(d)).ok);
}

static void test_spans_reject_empty_payload(void) {
  TEST_ASSERT_FALSE(improv_wifi_spans((const uint8_t *)"", 0).ok);
}

static void test_spans_exact_fit_password(void) {
  // 2 + sl + pl == len exactly is VALID (indices 2+sl … 2+sl+pl-1).
  const uint8_t d[] = {1, 'a', 2, 'x', 'y'};
  ImprovWifiSpans s = improv_wifi_spans(d, sizeof(d));
  TEST_ASSERT_TRUE(s.ok);
  TEST_ASSERT_EQUAL(3, s.pass_off);
  TEST_ASSERT_EQUAL(2, s.pass_len);
}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_envelope_accepts_exact_fit);
  RUN_TEST(test_envelope_rejects_short_frames);
  RUN_TEST(test_envelope_immune_to_uint8_wrap);
  RUN_TEST(test_spans_happy_path);
  RUN_TEST(test_spans_open_network_empty_password);
  RUN_TEST(test_spans_reject_uint8_wrap_ssid);
  RUN_TEST(test_spans_reject_ssid_len_equal_to_payload);
  RUN_TEST(test_spans_reject_password_overrun);
  RUN_TEST(test_spans_reject_empty_payload);
  RUN_TEST(test_spans_exact_fit_password);
  UNITY_END();
  return 0;
}
