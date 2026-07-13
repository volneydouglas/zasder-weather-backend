# Changelog

All notable changes to the Zasder Weather backend. Format based on
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

The running version is shown on the status page and at `GET /api/version`;
the backend checks GitHub daily and shows an "update available" banner
(disable with `UPDATE_CHECK=0`). To upgrade, run `bin/upgrade.sh`.

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
