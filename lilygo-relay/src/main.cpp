// LilyGO T3 LoRa32 V1.6.1 — receives weather-sensor packets via rtl_433_ESP
// and POSTs them to the Zasder Weather backend's /ingest/custom endpoint.
//
// Pipeline: sensor → 433/915 MHz OOK → SX1276 → rtl_433_ESP decoder →
// rtl_433_Callback() → zasder_post() → HTTPS POST → backend → SQLite.
//
// Provisioning flow:
//   1. First boot: WiFiManager comes up as WPA2 AP "ZasderLilyGO"
//      (password "zasder-setup"). User
//      joins from a phone, captive portal asks for Wi-Fi creds only
//      (NOT backend URL / token — those are configured later over the
//      LAN, which is way more forgiving than a one-shot captive form).
//   2. Once on the home Wi-Fi, the board exposes an HTTP config server
//      at http://zasder-lilygo.local/ (and at its DHCP'd IP). curl/
//      browser-POST backend_url + ingest_token there.
//   3. POSTs to the backend start immediately once both are set.
// To re-provision Wi-Fi, hit POST /reset (clears NVS + Wi-Fi creds).

#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <rtl_433_ESP.h>

#include "config_server.h"
#include "display.h"
#include "zasder_post.h"

#include "esp_system.h"          // esp_restart, esp_reset_reason
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <time.h>                // time, gmtime_r — for the nightly restart

// Per-packet JSON buffer handed to rtl_433_ESP's decoder. 512 B is the
// upstream example default and covers every weather-station packet we
// care about (the longest, Atlas multi-field, is ~280 B).
#define JSON_MSG_BUFFER 512
static char messageBuffer[JSON_MSG_BUFFER];

rtl_433_ESP rtl_433;

// millis() of the last decoded packet — fed in rtl_433_Callback, checked by
// the RX-liveness watchdog. The SX1276 receive path can wedge while loop()
// keeps running (so the loop-stall watchdog never fires and HTTP/status stays
// up), which silently kills all sensor data. 0 = no packet seen yet.
static volatile uint32_t g_lastRxMs = 0;

void rtl_433_Callback(char *message) {
  // rtl_433_ESP hands us one JSON object per decoded packet. We don't
  // try to coalesce Atlas's 8-message-type cycle on-device (limited
  // RAM, and the backend already does last-write-wins per field on
  // UPSERT). v1 behavior: post every packet, accept the small window
  // of partial observations between cycles.
  digitalWrite(LED_BUILTIN_RX, HIGH);
  g_lastRxMs = millis();          // feed the RX-liveness watchdog
  // Note the packet on the status server so /status reflects what we
  // just heard even if the POST is about to fail.
  {
    JsonDocument pkt;
    if (deserializeJson(pkt, message) == DeserializationError::Ok) {
      const char *m = pkt["model"] | "?";
      uint32_t pid  = pkt["id"]    | 0u;
      ZasderConfigServer::noteIncomingPacket(m, pid);
    }
  }
  zasder_post(message,
              ZasderConfigServer::backendUrl,
              ZasderConfigServer::ingestToken);
  digitalWrite(LED_BUILTIN_RX, LOW);
}

// ── self-healing: independent watchdog + Wi-Fi reconnect handling ──────
// The hang we kept hitting: a Wi-Fi drop (UniFi AUTH_FAIL) leaves the HTTP
// listener on a stale socket; handleClient() then wedges the whole loop()
// while the TCP/IP + mDNS tasks keep answering ping (so the board looks
// "up" but posts nothing). Two defenses:
//   1. A watchdog task pinned to core 0 (loop() runs on core 1). It resets
//      the chip if loop() stops feeding g_lastLoopMs — works even when the
//      loop is fully wedged. Deliberately NOT esp_task_wdt (its init API
//      differs across core versions); this uses only stable primitives.
//   2. A Wi-Fi event handler: kick a reconnect on disconnect, and on
//      GOT_IP flag the loop to re-bind the HTTP server + mDNS (onReconnect).
static volatile uint32_t g_lastLoopMs = 0;
static volatile bool g_wifiReconnect = false;
#define WDT_STALL_MS 60000UL     // reset if loop() stalls this long

// RX-liveness watchdog: reboot if the radio goes silent this long AFTER it
// has heard at least one packet. The Atlas (and ambient 433 traffic) arrives
// every ~10-30s, so a multi-minute silence means the SX1276 receive path has
// wedged even though loop() is still cycling. Generous default avoids false
// reboots during brief RF-quiet spells; override via build flag.
#ifndef RX_STALL_MS
#define RX_STALL_MS (5UL * 60UL * 1000UL)   // 5 minutes
#endif

// ── scheduled nightly restart ──────────────────────────────────────────
// Symptom we're chasing: after a few days of 24/7 uptime the OLED can
// desync into garbage (SSD1306 controller losing I2C sync on the shared
// power rail; possibly slow heap fragmentation too). Rather than pin down
// the exact cause, reboot once a night at a quiet local hour for a clean
// slate — this covers a memory leak OR a peripheral desync equally. A
// reboot costs ~15s of RF downtime (Wi-Fi + NTP re-sync); the Atlas
// re-posts within seconds and the backend is last-write-wins, so the gap
// is invisible in the app.
//
// Configurable via build flags. NIGHTLY_REBOOT_HOUR/MINUTE are the LOCAL
// wall-clock time; set the hour to -1 to disable. LOCAL_TZ_OFFSET_MINUTES
// shifts NTP's UTC to local time for this check only — it does NOT touch
// the UTC timestamps we POST (those stay on TZ_OFFSET_MINUTES). Arizona is
// MST (UTC-7) year-round with no DST, so -420 → 03:30 reboot = 3:30 AM AZ.
#ifndef NIGHTLY_REBOOT_HOUR
#define NIGHTLY_REBOOT_HOUR     3
#endif
#ifndef NIGHTLY_REBOOT_MINUTE
#define NIGHTLY_REBOOT_MINUTE   30
#endif
#ifndef LOCAL_TZ_OFFSET_MINUTES
#define LOCAL_TZ_OFFSET_MINUTES 0
#endif
// Ignore the reboot window during the first 2h of uptime. Uptime resets to
// 0 on reboot, so after the nightly restart the next eligible check is
// hours away — comfortably past the one-minute reboot window. Without this
// guard the board would re-trigger every 5s for the whole target minute.
#define NIGHTLY_MIN_UPTIME_MS   (2UL * 60UL * 60UL * 1000UL)

static void maybeNightlyRestart() {
#if NIGHTLY_REBOOT_HOUR >= 0
  if (millis() < NIGHTLY_MIN_UPTIME_MS) return;
  time_t nowUtc = time(nullptr);
  if (nowUtc < 1700000000) return;          // NTP not synced yet (pre-2023)
  time_t localEpoch = nowUtc + (time_t) LOCAL_TZ_OFFSET_MINUTES * 60;
  struct tm tmv;
  gmtime_r(&localEpoch, &tmv);              // offset epoch read as UTC = local wall clock
  if (tmv.tm_hour == NIGHTLY_REBOOT_HOUR && tmv.tm_min == NIGHTLY_REBOOT_MINUTE) {
    Serial.printf("[nightly] %02d:%02d local — scheduled restart\n",
                  tmv.tm_hour, tmv.tm_min);
    Serial.flush();
    esp_restart();
  }
#endif
}

static void watchdogTask(void *) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(5000));
    uint32_t last = g_lastLoopMs;
    if (last != 0 && (millis() - last) > WDT_STALL_MS) {
      Serial.println("[watchdog] loop() stalled >60s — restarting");
      Serial.flush();
      esp_restart();
    }
    // RX-liveness: catch a wedged SX1276 that the loop-stall check above
    // can't see (loop keeps running, HTTP/status stays up, but no packets).
    uint32_t lastRx = g_lastRxMs;
    if (lastRx != 0 && (millis() - lastRx) > RX_STALL_MS) {
      Serial.printf("[watchdog] no RX for >%lus — receiver wedged, restarting\n",
                    (unsigned long) (RX_STALL_MS / 1000));
      Serial.flush();
      esp_restart();
    }
    // Runs on core 0 alongside the stall check, so the nightly reboot fires
    // even if loop() (core 1) is wedged.
    maybeNightlyRestart();
  }
}

static void onWifiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  (void) info;
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    Serial.println("[wifi] STA disconnected — reconnecting");
    WiFi.reconnect();
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    Serial.printf("[wifi] got IP %s — re-binding services\n",
                  WiFi.localIP().toString().c_str());
    g_wifiReconnect = true;      // loop() calls onReconnect() in its own context
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("Zasder LilyGO relay starting");
  Serial.printf("  freq=%.2f MHz  source=%s\n",
                (double) RF_MODULE_FREQUENCY, ZASDER_SOURCE_TAG);
  // Why did we (re)boot? After the watchdog lands, this line tells us
  // whether the last boot was a power-on, a panic, or a watchdog reset.
  Serial.printf("  reset_reason=%d\n", (int) esp_reset_reason());
  // Register the Wi-Fi event handler before connecting so reconnects are
  // handled from first boot.
  WiFi.onEvent(onWifiEvent);

  pinMode(LED_BUILTIN_RX, OUTPUT);

  ZasderDisplay::begin();
  {
    char hdr[24];
    snprintf(hdr, sizeof(hdr), "Zasder %.0fMHz",
             (double) RF_MODULE_FREQUENCY);
    ZasderDisplay::update(hdr, ZASDER_SOURCE_TAG,
                          "WiFi: connecting", "", "");
  }

  // WiFiManager now handles ONLY Wi-Fi creds. Backend URL + ingest
  // token are deferred to the LAN-side HTTP config server below —
  // editing fields on the captive portal turned out to be unreliable
  // (Save button doesn't always persist params).
  WiFiManager wm;
  wm.setConfigPortalTimeout(300);
  // WPA2 password on the setup AP: without it the portal is an OPEN
  // network, and the home Wi-Fi credentials the user types into it
  // transit in cleartext — sniffable by anyone in RF range. A fixed,
  // README-documented password defeats passive capture (an evil-twin AP
  // remains inherent to the captive-portal pattern; documented in README).
  Serial.println("Wi-Fi: trying saved creds, AP=ZasderLilyGO (pw zasder-setup) if none");
  if (!wm.autoConnect("ZasderLilyGO", "zasder-setup")) {
    Serial.println("WiFi setup timed out — restarting");
    delay(500);
    ESP.restart();
  }
  Serial.printf("Wi-Fi OK  IP=%s\n", WiFi.localIP().toString().c_str());
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);

  // Start NTP. Without this, time() returns 0 and every POST goes in
  // with timestamp 1970-01-01 — the backend stores it as "56y ago".
  // We use UTC (offset 0) because the backend's /ingest/custom expects
  // ISO timestamps in UTC. Two SNTP servers for redundancy. Sync runs
  // asynchronously; first packets after boot may still POST before the
  // clock catches up — zasder_post() drops those (skipPost path).
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  Serial.println("NTP sync started (UTC)");
  {
    char ipLine[32];
    snprintf(ipLine, sizeof(ipLine), "IP: %s",
             WiFi.localIP().toString().c_str());
    ZasderDisplay::update(nullptr, nullptr, ipLine,
                          "config: /provision", "");
  }

  // Load backend creds from NVS (may be empty; POSTs just skip until
  // someone provisions them via /provision over HTTP).
  ZasderConfigServer::loadFromNvs();
  ZasderConfigServer::begin();
  Serial.printf("provision with: curl -X POST "
                "http://%s/provision -d 'backend_url=...&ingest_token=...'\n",
                WiFi.localIP().toString().c_str());

  rtl_433.initReceiver(RF_MODULE_RECEIVER_GPIO, RF_MODULE_FREQUENCY);
  rtl_433.setCallback(rtl_433_Callback, messageBuffer, JSON_MSG_BUFFER);
  rtl_433.enableReceiver();
  Serial.printf("ready — receiver up, has_token=%d has_url=%d\n",
                (int) (ZasderConfigServer::ingestToken.length() > 0),
                (int) (ZasderConfigServer::backendUrl.length()  > 0));

  // Start the independent watchdog last, once everything is up. Pinned to
  // core 0 so it keeps running even if the Arduino loop (core 1) wedges.
  xTaskCreatePinnedToCore(watchdogTask, "wdt", 2560, nullptr, 1, nullptr, 0);
  Serial.println("watchdog task started (resets if loop() stalls >60s)");
}

void loop() {
  g_lastLoopMs = millis();             // feed the watchdog
  if (g_wifiReconnect) {               // re-bind HTTP server/mDNS after a reconnect
    g_wifiReconnect = false;
    ZasderConfigServer::onReconnect();
  }
  rtl_433.loop();
  ZasderConfigServer::loop();
  ZasderDisplay::loop();
}
