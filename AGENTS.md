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
  a sensor-type tag (01=Atlas, 02=Fineoffset outdoor, 05=Davis,
  06=Tempest, 07=AirGradient, 08=Govee) and
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
  insights.py                Statistics rollups + /api/insights (default on since 1.9)
  stories.py                 Story cards: the producers behind /api/devices/{mac}/stories
  almanac.py                 Sun, moon and season math the sky stories read
  wu_import.py               Weather Underground history import (/api/import/wu)
  wu_upload.py               Live WU forwarding — re-posts ingested readings
                             to wunderground.com per device (app-managed via
                             PUT /api/devices/{mac}/wu-station)
  forecast_twc.py            TWC forecast source (needs a WU key)
  config_backup.py           /api/config/backup + /api/config/restore
  source_status.py           Per-ingest-source health for /api/sources
  ecowitt.py                 /ingest/ecowitt — Ecowitt "Customized" upload (Path G)
  ecowitt_cloud_*.py         Ecowitt cloud poller, api.ecowitt.net v3 (Path I);
                             reuses ecowitt.py's rain/battery/channel rules
  tempest_*.py               WeatherFlow Tempest cloud poller (Path F)
  integrations.py            App-managed cloud credentials (/api/integrations)
  alerts.py                  Alert monitor — device-down, thresholds, smart
                             alerts, storm summaries, morning report digest
  storm.py                   Storm episode tracking + summary messages
  climate.py                 /api/devices/{mac}/climate + NOAA report (rides the rollups)
  normals.py                 NOAA/NCEI 1991-2020 climate normals (US only)
  maintenance.py             History aging — thinning + JSON trimming
  self_update.py / updates.py  One-tap + automatic updates, release check
  airgradient_*.py           AirGradient air-quality integration (cloud + LAN)
  static/                    Status page HTML
tests/                       pytest suite — run with `pytest -q`
Dockerfile                   python:3.12-slim → uvicorn
fly.toml                     Fly.io app + volume + secrets configuration
requirements.txt             Runtime deps (FastAPI, httpx, aiosqlite, pydantic)
requirements-dev.txt         Test deps (pytest, testclient, anyio)
pytest.ini

wll-poller/                  Path E — Davis WeatherLink Live LAN poller
                             (pure-stdlib Python; docker compose or systemd
                             on a LAN host; posts to /ingest/custom)

weewx-bridge/                Path H — WeeWX extension POSTing archive
                             records to /ingest/custom (weectl install)

mcp/                         Read-only MCP server over the token-gated API

lilygo-relay/                ESP32 firmware (PlatformIO)
  src/
    main.cpp                 Setup, WiFi, rtl_433_ESP callback, loop
    zasder_post.cpp          rtl_433 JSON → /ingest/custom shape + HTTP POST
    config_server.cpp        LAN HTTP server (/status, /provision, /reset)
    display.cpp              OLED renderer (defensive — no-op if not detected)
    root_ca.h                ISRG Root X1 pinned CA (Let's Encrypt anchor)
  platformio.ini             Two envs: t3_v161_433 + t3_v161_915
  README.md                  Hardware + flashing + provisioning + security

bin/setup-fly.sh             Path-based Fly setup (asks sources first; create | update | --rotate-tokens | --print-tokens; non-interactive via --sources= --tz= --yes)
bin/setup-local.sh           Guided local Docker setup (generates tokens, writes .env, docker compose up)
bin/doctor.sh                Health checklist (fly auth, /healthz, both tokens, volume, pollers, recent data)
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
│   → If they ALSO have a WeatherLink Live (6100) gateway on the LAN:
│     prefer Path E (wll-poller) — ~2s freshness vs 60s cloud, no API
│     key needed. See wll-poller/README.md. Needs a small always-on
│     LAN host (Pi/NAS) since the backend can't reach the LAN.
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
├─ WeatherFlow Tempest
│   → Use Path F (Tempest cloud poller). Free personal token from
│     tempestwx.com; TEMPEST_TOKEN + TEMPEST_STATION_ID, or configure
│     from the app (Settings → Integrations).
│
├─ Ecowitt gateway/console (GW1000–GW3000, HP2551-class) on the LAN
│   → Use Path G: point the gateway's "Customized" upload at
│     /ingest/ecowitt?token=<INGEST_TOKEN>. The gateway is HTTP-only —
│     works directly against local Docker; a Fly backend needs a small
│     TLS-terminating forwarder on the LAN (README has a Caddyfile).
│     No extra hardware, no vendor cloud.
│   → Backend is HTTPS-only (Fly) and no forwarder is wanted: Path I,
│     the Ecowitt cloud poller. ECOWITT_APP_KEY + ECOWITT_API_KEY from
│     the ecowitt.net profile, or configure from the app (Settings →
│     Integrations → Ecowitt Cloud). One path per gateway, never both.
│
├─ Govee CO₂ / air-quality monitor (H5140 family)
│   → Path J: GOVEE_API_KEY from the Govee Home app, or Settings →
│     Integrations → Govee. Cloud-only; the device has no local API.
│
├─ Station already running under WeeWX (any of its 70+ families)
│   → Use Path H: `weectl extension install` the weewx-bridge/
│     extension, set server_url + ingest_token under
│     [StdRESTful][[Zasder]], restart WeeWX. See weewx-bridge/README.md.
│
└─ Multiple sensors / mix
    → Any combination of A+B+C+D+E+F+G+H+I works. They all post into the
      same backend and show up as separate device rows in the iOS app.

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
| `TEMPEST_TOKEN` + `TEMPEST_STATION_ID` | Path F only | Both required together; `TEMPEST_NAME`/`TEMPEST_POLL_INTERVAL_SECONDS` optional. App-stored Integrations values win over env. |
| `REVIEWER_API_TOKEN` | Optional | Secondary token for App Store reviewer. |
| `GUEST_API_TOKENS` | Optional | Comma-separated read-only tokens (family sharing): reads only, PII stripped. Each ≥32 chars, distinct from privileged tokens, placeholders rejected at boot. The app can also mint/revoke these per person — including write-tier share links (1.9, app-minted only; audited at `GET /api/write-audit`). |
| `HISTORY_DETAIL_DAYS` + `HISTORY_KEEP_INTERVAL_MINUTES` | Optional | Opt-in history thinning past N days (needs the insights rollups, on by default; rollups keep daily extremes). App-stored retention (`PUT /api/history-retention`) wins over env. |
| `HISTORY_JSON_DETAIL_DAYS` | Optional | Drop each old row's raw JSON payload (most of its bytes) while keeping every row's typed columns. Non-destructive half of history aging. |
| `WATER_YEAR_START_MONTH` | Optional | Water-year start for `/api/devices/{mac}/climate`. Default 10 (October); 1 = calendar year. |
| `AIRGRADIENT_LOCAL_HOSTS` | Optional | Comma-separated AirGradient monitor hosts/IPs for cloud-free LAN polling (local Docker installs). Cloud-token integration is app-managed instead. |
| `AUTO_UPDATE` + `FLY_API_TOKEN` | Optional | Self-updating Fly instance (app-scoped deploy token, `FlyV1` macaroon or `Bearer` both accepted). Never crosses a major version. |
| `PUBLIC_DASHBOARD` (+ `_MACS`, `_FIELDS`) | Optional | `1` replaces the status page screenshots with a live server-rendered dashboard; `/embed` serves it frameable for iframes. |
| `SMART_ALERTS` | Optional | `1` enables threshold-free weather-intelligent alerts (frost, dangerous heat, pressure drop, temp drops, wind ramps, gust fronts). |
| `STORM_SUMMARY` (+ `_QUIET_MINUTES`, `_MIN_TOTAL_IN`) | Optional | One report after the rain stops. Default on (0.05 in floor, 30 min quiet); app-saved values win over env. |
| `TIMEZONE` | Optional | IANA zone (e.g. `America/Phoenix`). Defaults UTC. |
| `WEATHERLINK_POLL_INTERVAL_SECONDS` | Optional | Default 60. Min 15. |
| `WEATHERLINK_YEARLY_RAIN_BASELINE_IN` | Optional | Inches to add to Davis's reported yearly rain (mid-year install). |
| `SHARED_BAROMETER_SOURCE_MAC` | Optional | For cross-device pressure tile fallback. |
| `WU_API_KEY` | Optional | Weather Underground PWS-owner key (free for uploading stations). Powers the TWC forecast source + the WU history import (`/api/import/wu`). An app-stored key (`PUT /api/config/wu-key`) takes precedence. |
| `INSIGHTS` | Optional | Server-side statistics rollups + `GET /api/insights`. ON by default since 1.9 (climate endpoints and history thinning ride the rollups); a needed backfill self-schedules on boot. Set `0` to disable (endpoints 404). |
| `INGEST_MAX_RAIN_RATE_IN_PER_HR` | Optional | Rain-glitch guard on `/ingest/custom`: drop a reading whose cumulative rain jumps faster than this (default 2.0 in/hr; 0 disables). |
| `INGEST_GUST_MAX_FACTOR` + `INGEST_GUST_MIN_MPH` | Optional | Gust-glitch guard on `/ingest/custom`: null a gust above `_MIN_MPH` (default 30) that exceeds `_MAX_FACTOR` × sustained wind (default 4.0; 0 disables). |
| `INGEST_MIN_INTERVAL_SECONDS` | Optional | History-write throttle for high-cadence sources; readings within N s of the last stored row skip history (live view unaffected; new-field posts always stored). Default 0 = store everything. |
| `INGEST_PLAUSIBILITY_BANDS` | Optional | Physical plausibility bands on `/ingest/custom`: values beyond world-record extremes (bit-flip temps, negative rain, 3000 mph gusts) are nulled field-by-field before reaching records/rollups/alerts. Default on; `false` stores readings as posted. |
| `INGEST_MAX_TEMP_JUMP_F` | Optional | Temperature-jump guard on `/ingest/custom`: a reading further than this from the device's last stored temperature (plus 60 °F per elapsed hour) is dropped; a persistent new level is accepted on the second sighting. Default 40; 0 disables. |
| `STATION_ELEVATION_FT` | Optional | With `PRESSURE_ABSOLUTE_MACS`: station elevation for sea-level pressure correction of sensors that report ABSOLUTE pressure (e.g. a WH32B over SDR). Default 0 = off. |
| `PRESSURE_ABSOLUTE_MACS` | Optional | Comma-separated MACs whose posted pressure is absolute station pressure; corrected to sea-level via `STATION_ELEVATION_FT` (true absolute kept in `baromabsin`). |
| `ALERT_EMAIL_TO` + `SMTP_HOST` | Optional | Both set = device-down email alerts on. SMTP_USERNAME/PASSWORD/PORT/SSL for transport (Gmail App Password works). |
| `ALERT_STALE_MINUTES` (+ `_BY_MAC`) | Optional | Minutes offline before alerting; per-MAC override map, `0` disables a device. Default 15. |
| `ALLOWED_HOSTS` | Recommended in prod | Comma-separated allow-list for Host header. Defaults `*`. |
| `DEBUG` | Never set in prod | `1` re-enables `/docs` (off by default). |

`.env.example` has full annotations. Read it.

## Fly.io deployment (Path: hosted)

```sh
# Pre-flight
brew install flyctl                                  # macOS — adjust per OS
fly auth signup     # or `fly auth login`
fly status                                           # confirms you're authed

# Setup (path-based, interactive)
./bin/setup-fly.sh
# Asks WHICH SOURCES first (AmbientWeather / Davis / LilyGO), then only
# prompts for what those need (app name, region, the chosen sources'
# credentials, timezone). Generates API_TOKEN + INGEST_TOKEN, sets them
# as Fly secrets, deploys, and writes the next steps (incl. LilyGO
# provision commands) to zasder-install-summary.txt.
#
# Non-interactive (e.g. driven by the web planner):
./bin/setup-fly.sh --sources=awn,davis,lilygo --tz=America/Phoenix --yes

# Health checklist after deploy
./bin/doctor.sh --app <app-name>

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

1. On the user's phone, join Wi-Fi network **`ZasderLilyGO`** (WPA2,
   password `zasder-setup`). Captive portal opens → enter home Wi-Fi
   creds → Save.
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
