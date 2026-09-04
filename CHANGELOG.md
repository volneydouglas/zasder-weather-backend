# Changelog

All notable changes to the Zasder Weather backend. Format based on
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

The running version is shown on the status page and at `GET /api/version`;
the backend checks GitHub daily and shows an "update available" banner
(disable with `UPDATE_CHECK=0`). To upgrade, run `bin/upgrade.sh`.

## [2.0.1] — 2026-09-04

### Fixed
- **The rain-start countdown card now ends.** The Live Activity that
  counts down to a forecast rain onset was started with a single push and
  left to expire on its own dismissal date, which ActivityKit only honours
  on end events. A card for rain that never came stayed on the Lock
  Screen until iOS's eight-hour cap. The server now ends it explicitly an
  hour after the predicted onset, or as soon as a storm episode opens on
  any station and the Storm Watch card takes over. Silent either way, one
  attempt, and a transport failure does not retry every tick. Server-side
  only: no app update needed.

## [2.0.0] — 2026-09-03

### Added
- **Story cards.** `GET /api/devices/{mac}/stories` returns finished,
  shareable weather stories written server-side in the reader's own
  units: heat and cold ledgers, wildest day, dry spell, humid month,
  water year, daylight, tonight's sky, growing season, storms that broke
  the heat, biggest swing, degree days, fire weather, the barometer's
  call, the shape of a year, the humidity tax, and new this build, **the
  forecast versus the backyard**: how far the day-ahead high and low ran
  from what your station measured last month, and how the rain calls
  graded. Producers decline rather than pad, so a young station gets
  fewer cards, not emptier ones. 404 when Insights is off.
- **A daily Zambretti ledger.** Once a day at 09:00 station time the
  barometer's call is written down as it was made, never revised, so a
  future scorecard can grade the 1920 slide rule against what happened.
  Thinning erases the pressure it was read from; the ledger keeps the
  call.
- **Rename any station.** `PUT /api/devices/{mac}/name` stores an
  operator name that wins over whatever the source posts (an Ecowitt
  gateway only knows its model, so it arrived as "Ecowitt (GW3000B)").
  One name everywhere: the device list, alerts, storm summaries, the
  morning report, story cards, the public page and `/metrics`. Blank
  goes back to the station's own name; the config backup carries the
  renames. In the app, the pencil on Settings, Stations.
- Indoor dew point (`dewPointin`) derived at ingest for consoles that
  report indoor temperature and humidity but no indoor dew of their own.
- **Ecowitt cloud poller (Path I).** An Ecowitt gateway can now feed an
  HTTPS-only backend through ecowitt.net instead of a LAN forwarder:
  `ECOWITT_APP_KEY` + `ECOWITT_API_KEY` (or Settings → Integrations →
  Ecowitt Cloud in the app) polls every weather station on the account
  once a minute, backfills the last day on first start, and carries the
  same batteries, sensor channels and tipping-gauge-over-haptic rain
  rule as the local `/ingest/ecowitt` path. Devices are keyed by their
  real MAC; the local path stays the recommended door on a LAN.
- **Govee CO₂ monitor (Path J).** A GoveeLife H5140 (or air-quality
  sibling) feeds the backend through Govee's Platform API: `GOVEE_API_KEY`
  (or Settings → Integrations → Govee in the app) polls every Wi-Fi air
  monitor on the account once a minute. CO₂, temperature and humidity,
  and PM2.5 where the model has it, land in the same columns an
  AirGradient fills; each monitor is its own `5D:5D:08:…` device with the
  air card. A monitor with no particle sensor gets a CO₂ hero and a
  24-hour CO₂ chart instead of "No PM data", in the app and on the
  public page.
- **The Comfortable Months.** A story card that ranks the calendar by how
  much of a waking day (7 am to 10 pm) the feels-like temperature sat
  between 60 and 80 °F, this year beside the record, from a new
  year-keyed comfort ledger (`comfort_rollups`) folded at ingest. An
  existing archive fills it with one background rebuild at first boot.
- **The morning report at a minute you choose.** `digest_minute` beside
  `digest_hour` on `PUT /api/alerts` (0 to 59; the app's Send around is a
  clock picker now), so the report can go at 7:29 instead of on the hour.
- **Server backups carry every alert preference.** Backup format 2:
  storm settings, rain start and heat day, quiet hours, digest hour and
  minute, per-device storm summaries and rule severity all round-trip;
  a format-1 file still restores.
- **`trim-head`.** `python -m app.maintenance trim-head --mac M
  --before-ms T --apply` drops a station's first readings from before it
  was outside (the sensor that spent its first hour on a desk), backs
  them up in full beside the database, and refolds that station's
  rollups.
- **Air monitors on the public page.** An AirGradient (or any air-only
  device) named in `PUBLIC_DASHBOARD_MACS`, or included by `all`, now
  renders its own air card: PM2.5 with its US EPA 2024 band (Good through
  Hazardous), PM10, CO2 with 1000 and 2000 ppm called out, TVOC and NOx
  indexes, temperature and humidity when the monitor reports them, and a
  24-hour PM2.5 chart. No weather hero, wind, rain, pressure or records for
  a monitor, and `PUBLIC_DASHBOARD_FIELDS` keeps applying to weather
  stations only. `/embed` carries the same card. The apps' Public web page
  picker lists monitors with an "Air quality" caption; until now they were
  hidden from it and dropped by the page.

### Fixed
- **History thinning no longer stalls ingest.** The first real pass over
  a multi-year archive held the database's single writer for minutes per
  step, and every station post in that window answered 503. Thinning is
  now a nightly batch job: it runs only inside a quiet-hour window you
  choose (`HISTORY_THIN_WINDOW_START`, default 02:00 station-local, for
  at most `HISTORY_THIN_WINDOW_MINUTES`, default 120), deletes a couple
  of thousand rows per short transaction with a pause between them,
  shrinks the batch on its own when a step runs long, backs off when the
  database is busy, and resumes the next night exactly where it stopped.
  The JSON trim (`HISTORY_JSON_DETAIL_DAYS`) runs in the same window
  with the same bounded steps, sharing the night's minutes. A big
  archive takes several nights the first time; readings keep flowing
  throughout. It never runs at boot. `GET /api/history-retention`
  gains the window knobs (also settable from the app) and a
  `thin_progress` document with `nights_remaining`; the server logs one
  summary line per night.
- **Ingest no longer fails while a large archive rebuilds its chart
  index.** The deferred rebuild held the database's single writer for
  minutes on big archives, and every write in that window (station
  posts, push-relay challenges) answered 500 "database is locked". The
  index is now built under a new name and swapped in afterwards, so
  charts stay covered during the build, and station posts that arrive
  while it runs are held in memory and written in order once it
  finishes (`{"queued": true}` in the response). A lock that still
  wins answers 503 with `Retry-After: 5` instead of 500, so relays and
  boards can back off.
- **A rollup rebuild no longer starves every other writer.** The
  boot-time rebuild committed per batch and re-took the lock at once, so
  for its whole run ingest, push registration and the alert tick
  answered "database is locked". It now folds 1,000 rows at a time and
  yields half a second between batches; a big archive takes minutes
  longer and drops nothing.
- A reading parked behind the chart-index rebuild and answered 200 was
  dropped if its replay failed for any reason other than the lock; it
  is re-parked and retried, three times, before being dropped with the
  reason.
- Anonymous status-page hits past the count cache's expiry each spawned
  their own full COUNT(*); one recount per station now, reaped at
  shutdown with every other app-owned task.
- An expired database snapshot could still be downloaded by a direct
  GET; it now answers 410 and is removed.
- Ecowitt Cloud: a stale outdoor-temperature timestamp no longer freezes
  the whole reading (the newest core group stamps it and a group more
  than 15 minutes behind is left out), and the 24-hour bootstrap window
  is sent in the device's own zone rather than UTC.
- A bearer or ingest token containing non-ASCII bytes answered 500
  instead of 401.
- A migration killed mid-backfill (the lightning columns, the storm
  capture) could leave its columns present and the backfill skipped
  forever; both now record a pending key and resume on the next boot.
- The widget-refresh push no longer lets a failed dead-token prune
  re-send reload pushes every tick for the length of a database hiccup.
- Indoor dew point now has the same plausibility band as the outdoor
  one, whether the console sent it or the server derived it.

### Docs
- The public README's API table and the module layout in AGENTS.md now
  list the stories endpoint and the story and almanac modules.

## [1.9.1] — 2026-08-30

### Fixed
- **The embed's scripts actually run now.** The security policy
  (`script-src 'self'`, correctly strict since it shipped) silently
  blocked the public pages' two inline scripts in every browser: the
  loading spinner never faded once it appeared, and the embed's
  auto-height messages were never posted — iframes never auto-sized, in
  any browser, ever. The exact script bodies are now allowed by sha256
  hash (no `unsafe-inline`), and a test hashes the scripts as actually
  served so a future edit can't silently regress. If you iframe your
  `/embed` with the auto-height snippet, it starts working with this
  release — you can drop any fixed iframe height.

## [1.9.0] — 2026-08-29

### Added
- **Ecowitt gateways post directly** (`POST /ingest/ecowitt`): point any
  GW1000–GW3000 gateway or console's "Customized" upload at your backend —
  no vendor cloud, no extra hardware. Metric consoles are converted on
  ingest, per-sensor battery flags feed the health watcher, and on
  dual-rain stations the tipping gauge wins over haptic rain (which
  phantom-tips when the mast gets bumped). See README Path G.
- **WeeWX bridge** (`weewx-bridge/`): a small extension that POSTs every
  archive record from an existing WeeWX install to `/ingest/custom` —
  any of WeeWX's 70+ station families rides along. See README Path H.
- **AirGradient LAN polling** (`AIRGRADIENT_LOCAL_HOSTS`): LAN-local
  backends can poll AirGradient monitors' local API directly — no cloud
  token needed. The cloud-token integration from 1.8 still works
  everywhere.
- **Morning report**: at your chosen hour, the daily digest becomes a
  full weather report — yesterday's numbers per station, the overnight
  alert log with severity dots, and today's outlook — as a formatted
  HTML email (plain-text alternative included) plus a compact morning
  push. With the iOS app, the same report lands as a lock-screen Live
  Activity card. Set the hour in the app (Settings → Notifications);
  push-only installs get the phone half without any SMTP config.
- **Climate endpoints** (ride the insights rollups, on by default):
  `GET /api/devices/{mac}/climate?year=` — twelve month rows
  (means, extremes with dates, rain, heating/cooling/growing degree
  days) plus annual totals and the running water year
  (`WATER_YEAR_START_MONTH`, default October);
  `GET /api/devices/{mac}/daily-series` — one row per local day for
  year-span charts; `GET /api/devices/{mac}/reports/noaa?year=[&month=]`
  — the classic NOAA-style fixed-width climate report as plain text.
- **Week records**: `/api/devices/{mac}/records` gains a `week` period —
  the trailing 7 local days, matching the charts' "7d" grammar.
- **Storm history**: `GET /api/devices/{mac}/storms` returns the
  structured stats behind recent storm summaries (total, peak rate,
  temps, gust, duration), newest first.
- **Write-access share links**: a second share tier the app can mint —
  station operations only (rename/relocate a device, alert toggles,
  threshold rules, push registration for the holder's own phone).
  Everything administrative stays owner-only, and every write by a
  shared link is attributed in an audit log (`GET /api/write-audit`:
  who, what, when — label + token tail, never the credential).
- **History aging** (all optional, off by default): thin raw rows older
  than `HISTORY_DETAIL_DAYS` to one per `HISTORY_KEEP_INTERVAL_MINUTES`,
  and/or drop the raw JSON payload past `HISTORY_JSON_DETAIL_DAYS`
  while keeping every row's typed columns. Daily rollups keep every
  day's true extremes either way. App-managed at
  `GET/PUT /api/history-retention` (app-stored values win over env);
  `GET /api/storage` breaks down where the database's bytes live.
- **Major alert tier**: threshold-rule urgency is now
  minor / standard / major / urgent. Major ignores quiet hours as a
  normal notification; urgent additionally arrives Time Sensitive
  (breaks through iOS Focus). Unknown future tiers round-trip verbatim
  through older clients instead of being downgraded.
- **Graceful major upgrades**: a release can vouch that it installs
  hands-free from your version (`upgrade.json`, `seamless_from`), which
  unlocks the one-tap update path across a major-version boundary.
  Unvouched majors still require the classic follow-the-release-notes
  upgrade. Automatic updates never cross majors regardless.
- **Disk-space visibility**: `/api/version` carries a disk block
  (total/free/used %), and a `disk_low` alert warns before the volume
  fills.
- 37 new typed columns from a field survey (soil temperature probes,
  leaf wetness, lightning details, and more), so those readings chart
  and record without the raw-JSON payload. Piezo (haptic) rain folds
  into the existing rain columns as a fallback.
- Insights rollups are ON by default (set INSIGHTS=0 to opt out): the
  climate endpoints, fast records on large archives, and history
  thinning all ride them, and a needed backfill now self-schedules in
  the background on boot instead of requiring a manual rebuild call.

### Changed
- Live Activity push-to-start tokens are stored app-wide, fixing
  starts for every activity type (storm/heat/rain/morning) — iOS hands
  every type the same token, and the old per-activity storage let the
  last registration win.
- Historical-import column backfill runs in smaller chunks, bounding
  the write-lock hold on small machines.
- The storm-summary notification's stat line is restructured to fit a
  single lock-screen banner line (`Hi 80°F | Lo 70°F | Gust: 25 mph`),
  and one-sided temperature data (sensor up for only part of the storm)
  renders instead of being dropped.

## [1.8.2] — 2026-08-26

### Fixed
- One-tap and automatic self-updates now authenticate correctly against the
  Fly Machines API. Deploy tokens (`fly tokens create deploy`) are macaroon
  tokens, which Fly accepts under the `FlyV1` scheme; the updater was
  sending them as `Bearer`, so the update step could fail with an auth error
  on servers provisioned by `setup-fly.sh`. Either token form stored in
  `FLY_API_TOKEN` (with or without the `FlyV1 ` prefix) now works.

## [1.8.1] — 2026-08-26

### Fixed
- Upgrading a large archive no longer risks a startup crash-loop: the
  one-time chart-index rebuild that 1.8.0 ran during boot could outlive
  a platform health-check window on million-row databases (the machine
  was killed mid-CREATE and restarted into the same rebuild forever).
  Archives past ~200k rows now boot immediately and rebuild the index
  in the background; charts are slower but correct until it completes.

## [1.8.0] — 2026-08-26

### Added
- Smart weather alerts: rapid temperature drops, wind ramps, sustained
  pipe-freeze cold, and gust-front (outflow) signatures — edge-triggered
  with re-arm deadbands, and co-firing kinds group into a single "front
  passage" notification. First-frost-of-the-season one-shot per station.
- Per-rule urgency on threshold alerts (minor / standard / urgent):
  urgent breaks quiet hours; minor stays quiet overnight and rides the
  daily digest. Severity is stored on the alert history and carried in
  webhook payloads.
- Quiet hours (below-warning pushes hold overnight) and a daily digest
  email summarizing everything that fired since the last one.
- NWS alert relay: severe/extreme government alerts push through your
  own channels, deduplicated globally across co-located stations.
- Lightning proximity alerts (episode-based, closer-strike re-alerts,
  30-minute all-clear) for lightning-capable stations.
- Station health watchdogs: sensor batteries (low + recovered), sensors
  gone quiet, pegged-humidity and seized-anemometer flatline detection.
- Storm Watch and Heat Day Live Activity feeds, including a manual
  storm-watch start endpoint for light-onset storms.
- Community upload fan-out: PWSWeather, Windy, WeatherCloud, and CWOP
  with per-target cadence and last-send health on each row.
- Outbound webhooks: every alert POSTs HMAC-signed JSON to registered
  https endpoints (SSRF-guarded), with pause/resume.
- CSV export: every stored column for a station over a range, streamed.
- Derived metrics endpoint: wet bulb, frost point, Delta-T, Fosberg and
  Chandler fire indices, density altitude, pressure tendency, and the
  Zambretti forecaster.
- Forecast snapshots stored as issued (~6h cadence) for future accuracy
  scoring.
- AirGradient air-quality integration: one account token polls every
  monitor; each location becomes its own device with PM1/PM2.5/PM10,
  CO2, and TVOC/NOx index columns. Air monitors are excluded from
  weather-station machinery (uploads, forecast location, NWS polling,
  public page) by design.
- Read-only MCP server (`mcp/`): ask an AI assistant about your
  stations, current conditions, derived metrics, records, and recent
  alerts over the token-gated API.
- iOS 26 push-updated widgets support and per-activity Live Activity
  token scoping.

### Changed
- The status page's per-device row counts are cached (stale-while-
  refresh), taking the anonymous front page from seconds to ~0.2s warm.
- Uploads, forecast snapshots, and widget refresh nudges run
  independently of email/push configuration, and registered webhooks
  count as an alert delivery channel of their own.
- Delta-based alerts and the pressure tendency skip windows that span a
  station outage instead of computing across the gap.

### Fixed
- CWOP connects survive blackholed IPs in the APRS-IS rotation and run
  on uvloop; humidity is clamped to the encodable range.
- Wet bulb reads as the dry-bulb temperature at saturation instead of
  vanishing in fog and rain.
- One malformed stored coordinate no longer skips NWS polling for every
  station, and forecast snapshots tolerate the same.

## [1.7.1] — 2026-08-25

### Fixed
- The public dashboard and `/embed` no longer make a visitor wait for a
  full page rebuild after a quiet spell. A cold rebuild can take several
  seconds on a large history; the server now returns the previous page
  instantly and rebuilds in the background (pages older than 15 minutes
  still rebuild in the foreground). First reported as an embed that
  "takes 10 seconds to load."

## [1.7.0] — 2026-08-25

### Added
- **Rain-start nowcast** (opt-in, `PUT /api/alerts {"rain_start": true}`):
  polls Open-Meteo's 15-minute precipitation model for your primary
  station's location and sends one alert when rain is expected within the
  hour — your station then confirms the real thing. On iOS 17.2+ the same
  event starts a **Live Activity**: a Lock Screen / Dynamic Island
  countdown to the onset, self-expiring after the event. Push-to-start
  tokens register at `POST /api/push/live-activity-token`.
- **Per-device ingest tokens.** Mint a revocable credential per sending
  device (`/api/ingest-tokens`: create/list/rename/reveal/revoke) — valid
  everywhere `INGEST_TOKEN` is, never as an API token. Revoking one board
  no longer unpairs the fleet, and the shared token keeps working.
- **Token auto-upgrade.** A device posting with the shared token can send
  `X-Token-Upgrade: request` and receives its own token in the ingest
  response — idempotent per device, self-healing after a device wipe,
  capped, and never issued to devices still in probation. The LilyGO
  firmware in this repo does this automatically on fresh flashes.
- **Alert history.** `GET /api/alerts/recent` lists what fired and when
  (device-down, rules, smart, storm, nowcast), backed by a capped
  `alert_log` table.
- **Alert rule editing.** `PATCH /api/alerts/rules/{id}` updates a rule's
  threshold or target station in place, resetting its trigger state.
- **Storm-summary controls.** Per-station mute
  (`PUT /api/devices/{mac}/alert {"storm_summary": false}`) and a
  delivery-channel choice (`storm_channels`: push/email/both). Summaries
  now include the gust front that arrives ahead of the rain window.
- **Read-only capability probe.** `GET /api/session` reports
  `can_write` + `forecast_source` so apps on a share token hide
  owner-only controls; limited reads get town-rounded coordinates
  (1 decimal) so sun times and NWS alerts work for guests.
- Database backup endpoints, rollup-derived rain periods for
  daily-counter stations (with request caching), NCEI climate normals,
  heat/cold distribution insight bands, and an Open-Meteo minutely
  proxy.

### Changed
- **Threshold alerts re-arm only after 15 minutes of continuous
  clearance** — instantaneous wind samples used to re-arm a rule through
  the deadband and fire every few minutes all afternoon.
- Records/summary aggregates ignore non-numeric values stored by upstream
  glitches; the AWN poller sanity-bounds timestamps like `/ingest/custom`.
- The hosted push relay accepts an optional `push_type`/`payload` for
  Live Activity delivery (older relays reject the new fields loudly).

### Fixed
- An `Infinity` alert-rule threshold could persist and permanently break
  `GET /api/alerts/rules`; rule creation now validates finiteness.
- An intermittent 500 on the backup endpoints (a filesystem race on
  SQLite's WAL sidecars), and a backup interrupted by a restart no longer
  wedges the job in "running" forever.
- Deleting a device now clears its storm tracker and probation state — a
  re-registered station no longer inherits an open storm.

## [1.6.2] — 2026-08-21

### Added
- **Light mode for the public pages.** The status page, public dashboard,
  and `/embed` now follow the visitor's system appearance (dark remains
  the default look), and `/embed?theme=light|dark` pins the palette so an
  embedded dashboard can match the page it sits on.
- **App-controllable sharing.** `GET/PUT /api/public-dashboard`
  (owner-token) reads and sets the public page's switch, station
  selection (primary only / `all` / a MAC list), and location label —
  stored server-side, winning over the env values, so the 1.7 apps can
  offer a proper sharing screen. Changes apply immediately (the page
  cache is busted on save).

## [1.6.1] — 2026-08-21

### Added
- **Embeddable dashboard.** `GET /embed` serves the public dashboard alone
  — no status chrome — with framing allowed, so you can put your weather
  inline on your own website with a single iframe:
  `<iframe src="https://YOUR-APP.fly.dev/embed" width="100%" height="1300"
  style="border:0"></iframe>`. Only exists when `PUBLIC_DASHBOARD=1`
  (404s otherwise); every other page keeps its strict anti-framing
  headers. Auto-refreshes every 5 minutes.

## [1.6.0] — 2026-08-20

Data quality, a new station source, records that answer instantly, and a
lot of "the app should not tell you about hardware you do not own".

### Added
- **Lightning.** Tempest strike data is captured (per-interval count, the
  trailing 1h/3h windows, nearest-strike distance), stored in real columns,
  charted, and kept as records ("most strikes in an hour"). Existing
  databases backfill from the raw blobs on first boot, so a storm captured
  before the upgrade still counts. Stations with no detector show nothing —
  never a confident zero.
- **Records from the daily rollups.** Month/year/all-time records are
  answered from pre-folded daily rollups instead of scanning the whole
  archive — a 1M-row archive went from a 110-second timeout to instant.
  Today keeps exact record times; rollups are only trusted when they cover
  the period end-to-end (stale or partial rollups fall back to the raw
  scan). Rain periods (week/month/year) also derive for stations that only
  report a daily counter, like the Tempest.
- **Share read-only access from the app.** Settings mints a per-person
  guest token and hands you a one-tap link; name each link when creating
  it, see every link you've handed out, and revoke one person from the
  app without touching the others (`POST/GET/DELETE /api/guest-tokens`).
  Share recipients get the weather, never the operator view — no SMTP
  identity, no alert recipient emails, no coordinates (the forecast
  endpoint's location echo is stripped for guests too).
- **Integrations in the app.** AmbientWeather, Davis WeatherLink and
  Tempest credentials can be configured from Settings
  (`/api/integrations`); the matching poller starts, restarts or stops
  immediately, no redeploy. Values live on your volume and win over env,
  like the WU key always has.
- **Automatic updates (opt-in).** With `AUTO_UPDATE=1` and an app-scoped
  deploy token, a Fly instance applies an official release about two days
  after it ships — same-major only, never a downgrade, and only after
  verifying the release image actually exists. The setup script offers it
  as a yes/no.
- **The public dashboard carries the app's summary boards** — the 24h
  high/low/gust strip and rain by period — so sharing your station's page
  replaces a screenshot.
- **WeatherFlow Tempest support.** A cloud poller (`TEMPEST_TOKEN` +
  `TEMPEST_STATION_ID`, both free from the Tempest web app under Data
  Authorizations) that reads the station's own coordinates and name, so a
  Tempest gets a working forecast and sunrise without any further setup.
  Note that the Tempest REST response is **metric** regardless of what the
  `station_units` block advertises.
- **Storm summary alerts.** One notification a set time after the last
  reported rain, summarising the whole event — duration, total, peak rate,
  temperature range and top gust — instead of alerting during it.
  `STORM_SUMMARY`, `STORM_SUMMARY_QUIET_MINUTES`,
  `STORM_SUMMARY_MIN_TOTAL_IN`, and configurable from the app.
- **Read-only guest tokens** (`GUEST_API_TOKENS`, comma-separated) for
  sharing your station with family. Accepted on reads and refused on every
  write, and each one is revocable on its own.
- **Setup codes.** `setup-fly.sh` now prints a single code carrying the
  backend URL and token, so nobody has to retype a 64-character hex string
  into a phone. It prints a separate read-only share code too.
- **The written forecast** from The Weather Company is passed through
  (`daypart.narrative`), for the app to show above the six-day strip. It was
  already in the payload being fetched and was simply discarded.
- Alert rules and smart alerts now report their firing state on
  `GET /api/alerts` and `/api/alerts/rules`, so a client without a push
  channel of its own can raise them locally.

### Fixed
- **The setup script's auto-update token was broken on arrival.** It
  stripped all whitespace from the Fly deploy token, but the interior
  space in `FlyV1 fm2_…` is part of the credential — every scripted
  opt-in stored a token the platform rejects. Now preserved (and passed
  via stdin, never argv).
- **The public page's 24h board understated every extreme.** The window
  is served as 1-minute averaged buckets, and the board took the max of
  the averages — a 21 mph gust could render as 12. It now reads the
  per-bucket true extremes.
- **Repaired data heals the Records screen.** Data repairs mark the
  rollup ledgers dirty; records fall back to raw scans (correct, slower)
  until a background rebuild — kicked off automatically at boot —
  re-folds history. A cleaned wind spike no longer lives on as a
  displayed record.
- **Saving wrong cloud-source keys is no longer a silent success.** The
  server tries the credentials once on save and the app shows the
  failure next to the field instead of an "On" that never produces data.
- **Cleared tokens stay cleared on the Mac.** The pre-1.6 login-keychain
  copy resurrected a deleted API or ingest token at relaunch — the
  poller could quietly resume posting with a credential you removed.
- **Sustained wind can no longer exceed its own gust.** The plausibility
  bands are a per-field check, so wind garbage landing inside every band
  walked straight through. An internal-consistency check now condemns the
  whole speed set when a reading contradicts itself, and
  `clean_implausible` finally applies the anemometer-sibling rule the live
  ingest path already had — the asymmetry that left in-band 51-55 mph
  "sustained" winds behind after a 255 mph gust was swept.
- `maintenance.clean_wind_inconsistent` retro-applies the same rule to
  stored history.
- A station with **no solar sensor** no longer reads as permanent night.
  `solarradiation` defaulting to zero meant a moon and the word "Night" at
  noon, forever, for most Davis and many Ecowitt units.
- **A colliding neighbor sensor can no longer rebaseline your rain
  counter.** The level-shift confirmation now requires the new level to
  hold for five minutes of posts (the guard evaluates every relay post,
  not just stored rows), and a level that falls back to the old baseline
  is remembered and refused for a day — a real level shift never reverts.
- **Storm summaries no longer fabricate back-dated storms** after the
  checker was off: a counter baseline older than six hours rebaselines
  silently instead of counting weeks of accumulated rain as one event.
- **Device probation forgets cold trails.** Corrupt-packet sightings
  spread further apart than the TTL no longer accumulate to admission, so
  a recurring bit-flip can never slowly mint a phantom station.
- Daily-rain derivation refuses lifetime cumulative counters and handles
  DST week/month boundaries exactly; a Tempest reading whose only content
  is lightning is stored, not discarded.

### Changed
- Storm and alert preferences resolve app-managed values over environment
  defaults, matching how the SMTP transport already worked.

## [1.5.1] — 2026-08-15

Data-quality fix for imported history. Weather Underground serves 255
(`0xFF`, the single-byte "no reading" sentinel) as a literal wind speed
when a station's anemometer drops out. Those values were being stored as
real readings and taking over all-time wind records.

### Fixed
- **Wind plausibility ceiling lowered from 260 mph to 254 mph.** The band
  exists to reject decode garbage without ever clipping a real reading,
  so it was set above the 253 mph world-record gust — but that left 255
  inside the band, and the sentinel sailed through. 254 still clears the
  world record and rejects `0xFF` every time.
- **The Weather Underground importer now applies the plausibility bands.**
  It was the only write path into `observations` with no quality checks
  at all, so whatever an archive held was stored as fact.
- **A rejected wind value now also clears the other wind speed channels
  on that reading.** They come from one anemometer, so if it reported an
  impossible value it was faulting, and its remaining speed readings are
  not evidence either. Clearing only the out-of-band field left behind
  in-range garbage (89.7–213.3 mph "sustained" winds on rows whose gust
  had just been rejected) that the bands could never catch on a later
  pass. Wind direction is unaffected — separate sensor channel.

### Added
- `maintenance.clean_implausible()` retro-applies the plausibility bands
  to already-stored history for operators who imported before this
  release. Dry-run by default, streams a JSONL backup before writing, and
  clears values field-by-field — rows and days are never deleted, so a
  reading with one bad field keeps its good ones. Run
  `POST /api/insights/rebuild` afterwards, since the daily rollups hold
  their own per-field maxima and do not notice an observations edit.

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
