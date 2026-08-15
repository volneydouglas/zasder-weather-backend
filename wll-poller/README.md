# wll-poller — Davis WeatherLink Live → Zasder Weather

A tiny pure-stdlib Python service that polls a **Davis WeatherLink Live**
gateway on your LAN and forwards observations to a Zasder Weather backend
via `/ingest/custom`. Runs on any always-on LAN host — a Raspberry Pi is
ideal.

## Moving to the macOS app

The Mac app has the same poller built in, so you can retire this script.
One thing matters during the switch: **use the same station ID**. This
script posts under `WLL_DEVICE_MAC` (default `5D:5D:05:00:00:01`), while
a fresh Mac install mints its own random ID. If they differ, your server
grows a SECOND station and the original stops updating, stranding its
history.

1. Install the Mac app and enter your backend URL and API token.
2. In its settings, set **Station ID** to this script's `WLL_DEVICE_MAC`
   value, then add your WeatherLink Live IP and ingest token.
3. Confirm readings keep arriving on the original station.
4. Only then remove the LaunchAgent (see below).

## Why local instead of the cloud poller

The WeatherLink **cloud** API updates every 60 s, needs an account-tied API
key, and adds an internet round-trip. The WLL **local** HTTP API on your
LAN serves a fresh snapshot on every request (the device broadcasts at 2.5 s
over UDP). Same physical Davis VP2, ~6× lower latency, no key, no quotas.

## What it does

- GETs `http://<WLL_HOST>/v1/current_conditions` every `WLL_POLL_SECONDS`
- Normalizes ISS / barometer / WLL-indoor sensor blocks into the
  `/ingest/custom` shape the backend expects
- Reads **one** ISS transmitter (a WLL supports 8). Blank `WLL_TXID` uses the
  lowest id reporting and warns if there are others; set it to pin one
- POSTs to `${BACKEND_URL}/ingest/custom` with the ingest bearer token
- Stateless — backend stores observations; the poller just translates +
  forwards and keeps going on errors

## Requirements

- Python 3.9+ (Pi OS Bookworm ships 3.11, macOS ships 3.9 — both fine)
- A running Zasder Weather backend with an `INGEST_TOKEN`
- A Davis WeatherLink Live on the same LAN as the machine running this

You do **not** need a Raspberry Pi. Any computer that stays on and is on the
same network as the WeatherLink Live works — including a Mac.

## Install on a Mac (easiest — no Docker, no Terminal editing)

If you have a Mac that stays on, this is the least fiddly option. macOS
already includes everything needed, so there is nothing to install: the
script asks three questions, checks each answer actually works, and sets the
poller to start automatically at login.

```sh
bash bin/setup-macos.sh
```

It will ask for:

1. **Your WeatherLink Live's IP address** — find it in the WeatherLink app
   under Account → Devices → your WLL → Device Info, or in your router's
   device list. It looks like `192.168.1.42`.
2. **Your backend URL** — the address you deployed, e.g.
   `https://your-app.fly.dev`.
3. **Your `INGEST_TOKEN`** — see [Where to find your
   INGEST_TOKEN](#where-to-find-your-ingest_token) below.

Before installing anything it sends one real reading, so a wrong token is
caught immediately rather than showing up as silence hours later.

```sh
# watch it run
tail -f ~/Library/Logs/zasder-wll-poller.log

# remove it completely
bash bin/setup-macos.sh --uninstall
```

Nothing rotates that log automatically. In normal operation it grows very
slowly, but a persistently failing setup (backend unreachable, revoked
token) warns every poll (~50 MB/yr at the default cadence). If it ever gets
big, truncate it any time — the poller just keeps appending:

```sh
: > ~/Library/Logs/zasder-wll-poller.log
```

The poller runs in the background as a launchd agent
(`~/Library/LaunchAgents/com.zasder.wll-poller.plist`), restarts if it ever
stops, and starts again at login. To keep it running you'll want the Mac set
to not sleep — System Settings → Lock Screen → "Turn display off on power
adapter when inactive", and in Battery/Energy settings enable "Prevent
automatic sleeping on power adapter when the display is off".

## Where to find your INGEST_TOKEN

The backend installer generated it and saved it, along with your backend URL,
into a file named **`zasder-install-summary.txt`** in the folder you
downloaded/cloned. If you're not sure where that folder ended up:

```sh
open "$(find ~ -name zasder-install-summary.txt -maxdepth 6 2>/dev/null | head -1)"
```

That opens it in TextEdit so you can copy the token out. If it finds nothing,
you can read the token straight from your deployed backend instead:

```sh
fly ssh console -a <your-app-name> -C 'printenv INGEST_TOKEN'
```

Note `INGEST_TOKEN` and `API_TOKEN` are two different values — the poller
needs `INGEST_TOKEN`.

## Install with Docker Compose

A small container that restarts on boot. Needs Docker + the Compose plugin
(`docker compose version`).

```sh
# 1. Make your own copy of the settings file, readable only by you —
#    it will hold your INGEST_TOKEN (a write credential for the backend)
cp .env.example .env
chmod 600 .env

# 2. Open it in a text editor and fill in the five values:
#      WLL_HOST, BACKEND_URL, INGEST_TOKEN, WLL_DEVICE_NAME, WLL_DEVICE_MAC
#    (WLL_DEVICE_MAC: change the last three bytes to your own values —
#    keeping the example's default collides with any other install
#    posting to the same/hosted backend)
#    On a Mac, this opens it in TextEdit — edit, then save with Cmd-S:
open -e .env
#    On a Pi/Linux, use nano instead:  nano .env

# 3. Build + start (detached, auto-restarts)
docker compose up -d --build

# 4. Watch it run
docker logs -f wll-poller
```

The compose service reads `.env`, uses default bridge networking (it reaches
the WLL on your LAN and the backend over the internet with no extra config),
and caps its logs. To stop: `docker compose down`. To update after a `git
pull`: `docker compose up -d --build`.

## Install on the Pi (systemd, no Docker)

Prefer running the script directly under systemd instead of a container:

```sh
# 1. Drop the script into /opt and the env file under /etc/zasder
sudo mkdir -p /opt/wll-poller /etc/zasder
sudo cp poller.py /opt/wll-poller/
sudo cp .env.example /etc/zasder/wll-poller.env
sudo chmod 600 /etc/zasder/wll-poller.env

# 2. Edit the env (WLL_HOST, BACKEND_URL, INGEST_TOKEN)
sudoedit /etc/zasder/wll-poller.env

# 3. Install + start the systemd unit
sudo cp wll-poller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wll-poller

# 4. Watch it run
journalctl -fu wll-poller
```

## Verify it's working

```sh
# Direct WLL check (no auth, just GET it) — use your WLL's LAN IP
curl -s http://<wll-host>/v1/current_conditions | python3 -m json.tool | head -40

# Backend should be receiving — recent observations on the device MAC
# (use the WLL_DEVICE_MAC you configured)
curl -s -H "Authorization: Bearer $API_TOKEN" \
  https://<your-backend>/api/devices | jq '.[] | select(.mac == "<your WLL_DEVICE_MAC>")'
```

If the dashboard's Davis card starts updating every ~10s instead of every
minute, you're done.

## Disabling the cloud poller (optional, after local is healthy)

The cloud poller is fully redundant once local is feeding the same MAC.
On the backend host:

```sh
fly secrets unset WEATHERLINK_API_KEY WEATHERLINK_API_SECRET \
                  WEATHERLINK_STATION_ID -a <your-app>
```

The backend will log "WeatherLink not configured — skipping Davis cloud
poller" on next start. Local is now the sole source.

## Tests

```sh
python3 -m unittest discover tests -v
```

The transform is pure (`to_observation`) and unit-tested against captured
WLL JSON samples; no network needed.

## Field mapping

| WLL field                      | Ingest field          | Notes |
|---|---|---|
| `temp` / `hum` / `dew_point`   | `outdoor.tempf` / `outdoor.humidity` / `outdoor.dew_point_f` | ISS |
| `heat_index` / `wind_chill` | `outdoor.feels_like` | Heat index at ≥80°F, wind chill at ≤50°F, air temp in between — WLL populates *both* indices at every temperature, so the regime has to be picked here. THSW deliberately **not** used: it adds a direct-sun load that runs 5–10°F hotter than every other source |
| `wind_speed_last` / `wind_dir_last` | `wind.speed_mph` / `wind.dir_deg` | |
| `wind_speed_hi_last_10_min`    | `wind.gust_mph`       | 10-min gust |
| `rainfall_last_60_min × rain_size` | `rain.hourly_in`  | last-hour ACCUMULATION (counts → inches), matching what the WeatherLink cloud poller writes to the same field. Deliberately NOT `rain_rate_last` — that's an instantaneous rate that spikes on bursts and reads ~0 in steady light rain |
| `rainfall_daily × rain_size`   | `rain.daily_in`       | counts → inches |
| `rainfall_monthly × rain_size` | `rain.monthly_in`     | counts → inches |
| `rainfall_year × rain_size`    | `rain.yearly_in`      | counts → inches |
| `solar_rad` / `uv_index`       | `solar.radiation_wm2` / `solar.uv` | |
| `temp_in` / `hum_in` (struct 4) | `indoor.tempf` / `indoor.humidity` | WLL itself (LSS Temp/Hum) |
| `bar_sea_level` / `bar_absolute` (struct 3) | `pressure.relative_inhg` / `pressure.absolute_inhg` | LSS BAR — backend treats relative as rel + abs |
