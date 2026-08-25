// Pure bounds/validation logic for the Improv serial parser — extracted
// from improv_serial.cpp (R6) so the native test env can pin it without
// Arduino. This is the code that faces raw bytes off the USB UART, and the
// extraction exists because it already shipped two memory-safety bugs the
// on-device path couldn't test for:
//
//   * every length check did its arithmetic in uint8_t, so dlen=254 wrapped
//     (uint8_t)(2+254) to 0, the check always passed, and the reads walked
//     off the end of the frame buffer (a real OOB read, reachable from any
//     crafted frame — or sufficiently unlucky line noise);
//   * the ssid-length check used > where >= was needed: when 1+sl == len
//     the password-length byte read at d[1+sl] was one past the payload.
//
// Rule of the file: ALL arithmetic in size_t, no reads implied that the
// returned spans don't cover. The Arduino side must index the payload ONLY
// through an ImprovWifiSpans that returned ok=true.
//
// Same pattern as auth_logic.h / post_fsm.h: header-only, no Arduino
// includes, shared verbatim between the firmware TU and the native tests.
#pragma once

#include <stddef.h>
#include <stdint.h>

// RPC envelope: data = [cmd, dlen, payload…]; `len` is the frame's declared
// data length. True when the declared payload fits inside the frame.
static inline bool improv_rpc_envelope_ok(uint8_t dlen, size_t len) {
  return len >= 2 && (size_t)2 + (size_t)dlen <= len;
}

// WIFI_SETTINGS payload: d = [ssid_len, ssid…, pass_len, pass…], `len`
// bytes total. Spans are offsets INTO d; valid only when ok is true.
typedef struct {
  bool ok;
  size_t ssid_off, ssid_len;
  size_t pass_off, pass_len;
} ImprovWifiSpans;

static inline ImprovWifiSpans improv_wifi_spans(const uint8_t *d, size_t len) {
  ImprovWifiSpans s = {false, 0, 0, 0, 0};
  if (len < 1) return s;
  size_t sl = d[0];
  // >= not >: d[1 + sl] (the password-length byte) is read next, so it
  // must itself sit inside len — the R6 off-by-one.
  if (1 + sl >= len) return s;
  size_t pl = d[1 + sl];
  if (2 + sl + pl > len) return s;
  s.ok = true;
  s.ssid_off = 1;        s.ssid_len = sl;
  s.pass_off = 2 + sl;   s.pass_len = pl;
  return s;
}
