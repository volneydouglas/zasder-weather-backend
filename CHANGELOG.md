# Changelog

All notable changes to the Zasder Weather backend. Format based on
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

The running version is shown on the status page and at `GET /api/version`;
the backend checks GitHub daily and shows an "update available" banner
(disable with `UPDATE_CHECK=0`). To upgrade, run `bin/upgrade.sh`.

## [1.2.1] — 2026-07-28

A full code-review pass over the backend. All fixes — no config changes needed.

### Security
- **AmbientWeather API keys no longer reach the logs.** AWN takes the keys as
  query params and httpx's error message embeds the full URL, which the pollers
  logged via `log.exception` — so any AWN 401/429/5xx wrote **both credentials in
  plaintext** to your logs. The client now raises a scrubbed error (status +
  path only). If you run the AWN poller, consider rotating your keys.
- **`/metrics` no longer publishes full MAC addresses.** It's open when enabled,
  so it now masks them to the last two bytes, matching the status page.

### Fixed
- **Smart alerts never fired.** The pressure-tendency lookup hit a rain-only
  assertion, raising on every check and taking frost + heat down with it.
  `SMART_ALERTS=1` now works as documented.
- **Wind roses could point the wrong way.** Wind direction is modular, so
  averaging 355° and 5° gave 180° — due *south* for a north wind. Bucketed
  history (>6h windows) now uses a circular mean.
- **Real wind gusts were being discarded.** The glitch guard compared a gust
  against `4 × sustained`, so when sustained wind read 0 — a squall front
  hitting a calm station — every gust above the floor was dropped. The
  maintenance cleaner had the same flaw and was deleting them permanently.
- **`/api/devices/{mac}/records` could return an empty body.** A request landing
  while a background computation was in flight got a 200 with `{}` (blank
  Records screen). Unknown MACs now 404 instead of populating an unbounded cache.
- **Charts could end early.** A short, busy window hit the row limit and dropped
  the *newest* rows.
- **The public status page could 500** on a non-finite wind direction.
- **MQTT no longer blocks startup.** `connect()` ran on the event loop, so an
  unreachable broker stalled all serving/ingest, and a failure left MQTT dead
  until redeploy. It now connects off-loop and retries with backoff.
- Rain rollups no longer surface a "wettest day" derived from a non-resetting
  cumulative counter, and `bin/maintenance` can purge existing artifacts.

### Internal
- Test suite no longer reaches the network (it was falling back to `.env` and
  polling the live AWN API): **131s → 4.5s**, 193 tests.

## [1.2.0] — 2026-07-17

### Added
- **Records & extremes.** New `GET /api/devices/{mac}/records` returns per-metric
  highs & lows — with the local time each was set — over today / this month /
  this year / all-time (temp, feels-like, dew point, humidity, pressure, wind,
  gust, UV, solar, rain). The public dashboard gains an all-time **Records**
  strip (hottest, coldest, peak gust, wettest day, high/low pressure).
- **Smart alerts** (opt-in, `SMART_ALERTS=1`). Weather-intelligent alerts that
  need no threshold config, delivered over the same email/push channels:
  **frost/freeze risk** (`SMART_ALERT_FROST_F`, default 35°F), **dangerous heat**
  (`SMART_ALERT_HEAT_F`, default 105°F feels-like), and a **rapid pressure drop**
  (`SMART_ALERT_PRESSURE_DROP_INHG`, default 0.06 inHg over 3h → storm approaching).
  Edge-triggered like threshold rules.
- **Prometheus `/metrics`** (opt-in, `PROMETHEUS_METRICS=1`). Every device's
  latest reading as Prometheus gauges — point Prometheus/Grafana at it for
  dashboards and alerting.
- **MQTT publishing with Home Assistant auto-discovery** (set `MQTT_HOST`). Each
  reading is published to `<prefix>/<node>/state` and retained HA discovery
  configs make every sensor appear in Home Assistant automatically, with the
  right units/device-classes. Config: `MQTT_PORT`, `MQTT_USERNAME`,
  `MQTT_PASSWORD`, `MQTT_TOPIC_PREFIX`, `MQTT_DISCOVERY_PREFIX`.

## [1.1.1] — 2026-07-16

### Security
- **LilyGO firmware: no anonymous re-provisioning after a token wipe.**
  Previously, when the backend rejected 5 consecutive posts with 401 the board
  wiped its token **and** cleared the `provisioned` flag, dropping back to the
  unauthenticated bootstrap state — a window where anything on your LAN could
  `POST /provision` and silently repoint the board at a hostile backend. The
  board now stays **locked** after a wipe. Re-pairing requires a per-device
  **setup key**: a random 8-char secret minted on first boot, stored in NVS
  separately from the token (so it survives wipes), shown on the OLED (only
  while a re-pair is pending) and the serial boot log, and never exposed over
  HTTP. `/provision` accepts either the current ingest token or the setup key.
  **Self-hosters running a LilyGO relay should reflash** (`pio run -e
  t3_v161_433 -t upload` / `_915`); existing Wi-Fi + backend creds are
  preserved across the flash.

## [1.1.0] — 2026-07-15

### Added
- **Public dashboard** (opt-in, `PUBLIC_DASHBOARD=1`). The status page at `/`
  can show a live, read-only view of your station — current conditions plus
  inline 24-hour charts and a link to the iOS app — in place of the app
  screenshots. Fully server-rendered (no client JS, no public data API; `/api/*`
  stays token-gated). Configure which station(s) with `PUBLIC_DASHBOARD_MACS`
  (unset = primary only, `all`, or a MAC allowlist) and which metrics with
  `PUBLIC_DASHBOARD_FIELDS` (default: temp, humidity, wind, pressure, rain).
  The temperature chart overlays the feels-like line, and a **wind rose**
  (16-sector, stacked by speed) rides alongside the wind chart. Page
  auto-refreshes every 2 minutes.

### Fixed
- **Rain rollups fall back to the monthly counter when the yearly counter is
  broken.** After a WeatherLink Jan-1 year reset, a stale yearly-rain baseline
  could clamp the derived weekly total to 0 even while the month showed rain.
  The rollup now detects a broken yearly counter and derives weekly/daily from
  the monthly counter instead.
- **Rain charts now catch sub-hundredth increments** from SDR sources by
  deriving `hourlyrainin` from the cumulative `yearlyrainin` deltas.

## [1.0.0] — 2026-07-13

First formally versioned release. Everything the backend has shipped to date,
now with a version + update mechanism.

### Added
- **Versioning + update check.** `GET /api/version` and the status page report
  the running version; a daily check against GitHub Releases surfaces an
  "update available" banner (opt-out `UPDATE_CHECK=0`).
- **`bin/upgrade.sh`** — one command to pull the latest and redeploy (Fly.io or
  Docker); the SQLite schema auto-migrates on boot.
- **Published Docker image** at `ghcr.io/volneydouglas/zasder-weather-backend`
  so Docker upgrades are `docker compose pull && up -d` (no local rebuild).
- Push notifications to **Android via FCM** (alongside iOS APNs), split by
  platform in the alert monitor.
- **LilyGO discovery mode** (`forward_all`) — post any decoded weather station
  (~180 rtl_433 protocols), not just Atlas/Fine Offset.
- Global request-body size limit (anonymous DoS guard) and constant-time token
  checks across all auth gates.

### Fixed
- **Rain chart missed light rain** from SDR sources: `/history` now derives the
  rain series from the cumulative `yearlyrainin` counter (those stations never
  post `hourlyrainin`), so even 0.01" shows up. Retroactive.
- Starlette bumped to fix the `/static` Range-header DoS (GHSA-7f5h-v6xp-fcq8).
- Public status page no longer discloses device location labels / full MACs;
  the read-only reviewer token can't read captures / discoveries / meters.

[1.0.0]: https://github.com/volneydouglas/zasder-weather-backend/releases/tag/v1.0.0
