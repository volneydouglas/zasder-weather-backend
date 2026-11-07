// The last few distinct sensors the radio decoded, newest first, for
// /status. `last_packet` alone shows whatever spoke last, which on a 433
// board is nearly always the Atlas (every ~10 s), so a second sensor's id
// was effectively unreadable; pairing a tower needs exactly that id.
//
// Header-only and Arduino-free (native tests). Fixed storage: no heap
// churn on a board that already guards its largest free block.
#pragma once

#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace HeardList {

constexpr int SLOTS = 8;
constexpr size_t LABEL_LEN = 48;

struct List {
  char label[SLOTS][LABEL_LEN] = {};
  unsigned long ms[SLOTS] = {};
  int count = 0;

  // Records `model #id` heard at nowMs. A sensor already listed moves to
  // the front with its new time; a new one goes in front and the oldest
  // drops off once all slots are used.
  void note(const char *model, uint32_t id, unsigned long nowMs) {
    char buf[LABEL_LEN];
    snprintf(buf, sizeof(buf), "%.32s #%lu", model ? model : "?",
             (unsigned long) id);
    int at = -1;
    for (int i = 0; i < count; i++) {
      if (strcmp(label[i], buf) == 0) { at = i; break; }
    }
    if (at < 0) at = count < SLOTS ? count++ : SLOTS - 1;
    for (int i = at; i > 0; i--) {
      memcpy(label[i], label[i - 1], LABEL_LEN);
      ms[i] = ms[i - 1];
    }
    memcpy(label[0], buf, LABEL_LEN);
    ms[0] = nowMs;
  }
};

}  // namespace HeardList
