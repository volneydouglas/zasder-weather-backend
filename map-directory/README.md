# Zasder Weather map directory

The small always-on service behind the shared station map at
`https://maps.zasder.com`. Every Zasder Weather server whose owner turns on
**Share on the map** signs a beacon every ten minutes and posts it here; the
directory keeps only the latest beacon per station and serves them to the
map page and the apps.

It ships open source so a club or a company can run its own map: deploy
this directory anywhere, and set `MAP_DIRECTORY_URL=https://your.host` on
each server that should appear on it.

## What a beacon carries

A location snapped to a grid (`precision`: `area` is a 0.5 km grid, `city`
a 10 km grid, `exact` only if the owner chose it), the
station's outdoor conditions, a display name only when the owner turned it
on, a visit link that is only ever the owner's own public page (nothing
typed, so nothing to spam with) and which the owner may keep off the map
entirely (see Link modes), the station's time zone, and the sending
software's version. The station id is a hash, never the MAC. Indoor
readings, air-monitor readings, rain history and credentials never leave
the owner's server.

## Link modes

A pin that carries its server's own address puts that address in the map's
data, where one read of `/v1/beacons` collects every listed server's
hostname at once. A beacon therefore names a `link_mode`:

- `direct` — the pin carries the server's own address, as every beacon did
  before 2.3. A beacon that names no mode means this one, so an older
  server's link keeps working exactly as its owner set it up.
- `id` — the address is sent to the directory and kept here; the pin
  carries `https://<directory>/s/<public id>` instead, and `/v1/beacons`
  never contains the address at all. The redirect is the only place it
  comes back, so someone who clicks one pin learns that one server's
  address. That is the deliberate limit of the mode: it stops the bulk
  case, not a determined visitor. This does NOT proxy — the directory
  carries no traffic for anyone's page.
- `none` — no link. A beacon whose owner sent no address is treated as
  `none` whatever its mode says, so a pin never offers a link into a 404.

The public id is minted on the server's FIRST beacon and never reissued:
it is the server's name in public, and links already handed out have to
keep working. It is the two letters the server reported as its region
(read from the place its owner typed, not reverse-geocoded off coordinates
that are deliberately fuzzed) followed by six characters, or eight
characters when it reported no region — Crockford base32 without I, L, O
or U, so nothing in it can be misread. `/s/<id>` resolves only while that
server is live on the map and still asking for `id` mode: withdraw, expire,
switch back or get blocked and the id stops working.

## Trust

Each server mints an Ed25519 key pair on first share. The directory pins
the first public key it sees for a server id and refuses beacons signed
by any other. Withdrawals are signed. Beacons carry their own expiry
(three hours) so a dead server drops off without anyone's help.

Every accepted message, beacon or withdrawal, advances the server's
`sent_ms` high-water mark, and nothing at or below it is accepted again
(409): a captured tombstone cannot be replayed later to knock a station
off the map, and an old beacon cannot roll a newer one back.

Limits: one beacon a minute per server and one withdrawal a minute;
thirty posts a minute per client address, counted before any signature
is checked (`MAP_POST_PER_MINUTE`); bodies are read in chunks and refused
past 8 KB before anything parses them, chunked or not; `software`, `tz`
and `sensor` are at most 64 characters; every reading in `conditions` is
stored as a number (a numeric string is converted, anything else is
refused). One server may list 16 stations; expired rows are swept before
the count. The directory pins at most `MAP_MAX_SERVERS` server ids (5000
by default): past that a NEW id is refused with 503 and a sentence, and
every id already on file keeps posting.

A blocked server's beacons are refused with 403 until the operator
unblocks it.

### Key rotation

A server may retire its key. The new key signs the beacon as usual and the
envelope carries `rotation: {prev_pubkey, sig}`, where `sig` is the OLD
key's signature over the canonical JSON `{"rotate": <server_id>, "to":
<new pubkey>}`. The directory verifies the proof against the key on file,
moves the pin, and answers `rotated: true`; the server drops the old key
on that answer. A key that is lost cannot bless a successor: the operator
forgets the server (below) and its next beacon pins afresh.

## API

- `POST /v1/beacons` — `{beacon, sig, pubkey[, rotation]}`; 200 with
  `station_id`, `expires_ms`, `rotated`, `public_id`, `link_mode` and
  `visit` (the link the pin will show, so a server can read back exactly
  what it published).
- `POST /v1/withdraw` — the same envelope around `{withdraw: true, ...}`.
- `GET /v1/beacons[?bbox=minlon,minlat,maxlon,maxlat]` — GeoJSON, public,
  cached a minute, CORS `*`.
- `GET /v1/stations/{station_id}` — one live station as a GeoJSON
  Feature (a share link's target; the page opens on it with
  `/?station=<id>`).
- `GET /v1/stats` — `{live, servers}` for a link elsewhere.
- `GET /s/{public_id}` — a small page naming the host the pin links to,
  with a Continue link to that server's own page; `no-store`, no
  referrer. The only route that discloses an `id`-mode server's address.
  A page rather than a redirect: the address is whatever the owner's
  server signed, and a reader should see where a link goes before being
  sent there.
- `GET /healthz` — `{ok, live, protocol}`.
- `GET /` — the map page. `/static/map.js` and `/static/map.css` are its
  code; nothing is inline.

### The operator

Set `MAP_ADMIN_TOKEN` (a long random string; on Fly, `fly secrets set
MAP_ADMIN_TOKEN=...`). Without it the admin surface does not exist (404).
With it, `Authorization: Bearer <token>` on:

- `GET /v1/admin/servers` — every server id: key, first and last seen,
  live station count and names, blocked.
- `POST /v1/admin/servers/{id}/block` — its stations come off the map now
  and its beacons are refused until unblocked. The pin stays.
- `POST /v1/admin/servers/{id}/unblock`.
- `POST /v1/admin/servers/{id}/public-id` — `{public_id}`. The operator's
  override to "an id is never reissued": that rule protects links already
  handed out, and only the operator can know whether any have been. For an
  id minted wrong, not as a vanity-id service — an id implying officialdom
  is not something a directory can adjudicate. Same alphabet as a minted
  one; 409 if taken.
- `DELETE /v1/admin/servers/{id}` — forget the pin and the stations (the
  owner who lost a key starts over with trust on first use).
- `GET /admin` — a small page for the same, token kept in the tab.

A wrong or missing token is a 401. Ten bad tokens in a minute from one
client address lock the admin routes for that address for the minute;
another address's bad tokens never lock the operator out.

The blocklist is a courtesy control, not a wall: a server id is whatever
the poster signed, so a blocked poster can mint a fresh key and a fresh
id and post again. What actually bounds a hostile poster is the rest of
the list above (posts per address, the servers cap, the station cap, the
body cap). Blocking is for the ordinary case, a misconfigured or
abandoned server whose owner is not answering.

## Headers

Every response carries a Content-Security-Policy (scripts and styles only
from this host and unpkg, tiles from OpenStreetMap, data from this host,
no framing), `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, `Permissions-Policy` and HSTS.

## Run it

```sh
pip install -r requirements.txt
DATABASE_PATH=./map.db MAP_BASE_URL=http://localhost:8080 \
  uvicorn app.main:app --port 8080
```

`MAP_BASE_URL` is this directory's own address, used to build the
`/s/<id>` links it hands out in `id` mode; it defaults to
`https://maps.zasder.com`, so a club running its own must set it or those
links will point back at ours.

On Fly: set the app name in `fly.toml` (the mirror ships it as
`zasder-map-CHANGEME`), then `fly launch --copy-config --no-deploy`,
`fly volumes create map_data --size 1`, `fly deploy`, then `fly certs add
map.example.com` and a CNAME. `fly.toml` carries a `/healthz` check: a
deploy is reported green only once the new machine answers it, so a
build that dies at boot fails the deploy loudly instead of sitting there.
The strategy is `rolling` on purpose; Fly does not allow `bluegreen` or
`canary` for a machine with a volume attached, and this one has the
database on one. The base image is digest-pinned in the `Dockerfile`;
the recipe to move it is in the comment above the `FROM` line.

Tests: `pytest`.
