# sdr-relay

Captures weather sensor data directly off the 433 MHz and 915 MHz ISM bands
with two RTL-SDR Blog V4 dongles and forwards it to the
[Zasder Weather backend](https://github.com/volneydouglas/zasder-weather-backend).
Bypasses any vendor cloud — sensor → RF → SDR → backend.

Replaces / complements the [acurite-relay](../relay/) DNS-hijack approach,
which depended on the AcuRite hub still uploading to
`atlasapi.myacurite.com`. The SDR path is independent of the hub and
survives any future cloud shutdown.

## What it captures

- **AcuRite Atlas** (433 MHz): temperature, humidity, wind, rain, UV, light.
  Decoded by rtl_433's built-in `Acurite-Atlas` decoder (`-R 40`). The Atlas
  cycles through 8 message types per upload pass, each carrying a subset of
  fields, so this service coalesces per-sensor state and posts a merged
  observation every ~60s.
- **Fine Offset / WS-2000 family** (915 MHz): the outdoor 6-in-1 array used
  by AmbientWeather WS-2000, WS-2902, and Ecowitt stations
  (`Fineoffset-WH24`, `-WH65B`, or `-WS80` packets). Each packet is a
  complete observation; posted as-is.
- **Optional indoor pairing**: if a `Fineoffset-WH32B` indoor sensor is
  configured, its temp/humidity/pressure are merged into the outdoor
  observation's `indoor` block.
- **Neptune R900 water meters** (915 MHz): captured to
  `/data/meters/<id>.jsonl` on the LAN host for every R900 sighting
  (local-only). Optionally forwarded to the backend's `/ingest/meter`
  endpoint for the IDs listed in `WATER_METER_IDS`. The companion
  [water-meter-watch](../water-meter-watch/) project reads the local
  JSONL files via a shared volume mount and serves a dashboard for
  inspection at `http://<lan-ip>:8080/`.

The 915 MHz dongle also hears stray traffic from neighbours' AcuRite,
LaCrosse, and ERT meters; those packets are quietly ignored.

## Hardware

- Raspberry Pi 4 (4 GB) or anything similar running Debian 12+
- 2× **[RTL-SDR Blog V4](https://www.rtl-sdr.com/v4/)** with antennas:
  one configured for 433 MHz, one for 915 MHz (the V4 ships with a
  dipole kit that covers both)
- **Powered USB hub** (recommended — V4 has a TCXO + LDO that benefit
  from clean power; bus-powered runs work but can drop frames during
  RPi USB power dips)

### Why marked-serial dongles?

USB enumeration order isn't stable across reboots, so addressing dongles
by `-d 0` or `-d 1` is fragile. Write a friendly serial to each EEPROM
once, then both `rtl_433` and this service can target by serial — no more
"which one is which?" guessing.

```sh
# Plug ONLY the 433 dongle in first
sudo rtl_eeprom -d 0 -s acurite433

# Unplug, replug, then plug in the second dongle
sudo rtl_eeprom -d :00000001 -s ws2000     # -d :SERIAL targets the
                                            # one still on factory default

# Replug. Confirm:
rtl_test
# Found 2 device(s):
#   0:  RTLSDRBlog, Blog V4, SN: acurite433
#   1:  RTLSDRBlog, Blog V4, SN: ws2000
```

(Note: the Debian-packaged `rtl_test` / `rtl_sdr` use `-d N` index syntax;
the `-d :serial` syntax is supported by `rtl_eeprom`, `rtl_433`, and
`rtldavis`. This service uses `serial=NAME` form for `rtl_433`.)

### Antenna placement

- 433 MHz dongle gets the 433 MHz antenna (longer dipole element).
  Vertical orientation, line-of-sight to your outdoor Atlas if possible.
- 915 MHz dongle gets the 915 MHz antenna (shorter dipole). Same idea.
- Plug both into the **powered USB hub**, not direct to the Pi —
  USB 3.0 ports on the Pi emit RF noise around 433 MHz that degrades
  reception. The hub plugged into a USB 2.0 (black) port is best.

## Host setup

The kernel's DVB-TV driver will grab any RTL-SDR dongle it sees and block
userspace tools. Blacklist it on the host:

```sh
sudo tee /etc/modprobe.d/blacklist-rtl-sdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2832_sdr
blacklist rtl2830
EOF
sudo update-initramfs -u   # optional but recommended on Pi OS
sudo reboot
```

After reboot, `lsmod | grep dvb` should be empty.

## Install & run

```sh
cp .env.example .env
# Fill in BACKEND_URL, INGEST_TOKEN, and any sensor IDs you've identified.
# If you don't know your sensor IDs yet, leave them at 0 and use rtl_433
# directly on the host to discover them first (see "Finding sensor IDs"
# below). Then re-edit .env.

docker compose up -d
docker logs -f sdr-relay
```

Within 30–60 seconds you should see lines like:

```
INFO sdr-relay: posted AcuRite Atlas (SDR) (acurite-atlas-sdr)
INFO sdr-relay: posted WS-2000 (SDR) (fineoffset-wh24-sdr)
INFO sdr-relay: meter 1583287502: 257328
```

Verify on the backend:

```sh
curl -H "Authorization: Bearer $API_TOKEN" $BACKEND_URL/api/devices
curl -H "Authorization: Bearer $API_TOKEN" $BACKEND_URL/api/meters
```

## Finding sensor IDs

Before configuring `.env`, run `rtl_433` directly to discover which
devices are in range:

```sh
# AcuRite Atlas (433 MHz)
rtl_433 -d "serial=acurite433" -R 40 -F json
# → look for "id": <some int>. That's your ATLAS_ID.

# Fine Offset outdoor + indoor (915 MHz)
rtl_433 -d "serial=ws2000" -f 915M -F json
# → look for Fineoffset-WH24/WH65B/WS80 (outdoor, set WH24_ID)
# → look for Fineoffset-WH32B (indoor, set WH32B_ID)
# → look for Neptune-R900 (water meters — note the ids for filtering later)
```

Ctrl-C after you've identified each. The IDs are stable per physical
sensor for years — you only need to do this once.

## Synthetic MAC scheme

Backend devices are keyed by MAC. SDR sensors don't have MACs, so this
service synthesises one per sensor using a deterministic format:

```
5D:5D:TT:HH:HH:HH
       ^  ^─ low 3 bytes of the RF sensor id
       └──── sensor type tag (01=Atlas, 02=Fine Offset outdoor,
                              03=Fine Offset indoor, 09=R900)
```

The `5D:5D` prefix is in the locally-administered MAC range (won't
collide with real hardware) and mnemonic for "SDR". You'll see e.g.
`5D:5D:01:00:02:C7` for an Atlas with sensor id 711.

## Coexisting with the network-level relay

You can run this service AND the legacy [acurite-relay](../relay/) at
the same time — they post to different device rows (the network relay
uses the AcuRite hub's real MAC; SDR uses the synthetic 5D:5D:01:...
MAC). Useful during the SDR rollout: keep both running for a few days,
compare data quality and reliability, then shut down the network relay
once you're confident.

When you do retire the network relay, the synthetic MAC means the SDR
device row is brand new — historical hub data stays attached to the old
MAC. If you'd prefer one continuous history, you can set the device's
MAC via env vars to match the hub's MAC instead of using the synthetic
scheme (would require a small code change).

## Tests

```sh
pip install pytest
pytest -q tests/
```

The tests cover the parsing/normalization logic; they don't exercise
rtl_433 itself (no SDR required to run them).

## Troubleshooting

- **`rtl_433 binary not found`** — install with `sudo apt install rtl-433`
  on the host, or run via Docker (the image bundles it).
- **`usb_open error -3` / `LIBUSB_ERROR_ACCESS`** — kernel DVB driver
  grabbed the device. Run the blacklist + reboot steps above. As a
  last-resort runtime fix: `sudo rmmod dvb_usb_rtl28xxu rtl2832_sdr rtl2832`.
- **No frames captured** — let it run for at least 2 minutes; some
  sensors only transmit every 60+ seconds. Atlas sends every ~18s but
  startup lock-on can take 10–20s. Try moving the antenna away from
  metal surfaces and USB 3.0 cables (they emit RF noise around 433 MHz).
- **Hearing neighbours' devices** — completely normal at 915 MHz, where
  utility meters and many weather stations broadcast openly. The router
  in this service filters by configured sensor ID; unmatched packets
  are silently dropped.
- **Backend rejects posts with 400** — usually means the `device.id`
  or `timestamp_utc` is malformed. Check the backend's logs:
  `fly logs -a <app>` or `docker logs zasder-backend`.

## License

Same as the parent project (MIT).
