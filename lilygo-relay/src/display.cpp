#include "display.h"

#include <Wire.h>
#include <U8g2lib.h>

// LilyGO T3 V1.6.1 OLED is a soldered SSD1306 128x64 on I2C pins
// 21 (SDA) / 22 (SCL). The `ttgo-lora32-v21new` board variant declares
// OLED_RST=16 — that's correct for V2.1 but on V1.6.1 there is NO
// dedicated reset line wired to the ESP32; the OLED reset is tied to
// the board's power-up reset. LilyGO's own SSD1306SimpleDemo defines
// OLED_RST as UNUSED_PIN for V1.6.1. Driving GPIO16 LOW on V1.6.1
// wedges either the I2C bus or a peripheral on the power rail, and
// Wire.endTransmission() never returns — the TG1 watchdog catches it
// ~5s in. So: pass U8X8_PIN_NONE for reset and never touch GPIO16.
static U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(
    U8G2_R0, /*reset=*/U8X8_PIN_NONE);

static bool _displayPresent = false;

// Burn-in mitigation: SSD1306 OLEDs degrade with 24/7 use, mostly as a
// function of brightness × time-pixel-lit. Two cheap defenses:
//   1. Run at ~30% contrast — biggest single-factor lifetime win.
//   2. Flip the display polarity (normal ↔ inverted) every 4h so the
//      pixels that were "lit white" become "lit dark" and vice versa,
//      evening out the wear across the panel.
static constexpr uint8_t        OLED_CONTRAST       = 80;
static constexpr unsigned long  INVERT_INTERVAL_MS  = 4UL * 60UL * 60UL * 1000UL;
static unsigned long            _lastInvertMs       = 0;
static bool                     _inverted           = false;

// Anti-corruption: the SSD1306 controller can drift out of sync after days
// of 24/7 use (noise/ESD on the rail shared with the SX1276), then render
// garbage even though the data path is fine. Periodically re-send the full
// init sequence to recover the panel — far cheaper than rebooting and it
// drops no RF. The nightly restart in main.cpp is the heavier backstop.
static constexpr unsigned long  REINIT_INTERVAL_MS  = 6UL * 60UL * 60UL * 1000UL;
static unsigned long            _lastReinitMs       = 0;

static String _header;
static String _line1;
static String _line2;
static String _line3;
static String _line4;

static void render() {
  if (!_displayPresent) return;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(0, 8, _header.c_str());
  u8g2.drawHLine(0, 11, 128);
  u8g2.drawStr(0, 23, _line1.c_str());
  u8g2.drawStr(0, 35, _line2.c_str());
  u8g2.drawStr(0, 47, _line3.c_str());
  u8g2.drawStr(0, 59, _line4.c_str());
  u8g2.sendBuffer();
}

namespace ZasderDisplay {

void begin() {
  // Let the board settle for a beat after Serial.begin() so any
  // power-rail glitches from the SX1276 settling don't coincide with
  // I2C init.
  delay(100);

  // Default-frequency Wire.begin() (100 kHz) per LilyGO's own example.
  // Bump to 400 kHz only after a successful probe.
  Wire.begin(OLED_SDA, OLED_SCL);
  Wire.setTimeOut(50);  // ms — converts a hung endTransmission into
                        // a return code 5 instead of WDT.

  // Probe for the SSD1306. The standard address is 0x3C; some clones
  // use 0x3D. Try both before giving up.
  for (uint8_t addr : {0x3C, 0x3D}) {
    Wire.beginTransmission(addr);
    uint8_t rc = Wire.endTransmission();
    if (rc == 0) {
      Serial.printf("OLED detected at 0x%02X — initializing U8g2\n", addr);
      u8g2.setI2CAddress(addr << 1);
      u8g2.begin();
      Wire.setClock(400000);
      u8g2.setContrast(OLED_CONTRAST);
      _lastInvertMs = millis();
      _lastReinitMs = millis();
      _displayPresent = true;
      _header = "Zasder LilyGO";
      _line1  = "booting...";
      _line2  = "";
      _line3  = "";
      _line4  = "";
      render();
      return;
    }
    Serial.printf("OLED not at 0x%02X (rc=%u)\n", addr, (unsigned) rc);
  }
  Serial.println("OLED not detected — display disabled, board continues");
  _displayPresent = false;
}

bool isPresent() { return _displayPresent; }

void loop() {
  if (!_displayPresent) return;
  unsigned long now = millis();
  // millis() wraps every ~49 days — using unsigned subtraction means
  // the comparison stays correct across wrap.
  if (now - _lastInvertMs >= INVERT_INTERVAL_MS) {
    _inverted = !_inverted;
    // SSD1306 raw commands: 0xA6 = normal, 0xA7 = inverted.
    u8g2.sendF("c", _inverted ? 0xA7 : 0xA6);
    _lastInvertMs = now;
  }
  if (now - _lastReinitMs >= REINIT_INTERVAL_MS) {
    // Re-send the SSD1306 init sequence to clear any controller desync,
    // then restore contrast + the current invert state and repaint. Wire's
    // 50 ms timeout (set in begin) keeps a stuck bus from wedging the loop.
    u8g2.begin();
    u8g2.setContrast(OLED_CONTRAST);
    u8g2.sendF("c", _inverted ? 0xA7 : 0xA6);
    render();
    _lastReinitMs = now;
  }
}

void update(const char *header,
            const char *line1,
            const char *line2,
            const char *line3,
            const char *line4) {
  if (header) _header = header;
  if (line1)  _line1  = line1;
  if (line2)  _line2  = line2;
  if (line3)  _line3  = line3;
  if (line4)  _line4  = line4;
  render();
}

}  // namespace ZasderDisplay
