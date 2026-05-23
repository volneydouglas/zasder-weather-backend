# Zasder Weather (backend)

Self-hosted weather-station backend. Pulls data from any combination of
**AmbientWeather cloud**, **Davis WeatherLink cloud**, or direct **433/915 MHz
RF capture** (LilyGO ESP32+SX1276), stores it in SQLite, and exposes a small
HTTP API that a [companion iOS app](https://apps.apple.com/) reads.

Built because [MyAcurite](https://www.acurite.com/) was killed by AcuRite in
2026 and Davis's WeatherLink Console is a paid cloud lock-in. Owning your
own backend means the data is yours, the dashboard is yours, the app keeps
working when vendors change their minds.

If you also want LLM-assisted setup (Claude Code, Cursor, Aider, etc.),
read **[AGENTS.md](AGENTS.md)** — it's the same setup story but written
for an AI agent.

## What you need

Pick **one or more** of these ingest paths. They all coexist — data shows up
as separate device rows in the iOS app.

| Path | Hardware needed | Data quality | Notes |
|---|---|---|---|
| **A. AmbientWeather cloud** | An AmbientWeather-registered station (WS-2000, WS-2902, etc.) | 60s cadence | Easiest if you already have one. Cloud-only — at the mercy of AWN's API. |
| **B. Davis WeatherLink cloud** | Davis Vantage Vue / Pro 2 + WeatherLink Console (any) | 1–5 min cadence (subscription-tier dependent) | Davis VP2 + 6313 Console works only via cloud — the new console doesn't broadcast in the legacy unencrypted protocol. |
| **C. LilyGO 433 MHz** | LilyGO T3 LoRa32 V1.6.1 board (~$25), AcuRite Atlas | ~16s real-time | Captures AcuRite Atlas via RTL433-style decode on ESP32. |
| **D. LilyGO 915 MHz** | Second LilyGO board, Fine Offset / AmbientWeather WS-2000 outdoor + WH32B indoor | ~16s real-time | Captures Fineoffset family (FSK). Merges WH32B indoor data into the outdoor station's tile grid. |

Two deployment modes for the backend itself:

- **Cloud (Fly.io)** — recommended. ~$0–5/month. Single command setup. Custom domain optional.
- **Local Docker** — runs on any Linux/macOS box with Docker. Good for "I want all my data on-premise."

The iOS app is published separately on the App Store and connects to whichever backend URL you give it.

## Quickstart: Fly.io + AmbientWeather (5 minutes)

```sh
# 1. Install Fly CLI
brew install flyctl
fly auth signup     # or `fly auth login`

# 2. Clone + run interactive setup
git clone https://github.com/volneydouglas/zasder-weather-backend.git
cd zasder-weather-backend
./bin/setup-fly.sh  # prompts for app name, region, AWN keys, TZ
                    # outputs an API_TOKEN — save it for the iOS app
```

When `setup-fly.sh` finishes you'll have a live backend at
`https://<app>.fly.dev/`. The status page at `/` proves it's running. Point
the iOS app at that URL + the printed `API_TOKEN` and you're done.

To add Davis cloud or a LilyGO SDR later: see the per-path sections below.

## Quickstart: local Docker

```sh
git clone https://github.com/volneydouglas/zasder-weather-backend.git
cd zasder-weather-backend
cp .env.example .env
$EDITOR .env        # at minimum, fill API_TOKEN + INGEST_TOKEN
docker compose up -d
```

The backend listens on `http://localhost:8080/`. The iOS app needs to reach
it on your LAN — point the app at `http://<your-mac-ip>:8080` and the same
`API_TOKEN` from `.env`.

## Path A — AmbientWeather cloud poller

Add to `.env` (or as Fly secrets):

```sh
AW_APPLICATION_KEY=<from https://ambientweather.net/account>
AW_API_KEY=<same page>
POLL_INTERVAL_SECONDS=60
```

Restart. The backend polls every 60s and stores each station's most recent
reading as a device row.

## Path B — Davis WeatherLink cloud poller

1. Sign in at https://www.weatherlink.com/account
2. **Scroll to the bottom-left** of the Account page — there's a section
   labeled **"API Key v2"** (Davis tucks it below the fold; that's why most
   people can't find it). Click **Generate v2 Key**.
3. Copy the **API Key** and **API Secret** (Secret is shown ONCE).
4. Find your station ID:
   ```sh
   curl -H "X-Api-Secret: <SECRET>" \
     "https://api.weatherlink.com/v2/stations?api-key=<KEY>"
   ```
   `station_id` is in the response.
5. Add to `.env`:
   ```sh
   WEATHERLINK_API_KEY=...
   WEATHERLINK_API_SECRET=...
   WEATHERLINK_STATION_ID=...
   WEATHERLINK_NAME=Davis Vantage Pro2 (Cloud)
   WEATHERLINK_LOCATION=Your City
   # If your ISS was installed mid-year and the cloud's yearly_rainin starts at 0:
   WEATHERLINK_YEARLY_RAIN_BASELINE_IN=0
   ```

Restart. Free WeatherLink tier exposes 5-minute current-conditions; Pro+
gives 1-minute. Both work; adjust `WEATHERLINK_POLL_INTERVAL_SECONDS`
(default 60) to match your tier.

## Paths C + D — LilyGO ESP32 SDR direct

These give real-time RF capture without going through any vendor cloud. One
LilyGO board per band (one for 433 MHz Atlas, one for 915 MHz Fineoffset).

See **[lilygo-relay/README.md](lilygo-relay/README.md)** for hardware, flashing,
provisioning, and field-tested gotchas. Short version:

```sh
brew install platformio
cd lilygo-relay
pio run -e t3_v161_433 -t upload    # for the 433 board
# or
pio run -e t3_v161_915 -t upload    # for the 915 board
```

After flashing, the board comes up as a `ZasderLilyGO` Wi-Fi access point.
Join it from a phone, fill in your home Wi-Fi creds, save. Then point it at
your backend via:
```sh
curl -X POST http://<board-ip>/provision \
  -d "backend_url=https://your-backend.fly.dev" \
  -d "ingest_token=$INGEST_TOKEN"
```
Data starts flowing in within seconds.

## What's in this repo

```
backend/             FastAPI app — pollers, /ingest/custom, /api/*, status page
lilygo-relay/        ESP32+SX1276 firmware (PlatformIO project)
bin/setup-fly.sh     Interactive Fly.io setup (creates app, volume, secrets)
docker-compose.yml   Local-deployment compose file
README.md            (this file — human-oriented)
AGENTS.md            LLM-friendly deployment guide
.env.example         Annotated environment template
```

## API

All `/api/*` routes require `Authorization: Bearer <API_TOKEN>`. iOS app
calls these. Public-readable status page at `/`.

| Method | Path | Notes |
|---|---|---|
| GET | `/` | HTML status page (no auth) |
| GET | `/healthz` | Liveness, no auth |
| GET | `/api/devices` | All devices + latest reading |
| GET | `/api/devices/{mac}/current` | Composite latest-non-null per field |
| GET | `/api/devices/{mac}/history?hours=24` | Time series, auto-bucketed for 3d/7d/30d |
| GET | `/api/devices/{mac}/summary?field=tempf&hours=24` | Min/max/avg/median + when |
| GET | `/api/forecast?lat=&lon=` | 7-day forecast (Open-Meteo) |
| POST | `/ingest/custom` | Source posts a normalized observation. `Authorization: Bearer <INGEST_TOKEN>` |
| POST | `/ingest/discovery` | Source posts a `(model, id)` RF sighting |
| GET | `/api/discoveries?since_hours=24` | Long-tail RF device survey |

## Tests

```sh
pytest -q                    # backend (auto-discovered)
cd lilygo-relay && pio test  # firmware unit tests (small)
```

## License

MIT for backend + setup scripts. `lilygo-relay/` ships under GPL-3.0
because it links against
[rtl_433_ESP](https://github.com/NorthernMan54/rtl_433_ESP) which is GPL.
The GPL is contained to that subdirectory; everything else stays MIT.

## Acknowledgments

- [rtl_433](https://github.com/merbanan/rtl_433) — the canonical RF-decode reference
- [rtl_433_ESP](https://github.com/NorthernMan54/rtl_433_ESP) — ports the decoders to ESP32
- [Open-Meteo](https://open-meteo.com/) — free forecast API
- [Fly.io](https://fly.io/) — backend hosting
