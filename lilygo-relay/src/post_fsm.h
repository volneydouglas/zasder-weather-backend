#pragma once

// 401-wipe counter state machine, split out of zasder_post.cpp so the
// native test env (`pio test -e native`) can pin its semantics. Keep this
// file free of Arduino headers.
//
// Contract: N *consecutive* 401s mean the token itself is wrong → wipe it.
// ANY other outcome — 2xx, 5xx, or a transport failure (rc < 0, e.g. a
// Wi-Fi flap or TLS timeout) — breaks the streak. If the counter reset
// only on 2xx it would count cumulative 401s, not consecutive ones: five
// 401s spread over weeks, separated by the flaps and timeouts this
// firmware already expects, would eventually wipe a perfectly good token
// and take the relay offline until someone physically re-paired it with
// the setup key. Erring toward NOT wiping is deliberate — a stale token
// costs 401s until it's noticed; a wrongly wiped one costs a trip to the
// board.
class Post401Tracker {
 public:
  explicit Post401Tracker(int threshold) : _threshold(threshold) {}

  // Feed one POST result (HTTP status code, or a negative HTTPClient
  // transport error). Returns true exactly when the wipe threshold is
  // reached; the streak also resets then, so a second wipe needs a full
  // fresh run of consecutive 401s (matters after the operator re-pairs
  // with another bad token).
  bool onResult(int rc) {
    if (rc == 401) {
      _consecutive++;
      if (_consecutive >= _threshold) {
        _consecutive = 0;
        return true;
      }
    } else {
      _consecutive = 0;
    }
    return false;
  }

  int consecutive() const { return _consecutive; }

 private:
  int _threshold;
  int _consecutive = 0;
};
