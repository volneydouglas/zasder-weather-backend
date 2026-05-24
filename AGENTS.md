# AGENTS.md — Deployment guide for LLM coding agents

This file is for an AI coding agent (Claude Code, Cursor, Aider, Continue,
etc.) helping a user deploy and operate the Zasder Weather backend +
companion iOS app. Humans should read `README.md` instead; this file
optimizes for an agent's workflow (concrete commands, decision trees,
known pitfalls).

If you are an agent, before doing anything else:

1. Read this entire file.
2. Read `README.md` for the human-oriented overview.
3. Ask the user **which ingest paths they want** and **where they want
   the backend** — don't guess. The answers determine 80% of the
   subsequent commands.

## Glossary

- **Backend** — FastAPI app in `app/`. Stores observations in
  SQLite, exposes `/api/*` (auth via `API_TOKEN`) for the iOS app, and
  `/ingest/custom` (auth via `INGEST_TOKEN`) for receivers.
- **Receiver / source** — anything that POSTs observations to
  `/ingest/custom`. AWN poller (built into backend), WeatherLink poller
  (built into backend), or a LilyGO board on the LAN.
- **MAC** — synthetic 6-byte identifier `5D:5D:TT:HH:HH:HH` where TT is
  a sensor-type tag (01=Atlas, 02=Fineoffset outdoor, 05=Davis) and
  HHHHHH is the low 3 bytes of the sensor's RF ID. Same physical
  sensor always lands on the same MAC across multiple receivers.
- **Composite latest** — `/api/devices/{mac}/current` returns the
  freshest non-null value per field across the last 5 min of obs, so
  multi-source devices show full tile grids in the iOS app.

## Repo layout

```
app/                         FastAPI source (the Python package)
  main.py                    Lifespan, security middleware, routes
  config.py                  Pydantic Settings — reads env vars
  db.py                      aiosqlite — schema, latest_observation, history
  ingest.py                  /ingest/custom + flatten()
  poller.py                  AmbientWeather cloud poller
  weatherlink_*.py           Davis WeatherLink cloud poller
  discovery.py               /ingest/discovery + /api/discoveries
  capture.py                 Optional raw-POST capture for debugging
  static/                    Status page HTML
tests/                       pytest suite — run with `pytest -q`
Dockerfile                   python:3.12-slim → uvicorn
fly.toml                     Fly.io app + volume + secrets configuration
requirements.txt             Runtime deps (FastAPI, httpx, aiosqlite, pydantic)
requirements-dev.txt         Test deps (pytest, testclient, anyio)
pytest.ini

lilygo-relay/                ESP32 firmware (PlatformIO)
  src/
    main.cpp                 Setup, WiFi, rtl_433_ESP callback, loop
    zasder_post.cpp          rtl_433 JSON → /ingest/custom shape + HTTP POST
    config_server.cpp        LAN HTTP server (/status, /provision, /reset)
    display.cpp              OLED renderer (defensive — no-op if not detected)
    root_ca.h                ISRG Root X1 pinned CA (Let's Encrypt anchor)
  platformio.ini             Two envs: t3_v161_433 + t3_v161_915
  README.md                  Hardware + flashing + provisioning + security

bin/setup-fly.sh             Interactive Fly setup (create | update | --rotate-tokens | --print-tokens)
docker-compose.yml           Backend-only compose for local deploy
.env.example                 Annotated environment template
README.md                    Human-facing setup guide
AGENTS.md                    This file
```

## Decision tree

```
Q: What hardware does the user have?
├─ Only an AmbientWeather-connected station
│   → Use Path A (AWN cloud poller).
│
├─ Davis Vantage Vue or Pro 2 + WeatherLink Console (any model)
│   → Use Path B (WeatherLink cloud poller).
│   → If they have the older Vantage Vue console (not 6313), they can
│     ADDITIONALLY use rtldavis SDR for sub-second data — but that
│     setup is NOT in the public repo. Recommend cloud.
│
├─ AcuRite Atlas
│   → Use Path C (LilyGO 433 MHz SDR). Need 1× LilyGO T3 LoRa32 V1.6.1.
│
├─ AmbientWeather WS-2000 / WS-2902 / Ecowitt / Fineoffset family
│   → Use Path D (LilyGO 915 MHz SDR). Need 1× LilyGO T3 LoRa32 V1.6.1.
│   → If WH32B indoor sensor: same LilyGO covers it via merge into outdoor.
│
└─ Multiple sensors / mix
    → Any combination of A+B+C+D works. They all post into the same
      backend and show up as separate device rows in the iOS app.

Q: Where do they want the backend?
├─ Hosted (cloud)
│   → Fly.io. ~$0–5/month. Public custom domain optional.
│   → ./bin/setup-fly.sh handles app creation, volume, secrets.
│
└─ Local (LAN-only)
    → docker compose up -d on any always-on Linux/macOS box.
    → iOS app connects to http://<host-ip>:8080 over LAN only.
```

## Required environment variables

| Var | Required? | Notes |
|---|---|---|
| `API_TOKEN` | YES | iOS app uses this. `openssl rand -hex 32`. |
| `INGEST_TOKEN` | YES if any LilyGO/receiver | Same source POSTs use this. |
| `DATABASE_PATH` | YES | `/data/weather.db` on Fly; `./data/weather.db` locally. |
| `AW_APPLICATION_KEY` + `AW_API_KEY` | Path A only | Both unset = AWN poller disabled silently. |
| `WEATHERLINK_API_KEY` + `_SECRET` + `_STATION_ID` | Path B only | All three required together. |
| `REVIEWER_API_TOKEN` | Optional | Secondary token for App Store reviewer. |
| `TIMEZONE` | Optional | IANA zone (e.g. `America/Phoenix`). Defaults UTC. |
| `WEATHERLINK_POLL_INTERVAL_SECONDS` | Optional | Default 60. Min 15. |
| `WEATHERLINK_YEARLY_RAIN_BASELINE_IN` | Optional | Inches to add to Davis's reported yearly rain (mid-year install). |
| `SHARED_BAROMETER_SOURCE_MAC` | Optional | For cross-device pressure tile fallback. |
| `ALLOWED_HOSTS` | Recommended in prod | Comma-separated allow-list for Host header. Defaults `*`. |
| `DEBUG` | Never set in prod | `1` re-enables `/docs` (off by default). |

`.env.example` has full annotations. Read it.

## Fly.io deployment (Path: hosted)

```sh
# Pre-flight
brew install flyctl                                  # macOS — adjust per OS
fly auth signup     # or `fly auth login`
fly status                                           # confirms you're authed

# Setup (interactive)
./bin/setup-fly.sh
# Prompts for: app name, region, AWN keys (optional), timezone.
# Generates and prints API_TOKEN + INGEST_TOKEN — capture these in
# your secrets manager. They're set as Fly secrets automatically.

# Deploy
fly deploy

# Add WeatherLink (Path B) later
fly secrets set -a <app-name> \
  WEATHERLINK_API_KEY=... \
  WEATHERLINK_API_SECRET=... \
  WEATHERLINK_STATION_ID=...
# Setting a secret auto-restarts the machine.

# Read a secret value (digests only show in `fly secrets list`)
fly ssh console -a <app-name> -C 'printenv WEATHERLINK_API_KEY'
```

Verify: visit `https://<app>.fly.dev/` — should show the status page.

## Local Docker deployment (Path: LAN)

```sh
cp .env.example .env
$EDITOR .env                                         # fill required vars
docker compose up -d
docker compose logs -f
```

Backend listens on `http://localhost:8080/`. iOS app needs `http://<host-lan-ip>:8080`.

## LilyGO flashing — full workflow

The two parts that confuse users: **PlatformIO setup** and **provisioning**.

### Install PlatformIO

```sh
brew install platformio                              # macOS
# or
pip install platformio                               # other OSes
pio --version                                        # confirm
```

### Identify the board

```sh
ls /dev/cu.usbserial-*                               # macOS
ls /dev/ttyUSB*                                      # Linux
```

The user should see something like `/dev/cu.usbserial-591F0011341` per
board. **Each LilyGO has a unique serial; remember which port maps to
which physical board** (label them with tape if needed). If two boards
are plugged in, you'll see two paths.

### Flash 433 board

```sh
cd lilygo-relay
pio run -e t3_v161_433 -t upload --upload-port /dev/cu.usbserial-<id>
```

First build pulls ~600 MB of toolchain + libs (~5–10 min). Subsequent
builds are <30 sec. Upload at 115200 baud takes ~85 sec per flash.

### Flash 915 board

Same as 433 but `pio run -e t3_v161_915 -t upload …`. Difference is the
PlatformIO env: 915 sets `OOK_MODULATION=false` (Fineoffset is FSK)
and a different `RF_MODULE_FREQUENCY` + source tag.

### Provisioning (after first boot)

1. On the user's phone, join Wi-Fi network **`ZasderLilyGO`** (open,
   no password). Captive portal opens → enter home Wi-Fi creds → Save.
2. Board reboots and joins home Wi-Fi. Find its IP from your router or
   serial monitor.
3. From any device on the LAN:
   ```sh
   curl -X POST http://<board-ip>/provision \
     --data-urlencode "backend_url=https://your-backend.example.com" \
     --data-urlencode "ingest_token=$INGEST_TOKEN"
   ```
4. Verify within 30 sec:
   ```sh
   curl http://<board-ip>/status              # should show has_token: true, pkts_posted_ok > 0
   ```

The board also exposes mDNS as `zasder-lilygo-XXXX.local` (XXXX = last
2 bytes of MAC).

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `pio run` errors `Not a PlatformIO project` | Wrong cwd | `cd lilygo-relay` before running `pio` |
| Flash fails mid-write, "stream stopped" | CP2104 corrupts at >115200 baud | Already pinned to 115200 in `platformio.ini` — if you see this, retry; the auto-reset can flake. Power-cycle board if persistent. |
| Board boots into AP mode every time | Empty `ingest_token` in NVS triggers `wm.startConfigPortal()` instead of `autoConnect` | This is **expected** until you POST to `/provision` (step 3 above). |
| `/api/devices/{mac}/current` returns all nulls | Multiple sources posting partial obs to same MAC | Composite-latest auto-fixes within ~5 min. If still null after 10 min, check `/api/devices` for any source posting at all. |
| Davis cloud poller silent | Missing one of the three required secrets | Backend logs `WeatherLink not configured` when any of `_KEY/_SECRET/_STATION_ID` is unset. All three needed. |
| `WeatherLink poll failed: HTTPError` | Wrong key/secret OR rate-limited | Free tier = 1000 req/hr. Default poll = 60s = 60 req/hr — comfortable. Verify keys with `curl -H "X-Api-Secret: ..." "https://api.weatherlink.com/v2/stations?api-key=..."` |
| iOS app shows "No data" but backend is healthy | Wrong `API_TOKEN` OR wrong backend URL in app settings | Both must match exactly. Try `curl -H "Authorization: Bearer $API_TOKEN" https://<backend>/api/devices` from your terminal to isolate. |
| Atlas card missing UV/lux | Atlas hardware quirk — UV/lux photodiode commonly dies and reports a stuck value | Set `ATLAS_UV_LUX_BROKEN=1` env on the LilyGO 433 to mask both fields. Hardware-level — not a software bug. |
| Davis "RAIN TODAY" shows large number | `WEATHERLINK_YEARLY_RAIN_BASELINE_IN` set but rain rollup is computing daily from yearly delta | Davis cloud reports daily-rain directly; we set it explicitly to bypass the rollup. Confirm latest backend code; if you have stale Davis observations with `yearlyrainin=0` and current has the baseline, the rollup math breaks. Fix: delete pre-baseline rows OR re-baseline. |

## Tests

```sh
pytest -q                       # backend, all of /tests
cd lilygo-relay && pio test     # firmware (small unit tests)
```

CI: `.github/workflows/ci.yml` runs backend tests on every push/PR.

## What's intentionally NOT in this repo

If the user asks for them, explain they're not part of the public template:

- **iOS app source** — stays private until App Store ship; closed-source.
- **Pi `sdr-relay`** (rtl_433 wrapper) — superseded by lilygo-relay for new users.
- **`davis-relay`** (rtldavis SDR) — doesn't work with the 2023+ Davis 6313
  console; users with older Vantage Vue consoles can find rtldavis online directly.
- **Legacy DNS-hijack AcuRite relay** — AcuRite cloud retired in 2026.
- **Water-meter integration** — maintainer's private side project.

## Patterns to follow when modifying code

- **Backend**: add new poller modules under `app/`, register in
  `main.py` lifespan with `if settings.x_configured` gating, write tests
  in `tests/`. Use existing `httpx.AsyncClient`, `aiosqlite`,
  `ingest._do_ingest()` for POST-shape ingest.
- **LilyGO firmware**: stay within `lilygo-relay/src/`. New protocols
  go in `modelTypeTag()` (zasder_post.cpp) and the corresponding
  decoder. The `WH32B` cache+merge pattern is a good template for
  paired sensors.
- **Field names**: backend `_flatten()` expects `wind.speed_mph`,
  `wind.gust_mph`, `wind.direction`, `outdoor.solar_wm2`,
  `pressure.relative_inhg`. NOT iOS-style `windspeedmph`. Mis-naming
  silently drops fields.

## When you're stuck

- Check `fly logs` (Fly) or `docker compose logs` (local).
- Check `curl http://<board>/status` (LilyGO).
- Check `/healthz` returns 200.
- Hit `/api/devices` with the right `API_TOKEN` to confirm devices exist.
- If multiple sources post to the same MAC, the composite-latest can
  hide source-specific issues — query the raw observations table via
  `fly ssh console` if needed.
