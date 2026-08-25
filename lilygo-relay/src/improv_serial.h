#pragma once
// Improv Wi-Fi over serial (improv-wifi.com/serial) — the protocol ESP Web
// Tools speaks after flashing, so the SAME browser dialog that installed the
// firmware can put the board on Wi-Fi: the user picks a network and types
// the password in the browser, the credentials travel over the USB cable
// (never through the setup AP), and the dialog then offers a "visit device"
// link straight to the board's config page by IP — no AP join, no mDNS.
//
// The WiFiManager captive portal stays as the fallback path: Improv only
// ADDS a provisioning route, it never replaces one (a phone-only setup has
// no USB host, and a failed display taught us never to have a single path).
namespace ZasderImprov {

// `feed` is called during the blocking waits (connect ~30s max, scan a few
// seconds) so the loop watchdog never mistakes provisioning for a stall.
void begin(void (*feed)());

// Poll the serial byte stream and answer any complete Improv packet.
// Cheap no-op when no bytes are waiting — safe to call from loop() so an
// already-provisioned board can still be re-pointed from the browser.
void service();

}  // namespace ZasderImprov
