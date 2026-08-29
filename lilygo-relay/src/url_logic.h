#pragma once
#include <ctype.h>
#include <stddef.h>
#include <string.h>

// backend_url validation, split out of config_server.cpp so the native
// test env can pin it (Arduino-free, like auth_logic.h). The poster only
// fails hours after provisioning when this lets a bad URL through — the
// operator has long stopped watching — so every rejected shape here is a
// deferred-failure bug this validation exists to prevent: missing scheme,
// host-less "https:///path" / "https://?q" / "https://#f", bare ":port",
// and hosts with spaces or control chars (the R6 finding). The input is
// expected pre-trimmed with trailing slashes already stripped (the
// caller's normalization); this decides, it does not rewrite.
namespace ZasderUrl {

// True when url is a full scheme://host... URL with a plausible host.
// allowHttp mirrors the TLS_INSECURE dev build; production accepts only
// https:// because the poster always connects through WiFiClientSecure.
inline bool validBackendUrl(const char *url, bool allowHttp) {
  if (!url) return false;
  const char *rest = nullptr;
  if (strncmp(url, "https://", 8) == 0) {
    rest = url + 8;
  } else if (allowHttp && strncmp(url, "http://", 7) == 0) {
    rest = url + 7;
  }
  if (!rest) return false;
  // The authority ends at the first '/', '?' or '#'.
  size_t end = strcspn(rest, "/?#");
  // Skip any userinfo@ prefix; what's left must start with a host, not a
  // bare :port.
  size_t start = 0;
  for (size_t i = 0; i < end; i++) {
    if (rest[i] == '@') start = i + 1;
  }
  if (start >= end) return false;      // empty host (or authority was "@")
  if (rest[start] == ':') return false;
  // Split host from an optional :port. Bracketed IPv6 literals own their
  // colons, so the port separator is only the colon AFTER ']' (or the
  // first colon for a plain host). "https://[" and "https://host:abc"
  // both persisted fine and failed hours later at POST time (CodeRabbit,
  // PR #33) — the deferred-failure shape this header exists to reject.
  size_t host_end = end;
  if (rest[start] == '[') {
    size_t close = start + 1;
    while (close < end && rest[close] != ']') {
      char c = rest[close];
      if (!(isxdigit((unsigned char) c) || c == ':' || c == '.')) {
        return false;                  // junk inside the brackets
      }
      close++;
    }
    if (close >= end || close == start + 1) return false;  // no ']' / "[]"
    host_end = close + 1;
  } else {
    for (size_t i = start; i < end; i++) {
      if (rest[i] == ':') { host_end = i; break; }
      char c = rest[i];
      if (!(isalnum((unsigned char) c) || c == '.' || c == '-')) {
        return false;
      }
    }
  }
  // Optional port: ':' then 1-5 digits, 1..65535.
  if (host_end < end) {
    if (rest[host_end] != ':') return false;   // ']' followed by junk
    size_t p = host_end + 1;
    if (p >= end) return false;                // trailing bare ':'
    unsigned long port = 0;
    for (; p < end; p++) {
      if (!isdigit((unsigned char) rest[p])) return false;
      port = port * 10 + (unsigned long) (rest[p] - '0');
      if (port > 65535) return false;
    }
    if (port == 0) return false;
  }
  return true;
}

}  // namespace ZasderUrl
