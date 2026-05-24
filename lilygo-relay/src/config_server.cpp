#include "config_server.h"

#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

#include "display.h"

namespace ZasderConfigServer {

String backendUrl;
String ingestToken;

static WebServer server(80);
static Preferences prefs;
// Set to true on first successful /provision that lands both
// backend_url AND ingest_token. From that point on, /provision and
// /reset require the caller to prove they know the current
// ingest_token (Authorization: Bearer header OR `current_token` form
// field). Prevents LAN hijack: a malicious page on the same network
// can't repoint the board and silently capture the token.
//
// Recovery if the token is lost: hold RST and re-plug while pressing
// reset, then reflash the firmware over USB — that re-flashes NVS
// only optionally (depending on partition); the NVS-clear path is
// the dedicated /reset route which itself requires auth. The board
// can be physically reset by holding RST + power cycle and reflashing
// with NVS erase via `pio run -t erase`.
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

void loadFromNvs() {
  prefs.begin("zasder", /*readOnly=*/false);
  backendUrl  = prefs.getString("backend_url",  "");
  ingestToken = prefs.getString("ingest_token", "");
  provisioned = prefs.getBool("provisioned", false);
  // Self-heal: if NVS lost the flag but both creds are present (e.g.
  // upgrading from a firmware build that predates the lock), treat the
  // board as already provisioned so the lock takes effect immediately
  // rather than after the next provisioning event.
  if (!provisioned && backendUrl.length() > 0 && ingestToken.length() > 0) {
    provisioned = true;
    prefs.putBool("provisioned", true);
  }
}

void wipeIngestToken() {
  // Also clear the provisioned flag: checkAuth() requires a non-empty
  // current token to verify Bearer auth, so leaving provisioned=true
  // after wiping the token would lock /provision out forever (only
  // recovery would be USB-reflash + NVS-erase). Reverting to the
  // unprovisioned state lets the operator re-pair from any LAN
  // device with no proof-of-ownership required (matching the
  // first-boot bootstrap path).
  prefs.remove("ingest_token");
  prefs.remove("provisioned");
  ingestToken = "";
  provisioned = false;
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
  body += "  \"has_token\": "    + String(ingestToken.length() > 0 ? "true" : "false") + ",\n";
  body += "  \"token_len\": "    + String((unsigned) ingestToken.length()) + ",\n";
  body += "  \"provisioned\": "  + String(provisioned ? "true" : "false") + ",\n";
  body += "  \"pkts_decoded\": " + String(pktsDecoded) + ",\n";
  body += "  \"pkts_posted_ok\": "+ String(pktsPostedOk) + ",\n";
  body += "  \"pkts_401\": "     + String(pkts401) + ",\n";
  body += "  \"last_packet\": \""+ escapeJson(lastPacket)   + "\",\n";
  body += "  \"last_post\": \""  + escapeJson(lastPostText) + "\"\n";
  body += "}\n";
  server.send(200, "application/json", body);
}

// Constant-time string compare. Avoids leaking the token length /
// prefix via timing analysis from a LAN attacker spraying guesses.
static bool secureEquals(const String &a, const String &b) {
  if (a.length() != b.length()) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < a.length(); i++) {
    diff |= ((uint8_t) a[i]) ^ ((uint8_t) b[i]);
  }
  return diff == 0;
}

// Returns true if the caller proved they know the current
// ingest_token. Two delivery channels accepted:
//   * Authorization: Bearer <token>   (preferred — curl friendly)
//   * current_token form field        (browser-form friendly)
// On an unprovisioned board, every request is treated as authorized
// so the first /provision works without a chicken-and-egg loop.
static bool checkAuth() {
  if (!provisioned) return true;
  String supplied;
  if (server.hasHeader("Authorization")) {
    String h = server.header("Authorization");
    if (h.startsWith("Bearer ")) supplied = h.substring(7);
  }
  if (supplied.length() == 0 && server.hasArg("current_token")) {
    supplied = server.arg("current_token");
  }
  return supplied.length() > 0 && secureEquals(supplied, ingestToken);
}

static void handleProvision() {
  if (!checkAuth()) {
    server.send(403, "text/plain",
                "forbidden: this board is already provisioned. Re-send "
                "with Authorization: Bearer <current_ingest_token> or "
                "include current_token=<...> as a form field.\n");
    return;
  }
  // Accept both form-encoded and ?query=string. backend_url is
  // required; ingest_token can be set independently (handy for token
  // rotation without re-entering the URL).
  bool changed = false;
  if (server.hasArg("backend_url")) {
    String v = server.arg("backend_url");
    v.trim();
    if (v.length() > 0) {
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
  if (!changed) {
    server.send(400, "text/plain",
                "expected at least one of backend_url, ingest_token "
                "as form/query args");
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
  for (int i = 0; i < 12; i++) {
    digitalWrite(LED_BUILTIN_RX, i & 1);
    delay(250);
  }
  digitalWrite(LED_BUILTIN_RX, LOW);
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
    body +=
      "<p><b>Locked.</b> Changes require the current ingest token.</p>"
      "<form method='POST' action='/provision'>"
      "Current ingest token (proof-of-ownership): "
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
      "<form method='POST' action='/provision'>"
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
  const char *wantedHeaders[] = {"Authorization"};
  server.collectHeaders(wantedHeaders,
                        sizeof(wantedHeaders) / sizeof(wantedHeaders[0]));
  server.begin();
  Serial.printf("config server: http://%s/ (or http://%s.local/)\n",
                WiFi.localIP().toString().c_str(), mdnsName.c_str());
}

static void cycleDiagLine() {
  unsigned long now = millis();
  char buf[24];
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
