// Host-side tests (`pio test -e native`) for the two security-critical
// pieces of pure logic in this firmware:
//
//   * ZasderAuth::authMatches — the credential-matrix behind checkAuth()
//     in config_server.cpp (first-provision setup-key requirement, token
//     OR key acceptance, post-wipe re-pair, empty-candidate rejection).
//   * Post401Tracker — the 401-wipe counter behind zasder_post.cpp
//     (consecutive-not-cumulative counting; transport errors and 5xx
//     break the streak).
//
// The HTTP-channel plumbing (Authorization vs X-Setup-Key vs form fields,
// checked independently so a stale Bearer header can't mask a good
// setup_key) lives in checkAuth() itself and needs the Arduino WebServer;
// it is documented there and exercised on-device.

#include <unity.h>

#include "auth_logic.h"
#include "post_fsm.h"

void setUp() {}
void tearDown() {}

// ── authMatches: first-provision matrix ───────────────────────────────
// Fresh board: no ingest token yet, setup key minted at boot.

static void test_fresh_board_denies_anonymous() {
  // No credentials at all → denied. This is the R2-01 class of bug: an
  // unprovisioned board must never accept an anonymous caller.
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("", "", "ABCD2345"));
  TEST_ASSERT_FALSE(ZasderAuth::authMatches(nullptr, "", "ABCD2345"));
}

static void test_fresh_board_accepts_setup_key() {
  TEST_ASSERT_TRUE(ZasderAuth::authMatches("ABCD2345", "", "ABCD2345"));
}

static void test_fresh_board_denies_wrong_key() {
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("WRONGKEY", "", "ABCD2345"));
  // Prefix / truncation must not match either.
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("ABCD234", "", "ABCD2345"));
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("ABCD23456", "", "ABCD2345"));
}

// ── authMatches: provisioned board (token AND key both valid) ─────────

static void test_provisioned_accepts_token_or_key() {
  TEST_ASSERT_TRUE(ZasderAuth::authMatches("tok-secret", "tok-secret", "ABCD2345"));
  TEST_ASSERT_TRUE(ZasderAuth::authMatches("ABCD2345",  "tok-secret", "ABCD2345"));
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("neither",  "tok-secret", "ABCD2345"));
}

// ── authMatches: post-wipe re-pair ────────────────────────────────────
// 401 auto-recovery wiped the token; the setup key survives in NVS.

static void test_post_wipe_denies_old_token_allows_key() {
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("old-token", "", "ABCD2345"));
  TEST_ASSERT_TRUE(ZasderAuth::authMatches("ABCD2345",  "", "ABCD2345"));
}

// ── authMatches: empty candidate can never match ──────────────────────
// "" == "" would otherwise be an anonymous pass on a board whose token
// (or, hypothetically, key) is empty.

static void test_empty_candidate_never_matches() {
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("", "", ""));
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("", "tok", "key"));
  // Empty stored credentials must not match a non-empty candidate either.
  TEST_ASSERT_FALSE(ZasderAuth::authMatches("x", "", ""));
}

// ── secureEquals basics ───────────────────────────────────────────────

static void test_secure_equals() {
  TEST_ASSERT_TRUE(ZasderAuth::secureEquals("abc", 3, "abc", 3));
  TEST_ASSERT_FALSE(ZasderAuth::secureEquals("abc", 3, "abd", 3));
  TEST_ASSERT_FALSE(ZasderAuth::secureEquals("abc", 3, "ab", 2));
  TEST_ASSERT_TRUE(ZasderAuth::secureEquals("", 0, "", 0));
}

// ── Post401Tracker: wipe fires on the 5th consecutive 401 ─────────────

static void test_five_consecutive_401s_wipe() {
  Post401Tracker t(5);
  for (int i = 0; i < 4; i++) TEST_ASSERT_FALSE(t.onResult(401));
  TEST_ASSERT_TRUE(t.onResult(401));
  // Streak resets after the wipe: the next 401 run starts from zero.
  TEST_ASSERT_EQUAL_INT(0, t.consecutive());
  for (int i = 0; i < 4; i++) TEST_ASSERT_FALSE(t.onResult(401));
  TEST_ASSERT_TRUE(t.onResult(401));
}

// ── Post401Tracker: transport error breaks the streak ────────────────
// This is the exact regression the firmware CR-28 fix addressed: the
// counter must be consecutive, not cumulative across Wi-Fi flaps.

static void test_transport_error_breaks_streak() {
  Post401Tracker t(5);
  TEST_ASSERT_FALSE(t.onResult(401));
  TEST_ASSERT_FALSE(t.onResult(-1));   // HTTPClient transport failure
  for (int i = 0; i < 4; i++) TEST_ASSERT_FALSE(t.onResult(401));
  TEST_ASSERT_EQUAL_INT(4, t.consecutive());  // 4, not 5 — no wipe
}

static void test_success_breaks_streak() {
  Post401Tracker t(5);
  for (int i = 0; i < 4; i++) TEST_ASSERT_FALSE(t.onResult(401));
  TEST_ASSERT_FALSE(t.onResult(200));
  for (int i = 0; i < 4; i++) TEST_ASSERT_FALSE(t.onResult(401));
  TEST_ASSERT_EQUAL_INT(4, t.consecutive());
}

static void test_5xx_breaks_streak() {
  Post401Tracker t(5);
  for (int i = 0; i < 4; i++) TEST_ASSERT_FALSE(t.onResult(401));
  TEST_ASSERT_FALSE(t.onResult(503));
  TEST_ASSERT_EQUAL_INT(0, t.consecutive());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_fresh_board_denies_anonymous);
  RUN_TEST(test_fresh_board_accepts_setup_key);
  RUN_TEST(test_fresh_board_denies_wrong_key);
  RUN_TEST(test_provisioned_accepts_token_or_key);
  RUN_TEST(test_post_wipe_denies_old_token_allows_key);
  RUN_TEST(test_empty_candidate_never_matches);
  RUN_TEST(test_secure_equals);
  RUN_TEST(test_five_consecutive_401s_wipe);
  RUN_TEST(test_transport_error_breaks_streak);
  RUN_TEST(test_success_breaks_streak);
  RUN_TEST(test_5xx_breaks_streak);
  return UNITY_END();
}
