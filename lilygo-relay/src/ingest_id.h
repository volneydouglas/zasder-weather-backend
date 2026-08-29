#pragma once
#include <stdint.h>
#include <stdio.h>

// Synthetic device-id ("mac") construction, split out of zasder_post.cpp
// so the native test env (`pio test -e native`) can pin it without the
// Arduino framework — same rule as auth_logic.h: no Arduino headers, no
// `String`. This 12-hex-char id is the PRIMARY KEY the backend stores
// every observation row under. The "5D5D" prefix marks synthetic ids
// apart from real station macs, then one type-tag byte and the low 24
// bits of the rtl_433 sensor id. Any change here — prefix, byte order,
// width, casing — silently re-keys every device row on every backend
// this firmware posts to, orphaning the station's entire history. That
// is why the exact layout is pinned by test/test_identity.
namespace ZasderId {

// Writes exactly 12 uppercase hex chars + NUL into out[13].
inline void synthMac(uint8_t typeTag, uint32_t id, char out[13]) {
  snprintf(out, 13, "5D5D%02X%02X%02X%02X",
           typeTag,
           (unsigned) ((id >> 16) & 0xFF),
           (unsigned) ((id >>  8) & 0xFF),
           (unsigned) ( id        & 0xFF));
}

}  // namespace ZasderId
