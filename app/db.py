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
    name = inner.get("name") or info.get("name")
    coords = inner.get("coords") or {}
    location = coords.get("location") or coords.get("address") or inner.get("location")
    last = info.get("lastData") or {}
    last_seen_ms = last.get("dateutc")
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO devices (mac, name, location, info_json, last_seen_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                name = excluded.name,
                location = excluded.location,
                info_json = excluded.info_json,
                last_seen_ms = excluded.last_seen_ms
            """,
            (mac, name, location, json.dumps(info), last_seen_ms),
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
    async with connect() as db:
        row = await (await db.execute(
            "SELECT data_json FROM observations WHERE mac = ? ORDER BY dateutc_ms DESC LIMIT 1",
            (mac,),
        )).fetchone()
    return json.loads(row["data_json"]) if row else None


async def history(
    mac: str, start_ms: int, end_ms: int, limit: int = 5000
) -> list[dict[str, Any]]:
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
