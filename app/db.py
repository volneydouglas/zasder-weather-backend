import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac          TEXT PRIMARY KEY,
    name         TEXT,
    location     TEXT,
    info_json    TEXT,
    last_seen_ms INTEGER
);

CREATE TABLE IF NOT EXISTS observations (
    mac            TEXT NOT NULL,
    dateutc_ms     INTEGER NOT NULL,
    data_json      TEXT NOT NULL,
    tempf          REAL,
    feels_like     REAL,
    dew_point      REAL,
    humidity       REAL,
    tempinf        REAL,
    humidityin     REAL,
    baromrelin     REAL,
    baromabsin     REAL,
    windspeedmph   REAL,
    windgustmph    REAL,
    maxdailygust   REAL,
    winddir        REAL,
    hourlyrainin   REAL,
    eventrainin    REAL,
    dailyrainin    REAL,
    weeklyrainin   REAL,
    monthlyrainin  REAL,
    yearlyrainin   REAL,
    uv             REAL,
    solarradiation REAL,
    PRIMARY KEY (mac, dateutc_ms)
);

CREATE INDEX IF NOT EXISTS idx_obs_mac_date
    ON observations (mac, dateutc_ms DESC);

-- "discoveries" = the long-tail of RF devices the SDR happens to hear that
-- aren't our configured sensors: neighbors' weather stations, TPMS from
-- passing cars, garage remotes, utility meters, etc. Useful for "what's
-- around me?" surveys without polluting the main observations table.
CREATE TABLE IF NOT EXISTS discoveries (
    model         TEXT NOT NULL,
    sensor_id     TEXT NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    sample_json   TEXT,
    PRIMARY KEY (model, sensor_id)
);

CREATE INDEX IF NOT EXISTS idx_discoveries_last_seen
    ON discoveries (last_seen_ms DESC);
"""


def _ensure_dir() -> None:
    parent = Path(settings.database_path).parent
    parent.mkdir(parents=True, exist_ok=True)


# Map our DB column -> AmbientWeather JSON field (handles camelCase fields).
_FIELD_MAP: dict[str, str] = {
    "tempf": "tempf",
    "feels_like": "feelsLike",
    "dew_point": "dewPoint",
    "humidity": "humidity",
    "tempinf": "tempinf",
    "humidityin": "humidityin",
    "baromrelin": "baromrelin",
    "baromabsin": "baromabsin",
    "windspeedmph": "windspeedmph",
    "windgustmph": "windgustmph",
    "maxdailygust": "maxdailygust",
    "winddir": "winddir",
    "hourlyrainin": "hourlyrainin",
    "eventrainin": "eventrainin",
    "dailyrainin": "dailyrainin",
    "weeklyrainin": "weeklyrainin",
    "monthlyrainin": "monthlyrainin",
    "yearlyrainin": "yearlyrainin",
    "uv": "uv",
    "solarradiation": "solarradiation",
}
_COLUMNS = list(_FIELD_MAP.keys())
# Numeric columns that can be queried via /summary (use the API field name).
QUERYABLE_FIELDS = set(_FIELD_MAP.values())


async def init_db() -> None:
    _ensure_dir()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def upsert_device(mac: str, info: dict[str, Any]) -> None:
    inner = info.get("info") or {}
    # Explicit name = operator-supplied device.name from the POST. Auto-name
    # is the source-derived fallback used only on first INSERT — see
    # ingest._device_label() / _auto_device_name() for the split.
    explicit_name = info.get("name")
    auto_name = info.get("auto_name")
    coords = inner.get("coords") or {}
    location = coords.get("location") or coords.get("address") or inner.get("location")
    last = info.get("lastData") or {}
    last_seen_ms = last.get("dateutc")
    # Effective name for the INSERT path: prefer explicit, fall back to
    # auto. On UPDATE, COALESCE preserves the existing row name when no
    # explicit name was provided — so a secondary source POSTing without
    # device.name doesn't flip the friendly name the operator (or first
    # source) set.
    insert_name = explicit_name or auto_name
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO devices (mac, name, location, info_json, last_seen_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                name = COALESCE(?, devices.name, excluded.name),
                location = excluded.location,
                info_json = excluded.info_json,
                last_seen_ms = excluded.last_seen_ms
            """,
            (mac, insert_name, location, json.dumps(info), last_seen_ms,
             explicit_name),
        )
        await db.commit()


async def insert_observations(mac: str, rows: list[dict[str, Any]]) -> int:
    """Insert observations, ignoring duplicates by (mac, dateutc). Returns rows added."""
    if not rows:
        return 0
    payload = []
    for r in rows:
        ts = r.get("dateutc")
        if ts is None:
            continue
        values = [r.get(_FIELD_MAP[c]) for c in _COLUMNS]
        payload.append((mac, ts, json.dumps(r), *values))
    async with connect() as db:
        cur = await db.executemany(
            f"""
            INSERT OR IGNORE INTO observations
              (mac, dateutc_ms, data_json, {", ".join(_COLUMNS)})
            VALUES (?, ?, ?, {", ".join("?" for _ in _COLUMNS)})
            """,
            payload,
        )
        await db.commit()
        return cur.rowcount or 0


async def list_devices() -> list[dict[str, Any]]:
    async with connect() as db:
        # Stable insertion order via rowid — first device added (typically the
        # operator's primary station, registered when the AWN poller booted)
        # stays first. The iOS app's Settings → Devices lets the user override
        # with a drag-to-reorder.
        rows = await (await db.execute(
            "SELECT mac, name, location, info_json, last_seen_ms FROM devices ORDER BY rowid"
        )).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        info = json.loads(r["info_json"]) if r["info_json"] else {}
        out.append({
            "mac": r["mac"],
            "name": r["name"],
            "location": r["location"],
            "lastSeen": r["last_seen_ms"],
            "lastData": info.get("lastData"),
            "info": info.get("info"),
        })
    return out


async def observation_count(mac: str) -> int:
    """Total stored rows for a device. Used by the public /status page."""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT COUNT(*) AS n FROM observations WHERE mac = ?", (mac,)
        )).fetchone()
    return row["n"] if row else 0


async def latest_observation(mac: str) -> dict[str, Any] | None:
    """Composite "latest" — for each AWN field, return the most recent
    NON-NULL value across the last ~5 minutes of observations. Fixes
    the partial-poster problem where a device has multiple producers
    posting different field subsets at different cadences (e.g.,
    LilyGO 433 posts every Atlas RF packet ~16-30s with rotating
    partial fields, while the Pi's sdr-relay coalesces every 60s with
    all fields). Using strict "latest row" loses fields between
    coalesced posts; this composite preserves them.

    Returns the dateutc of the freshest contributing row so the iOS
    app's "last update" indicator still moves forward in real time."""
    LOOKBACK_MS = 5 * 60 * 1000
    async with connect() as db:
        # Fetch the freshest row first to seed dateutc + any always-
        # present keys (helps with rows that have an unusual shape).
        freshest_row = await (await db.execute(
            "SELECT data_json, dateutc_ms FROM observations "
            "WHERE mac = ? ORDER BY dateutc_ms DESC LIMIT 1",
            (mac,),
        )).fetchone()
        if not freshest_row:
            return None
        cutoff_ms = freshest_row["dateutc_ms"] - LOOKBACK_MS
        recent_rows = await (await db.execute(
            "SELECT data_json FROM observations "
            "WHERE mac = ? AND dateutc_ms >= ? "
            "ORDER BY dateutc_ms DESC",
            (mac, cutoff_ms),
        )).fetchall()
    # Start from freshest row (preserves dateutc + any fields it has),
    # then fill in nulls from older rows in the lookback window.
    out: dict[str, Any] = dict(json.loads(freshest_row["data_json"]))
    for r in recent_rows[1:]:
        older = json.loads(r["data_json"])
        for k, v in older.items():
            if v is not None and out.get(k) is None:
                out[k] = v

    # Cross-device pressure/indoor fallback: if this device still has
    # no barometer reading and the operator has named a shared source
    # MAC, pull pressure (and indoor temp/humidity) from that source's
    # most recent observation. Use case: Atlas + WS-2000 outdoor
    # stations don't include a barometer; a co-located WH32B-paired
    # device (Crestview SDR) or Davis cloud does. Single env var lets
    # one source share its barometer with all the others.
    src_mac = settings.shared_barometer_source_mac
    needs_pressure = out.get("baromrelin") is None
    if src_mac and needs_pressure and src_mac != mac:
        from . import config  # avoid circular at module import
        async with connect() as db:
            src_row = await (await db.execute(
                "SELECT data_json FROM observations WHERE mac = ? "
                "AND baromrelin IS NOT NULL ORDER BY dateutc_ms DESC LIMIT 1",
                (src_mac,),
            )).fetchone()
        if src_row:
            src = json.loads(src_row["data_json"])
            for k in ("baromrelin", "baromabsin"):
                if out.get(k) is None and src.get(k) is not None:
                    out[k] = src[k]
            for k in ("tempinf", "humidityin"):
                if out.get(k) is None and src.get(k) is not None:
                    out[k] = src[k]
    return out


def _auto_bucket_ms(window_ms: int) -> int:
    """Pick a bucket size so a chart of `window_ms` returns a tractable
    number of points (~200-2000) without being capped by a row LIMIT.
    Returns 0 = no bucketing (return raw rows)."""
    span_h = window_ms / 3_600_000
    if span_h <= 6:    return 0                  # raw — typical SDR rate gives ~1.3K/6h
    if span_h <= 24:   return 60_000             # 1-min buckets → ≤1440 points
    if span_h <= 72:   return 5 * 60_000         # 5-min  → ≤864 points
    if span_h <= 168:  return 15 * 60_000        # 15-min → ≤672 points
    return 60 * 60_000                           # 1-hour → ≤720 points for 30d


async def history(
    mac: str, start_ms: int, end_ms: int, limit: int = 5000
) -> list[dict[str, Any]]:
    """Time-series for a device. Auto-downsamples for windows > 6h so the
    iOS app's 3d/7d charts don't get truncated by the row LIMIT.

    For raw windows: returns the parsed data_json (full source) so the
    Charts tab + Dashboard's recent-history both see identical shape.

    For bucketed windows: returns synthesized rows with AVG()-aggregated
    numeric fields and the bucket-midpoint timestamp. Same dict shape
    the iOS app already reads — just no `_source` (not needed for charts).
    """
    bucket_ms = _auto_bucket_ms(end_ms - start_ms)
    if bucket_ms == 0:
        async with connect() as db:
            rows = await (await db.execute(
                """
                SELECT data_json FROM observations
                WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?
                ORDER BY dateutc_ms ASC
                LIMIT ?
                """,
                (mac, start_ms, end_ms, limit),
            )).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # Bucketed: GROUP BY (dateutc_ms / bucket_ms), AVG every numeric column.
    # bucket_ms is computed by us (not user input) so f-string interpolation
    # is safe here. The midpoint timestamp puts the point in the middle of
    # the bucket, which is what most chart libraries expect.
    half = bucket_ms // 2
    sql = f"""
        SELECT
          (dateutc_ms / {bucket_ms}) * {bucket_ms} + {half} AS dateutc,
          AVG(tempf)          AS tempf,
          AVG(feels_like)     AS feelsLike,
          AVG(dew_point)      AS dewPoint,
          AVG(humidity)       AS humidity,
          AVG(tempinf)        AS tempinf,
          AVG(humidityin)     AS humidityin,
          AVG(baromrelin)     AS baromrelin,
          AVG(baromabsin)     AS baromabsin,
          AVG(windspeedmph)   AS windspeedmph,
          AVG(windgustmph)    AS windgustmph,
          MAX(maxdailygust)   AS maxdailygust,
          AVG(winddir)        AS winddir,
          AVG(hourlyrainin)   AS hourlyrainin,
          AVG(eventrainin)    AS eventrainin,
          AVG(dailyrainin)    AS dailyrainin,
          AVG(weeklyrainin)   AS weeklyrainin,
          AVG(monthlyrainin)  AS monthlyrainin,
          AVG(yearlyrainin)   AS yearlyrainin,
          AVG(uv)             AS uv,
          AVG(solarradiation) AS solarradiation
        FROM observations
        WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?
        GROUP BY dateutc_ms / {bucket_ms}
        ORDER BY dateutc ASC
        LIMIT ?
    """
    async with connect() as db:
        rows = await (await db.execute(sql,
            (mac, start_ms, end_ms, limit))).fetchall()
    return [dict(r) for r in rows]


async def upsert_discovery(model: str, sensor_id: str,
                           now_ms: int, sample: dict[str, Any]) -> None:
    """Bump the seen-count + last_seen for a (model, sensor_id) we've heard
    on the airwaves. Inserts a new row on first sighting with the full
    payload as `sample_json` (for "what does this device look like?"
    inspection). Subsequent sightings only update counters; the sample
    stays as captured the first time."""
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO discoveries (model, sensor_id, first_seen_ms,
                                     last_seen_ms, seen_count, sample_json)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(model, sensor_id) DO UPDATE SET
                last_seen_ms = excluded.last_seen_ms,
                seen_count   = seen_count + 1
            """,
            (model, sensor_id, now_ms, now_ms, json.dumps(sample)),
        )
        await db.commit()


async def list_discoveries(since_ms: int | None = None,
                           limit: int = 500) -> list[dict[str, Any]]:
    """Latest-seen-first list of distinct RF devices we've decoded."""
    where = "WHERE last_seen_ms >= ? " if since_ms else ""
    params: tuple = (since_ms, limit) if since_ms else (limit,)
    async with connect() as db:
        rows = await (await db.execute(
            f"""
            SELECT model, sensor_id, first_seen_ms, last_seen_ms,
                   seen_count, sample_json
            FROM discoveries
            {where}
            ORDER BY last_seen_ms DESC
            LIMIT ?
            """,
            params,
        )).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        sample = None
        if r["sample_json"]:
            try: sample = json.loads(r["sample_json"])
            except json.JSONDecodeError: pass
        out.append({
            "model": r["model"],
            "id": r["sensor_id"],
            "first_seen_ms": r["first_seen_ms"],
            "last_seen_ms": r["last_seen_ms"],
            "seen_count": r["seen_count"],
            "sample": sample,
        })
    return out


async def yearly_rain_at_or_before(mac: str, cutoff_ms: int) -> float | None:
    """Most recent yearlyrainin value for `mac` at or before `cutoff_ms`.
    Falls back to the earliest yearlyrainin we have on file if no row sits
    before the cutoff — so a freshly-deployed SDR sensor still gets sensible
    daily/weekly/monthly rollups (treated as "rain since start of monitoring"
    until our data span covers the full period). Returns None only if the
    device has zero yearlyrainin observations at all."""
    async with connect() as db:
        row = await (await db.execute(
            """
            SELECT yearlyrainin FROM observations
            WHERE mac = ? AND dateutc_ms <= ? AND yearlyrainin IS NOT NULL
            ORDER BY dateutc_ms DESC LIMIT 1
            """,
            (mac, cutoff_ms),
        )).fetchone()
        if row:
            return row["yearlyrainin"]
        # Fallback — first-ever value for the device.
        row = await (await db.execute(
            """
            SELECT yearlyrainin FROM observations
            WHERE mac = ? AND yearlyrainin IS NOT NULL
            ORDER BY dateutc_ms ASC LIMIT 1
            """,
            (mac,),
        )).fetchone()
    return row["yearlyrainin"] if row else None


async def rain_rollups(mac: str, tz_name: str = "UTC") -> dict[str, float | None]:
    """Compute hourly/daily/weekly/monthly rain by differencing the current
    yearlyrainin against historical yearlyrainin at the start of each period
    boundary (in local time per `tz_name`). Returns None for any period we
    can't compute (no qualifying row before the boundary). Clamps negatives
    to 0 to handle counter resets / calibration changes."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    async with connect() as db:
        row = await (await db.execute(
            "SELECT yearlyrainin FROM observations WHERE mac = ? "
            "AND yearlyrainin IS NOT NULL ORDER BY dateutc_ms DESC LIMIT 1",
            (mac,),
        )).fetchone()
    if not row or row["yearlyrainin"] is None:
        return {"hourly_in": None, "daily_in": None,
                "weekly_in": None, "monthly_in": None}
    current = float(row["yearlyrainin"])
    now_local = datetime.now(tz=tz)
    top_of_hour    = now_local.replace(minute=0, second=0, microsecond=0)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # US-meteorology convention: weeks start Sunday. Python's weekday():
    # Mon=0..Sun=6 → days since Sunday = (weekday + 1) % 7.
    start_of_week  = start_of_today - timedelta(days=(now_local.weekday() + 1) % 7)
    start_of_month = start_of_today.replace(day=1)
    out: dict[str, float | None] = {}
    for name, boundary in (("hourly_in", top_of_hour),
                            ("daily_in", start_of_today),
                            ("weekly_in", start_of_week),
                            ("monthly_in", start_of_month)):
        boundary_ms = int(boundary.timestamp() * 1000)
        prior = await yearly_rain_at_or_before(mac, boundary_ms)
        out[name] = None if prior is None else round(max(0.0, current - prior), 3)
    return out


async def aggregate(
    mac: str, field: str, start_ms: int, end_ms: int
) -> dict[str, Any]:
    """`field` is the public API field name (e.g. 'tempf', 'feelsLike')."""
    # Resolve the API field name to the DB column.
    inverse = {v: k for k, v in _FIELD_MAP.items()}
    if field not in inverse:
        raise ValueError(f"unknown field {field!r}")
    col = inverse[field]
    async with connect() as db:
        row = await (await db.execute(
            f"""
            SELECT
              MIN({col}) AS lo,
              MAX({col}) AS hi,
              AVG({col}) AS avg,
              COUNT({col}) AS n
            FROM observations
            WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?
            """,
            (mac, start_ms, end_ms),
        )).fetchone()
        hi_row = await (await db.execute(
            f"SELECT dateutc_ms FROM observations WHERE mac = ? AND dateutc_ms BETWEEN ? AND ? AND {col} = ? LIMIT 1",
            (mac, start_ms, end_ms, row["hi"]),
        )).fetchone() if row["hi"] is not None else None
        lo_row = await (await db.execute(
            f"SELECT dateutc_ms FROM observations WHERE mac = ? AND dateutc_ms BETWEEN ? AND ? AND {col} = ? LIMIT 1",
            (mac, start_ms, end_ms, row["lo"]),
        )).fetchone() if row["lo"] is not None else None
    return {
        "field": field,
        "min": row["lo"],
        "max": row["hi"],
        "avg": row["avg"],
        "count": row["n"],
        "minAt": lo_row["dateutc_ms"] if lo_row else None,
        "maxAt": hi_row["dateutc_ms"] if hi_row else None,
    }
