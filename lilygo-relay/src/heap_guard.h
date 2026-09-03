#pragma once

#include <stddef.h>
#include <stdint.h>

// Heap-guard state machine for rtl_433_ESP's pulse-train path, split out
// of main.cpp so the native test env (`pio test -e native`) can pin it.
// Keep this file free of Arduino headers.
//
// Why it exists (2026-09-02, the 433 board's crash loop): rtl_433_ESP::loop()
// heap_caps_calloc()s one pulse_data_t (~9.7 KB) per received pulse train
// and memcpy()s into it WITHOUT a NULL check, then queues it (depth 5) for
// the decoder task. Our callback — a blocking HTTPS POST — runs on that
// decoder task, so while a POST is in flight the trains stack up in the
// heap. 433.92 MHz is a noisy band; three or four trains behind one slow
// POST and the next calloc returns NULL → StoreProhibited → reset → the
// same race next boot. Nothing about the toolchain changed; the trigger is
// RF traffic against TLS latency.
//
// Contract: each loop() iteration feeds the largest allocatable block and
// the clock. When the block can hold a train plus margin, drain. When it
// can't, HOLD the drain (the two receive buffers keep what they hold; the
// ISR drops anything beyond — a lost noise burst instead of a lost board).
// A transient dip clears within a second and reports Recovered. If the
// shortage persists past restartAfterMs the heap is genuinely gone (leak
// or fragmentation) and the verdict is Restart: a clean esp_restart()
// beats a panic loop. The elapsed math is uint32 subtraction, so a
// millis() wrap mid-hold does not extend or shorten the window.
class HeapGuard {
 public:
  enum class Verdict {
    Drain,      // heap fine, was fine: let the library run
    Recovered,  // heap fine after a hold: log it, then drain
    Hold,       // first low sample of an episode: log + count, skip drain
    Holding,    // still low, inside the restart window: skip drain
    Restart     // low for longer than restartAfterMs: restart cleanly
  };

  HeapGuard(size_t needBytes, uint32_t restartAfterMs)
      : _need(needBytes), _restartAfterMs(restartAfterMs) {}

  Verdict step(size_t maxAlloc, uint32_t nowMs) {
    if (maxAlloc >= _need) {
      if (!_holding) return Verdict::Drain;
      _holding = false;
      _lastHoldMs = nowMs - _lowSinceMs;
      return Verdict::Recovered;
    }
    if (!_holding) {
      _holding = true;
      _lowSinceMs = nowMs;
      _trips++;
      return Verdict::Hold;
    }
    if (nowMs - _lowSinceMs > _restartAfterMs) return Verdict::Restart;
    return Verdict::Holding;
  }

  size_t   need() const        { return _need; }
  bool     holding() const     { return _holding; }
  uint32_t trips() const       { return _trips; }
  // Duration of the most recent completed hold (valid after Recovered).
  uint32_t lastHoldMs() const  { return _lastHoldMs; }
  // millis() of the current hold's start (valid while holding()).
  uint32_t lowSinceMs() const  { return _lowSinceMs; }

 private:
  size_t   _need;
  uint32_t _restartAfterMs;
  bool     _holding = false;
  uint32_t _lowSinceMs = 0;
  uint32_t _lastHoldMs = 0;
  uint32_t _trips = 0;
};
