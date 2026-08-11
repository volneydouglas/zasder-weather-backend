#pragma once
#include <stddef.h>
#include <string.h>

// Pure credential-matching logic, split out of config_server.cpp so the
// native test env (`pio test -e native`) can compile and pin it without
// the Arduino framework. Keep this file free of Arduino headers and
// `String` — that is the entire point of the split. The HTTP-channel
// plumbing (which header/form field a credential arrives in) stays in
// config_server.cpp's checkAuth(); the security-critical "does this
// candidate prove ownership?" decision lives here.
namespace ZasderAuth {

// Constant-time compare. Avoids leaking the token prefix via timing to a
// LAN attacker spraying guesses. Length inequality still returns early —
// the length itself is not treated as secret, matching the previous
// String-based implementation.
inline bool secureEquals(const char *a, size_t alen,
                         const char *b, size_t blen) {
  if (alen != blen) return false;
  unsigned char diff = 0;
  for (size_t i = 0; i < alen; i++) {
    diff |= (unsigned char) a[i] ^ (unsigned char) b[i];
  }
  return diff == 0;
}

// True if `candidate` proves ownership of the board: it equals the current
// ingest token OR the per-device setup key. Empty/absent credentials never
// match — an empty candidate must not authenticate against an empty stored
// token (a fresh board has no token yet, and "" == "" would otherwise be
// an anonymous pass on exactly the board the setup-key requirement is
// meant to protect).
inline bool authMatches(const char *candidate,
                        const char *token,
                        const char *setupKey) {
  size_t clen = candidate ? strlen(candidate) : 0;
  if (clen == 0) return false;
  size_t tlen = token    ? strlen(token)    : 0;
  size_t klen = setupKey ? strlen(setupKey) : 0;
  bool tokenOk = tlen > 0 && secureEquals(candidate, clen, token, tlen);
  bool keyOk   = klen > 0 && secureEquals(candidate, clen, setupKey, klen);
  return tokenOk || keyOk;
}

}  // namespace ZasderAuth
