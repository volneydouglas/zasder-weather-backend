# Zasder Weather (backend)

Self-hosted weather-station backend. Pulls data from any combination of
**AmbientWeather cloud**, **Davis WeatherLink cloud**, or direct **433/915 MHz
RF capture** (LilyGO ESP32+SX1276), stores it in SQLite, and exposes a small
HTTP API that a [companion iOS app](https://zasder.com/weather) reads.

Built because [MyAcurite](https://www.acurite.com/) was killed by AcuRite in
2026 and Davis's WeatherLink Console is a paid cloud lock-in. Owning your
own backend means the data is yours, the dashboard is yours, the app keeps
working when vendors change their minds.

**Not sure what you need?** The
**[install planner](https://zasder.com/weather-helper)** asks what hardware
you have and what you want, then prints a tailored, difficulty-tagged
checklist — which LilyGO board(s) to buy, the exact commands to run, and a
ready-to-paste `setup-fly.sh` one-liner. It runs entirely in your browser
(no login, nothing stored on a server).

If you want LLM-assisted setup (Claude Code, Cursor, Aider, etc.), read
**[AGENTS.md](AGENTS.md)** — the same setup story written for an AI agent.

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

- **Cloud (Fly.io)** — recommended, and the only *supported* way to reach
  your data off your LAN. ~$2/month, works anywhere you have internet.
- **Local Docker** — runs on any always-on Linux/macOS box. LAN-only:
  exposing it to the internet (port-forward, tunnel, reverse proxy) is on
  you. Good for "I want all my data on-premise."

The iOS app ([app page](https://zasder.com/weather)) is distributed separately and connects to whichever backend URL you give it.

## Quickstart: Fly.io (5 minutes)

```sh
# 1. Install Fly CLI
brew install flyctl
fly auth signup     # or `fly auth login`

# 2. Clone + run the path-based setup
git clone https://github.com/volneydouglas/zasder-weather-backend.git
cd zasder-weather-backend
./bin/setup-fly.sh
```

`setup-fly.sh` asks **which sources you want first** (AmbientWeather / Davis
/ LilyGO), then only prompts for what those paths need. It generates your
tokens, creates the app + volume + secrets, deploys, and at the end prints —
**and saves to `zasder-install-summary.txt`** — the exact next steps for
each path you chose (iOS token, LilyGO provision commands, verify curls).
Terminal scrollback gets lost; the summary file doesn't.

When it finishes you'll have a live backend at `https://<app>.fly.dev/`. The
status page at `/` proves it's running. Point the iOS app at that URL + the
printed `API_TOKEN` and you're done.

Stuck? Run the health checklist:

```sh
./bin/doctor.sh        # fly auth, /healthz, both tokens, volume, pollers, recent data
```

## Quickstart: local Docker

```sh
git clone https://github.com/volneydouglas/zasder-weather-backend.git
cd zasder-weather-backend
./bin/setup-local.sh   # generates tokens, asks sources + TZ, writes .env, starts the stack
```

`setup-local.sh` is the LAN counterpart to `setup-fly.sh` — same source
checklist, but it writes `.env` and runs `docker compose up -d` for you.
(Prefer to do it by hand? `cp .env.example .env`, fill `API_TOKEN` +
`INGEST_TOKEN`, then `docker compose up -d`.)

The backend listens on `http://localhost:8080/`. The iOS app needs to reach
it on your LAN — point the app at `http://<your-host-ip>:8080` and the same
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
your backend. Set the two values once and reuse them so you don't fat-finger
the token (the board also advertises mDNS as `zasder-lilygo-XXXX.local`,
where `XXXX` is the last 2 bytes of its MAC):

```sh
export BACKEND_URL="https://your-app.fly.dev"
export INGEST_TOKEN="paste-token-here"      # from zasder-install-summary.txt

curl -X POST "http://zasder-lilygo-1234.local/provision" \
  --data-urlencode "backend_url=$BACKEND_URL" \
  --data-urlencode "ingest_token=$INGEST_TOKEN"
```

Data starts flowing in within seconds. `--data-urlencode` is safer than
plain `-d` because tokens and URLs can contain characters `-d` would mangle.

### Calibrating yearly rain (LilyGO only)

LilyGO boards POST the sensor's **raw lifetime rain counter** (an Atlas that's
been running for years might report 30+ inches). Without calibration the iOS
app shows that lifetime total as "yearly rain." Fix it with a per-MAC offset
so the stored value = `lifetime − offset`:

```sh
# 1. Read what the board is currently posting:
curl -H "Authorization: Bearer $API_TOKEN" \
  "$BACKEND_URL/api/devices/5D:5D:01:00:02:C7/current" | grep -i yearlyrain

# 2. Set the offset (subtracts the sensor's lifetime so YTD starts ~real).
#    --ytd is your true year-to-date inches (default 0 = "count from zero now"):
./bin/set-rain-offset.sh 5D:5D:01:00:02:C7 3.58 --ytd=0.73     # offset → 2.85
```

The helper merges into the `INGEST_YEARLY_RAIN_OFFSETS` Fly secret without
disturbing other MACs. Local Docker users: set the same JSON map in `.env`
(see `.env.example`).

## Device-down email alerts (optional)

The backend can email you when a device that was reporting goes quiet —
an SDR board that hangs, a dead sensor battery, an expired cloud key. It
watches every device, baselines each on first sight (so it won't nag about
ones that were already gone), alerts on the OK→stale transition, and sends
a recovery note when data resumes.

Set the SMTP transport as secrets — easiest is a Gmail **App Password**:

```sh
fly secrets set -a <app> \
  ALERT_EMAIL_TO=you@example.com \
  SMTP_HOST=smtp.gmail.com SMTP_PORT=587 \
  SMTP_USERNAME=you@gmail.com SMTP_PASSWORD=your-app-password
```

Tune how long offline counts as "down" per device (SDRs tight, cloud feeds
looser) with `ALERT_STALE_MINUTES` + the per-MAC `ALERT_STALE_MINUTES_BY_MAC`
map (set a MAC to `0` to stop watching it). See `.env.example` for all knobs.

Everything except the SMTP password can also be managed from the **iOS app**
(Settings → Notifications) via the `/api/alerts` endpoints — recipients,
per-device on/off + thresholds, and even the SMTP server itself (the password
is write-only: the app can set it, the API never returns it). DB settings
override the env defaults and take effect within a minute, no redeploy.

**Threshold alerts** (e.g. "temp above 100°F", "any rain") are stored
server-side too, via `/api/alerts/rules`, and evaluated against incoming data
so they fire even when the app is closed.

## Push notifications (optional)

Alerts can also arrive as iOS push, not just email. Email needs no Apple
account and is the simplest default — push is an optional upgrade with two
ways to enable it:

- **Your own APNs key** — if you build and ship your own iOS app under your
  own Apple Developer account, set `APNS_KEY_ID` / `APNS_TEAM_ID` /
  `APNS_KEY_P8` / `APNS_TOPIC` / `APNS_ENV` (as secrets). The backend then
  signs and sends push directly to Apple.
- **A hosted relay** — if you run the official Zasder Weather app with your
  own backend, you can't hold Apple's key for that app. Instead this backend
  forwards alerts to a relay that does: set `APNS_RELAY_URL` +
  `APNS_RELAY_TOKEN`. Enable push in the app (Settings → Notifications) and it
  obtains the token and configures the backend for you — no Apple account
  needed on your side.

See `.env.example` for both blocks. Leave all of it unset to use email only.

## What's in this repo

```
app/                    FastAPI app — pollers, /ingest/custom, /api/*, status page
tests/                  pytest suite (run `pytest -q`)
lilygo-relay/           ESP32+SX1276 firmware (PlatformIO project)
bin/setup-fly.sh        Path-based Fly.io setup (sources → app, volume, secrets, summary)
bin/setup-local.sh      Guided local Docker setup (tokens, .env, docker compose up)
bin/doctor.sh           Health checklist (auth, /healthz, tokens, volume, pollers, data)
bin/set-rain-offset.sh  Calibrate a LilyGO device's yearly rain (lifetime → real YTD)
docker-compose.yml      Local-deployment compose file
README.md               (this file — human-oriented)
AGENTS.md               LLM-friendly deployment guide
.env.example            Annotated environment template
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
| GET/PUT | `/api/alerts` | Device-down alert prefs (app-managed; SMTP password write-only) |
| PUT | `/api/devices/{mac}/alert` | Per-device monitor toggle + threshold |
| GET/POST/PATCH/DELETE | `/api/alerts/rules` | Threshold alert rules (e.g. tempf above 100), evaluated server-side; PATCH toggles `enabled` |
| POST | `/api/alerts/test` | Send a test alert email to the configured recipients |
| POST | `/api/push/register` | Register an iOS APNs device token for push alerts |
| GET/PUT | `/api/push/relay` | App-managed relay config (URL + token); token write-only, never returned. PUT enforces `https://` + rejects private/loopback hosts |
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
