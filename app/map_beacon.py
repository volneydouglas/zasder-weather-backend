"""The map beacon (2.3): the owner's opt-in appearance on the shared
station map at maps.zasder.com. OFF by default. When on, every ten
minutes the server signs a small record — a fuzzed location, the
station's current outdoor conditions, and optionally a display name and
a visit link — and POSTs it to the directory. Switching off sends a
signed withdrawal. Beacons expire on their own, so a dead server drops
off the map without anyone's help.

What never leaves the server: the MAC (the station id is a hash of the
server id and MAC), exact coordinates (the location is snapped to a
0.5 km grid, Volney's call 2026-09-13), indoor readings, air-monitor
readings, credentials, the server URL unless the owner types a visit
link. The directory keeps only the latest beacon per station.

Identity: an Ed25519 key pair minted on first enable and kept in
server_kv beside the server id. The directory pins the public key it
first sees for a server id (trust on first use) and refuses a beacon
signed by any other, so nobody can impersonate a station they do not
run. The envelope carries the public key so a fresh directory can pin
it. Pure builders here; the tick calls `publish_if_due`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import config, db
from .version import __version__

log = logging.getLogger("zasder.map")

CONFIG_KEY = "map.share"
STATUS_KEY = "map.share.status"
KEY_KEY = "map.share.key"
# The key being retired during a rotation: it signs the hand-over proof
# on every beacon until the directory answers rotated=true, then it is
# dropped. Lost-key recovery (no old key to sign with) is the directory
# operator's "forget" action.
PREV_KEY_KEY = "map.share.key.prev"
# Withdrawals the directory has not yet taken (R23): a station switched
# away from, or the switch turned off, while the directory was down. The
# tick retries them until a 2xx, or until the beacon's own TTL has passed
# and the pin is gone anyway. JSON list of {"mac", "since_ms"}.
PENDING_WITHDRAW_KEY = "map.share.pending_withdraw"
# The stamp on the LAST message this server sent the directory, whatever
# kind it was (S3, 2026-09-16 review): the directory refuses a message
# stamped at or below the last one it accepted from this server, and a
# tick that withdrew a station and then published one with the same
# `now_ms` had its beacon 409'd, leaving the pin off the map for a whole
# interval. Every outgoing stamp is max(now, last + 1), persisted here so
# a restart cannot reissue one.
SENT_KEY = "map.share.last_sent_ms"
INTERVAL_MS = 10 * 60_000
TTL_MS = 3 * 3_600_000            # a beacon nobody renews is gone in three hours
# Location precision (Volney 09-14, "a drop down: exact, approximate,
# city"): the grid the true point is snapped to before anything leaves.
FUZZ_KM = 0.5                     # "area": within about half a kilometre
CITY_KM = 10.0                    # "city": within about ten kilometres
PRECISIONS = ("exact", "area", "city")
DEFAULT_PRECISION = "area"
# The outdoor set a stranger sees. Indoor, CO2, PM2.5, lightning and the
# rain counters that reveal a day's habits stay home.
CONDITION_FIELDS = ("tempf", "feelsLike", "humidity", "dewPoint",
                    "windspeedmph", "windgustmph", "winddir",
                    "baromrelin", "hourlyrainin", "dailyrainin", "uv",
                    "solarradiation")
PROTOCOL = 1
# What a publish that reached the send with the switch off answers (S1):
# not stamped as an error, since the owner asked for exactly this.
SHARING_OFF = "Map sharing is off."
# A rotation while the last one is still unacknowledged (S2): the retiring
# key is the only one the directory may still hold, and rotating again
# would throw away the one it actually has.
ROTATION_PENDING = ("The last key rotation is still waiting for the directory to "
                    "acknowledge it. Every beacon retries the hand-over; rotate "
                    "again once one has been accepted.")


class RotationRefused(RuntimeError):
    """`rotate` could not rotate; the message is the sentence for the owner."""


_last_publish_ms: dict[str, int] = {}
# One lock around mint + sign + post (R23): two first-enable calls used
# to mint two keys and the directory pinned whichever landed first; a
# rotate overlapping a publish could sign with a key the row no longer
# held. Built lazily — an asyncio.Lock binds to the first loop that
# awaits it and the suite runs asyncio.run() per test — and reset per
# test like main._PUBLIC_DASH_LOCK.
_LOCK: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:                 # no await between test and assignment
        _LOCK = asyncio.Lock()
    return _LOCK


def _reset_for_tests() -> None:
    global _LOCK
    _last_publish_ms.clear()
    _LOCK = None


# ── config + status (server_kv, like share_targets) ─────────────────────

async def get_config() -> dict[str, Any]:
    raw = await db.get_kv(CONFIG_KEY)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def set_config(cfg: dict[str, Any]) -> None:
    await db.set_kv(CONFIG_KEY, json.dumps(cfg) if cfg else None)


async def get_status() -> dict[str, Any]:
    raw = await db.get_kv(STATUS_KEY)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def _stamp(ok: bool, error: str | None, now_ms: int) -> None:
    st = await get_status()
    if ok:
        st["last_ok_ms"] = now_ms
        st["last_error"] = None
        st["last_error_ms"] = None
    else:
        st["last_error"] = (error or "unknown")[:200]
        st["last_error_ms"] = now_ms
    await db.set_kv(STATUS_KEY, json.dumps(st))


# ── identity ────────────────────────────────────────────────────────────

async def ensure_key() -> ed25519.Ed25519PrivateKey:
    """The server's map signing key, minted once and kept in server_kv."""
    async with _lock():
        return await _ensure_key_locked()


async def _ensure_key_locked() -> ed25519.Ed25519PrivateKey:
    """`ensure_key` for a caller already holding the lock."""
    raw = await db.get_kv(KEY_KEY)
    if raw:
        try:
            key = serialization.load_pem_private_key(raw.encode(), password=None)
            if isinstance(key, ed25519.Ed25519PrivateKey):
                return key
        except (ValueError, TypeError):
            log.warning("map signing key unreadable; minting a new one")
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    await db.set_kv(KEY_KEY, pem)
    return key


async def rotate_key() -> str:
    """Mint a new signing key; the current one moves to PREV_KEY_KEY so
    the next beacons carry its blessing of the new key. Returns the new
    public key.

    Refused (RotationRefused) while a rotation is still unacknowledged.
    It used to rotate anyway and keep the OLDEST unacknowledged key as
    the blesser, on the theory that the directory still held it; but a
    rotation the directory ACCEPTED whose reply was lost leaves the
    directory holding the current key, and a second rotation then threw
    that key away and sent A's blessing of C to a directory holding B,
    which no later message could satisfy (S2, 2026-09-16 review). The
    route resolves a pending rotation with a beacon first (`rotate`)."""
    async with _lock():
        if await db.get_kv(PREV_KEY_KEY):
            raise RotationRefused(ROTATION_PENDING)
        current = await _ensure_key_locked()
        await db.set_kv(PREV_KEY_KEY, _pem(current))
        new = ed25519.Ed25519PrivateKey.generate()
        await db.set_kv(KEY_KEY, _pem(new))
    _last_publish_ms.clear()          # the next tick sends the proof at once
    return public_key_b64(new)


async def rotate(devices: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    """The rotate route's body: hand over to a new key and send the proof
    now. A rotation still awaiting acknowledgement is resolved first with
    an ordinary beacon (any 2xx clears it, whichever key the directory
    turns out to hold); only if that lands does the key rotate. A
    directory that is down or refusing answers RotationRefused with the
    reason in a sentence, and nothing changes."""
    if await rotation_pending():
        res = await publish_once(devices, now_ms)
        if not res.get("ok"):
            raise RotationRefused(f"{ROTATION_PENDING} ({res.get('error')})")
    pubkey = await rotate_key()
    published = await publish_once(devices, now_ms)
    return {"ok": True, "pubkey": pubkey, "published": published,
            "rotation_pending": await rotation_pending()}


async def _prev_key() -> ed25519.Ed25519PrivateKey | None:
    raw = await db.get_kv(PREV_KEY_KEY)
    if not raw:
        return None
    try:
        key = serialization.load_pem_private_key(raw.encode(), password=None)
        return key if isinstance(key, ed25519.Ed25519PrivateKey) else None
    except (ValueError, TypeError):
        return None


async def rotation_pending() -> bool:
    return bool(await db.get_kv(PREV_KEY_KEY))


def _pem(key: ed25519.Ed25519PrivateKey) -> str:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def rotation_message(server_id: str, new_pubkey: str) -> bytes:
    """The bytes the OLD key signs; the directory builds the same."""
    return canonical({"rotate": server_id, "to": new_pubkey})


def public_key_b64(key: ed25519.Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def station_id(server_id: str, mac: str) -> str:
    """Stable, public, and not the MAC: sixteen hex of a hash."""
    return hashlib.sha256(f"{server_id}:{mac.upper()}".encode()).hexdigest()[:16]


# ── the beacon ───────────────────────────────────────────────────────────

def precision_of(cfg: dict[str, Any]) -> str:
    """The configured precision; a pre-09-14 row's exact_location flag
    still means what it meant."""
    p = cfg.get("location_precision")
    if p in PRECISIONS:
        return str(p)
    return "exact" if cfg.get("exact_location") else DEFAULT_PRECISION


def place(lat: float, lon: float, precision: str) -> tuple[float, float]:
    if precision == "exact":
        return round(lat, 5), round(lon, 5)
    return fuzz(lat, lon, CITY_KM if precision == "city" else FUZZ_KM)


def public_page_url() -> str | None:
    """The server's own public page, the ONLY visit link a beacon may
    carry (Volney 09-14: no typed URLs, nothing to spam with). Known
    only when PUBLIC_BASE_URL is set or the process is provably on Fly;
    https only, because the directory refuses anything else."""
    from .oauth import _fly_host, _public_base_url_origin
    origin = _public_base_url_origin()
    if origin is None:
        host = _fly_host()
        origin = f"https://{host}" if host else None
    if not origin or not origin.startswith("https://"):
        return None
    return origin.rstrip("/") + "/"


# How the map may reach this server from its pin. `direct` is the pre-2.3
# behaviour (the pin carries this server's own address); `id` hands the
# directory the address privately and the pin carries a /s/<id> redirect
# instead, so the address is not in the map's data for anyone to scrape;
# `none` publishes no link at all.
LINK_MODES = ("direct", "id", "none")
DEFAULT_LINK_MODE = "id"

_REGION_RE = re.compile(r"[,\s]\s*([A-Za-z]{2})\s*$")


def link_mode_of(cfg: dict[str, Any]) -> str:
    mode = str(cfg.get("link_mode") or "")
    return mode if mode in LINK_MODES else DEFAULT_LINK_MODE


async def region_hint() -> str | None:
    """Two letters for the front of the assigned public id, taken from the
    place the owner ALREADY typed for the public dashboard ("Chandler, AZ"
    -> "AZ"). Read from their own words rather than reverse-geocoded: the
    beacon's coordinates are deliberately fuzzed, a lookup table would be
    wrong along every border, and the id is permanent once minted. No
    recognisable place, no prefix — the id is simply all-random then."""
    from .main import _pd_effective
    try:
        loc = str((await _pd_effective()).get("location") or "")
    except Exception:                        # noqa: BLE001
        return None
    m = _REGION_RE.search(loc.strip())
    return m.group(1).upper() if m else None


async def public_page_enabled() -> bool:
    from .main import _pd_effective
    return bool((await _pd_effective())["enabled"])


def fuzz(lat: float, lon: float, km: float = FUZZ_KM) -> tuple[float, float]:
    """Snap to the centre of a `km` grid cell, so the same station always
    lands on the same point and the true location is anywhere in the
    cell. Longitude cells shrink with latitude so the cell stays square."""
    lat_step = km / 111.32
    flat = (math.floor(lat / lat_step) + 0.5) * lat_step
    # The longitude grid is sized from the CELL's latitude, not the
    # point's, so every point in a row shares one grid and the centre
    # is a fixed point of the snap.
    cos = max(0.05, math.cos(math.radians(flat)))
    lon_step = km / (111.32 * cos)
    flon = (math.floor(lon / lon_step) + 0.5) * lon_step
    flon = ((flon + 180) % 360) - 180
    return round(max(-90.0, min(90.0, flat)), 5), round(flon, 5)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def conditions(obs: dict[str, Any]) -> dict[str, float]:
    """The outdoor readings that go out; absent stays absent."""
    out: dict[str, float] = {}
    for f in CONDITION_FIELDS:
        v = _num(obs.get(f))
        if v is not None:
            out[f] = round(v, 2)
    return out


def build(*, server_id: str, station: dict[str, Any], cfg: dict[str, Any],
          now_ms: int, visit_url: str | None = None,
          region: str | None = None) -> dict[str, Any] | None:
    """The unsigned beacon, or None when the station cannot be placed
    (no coordinates) or has no reading. `visit_url` is the server's own
    public page when the owner chose to link it and the page is on;
    the caller resolves it (a pure builder does no I/O)."""
    from .share_targets import _coords
    coords = _coords(station)
    obs = station.get("lastData") or {}
    if coords is None or not obs:
        return None
    precision = precision_of(cfg)
    lat, lon = place(coords[0], coords[1], precision)
    observed = obs.get("dateutc")
    observed_ms = int(observed) if isinstance(observed, (int, float)) else now_ms
    beacon: dict[str, Any] = {
        "v": PROTOCOL,
        "server_id": server_id,
        "station_id": station_id(server_id, str(station.get("mac") or "")),
        "lat": lat, "lon": lon,
        "fuzzed": precision != "exact",
        "precision": precision,
        "tz": config.settings.timezone,
        "observed_ms": observed_ms,
        "sent_ms": now_ms,
        "expires_ms": now_ms + TTL_MS,
        "conditions": conditions(obs),
        "software": f"zasder-weather-backend/{__version__}",
    }
    if cfg.get("name_visible"):
        name = str(station.get("name") or "").strip()[:48]
        if name:
            beacon["name"] = name
    # No page to send anyone to means the mode is `none`, whatever the
    # config says. An `id` beacon with no address behind it would put a
    # Visit link on the pin that resolves to a 404.
    mode = link_mode_of(cfg)
    usable = bool(visit_url and visit_url.startswith("https://") and len(visit_url) <= 200)
    if not usable:
        mode = "none"
    beacon["link_mode"] = mode
    if mode != "none":
        # In `id` mode this still goes to the directory — it has to, or
        # /s/<id> has nowhere to send anyone — but the directory keeps it
        # and never serves it with the pin.
        beacon["visit_url"] = visit_url
    if mode == "id" and region:
        beacon["region"] = region
    kind = str((station.get("info") or {}).get("type") or "").strip()[:32]
    if kind:
        beacon["sensor"] = kind
    return beacon


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def sign(payload: dict[str, Any], key: ed25519.Ed25519PrivateKey,
         prev: ed25519.Ed25519PrivateKey | None = None) -> dict[str, Any]:
    """The envelope the directory takes: the beacon, its signature over
    the canonical JSON, and the public key that made it. With `prev`,
    the retiring key's blessing of the new one rides along."""
    sig = key.sign(canonical(payload))
    env: dict[str, Any] = {"beacon": payload, "sig": base64.b64encode(sig).decode(),
                           "pubkey": public_key_b64(key)}
    if prev is not None:
        proof = prev.sign(rotation_message(str(payload.get("server_id")), env["pubkey"]))
        env["rotation"] = {"prev_pubkey": public_key_b64(prev),
                           "sig": base64.b64encode(proof).decode()}
    return env


def withdrawal(*, server_id: str, station_id_: str, now_ms: int) -> dict[str, Any]:
    return {"v": PROTOCOL, "server_id": server_id, "station_id": station_id_,
            "withdraw": True, "sent_ms": now_ms}


def directory_url() -> str:
    return str(config.settings.map_directory_url).rstrip("/")


# ── the tick ─────────────────────────────────────────────────────────────

# The directory refuses one beacon a minute per server. That is a WAIT, not
# a fault: the beacon already on file is ours and the map is current, so the
# owner must not be shown a warning triangle over it (2026-09-14 — saving and
# then tapping Send now inside the same minute painted
# `HTTP 429: {"detail":"one beacon a minute"}` twice).
THROTTLED = "The directory takes one beacon a minute. The last one went out a moment ago."


def _explain(status: int, text: str) -> str:
    """The directory's own refusals, in the words the owner reads. Anything
    unrecognised keeps the raw status and body so a new refusal is still
    diagnosable rather than swallowed."""
    detail = ""
    try:
        body = json.loads(text)
        if isinstance(body, dict):
            detail = str(body.get("detail") or "")
    except ValueError:
        pass
    if status == 429 and "a minute" in detail:
        return THROTTLED
    if status == 429 and "stations" in detail:
        return detail
    if status == 403 and "blocked" in detail:
        return "The directory has blocked this server."
    if status == 403 and "key" in detail:
        return ("The directory holds a different signing key for this server. "
                "Rotate the key to hand over to the new one.")
    if status == 403 and "another server" in detail:
        return "Another server already lists that station."
    if status == 409:
        return "The directory already has a newer beacon from this server."
    return f"HTTP {status}: {text[:120]}"


async def _post(path: str, envelope: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """`(None, reply body)` on success, `(error sentence, {})` otherwise.
    Never raises. The body is returned rather than kept in a module
    global (R23): two overlapping posts read each other's reply."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(directory_url() + path, json=envelope,
                                  headers={"User-Agent": f"zasder-weather-backend/{__version__}"})
        # Only a 2xx is an acceptance. httpx does not follow redirects, so
        # a 3xx is a reply the directory never saw: stamping it as success
        # would let _close_rotation drop the retiring key on a bounce.
        if not 200 <= r.status_code < 300:
            return _explain(r.status_code, r.text), {}
        try:
            body = r.json()
        except ValueError:
            body = None
        return None, (body if isinstance(body, dict) else {})
    except Exception as e:                       # noqa: BLE001
        return f"{type(e).__name__}", {}


async def _next_sent_ms(now_ms: int) -> int:
    """The stamp for the next message to the directory: the wall clock,
    unless that would not be strictly later than the last message sent,
    in which case one more than that. Caller holds the lock; the value is
    persisted before it is used, so no two messages ever share one."""
    raw = await db.get_kv(SENT_KEY)
    try:
        last = int(raw) if raw else 0
    except ValueError:
        last = 0
    sent = max(int(now_ms), last + 1)
    await db.set_kv(SENT_KEY, str(sent))
    return sent


async def publish_once(devices: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    """One beacon right now (the app's Save and verify, and the tick).
    `now_ms` is the wall clock for the staleness check and the status
    stamp; the beacon's own `sent_ms` comes from `_next_sent_ms`."""
    from . import share_targets as st
    from .main import ensure_server_id
    cfg = await get_config()
    station = st.station_for(cfg, devices)
    if station is None:
        err = "the chosen station has no reading yet"
        await _stamp(False, err, now_ms)
        return {"ok": False, "error": err}
    server_id = await ensure_server_id()
    beacon = build(server_id=server_id, station=station, cfg=cfg, now_ms=now_ms,
                   visit_url=await resolved_visit_url(cfg),
                   region=await region_hint())
    if beacon is None:
        err = "the station has no location; set it under Devices"
        await _stamp(False, err, now_ms)
        return {"ok": False, "error": err}
    if now_ms - beacon["observed_ms"] > 2 * 3_600_000:
        err = "newest reading is over two hours old; not published"
        await _stamp(False, err, now_ms)
        return {"ok": False, "error": err}
    async with _lock():
        # S1: the switch is read again HERE, under the same lock the off
        # transition takes around its config write and tombstone. Save and
        # verify checks the switch, then awaits the device list; a PUT that
        # turned sharing off in that gap withdrew the pin, and the resumed
        # verify put it straight back. Nothing signs or sends after the
        # off transition has completed.
        if not (await get_config()).get("enabled"):
            return {"ok": False, "throttled": False, "error": SHARING_OFF,
                    "station_id": beacon["station_id"]}
        sent = await _next_sent_ms(now_ms)
        beacon["sent_ms"] = sent
        beacon["expires_ms"] = sent + TTL_MS
        key = await _ensure_key_locked()
        err, body = await _post("/v1/beacons", sign(beacon, key, await _prev_key()))
        if err == THROTTLED:
            # Not stamped as a failure: the last beacon stands and the tick
            # will send the next one. `last_error` is what the app paints a
            # warning over, and there is nothing here for the owner to fix.
            _last_publish_ms["beacon"] = now_ms
            return {"ok": False, "throttled": True, "error": err,
                    "station_id": beacon["station_id"]}
        await _stamp(err is None, err, now_ms)
        _last_publish_ms["beacon"] = now_ms
        if err is None:
            await _close_rotation()
            await _record_directory_reply(body)
    return {"ok": err is None, "throttled": False, "error": err,
            "station_id": beacon["station_id"]}


async def _record_directory_reply(reply: dict[str, Any]) -> None:
    """Keep what the directory told us about ourselves — the public id it
    assigned and the link it will show — so the owner can read their id in
    the app without the app having to ask the directory."""
    pid = reply.get("public_id")
    visit = reply.get("visit")
    if not isinstance(pid, str) or not pid:
        return
    st = await get_status()
    if st.get("public_id") == pid and st.get("visit") == visit:
        return
    st["public_id"] = pid
    st["visit"] = visit if isinstance(visit, str) else None
    await db.set_kv(STATUS_KEY, json.dumps(st))


async def _close_rotation() -> None:
    """Called after ANY 2xx from the directory: a beacon or a withdrawal
    it accepted was signed by the current key, so the current key is the
    one it has on file, and the retiring key can go. It used to wait for
    an explicit rotated=true (R23): a lost acknowledgement left PREV=A
    behind, the next rotate blessed C with A, and every beacon 403'd
    until the operator forgot the server. Caller holds the lock."""
    if await db.get_kv(PREV_KEY_KEY):
        await db.set_kv(PREV_KEY_KEY, None)
        log.info("map signing key rotation acknowledged by the directory")


async def resolved_visit_url(cfg: dict[str, Any]) -> str | None:
    """The public page, only when the owner linked it AND the page is on
    right now. Turning the public page off later silently drops the link
    from the next beacon; nothing stale stays advertised."""
    if link_mode_of(cfg) == "none":
        return None
    if not cfg.get("visit_public_page"):
        return None
    if not await public_page_enabled():
        return None
    return public_page_url()


def effective_mac(cfg: dict[str, Any] | None,
                  devices: list[dict[str, Any]]) -> str | None:
    """The mac a beacon goes out under: the configured one, or the station
    `station_for` would pick when nothing is configured. Unlike
    `station_for` an EXPLICIT mac is returned even when that station is
    currently silent — a station that has gone quiet still has a beacon on
    the directory, and that beacon still has to be withdrawable."""
    from . import share_targets as st
    want = str((cfg or {}).get("mac") or "").upper()
    if want:
        return want
    station = st.station_for(cfg, devices)
    mac = str((station or {}).get("mac") or "").upper()
    return mac or None


# What the PUT tells the owner when the directory did not take the
# withdrawal. Not an error status: the switch IS off (R23).
WITHDRAW_PENDING = ("The directory could not be reached to take the old pin down. "
                    "The server will keep trying, and the pin expires on its own "
                    "within three hours.")


async def withdraw_mac(mac: str, now_ms: int) -> dict[str, Any]:
    """The signed tombstone for ONE mac, whatever the config now says. A
    refusal or an unreachable directory queues the withdrawal for the
    tick to retry (`retry_pending_withdrawals`); the caller's own
    bookkeeping is already done, so the answer is never an exception."""
    async with _lock():
        return await _withdraw_mac_locked(mac, now_ms)


async def turn_off(cfg: dict[str, Any], mac: str | None, now_ms: int) -> dict[str, Any]:
    """The off transition, as one step under the publication lock (S1):
    the config write that flips the switch and the tombstone for the pin
    happen with nothing able to sign in between, so a publish that has
    already passed its own switch check finds the switch off when it
    reaches the send, and a publish already sending finishes before the
    switch flips and is then withdrawn by a later stamp. `mac` None means
    there was no pin to withdraw."""
    async with _lock():
        await set_config(cfg)
        if mac is None:
            return {"ok": False, "error": "no station to withdraw"}
        try:
            return await _withdraw_mac_locked(mac, now_ms)
        except Exception:                        # noqa: BLE001
            # The switch IS off; the tombstone is best-effort and the
            # tick retries whatever is queued.
            log.exception("map: could not withdraw the station")
            return {"ok": False, "error": "withdrawal failed"}


async def drop_pending_withdrawal(mac: str) -> None:
    """A station switched back on (or back to) while its withdrawal was
    still queued: that withdrawal is obsolete. Sending it would take down
    the pin the next beacon is about to renew (S3)."""
    mac = mac.upper()
    pending = await pending_withdrawals()
    kept = [p for p in pending if str(p.get("mac", "")).upper() != mac]
    if len(kept) != len(pending):
        await _set_pending_withdrawals(kept)
        log.info("map: queued withdrawal of %s dropped; the station is back on", mac)


async def _withdraw_mac_locked(mac: str, now_ms: int) -> dict[str, Any]:
    """`withdraw_mac` for a caller already holding the lock."""
    from .main import ensure_server_id
    server_id = await ensure_server_id()
    mac = mac.upper()
    key = await _ensure_key_locked()
    tomb = withdrawal(server_id=server_id,
                      station_id_=station_id(server_id, mac),
                      now_ms=await _next_sent_ms(now_ms))
    err, _body = await _post("/v1/withdraw", sign(tomb, key, await _prev_key()))
    if err is None:
        await _close_rotation()
        await _set_pending_withdrawals(
            [p for p in await pending_withdrawals() if p.get("mac") != mac])
    else:
        pending = await pending_withdrawals()
        if not any(p.get("mac") == mac for p in pending):
            pending.append({"mac": mac, "since_ms": now_ms})
            await _set_pending_withdrawals(pending)
        log.warning("map: withdrawal of %s not taken (%s); queued for retry", mac, err)
    return {"ok": err is None, "error": err}


async def pending_withdrawals() -> list[dict[str, Any]]:
    raw = await db.get_kv(PENDING_WITHDRAW_KEY)
    if not raw:
        return []
    try:
        lst = json.loads(raw)
    except ValueError:
        return []
    return [p for p in lst if isinstance(p, dict) and isinstance(p.get("mac"), str)] \
        if isinstance(lst, list) else []


async def _set_pending_withdrawals(pending: list[dict[str, Any]]) -> None:
    await db.set_kv(PENDING_WITHDRAW_KEY, json.dumps(pending) if pending else None)


async def retry_pending_withdrawals(now_ms: int) -> None:
    """Every INTERVAL_MS, whatever the switch says: the pins these name
    are public until the directory hears the tombstone. One whose beacon
    has outlived its TTL is dropped — it is off the map already."""
    pending = await pending_withdrawals()
    if not pending:
        return
    if now_ms - _last_publish_ms.get("withdraw", 0) < INTERVAL_MS:
        return
    _last_publish_ms["withdraw"] = now_ms
    for p in pending:
        since = p.get("since_ms")
        if isinstance(since, (int, float)) and now_ms - since > TTL_MS:
            await _set_pending_withdrawals(
                [q for q in await pending_withdrawals() if q.get("mac") != p["mac"]])
            log.info("map: withdrawal of %s dropped; its beacon has expired", p["mac"])
            continue
        try:
            await withdraw_mac(str(p["mac"]), now_ms)
        except Exception:                        # noqa: BLE001
            log.exception("map: withdrawal retry failed")


async def withdraw(devices: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    """The signed tombstone, sent when the switch goes off."""
    mac = effective_mac(await get_config(), devices)
    if mac is None:
        return {"ok": False, "error": "no station to withdraw"}
    return await withdraw_mac(mac, now_ms)


async def publish_if_due(devices: list[dict[str, Any]], now_ms: int) -> None:
    """Every INTERVAL_MS while enabled. Best-effort: nothing here may
    block or break the monitor tick."""
    try:
        await retry_pending_withdrawals(now_ms)
    except Exception:
        log.exception("map withdrawal retry failed")
    cfg = await get_config()
    if not cfg.get("enabled"):
        return
    if now_ms - _last_publish_ms.get("beacon", 0) < INTERVAL_MS:
        return
    _last_publish_ms["beacon"] = now_ms
    try:
        await publish_once(devices, now_ms)
    except Exception:
        log.exception("map beacon failed")
