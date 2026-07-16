# Changelog

All notable changes to the Zasder Weather backend. Format based on
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

The running version is shown on the status page and at `GET /api/version`;
the backend checks GitHub daily and shows an "update available" banner
(disable with `UPDATE_CHECK=0`). To upgrade, run `bin/upgrade.sh`.

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
