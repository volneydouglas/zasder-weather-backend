// LilyGO T3 LoRa32 V1.6.1 — receives weather-sensor packets via rtl_433_ESP
// and POSTs them to the Zasder Weather backend's /ingest/custom endpoint.
//
// Pipeline: sensor → 433/915 MHz OOK → SX1276 → rtl_433_ESP decoder →
// rtl_433_Callback() → zasder_post() → HTTPS POST → backend → SQLite.
//
// First-boot provisioning is via WiFiManager: the board comes up as an
// access point named "ZasderLilyGO". Joining it opens a captive portal
// where the user enters their Wi-Fi creds + BACKEND_URL + INGEST_TOKEN.
// Saved to NVS so subsequent boots connect directly. To re-provision
// (e.g. changed Wi-Fi password), hold BOOT for 5s while plugging in
// power — the AP comes back up.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <Preferences.h>
#include <rtl_433_ESP.h>

#include "zasder_post.h"

rtl_433_ESP rtl_433(RECEIVER_GPIO);
Preferences prefs;

// Provisioned values, kept in NVS under namespace "zasder".
String backendUrl;
String ingestToken;

// Captive-portal custom fields. Exposed in the WiFiManager web UI
// alongside the standard Wi-Fi SSID/password pickers so the user can
// enter everything at once.
WiFiManagerParameter pBackendUrl("backend_url", "Backend URL",
                                 "https://weather.example.com", 96);
WiFiManagerParameter pIngestToken("ingest_token", "Ingest token", "", 96);

static volatile bool saveRequested = false;
static void onSave() { saveRequested = true; }

void rtl_433_Callback(char *message) {
  // rtl_433_ESP hands us one JSON object per decoded packet. We don't
  // try to coalesce Atlas's 8-message-type cycle on-device (limited
  // RAM, and the backend already does last-write-wins per field on
  // UPSERT). v1 behavior: post every packet, accept the small window
  // of partial observations between cycles.
  digitalWrite(LED_BUILTIN_RX, HIGH);
  zasder_post(message, backendUrl, ingestToken);
  digitalWrite(LED_BUILTIN_RX, LOW);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("Zasder LilyGO relay starting");
  Serial.printf("  freq=%.2f MHz  source=%s\n",
                (double) RF_MODULE_FREQUENCY, ZASDER_SOURCE_TAG);

  pinMode(LED_BUILTIN_RX, OUTPUT);

  prefs.begin("zasder", /*readOnly=*/false);
  backendUrl  = prefs.getString("backend_url",  "");
  ingestToken = prefs.getString("ingest_token", "");

  pBackendUrl.setValue(backendUrl.c_str(), 96);
  pIngestToken.setValue(ingestToken.c_str(), 96);

  WiFiManager wm;
  wm.addParameter(&pBackendUrl);
  wm.addParameter(&pIngestToken);
  wm.setSaveConfigCallback(onSave);
  // 5 min portal timeout — if no one connects, reboot and retry. Keeps
  // a misconfigured board from sitting in AP mode forever after a
  // power blip.
  wm.setConfigPortalTimeout(300);

  if (!wm.autoConnect("ZasderLilyGO")) {
    Serial.println("WiFiManager timed out — restarting");
    ESP.restart();
  }

  if (saveRequested) {
    backendUrl  = pBackendUrl.getValue();
    ingestToken = pIngestToken.getValue();
    prefs.putString("backend_url",  backendUrl);
    prefs.putString("ingest_token", ingestToken);
    Serial.println("Saved provisioning to NVS");
  }

  Serial.printf("Wi-Fi OK  IP=%s\n", WiFi.localIP().toString().c_str());
  if (backendUrl.isEmpty() || ingestToken.isEmpty()) {
    Serial.println("WARN: backend_url or ingest_token empty — packets "
                   "will decode but POSTs will be skipped");
  }

  rtl_433.initReceiver(RECEIVER_GPIO, RF_MODULE_FREQUENCY);
  rtl_433.setCallback(rtl_433_Callback);
  rtl_433.enableReceiver();
  Serial.println("Receiver enabled");
}

void loop() {
  rtl_433.loop();
}
