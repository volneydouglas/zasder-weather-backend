# Zasder Weather — Backend

Self-hosted backend for the **Zasder Weather** iOS app. Pulls
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

There are two independent decisions: **what stations are you ingesting
from**, and **where do you want to host the backend**. They mix and
match:

|                              | **Backend on Fly.io** (cloud)                | **Backend on your LAN** (Pi / NAS / laptop)  |
|------------------------------|----------------------------------------------|----------------------------------------------|
| **AmbientWeather only**      | A — easiest. Fly polls AWN every minute.     | B — same idea but you host the API.          |
| **AcuRite only**             | C — needs a *local relay* on your LAN too.   | D — relay + backend can live on one box.     |
| **Both**                     | A + relay → cloud backend                    | B + relay → local backend                    |

**TL;DR for typical users:**

- **Just AWN?** → Path A (cheapest, no LAN equipment needed).
- **AcuRite Atlas/Access?** → A relay always lives on your LAN. Then
  pick whether the *backend* is also LAN (D) or cloud (C).
- **Both stations?** → Run AWN through whichever backend you pick;
  add the LAN relay for AcuRite to forward into the same backend.

## What you need (every path)

- An **AmbientWeather** station that's reporting to ambientweather.net,
  and an [Application Key + API Key](https://ambientweather.net/account)
  → **only if you're ingesting AWN data**.
- An **AcuRite Atlas / Access hub** on your LAN → **only if you're
  ingesting AcuRite data**.
- The Zasder Weather iOS app from the App Store, plus a way to give it
  a backend URL + bearer token (you'll generate that during setup).

For paths involving Fly.io: a free Fly.io account.
For paths involving local hosting: a machine that can run Docker
(Raspberry Pi 3+ with wired ethernet is plenty).

---

## Path A — Fly.io + AmbientWeather (easiest)

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

## Path B — Local backend + AmbientWeather

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

## Path C — Fly.io backend + AcuRite (relay on LAN)

Why a relay? The AcuRite Atlas hub speaks **TLS 1.1** with a
self-signed-friendly handshake. Fly's edge enforces TLS 1.2+, so the
hub's POSTs can't reach Fly directly. Instead, a tiny container on your
LAN catches the hub, parses the data, and forwards JSON over plain
TLS 1.2+ HTTPS to your Fly backend.

### Steps

1. **Set up the Fly backend first** (Path A above). When it finishes,
   you'll have a `BACKEND_URL` and the script also generated an
   `INGEST_TOKEN` for you. (If you skipped that — set one now:
   `fly secrets set INGEST_TOKEN=$(openssl rand -hex 32) -a your-app`.)

2. **Pick a LAN host for the relay** — a Raspberry Pi (any Pi 3+ works)
   or any always-on Linux box with Docker. The host needs ports 80 +
   443 free.

3. **Deploy the relay** on that host:

   ```sh
   cd relay
   cp .env.example .env
   # Edit .env — set BACKEND_URL=https://your-app.fly.dev and
   # INGEST_TOKEN to the same token you set as a Fly secret above.
   docker compose up -d
   docker logs -f acurite-relay
   ```

4. **Hijack DNS for `atlasapi.myacurite.com`** so it resolves to the
   relay machine's LAN IP. Steps depend on your router — see
   [`relay/README.md`](relay/README.md#dns-hijack) for UniFi /
   Pi-hole / dnsmasq snippets.

5. **Power-cycle the AcuRite hub** so it re-resolves DNS at boot. Within
   a minute you should see live POSTs in `docker logs acurite-relay`,
   and the AcuRite station appears as a second device in the iOS app.

---

## Path D — Local backend + AcuRite (everything on one LAN box)

Same as Path C but the backend runs on your LAN too. The relay can
target `http://<host-ip>:8080` (or `http://localhost:8080` if running
on the same host as the backend).

```sh
# Backend
cp .env.example .env
# Edit: AW_*, API_TOKEN, INGEST_TOKEN (all freshly generated)
docker compose up -d

# Relay (same host or another LAN host)
cd relay
cp .env.example .env
# Edit: BACKEND_URL=http://<backend-host-ip>:8080
#       INGEST_TOKEN=<same as backend's>
docker compose up -d
```

---

## Configuration reference

All configuration via environment variables (or a `.env` file). See
`.env.example` for the annotated backend list and `relay/.env.example`
for the relay.

### Backend

| Var                       | Required | Notes                                           |
|---------------------------|----------|-------------------------------------------------|
| `AW_APPLICATION_KEY`      | for AWN  | From AmbientWeather account → API keys          |
| `AW_API_KEY`              | for AWN  | Same place                                      |
| `API_TOKEN`               | yes      | Long random string. iOS sends as Bearer token   |
| `INGEST_TOKEN`            | for relay| Long random string. Relay POSTs use this        |
| `REVIEWER_API_TOKEN`      | no       | Optional secondary read token (App Store demos) |
| `POLL_INTERVAL_SECONDS`   | no (60)  | AWN rate-limits at 1 req/s; don't go below 30   |
| `DATABASE_PATH`           | no       | SQLite file path. Fly default `/data/weather.db`|
| `FORECAST_LAT`/`_LON`     | no       | Open-Meteo forecast coords. Defaults to your station's |

### Relay

| Var                | Required | Notes                                      |
|--------------------|----------|--------------------------------------------|
| `BACKEND_URL`      | no       | Where to POST observations                 |
| `INGEST_TOKEN`     | no       | Must match backend's INGEST_TOKEN          |
| `WU_STATION_ID`    | no       | Optional Wunderground PWS upload           |
| `WU_PASSWORD`      | no       | Same                                       |
| `STATION_NAME`     | no       | Override "AcuRite Atlas" with your label   |
| `STATION_LOCATION` | no       | e.g. `Chandler` — shown in iOS app         |

## API

All `/api/*` routes require `Authorization: Bearer <API_TOKEN>`.

| Method | Path                                              | Notes                       |
|--------|---------------------------------------------------|-----------------------------|
| GET    | `/`                                               | Public read-only status     |
| GET    | `/healthz`                                        | Liveness, no auth           |
| GET    | `/api/devices`                                    | Devices + latest reading    |
| GET    | `/api/devices/{mac}/current`                      | Most recent observation     |
| GET    | `/api/devices/{mac}/history?hours=24`             | Time series                 |
| GET    | `/api/devices/{mac}/summary?field=tempf&hours=24` | Min/max/avg + when          |
| GET    | `/api/forecast?lat=&lon=`                         | 7-day forecast (Open-Meteo) |
| POST   | `/ingest/custom/{INGEST_TOKEN}`                   | For relay / SDR ingest      |

## Tests

```sh
pip install -r requirements-dev.txt
pytest -q                  # backend tests
pytest -q relay/tests/     # relay parser tests
```

CI runs both on every push (`.github/workflows/ci.yml`).

## License

[MIT](LICENSE) — do whatever you want, no warranty.

## Contributing

This is a hobby project mirrored from a private monorepo. Issues + PRs
welcome but don't expect rapid turnaround. For larger ideas, open an
issue first to chat about scope before sinking time into a PR.
