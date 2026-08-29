// Host-side tests (`pio test -e native`) for the two pieces of pure
// logic extracted in the 1.9 test-debt pass (TEST_GAP_AUDIT Tier 1):
//
//   * ZasderId::synthMac — the synthetic device id every observation row
//     is keyed under on the backend. The exact layout ("5D5D" prefix,
//     type tag, low 24 bits of the sensor id, uppercase hex) is load-
//     bearing: a regression here silently re-keys every device row and
//     orphans the station's history.
//   * ZasderUrl::validBackendUrl — the backend_url gate in
//     config_server.cpp. Every accepted-but-broken shape fails hours
//     later at POST time, after the operator stopped watching.

#include <unity.h>

#include "ingest_id.h"
#include "url_logic.h"

void setUp() {}
void tearDown() {}

// ── synthMac layout pins ──────────────────────────────────────────────

static void test_synth_mac_exact_layout() {
  char buf[13];
  ZasderId::synthMac(0x28, 0x0000B3, buf);
  TEST_ASSERT_EQUAL_STRING("5D5D280000B3", buf);
}

static void test_synth_mac_full_24_bit_id() {
  char buf[13];
  ZasderId::synthMac(0x01, 0xAABBCC, buf);
  TEST_ASSERT_EQUAL_STRING("5D5D01AABBCC", buf);
}

static void test_synth_mac_truncates_to_low_24_bits() {
  // rtl_433 ids can exceed 24 bits; only the low 3 bytes participate.
  // The high byte must be DROPPED, not shifted — both ids below must
  // collide on purpose (the historical behavior rows are keyed under).
  char a[13], b[13];
  ZasderId::synthMac(0x28, 0xFFAABBCC, a);
  ZasderId::synthMac(0x28, 0x00AABBCC, b);
  TEST_ASSERT_EQUAL_STRING("5D5D28AABBCC", a);
  TEST_ASSERT_EQUAL_STRING(a, b);
}

static void test_synth_mac_zero_pads_and_uppercases() {
  char buf[13];
  ZasderId::synthMac(0x0A, 0x00000F, buf);
  TEST_ASSERT_EQUAL_STRING("5D5D0A00000F", buf);
  TEST_ASSERT_EQUAL_INT(12, (int) strlen(buf));
}

static void test_synth_mac_type_tag_distinguishes_sensors() {
  // Same rtl_433 id decoded by two different sensor models must land on
  // two different device rows — the type tag is what keeps them apart.
  char a[13], b[13];
  ZasderId::synthMac(0x28, 711, a);
  ZasderId::synthMac(0x30, 711, b);
  TEST_ASSERT_TRUE(strcmp(a, b) != 0);
}

// ── backend_url validation ────────────────────────────────────────────

static void test_url_accepts_normal_https() {
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://zasder.fly.dev", false));
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://host.example.com:8443", false));
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://host/path/kept", false));
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://user@host.example.com", false));
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://[2001:db8::1]:443", false));
}

static void test_url_http_only_in_insecure_build() {
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("http://host", false));
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("http://host", true));
  // The dev build still validates the host — insecure ≠ anything-goes.
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("http:///path", true));
}

static void test_url_rejects_hostless_shapes() {
  // Each of these persisted fine and failed hours later at POST time.
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https:///path", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://?q=1", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://#frag", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://:8080", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://@", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://user@", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://user@:443", false));
}

static void test_url_rejects_missing_or_wrong_scheme() {
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("zasder.fly.dev", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("ftp://host", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl(nullptr, false));
}

static void test_url_rejects_malformed_ipv6_and_ports() {
  // CodeRabbit PR #33: these passed the flat charset check and only
  // failed hours later at POST time.
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://[", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://[]", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://[::1", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://[zz::1]", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://[::1]junk", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://host:abc", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://host:", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://host:0", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://host:70000", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://host:80:80", false));
  // ...while the real shapes keep passing.
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://host:65535", false));
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://[::1]", false));
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://[2001:db8::1]:8443/x", false));
}

static void test_url_rejects_bad_host_charset() {
  // The R6 finding: a space or control char in the host passed
  // provisioning and only died at POST time.
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://ho st", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://host\x07", false));
  TEST_ASSERT_FALSE(ZasderUrl::validBackendUrl("https://h_ost", false));
  // …but the delimiter chars after the authority stay legal.
  TEST_ASSERT_TRUE(ZasderUrl::validBackendUrl("https://host/pa th ok", false));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_synth_mac_exact_layout);
  RUN_TEST(test_synth_mac_full_24_bit_id);
  RUN_TEST(test_synth_mac_truncates_to_low_24_bits);
  RUN_TEST(test_synth_mac_zero_pads_and_uppercases);
  RUN_TEST(test_synth_mac_type_tag_distinguishes_sensors);
  RUN_TEST(test_url_accepts_normal_https);
  RUN_TEST(test_url_http_only_in_insecure_build);
  RUN_TEST(test_url_rejects_hostless_shapes);
  RUN_TEST(test_url_rejects_malformed_ipv6_and_ports);
  RUN_TEST(test_url_rejects_missing_or_wrong_scheme);
  RUN_TEST(test_url_rejects_bad_host_charset);
  return UNITY_END();
}
