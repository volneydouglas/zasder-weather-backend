// Minimal Improv Wi-Fi serial implementation (spec v1). Hand-rolled rather
// than a library dependency: the protocol is ~a page of spec, and the two
// Arduino libraries for it each pull in their own Wi-Fi handling — this
// board's Wi-Fi lifecycle already belongs to WiFiManager + main.cpp.
//
// Frame:  "IMPROV" 0x01 <type> <len> <data…> <checksum>
// where checksum = (sum of every previous byte) & 0xFF.
#include "improv_serial.h"
#include "improv_frame.h"

#include <Arduino.h>
#include <WiFi.h>

namespace {

// ── protocol constants ────────────────────────────────────────────────────
constexpr uint8_t TYPE_CURRENT_STATE = 0x01;
constexpr uint8_t TYPE_ERROR_STATE   = 0x02;
constexpr uint8_t TYPE_RPC           = 0x03;
constexpr uint8_t TYPE_RPC_RESPONSE  = 0x04;

constexpr uint8_t STATE_READY        = 0x02;
constexpr uint8_t STATE_PROVISIONING = 0x03;
constexpr uint8_t STATE_PROVISIONED  = 0x04;

constexpr uint8_t ERR_NONE              = 0x00;
constexpr uint8_t ERR_INVALID_RPC       = 0x01;
constexpr uint8_t ERR_UNKNOWN_RPC       = 0x02;
constexpr uint8_t ERR_UNABLE_TO_CONNECT = 0x03;

constexpr uint8_t CMD_WIFI_SETTINGS     = 0x01;
constexpr uint8_t CMD_GET_CURRENT_STATE = 0x02;
constexpr uint8_t CMD_GET_DEVICE_INFO   = 0x03;
constexpr uint8_t CMD_GET_WIFI_NETWORKS = 0x04;

void (*g_feed)() = nullptr;

// ── framing ───────────────────────────────────────────────────────────────
void sendPacket(uint8_t type, const uint8_t *data, uint8_t len) {
  uint8_t out[10 + 255];
  size_t n = 0;
  memcpy(out, "IMPROV", 6); n = 6;
  out[n++] = 0x01;           // protocol version
  out[n++] = type;
  out[n++] = len;
  memcpy(out + n, data, len); n += len;
  uint32_t sum = 0;
  for (size_t i = 0; i < n; i++) sum += out[i];
  out[n++] = sum & 0xFF;
  Serial.write(out, n);
  Serial.write('\n');        // spec: packets may be newline-separated
}

void sendState(uint8_t state) { sendPacket(TYPE_CURRENT_STATE, &state, 1); }
void sendError(uint8_t err)   { sendPacket(TYPE_ERROR_STATE, &err, 1); }

// RPC response: [answered command, payload length, (len,string)…]
void sendRpcResponse(uint8_t cmd, const char *const strings[], uint8_t count) {
  uint8_t data[255];
  size_t n = 2;              // cmd + payload-length placeholder
  data[0] = cmd;
  for (uint8_t i = 0; i < count; i++) {
    size_t sl = strlen(strings[i]);
    if (sl > 63) sl = 63;                       // keep frames tiny
    if (n + 1 + sl > sizeof(data)) break;
    data[n++] = (uint8_t)sl;
    memcpy(data + n, strings[i], sl); n += sl;
  }
  data[1] = (uint8_t)(n - 2);
  sendPacket(TYPE_RPC_RESPONSE, data, (uint8_t)n);
}

uint8_t currentState() {
  return WiFi.status() == WL_CONNECTED ? STATE_PROVISIONED : STATE_READY;
}

void sendDeviceUrl(uint8_t cmd) {
  String url = "http://" + WiFi.localIP().toString() + "/";
  const char *one[] = { url.c_str() };
  sendRpcResponse(cmd, one, 1);
}

// ── RPC handlers ──────────────────────────────────────────────────────────
void handleWifiSettings(const uint8_t *d, uint8_t len) {
  // d: [ssid_len, ssid…, pass_len, pass…]. All bounds math lives in
  // improv_frame.h (pure, native-tested) after the R6 uint8_t-truncation
  // OOB read — this function may index the payload ONLY through spans
  // that came back ok.
  ImprovWifiSpans sp = improv_wifi_spans(d, len);
  if (!sp.ok) { sendError(ERR_INVALID_RPC); return; }
  char ssid[33] = {0}, pass[65] = {0};
  memcpy(ssid, d + sp.ssid_off, sp.ssid_len > 32 ? 32 : sp.ssid_len);
  memcpy(pass, d + sp.pass_off, sp.pass_len > 64 ? 64 : sp.pass_len);

  Serial.printf("Improv: joining \"%s\"\n", ssid);
  sendState(STATE_PROVISIONING);
  // persistent(true) BEFORE begin: the creds must land in NVS so the normal
  // WiFiManager autoConnect path finds them on every later boot.
  WiFi.persistent(true);
  WiFi.begin(ssid, pass);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000UL) {
    if (g_feed) g_feed();
    delay(100);
  }
  if (WiFi.status() == WL_CONNECTED) {
    sendState(STATE_PROVISIONED);
    sendDeviceUrl(CMD_WIFI_SETTINGS);   // browser shows "visit device"
  } else {
    sendError(ERR_UNABLE_TO_CONNECT);
    sendState(STATE_READY);
  }
}

void handleGetNetworks() {
  if (g_feed) g_feed();
  int16_t n = WiFi.scanNetworks(/*async=*/false, /*hidden=*/false);
  if (g_feed) g_feed();
  for (int16_t i = 0; i < n && i < 25; i++) {
    // Named locals, not temporaries (R6): WiFi.SSID(i) returns a String BY
    // VALUE — .c_str() on the temporary dangles the moment the declaration
    // ends, and sendRpcResponse then strlen/memcpys freed heap. It worked
    // by luck (nothing reallocated in between); same fix sendDeviceUrl
    // already carries.
    String ssid = WiFi.SSID(i);
    String rssi = String(WiFi.RSSI(i));
    const char *strings[] = {
      ssid.c_str(), rssi.c_str(),
      WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "NO" : "YES",
    };
    sendRpcResponse(CMD_GET_WIFI_NETWORKS, strings, 3);
  }
  WiFi.scanDelete();
  sendRpcResponse(CMD_GET_WIFI_NETWORKS, nullptr, 0);  // end-of-list marker
}

void handleRpc(const uint8_t *data, uint8_t len) {
  if (len < 2) { sendError(ERR_INVALID_RPC); return; }
  uint8_t cmd = data[0], dlen = data[1];
  // Envelope math in improv_frame.h — the same uint8_t-truncation class
  // as handleWifiSettings, pinned natively (R6).
  if (!improv_rpc_envelope_ok(dlen, len)) { sendError(ERR_INVALID_RPC); return; }
  switch (cmd) {
    case CMD_GET_CURRENT_STATE: {
      uint8_t s = currentState();
      sendState(s);
      if (s == STATE_PROVISIONED) sendDeviceUrl(CMD_GET_CURRENT_STATE);
      break;
    }
    case CMD_GET_DEVICE_INFO: {
      const char *info[] = {
        "Zasder LilyGO Relay",           // firmware name
#ifdef ZASDER_FW_VERSION
        ZASDER_FW_VERSION,
#else
        "dev",
#endif
        "ESP32",                          // chip variant
        "LilyGO T3 V1.6.1",               // device name
      };
      sendRpcResponse(CMD_GET_DEVICE_INFO, info, 4);
      break;
    }
    case CMD_GET_WIFI_NETWORKS: handleGetNetworks(); break;
    case CMD_WIFI_SETTINGS:     handleWifiSettings(data + 2, dlen); break;
    default:                    sendError(ERR_UNKNOWN_RPC); break;
  }
}

// ── incremental parser ────────────────────────────────────────────────────
// Rolling match on the "IMPROV" header so log noise between packets (this
// UART also carries our logs) can never desync the parser.
uint8_t  g_buf[6 + 3 + 255 + 1];
size_t   g_pos = 0;

void feedByte(uint8_t b) {
  static const uint8_t HDR[6] = {'I', 'M', 'P', 'R', 'O', 'V'};
  if (g_pos < 6) {
    if (b == HDR[g_pos]) { g_buf[g_pos++] = b; }
    else { g_pos = (b == HDR[0]) ? 1 : 0; if (g_pos) g_buf[0] = b; }
    return;
  }
  g_buf[g_pos++] = b;
  if (g_pos < 9) return;                        // need version+type+len
  uint8_t len = g_buf[8];
  if (g_pos < (size_t)(9 + len + 1)) return;    // + checksum
  uint32_t sum = 0;
  for (size_t i = 0; i < g_pos - 1; i++) sum += g_buf[i];
  bool ok = ((sum & 0xFF) == g_buf[g_pos - 1]) && g_buf[6] == 0x01;
  uint8_t type = g_buf[7];
  const uint8_t *data = g_buf + 9;
  g_pos = 0;
  if (!ok) return;                              // bad checksum: ignore
  if (type == TYPE_RPC) handleRpc(data, len);
  // CURRENT_STATE etc. from the host are not a thing; ignore other types.
}

}  // namespace

namespace ZasderImprov {

void begin(void (*feed)()) { g_feed = feed; }

void service() {
  // Bounded per pass (CodeRabbit, PR #27): a host that keeps the UART
  // saturated could hold this loop forever — starving rtl_433.loop(),
  // the config server, and the watchdog feed until the 60s restart
  // fired and dropped decoded packets. 64 bytes ≫ any Improv frame.
  constexpr size_t kMaxBytesPerPass = 64;
  for (size_t i = 0; i < kMaxBytesPerPass && Serial.available() > 0; i++) {
    feedByte((uint8_t)Serial.read());
  }
}

}  // namespace ZasderImprov
