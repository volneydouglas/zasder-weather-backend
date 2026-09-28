"""Neighbour stations (2.5, Doren's ask 2a).

Nearby Weather Underground stations, drawn beside yours in the Charts
comparison. The design was fixed on 2026-09-21 and this module follows it:

- **Never `devices` rows.** A neighbour is somebody else's station; it has
  no alerts, no rollups, no records, no share uploads, and a device row
  would drag it into every one of those. It lives in its own two tables,
  `neighbor_stations` and `neighbor_observations`.
- **Fetched lazily, when Compare opens.** Nothing polls in the background:
  the owner's WU key has a daily quota that their own imports and forecast
  share, and a neighbour nobody is looking at is not worth a call. The
  routes fetch when the stored copy is stale and serve what is stored.
- **`qcStatus` is honoured.** WU marks each station and observation as
  passed (1), failed (0) or unchecked (-1). A station that failed its
  quality check is listed as such and never drawn; a failed row is
  dropped.
- **History rows are AVERAGES, not instants.** WU's recent and archive
  observations are five-minute summaries (`tempAvg`, `windgustHigh`, ...).
  They are stored as the averages they are, under the same mapping the WU
  importer uses (`wu_import.transform_observation`), which already knows
  that `precipRate` is not `hourlyrainin`.

Units stay API-native: `units=e` returns the backend's storage units.
The API key never persists here, is never logged and never appears in an
error the routes return (`source_status.redact`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import Any

import httpx

from . import db, source_status
from .wu_import import transform_observation

log = logging.getLogger("zasder.neighbors")

NEAR_URL = "https://api.weather.com/v3/location/near"
RECENT_URL = "https://api.weather.com/v2/pws/observations/all/1day"
HISTORY_URL = "https://api.weather.com/v2/pws/history/all"

# How many stations to offer, and how far out. WU's near search returns
# ten; a station 40 km away is weather somewhere else.
MAX_STATIONS = 10
MAX_DISTANCE_KM = 25.0
# The near list moves slowly (stations come and go over months).
DISCOVERY_TTL_MS = 7 * 86_400_000
# The recent day is re-fetched at most this often per station.
RECENT_TTL_MS = 10 * 60_000
# How far back a comparison can reach. Each older day is one archive call
# per station, fetched once and kept.
MAX_DAYS = 7
# Observations older than this are swept when new ones are stored.
KEEP_MS = (MAX_DAYS + 1) * 86_400_000
TIMEOUT_S = 10.0

QC_PASSED, QC_FAILED, QC_UNCHECKED = 1, 0, -1

# A WU station ID: letters and digits (KPAIRWIN51, IPHOEN123). Validated
# before it is put into a URL or a query.
_STATION_ID = re.compile(r"^[A-Z0-9]{3,24}$")

# One fetch per station at a time. Built lazily and reset by the tests'
# conftest: an asyncio.Lock binds to the first event loop that awaits it.
_LOCKS: dict[str, asyncio.Lock] = {}


def valid_station_id(sid: str) -> bool:
    return bool(_STATION_ID.match(sid or ""))


class NotConfigured(Exception):
    """No WU API key on this server."""


class UpstreamError(Exception):
    """WU did not answer usefully. The message is already redacted."""


def _lock(key: str) -> asyncio.Lock:
    lk = _LOCKS.get(key)
    if lk is None:
        lk = _LOCKS[key] = asyncio.Lock()
    return lk


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


async def api_key() -> str | None:
    from .config import settings
    return await db.get_kv("wu_api_key") or settings.wu_api_key


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    """One WU call. The seam the tests replace. 204 (WU's "no data for
    that day") is an empty answer, not an error."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.get(url, params=params)
    except httpx.HTTPError as e:
        raise UpstreamError(source_status.redact(f"{type(e).__name__}: {e}")) from None
    if r.status_code == 204:
        return None
    if r.status_code in (401, 403):
        raise UpstreamError("Weather Underground refused the API key")
    if r.status_code == 429:
        raise UpstreamError("Weather Underground rate limit reached")
    if r.status_code >= 400:
        raise UpstreamError(f"Weather Underground answered HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise UpstreamError("Weather Underground sent something that is not JSON") from None


def parse_near(body: Any) -> list[dict[str, Any]]:
    """WU's parallel-array near answer → one dict per station."""
    loc = (body or {}).get("location") or {}
    ids = loc.get("stationId") or []
    out = []
    for i, sid in enumerate(ids):
        def at(key: str) -> Any:
            arr = loc.get(key) or []
            return arr[i] if i < len(arr) else None
        sid = str(sid or "").upper()
        lat, lon = at("latitude"), at("longitude")
        if not valid_station_id(sid) or not isinstance(lat, (int, float)) \
                or not isinstance(lon, (int, float)):
            continue
        qc = at("qcStatus")
        out.append({"station_id": sid, "name": at("stationName") or sid,
                    "lat": float(lat), "lon": float(lon),
                    "qc_status": int(qc) if isinstance(qc, (int, float)) else QC_UNCHECKED})
    return out


async def _own_wu_ids() -> set[str]:
    """This server's own stations on WU: never offered as a neighbour."""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT wu_station_id FROM wu_station_map")).fetchall()
    return {str(r[0]).upper() for r in rows if r[0]}


def _discovery_key(lat: float, lon: float) -> str:
    return f"neighbors.discovered.{lat:.2f},{lon:.2f}"


def _ids_key(lat: float, lon: float) -> str:
    """Membership of one search, by the EXACT point it searched from: the
    discovery key rounds to 0.01 degrees, so two stations in one cell
    shared a list and the second search overwrote the first's (Greptile,
    PR #50)."""
    return f"neighbors.ids.{lat:.5f},{lon:.5f}"


async def stations_near(lat: float, lon: float, now_ms: int | None = None,
                        refresh: bool = False) -> list[dict[str, Any]]:
    """The nearby stations, nearest first, discovering them when the stored
    list for this spot is missing or a week old. Raises NotConfigured
    without a key and UpstreamError when a needed discovery fails."""
    now_ms = now_ms or int(time.time() * 1000)
    key = _discovery_key(lat, lon)
    async with _lock(key):
        # Judged by THIS point's own last search (PR #50): the shared cell
        # timestamp stayed fresh while another point in the same 0.01-degree
        # cell kept searching, so this point's list never aged out
        # (CodeRabbit); and a point with no list of its own (its cell
        # searched from elsewhere, or a search from before 2.5.1) listed
        # every stored station in range, vanished ones included (Greptile).
        raw_own = await db.get_kv(_ids_key(lat, lon))
        own_ms = 0
        if raw_own:
            parsed_own = json.loads(raw_own)
            own_ms = int(parsed_own.get("ms") or 0) if isinstance(parsed_own, dict) else 0
        stale = refresh or not own_ms or now_ms - own_ms > DISCOVERY_TTL_MS
        if stale:
            k = await api_key()
            if not k:
                raise NotConfigured()
            body = await _get_json(NEAR_URL, {"geocode": f"{lat},{lon}",
                                              "product": "pws", "format": "json",
                                              "apiKey": k})
            found = parse_near(body)
            async with db.connect() as conn:
                for s in found:
                    await conn.execute(
                        "INSERT INTO neighbor_stations "
                        "(station_id, name, lat, lon, qc_status, seen_ms) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(station_id) DO UPDATE SET name = excluded.name, "
                        "lat = excluded.lat, lon = excluded.lon, "
                        "qc_status = excluded.qc_status, seen_ms = excluded.seen_ms",
                        (s["station_id"], s["name"], s["lat"], s["lon"],
                         s["qc_status"], now_ms))
                await conn.commit()
            await db.set_kv(key, str(now_ms))
            await db.set_kv(_ids_key(lat, lon), json.dumps(
                {"ms": now_ms, "ids": [s["station_id"] for s in found]}))
            # Bounded (CodeRabbit, PR #50): a list no search has refreshed in
            # two discovery periods belongs to a point nothing asks about any
            # more (a moved station), and is swept.
            async with db.connect() as conn:
                await conn.execute(
                    "DELETE FROM server_kv WHERE k LIKE 'neighbors.ids.%' "
                    "AND COALESCE(json_extract(v, '$.ms'), 0) < ?",
                    (now_ms - 2 * DISCOVERY_TTL_MS,))
                await conn.commit()
    # Only what THIS spot's latest search returned (R25-30, the 2.5
    # detailed review): a station WU stopped listing kept its old row and
    # stayed on the list forever. Kept per spot, not by the station's
    # shared seen_ms, which another spot's search refreshes (CodeRabbit and
    # Greptile, PR #50). Every point has its list by here (a point without
    # one searched above).
    raw_ids = await db.get_kv(_ids_key(lat, lon))
    parsed = json.loads(raw_ids) if raw_ids else {}
    listed = set(parsed.get("ids") or [] if isinstance(parsed, dict) else parsed or [])
    own = await _own_wu_ids()
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT station_id, name, lat, lon, qc_status, seen_ms, fetched_ms "
            "FROM neighbor_stations")).fetchall()
    out = []
    for r in rows:
        if r["station_id"] in own or (listed is not None and r["station_id"] not in listed):
            continue
        d = distance_km(lat, lon, r["lat"], r["lon"])
        if d > MAX_DISTANCE_KM:
            continue
        out.append({"id": r["station_id"], "name": r["name"],
                    "lat": round(r["lat"], 3), "lon": round(r["lon"], 3),
                    "distance_km": round(d, 1),
                    "qc": {QC_PASSED: "passed", QC_FAILED: "failed"}.get(
                        r["qc_status"], "unchecked"),
                    "fetched_ms": r["fetched_ms"]})
    out.sort(key=lambda s: s["distance_km"])
    return out[:MAX_STATIONS]


async def known_station(sid: str) -> dict[str, Any] | None:
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT station_id, name, qc_status, fetched_ms FROM neighbor_stations "
            "WHERE station_id = ?", (sid,))).fetchone()
    return dict(row) if row else None


def _row(o: dict[str, Any], sid: str) -> dict[str, Any] | None:
    """A WU observation → the stored neighbour row, or None for a row that
    failed WU's quality check or has no timestamp."""
    if o.get("qcStatus") == QC_FAILED:
        return None
    t = transform_observation(o, sid)
    if t is None:
        return None
    return {"ts_ms": t["dateutc"], "tempf": t.get("tempf"),
            "humidity": t.get("humidity"), "dew_point": t.get("dewPoint"),
            "feels_like": t.get("feelsLike"),
            "windspeedmph": t.get("windspeedmph"),
            "windgustmph": t.get("windgustmph"), "winddir": t.get("winddir"),
            "baromrelin": t.get("baromrelin"),
            "dailyrainin": t.get("dailyrainin")}


_OBS_COLS = ("tempf", "humidity", "dew_point", "feels_like", "windspeedmph",
             "windgustmph", "winddir", "baromrelin", "dailyrainin")


async def _store(sid: str, rows: list[dict[str, Any]], now_ms: int) -> int:
    if not rows:
        return 0
    cols = ", ".join(_OBS_COLS)
    marks = ", ".join("?" for _ in _OBS_COLS)
    async with db.connect() as conn:
        await conn.executemany(
            f"INSERT OR REPLACE INTO neighbor_observations (station_id, ts_ms, {cols}) "
            f"VALUES (?, ?, {marks})",
            [(sid, r["ts_ms"], *(r[c] for c in _OBS_COLS)) for r in rows])
        await conn.execute(
            "DELETE FROM neighbor_observations WHERE ts_ms < ?", (now_ms - KEEP_MS,))
        await conn.commit()
    return len(rows)


async def _fetched_days(sid: str) -> set[str]:
    raw = await db.get_kv(f"neighbors.days.{sid}")
    return set(raw.split(",")) if raw else set()


async def refresh(sid: str, start_ms: int, now_ms: int | None = None) -> None:
    """Make the stored copy of `sid` cover [start_ms, now]: the recent day
    when it is stale, plus any older whole day not yet fetched (an archive
    day never changes, so it is fetched once)."""
    from datetime import datetime, timedelta, timezone
    now_ms = now_ms or int(time.time() * 1000)
    k = await api_key()
    if not k:
        raise NotConfigured()
    async with _lock("station." + sid):
        st = await known_station(sid)
        fetched = (st or {}).get("fetched_ms")
        if not fetched or now_ms - int(fetched) > RECENT_TTL_MS:
            body = await _get_json(RECENT_URL, {"stationId": sid, "format": "json",
                                                "units": "e", "apiKey": k})
            rows = [r for r in (_row(o, sid) for o in (body or {}).get(
                "observations") or []) if r]
            await _store(sid, rows, now_ms)
            async with db.connect() as conn:
                await conn.execute("UPDATE neighbor_stations SET fetched_ms = ? "
                                   "WHERE station_id = ?", (now_ms, sid))
                await conn.commit()
        # Older days: UTC dates, the archive's own key. The recent call
        # covers the last 24 hours, so only days ending before that.
        start_ms = max(start_ms, now_ms - MAX_DAYS * 86_400_000)
        recent_edge = now_ms - 86_400_000
        done = await _fetched_days(sid)
        day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        last = datetime.fromtimestamp(recent_edge / 1000, timezone.utc).date()
        today = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
        while day <= last and day < today:
            key = day.strftime("%Y%m%d")
            if key not in done:
                body = await _get_json(HISTORY_URL, {"stationId": sid, "date": key,
                                                     "format": "json", "units": "e",
                                                     "apiKey": k})
                rows = [r for r in (_row(o, sid) for o in (body or {}).get(
                    "observations") or []) if r]
                await _store(sid, rows, now_ms)
                done.add(key)
                keep = sorted(done)[-(MAX_DAYS + 2):]
                await db.set_kv(f"neighbors.days.{sid}", ",".join(keep))
            day += timedelta(days=1)


async def history(sid: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Stored rows for `sid` in the window, oldest first, in the shape the
    apps decode as an Observation."""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT ts_ms, " + ", ".join(_OBS_COLS) + " FROM neighbor_observations "
            "WHERE station_id = ? AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms",
            (sid, start_ms, end_ms))).fetchall()
    out = []
    for r in rows:
        out.append({"dateutc": r["ts_ms"], "tempf": r["tempf"],
                    "humidity": r["humidity"], "dewPoint": r["dew_point"],
                    "feelsLike": r["feels_like"],
                    "windspeedmph": r["windspeedmph"],
                    "windgustmph": r["windgustmph"], "winddir": r["winddir"],
                    "baromrelin": r["baromrelin"],
                    "dailyrainin": r["dailyrainin"]})
    return out


# ── Neighbour QC (2.5, C3) ───────────────────────────────────────────────
# Once neighbours are on hand, a sensor that runs away from all of them is
# visible: a radiation shield in the afternoon sun reads hot against the
# street, a clogged hygrometer reads damp, a barometer that was never set
# reads off by a constant. Hour by hour over the last day, the station's
# hourly mean against the MEDIAN of the neighbours that passed WU's quality
# check; a field is "drifting" when the median gap is past its threshold
# AND the gap keeps one sign most of the hours (a passing shower that hit
# one yard and not the next is not a sensor problem).
#
# Uses only neighbour rows already stored (they are fetched when Compare
# opens; nothing here polls). Needs at least MIN_NEIGHBOURS neighbours with
# data in an hour for that hour to count, and MIN_HOURS such hours.

DRIFT_FIELDS = {
    # field: (station column, neighbour column, threshold, unit)
    "tempf": ("tempf", "tempf", 4.0, "°F"),
    "humidity": ("humidity", "humidity", 12.0, "%"),
    "baromrelin": ("baromrelin", "baromrelin", 0.08, "inHg"),
}
MIN_NEIGHBOURS = 2
MIN_HOURS = 8
SAME_SIGN_SHARE = 0.75


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def drift_verdict(gaps: list[float], threshold: float) -> dict[str, Any] | None:
    """Pure: hourly gaps (station minus neighbour median) → the field's
    verdict, or None with too few hours."""
    if len(gaps) < MIN_HOURS:
        return None
    med = _median(gaps)
    same = sum(1 for g in gaps if (g > 0) == (med > 0) and g != 0) / len(gaps)
    drifting = abs(med) >= threshold and same >= SAME_SIGN_SHARE
    return {"median_gap": round(med, 2), "hours": len(gaps),
            "same_sign_share": round(same, 2), "drifting": drifting}


async def drift(mac: str, coords: tuple[float, float] | None,
                now_ms: int | None = None, hours: int = 24) -> dict[str, Any]:
    """Only stations within MAX_DISTANCE_KM of `coords` are witnesses: the
    neighbour tables hold every station any of this server's stations was
    compared with, and a station 400 km off agreeing with another is no
    evidence about this yard (Greptile, PR #48). No location, no witnesses."""
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - hours * 3_600_000
    cols = ", ".join(f"AVG({c[0]})" for c in DRIFT_FIELDS.values())
    ncols = ", ".join(f"AVG({c[1]})" for c in DRIFT_FIELDS.values())
    async with db.connect() as conn:
        mine = await (await conn.execute(
            f"SELECT dateutc_ms / 3600000 AS h, {cols} FROM observations "
            f"WHERE mac = ? AND dateutc_ms >= ? GROUP BY h", (mac, since))).fetchall()
        theirs = await (await conn.execute(
            f"SELECT o.station_id, o.ts_ms / 3600000 AS h, s.lat, s.lon, {ncols} "
            f"FROM neighbor_observations o JOIN neighbor_stations s "
            f"ON s.station_id = o.station_id "
            f"WHERE o.ts_ms >= ? AND COALESCE(s.qc_status, -1) != ? "
            f"GROUP BY o.station_id, h", (since, QC_FAILED))).fetchall()
    theirs = [r for r in theirs if coords is not None
              and distance_km(coords[0], coords[1], r[2], r[3]) <= MAX_DISTANCE_KM]
    stations = {r[0] for r in theirs}
    by_hour: dict[int, list[tuple]] = {}
    for r in theirs:
        by_hour.setdefault(int(r[1]), []).append(tuple(r)[4:])
    out: dict[str, Any] = {"hours": hours, "neighbours": len(stations), "fields": {}}
    for i, (field, (_, _, threshold, unit)) in enumerate(DRIFT_FIELDS.items()):
        gaps = []
        for r in mine:
            v = r[1 + i]
            vals = [n[i] for n in by_hour.get(int(r[0]), []) if n[i] is not None]
            if v is None or len(vals) < MIN_NEIGHBOURS:
                continue
            gaps.append(float(v) - _median([float(x) for x in vals]))
        verdict = drift_verdict(gaps, threshold)
        if verdict is not None:
            verdict["unit"] = unit
            out["fields"][field] = verdict
    return out
