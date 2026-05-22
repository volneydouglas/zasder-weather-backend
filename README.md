# Zasder Weather — Backend

Self-hosted backend for the **Zasder Weather** iOS app. Ingests
observations from supported weather stations, stores them in SQLite,
and serves a small HTTP API the iOS app reads. Pairs Open-Meteo for
the 7-day forecast.

The iOS app is on the App Store. This repo is the server-side piece —
get it running somewhere your phone can reach (Fly.io, your home
network, a VPS) and point the app at it.

> [!IMPORTANT]
> **You need to run your own backend.** The official instance at
> `weather.zasder.com` is the developer's personal deployment and is
> **not** shared. Don't point your app at it — you'll get an
> `invalid token` error. Pick one of the paths below.

## Pick your path

Two independent decisions: **how you ingest data** (cloud API or
direct-RF SDR), and **where you host the backend** (Fly.io or LAN).

| Ingest method            | Works for                          | Cadence | LAN hardware?                 |
|--------------------------|------------------------------------|---------|-------------------------------|
| **AmbientWeather API**   | AWN-registered stations            | 60 s    | None — cloud-to-cloud         |
| **SDR direct** *(rec.)*  | AcuRite Atlas, Fine Offset (WS-2000, WH-31, WS-5000, WH-65) | 16–30 s | ~$30 RTL-SDR dongle + Pi (or any always-on Linux box) |
| **DNS-hijack relay** *(legacy)* | AcuRite Atlas via the AcuRite hub          | sensor-dependent | Pi running a small Docker container |

**Recommended for most people**: SDR direct + Fly hosting. Sensor data
flows: outdoor sensor → 433/915 MHz RF → SDR on your Pi → backend on
Fly.io → iOS app. No vendor cloud anywhere; survives any future cloud
shutdown by sensor manufacturers (AcuRite has been gradually killing
theirs).

| | **Backend on Fly.io** | **Backend on your LAN** |
|---|---|---|
| **AWN API only** | Path A (easiest start) | Path B |
| **SDR direct** | Path C (recommended) | Path D |
| **AWN + SDR mixed** | Path A + add SDR | Path B + add SDR |
| **AcuRite legacy** | Path E (deprecated, but works while AcuRite cloud is alive) | same |

You can run multiple ingest methods at once — they all post into the
same backend and surface as separate devices in the iOS app.

## What you need (every path)

- An **AmbientWeather** station that's reporting to ambientweather.net,
  and an [Application Key + API Key](https://ambientweather.net/account)
  → **only if you're ingesting via AWN API**.
- An **AcuRite Atlas / Fine Offset / WS-2000 / Ecowitt** outdoor station
  → **only if you're using SDR direct**.
- The Zasder Weather iOS app from the App Store, plus a way to give it
  a backend URL + bearer token (you'll generate that during setup).

For paths involving Fly.io: a free Fly.io account.
For LAN hosting: a machine that can run Docker (Raspberry Pi 3+ with
wired ethernet is plenty).

---

## Path A — Fly.io + AmbientWeather API (easiest)

```sh
brew install flyctl                      # macOS; otherwise see fly.io/docs/install/
fly auth signup                          # or `fly auth login` if you have an account
./bin/setup-fly.sh                       # interactive — prompts for AWN keys
```

When it finishes the script prints:

```
Backend URL:   https://your-app-name.fly.dev
Bearer Token:  <a long random string>
```

Open the iOS app → **Settings** → paste both → **Test connection**. Done.

### Optional: custom domain

```sh
fly certs add your-domain.example.com
fly certs show your-domain.example.com    # add the A + AAAA records it prints to your DNS
```

---

## Path B — Local backend + AmbientWeather API

For people who'd rather run everything on their own machine. Requires
a Pi/NAS/always-on box on your LAN running Docker.

```sh
cp .env.example .env
# Edit .env — set AW_APPLICATION_KEY, AW_API_KEY, and a fresh API_TOKEN
#   (generate with:  openssl rand -hex 32)
docker compose up -d
```

Find the host's LAN IP, then in the iOS app → **Settings**:

- **Backend URL**: `http://<that-lan-ip>:8080`
- **Bearer Token**: the `API_TOKEN` you set in `.env`

**Heads-up:** your phone needs to be on the same Wi-Fi as the host. For
remote access without exposing the port to the internet, look at
[Tailscale](https://tailscale.com) or WireGuard.

---

## Path C — Fly.io backend + SDR direct (recommended)

Skip any vendor's cloud entirely. A tiny container on a LAN host runs
[rtl_433](https://github.com/merbanan/rtl_433) against an RTL-SDR
dongle, decodes packets from your outdoor sensor's 433/915 MHz
broadcasts, and forwards them as normalized JSON to your Fly backend.

The sensor data path becomes: **sensor → RF → SDR → relay → Fly → app**.
Works even if AcuRite, AmbientWeather, or any other manufacturer kills
their cloud tomorrow (which has been a recurring story — that's why
this path exists).

### Hardware needed

- 1× **[RTL-SDR Blog V4](https://www.rtl-sdr.com/v4/)** USB dongle (~$25)
  with its bundled dipole antenna kit
- Optionally a 2nd V4 for dual-band capture (433 MHz for AcuRite +
  915 MHz for Fine Offset / WS-2000 simultaneously)
- A **powered USB hub** (V4 has TCXO+LDO that benefit from clean power)
- Any always-on Linux box on your LAN to host it (Pi 3+ works fine)

### Steps

1. **Set up the Fly backend** (Path A above). When it finishes, you'll
   have a `BACKEND_URL` and an `INGEST_TOKEN`.

2. **Deploy the SDR relay** on your LAN host:

   ```sh
   cd sdr-relay
   cp .env.example .env
   # Edit .env — set BACKEND_URL + INGEST_TOKEN, plus the sensor IDs
   # for your station (see sdr-relay/README.md for how to find them).
   docker compose up -d
   docker logs -f sdr-relay
   ```

3. **First-time sensor discovery** — before configuring, run rtl_433
   directly to identify the sensor IDs in range:

   ```sh
   rtl_433 -d "serial=acurite433" -R 40 -F json      # AcuRite Atlas (433 MHz)
   rtl_433 -d "serial=ws2000" -f 915M -F json        # Fine Offset / WS-2000 (915 MHz)
   ```

   Look for `"id": <integer>` in the output and put those values in
   `.env` as `ATLAS_ID` / `WH24_ID` / `WH32B_ID`. See
   [`sdr-relay/README.md`](sdr-relay/README.md) for the full guide
   including kernel-driver blacklisting, dongle-EEPROM marking,
   and per-device field maps.

4. **Open the iOS app.** Within a minute or two new device rows appear
   for each configured sensor.

### Bonus: long-tail RF discovery survey

The SDR also hears a lot of other 433/915 MHz traffic — TPMS from
passing cars, garage remotes, security sensors, smoke detectors, etc.
Every decoded packet is recorded locally in `/data/discoveries.json`
on the LAN host (deduped by `(model, id)` with first/last-seen and a
sample payload). View any time with:

```sh
docker exec sdr-relay cat /data/discoveries.json | jq .
```

Set `DISCOVERY_FORWARD_TO_BACKEND=1` to also POST sightings to the
backend (queryable via `/api/discoveries`) — useful for single-tenant
personal deployments. Leave at `0` for shared or multi-tenant
deployments since neighbors' RF traffic doesn't belong in a cloud DB.

---

## Path D — Local backend + SDR direct

Same as Path C but everything lives on one LAN box. The SDR relay
just points at `http://localhost:8080`:

```sh
# Backend
cp .env.example .env
# Edit: AW_* optional, API_TOKEN + INGEST_TOKEN required
docker compose up -d

# SDR relay (same host or another LAN host)
cd sdr-relay
cp .env.example .env
# BACKEND_URL=http://<backend-host-ip>:8080
# INGEST_TOKEN=<same as backend's>
docker compose up -d
```

---

## Path E — AcuRite via legacy DNS-hijack relay

> [!WARNING]
> AcuRite has been progressively shutting down their cloud services.
> This path depends on the hub still uploading to
> `atlasapi.myacurite.com` and on AcuRite's firmware behavior, both of
> which can change without notice. **Prefer Path C/D (SDR direct).**
> This path is documented for users with existing hub setups they
> haven't migrated yet.

The AcuRite Atlas hub speaks **TLS 1.1** and posts to a hardcoded
hostname. Fly's edge enforces TLS 1.2+ so the hub can't reach Fly
directly. A LAN-side container terminates TLS 1.1, parses the
Wunderground-format POST, and forwards JSON to your backend.

See [`relay/README.md`](relay/README.md) for the full DNS-hijack setup,
self-signed cert handling, and troubleshooting. Briefly:

```sh
cd relay
cp .env.example .env
# Edit BACKEND_URL + INGEST_TOKEN
docker compose up -d
```

Then override `atlasapi.myacurite.com → <relay-lan-ip>` in your
router's DNS and power-cycle the hub.

---

## Configuration reference

All configuration via environment variables (or a `.env` file). See
`.env.example` for the annotated backend list, `sdr-relay/.env.example`
for the SDR relay, and `relay/.env.example` for the legacy hub relay.

### Backend

| Var                       | Required | Notes                                           |
|---------------------------|----------|-------------------------------------------------|
| `API_TOKEN`               | yes      | Long random string. iOS sends as Bearer token   |
| `INGEST_TOKEN`            | for SDR / legacy relay | Long random string. Sources POST with this as Bearer |
| `AW_APPLICATION_KEY`      | for AWN  | From AmbientWeather account → API keys          |
| `AW_API_KEY`              | for AWN  | Same place                                      |
| `REVIEWER_API_TOKEN`      | no       | Optional secondary read token (App Store demos) |
| `POLL_INTERVAL_SECONDS`   | no (60)  | AWN rate-limits at 1 req/s; don't go below 30   |
| `DATABASE_PATH`           | no       | SQLite file path. Fly default `/data/weather.db`|
| `FORECAST_LAT`/`_LON`     | no       | Open-Meteo forecast coords. Defaults to your station's |
| `TIMEZONE`                | no (UTC) | IANA TZ (e.g. `America/Phoenix`) for local-time rain rollup bucketing |

### SDR relay

See [`sdr-relay/.env.example`](sdr-relay/.env.example) — the major
ones are `ATLAS_ID`, `WH24_ID`, `WH32B_ID` (sensor IDs you discover
via rtl_433), `*_RAIN_YEARLY_BASELINE_IN` (calibrate cumulative rain
counter to a known value), and `MAX_RAIN_DELTA_IN` (sanity ceiling
to reject decoder glitches).

### Legacy hub relay

See [`relay/.env.example`](relay/.env.example) — minimal: just
`BACKEND_URL` + `INGEST_TOKEN`, plus optional Wunderground
rebroadcast and human-readable station labels.

## API

All `/api/*` routes require `Authorization: Bearer <API_TOKEN>`.

| Method | Path                                              | Notes                       |
|--------|---------------------------------------------------|-----------------------------|
| GET    | `/`                                               | Public read-only status page (HTML) |
| GET    | `/healthz`                                        | Liveness, no auth           |
| GET    | `/api/devices`                                    | Devices + latest reading    |
| GET    | `/api/devices/{mac}/current`                      | Most recent observation (with on-the-fly rain rollups for sources that post only `yearly_in`) |
| GET    | `/api/devices/{mac}/history?hours=24`             | Time series                 |
| GET    | `/api/devices/{mac}/summary?field=tempf&hours=24` | Min/max/avg + when          |
| GET    | `/api/forecast?lat=&lon=`                         | 7-day forecast (Open-Meteo) |
| POST   | `/ingest/custom`                                  | Source posts a normalized observation. `Authorization: Bearer <INGEST_TOKEN>` |
| POST   | `/ingest/discovery`                               | Source posts a `(model, id)` RF sighting. Same auth. Used by the SDR relay to populate the long-tail survey of nearby devices |
| GET    | `/api/discoveries?since_hours=24`                 | Survey of distinct RF devices the SDR has decoded — neighbors' weather stations, TPMS, garage remotes, utility meters, etc. Latest-seen first |

The legacy path-form `/ingest/custom/{INGEST_TOKEN}` was removed
2026-05-21 because tokens in URLs leak into proxy and access logs.
The header form is the only supported way to authenticate ingest now.

## Tests

```sh
pip install -r requirements-dev.txt
pytest -q                       # backend tests
pytest -q relay/tests/          # legacy relay parser tests
pytest -q sdr-relay/tests/      # SDR relay normalization + rain accumulator tests
```

CI runs all three on every push (`.github/workflows/ci.yml`).

## License

[MIT](LICENSE) — do whatever you want, no warranty.

## Contributing

This is a hobby project mirrored from a private monorepo. Issues + PRs
welcome but don't expect rapid turnaround. For larger ideas, open an
issue first to chat about scope before sinking time into a PR.
