# wll-poller — Davis WeatherLink Live → Zasder Weather

A tiny pure-stdlib Python service that polls a **Davis WeatherLink Live**
gateway on your LAN and forwards observations to a Zasder Weather backend
via `/ingest/custom`. Runs on any always-on LAN host — a Raspberry Pi is
ideal.

## Why local instead of the cloud poller

The WeatherLink **cloud** API updates every 60 s, needs an account-tied API
key, and adds an internet round-trip. The WLL **local** HTTP API on your
LAN serves a fresh snapshot on every request (the device broadcasts at 2.5 s
over UDP). Same physical Davis VP2, ~6× lower latency, no key, no quotas.

## What it does

- GETs `http://<WLL_HOST>/v1/current_conditions` every `WLL_POLL_SECONDS`
- Normalizes ISS / barometer / WLL-indoor sensor blocks into the
  `/ingest/custom` shape the backend expects
- POSTs to `${BACKEND_URL}/ingest/custom` with the ingest bearer token
- Stateless — backend stores observations; the poller just translates +
  forwards and keeps going on errors

## Requirements

- Python 3.9+ (Pi OS Bookworm ships 3.11 — fine)
- A running Zasder Weather backend with an `INGEST_TOKEN`
- A Davis WeatherLink Live on the same LAN as the Pi

## Install with Docker Compose (recommended)

The simplest path — a small container that restarts on boot. Needs Docker +
the Compose plugin (`docker compose version`).

```sh
# 1. Configure
cp .env.example .env
# edit: WLL_HOST, BACKEND_URL, INGEST_TOKEN, WLL_DEVICE_NAME

# 2. Build + start (detached, auto-restarts)
docker compose up -d --build

# 3. Watch it run
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
curl -s -H "Authorization: Bearer $API_TOKEN" \
  https://<your-backend>/api/devices | jq '.[] | select(.mac == "5D:5D:05:00:00:01")'
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
| `thsw_index` ‖ `heat_index` ‖ `wind_chill` | `outdoor.feels_like` | THSW preferred (sun-aware) |
| `wind_speed_last` / `wind_dir_last` | `wind.speed_mph` / `wind.dir_deg` | |
| `wind_speed_hi_last_10_min`    | `wind.gust_mph`       | 10-min gust |
| `rain_rate_last × rain_size`   | `rain.hourly_in`      | counts/hr × size = in/hr |
| `rainfall_daily × rain_size`   | `rain.daily_in`       | counts → inches |
| `rainfall_year × rain_size`    | `rain.yearly_in`      | apply `INGEST_YEARLY_RAIN_OFFSETS` on backend if needed |
| `solar_rad` / `uv_index`       | `solar.radiation_wm2` / `solar.uv` | |
| `temp_in` / `hum_in` (struct 4) | `indoor.tempf` / `indoor.humidity` | WLL itself (LSS Temp/Hum) |
| `bar_sea_level` / `bar_absolute` (struct 3) | `pressure.relative_inhg` / `pressure.absolute_inhg` | LSS BAR — backend treats relative as rel + abs |
