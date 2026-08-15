# Changelog

All notable changes to the Zasder Weather backend. Format based on
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

The running version is shown on the status page and at `GET /api/version`;
the backend checks GitHub daily and shows an "update available" banner
(disable with `UPDATE_CHECK=0`). To upgrade, run `bin/upgrade.sh`.

## [1.5.0] — 2026-08-13

The data-quality release: plausibility guards keep sensor glitches out
of your records, sensor drift becomes visible, and WU forwarding keeps
a Weather Underground station alive after vendor forwarding shutdowns.

### Added
- **Weather Underground live forwarding** (`PUT /api/devices/{mac}/wu-station`
  with `upload_enabled` + write-only `upload_key`): posts a station's
  readings straight to wunderground.com, throttled to 60 s per station,
  health surfaced in `GET /api/sources`.
- **Ingest plausibility bands** (`INGEST_PLAUSIBILITY_BANDS`, default on):
  per-field physical bounds beyond world-record extremes — decode garbage
  (bit-flip temperatures, negative rain, 3000 mph gusts) is nulled
  field-by-field before it reaches records, rollups or alerts.
- **Daily-rain + temperature spike guards**: the yearly-rain guard's
  rate×elapsed allowance and level-shift rebaseline now also cover
  `dailyrainin` jumps and impossible temperature steps
  (`INGEST_MAX_TEMP_JUMP_F`, default 40 °F + 60 °F/h accrued allowance).
  A persistent new level (sensor swap) is accepted on the second sighting.
- **Per-day temperature series** (`GET /api/insights/daily`, INSIGHTS-
  gated): rollup-backed lo/hi/mean per day — powers the app's sensor-
  drift card.
- **Elevation-based sea-level pressure correction**
  (`STATION_ELEVATION_FT` + `PRESSURE_ABSOLUTE_MACS`): absolute-pressure
  sensors (e.g. a WH32B over SDR) are corrected to sea level from the
  operator's real elevation; the true absolute reading is kept in
  `baromabsin`.
- Insights: rain-gap fields (last rain day/amount, current + per-year
  longest dry streak).

### Fixed
- WU import: single automatic retry on a transient transport error
  (same-day, budget-respecting), and millisecond-epoch timestamps from
  pre-2019 WU archives are normalized instead of rejected.
- Pressure correction is applied BEFORE the plausibility bands, so
  high-elevation stations aren't nulled by the sea-level band.
- First insert from the Mac app's WLL poller without an explicit name is
  labeled "Davis WeatherLink Live" (was "Davis Wll Local"); an explicit
  `device.name` is still the only thing that renames an existing station.

## [1.4.0] — 2026-08-12

The history release: import your Weather Underground archive, explore it,
and get server-side statistics — plus a TWC forecast option and a deep
security/robustness pass (99 review findings fixed).

### Added
- **Weather Underground history import** (`POST /api/import/wu` + status/
  cancel): day-by-day backfill of a station's WU archive into your own
  database. Idempotent, quota-aware (~1,400 calls/day with resume), dry-run
  mode, per-device station mapping (`PUT /api/devices/{mac}/wu-station`).
- **Insights** (opt-in `INSIGHTS=1`): daily/hourly rollups maintained at
  ingest + `GET /api/insights` — heat and cold ledgers (incl. frost-free
  season), rain year-over-year, monthly anomalies, temperature and
  feels-like month×hour grids, degree days. `POST /api/insights/rebuild`
  backfills rollups for existing data.
- **TWC forecast source** (`GET /api/forecast?source=twc`): 5-day forecast
  via a free WU PWS-owner key; automatic marked fallback to Open-Meteo on
  any failure. App-managed key storage (`PUT /api/config/wu-key`,
  write-only like the SMTP password) or `WU_API_KEY` env.
- **Email alert scope**: `email_scope=device_down` limits email to
  device-down alerts while push keeps everything.
- Ranged history (`end_ms`) for month browsing; battery status from relay
  sources now mapped end-to-end.

### Fixed
- 99 findings from a deep code review, including: API keys no longer
  leak into server logs via HTTP client logging; overflow/junk metric
  values can no longer permanently break `/records`/`/summary`; restore
  validates input before touching existing data; imports resume after
  transient network failures instead of re-burning quota; `database is
  locked` errors under concurrent writes (busy_timeout + batched
  rebuilds); many hardening and correctness fixes across ingest, alerts,
  relay, and the public dashboard.

### Changed
- `/history` accepts up to 745 hours (DST-long months).
- Setup scripts hide credential prompts; docs cover all new settings.

## [1.3.2] — 2026-08-11

Fast-follow to 1.3.1. If you configured the yearly-rain offset calibration
introduced in 1.3.1, upgrade now — it corrupted rain history in production
within an hour of release and has been removed.

### Fixed — data integrity
- **Yearly-rain offsets removed** (`INGEST_YEARLY_RAIN_OFFSETS` is now inert
  and ignored): applied to a station whose yearly counter already is true
  year-to-date, the offset clamped the real total to 0.0, and rows stored
  before an offset was configured used the unshifted scale, so
  year-over-history deltas went negative and yearly-rain records vanished.
  Raw counter values now pass through untouched.
- **A persistent rain-counter level shift no longer disables rain forever.**
  The ingest glitch guard rejects impossible jumps, but a genuine level
  shift (counter swap, station recalibration) previously kept every
  subsequent reading nulled. One corroborating reading at the new level now
  rebaselines the guard — and corroboration must arrive at least 90 s after
  the rejection, so rtl_433's duplicate decodes of a single radio
  transmission (or a neighboring sensor on a colliding radio ID) can't
  confirm themselves.
- **Yearly-rain history repair tool** (`app/maintenance.py`): repairs
  history corrupted by the removed offsets, streams its pre-repair backup in
  constant memory (the previous whole-table read could OOM small instances),
  and handles rows from before the first counter era boundary.

### Fixed
- Writes with a **valid read-only token** now return **403 with an explicit
  "this access token is read-only" message** instead of 401 "invalid token",
  which misread as broken credentials. Unknown tokens still get 401.

## [1.3.1] — 2026-08-11

Numbered 1.3.1 rather than 1.3.0 deliberately: a handful of early instances
were deployed from pre-release 1.3.0 code that predates the fixes below, so
the 1.3.0 string already exists in the wild attached to different code. If
`/api/version` says 1.3.0, upgrade — you have the early build.

Ships alongside Zasder Weather 1.3.0 for iOS, watchOS and **macOS** — from
this release the apps and the backend share one version number. This release
also absorbs three full code-review rounds (≈350 findings worked); the
data-integrity and security items below are the ones self-hosters will feel.

### Fixed — data integrity
- **Davis wind and solar were silently discarded at ingest** when fed by the
  bundled SDR relays: the relays sent the backend's *column* names
  (`windspeedmph`, `winddir`, `solarradiation`) where the ingest contract
  reads `speed_mph` / `direction` / `solar_wm2`. If you run `davis-relay` or
  the rtl_433 Davis path, update the relay too — wind and solar start
  appearing again.
- **A calm reading no longer suppresses the whole post**: relays treated
  0.0 mph (and 0 °F) as "no data" and skipped posting, stalling temperature
  and humidity until the wind picked up.
- **Cold-weather "feels like" was wrong end to end**: relays computed heat
  index regardless of temperature, and the backend prefers a source-provided
  feels_like. Heat index now applies only ≥ 80 °F, wind chill ≤ 50 °F.
- Non-finite readings can no longer poison a station: an overflow string like
  `"1e999"` used to ingest as `inf` and break `/current` and history JSON for
  that row's lifetime. Scrubbed at ingest AND at the storage choke point.
- Out-of-order posts no longer regress a device's `last_seen`/name/location
  (no more false stale alerts after a delayed packet).
- Timestamps get sanity bounds: far-future clamps to server time, ancient
  posts are rejected instead of stored.
- Deleting a device now also removes its location and alert state, so a
  re-registered MAC no longer inherits either.

### Fixed — alerts & push
- **A device-down alert whose first delivery failed was dropped forever**
  (state advanced before delivery). Delivery failures now retry on the next
  tick until one succeeds.
- Threshold alerts gained a re-arm deadband, ending flapping notifications
  when a reading hovers at the threshold.
- A missing or misspelled `APNS_ENV` no longer silently prunes every
  registered push token (`BadDeviceToken` on guessed environments is treated
  as config error, not a dead device). Same fix applied to the relay path and
  FCM (prunes only on `UNREGISTERED`).

### Security
- The WeatherLink API key no longer appears in logs on failed polls (it
  travels as a query parameter; error messages now carry the path only).
- The read-only reviewer/demo token can no longer read operator PII: station
  coordinates are stripped from `/api/devices`, and `/api/config/backup` is
  write-gated.
- The unauthenticated relay challenge endpoint is rate-limited per client IP
  (keyed on the edge-provided address, not spoofable `X-Forwarded-For`) with
  a hard cap on stored challenges.
- App Attest verification now checks certificate validity windows and
  requires the AT flag.

### Added
- **`GET /api/sources`** — health of each ingest source: configured or not,
  last success, last error (with credentials redacted). A poller that
  quietly stops is now distinguishable from dead hardware.
- **`GET /api/config/backup` / `POST /api/config/restore`** — server-side
  configuration backup (alert rules, prefs, device locations). Tokens and
  SMTP passwords are never included; restores validate before deleting.
- Ingest hardening: malformed JSON types return 400 instead of 500.

### Upgrading
`./bin/upgrade.sh` as usual. If a release edits `fly.toml`, the script now
carries your app/region pin across the pull and restores it even when the
pull fails. Update any bundled relays/pollers at the same time to get the
Davis field fix.


## [1.2.2] — 2026-08-09

Fixes found by a second review pass (CodeRabbit) after 1.2.1 shipped, plus a
round of setup fixes prompted by a self-hoster who got stuck. All fixes — no
configuration changes needed.

### Added
- **`wll-poller/bin/setup-macos.sh` — one-command WeatherLink Live setup on a
  Mac.** You no longer need a Raspberry Pi or Docker to run the WLL poller: any
  always-on Mac works. The script asks three questions, verifies each answer
  (it reaches your WLL, reaches your backend, and posts one real reading so a
  wrong token fails immediately instead of silently), then installs a launchd
  agent that starts at login and restarts itself. `--uninstall` reverses it.

### Fixed
- **`setup-fly.sh` accepted anything as an app name.** Pasting the next command
  from the README into the "App name:" prompt — an easy mistake, since the
  prompt looks like an ordinary Terminal line — sent that whole string to Fly,
  which rejected it with an unrelated-sounding *"Name blocked by abuse filter"*.
  App names are now validated against Fly's rules, and a pasted command gets
  told it's a command, not a name.
- **Setup docs assumed you knew how to edit a file from the Terminal.** A bare
  `# edit: WLL_HOST, BACKEND_URL, INGEST_TOKEN` comment was the only instruction
  for a required step. The READMEs now say which editor to use (`open -e` on
  macOS, `nano` on Linux), state plainly that `#` lines are comments, and say
  where `zasder-install-summary.txt` is written — with a `find` command for when
  it's lost.
- The `wll-poller` README and its unit test both claimed THSW was the preferred
  "feels like" source; the code has deliberately used heat index since it landed
  (THSW runs 5–10°F hotter than every other source in the app). The test was
  asserting behaviour the code doesn't have.
- **Smart alerts could cry wolf.** The 3-hour pressure-tendency lookup fell back
  to the earliest reading on file when nothing older than the window existed, so
  on a young device a "3h delta" could actually span minutes and fire a bogus
  storm alert. It now reports "not computable" instead. Rain rollups keep the
  earliest-row fallback they legitimately want.
- **`/metrics` could break an entire Prometheus scrape.** A non-finite reading
  rendered as `inf`, which isn't a valid sample value. (The public dashboard's
  copy of this guard was fixed in 1.2.1; the exporter had its own.)
- **Indoor temperature/humidity were missing from bucketed history**, so any
  client charting a window longer than 6 hours saw no indoor data — the iOS
  dashboard's indoor sparkline was blank. Both fields are now selected and
  covered by the chart index.
- **Temperatures on the public dashboard rendered without a unit** — "115" next
  to a "30.04 inHg" that had one.
- **A legitimate "wettest day" could be suppressed** if every reading in the
  period sat above the cumulative-counter threshold (a station that came online
  mid-downpour). The counter is now judged once over all history.
- The records cache could evict a lock that was still held, letting a duplicate
  computation run for the same device.

### Changed
- The public status page caches its rendered dashboard for ~100s and coalesces
  concurrent cache misses, so the one unauthenticated compute path stays flat
  under load instead of running a full 24h aggregation per request.
- Test suite no longer touches the network for push either (APNs/FCM/relay env
  is blanked alongside the cloud-poller keys).

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
