# AcuRite hub relay (legacy)

> [!WARNING]
> **Prefer the [SDR relay](../sdr-relay/) instead of this one for new
> deployments.** AcuRite has been progressively shutting down their
> cloud services. This relay depends on the hub still uploading to
> `atlasapi.myacurite.com` and on AcuRite firmware behavior, both of
> which can change without notice. The SDR path captures the sensor's
> RF broadcasts directly and is independent of any vendor cloud.

A tiny Docker container that lets a local-network weather station with
firmware that can't reach a modern cloud (TLS 1.1, hardcoded hostnames,
etc) push observations into your Zasder Weather backend.

Currently supports:

- **AcuRite Atlas / Access hubs** — via DNS hijack of
  `atlasapi.myacurite.com` to this container's IP. Hub speaks TLS 1.1
  with a self-signed cert it doesn't validate strictly.

Not needed for AmbientWeather stations — those are polled cloud-side
by the backend itself.

## What it does

1. Listens on ports 80 + 443 on the host network with a self-signed
   cert valid for `atlasapi.myacurite.com`.
2. Parses Wunderground rapidfire PWS query strings (the format the
   Atlas hub sends).
3. Writes both the raw payload and a normalized observation JSONL log
   to `./data/` for audit / future replay.
4. Optionally forwards the normalized observation to your backend's
   `/ingest/custom/<INGEST_TOKEN>` endpoint.
5. Optionally uploads to Wunderground using your PWS credentials.

## Setup

```sh
cd relay
cp .env.example .env
# Edit .env — at minimum set BACKEND_URL + INGEST_TOKEN
docker compose up -d
```

The container auto-generates a self-signed cert on first boot (stored
in the `certs/` volume so it persists across restarts).

## DNS hijack

Your weather station hub almost certainly tries to reach
`atlasapi.myacurite.com` (or similar). To redirect those POSTs to this
relay, override DNS on your router so that hostname resolves to this
machine's LAN IP.

**UniFi (UDM Pro / SE / Beast):** Settings → Routing → DNS →
Custom DNS Records → Add `atlasapi.myacurite.com` → A record →
this host's LAN IP.

**Pi-hole / AdGuard Home:** Local DNS → Add `atlasapi.myacurite.com →
<this host's LAN IP>`.

**Plain `dnsmasq`:** add `address=/atlasapi.myacurite.com/<lan-ip>`
to `/etc/dnsmasq.d/acurite.conf` and reload.

After the DNS change, **power-cycle the hub** so it re-resolves the
hostname at boot.

## Verify it's working

```sh
docker logs -f acurite-relay
```

Within a minute you should see:

```
[2026-05-14T...Z] HTTPS obs from <hub-ip>: 98.3°F, wind 7.0 mph, ... → backend
```

If you see no traffic after 5 min:

- Confirm DNS override is active: `dig @<your-router-ip>
  atlasapi.myacurite.com` should return your relay's IP.
- Confirm the hub is online: visit `http://<hub-ip>/`.
- Confirm the hub did re-resolve DNS — power-cycle it again.

## Troubleshooting

**Container exits immediately:** check `docker logs acurite-relay`.
Most common: port 80 or 443 already in use on the host. Free them or
move the relay to a different machine.

**Forwards to backend fail:** check the log for `WARN: forward
failed`. Most likely your `BACKEND_URL` or `INGEST_TOKEN` is wrong.
Verify with `curl -X POST <BACKEND_URL>/ingest/custom/<token> -d '{}'`
— should return `400 missing or invalid timestamp_utc` (a polite
rejection means routing + auth work).
