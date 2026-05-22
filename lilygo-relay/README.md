# lilygo-relay

ESP32 + SX1276 firmware that captures weather sensor data off 433 MHz
or 915 MHz and forwards it to the [Zasder Weather
backend](https://github.com/volneydouglas/zasder-weather-backend) over
Wi-Fi. The budget alternative to the RTL-SDR + Raspberry Pi path: one
[LilyGO T3 LoRa32 V1.6.1](https://www.lilygo.cc/products/lora3) board
(~$25) per band, no Pi required.

Built on [rtl_433_ESP](https://github.com/NorthernMan54/rtl_433_ESP)
(port of selected rtl_433 decoders to the ESP32 + SX1276) plus
[WiFiManager](https://github.com/tzapu/WiFiManager) for first-boot
provisioning. The SX1276 is operated in raw OOK RX mode — the same
modulation rtl_433 expects from an SDR — not as a LoRa modem.

## What it captures

- **AcuRite Atlas** (433.92 MHz) — temperature, humidity, wind, rain,
  UV, light. Decoded by rtl_433_ESP's port of `Acurite-Atlas`.
- **Fine Offset / WS-2000 family** (915 MHz) — the outdoor 6-in-1
  array used by AmbientWeather WS-2000, WS-2902, Ecowitt
  (`Fineoffset-WH24`, `-WH65B`, `-WS80`).
- **Optional indoor pairing** (915 MHz) — if a `Fineoffset-WH32B`
  indoor sensor is in range, its temp/humidity/pressure show up as a
  separate device row; future firmware versions may merge it into the
  outdoor station's `indoor` block the way `sdr-relay` does.

One board covers one band, so a typical install is two boards: one
flashed for 433, one for 915. They run independently — no
synchronization needed; both POST to the same backend.

## vs. sdr-relay

|                          | sdr-relay (RTL-SDR + Pi) | lilygo-relay (LilyGO ESP32) |
|--------------------------|-------------------------|-----------------------------|
| Hardware cost (both bands) | ~$80 (2 dongles + Pi 4) | ~$50 (2 boards)             |
| Setup                    | Docker compose on Linux | Flash firmware, join AP, enter creds |
| Decoder coverage         | All ~150 rtl_433 protocols | ~30 protocols (rtl_433_ESP subset) |
| Atlas multi-packet coalescing | Yes (60s window)    | No — posts each packet raw  |
| Long-tail RF discovery   | Yes                     | No                          |
| Maintenance              | Standard Linux box      | Self-contained appliance    |

The right pick: **sdr-relay** if you already have a Pi and want the
full protocol surface; **lilygo-relay** if you want the cheapest
possible turnkey weather receiver and don't need the discovery survey.

## Build

[PlatformIO](https://platformio.org/) (Core or VS Code extension).

```sh
git clone https://github.com/volneydouglas/zasder-weather-backend.git
cd zasder-weather-backend/lilygo-relay

# Plug in the LilyGO. On macOS it shows up as /dev/cu.usbserial-XXXX;
# Linux is /dev/ttyUSB0. PlatformIO auto-detects.

pio run -e t3_v161_433 -t upload   # 433 MHz build (AcuRite Atlas)
# or
pio run -e t3_v161_915 -t upload   # 915 MHz build (Fineoffset family)

pio device monitor                  # watch the serial log
```

The two envs share the same source; they differ only in
`RF_MODULE_FREQUENCY` and the `source` tag stamped into each
observation. Flash whichever band matches the dongle.

## First-boot provisioning

After flashing, the board comes up as a Wi-Fi access point named
**`ZasderLilyGO`**. Join it from a phone or laptop — a captive portal
opens automatically (or browse to `http://192.168.4.1`). Fill in:

- Your home Wi-Fi SSID + password
- `BACKEND_URL` — bare base, no trailing slash, e.g. `https://weather.example.com`
- `INGEST_TOKEN` — must match the value of `INGEST_TOKEN` in the
  backend's environment. Same token sdr-relay / legacy relay use.

Hit save. The board reboots, joins your Wi-Fi, and starts POSTing
observations within a minute or two.

To re-provision (changed Wi-Fi, swapped backend, etc.) hold the BOOT
button while plugging the board back in — the AP comes back up.

## Verify

Once provisioned, check the backend's `/api/devices` to see the new
device row appear:

```sh
curl -H "Authorization: Bearer $API_TOKEN" $BACKEND_URL/api/devices
```

You should see a `5D:5D:01:...` MAC for an Atlas board or
`5D:5D:02:...` for a Fineoffset board, alongside any sdr-relay or
legacy-relay devices you already have. The synthetic MAC scheme is
identical between sdr-relay and lilygo-relay, so the same physical
sensor lands on the same device row regardless of which receiver you
use — that means you can A/B test by running both in parallel without
duplicate rows.

## Limitations (v1)

These are conscious choices to keep v1 simple and the binary small.
None of them block daily operation; each has a planned fix.

- **No Atlas multi-packet coalescing.** Atlas cycles 8 message types
  per upload pass, each with a different field subset. sdr-relay
  buffers them for ~60s and posts one merged observation; lilygo-relay
  POSTs every packet as-is. The backend's UPSERT means the freshest
  value of each field wins, but a single `/api/devices/{mac}/current`
  read could return a row with e.g. `tempf` set but `windspeedmph` not
  yet replaced from the previous cycle. Self-corrects within ~60s.
- **No yearly-rain calibration.** sdr-relay baselines against a value
  you supply at deploy time (e.g. your prior AWN reading) and tracks
  deltas. lilygo-relay just forwards rtl_433's lifetime cumulative
  counter — fine for "is it raining right now" but useless for
  yearly totals until you do the math externally.
- **No long-tail RF discovery.** sdr-relay records every decoded
  packet's `(model, id)` to `/data/discoveries.json` for an inventory
  of nearby RF devices. The LilyGO firmware doesn't — register a
  subset of decoders at build time and that's what you get.
- **`setInsecure()` on TLS.** v1 doesn't validate the backend's TLS
  certificate. Pinning the Let's Encrypt root via `setCACert()` is a
  small follow-up — adds ~2 KB of program memory.

## License

GPL-3.0. Inherited from rtl_433_ESP, which is GPL-3.0 — any firmware
linking against its decoders is GPL by extension. Fine for an
open-source self-host project; just be aware if you ever consider
bundling this into proprietary firmware.
