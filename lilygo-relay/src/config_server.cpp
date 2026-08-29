#include "config_server.h"

#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_random.h>

#include "auth_logic.h"
#include "display.h"
#include "url_logic.h"

namespace ZasderConfigServer {

String backendUrl;
String ingestToken;
String setupKey;
bool forwardAll = false;

static WebServer server(80);
static Preferences prefs;
// Set to true on first successful /provision that lands both
// backend_url AND ingest_token. From that point on, /provision and
// /reset require the caller to prove they know the current
// ingest_token OR the per-device setup key (Authorization: Bearer
// header, `current_token`, or `setup_key` form field). Prevents LAN
// hijack: a malicious page on the same network can't repoint the board
// and silently capture the token — even right after a 401 token wipe,
// when the board stays LOCKED and only the setup key (physical-access
// secret) re-opens /provision.
//
// Recovery if BOTH the token and setup key are lost: physical USB
// reflash with NVS erase (`pio run -t erase`), which mints a fresh
// setup key on next boot. The dedicated /reset route also wipes NVS
// but itself requires auth.
static bool provisioned = false;
static String        lastPacket    = "(none)";
static String        lastPostText  = "(none)";
static uint32_t      pktsDecoded   = 0;
static uint32_t      pktsPostedOk  = 0;
static uint32_t      pkts401       = 0;
static uint32_t      bootMs        = 0;
static unsigned long lastPacketMs  = 0;

// Cycling diagnostic line on the OLED — rotates IP / mDNS / uptime /
// RSSI / rx-age every 5 s so the most useful operator-debug fields all
// surface without us needing a dedicated 6-line OLED. Total cycle is
// 25 s; if the board's dead you'll see the rx-age slot tick up which
// is the loudest indicator that the radio went quiet.
static constexpr unsigned long DIAG_CYCLE_MS = 5000;
static unsigned long _lastDiagMs = 0;
static int _diagIndex = 0;

// mDNS name is computed per-board in begin() — `zasder-lilygo-XXXX`
// where XXXX is the last two bytes of the chip MAC (lowercase, no
// colons). Multiple LilyGOs on the same LAN otherwise collide on
// `zasder-lilygo.local` and the resolver picks one at random.
static String mdnsName;

// Mint a random 8-char setup key. Alphabet excludes ambiguous glyphs
// (0/O/1/I) so it's readable off the small OLED. ~40 bits of entropy —
// far beyond brute-forcing over a home LAN against a single ESP32.
static String generateSetupKey() {
  static const char alphabet[] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";  // 32 chars
  String k;
  k.reserve(8);
  for (int i = 0; i < 8; i++) k += alphabet[esp_random() % 32];
  return k;
}

// Print the setup key to serial. Only called when a provision is actually
// pending: the boot that mints it, a never-provisioned board, or a token
// wiped after repeated 401s. It authorizes provisioning AND /reset, so a
// retained or forwarded serial log would hand over control of the board —
// it stays out of the log during normal operation.
static void announceSetupKey() {
  Serial.printf("setup key (provision/re-pair proof): %s\n", setupKey.c_str());
}

void loadFromNvs() {
  prefs.begin("zasder", /*readOnly=*/false);
  backendUrl  = prefs.getString("backend_url",  "");
  ingestToken = prefs.getString("ingest_token", "");
  provisioned = prefs.getBool("provisioned", false);
  forwardAll  = prefs.getBool("forward_all", false);
  // Per-device setup key: mint + persist on first boot (or after an NVS
  // erase). Kept separate from the token so a 401 wipe never removes it.
  bool minted = false;
  setupKey = prefs.getString("setup_key", "");
  if (setupKey.length() == 0) {
    setupKey = generateSetupKey();
    prefs.putString("setup_key", setupKey);
    minted = true;
  }
  if (minted || ingestToken.length() == 0) {
    announceSetupKey();
  } else {
    Serial.println("setup key: set (printed only when a re-pair is pending)");
  }
  // Self-heal: if NVS lost the flag but both creds are present (e.g.
  // upgrading from a firmware build that predates the lock), treat the
  // board as already provisioned so the lock takes effect immediately
  // rather than after the next provisioning event.
  if (!provisioned && backendUrl.length() > 0 && ingestToken.length() > 0) {
    provisioned = true;
    prefs.putBool("provisioned", true);
  }
}

void adoptIngestToken(const String &token) {
  // The server's assignment arrives over the TLS channel the board already
  // authenticated on, so adopting it is no more trusting than the POST
  // that carried it. NVS write first, then the live variable — a reboot
  // between the two lines must come up with the NEW token, not a half
  // state (the reverse order could post once with a token that was never
  // persisted and then forget it).
  prefs.putString("ingest_token", token);
  ingestToken = token;
  Serial.println("[token-upgrade] adopted a per-device token from the "
                 "server (revocable in the app under Device tokens)");
}

void wipeIngestToken() {
  // Wipe ONLY the stale token — keep provisioned=true so the board stays
  // locked. checkAuth() still authorizes re-provisioning via the setup
  // key (the token is gone, but the setup key persists), so an attacker
  // on the LAN can't repoint the board just because a 401 fired. The
  // operator re-pairs with the setup key shown on the device.
  prefs.remove("ingest_token");
  ingestToken = "";
  // Re-announce on serial. loadFromNvs() only runs at boot, so an operator
  // watching the serial console during a live 401 wipe would otherwise see
  // the board lock itself and never learn the key needed to re-pair it —
  // the OLED shows it, but a headless board has no OLED to read.
  announceSetupKey();
}

// ── handlers ──────────────────────────────────────────────────────────

static String escapeJson(const String &s) {
  String out;
  out.reserve(s.length() + 4);
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    switch (c) {
      case '"':  out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if ((uint8_t) c < 0x20) {
          char buf[8];
          snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += c;
        }
    }
  }
  return out;
}

// Deliberately unauthenticated: this is the read-only diagnostic an
// operator hits first when a board misbehaves, often from a phone with no
// way to attach a Bearer header. It never returns the token or the setup
// key — only `has_token`/`token_len`. It DOES disclose the backend URL and
// the board's MAC to anyone on the LAN; for a home-LAN device that's an
// accepted trade against losing the zero-friction diagnostic. If the board
// ever lives on a shared/untrusted network, gate the sensitive fields
// behind checkAuth() and keep only uptime/IP public.
static void handleStatus() {
  uint32_t uptimeS = (millis() - bootMs) / 1000;
  String mac = WiFi.macAddress();
  String ip  = WiFi.localIP().toString();
  String body;
  body.reserve(512);
  body  = "{\n";
  body += "  \"mac\": \""        + escapeJson(mac) + "\",\n";
  body += "  \"ip\": \""         + escapeJson(ip)  + "\",\n";
  body += "  \"mdns\": \""       + mdnsName + ".local\",\n";
  body += "  \"uptime_s\": "     + String(uptimeS) + ",\n";
  body += "  \"freq_mhz\": "     + String((double) RF_MODULE_FREQUENCY, 2) + ",\n";
  body += "  \"source\": \""     + String(ZASDER_SOURCE_TAG) + "\",\n";
  body += "  \"backend_url\": \""+ escapeJson(backendUrl) + "\",\n";
  // Whether THIS build verifies the server certificate. A TLS_INSECURE board
  // is a debug build that sends the ingest token in a readable POST to
  // anyone on the Wi-Fi path — and until this line it looked identical to a
  // pinned one from every diagnostic surface, so a dev image left in service
  // was undetectable without recompiling. Report it.
#if defined(TLS_INSECURE) && TLS_INSECURE
  body += "  \"tls\": \"INSECURE-no-cert-check\",\n";
#else
  body += "  \"tls\": \"pinned\",\n";
#endif
  body += "  \"has_token\": "    + String(ingestToken.length() > 0 ? "true" : "false") + ",\n";
  body += "  \"token_len\": "    + String((unsigned) ingestToken.length()) + ",\n";
  body += "  \"provisioned\": "  + String(provisioned ? "true" : "false") + ",\n";
  body += "  \"forward_all\": "  + String(forwardAll ? "true" : "false") + ",\n";
  body += "  \"pkts_decoded\": " + String(pktsDecoded) + ",\n";
  body += "  \"pkts_posted_ok\": "+ String(pktsPostedOk) + ",\n";
  body += "  \"pkts_401\": "     + String(pkts401) + ",\n";
  body += "  \"last_packet\": \""+ escapeJson(lastPacket)   + "\",\n";
  body += "  \"last_post\": \""  + escapeJson(lastPostText) + "\"\n";
  body += "}\n";
  server.send(200, "application/json", body);
}

// Returns true if the caller proved ownership of this board — by knowing
// EITHER the current ingest_token OR the per-device setup key. Three
// delivery channels accepted, checked against both credentials:
//   * Authorization: Bearer <secret>   (preferred — curl friendly)
//   * current_token form field         (browser-form friendly)
//   * setup_key form field             (explicit re-pair after a wipe)
// There is NO anonymous path. An unprovisioned board has no ingest_token
// yet, so the setup key is the only credential that opens the first
// /provision — it is on the OLED and in the serial banner for whoever can
// physically see the board.
static bool checkAuth() {
  // An unprovisioned board used to accept ANY caller. Whoever reached it first
  // won: they could set their own backend_url + ingest_token, which flips
  // `provisioned` and locks the board under their credentials — after which
  // the actual owner can't even /reset it, because that now requires the
  // squatter's token. Recovery is a physical USB NVS erase.
  //
  // The per-device setup key already exists for exactly this kind of proof;
  // it was simply not required for the FIRST provision. Now it always is.
  // The key is on the OLED and the serial banner while the board is
  // unprovisioned, so the person standing at it can read it and nobody else
  // can. `matches` below still accepts the ingest token as well, but there
  // isn't one yet in this state, so first provision is setup-key-only.
  //
  // Check EVERY channel independently. Collapsing them into one `supplied`
  // slot meant a stale Authorization header masked a perfectly good setup_key
  // sent alongside it — so a client couldn't recover after a token wipe.
  //
  // The matching decision itself (constant-time compare, empty-candidate /
  // empty-credential rules) lives in auth_logic.h so the native test env
  // can pin the whole matrix without the Arduino framework.
  //
  // The ingest token only counts once `provisioned` is true. handleProvision
  // accepts ingest_token WITHOUT backend_url (token rotation), so a partial
  // first provision can leave a stored token while provisioned stays false —
  // and this check would then accept a credential that the unprovisioned 403
  // guidance and the setup form never mention. The documented contract is
  // that first provisioning takes the setup key only; passing "" for the
  // token reuses authMatches's pinned "empty credential never matches" rule.
  auto matches = [](const String &candidate) -> bool {
    return ZasderAuth::authMatches(candidate.c_str(),
                                   provisioned ? ingestToken.c_str() : "",
                                   setupKey.c_str());
  };

  if (server.hasHeader("Authorization")) {
    String h = server.header("Authorization");
    if (h.startsWith("Bearer ") && matches(h.substring(7))) return true;
  }
  // Preferred channel for the setup key: a HEADER. Anything in a URL query
  // string ends up in shell history, proxy logs and the browser's address bar,
  // and this credential authorizes re-pairing and /reset.
  //
  // Known, deliberate gap: the two form-field channels below also accept the
  // credential as a URL ?query= parameter. ESP32 WebServer merges query-string
  // and POST-body parameters into one arg list with no way to tell them apart,
  // so rejecting query delivery would also reject the body field the board's
  // own HTML form submits. Closing it would mean parsing the raw request line
  // ourselves; instead the README and the serial banner steer every documented
  // flow to the header or the form body.
  if (server.hasHeader("X-Setup-Key") && matches(server.header("X-Setup-Key")))
    return true;
  if (server.hasArg("current_token") && matches(server.arg("current_token")))
    return true;
  if (server.hasArg("setup_key") && matches(server.arg("setup_key")))
    return true;
  return false;
}

static void handleProvision() {
  if (!checkAuth()) {
    // Two different states reach here and they need different instructions.
    // A single "already provisioned" message was actively misleading on a
    // fresh board — it named a credential that does not exist yet and never
    // mentioned the setup key the first provision actually requires.
    if (provisioned) {
      server.send(403, "text/plain",
                  "forbidden: this board is already provisioned. Re-send "
                  "with Authorization: Bearer <current_ingest_token>, or "
                  "include current_token=<...> as a form field. If the "
                  "token was wiped (repeated 401s), use the setup key shown "
                  "on the board's screen: setup_key=<...>.\n");
    } else {
      server.send(403, "text/plain",
                  "forbidden: first provisioning needs this board's setup "
                  "key, shown on its OLED and in the serial boot log. "
                  "Re-send with the X-Setup-Key header, or include "
                  "setup_key=<...> as a form field. The web form on / has a "
                  "field for it.\n");
    }
    return;
  }
  // Accept both form-encoded and ?query=string. backend_url is
  // required; ingest_token can be set independently (handy for token
  // rotation without re-entering the URL).
  bool changed = false;
  if (server.hasArg("backend_url")) {
    String v = server.arg("backend_url");
    v.trim();
    // Normalize away trailing slashes: the poster appends "/ingest/custom",
    // so "https://host/" would silently become "https://host//ingest/custom".
    while (v.endsWith("/")) v.remove(v.length() - 1);
    if (v.length() > 0) {
      // Require a full https://host URL (http:// too only in a
      // TLS_INSECURE dev build). The poster always connects through
      // WiFiClientSecure, so a bad URL persists fine and then fails hours
      // later at POST time with an opaque error — reject it now, while
      // the operator is watching. The scheme/host/charset decision lives
      // in url_logic.h (Arduino-free) so the native tests can pin every
      // rejected shape.
#if defined(TLS_INSECURE) && TLS_INSECURE
      constexpr bool allowHttp = true;
#else
      constexpr bool allowHttp = false;
#endif
      if (!ZasderUrl::validBackendUrl(v.c_str(), allowHttp)) {
        server.send(400, "text/plain",
                    "backend_url must be a full https://host URL (the "
                    "firmware posts over TLS; a plain-http backend needs a "
                    "TLS_INSECURE build)\n");
        return;
      }
      backendUrl = v;
      prefs.putString("backend_url", backendUrl);
      changed = true;
    }
  }
  if (server.hasArg("ingest_token")) {
    String v = server.arg("ingest_token");
    v.trim();
    if (v.length() > 0) {
      ingestToken = v;
      prefs.putString("ingest_token", ingestToken);
      changed = true;
    }
  }
  // Optional: forward-all toggle (see config_server.h). Accepts 1/0,
  // true/false, on/off.
  if (server.hasArg("forward_all")) {
    String v = server.arg("forward_all");
    v.trim(); v.toLowerCase();
    forwardAll = (v == "1" || v == "true" || v == "on");
    prefs.putBool("forward_all", forwardAll);
    Serial.printf("forward_all=%s\n", forwardAll ? "true" : "false");
    changed = true;
  }
  if (!changed) {
    server.send(400, "text/plain",
                "expected at least one of backend_url, ingest_token, "
                "forward_all as form/query args");
    return;
  }
  // First successful provision flips the lock; from now on changes
  // require Bearer auth with the current token.
  if (!provisioned && backendUrl.length() > 0 && ingestToken.length() > 0) {
    provisioned = true;
    prefs.putBool("provisioned", true);
    Serial.println("provisioning lock engaged — future changes require Bearer auth");
  }
  Serial.printf("provisioned: backend_url=%s ingest_token_len=%u\n",
                backendUrl.c_str(), (unsigned) ingestToken.length());
  handleStatus();  // reply with the fresh status
}

// Non-blocking identify blink, driven from loop(). The handler only arms
// the deadline: the old delay(250)×12 stalled loop() — and with it
// rtl_433.loop() + handleClient() — for a full 3 s, dropping any packets
// decoded during the blink.
static bool          identifyActive  = false;
static unsigned long identifyUntilMs = 0;

static void handleIdentify() {
  if (!checkAuth()) {
    server.send(403, "text/plain", "forbidden\n");
    return;
  }
  // Blink the on-board LED for 3 s. Lets the user pick this specific
  // board out of a stack of identical-looking ones — `curl -X POST
  // -H "Authorization: Bearer $INGEST_TOKEN"
  // http://zasder-lilygo.local/identify` and watch which LED dances.
  server.send(200, "text/plain", "blinking 3s\n");
  identifyActive  = true;
  identifyUntilMs = millis() + 3000;
}

// Called every pass through loop() below. Overwrites whatever the RX-flash
// path last did to the LED while active (cosmetic — RX itself is not
// affected), and leaves the LED LOW when done.
static void serviceIdentifyBlink() {
  if (!identifyActive) return;
  unsigned long now = millis();
  if ((long) (now - identifyUntilMs) < 0) {   // wrap-safe "now < until"
    digitalWrite(LED_BUILTIN_RX, (now / 250) & 1);
  } else {
    digitalWrite(LED_BUILTIN_RX, LOW);
    identifyActive = false;
  }
}

static void handleReset() {
  if (!checkAuth()) {
    server.send(403, "text/plain", "forbidden\n");
    return;
  }
  server.send(200, "text/plain",
              "wiping NVS + rebooting in 1s — board will return to "
              "Wi-Fi AP portal on next boot\n");
  delay(1000);
  prefs.clear();
  prefs.end();
  WiFi.disconnect(true, true);  // also wipe stored Wi-Fi creds
  delay(500);
  ESP.restart();
}

static void handleRoot() {
  // Tiny HTML page so a browser visit also works — pulls the JSON from
  // /status under the hood. When the board is already provisioned, the
  // form requires the operator to re-enter the current ingest token as
  // proof-of-ownership (mirrors the API's Bearer auth requirement).
  String body =
    "<!doctype html><html><body style='font-family:sans-serif'>"
    "<h2>Zasder LilyGO relay</h2>"
    "<p>JSON: <code><a href='/status'>/status</a></code></p>";

  if (provisioned) {
    bool awaitingRepair = ingestToken.length() == 0;
    body +=
      "<p><b>Locked.</b> Changes require the current ingest token "
      "&mdash; or, if the token was wiped after repeated 401s, the "
      "<b>setup key</b> shown on the board's screen.</p>";
    if (awaitingRepair) {
      body +=
        "<p style='color:#b00'>Token was wiped &mdash; enter the setup "
        "key (on the OLED) below to re-pair.</p>";
    }
    body +=
      "<form method='POST' action='/provision'>"
      "Current ingest token <i>or</i> setup key (proof-of-ownership): "
      "<input name='current_token' size='40' type='password'><br>"
      "New backend URL (leave blank to keep): "
      "<input name='backend_url' size='40'><br>"
      "New ingest token (leave blank to keep): "
      "<input name='ingest_token' size='40'><br>"
      "<button>Update</button>"
      "</form>";
  } else {
    body +=
      "<p><b>Unprovisioned.</b> First provisioning locks the board.</p>"
      "<p>Enter the <b>setup key</b> shown on the board's OLED (and in the "
      "serial boot log). It proves you can physically see this board, so "
      "nobody else on the network can claim it first.</p>"
      "<form method='POST' action='/provision'>"
      "Setup key: <input name='setup_key' size='16'><br>"
      "Backend URL: <input name='backend_url' size='40'><br>"
      "Ingest token: <input name='ingest_token' size='40'><br>"
      "<button>Provision</button>"
      "</form>";
  }

  body +=
    "<p>Identify / reset require the current ingest token via "
    "<code>Authorization: Bearer ...</code>. Use <code>curl</code> "
    "for those.</p>"
    "</body></html>";
  server.send(200, "text/html", body);
}

// ── public glue ───────────────────────────────────────────────────────

void begin() {
  bootMs = millis();
  // Build per-board mDNS name from last 2 MAC bytes.
  String mac = WiFi.macAddress();           // "F0:24:F9:AF:22:E4"
  String suffix = mac.substring(12);        // "22:E4"
  suffix.replace(":", "");
  suffix.toLowerCase();
  mdnsName = String("zasder-lilygo-") + suffix;

  if (MDNS.begin(mdnsName.c_str())) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("mDNS up: %s.local\n", mdnsName.c_str());
  } else {
    Serial.println("mDNS begin failed (board still reachable by IP)");
  }
  server.on("/",          HTTP_GET,  handleRoot);
  server.on("/status",    HTTP_GET,  handleStatus);
  server.on("/provision", HTTP_POST, handleProvision);
  server.on("/identify",  HTTP_POST, handleIdentify);
  server.on("/reset",     HTTP_POST, handleReset);
  // ESP32 WebServer ignores headers by default; whitelist Authorization
  // so checkAuth() can read it.
  const char *wantedHeaders[] = {"Authorization", "X-Setup-Key"};
  server.collectHeaders(wantedHeaders,
                        sizeof(wantedHeaders) / sizeof(wantedHeaders[0]));
  server.begin();
  Serial.printf("config server: http://%s/ (or http://%s.local/)\n",
                WiFi.localIP().toString().c_str(), mdnsName.c_str());
}

void onReconnect() {
  // Route handlers registered in begin() persist on the server object;
  // we only need to re-bind the listening socket and re-announce mDNS on
  // the new IP. This clears the stale post-reconnect socket that would
  // otherwise wedge handleClient().
  server.stop();
  server.begin();
  MDNS.end();
  if (MDNS.begin(mdnsName.c_str())) {
    MDNS.addService("http", "tcp", 80);
  }
  Serial.printf("[reconnect] HTTP server + mDNS re-bound on %s\n",
                WiFi.localIP().toString().c_str());
}

static void cycleDiagLine() {
  unsigned long now = millis();
  char buf[24];
  // Locked with a wiped token = awaiting re-pair. Surface the setup key
  // so the operator standing at the board can read it and re-provision.
  // Only shown in this state — hidden during normal operation so the
  // recovery secret isn't left on-screen permanently.
  // Two states need the key on screen: awaiting re-pair after a token wipe,
  // and never-provisioned. First provision now REQUIRES this key (see
  // checkAuth), so without showing it here a fresh board is unsetupable.
  if (ingestToken.length() == 0) {
    snprintf(buf, sizeof(buf), "%s: %s",
             provisioned ? "re-pair key" : "setup key", setupKey.c_str());
    ZasderDisplay::update(nullptr, nullptr, buf, nullptr, nullptr);
    return;
  }
  switch (_diagIndex) {
    case 0:
      snprintf(buf, sizeof(buf), "IP: %s",
               WiFi.localIP().toString().c_str());
      break;
    case 1:
      // Trim "zasder-lilygo-" prefix on the OLED line — saves space.
      snprintf(buf, sizeof(buf), "mDNS: ...-%s",
               mdnsName.length() > 14 ? mdnsName.c_str() + 14 : "?");
      break;
    case 2: {
      unsigned long upS = (now - bootMs) / 1000;
      if (upS < 60)        snprintf(buf, sizeof(buf), "up: %lus", upS);
      else if (upS < 3600) snprintf(buf, sizeof(buf), "up: %lum%lus",
                                    upS / 60, upS % 60);
      else if (upS < 86400) snprintf(buf, sizeof(buf), "up: %luh%lum",
                                    upS / 3600, (upS % 3600) / 60);
      else                 snprintf(buf, sizeof(buf), "up: %lud%luh",
                                    upS / 86400, (upS % 86400) / 3600);
      break;
    }
    case 3:
      snprintf(buf, sizeof(buf), "WiFi: %d dBm", (int) WiFi.RSSI());
      break;
    case 4:
      if (lastPacketMs == 0) {
        snprintf(buf, sizeof(buf), "rx age: never");
      } else {
        unsigned long ago = (now - lastPacketMs) / 1000;
        if (ago < 60)        snprintf(buf, sizeof(buf), "rx age: %lus", ago);
        else if (ago < 3600) snprintf(buf, sizeof(buf), "rx age: %lum", ago / 60);
        else                 snprintf(buf, sizeof(buf), "rx age: %luh", ago / 3600);
      }
      break;
  }
  _diagIndex = (_diagIndex + 1) % 5;
  ZasderDisplay::update(nullptr, nullptr, buf, nullptr, nullptr);
}

void loop() {
  server.handleClient();
  serviceIdentifyBlink();
  unsigned long now = millis();
  if (now - _lastDiagMs >= DIAG_CYCLE_MS) {
    _lastDiagMs = now;
    cycleDiagLine();
  }
}

void noteIncomingPacket(const char *model, uint32_t id) {
  pktsDecoded++;
  lastPacketMs = millis();
  char buf[48];
  snprintf(buf, sizeof(buf), "%.32s #%u", model, (unsigned) id);
  lastPacket = buf;
  char dispLine[24];
  snprintf(dispLine, sizeof(dispLine), "rx: %.14s#%u",
           model, (unsigned) id);
  // Updates line 3 only — line 2 is owned by the cycling diag below.
  ZasderDisplay::update(nullptr, nullptr, nullptr, dispLine, nullptr);
}

void notePostResult(int httpCode) {
  if (httpCode >= 200 && httpCode < 300) {
    pktsPostedOk++;
    lastPostText = String(httpCode) + " OK";
  } else if (httpCode == 401) {
    pkts401++;
    lastPostText = "401 unauthorized";
  } else {
    lastPostText = String(httpCode) + " err";
  }
  char dispLine[24];
  snprintf(dispLine, sizeof(dispLine), "post: %s",
           lastPostText.c_str());
  // Counters in the header, last POST result on line 4. Line 1
  // (source tag), line 2 (cycling diag), and line 3 (last rx) are
  // each owned by other callers.
  char hdrLine[24];
  snprintf(hdrLine, sizeof(hdrLine), "ok=%lu 401=%lu",
           (unsigned long) pktsPostedOk, (unsigned long) pkts401);
  ZasderDisplay::update(hdrLine, nullptr, nullptr, nullptr, dispLine);
}

}  // namespace ZasderConfigServer
