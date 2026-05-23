# davis-relay

Captures Davis Vantage Pro 2 ISS (Integrated Sensor Suite) weather data
off 915 MHz via an RTL-SDR dongle and forwards it to the
[Zasder Weather backend](https://github.com/volneydouglas/zasder-weather-backend).

## Why a separate service from sdr-relay?

Davis ISS uses **frequency-hopping spread spectrum (FHSS)** across 51
channels in the 902-928 MHz band, dwelling ~2.5s per channel. Decoding
it requires constantly retuning the SDR to follow the hop pattern.

`rtl_433` (which sdr-relay uses) does NOT have a Davis decoder
(upstream issues [#970](https://github.com/merbanan/rtl_433/issues/970)
and [#2093](https://github.com/merbanan/rtl_433/issues/2093) — won't
ever be added) and tunes the SDR to one fixed frequency anyway. So
Davis needs its own pipeline.

The right tool is [**rtldavis**](https://github.com/lheijst/rtldavis) —
a Go binary that knows the FHSS hop pattern, retunes the RTL-SDR in
real time, and emits raw decoded 8-byte packets as text. This service
wraps rtldavis: parses the 8-byte payload via `davis_iss.py` (~180
lines, ported from
[weewx-rtldavis](https://github.com/lheijst/weewx-rtldavis)), coalesces
rotating fields across packets, and POSTs to `/ingest/custom`.

## Hardware

- **RTL-SDR dongle** dedicated to this service. Can't be shared with
  rtl_433 or any other process. RTL-SDR Blog V4 with the
  `librtlsdr0 ≥ 2.0` driver (Debian Bookworm package) is known good.
- **915 MHz antenna** (the V4's stock dipole works fine indoors at
  reasonable range; better antenna helps if Davis is far / behind walls).
- **Davis VP2 ISS** with a DIP-switch transmitter ID of 1..8 (the
  default is 1).

## What it decodes

Davis broadcasts 10 packet types in rotation (~2.5s each), each
carrying wind speed + direction always plus ONE rotating field:

| Msg type | Field | Notes |
|----------|-------|-------|
| 0x2 | supercap voltage | ISS battery health |
| 0x4 | UV index | |
| 0x6 | solar radiation | W/m² |
| 0x8 | temperature | °F (digital path) |
| 0x9 | wind gust | mph (10-min peak) |
| 0xA | humidity | % (digital path) |
| 0xE | rain bucket tips | 7-bit rolling counter |

A full set of all rotating fields takes ~25-50s under good FHSS
reception. Our SDR typically gets ~50-80% of packets at our hop window;
the accumulator survives the gaps so per-field staleness is bounded by
the rotation period.

## Configuration

All via env vars (see `.env.example`):

| Var | Default | Notes |
|-----|---------|-------|
| `BACKEND_URL` | — | required |
| `INGEST_TOKEN` | — | required; same as the backend secret |
| `DAVIS_ID` | `1` | transmitter ID (1..8) from ISS DIP switches |
| `DAVIS_NAME` | `Davis Vantage Pro2 (SDR)` | device.name on the backend |
| `DAVIS_LOCATION` | `""` | shown in the iOS app under the device |
| `DAVIS_RAIN_BUCKET_IN` | `0.01` | inches per tip; metric VP2 = 0.00787 |
| `DAVIS_RAIN_YEARLY_BASELINE_IN` | `0` | inches at deploy time |
| `DAVIS_WIND_DIR_OFFSET` | `0` | degrees calibration offset |
| `LOG_LEVEL` | `INFO` | set to `DEBUG` to see hop events |

## Synthetic MAC

Davis sensors don't have MACs, so we synthesise one with the same
scheme sdr-relay uses (`5D:5D:TT:HH:HH:HH`):

- `5D:5D` — locally-administered prefix
- `05` — Davis type tag (next available after 0x04 = LaCrosse)
- last 3 bytes — DAVIS_ID

So `DAVIS_ID=1` lands as MAC `5D:5D:05:00:00:01` on the backend.

## Running

```sh
cp .env.example .env
$EDITOR .env                          # fill in BACKEND_URL, INGEST_TOKEN, DAVIS_ID

docker compose up -d --build
docker logs -f davis-relay
```

You should see within a minute or two:
```
INFO davis-relay: starting: rtldavis -tf US -tr 1
INFO davis-relay: posting to https://... every 30s
INFO davis-relay: first Davis packet decoded: type=0x8 fields=['transmitter_id','battery_low','wind_speed_mph','wind_dir_deg','temp_f']
INFO davis-relay: posted: tempf=72.3 hum=42 wind=3.0@180
```

Verify the device row appeared on the backend:
```sh
curl -H "Authorization: Bearer $API_TOKEN" $BACKEND_URL/api/devices | grep 5D:5D:05
```

## Limitations (v1)

- **No EU / NZ frequency plan** — `-tf US` hardcoded. Easy to expose
  via env if needed.
- **Soil moisture, leaf wetness, extra temp/humidity stations** — not
  decoded. Standard VP2 ISS doesn't have them; if your station does,
  parse those bytes in `davis_iss.py`.
- **Wind direction calibration** — single linear offset; for non-linear
  sensor drift, edit `davis_iss.parse()` or the offset table at
  install time.
- **No retransmit / reliability counters** — we report whatever
  rtldavis decoded successfully; lost packets are silent.

## License

GPL-3.0. `davis_iss.py` is derived from `lheijst/weewx-rtldavis`
(GPL-3.0), and the underlying rtldavis binary is also GPL-3.0.
