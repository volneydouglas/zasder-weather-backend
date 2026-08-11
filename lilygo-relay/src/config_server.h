#pragma once
#include <Arduino.h>

// Tiny HTTP server on port 80 exposing:
//   GET  /              HTML status page + browser provisioning form
//                       (setup-key field when unprovisioned, current-token
//                       field once locked)
//   GET  /status        JSON status (uptime, IP, mac, last packet/post,
//                       which creds are set — never the token itself)
//   POST /provision     form fields backend_url + ingest_token; saves
//                       to NVS, replies 200 with new status. EVERY call
//                       requires proof-of-ownership — the per-device setup
//                       key (see below) for the first provision, and the
//                       current ingest token OR that key thereafter. There
//                       is no anonymous provisioning at any point.
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
// Re-bind the HTTP listener + re-announce mDNS after a Wi-Fi reconnect.
// A dropped/reconnected STA invalidates the old listening socket; without
// this, handleClient() can wedge on the stale socket (loop freezes while
// ping/mDNS still answer). Safe to call repeatedly from loop().
void onReconnect();

// Called by the rest of the firmware to feed the status page.
void noteIncomingPacket(const char *model, uint32_t id);
void notePostResult(int httpCode);

// Backend creds — owned here so /provision can update them in-place.
// main + zasder_post read these directly (extern below).
extern String backendUrl;
extern String ingestToken;

// Per-device setup key: a random 8-char secret minted on first boot and
// kept in NVS across token wipes. It is the proof-of-ownership for BOTH
// provisioning events:
//   * the FIRST /provision on a fresh board — there is no ingest_token yet,
//     so this key is the only thing that opens it. Without that requirement
//     whoever reached an unprovisioned board first could claim it and lock
//     the owner out, recoverable only by a physical NVS erase.
//   * re-provisioning after repeated 401s wiped the token, when the key is
//     again the only remaining credential.
// Shown on the OLED + serial only while a provision is pending. It is never
// returned by any HTTP route — /status reports `has_token` and `token_len`
// and nothing about this key — so reading it requires physical sight of the
// board or its serial console. Like the token and the Wi-Fi credentials it
// sits in plaintext NVS (no flash encryption in this build), so "physical
// access" includes a USB `esptool read_flash` — see the README's Security
// section for that residual risk. Lost it? Physical USB reflash with NVS
// erase (`pio run -t erase`) mints a fresh one. See config_server.cpp.
extern String setupKey;

// Opt-in: forward ANY decoded rtl_433 station that carries weather fields
// (temp/humidity/wind/rain), not just the whitelisted Atlas/Fineoffset
// models. Off by default — the 433 band is full of neighbors' sensors and
// each forwarded model+id becomes its own device row on the backend.
// This is the forward-compat path for new hardware (e.g. AcuRite Optimus,
// rtl_433 issue #3444): the moment the bundled decoders learn a model it
// flows through with no firmware change. Set via POST /provision field
// `forward_all` (1/0).
extern bool forwardAll;

void loadFromNvs();  // populates backendUrl + ingestToken from NVS
void wipeIngestToken();  // for the 401-auto-recovery path

}  // namespace ZasderConfigServer
