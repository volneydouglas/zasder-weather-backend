# lilygo-relay

ESP32 + SX1276 firmware that captures weather sensor data off 433 MHz
or 915 MHz and forwards it to the [Zasder Weather
backend](https://github.com/volneydouglas/zasder-weather-backend) over
Wi-Fi. One [LilyGO T3 LoRa32 V1.6.1](https://www.lilygo.cc/products/lora3)
board (~$25) per band, no Raspberry Pi required.

Built on [rtl_433_ESP](https://github.com/NorthernMan54/rtl_433_ESP)
(port of selected rtl_433 decoders to ESP32 + SX1276) plus
[WiFiManager](https://github.com/tzapu/WiFiManager) for first-boot
provisioning. The SX1276 is operated in raw OOK/FSK RX mode — the same
modulation rtl_433 expects from an SDR — not as a LoRa modem.

## What it captures

- **AcuRite Atlas** (433.92 MHz, OOK) — temperature, humidity, wind,
  rain, UV, light, dew point computed on-device. Decoded by
  rtl_433_ESP's port of `Acurite-Atlas`.
- **Fine Offset / WS-2000 family** (915 MHz, FSK) — the outdoor 6-in-1
  array used by AmbientWeather WS-2000 / WS-2902 / Ecowitt
  (`Fineoffset-WH24`, `-WH65B`, `-WS80`).
- **Optional indoor WH32B** (915 MHz, FSK) — if a `Fineoffset-WH32B`
  indoor sensor is in range, its temp / humidity / pressure are cached
  and **merged into the outdoor station's** `indoor` + `pressure`
  blocks — does not spawn a separate indoor-only device row.

One board covers one band, so a typical install is two boards: one
flashed for 433, one for 915. They run independently — no
synchronization needed; both POST to the same backend.

## Build

[PlatformIO](https://platformio.org/) (Core or VS Code extension).

```sh
brew install platformio                              # macOS — others: see platformio.org

git clone https://github.com/volneydouglas/zasder-weather-backend.git
cd zasder-weather-backend/lilygo-relay

# Plug in the LilyGO. On macOS it shows up as /dev/cu.usbserial-XXXX;
# Linux is /dev/ttyUSB0. PlatformIO auto-detects.

pio run -e t3_v161_433 -t upload      # 433 MHz build (AcuRite Atlas)
# or
pio run -e t3_v161_915 -t upload      # 915 MHz build (Fineoffset family)
```

The two envs share the same source; they differ only in
`RF_MODULE_FREQUENCY`, the `OOK_MODULATION` flag (433=OOK, 915=FSK),
and the `source` tag stamped into each observation. Flash whichever
band matches the dongle.

### Known flashing quirks

- **Upload speed is pinned to 115200** because the CP2104 on this board
  corrupts the stream mid-write at the PIO default (921600). At 115200
  a full flash takes ~85 sec.
- **`huge_app.csv` partition** is required (set in `platformio.ini`)
  — default ESP32 partitions give the app only 1.25 MB; rtl_433_ESP +
  WiFiManager + ArduinoJson is ~1.4 MB.
- If you see `kAMDMobileImageMounterDeviceLocked` or any other USB
  hiccup, the CP2104 auto-reset can flake — power-cycle the board
  with the USB cable plugged in and retry the upload.

## First-boot provisioning

Two-step pattern. **Step 1** sets up Wi-Fi only; **step 2** sets backend
URL + ingest token via a local HTTP POST. Keeps the captive portal
simple and avoids the field-loss bug we hit with WiFiManager params.

### Step 1 — Wi-Fi

After flashing, the board comes up as a Wi-Fi access point named
**`ZasderLilyGO`**. Join it from a phone or laptop — a captive portal
opens automatically (or browse to `http://192.168.4.1`). Fill in your
home Wi-Fi SSID + password and hit Save. The board reboots, joins your
Wi-Fi, and announces itself via mDNS as `zasder-lilygo-XXXX.local`
(where XXXX = last 2 bytes of the chip MAC).

### Step 2 — Backend URL + token

From any device on the same LAN:

```sh
curl -X POST http://<board-ip-or-mdns>/provision \
  --data-urlencode "backend_url=https://your-backend.example.com" \
  --data-urlencode "ingest_token=$INGEST_TOKEN"
```

Replace `$INGEST_TOKEN` with the value of `INGEST_TOKEN` from your
backend's environment. The board immediately starts POSTing
observations every time it decodes a packet.

The status page at `http://<board>/status` returns JSON: IP, uptime,
packet counts, last-RX info, last-POST result. The status page also
ships a browser-friendly form at `/` for non-curl users.

### Re-provisioning

The first successful `/provision` **locks** the board. Subsequent
changes (rotate token, repoint backend URL, `/reset`, `/identify`)
require proof-of-ownership by re-presenting the **current** ingest
token. Two ways:

```sh
# Header form (curl / automation):
curl -X POST http://<board>/provision \
  -H "Authorization: Bearer $CURRENT_INGEST_TOKEN" \
  --data-urlencode "backend_url=https://new-backend.example.com"

# Form-field form (browser-friendly — the HTML form at / has a
# 'Current ingest token' field that does the same thing):
curl -X POST http://<board>/provision \
  --data-urlencode "current_token=$CURRENT_INGEST_TOKEN" \
  --data-urlencode "ingest_token=$NEW_TOKEN"
```

This blocks the LAN-hijack class of attack — anything on your Wi-Fi
that doesn't know the current token can't repoint the board to a
malicious backend and capture the next post. `/status` stays open
(read-only diagnostic; reports `provisioned: true|false`).

If you lost the current token: hold RST and power-cycle, then USB-reflash with `pio run -t erase` to wipe NVS, then re-flash and start fresh.

## Verify

```sh
curl -H "Authorization: Bearer $API_TOKEN" "$BACKEND_URL/api/devices"
```

Look for `5D:5D:01:...` (Atlas) or `5D:5D:02:...` (Fineoffset outdoor).
The synthetic-MAC scheme means the same physical sensor lands on the
same device row no matter which receiver(s) catch it.

## OLED status display

Each board has a built-in 0.96" SSD1306 OLED. It auto-renders:

- Header: live POST counters (`ok=N 401=N`)
- Row 1: source tag (e.g. `acurite-atlas-lilygo`)
- Row 2: **cycles every 5s** through IP / mDNS / uptime / WiFi RSSI / last-rx age
- Row 3: last RX (`rx: Acurite-Atlas#711`)
- Row 4: last POST result (`post: 200 OK`)

If you're debugging a dead board, the cycling row gives every diagnostic
you'd want without needing serial. Burn-in mitigated via 30%-contrast
default + automatic polarity invert every 4 hours.

## Field-tested gotchas

- **OLED reset pin on V1.6.1**: PlatformIO's `ttgo-lora32-v21new` variant
  declares `OLED_RST=16`, but on V1.6.1 GPIO16 is **NOT** the OLED reset.
  LilyGO's own example marks it `UNUSED_PIN`. We pass `U8X8_PIN_NONE` to
  the U8g2 constructor and never touch GPIO16 — fixes a hard WDT loop
  during display init.
- **Fineoffset is FSK, not OOK**: the 915 build sets `OOK_MODULATION=false`.
  Without it, the SX1276 is in OOK mode and literally cannot demodulate
  Fineoffset's FSK packets.
- **Wrong board variant on V1.6.1 sub-revisions**: only one physical
  button (RST + a power slide switch — no BOOT button). Auto-reset via
  CP2104 DTR/RTS works for upload; manual bootloader entry isn't available.

## Security

- **TLS cert pinning** — ISRG Root X1 (Let's Encrypt's anchor) is
  baked into `src/root_ca.h`. The firmware calls
  `setCACert(ZASDER_ROOT_CA)`, so the backend's cert chain must
  validate to that root. Fly.io edge + any custom domain provisioned
  via `fly certs` qualifies (LE-issued). If you're running a
  self-signed dev backend, set `-DTLS_INSECURE=1` in
  `platformio.ini`'s `build_flags` — opt-in only; insecure TLS
  exposes the ingest token to anyone on the Wi-Fi path.
- **Provisioning lock** — first successful `/provision` flips an NVS
  flag; from then on `/provision`, `/reset`, and `/identify` require
  the current ingest token as Bearer auth (or `current_token=` form
  field). `/status` stays open.
- **Token in NVS, not flash** — secrets live in encrypted NVS
  partition; a physical-access attacker who pulls the chip would need
  to defeat NVS encryption to extract them.

## Limitations (v1)

- **No long-tail RF discovery.** Firmware only decodes the configured
  protocol set. To survey what's else nearby, run rtl_433 on a separate
  RTL-SDR — they coexist fine.
- **Yearly-rain calibration**: rtl_433 emits a lifetime cumulative rain
  counter; the firmware posts it as `rain.yearly_in`. Use the backend's
  `INGEST_YEARLY_RAIN_OFFSETS` env (per-MAC JSON map) to subtract the
  sensor's lifetime baseline so iOS shows actual YTD inches.
- **Single-band per board.** One LilyGO covers one band (433 OR 915,
  build-time choice). Two-band coverage = two boards.

## License

GPL-3.0. Inherited from rtl_433_ESP. The GPL is contained to this
subdirectory; the rest of the parent repo is MIT.
