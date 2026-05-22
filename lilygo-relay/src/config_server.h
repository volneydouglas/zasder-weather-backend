#pragma once
#include <Arduino.h>

// Tiny HTTP server on port 80 exposing:
//   GET  /              JSON status (uptime, IP, mac, last packet/post,
//                       which creds are set — never the token itself)
//   POST /provision     form fields backend_url + ingest_token; saves
//                       to NVS, replies 200 with new status
//   POST /identify      blinks the on-board LED for 3 s so you can pick
//                       this board out of a row of identical-looking
//                       LilyGOs on a shelf
//   POST /reset         wipes NVS + reboots into Wi-Fi-only AP portal
//                       (Wi-Fi creds + everything else cleared)
//
// Also starts mDNS as `zasder-lilygo.local` so the board can be reached
// without an IP lookup on Bonjour-aware OSes (macOS/iOS/Linux/Win10+).

namespace ZasderConfigServer {

void begin();        // call after WiFi is connected
void loop();         // call from main loop()

// Called by the rest of the firmware to feed the status page.
void noteIncomingPacket(const char *model, uint32_t id);
void notePostResult(int httpCode);

// Backend creds — owned here so /provision can update them in-place.
// main + zasder_post read these directly (extern below).
extern String backendUrl;
extern String ingestToken;

void loadFromNvs();  // populates backendUrl + ingestToken from NVS
void wipeIngestToken();  // for the 401-auto-recovery path

}  // namespace ZasderConfigServer
