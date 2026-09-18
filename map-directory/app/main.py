"""The Zasder Weather station map directory (2.3): a tiny always-on
service that keeps the LATEST signed beacon per station and serves them
to the map. It runs at maps.zasder.com and ships open source, so a club
can run its own and point its servers at it (MAP_DIRECTORY_URL).

What it stores: the beacon exactly as signed (a fuzzed location, the
outdoor conditions, an optional name and visit link), the public key
pinned for the server id on first sight, and receipt times. Nothing
else: no accounts, no history, no IP addresses.

Trust on first use: the first beacon a server id sends pins its public
key; every later beacon must verify against it. A withdrawal is a
signed tombstone. A beacon past its own expiry is not served and is
swept. Replays and clock skew: a beacon older than the pinned one, one
sent more than ten minutes in the future, or one that expires more than
six hours out is refused. Every accepted message (beacon or withdrawal)
advances the server's high-water mark (its `sent_ms`, clamped to the
wall clock at receipt so a fast clock cannot lock its owner out) and
nothing at or below it is accepted again; a stamp accepted ahead of the
clock is remembered by value until the clock passes it. So a captured
tombstone cannot be replayed to knock a station off the map. Every epoch
field must sit after 2000 and inside an Int64. One beacon a minute per
server id, and one withdrawal a minute.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import os
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("zasder.map")
logging.basicConfig(level=logging.INFO)

DB_PATH = os.environ.get("DATABASE_PATH", "/data/map.db")
STATIC = Path(__file__).resolve().parent.parent / "static"
PROTOCOL = 1
MAX_TTL_MS = 6 * 3_600_000
MAX_FUTURE_MS = 10 * 60_000
# Every epoch field on the wire (sent_ms, expires_ms, observed_ms) must be
# a time this directory can serve (F08): after 2000-01-01 and inside a
# signed 64-bit int. Python took 10**40 as an int and served it; every
# native reader decodes epoch millis into an Int64 and dropped the pin.
EPOCH_MIN_MS = 946_684_800_000
INT64_MAX = 2**63 - 1
MIN_INTERVAL_MS = 60_000
# The envelope is read off the wire in chunks and refused past this many
# bytes BEFORE anything parses it — a Content-Length check alone lets a
# chunked body of any size straight through to the model (R23).
MAX_BODY = 8 * 1024
# The free-text fields a beacon carries (software version, time zone,
# sensor kind). Bounded so a body under the cap cannot still stuff the
# map's data with one 8 KB string.
MAX_TEXT = 64
# One server may list this many stations; past it a beacon is refused
# (a runaway or hostile server cannot paper the map).
MAX_STATIONS_PER_SERVER = 16
# This directory's own address, used to build the /s/<id> links it hands
# out in "id" link mode. A club running its own directory sets this.
BASE_URL = os.environ.get("MAP_BASE_URL", "https://maps.zasder.com").rstrip("/")
# Crockford base32 minus I, L, O and U: no character a person can misread
# as another when they read an id aloud or copy it off a screen.
ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ID_RANDOM_LEN = 6
# How a server may be reached from its pin:
#   direct — the pin carries the server's own address (the pre-2.3 behaviour,
#            and what a beacon that names no mode means)
#   id     — the pin carries https://<directory>/s/<public id>, which
#            redirects; the address itself is never served to the map
#   none   — no link at all
LINK_MODES = ("direct", "id", "none")
# The operator's token for the blocklist (MAP_ADMIN_TOKEN). Unset =
# the admin routes do not exist (404), so a directory with no operator
# has no admin surface at all.
ADMIN_TOKEN = os.environ.get("MAP_ADMIN_TOKEN", "").strip()
# ms of recent bad tokens, PER CLIENT ADDRESS. One global bucket let ten
# bad bearers a minute from anyone lock the operator out of the blocklist
# (R23); uvicorn runs with --proxy-headers, so behind Fly's proxy the
# address is the caller's, not the proxy's.
_admin_failures: dict[str, list[int]] = {}
ADMIN_FAIL_LIMIT = 10                      # per minute per address, then 429

# The map page's Content-Security-Policy. Scripts and styles come only
# from this host and unpkg (Leaflet, pinned with SRI), tiles from
# OpenStreetMap, data from this host. No inline script: the page's
# code lives in /static/map.js, and the pins are coloured by class.
CSP = ("default-src 'none'; "
       "script-src 'self' https://unpkg.com; "
       "style-src 'self' https://unpkg.com; "
       "img-src 'self' data: https://tile.openstreetmap.org https://unpkg.com; "
       "connect-src 'self'; "
       "font-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    server_id  TEXT PRIMARY KEY,
    pubkey     TEXT NOT NULL,
    first_ms   INTEGER NOT NULL,
    last_ms    INTEGER NOT NULL,
    blocked    INTEGER NOT NULL DEFAULT 0,
    -- Assigned once, on the first beacon, and never reissued: the id is
    -- what a pin shows and what /s/<id> resolves, so it is the server's
    -- name in public and changing it would break every link already out.
    public_id  TEXT,
    -- The address /s/<public_id> redirects to. Held HERE rather than
    -- served with the beacon: in "id" mode the map's own data must not
    -- carry it, or hiding it behind the id buys nothing.
    visit_url  TEXT,
    -- The `sent_ms` of the last message (beacon or withdrawal) accepted
    -- from this server, clamped to the wall clock at receipt. Nothing at
    -- or below it is accepted again: a captured tombstone replayed later,
    -- or an old beacon, is refused. See _accept_sent for the clamp.
    last_sent_ms INTEGER
);
CREATE TABLE IF NOT EXISTS beacons (
    station_id  TEXT PRIMARY KEY,
    server_id   TEXT NOT NULL,
    sent_ms     INTEGER NOT NULL,
    expires_ms  INTEGER NOT NULL,
    received_ms INTEGER NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beacons_expires ON beacons(expires_ms);
-- Stamps accepted AHEAD of the wall clock (a fast sender's), kept until
-- wall time passes them so that a replay inside that window is still
-- refused. The per-server mark is clamped to receipt time (see
-- _accept_sent) and cannot remember them by itself. At most one stamp a
-- minute for at most MAX_FUTURE_MS per server: a handful of rows.
CREATE TABLE IF NOT EXISTS future_stamps (
    server_id TEXT NOT NULL,
    sent_ms   INTEGER NOT NULL,
    PRIMARY KEY (server_id, sent_ms)
);
"""
# NOT in SCHEMA: `executescript` runs it before the migration below can add
# the column, and on a directory that predates the ids `servers` has no
# `public_id` yet — the index statement then dies with "no such column" and
# takes the whole process down at boot. (2026-09-14: it did exactly that on
# zasder-map. Every test had a fresh database, where SCHEMA creates the
# table WITH the column, so nothing caught it until the deploy.)
PUBLIC_ID_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS idx_servers_public_id "
                   "ON servers(public_id) WHERE public_id IS NOT NULL")

class _BoundedRecent(OrderedDict):
    """server id → ms, bounded: the oldest entry goes when the bound is
    passed. Unbounded, a sender minting a fresh server id per request grew
    it one entry per request for the life of the process (R23)."""

    def __init__(self, bound: int) -> None:
        super().__init__()
        self.bound = bound

    def __setitem__(self, key: str, value: int) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.bound:
            self.popitem(last=False)


RECENT_BOUND = 10_000
_last_seen_ms: _BoundedRecent = _BoundedRecent(RECENT_BOUND)
# Withdrawals get their own minute: a server that posts a beacon and is
# switched off a moment later must still be able to withdraw it.
_last_withdraw_ms: _BoundedRecent = _BoundedRecent(RECENT_BOUND)
# Per-client-address windows (ms of recent hits), pruned on every call so
# an address that went quiet costs nothing after its minute.
_post_hits: dict[str, list[int]] = {}
# Posts a minute from one address, before any signature is checked. A
# server posts one beacon every ten minutes, so this is far above any
# honest load and well below what fills the volume: without it a sender
# with a fresh key per request mints a fresh server id per request, which
# the per-server minute cannot see.
POST_IP_LIMIT = int(os.environ.get("MAP_POST_PER_MINUTE", "30"))
# How many server ids the directory will ever pin. A NEW id past this is
# refused with 503 and a sentence; every id already on file keeps posting.
# The operator raises it (MAP_MAX_SERVERS) when the map earns it.
MAX_SERVERS = int(os.environ.get("MAP_MAX_SERVERS", "5000"))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _window(store: dict[str, list[int]], key: str, now_ms: int) -> list[int]:
    """The hits in the last minute for `key`, with every stale entry (this
    key's and every other's) dropped so the store stays bounded by the
    request rate, not by how many addresses have ever called."""
    for k in list(store):
        kept = [t for t in store[k] if now_ms - t < 60_000]
        if kept:
            store[k] = kept
        else:
            del store[k]
    return store.get(key, [])


def _throttle_posts(request: Request, now_ms: int) -> None:
    ip = _client_ip(request)
    hits = _window(_post_hits, ip, now_ms)
    if len(hits) >= POST_IP_LIMIT:
        raise HTTPException(status_code=429, detail="too many posts from this address")
    _post_hits[ip] = hits + [now_ms]


async def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # A directory that predates the ids has the table without them;
        # CREATE TABLE IF NOT EXISTS does not add a column to one that is
        # already there.
        cols = {r[1] for r in await (await db.execute(
            "PRAGMA table_info(servers)")).fetchall()}
        # The loop's own literal tuple is the whitelist for the interpolated
        # column name: nothing from a request reaches this string, and the
        # check is the tuple itself rather than an assert (asserts vanish
        # under python -O).
        # Every ALTER runs BEFORE any index that might name one of these
        # columns (the 2026-09-14 lesson above): a new column goes in this
        # tuple, never in SCHEMA alone, and never behind an index.
        for col, kind in (("public_id", "TEXT"), ("visit_url", "TEXT"),
                          ("last_sent_ms", "INTEGER")):
            if col not in cols:
                await db.execute(f"ALTER TABLE servers ADD COLUMN {col} {kind}")
        await db.execute(PUBLIC_ID_INDEX)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Zasder Weather map directory", lifespan=lifespan,
              docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    return resp


class Envelope(BaseModel):
    beacon: dict[str, Any]
    sig: str = Field(max_length=200)
    pubkey: str = Field(max_length=100)
    # Key rotation: {"prev_pubkey": <the key on file>, "sig": <that key's
    # signature over rotation_message(server_id, pubkey)>}. Present on
    # every beacon until the directory answers rotated=true.
    rotation: dict[str, str] | None = None


def rotation_message(server_id: str, new_pubkey: str) -> bytes:
    """What the OLD key signs to hand the server id to the new key."""
    return canonical({"rotate": server_id, "to": new_pubkey})


def _rotation_ok(rot: dict[str, str] | None, server_id: str, on_file: str,
                 new_pubkey: str) -> bool:
    if not rot:
        return False
    prev, sig = rot.get("prev_pubkey"), rot.get("sig")
    if not isinstance(prev, str) or not isinstance(sig, str) or prev != on_file:
        return False
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(prev))
        pub.verify(base64.b64decode(sig), rotation_message(server_id, new_pubkey))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def verify(env: Envelope) -> None:
    """The signature must match the envelope's own public key; the pin
    check against the server id comes after."""
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(env.pubkey))
        pub.verify(base64.b64decode(env.sig), canonical(env.beacon))
    except (InvalidSignature, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="bad signature")


def _num(v: Any) -> float | None:
    """A finite number, or None. A bool is not a reading (True is 1.0 to
    float(), and a `tempf: true` pin would read as one degree)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _epoch(v: Any) -> bool:
    """An epoch-millisecond field a native reader can hold: an int after
    2000-01-01 and no larger than a signed 64-bit int (F08)."""
    return _int(v) and EPOCH_MIN_MS < v <= INT64_MAX


def validate_beacon(b: dict[str, Any], now_ms: int) -> None:
    """Refuse what the map cannot serve, and NORMALISE what it stores: the
    signature was checked over the bytes the server sent, and what goes
    into the table is this function's reading of them. Every reading is
    stored as a float — a numeric STRING passed `_num` and was stored as
    sent, and `"29.71".toFixed` then threw inside the map page's render,
    which blanked the whole map for every visitor until the beacon expired
    (R23). One poster must never be able to do that."""
    if b.get("v") != PROTOCOL:
        raise HTTPException(status_code=400, detail="unsupported beacon version")
    for k in ("server_id", "station_id"):
        v = b.get(k)
        if not isinstance(v, str) or not (4 <= len(v) <= 64):
            raise HTTPException(status_code=400, detail=f"bad {k}")
    sent = b.get("sent_ms")
    if not _epoch(sent):
        raise HTTPException(status_code=400, detail="bad times")
    if sent > now_ms + MAX_FUTURE_MS:
        raise HTTPException(status_code=400, detail="sent in the future")
    if b.get("withdraw"):
        return
    lat, lon = _num(b.get("lat")), _num(b.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="bad location")
    b["lat"], b["lon"] = lat, lon
    exp = b.get("expires_ms")
    if not _epoch(exp):
        raise HTTPException(status_code=400, detail="bad times")
    if exp <= now_ms:
        raise HTTPException(status_code=400, detail="already expired")
    if exp - sent > MAX_TTL_MS:
        raise HTTPException(status_code=400, detail="expiry too far out")
    cond = b.get("conditions")
    if not isinstance(cond, dict) or len(cond) > 24:
        raise HTTPException(status_code=400, detail="bad conditions")
    clean: dict[str, float] = {}
    for k, v in cond.items():
        f = _num(v)
        if not isinstance(k, str) or len(k) > 24 or f is None:
            raise HTTPException(status_code=400, detail="bad conditions")
        clean[k] = f
    b["conditions"] = clean
    for k in ("software", "tz", "sensor"):
        v = b.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > MAX_TEXT):
            raise HTTPException(status_code=400, detail=f"bad {k}")
    fuzzed = b.get("fuzzed")
    if fuzzed is not None and not isinstance(fuzzed, bool):
        raise HTTPException(status_code=400, detail="bad fuzzed")
    observed = b.get("observed_ms")
    if observed is not None and (not _epoch(observed) or observed > now_ms + MAX_FUTURE_MS):
        raise HTTPException(status_code=400,
                            detail="observed_ms is not a time between 2000 and ten minutes from now")
    prec = b.get("precision")
    if prec is not None and prec not in ("exact", "area", "city"):
        raise HTTPException(status_code=400, detail="bad precision")
    name = b.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 48):
        raise HTTPException(status_code=400, detail="bad name")
    visit = b.get("visit_url")
    if visit is not None and (not isinstance(visit, str) or not visit.startswith("https://")
                              or len(visit) > 200):
        raise HTTPException(status_code=400, detail="bad visit_url")
    mode = b.get("link_mode")
    if mode is not None and mode not in LINK_MODES:
        raise HTTPException(status_code=400, detail="bad link_mode")
    region = b.get("region")
    if region is not None and (not isinstance(region, str) or len(region) != 2
                               or not region.isalpha() or not region.isupper()):
        raise HTTPException(status_code=400, detail="bad region")


def link_mode_of(b: dict[str, Any]) -> str:
    """A beacon that names no mode is `direct`: that is what every beacon
    minted before 2.3 meant by carrying a visit_url at all, and silently
    upgrading one to a redirect would break links already handed out."""
    mode = b.get("link_mode")
    return mode if mode in LINK_MODES else "direct"


def mint_public_id(region: str | None, taken: set[str]) -> str:
    """`AZ4K7P2Q` — the region the server reported, then six random
    characters; eight random ones when it reported none, so every id is
    the same length either way. Assigned once and never reissued."""
    prefix = region if region and len(region) == 2 else ""
    width = ID_RANDOM_LEN if prefix else ID_RANDOM_LEN + 2
    for _ in range(64):
        body = "".join(secrets.choice(ID_ALPHABET) for _ in range(width))
        candidate = f"{prefix}{body}"
        if candidate not in taken:
            return candidate
    raise HTTPException(status_code=503, detail="could not mint a public id")


async def _pin(db: aiosqlite.Connection, server_id: str, pubkey: str, now_ms: int,
               rotation: dict[str, str] | None = None) -> bool:
    """Trust on first use; refuse a different key afterwards, unless the
    key on file signed a hand-over to the new one (rotation). Returns
    True when a rotation was applied on this call."""
    row = await (await db.execute(
        "SELECT pubkey, blocked FROM servers WHERE server_id = ?", (server_id,))).fetchone()
    if row is None:
        # A fresh key mints a fresh id, so "one server a minute" cannot
        # bound how many ids one sender pins. The table can (R23).
        n = (await (await db.execute("SELECT COUNT(*) FROM servers")).fetchone())[0]
        if n >= MAX_SERVERS:
            raise HTTPException(
                status_code=503,
                detail=f"this directory lists {MAX_SERVERS} servers and is not taking "
                       "new ones; servers already listed keep posting")
        await db.execute(
            "INSERT INTO servers (server_id, pubkey, first_ms, last_ms) VALUES (?, ?, ?, ?)",
            (server_id, pubkey, now_ms, now_ms))
        return False
    if row[1]:
        raise HTTPException(status_code=403, detail="server blocked")
    rotated = False
    if row[0] != pubkey:
        if not _rotation_ok(rotation, server_id, row[0], pubkey):
            raise HTTPException(status_code=403, detail="key does not match the one on file")
        await db.execute("UPDATE servers SET pubkey = ? WHERE server_id = ?", (pubkey, server_id))
        log.info("key rotated for %s", server_id)
        rotated = True
    await db.execute("UPDATE servers SET last_ms = ? WHERE server_id = ?", (now_ms, server_id))
    return rotated


async def _read_envelope(request: Request) -> Envelope:
    """The body, read off the wire in chunks and refused the moment it
    passes MAX_BODY — before pydantic sees a byte of it. The declared
    length is checked first as a courtesy; a chunked body declares none,
    and the streamed count is what actually holds."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY:
                raise HTTPException(status_code=413, detail="beacon too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="bad content-length")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(status_code=413, detail="beacon too large")
    try:
        return Envelope.model_validate_json(bytes(body))
    except ValidationError:
        raise HTTPException(status_code=400, detail="bad envelope")


async def _accept_sent(db: aiosqlite.Connection, server_id: str, sent_ms: int,
                       now_ms: int, *, rotated: bool = False) -> None:
    """The per-server high-water mark. Anything at or below the last
    accepted `sent_ms` is a replay (a captured tombstone posted again, an
    old beacon) and is refused; the mark moves only on acceptance, so the
    caller commits it with the row it belongs to.

    The mark stored is `min(sent_ms, now_ms)`, not the sender's stamp: a
    server whose clock ran fast used to set a mark in the future and was
    then locked out with 409 until wall time caught up with its own
    mistake, even after it fixed the clock. A stamp never runs past
    MAX_FUTURE_MS anyway (validate_beacon), so the mark is at most that
    far behind the stamp. What the clamped mark cannot do is remember
    the future-dated stamps it let through, so those are kept by value
    in `future_stamps` until wall time passes them; a replay in that
    window is refused by the table, and after it by the mark. A key
    rotation the directory just applied resets both: the new key's
    first message is the owner starting over, and the proof it carries
    was signed by the key on file, which no replay can produce."""
    row = await (await db.execute(
        "SELECT last_sent_ms FROM servers WHERE server_id = ?", (server_id,))).fetchone()
    mark = row[0] if row is not None else None
    if not rotated:
        if mark is not None and sent_ms <= mark:
            raise HTTPException(status_code=409, detail="older than the last message on file")
        seen = await (await db.execute(
            "SELECT 1 FROM future_stamps WHERE server_id = ? AND sent_ms = ?",
            (server_id, sent_ms))).fetchone()
        if seen is not None:
            raise HTTPException(status_code=409, detail="already accepted that message")
    else:
        await db.execute("DELETE FROM future_stamps WHERE server_id = ?", (server_id,))
    new_mark = min(sent_ms, now_ms)
    await db.execute("UPDATE servers SET last_sent_ms = ? WHERE server_id = ?",
                     (new_mark, server_id))
    # Stamps at or below the mark are refused by the mark; drop them.
    await db.execute("DELETE FROM future_stamps WHERE server_id = ? AND sent_ms <= ?",
                     (server_id, new_mark))
    if sent_ms > now_ms:
        await db.execute("INSERT OR IGNORE INTO future_stamps (server_id, sent_ms) VALUES (?, ?)",
                         (server_id, sent_ms))


@app.post("/v1/beacons")
async def post_beacon(request: Request) -> JSONResponse:
    now_ms = int(time.time() * 1000)
    _throttle_posts(request, now_ms)
    env = await _read_envelope(request)
    verify(env)
    b = env.beacon
    validate_beacon(b, now_ms)
    if b.get("withdraw"):
        raise HTTPException(status_code=400, detail="withdrawals go to /v1/withdraw")
    sid = b["server_id"]
    last = _last_seen_ms.get(sid, 0)
    if now_ms - last < MIN_INTERVAL_MS:
        raise HTTPException(status_code=429, detail="one beacon a minute")
    async with aiosqlite.connect(DB_PATH) as db:
        # Expired rows go first: they are not on the map, and they must not
        # count against the server's station cap either.
        await _sweep(db, now_ms)
        rotated = await _pin(db, sid, env.pubkey, now_ms, env.rotation)
        await _accept_sent(db, sid, b["sent_ms"], now_ms, rotated=rotated)
        # Minted the first time a server actually ASKS for `id` mode, and
        # then left alone. Not on any earlier beacon: a server running an
        # older build sends no region, and an id minted from one of those
        # would be stuck without its region prefix for good — which is
        # exactly what happened when the directory was upgraded ahead of
        # the servers posting to it (2026-09-14). A `direct` or `none`
        # server has no use for an id until it switches.
        mode = link_mode_of(b)
        public_id = await _ensure_public_id(db, sid, b.get("region"),
                                            mint=(mode == "id"))
        visit = b.get("visit_url") if mode in ("direct", "id") else None
        await db.execute("UPDATE servers SET visit_url = ? WHERE server_id = ?",
                         (visit, sid))
        prev = await (await db.execute(
            "SELECT sent_ms, server_id, received_ms FROM beacons WHERE station_id = ?",
            (b["station_id"],))).fetchone()
        if prev is not None:
            if prev[1] != sid:
                raise HTTPException(status_code=403, detail="station belongs to another server")
            # Same clamp as _accept_sent: the beacon on file from a fast
            # clock must not hold the station until its stamp comes true.
            if b["sent_ms"] <= min(prev[0], prev[2]):
                raise HTTPException(status_code=409, detail="older than the beacon on file")
        else:
            n = (await (await db.execute(
                "SELECT COUNT(*) FROM beacons WHERE server_id = ?", (sid,))).fetchone())[0]
            if n >= MAX_STATIONS_PER_SERVER:
                raise HTTPException(status_code=429,
                                    detail=f"a server may list {MAX_STATIONS_PER_SERVER} stations")
        await db.execute(
            "INSERT OR REPLACE INTO beacons (station_id, server_id, sent_ms, expires_ms, "
            "received_ms, lat, lon, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (b["station_id"], sid, b["sent_ms"], b["expires_ms"], now_ms,
             float(b["lat"]), float(b["lon"]), json.dumps(b)))
        await db.commit()
    _last_seen_ms[sid] = now_ms
    return JSONResponse({"ok": True, "station_id": b["station_id"], "expires_ms": b["expires_ms"],
                         "rotated": rotated, "public_id": public_id,
                         "link_mode": mode,
                         "visit": _visit_link(mode, b.get("visit_url"), public_id)})


@app.post("/v1/withdraw")
async def post_withdraw(request: Request) -> JSONResponse:
    now_ms = int(time.time() * 1000)
    _throttle_posts(request, now_ms)
    env = await _read_envelope(request)
    verify(env)
    b = env.beacon
    validate_beacon(b, now_ms)
    if not b.get("withdraw"):
        raise HTTPException(status_code=400, detail="not a withdrawal")
    sid = b["server_id"]
    if now_ms - _last_withdraw_ms.get(sid, 0) < MIN_INTERVAL_MS:
        raise HTTPException(status_code=429, detail="one withdrawal a minute")
    async with aiosqlite.connect(DB_PATH) as db:
        rotated = await _pin(db, sid, env.pubkey, now_ms, env.rotation)
        await _accept_sent(db, sid, b["sent_ms"], now_ms, rotated=rotated)
        cur = await db.execute("DELETE FROM beacons WHERE station_id = ? AND server_id = ?",
                               (b["station_id"], sid))
        await db.commit()
    _last_withdraw_ms[sid] = now_ms
    return JSONResponse({"ok": True, "removed": cur.rowcount, "rotated": rotated})


async def _ensure_public_id(db: aiosqlite.Connection, server_id: str,
                            region: Any, mint: bool) -> str | None:
    row = await (await db.execute(
        "SELECT public_id FROM servers WHERE server_id = ?", (server_id,))).fetchone()
    if row is not None and row[0]:
        return str(row[0])
    if not mint:
        return None
    taken = {r[0] for r in await (await db.execute(
        "SELECT public_id FROM servers WHERE public_id IS NOT NULL")).fetchall()}
    minted = mint_public_id(region if isinstance(region, str) else None, taken)
    await db.execute("UPDATE servers SET public_id = ? WHERE server_id = ?",
                     (minted, server_id))
    return minted


def _visit_link(mode: str, visit_url: Any, public_id: str | None) -> str | None:
    """What the MAP is allowed to show. In `id` mode the server's own
    address never appears here — that is the whole point of the mode, and
    a pin that carried both would hide nothing.

    `visit_url` is what the server signed, and it decides whether there is
    a link AT ALL: an `id` pin whose server sent no address would offer a
    Visit link that resolves to a 404."""
    if mode == "none" or not isinstance(visit_url, str) or not visit_url:
        return None
    if mode == "id":
        return f"{BASE_URL}/s/{public_id}" if public_id else None
    return visit_url


def _feature(b: dict[str, Any], received_ms: int,
             public_id: str | None = None) -> dict[str, Any]:
    props = {k: b.get(k) for k in ("station_id", "name", "sensor", "tz",
                                    "observed_ms", "sent_ms", "expires_ms", "fuzzed",
                                    "precision", "software")}
    # NEVER copied straight out of the body: `visit_url` is whatever the
    # server signed, and in `id` mode the map must not see it.
    mode = link_mode_of(b)
    props["link_mode"] = mode
    props["visit_url"] = _visit_link(mode, b.get("visit_url"), public_id)
    if mode == "id" and props["visit_url"]:
        props["public_id"] = public_id
    props["conditions"] = b.get("conditions") or {}
    props["received_ms"] = received_ms
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [b["lon"], b["lat"]]},
            "properties": {k: v for k, v in props.items() if v is not None}}


async def _sweep(db: aiosqlite.Connection, now_ms: int) -> None:
    await db.execute("DELETE FROM beacons WHERE expires_ms <= ?", (now_ms,))
    await db.commit()


@app.get("/v1/beacons")
async def list_beacons(bbox: str | None = None) -> JSONResponse:
    """Every live beacon, as GeoJSON. `bbox=minlon,minlat,maxlon,maxlat`
    narrows it. Public: this is the map."""
    now_ms = int(time.time() * 1000)
    # Qualified: the public id comes from a join, so a bare column name
    # here would be ambiguous.
    where = "b.expires_ms > ?"
    args: list[Any] = [now_ms]
    if bbox:
        try:
            minlon, minlat, maxlon, maxlat = (float(x) for x in bbox.split(","))
        except ValueError:
            raise HTTPException(status_code=400, detail="bad bbox")
        where += " AND b.lat BETWEEN ? AND ? AND b.lon BETWEEN ? AND ?"
        args += [minlat, maxlat, minlon, maxlon]
    async with aiosqlite.connect(DB_PATH) as db:
        await _sweep(db, now_ms)
        rows = await (await db.execute(
            f"SELECT b.body, b.received_ms, s.public_id FROM beacons b "
            f"LEFT JOIN servers s ON s.server_id = b.server_id "
            f"WHERE {where} ORDER BY b.received_ms DESC LIMIT 5000",
            args)).fetchall()
    features = [_feature(json.loads(body), received, pid) for body, received, pid in rows]
    return JSONResponse({"type": "FeatureCollection", "features": features,
                         "count": len(features), "generated_ms": now_ms},
                        headers={"Cache-Control": "public, max-age=60",
                                 "Access-Control-Allow-Origin": "*"})


@app.get("/v1/stats")
async def stats() -> JSONResponse:
    """For a link elsewhere ("N stations sharing"): live count and the
    number of servers ever seen. Public, cached a minute."""
    now_ms = int(time.time() * 1000)
    async with aiosqlite.connect(DB_PATH) as db:
        live = (await (await db.execute(
            "SELECT COUNT(*) FROM beacons WHERE expires_ms > ?", (now_ms,))).fetchone())[0]
        servers = (await (await db.execute(
            "SELECT COUNT(*) FROM servers WHERE blocked = 0")).fetchone())[0]
    return JSONResponse({"live": live, "servers": servers, "generated_ms": now_ms},
                        headers={"Cache-Control": "public, max-age=60",
                                 "Access-Control-Allow-Origin": "*"})


@app.get("/v1/stations/{station_id}")
async def get_station(station_id: str) -> JSONResponse:
    """One live station as a GeoJSON Feature (a share link's target)."""
    now_ms = int(time.time() * 1000)
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT b.body, b.received_ms, s.public_id FROM beacons b "
            "LEFT JOIN servers s ON s.server_id = b.server_id "
            "WHERE b.station_id = ? AND b.expires_ms > ?",
            (station_id[:64], now_ms))).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no live beacon for that station")
    return JSONResponse(_feature(json.loads(row[0]), row[1], row[2]),
                        headers={"Cache-Control": "public, max-age=60",
                                 "Access-Control-Allow-Origin": "*"})


def _visit_page(public_id: str, url: str, host: str) -> str:
    """The interstitial. No inline script or style (the map's CSP applies
    here too); the stylesheet is /static/visit.css. Everything from the
    row is escaped: the address is whatever the owner's server signed."""
    h, u, pid = html.escape(host), html.escape(url, quote=True), html.escape(public_id)
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<meta name=\"referrer\" content=\"no-referrer\">\n"
            f"<title>Leaving the map · {pid}</title>\n"
            "<link rel=\"stylesheet\" href=\"/static/visit.css\">\n</head>\n<body>\n"
            "<main>\n<p class=\"brand\"><i>zasder</i><b>WEATHER</b> · station map</p>\n"
            f"<h1>Leaving the map</h1>\n"
            f"<p>Station <code>{pid}</code> links to a page on <b>{h}</b>. That server "
            "is run by the station's owner, not by this directory, and the directory "
            "carries none of its traffic.</p>\n"
            f"<p><a class=\"go\" href=\"{u}\" rel=\"noopener noreferrer nofollow\">"
            f"Continue to {h}</a></p>\n"
            "<p class=\"muted\"><a href=\"/\">Back to the map</a></p>\n"
            "</main>\n</body>\n</html>\n")


@app.get("/s/{public_id}")
async def visit(public_id: str) -> Response:
    """The one place a server's own address is handed out in `id` mode:
    a page that names the destination host and offers a Continue link,
    not a value the map serves. It used to be a bare 302 — an open
    redirect to any https address a poster signed, and a reader who
    clicked a pin was on a stranger's server before seeing where the link
    went (R23). This does NOT proxy — whoever continues learns that one
    server's address, which is the deliberate limit of the mode. What it
    stops is the bulk case: reading every listed server's address
    straight out of /v1/beacons.

    Only a server that is live on the map and still asking for `id` mode
    resolves. A withdrawn, expired, blocked or switched-back server is a
    404, so an id stops working the moment its owner stops sharing."""
    pid = public_id.strip().upper()[:16]
    now_ms = int(time.time() * 1000)
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT s.visit_url, s.blocked, s.server_id FROM servers s "
            "WHERE s.public_id = ?", (pid,))).fetchone()
        if row is None or row[1] or not row[0]:
            raise HTTPException(status_code=404, detail="no such station")
        live = await (await db.execute(
            "SELECT body FROM beacons WHERE server_id = ? AND expires_ms > ? "
            "ORDER BY sent_ms DESC LIMIT 1", (row[2], now_ms))).fetchone()
    if live is None or link_mode_of(json.loads(live[0])) != "id":
        raise HTTPException(status_code=404, detail="no such station")
    url = str(row[0])
    host = urlsplit(url).hostname if url.startswith("https://") else None
    if not host:
        raise HTTPException(status_code=404, detail="no such station")
    return HTMLResponse(_visit_page(pid, url, host), headers={
        # Never cached: the target follows the owner's own address, and a
        # cached page would outlive their switching the link off.
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    })


# ── the operator (blocklist) ─────────────────────────────────────────

def _admin(request: Request) -> None:
    """Bearer MAP_ADMIN_TOKEN, constant-time; ten bad tries a minute PER
    ADDRESS, so a stranger's bad tokens never lock the operator out. The
    compare is over bytes: `compare_digest` on str raises on a non-ASCII
    token, which was a 500 the failure counter never saw (R23)."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=404)
    now_ms = int(time.time() * 1000)
    ip = _client_ip(request)
    failures = _window(_admin_failures, ip, now_ms)
    if len(failures) >= ADMIN_FAIL_LIMIT:
        raise HTTPException(status_code=429, detail="too many bad tokens")
    got = request.headers.get("authorization", "")
    ok = got.startswith("Bearer ") and hmac.compare_digest(
        got[7:].strip().encode("utf-8", "surrogateescape"), ADMIN_TOKEN.encode())
    if not ok:
        _admin_failures[ip] = failures + [now_ms]
        raise HTTPException(status_code=401, detail="bad admin token",
                            headers={"WWW-Authenticate": "Bearer"})


@app.get("/v1/admin/servers")
async def admin_servers(request: Request) -> JSONResponse:
    _admin(request)
    now_ms = int(time.time() * 1000)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT s.server_id, s.pubkey, s.first_ms, s.last_ms, s.blocked, "
            "(SELECT COUNT(*) FROM beacons b WHERE b.server_id = s.server_id AND b.expires_ms > ?), "
            "(SELECT GROUP_CONCAT(json_extract(b.body, '$.name'), ' / ') FROM beacons b "
            " WHERE b.server_id = s.server_id AND b.expires_ms > ?) "
            "FROM servers s ORDER BY s.last_ms DESC", (now_ms, now_ms))).fetchall()
    return JSONResponse({"servers": [
        {"server_id": r[0], "pubkey": r[1], "first_ms": r[2], "last_ms": r[3],
         "blocked": bool(r[4]), "live": r[5], "names": r[6]} for r in rows],
        "generated_ms": now_ms}, headers={"Cache-Control": "no-store"})


@app.post("/v1/admin/servers/{server_id}/block")
async def admin_block(server_id: str, request: Request) -> JSONResponse:
    """Block: its beacons come off the map now and every later one is
    refused (403) until unblocked. The pin stays, so the same key is
    recognised when it is unblocked."""
    _admin(request)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE servers SET blocked = 1 WHERE server_id = ?", (server_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="unknown server")
        gone = await db.execute("DELETE FROM beacons WHERE server_id = ?", (server_id,))
        await db.commit()
    log.warning("server %s blocked by the operator", server_id)
    return JSONResponse({"ok": True, "server_id": server_id, "removed": gone.rowcount})


@app.post("/v1/admin/servers/{server_id}/unblock")
async def admin_unblock(server_id: str, request: Request) -> JSONResponse:
    _admin(request)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE servers SET blocked = 0 WHERE server_id = ?", (server_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="unknown server")
        await db.commit()
    return JSONResponse({"ok": True, "server_id": server_id})


class PublicIdIn(BaseModel):
    public_id: str = Field(max_length=16)


@app.post("/v1/admin/servers/{server_id}/public-id")
async def admin_set_public_id(server_id: str, body: PublicIdIn,
                              request: Request) -> JSONResponse:
    """The operator's override to "an id is never reissued". That rule
    protects links already handed out, and nothing else — so the operator,
    who is the only one who can know whether any have been, is the only one
    who may break it.

    What it is FOR: an id that was minted wrong. The first of those was
    mine — the directory was deployed ahead of the servers posting to it,
    so ids were minted from beacons that carried no region and came out
    without their prefix (2026-09-14). Lazy minting stopped that happening
    again; this fixes the ones already on file.

    It is NOT a vanity-id service. Exposing it to owners would let anyone
    claim an id that implies officialdom, and there is no authority here to
    adjudicate that."""
    _admin(request)
    want = body.public_id.strip().upper()
    # The same shape mint_public_id produces, and the same alphabet: an id
    # a person cannot read back correctly is worse than a random one.
    if not (4 <= len(want) <= 16) or any(c not in ID_ALPHABET for c in want):
        raise HTTPException(
            status_code=400,
            detail=f"a public id is 4-16 characters from {ID_ALPHABET}")
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT public_id FROM servers WHERE server_id = ?", (server_id,))).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown server")
        try:
            await db.execute("UPDATE servers SET public_id = ? WHERE server_id = ?",
                             (want, server_id))
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail="that id is taken")
    log.warning("public id for %s set to %s by the operator (was %s)",
                server_id, want, row[0] or "unset")
    return JSONResponse({"ok": True, "server_id": server_id,
                         "public_id": want, "previous": row[0]})


@app.delete("/v1/admin/servers/{server_id}")
async def admin_forget(server_id: str, request: Request) -> JSONResponse:
    """Forget the pin and the beacons: the owner who lost a key (no
    rotation proof possible) starts over with trust on first use."""
    _admin(request)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM servers WHERE server_id = ?", (server_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="unknown server")
        await db.execute("DELETE FROM beacons WHERE server_id = ?", (server_id,))
        await db.execute("DELETE FROM future_stamps WHERE server_id = ?", (server_id,))
        await db.commit()
    log.warning("server %s forgotten by the operator", server_id)
    return JSONResponse({"ok": True, "server_id": server_id})


@app.get("/admin")
async def admin_page() -> Response:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=404)
    return FileResponse(STATIC / "admin.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    async with aiosqlite.connect(DB_PATH) as db:
        n = (await (await db.execute(
            "SELECT COUNT(*) FROM beacons WHERE expires_ms > ?",
            (int(time.time() * 1000),))).fetchone())[0]
    return JSONResponse({"ok": True, "live": n, "protocol": PROTOCOL})


@app.get("/")
async def index() -> Response:
    return FileResponse(STATIC / "index.html", media_type="text/html")
