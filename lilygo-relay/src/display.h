#pragma once
#include <Arduino.h>

// Minimal status display for the LilyGO T3's onboard SSD1306 OLED.
// Four short lines + a header. Update any subset of fields; pass
// nullptr to leave a field unchanged. If the OLED isn't present, all
// calls become no-ops — board boots normally and POSTs continue.

namespace ZasderDisplay {

void begin();
bool isPresent();  // false if I2C probe failed; renders are no-ops
void loop();       // call from main loop() — runs the 4h invert timer

void update(const char *header,
            const char *line1,
            const char *line2,
            const char *line3,
            const char *line4);

}  // namespace ZasderDisplay
