# Zasder Weather (backend)

[![Release](https://img.shields.io/github/v/release/volneydouglas/zasder-weather-backend?label=release)](https://github.com/volneydouglas/zasder-weather-backend/releases)
[![Changelog](https://img.shields.io/badge/changelog-md-blue)](CHANGELOG.md)

Self-hosted weather-station backend. Pulls data from any combination of
**AmbientWeather cloud**, **Davis WeatherLink cloud or LAN**, **WeatherFlow
Tempest cloud**, or direct **433/915 MHz RF capture** (LilyGO ESP32+SX1276),
stores it in SQLite, and exposes a small HTTP API that a
[companion iOS app](https://zasder.com/weather) reads.

Built because [MyAcurite](https://www.acurite.com/) was killed by AcuRite in
2026 and Davis's WeatherLink Console is a paid cloud lock-in. Owning your
own backend means the data is yours, the dashboard is yours, the app keeps
working when vendors change their minds.

**Not sure what you need?** The
**[install planner](https://zasder.com/weather-helper)** asks what hardware
you have and what you want, then prints a tailored, difficulty-tagged
checklist — which LilyGO board(s) to buy, the exact commands to run, and a
ready-to-paste `setup-fly.sh` one-liner. It runs entirely in your browser
(no login, nothing stored on a server). It also has a **device finder** —
search your station by brand/model (AcuRite, LaCrosse, Oregon Scientific,
Ecowitt, Davis, …) to see how it's supported and what to buy.

If you want LLM-assisted setup (Claude Code, Cursor, Aider, etc.), read
**[AGENTS.md](AGENTS.md)** — the same setup story written for an AI agent.

## What you need

Pick **one or more** of these ingest paths. They all coexist — data shows up
as separate device rows in the iOS app.

| Path | Hardware needed | Data quality | Notes |
|---|---|---|---|
| **A. AmbientWeather cloud** | An AmbientWeather-registered station (WS-2000, WS-2902, etc.) | 60s cadence | Easiest if you already have one. Cloud-only — at the mercy of AWN's API. |
| **B. Davis WeatherLink cloud** | Davis Vantage Vue / Pro 2 + WeatherLink Console (any) | 1–5 min cadence (subscription-tier dependent) | Davis VP2 + 6313 Console works only via cloud — the new console doesn't broadcast in the legacy unencrypted protocol. |
| **C. LilyGO 433 MHz** | LilyGO T3 LoRa32 V1.6.1 board (~$20), AcuRite Atlas — or any 433 MHz OOK station via **discovery mode** | ~16s real-time | Captures AcuRite Atlas first-class; `forward_all=1` additionally posts any decoded 433 MHz weather station (LaCrosse, Oregon Scientific, AcuRite towers/5-in-1, Auriol, TFA, … ~180 protocols). |
| **D. LilyGO 915 MHz** | Second LilyGO board, Fine Offset / AmbientWeather WS-2000 outdoor + WH32B indoor — or other 915 MHz FSK stations via **discovery mode** | ~16s real-time | Captures Fineoffset family (FSK) first-class; merges WH32B indoor data into the outdoor station's tile grid. |
| **E. Davis WeatherLink Live LAN** | A WLL gateway + any always-on LAN host (Pi, Mac, NAS) | ~2s freshness | The better Davis path when the WLL and a small host share a LAN — no API key, no cloud round-trip. |
| **F. WeatherFlow Tempest cloud** | A Tempest station (no extra hardware) | 60s cadence | Free personal access token from tempestwx.com. Also configurable from the app: Settings → Integrations. |
| **G. Ecowitt gateway LAN** | Any Ecowitt gateway/console (GW1000–GW3000, HP2551-class) | 16s+ | The gateway POSTs its "Customized" upload straight to the backend — no vendor cloud, no extra hardware. HTTP-only firmware: direct against local Docker; a cloud backend needs a small LAN forwarder. |
| **H. WeeWX bridge** | A station already running under WeeWX (70+ families) | your archive interval | One extension forwards every archive record; WeeWX keeps doing everything it does today. |

Board to buy for C/D: **LilyGO T3 LoRa32 V1.6.1** from the
[official LilyGO store](https://lilygo.cc/products/lora3) — variant
**"LILYGO 433MHz [Q134]"** for path C, **"Paxcounter 915MHz [Q211]"** for
path D. (Not an affiliate link; buy anywhere — just match the model and
frequency. The firmware it ships with doesn't matter, you'll flash this
repo's.)

Two deployment modes for the backend itself:

- **Cloud (Fly.io)** — recommended, and the only *supported* way to reach
  your data off your LAN. ~$2/month, works anywhere you have internet.
- **Local Docker** — runs on any always-on Linux/macOS box. LAN-only:
  exposing it to the internet (port-forward, tunnel, reverse proxy) is on
  you. Good for "I want all my data on-premise."

The iOS app ([app page](https://zasder.com/weather)) is distributed separately and connects to whichever backend URL you give it.

## Quickstart: Fly.io (5 minutes)

### On a Mac: no Terminal required

If you'd rather not use the Terminal at all, you don't have to:

1. Click the green **Code** button at the top of this page →
   **Download ZIP**.
2. Double-click the downloaded ZIP to unpack it.
3. Inside the folder, **double-click `Zasder Weather Setup.command`**.

macOS will likely say the file is *"from an unidentified developer."* That's
expected for a script downloaded from the internet — **right-click the file
and choose Open** instead, then confirm. You only do this once.

It installs the Fly.io tool, signs you in through your browser, creates your
server, and offers to connect a Davis WeatherLink Live at the end. It asks
questions along the way — **type short answers and press return**; don't
paste commands into them.

### Or with the Terminal

```sh
# 1. Install Fly CLI
brew install flyctl
fly auth signup     # or `fly auth login`

# 2. Clone + run the path-based setup
git clone https://github.com/volneydouglas/zasder-weather-backend.git
cd zasder-weather-backend
./bin/setup-fly.sh
```

> **Paste these one line at a time**, waiting for each to finish. Lines
> starting with `#` are comments — they explain the step and do nothing if
> you paste them, so you can skip them.
>
> Once `./bin/setup-fly.sh` is running it starts **asking you questions**
> (app name, region, which stations you have). At those prompts, *type your
> answer and press return* — don't paste the next command from this page.
> Pasting a command into a question is the single most common way this goes
> wrong.

`setup-fly.sh` asks **which sources you want first** (AmbientWeather / Davis
/ Tempest / LilyGO), then only prompts for what those paths need. It generates your
tokens, creates the app + volume + secrets, deploys, and at the end prints —
**and saves to `zasder-install-summary.txt`** — the exact next steps for
each path you chose (iOS token, LilyGO provision commands, verify curls).

That summary file is written **inside the `zasder-weather-backend` folder you
just cloned**, so from that folder you can open it with:

```sh
open zasder-install-summary.txt      # macOS
cat zasder-install-summary.txt       # Linux
```

Terminal scrollback gets lost; the summary file doesn't. If you ever lose it,
`./bin/setup-fly.sh --print-tokens` reads your tokens back off the running app.

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

> **LilyGO boards require an HTTPS backend URL.** The firmware is TLS-only
> (it always uses `WiFiClientSecure`), so a plain-`http://…:8080` local-Docker
> backend will provision fine but **never receive LilyGO data** — the POSTs
> silently fail. To use Paths C/D, either deploy to Fly.io (automatic HTTPS)
> or put a TLS-terminating reverse proxy in front of local Docker and provision
> the board with that `https://` URL.

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

> **Have a WeatherLink Live (WLL) gateway on the same LAN as a Raspberry
> Pi?** [Path E below](#path-e--davis-weatherlink-live-lan-local-poller)
> is the better option: ~6× fresher data, no API key, no cloud round-trip.
> Use Path B (cloud) only if your WLL isn't reachable from a host that can
> POST to this backend.


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

> **The backend URL you provision must be HTTPS.** The firmware is TLS-only,
> so a plain-HTTP local-Docker backend (`http://<host-ip>:8080`) will NOT
> receive LilyGO data even though provisioning appears to succeed. Use Fly.io
> (auto HTTPS) or front local Docker with a TLS-terminating reverse proxy.

See **[lilygo-relay/README.md](lilygo-relay/README.md)** for hardware, flashing,
provisioning, and field-tested gotchas. Short version:

```sh
brew install platformio
cd lilygo-relay
pio run -e t3_v161_433 -t upload    # for the 433 board
# or
pio run -e t3_v161_915 -t upload    # for the 915 board
```

After flashing, the board comes up as a `ZasderLilyGO` Wi-Fi access point
(WPA2 password `zasder-setup`). Join it from a phone, fill in your home
Wi-Fi creds, save. Then point it at
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

### Discovery mode — stations beyond Atlas/Fineoffset

Out of the box a board posts only the first-class models (Atlas on 433,
Fineoffset family on 915) and drops everything else it decodes. Flip on
**discovery mode** to forward *any* decoded weather station — the firmware
bundles the full rtl_433 decoder set (~180 protocols at 433 MHz OOK:
LaCrosse, Oregon Scientific, AcuRite 5-in-1/towers/986, Ambient F007TH,
Auriol, TFA, Nexus/Prologue, …):

```sh
curl -X POST "http://zasder-lilygo-1234.local/provision" \
  --data-urlencode "forward_all=1"     # add current_token=... once provisioned
```

- Each discovered station shows up as its own device row named
  `<Model> <id>` (e.g. `LaCrosse-TX141THBv2 3404534`) — hide any
  neighbors' sensors you don't want in the iOS app.
- Non-weather transmissions (garage doors, TPMS, smoke detectors) are
  filtered out automatically — only packets carrying temp/humidity/wind/
  rain/UV/pressure fields are forwarded.
- Turn it back off with `forward_all=0`; the flag persists across reboots
  and shows in `GET /status`.
- New hardware support arrives with rtl_433 itself — e.g. the AcuRite
  Optimus will flow through as soon as
  [merbanan/rtl_433#3444](https://github.com/merbanan/rtl_433/issues/3444)
  lands a decoder, no firmware change needed here.

Search your exact station in the
[planner's device finder](https://zasder.com/weather-helper) to see which
band/board it needs.

## Path F — WeatherFlow Tempest cloud poller

Add to `.env` (or as Fly secrets):

```sh
TEMPEST_TOKEN=<personal access token from https://tempestwx.com/settings/tokens>
TEMPEST_STATION_ID=<the number in your station's URL on that site>
```

Restart. The backend polls every 60s (`TEMPEST_POLL_INTERVAL_SECONDS` to
change), converts the metric REST payload to API-native units, and captures
lightning (strike counts + nearest-strike distance) alongside the usual
readings. `TEMPEST_NAME` overrides the station's display name.

No terminal? The app can configure this instead: **Settings →
Integrations → WeatherFlow Tempest** — app-stored values win over these
env values.

## Path G — Ecowitt gateway (LAN, direct — no relay needed)

Every Ecowitt gateway and console (GW1000 through GW3000, HP2551-class
displays) can POST its readings straight to your backend — no vendor
cloud, no extra hardware, nothing to install. In the WSView Plus app (or
the gateway's web UI): **… → Customized**, then set

- **Protocol**: Ecowitt
- **Server / IP**: your backend's LAN hostname or IP (no `http://`)
- **Path**: `/ingest/ecowitt?token=<your INGEST_TOKEN>`
- **Port**: 8080 (the docker-compose default), **Upload interval**: 16s
  or higher

**The gateway speaks plain HTTP only — it cannot do TLS.** That means
this path works directly against a backend on your own network (the
docker-compose setup above), but NOT against an HTTPS-only host like a
Fly deploy: pointed at port 443 every upload fails silently, and port 80
just gets a redirect the firmware won't follow. For a cloud-hosted
backend, run any small TLS-terminating forwarder on the LAN and aim the
gateway at it — a one-line Caddyfile does it:

```
:8081
reverse_proxy https://your-app.fly.dev {
	header_up Host your-app.fly.dev
}
```

The token rides the path because Ecowitt firmware cannot send HTTP
headers — treat that URL like the credential it contains, and remember
the gateway→backend hop is cleartext on your LAN. Readings
arrive in US-native units (the storage convention, no conversion loss),
per-sensor battery flags are normalized into the health watcher, and on
dual-rain stations (say a WS90's haptic sensor plus a WH40 tipping
gauge) the tipping gauge wins wherever both report — haptic rain
phantom-tips when the mast gets bumped.

## Path H — already running WeeWX? Bridge it over

If WeeWX already runs your station — any of its 70+ supported station
families — one small extension sends every archive record to your backend
too. WeeWX keeps doing everything it does today.

```sh
weectl extension install /path/to/weewx-bridge   # WeeWX 5
```

Then set `server_url` and `ingest_token` under `[StdRESTful] [[Zasder]]`
in `weewx.conf` and restart WeeWX. Full details in
**[weewx-bridge/README.md](weewx-bridge/README.md)**.

## Path E — Davis WeatherLink LIVE (LAN, local poller)

If your Davis station has a **WeatherLink Live** gateway (the small box that
plugs into your router), a tiny poller on the same LAN can serve fresh
sensor data every few seconds — *much* faster than the 60-second cloud
poll, with no API key and no internet round-trip. The poller runs on any
always-on machine on the same network as the WeatherLink Live — a Raspberry
Pi, a Mac, a NAS — and POSTs to your backend's `/ingest/custom`. The backend
itself needs no extra configuration.

See **[wll-poller/README.md](wll-poller/README.md)** for the full install.

**On a Mac** — no Docker and no file editing; it asks three questions and
sets itself to start automatically at login:

```sh
cd wll-poller
bash bin/setup-macos.sh
```

**With Docker** — Raspberry Pi, Linux, or a Mac running Docker:

```sh
cd wll-poller
cp .env.example .env

# Now open .env in a text editor and fill in these four values:
#   WLL_HOST, BACKEND_URL, INGEST_TOKEN, WLL_DEVICE_NAME
open -e .env     # macOS — opens TextEdit; edit, then save with Cmd-S
# nano .env      # Raspberry Pi / Linux — edit, then Ctrl-O, Enter, Ctrl-X

docker compose up -d --build
docker logs -f wll-poller
```

Your `INGEST_TOKEN` is in the `zasder-install-summary.txt` file the installer
wrote into the folder you cloned. If you can't find it:

```sh
open "$(find ~ -name zasder-install-summary.txt -maxdepth 6 2>/dev/null | head -1)"
```

If you previously ran Path B (cloud) for the same Davis, point
`WLL_DEVICE_MAC` at the cloud poller's MAC (default
`5D:5D:05:00:00:01` already matches) so both feeds land on one device row
— WLL wins by recency on every read. Then you can disable Path B:

```sh
fly secrets unset -a <app> \
  WEATHERLINK_API_KEY WEATHERLINK_API_SECRET WEATHERLINK_STATION_ID \
  WEATHERLINK_YEARLY_RAIN_BASELINE_IN WEATHERLINK_NAME WEATHERLINK_LOCATION \
  WEATHERLINK_POLL_INTERVAL_SECONDS
```

(`WEATHERLINK_YEARLY_RAIN_BASELINE_IN` applies only to the Path B cloud
poller. The WLL local path and LilyGO boards post lifetime counters raw.)

### A note on yearly rain (LilyGO + WLL)

LilyGO boards (and WLL local) POST the sensor's **raw lifetime rain
counter**, and that's fine: daily/weekly/monthly/yearly rain shown in the
app is computed from the *changes* in that counter over stored history,
not from its absolute value, so no calibration is needed. The old
`INGEST_YEARLY_RAIN_OFFSETS` mechanism was removed in v1.3.2 (it could
corrupt rain history) — if an old config still sets it, it is ignored;
remove it.

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
so they fire even when the app is closed. Each rule carries an urgency —
**minor / standard / major / urgent** (1.9 adds major): minor holds during
quiet hours and rides the daily digest, standard always pushes except
during quiet hours, major ignores quiet hours, and urgent additionally
arrives Time Sensitive on iOS (breaks through Focus — for alerts worth
waking up for).

**Morning report** (1.9): pick a digest hour in the app (Settings →
Notifications) and each morning you get a full weather report —
yesterday's highs, lows, rain, and gusts per station, the overnight alert
log, and today's outlook — as a formatted HTML email plus a compact push.
With the iOS app installed, the same report also lands as a lock-screen
Live Activity card that dismisses itself by mid-morning. The email half
needs the SMTP config above; the phone half works on push alone.

## Push notifications (optional)

Alerts can also arrive as mobile push, not just email. Email needs no vendor
account and is the simplest default — push is an optional upgrade:

- **iOS — your own APNs key** — if you build and ship your own iOS app under
  your own Apple Developer account, set `APNS_KEY_ID` / `APNS_TEAM_ID` /
  `APNS_KEY_P8` / `APNS_TOPIC` / `APNS_ENV` (as secrets). The backend then
  signs and sends push directly to Apple.
- **iOS — a hosted relay** — if you run the official Zasder Weather app with
  your own backend, you can't hold Apple's key for that app. Instead this
  backend forwards alerts to a relay that does: set `APNS_RELAY_URL` +
  `APNS_RELAY_TOKEN`. Enable push in the app (Settings → Notifications) and it
  obtains the token and configures the backend for you — no Apple account
  needed on your side.
- **Android — FCM** — if you build and ship your own Android app with your own
  Firebase project, set `FCM_SERVICE_ACCOUNT_JSON` (the service-account key
  JSON, as a secret; `project_id` is read from it). The backend delivers to
  Android via FCM and iOS via APNs in parallel — the alert monitor splits by
  each registered token's platform.

See `.env.example` for the blocks. Leave all of it unset to use email only.

## Sharing your station (optional)

The iOS app can mint **share links** so family or a co-op can see (or help
run) your station without holding your API token:

- **Read links** — full dashboard, charts, and history, but read-only and
  with your infrastructure hidden (no SMTP identity, no token lists, no
  backups). Revocable one by one from the app.
- **Write links** (1.9) — everything a read link shows, plus station
  operations: rename or relocate a device, toggle per-device alerts, edit
  threshold rules, register the holder's own phone for push, start a
  storm watch. Everything administrative — tokens, credentials, backups,
  updates, retention, device deletion — stays owner-only. Every write is
  attributed: `GET /api/write-audit` shows who changed what and when
  (label + token tail, never the credential itself).

Guest read tokens can also be provisioned by env (`GUEST_API_TOKENS`) for
setups without the app.

## Public dashboard (optional)

By default the status page at `/` shows the app screenshots. Set
`PUBLIC_DASHBOARD=1` to replace them with a live, read-only dashboard of your
station — current conditions plus inline 24-hour charts, and a "Get the iOS app"
link. It's fully server-rendered: no client JavaScript, no public data API. Your
`/api/*` endpoints stay token-gated; only the pre-rendered numbers and charts on
that one page are exposed.

- `PUBLIC_DASHBOARD_MACS` — unset shows your **primary** (first) station only;
  `all` shows every visible device; or a comma-separated MAC allowlist.
- `PUBLIC_DASHBOARD_FIELDS` — unset charts the core set
  (`tempf,humidity,windspeedmph,baromrelin,hourlyrainin`); pass a comma-separated
  subset to pick which, in page order.
- `PUBLIC_DASHBOARD_APP_URL` — the app-link target (defaults to the App Store
  listing).

The page auto-refreshes every 2 minutes. Leave `PUBLIC_DASHBOARD` unset (or `0`)
to keep the screenshots.

### Embed it on your own website (1.6.1+)

With the dashboard on, `GET /embed` serves the dashboard **alone** — no
status chrome — with framing allowed, so you can put your weather inline
on any site you run with one iframe:

```html
<iframe src="https://YOUR-APP.fly.dev/embed" title="Weather dashboard"
        width="100%" height="1300" style="border:0" loading="lazy"></iframe>
```

Adjust `height` to taste. The embed reuses the same cached dashboard
fragment as the front page — no second dashboard build — auto-refreshes
every 5 minutes, and 404s while `PUBLIC_DASHBOARD` is off.

By default the page follows the visitor's system appearance
(`prefers-color-scheme`, dark being the base look). A site with a fixed
palette can pin it: `/embed?theme=light` or `/embed?theme=dark`. Every other page keeps strict
anti-framing headers — `/embed` is the one page designed to be framed.
Live example: <https://weather.zasder.com/embed>.

## Records & climate

`GET /api/devices/{mac}/records` returns per-metric highs & lows — with the
local time each was set — over **today / past week / this month / this year /
all-time** (temperature, feels-like, dew point, humidity, pressure, wind,
gust, UV, solar, rain). Results are cached 15 minutes per device. When the
public dashboard is on, an all-time **Records** strip (hottest, coldest, peak
gust, wettest day, high/low pressure) is rendered under the charts.

Three climate endpoints (1.9) answer the longer
questions from the same rollups: `/api/devices/{mac}/climate?year=` gives
twelve month rows (means, extremes with dates, rain, heating/cooling/
growing degree days) plus annual totals and the running **water year**
(`WATER_YEAR_START_MONTH`, default October — set `1` for calendar year);
`/api/devices/{mac}/daily-series` serves one row per local day for
year-span charts; and `/api/devices/{mac}/reports/noaa?year=[&month=]`
renders the classic NOAA-style fixed-width climate report as plain text.

## History retention (optional)

By default every reading is kept forever. On multi-year archives you can
opt into aging: raw rows older than `HISTORY_DETAIL_DAYS` are thinned to
one per `HISTORY_KEEP_INTERVAL_MINUTES`, and/or each row's raw JSON
payload (most of its bytes) is dropped past `HISTORY_JSON_DETAIL_DAYS`
while every charted field keeps its typed column. Daily rollups preserve
every day's true extremes either way, so records and climate stay exact.
The iOS app manages the same settings at `GET/PUT /api/history-retention`
(app-stored values win over env), and `GET /api/storage` shows where the
database's bytes actually live. See `.env.example` for the knobs and
floors.

## Smart alerts (optional)

Set `SMART_ALERTS=1` for weather-intelligent alerts that need no threshold
config, delivered over the same email/push channels as the device-down and
threshold alerts:

- **Frost/freeze risk** — outdoor temp at/below `SMART_ALERT_FROST_F` (35°F).
- **Dangerous heat** — feels-like at/above `SMART_ALERT_HEAT_F` (105°F).
- **Rapid pressure drop** — barometer falls more than
  `SMART_ALERT_PRESSURE_DROP_INHG` (0.06 inHg) over 3 hours → storm approaching.

Each fires once when the condition starts and re-arms when it clears.

## AirGradient air quality (optional)

Have an [AirGradient](https://www.airgradient.com/) monitor? One account
token (app: **Settings → Integrations → AirGradient**) polls every monitor
on the account; each location becomes its own device with PM1/PM2.5/PM10,
CO2, and TVOC/NOx columns. Air monitors stay out of the weather-station
machinery (uploads, forecast location, NWS polling, public page) by
design. A LAN-local backend can skip the cloud entirely (1.9): set
`AIRGRADIENT_LOCAL_HOSTS` to the monitors' hostnames/IPs and the backend
reads their local API directly.

## Prometheus & Grafana (optional)

Set `PROMETHEUS_METRICS=1` to expose `GET /metrics` in Prometheus text format —
every device's latest reading as gauges (`zasder_temperature_fahrenheit`,
`zasder_pressure_inhg`, `zasder_wind_gust_mph`, …, plus
`zasder_device_last_seen_seconds`). Point Prometheus at it and build Grafana
dashboards / alerts. Example scrape:

```yaml
scrape_configs:
  - job_name: zasder-weather
    metrics_path: /metrics
    static_configs:
      - targets: ["your-backend-host"]
```

## Home Assistant (MQTT, optional)

Set `MQTT_HOST` (and `MQTT_USERNAME`/`MQTT_PASSWORD` if your broker needs auth)
to publish readings to MQTT. The backend sends retained **Home Assistant MQTT
discovery** configs, so each station's sensors appear in Home Assistant
automatically with the right units and device classes — no YAML. State is
published to `<MQTT_TOPIC_PREFIX>/<node>/state` every ~30 seconds. Tune with
`MQTT_PORT`, `MQTT_TOPIC_PREFIX`, and `MQTT_DISCOVERY_PREFIX`.

## Upgrading

The backend checks GitHub once a day and shows an **"update available"** banner
on the status page (and at `GET /api/version`) when a newer release exists.
Disable with `UPDATE_CHECK=0`. See [CHANGELOG.md](CHANGELOG.md) for what changed.

### Automatic updates (Fly.io, optional)

If you said yes during setup, your instance updates **itself**: about two days
after an official release ships, it verifies the release image exists, swaps
its own machine onto it, and restarts (sub-minute; your `/data` volume and
settings are untouched). The delay exists so a bad release can be pulled
before any auto-updater picks it up; the updater also never crosses a major
version (those may carry manual steps) and never downgrades.

Enable it later on an existing instance:

```sh
# Piped: the token (FlyV1 fm2_…, the interior space is part of it) never
# touches your shell history or the process list.
fly tokens create deploy --app <your-app> |     # app-scoped, deploy-only
  fly secrets set FLY_API_TOKEN=- AUTO_UPDATE=1 --app <your-app>
```

Turn it off any time with `fly secrets set AUTO_UPDATE=0 --app <your-app>` —
and if you want the deploy *capability* gone too, not just unused, remove the
token: `fly secrets unset FLY_API_TOKEN --app <your-app>`.
The full knob list (delay hours, image repository) is in `.env.example`.

**Major versions** (1.9+): the one-tap update in the app normally stops at
a major-version boundary, because a major can carry manual steps. A release
that needs none *vouches* for itself (an `upgrade.json` at its tag stating
the oldest version it installs onto hands-free), and the one-tap path then
works across the boundary too. Unvouched majors keep the classic
follow-the-release-notes flow, and automatic updates never cross a major
either way.

To upgrade, from the repo directory:

```sh
./bin/upgrade.sh          # auto-detects Fly.io or Docker
```

`upgrade.sh` handles one Fly-specific wrinkle for you: `setup-fly.sh` pinned
your app name and region into `fly.toml`, which is a tracked file, so your
checkout is permanently "modified". The script saves that file to
`fly.toml.bak`, fast-forwards, and re-applies your app name and region.

By hand:

```sh
# Fly.io — prefer ./bin/upgrade.sh, which handles all of this for you.
#
# By hand: setup-fly.sh pins your app name and region into fly.toml, which is
# tracked, so a plain `git pull` stops with "local changes" the moment a
# release edits that file. Do NOT `git stash pop` across it — if the release
# also changed fly.toml (precisely when you needed the stash) the pop
# conflicts, the && chain stops, and you are left mid-conflict with no deploy.
cp fly.toml fly.toml.bak && git checkout -- fly.toml && git pull --ff-only
# then copy `app` and `primary_region` back from fly.toml.bak into fly.toml
fly deploy

# Docker (published image — no local rebuild)
git pull && docker compose pull && docker compose up -d
```

The SQLite schema **auto-migrates on boot** (idempotent `CREATE TABLE IF NOT
EXISTS` + `ALTER`), and your `/data` volume is never touched — upgrades are
safe and reversible (pin an older tag to roll back). Docker users pull the
published image from
`ghcr.io/volneydouglas/zasder-weather-backend` (pin a version by replacing
`:latest` with e.g. `:1.0.0` in `docker-compose.yml`).

## What's in this repo

```
app/                    FastAPI app — pollers, ingest routes, /api/*, status page
tests/                  pytest suite (run `pytest -q`)
lilygo-relay/           ESP32+SX1276 firmware (PlatformIO project)
wll-poller/             Davis WeatherLink Live LAN poller (Path E)
weewx-bridge/           WeeWX extension → /ingest/custom (Path H)
mcp/                    Read-only MCP server — your stations, readable by an AI assistant
bin/setup-fly.sh        Path-based Fly.io setup (sources → app, volume, secrets, summary)
bin/setup-local.sh      Guided local Docker setup (tokens, .env, docker compose up)
bin/doctor.sh           Health checklist (auth, /healthz, tokens, volume, pollers, data)
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
| GET | `/healthz` | Liveness + running version, no auth |
| GET | `/api/version` | Running version, latest release, and whether an update is available (no auth) |
| GET | `/api/devices` | All devices + latest reading |
| DELETE | `/api/devices/{mac}` | Remove a retired device + all its observations + alert state (token-gated) |
| GET | `/api/devices/{mac}/current` | Composite latest-non-null per field |
| GET | `/api/devices/{mac}/history?hours=24` | Time series, auto-bucketed for 3d/7d/30d. Optional `end_ms=` sets the window END (epoch ms) to page back through older/imported history |
| GET | `/api/devices/{mac}/summary?field=tempf&hours=24` | Min/max/avg/median + when |
| GET | `/api/devices/{mac}/records` | All-time / yearly / monthly / weekly / today highs & lows per metric, with when each was set |
| GET | `/api/devices/{mac}/climate?year=` | Monthly climate rows (means, extremes, rain, degree days) + annual totals + water year |
| GET | `/api/devices/{mac}/daily-series` | One row per local day (min/max/mean temp, rain, peak gust) for year-span charts |
| GET | `/api/devices/{mac}/reports/noaa?year=[&month=]` | Classic NOAA-style fixed-width climate report, plain text |
| GET | `/api/devices/{mac}/storms` | Recent closed storm episodes — the structured stats behind each storm summary |
| GET/PUT | `/api/history-retention` | History-aging settings (thin/JSON windows) — app-stored values win over the `HISTORY_*` env vars |
| GET | `/api/write-audit` | Attribution log for write-share links: who changed what, when (owner-only) |
| GET | `/api/sources` | Health of each ingest source (last success, last error) — tells a dead API key from dead hardware |
| GET | `/api/insights?mac=` | Server-side statistics rollups (on by default since 1.9; `INSIGHTS=0` disables) |
| POST | `/api/insights/rebuild` | Force a rollup rebuild (optional `?mac=`) — normally unneeded, a backfill self-schedules on boot |
| POST | `/api/import/wu` | Start a Weather Underground history import into a device (`dry_run` supported); progress at GET `/api/import/wu/status` |
| GET/PUT | `/api/config/wu-key` | Server-stored WU API key (write-only — GET reports only configured/source; falls back to the `WU_API_KEY` env var) |
| GET/PUT | `/api/devices/{mac}/wu-station` | WU station ID associated with a device — the import target mapping, plus live WU forwarding: `upload_key` (the WU *station* key, write-only — GET reports only `upload_key_set`) and `upload_enabled` turn on per-reading uploads to wunderground.com, keeping a WU station alive when MyAcurite/other forwarding shuts down. Upload health appears in `/api/sources` (`wu_upload` block) |
| GET | `/api/config/backup` | Export of operator config (alerts, rules, names…) — no tokens, no SMTP password |
| POST | `/api/config/restore` | Restore a config export onto a fresh instance |
| GET | `/api/forecast?lat=&lon=` | 7-day forecast (Open-Meteo) |
| GET/PUT | `/api/alerts` | Device-down alert prefs (app-managed; SMTP password write-only) |
| PUT | `/api/devices/{mac}/alert` | Per-device monitor toggle + threshold |
| GET/POST/PATCH/DELETE | `/api/alerts/rules` | Threshold alert rules (e.g. tempf above 100), evaluated server-side; PATCH toggles `enabled` |
| POST | `/api/alerts/test` | Send a test alert email to the configured recipients |
| POST | `/api/push/register` | Register a push token (iOS APNs or Android FCM — `platform` field) |
| GET/PUT | `/api/push/relay` | App-managed relay config (URL + token); token write-only, never returned. PUT enforces `https://` + rejects private/loopback hosts |
| GET | `/api/storage` | Where the database's bytes live: per-table sizes, the data_json split, thinning state |
| POST | `/api/update/check` | Run the daily release check right now (the background check is once a day) |
| POST | `/ingest/custom` | Source posts a normalized observation. `Authorization: Bearer <INGEST_TOKEN>` |
| POST | `/ingest/ecowitt` | Ecowitt gateway "Customized" upload (form-encoded; imperial or metric consoles both work — metric keys are converted on ingest). Set the gateway's path to `/ingest/ecowitt?token=<INGEST_TOKEN>` — Ecowitt firmware can't send headers |
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
