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

-- Covering index for the chart-history aggregation (db.history bucketed
-- path). Includes every column that query reads so SQLite serves it
-- index-only and never touches the fat data_json-bearing rows — a 7d/3d
-- chart drops from ~9s to <0.1s. The trailing payload columns MUST stay in
-- sync with the bucketed SELECT in db.history(); adding a charted field
-- there without adding it here silently re-introduces the full-row fetch.
CREATE INDEX IF NOT EXISTS idx_obs_chart
    ON observations (mac, dateutc_ms, tempf, feels_like, humidity, baromrelin,
                     uv, windspeedmph, dew_point, solarradiation, hourlyrainin,
                     winddir, yearlyrainin);

-- Operator-set per-device location (lat/lon), entered from the iOS app's
-- per-device Location setting. Takes precedence over the ingest-time default
-- (config.forecast_lat/lon) so a station the operator pinned to a specific
-- place isn't overwritten by the next reading. Overlaid onto info.coords in
-- list_devices; the top-ordered device drives the forecast + sun/moon dial.
CREATE TABLE IF NOT EXISTS device_location (
    mac         TEXT PRIMARY KEY,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    label       TEXT,
    updated_ms  INTEGER
);

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

-- Per-device staleness-alert state. Persisted (not in-memory) so a Fly
-- restart / redeploy doesn't re-fire alerts for devices that are already
-- known-stale. `state` is 'ok' | 'stale'; `changed_ms` is when it last
-- flipped; `notified_ms` is when we last emailed about the current state.
CREATE TABLE IF NOT EXISTS device_alert_state (
    mac          TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    last_seen_ms INTEGER,
    changed_ms   INTEGER NOT NULL,
    notified_ms  INTEGER
);

-- App-managed alert PREFERENCES (distinct from secret SMTP transport, which
-- stays in env). Singleton global row + per-device overrides. NULL columns
-- mean "inherit the env default", so the app only stores what it changes.
CREATE TABLE IF NOT EXISTS alert_prefs (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    enabled               INTEGER,   -- 0/1, NULL = on (when transport configured)
    default_threshold_min REAL,      -- NULL = env ALERT_STALE_MINUTES
    repeat_hours          REAL,      -- NULL = env ALERT_REPEAT_HOURS
    recipients            TEXT,       -- comma-separated, NULL = env ALERT_EMAIL_TO
    -- App-managed SMTP transport (NULL = fall back to the env secret). The
    -- password is write-only over the API. Stored in the DB on the Fly
    -- volume rather than Fly's secret store — fine for a revocable,
    -- single-tenant Gmail App Password; never returned by GET /api/alerts.
    smtp_host             TEXT,
    smtp_port             INTEGER,
    smtp_username         TEXT,
    smtp_password         TEXT,
    smtp_from             TEXT,
    smtp_tls              INTEGER,
    smtp_ssl              INTEGER
);

CREATE TABLE IF NOT EXISTS device_alert_prefs (
    mac           TEXT PRIMARY KEY,
    monitor       INTEGER NOT NULL DEFAULT 1,   -- 0 = don't watch this device
    threshold_min REAL                          -- NULL = use default threshold
);

-- APNs device tokens registered by the iOS app. `env` records whether the
-- token came from a sandbox (dev) or production (App Store) build, since each
-- only works against the matching APNs host.
CREATE TABLE IF NOT EXISTS push_tokens (
    token        TEXT PRIMARY KEY,
    platform     TEXT NOT NULL DEFAULT 'ios',
    env          TEXT,
    created_ms   INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL
);

-- Server-side threshold alert rules (e.g. tempf above 100). target_mac NULL =
-- any device. comparator: above|below|equalTo. threshold is API-native units
-- (°F, mph, in, inHg). Edge-triggered: alert_rule_state tracks per-(rule,device)
-- triggered state so we fire once on crossing and re-arm when it clears.
CREATE TABLE IF NOT EXISTS alert_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_mac  TEXT,
    field       TEXT NOT NULL,
    comparator  TEXT NOT NULL,
    threshold   REAL NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_rule_state (
    rule_id    INTEGER NOT NULL,
    mac        TEXT NOT NULL,
    triggered  INTEGER NOT NULL DEFAULT 0,
    changed_ms INTEGER,
    PRIMARY KEY (rule_id, mac)
);

-- App-managed push-relay config (single row). Lets the iOS app point this
-- backend at a hosted push relay without a redeploy: the app does the App
-- Attest handshake with the relay, gets a token, and PUTs {url,token} here.
-- The token is write-only over the API (GET reports only whether it's set).
-- Resolved DB-over-env by apns.effective_relay (mirrors the SMTP pattern).
CREATE TABLE IF NOT EXISTS push_relay (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    relay_url   TEXT,
    relay_token TEXT,
    updated_ms  INTEGER
);
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
        # WAL lets the constant ingest writes and the chart-history reads run
        # without blocking each other. Under the default rollback journal a
        # multi-second history aggregation holds a lock that stalls ingest for
        # its whole duration. journal_mode persists in the DB header, so this
        # is effectively a one-time switch re-asserted on every boot.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript(SCHEMA)
        # Migrate older DBs: add any alert_prefs columns the schema gained
        # after the table was first created (SQLite CREATE IF NOT EXISTS
        # won't add columns to an existing table).
        cur = await db.execute("PRAGMA table_info(alert_prefs)")
        existing = {r[1] for r in await cur.fetchall()}
        for col, decl in (
            ("smtp_host", "TEXT"), ("smtp_port", "INTEGER"),
            ("smtp_username", "TEXT"), ("smtp_password", "TEXT"),
            ("smtp_from", "TEXT"), ("smtp_tls", "INTEGER"), ("smtp_ssl", "INTEGER"),
        ):
            if col not in existing:
                await db.execute(f"ALTER TABLE alert_prefs ADD COLUMN {col} {decl}")
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


async def last_stored_observation(mac: str) -> tuple[int, dict[str, Any]] | None:
    """(dateutc_ms, parsed data_json) of the single most recent stored row for
    `mac`, or None if the device has no history. Single-row lookup via
    idx_obs_mac_date. Used by the ingest write-throttle to decide whether a new
    reading is too close behind the last stored one. (Distinct from
    `latest_observation`, which composites non-null fields across a lookback
    window for the live /current view.)"""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT dateutc_ms, data_json FROM observations WHERE mac = ? "
            "ORDER BY dateutc_ms DESC LIMIT 1", (mac,)
        )).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["data_json"])
    except (json.JSONDecodeError, TypeError):
        data = {}
    return int(row["dateutc_ms"]), data


async def set_device_location(mac: str, lat: float, lon: float,
                              label: str | None, now_ms: int) -> None:
    """Persist an operator-set location for a device (iOS per-device Location
    setting). Overrides the ingest-time default in list_devices."""
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO device_location (mac, lat, lon, label, updated_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                lat = excluded.lat, lon = excluded.lon,
                label = excluded.label, updated_ms = excluded.updated_ms
            """,
            (mac, lat, lon, label, now_ms),
        )
        await db.commit()


async def device_locations() -> dict[str, dict[str, Any]]:
    """All operator-set locations, keyed by MAC."""
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, lat, lon, label FROM device_location"
        )).fetchall()
    return {r["mac"]: {"lat": r["lat"], "lon": r["lon"], "label": r["label"]}
            for r in rows}


async def list_devices() -> list[dict[str, Any]]:
    async with connect() as db:
        # Stable insertion order via rowid — first device added (typically the
        # operator's primary station, registered when the AWN poller booted)
        # stays first. The iOS app's Settings → Devices lets the user override
        # with a drag-to-reorder.
        rows = await (await db.execute(
            "SELECT mac, name, location, info_json, last_seen_ms FROM devices ORDER BY rowid"
        )).fetchall()
    overrides = await device_locations()
    out: list[dict[str, Any]] = []
    for r in rows:
        info = json.loads(r["info_json"]) if r["info_json"] else {}
        inner = info.get("info") or {}
        # Operator-set location wins over whatever the ingest path stamped.
        loc = overrides.get(r["mac"])
        if loc is not None:
            inner = {**inner, "coords": {
                "location": loc.get("label") or inner.get("location"),
                "coords": {"lat": loc["lat"], "lon": loc["lon"]}}}
        out.append({
            "mac": r["mac"],
            "name": r["name"],
            "location": r["location"],
            "lastSeen": r["last_seen_ms"],
            "lastData": info.get("lastData"),
            "info": inner,
        })
    return out


async def delete_device(mac: str) -> dict[str, int]:
    """Remove a device and everything tied to it. Used when a source goes
    away (e.g. you stop polling a cloud feed) so a stale row doesn't sit on
    the dashboard. Returns a count summary; device count = 0 means unknown MAC."""
    async with connect() as db:
        async def _del(sql: str) -> int:
            cur = await db.execute(sql, (mac,))
            return cur.rowcount or 0
        n_obs   = await _del("DELETE FROM observations WHERE mac = ?")
        n_devs  = await _del("DELETE FROM devices      WHERE mac = ?")
        n_pref  = await _del("DELETE FROM device_alert_prefs WHERE mac = ?")
        n_state = await _del("DELETE FROM device_alert_state WHERE mac = ?")
        n_rule  = await _del("DELETE FROM alert_rule_state   WHERE mac = ?")
        await db.commit()
    return {"devices": n_devs, "observations": n_obs,
            "alert_prefs": n_pref, "alert_state": n_state,
            "rule_state": n_rule}


async def get_alert_states() -> dict[str, dict[str, Any]]:
    """All persisted per-device alert states, keyed by MAC."""
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, state, last_seen_ms, changed_ms, notified_ms "
            "FROM device_alert_state"
        )).fetchall()
    return {
        r["mac"]: {
            "state": r["state"],
            "last_seen_ms": r["last_seen_ms"],
            "changed_ms": r["changed_ms"],
            "notified_ms": r["notified_ms"],
        }
        for r in rows
    }


async def upsert_alert_state(mac: str, state: str, last_seen_ms: int | None,
                             changed_ms: int, notified_ms: int | None) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO device_alert_state (mac, state, last_seen_ms, changed_ms, notified_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                state        = excluded.state,
                last_seen_ms = excluded.last_seen_ms,
                changed_ms   = excluded.changed_ms,
                notified_ms  = excluded.notified_ms
            """,
            (mac, state, last_seen_ms, changed_ms, notified_ms),
        )
        await db.commit()


_ALERT_PREF_COLS = ("enabled", "default_threshold_min", "repeat_hours", "recipients",
                    "smtp_host", "smtp_port", "smtp_username", "smtp_password",
                    "smtp_from", "smtp_tls", "smtp_ssl")


async def get_alert_prefs() -> dict[str, Any]:
    """Global alert preferences (singleton). NULLs mean 'inherit env default'."""
    async with connect() as db:
        row = await (await db.execute(
            f"SELECT {', '.join(_ALERT_PREF_COLS)} FROM alert_prefs WHERE id = 1"
        )).fetchone()
    if not row:
        return {c: None for c in _ALERT_PREF_COLS}
    return {c: row[c] for c in _ALERT_PREF_COLS}


async def set_alert_prefs(**fields: Any) -> None:
    """Update only the provided global-pref columns on the singleton row."""
    cols = [c for c in _ALERT_PREF_COLS if c in fields]
    if not cols:
        return
    async with connect() as db:
        await db.execute("INSERT OR IGNORE INTO alert_prefs (id) VALUES (1)")
        await db.execute(
            f"UPDATE alert_prefs SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = 1",
            [fields[c] for c in cols],
        )
        await db.commit()


async def get_device_alert_prefs() -> dict[str, dict[str, Any]]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, monitor, threshold_min FROM device_alert_prefs"
        )).fetchall()
    return {
        r["mac"]: {"monitor": bool(r["monitor"]), "threshold_min": r["threshold_min"]}
        for r in rows
    }


async def upsert_device_alert_pref(mac: str, monitor: bool,
                                   threshold_min: float | None) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO device_alert_prefs (mac, monitor, threshold_min)
            VALUES (?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                monitor = excluded.monitor,
                threshold_min = excluded.threshold_min
            """,
            (mac, 1 if monitor else 0, threshold_min),
        )
        await db.commit()


async def create_alert_rule(target_mac: str | None, field: str,
                            comparator: str, threshold: float) -> dict[str, Any]:
    now = int(__import__("time").time() * 1000)
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO alert_rules (target_mac, field, comparator, threshold, enabled, created_ms) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (target_mac, field, comparator, threshold, now),
        )
        await db.commit()
        rid = cur.lastrowid
    return {"id": rid, "target_mac": target_mac, "field": field,
            "comparator": comparator, "threshold": threshold, "enabled": True}


async def list_alert_rules(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT id, target_mac, field, comparator, threshold, enabled FROM alert_rules"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    async with connect() as db:
        rows = await (await db.execute(sql)).fetchall()
    return [{"id": r["id"], "target_mac": r["target_mac"], "field": r["field"],
             "comparator": r["comparator"], "threshold": r["threshold"],
             "enabled": bool(r["enabled"])} for r in rows]


async def delete_alert_rule(rule_id: int) -> int:
    async with connect() as db:
        cur = await db.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
        await db.execute("DELETE FROM alert_rule_state WHERE rule_id = ?", (rule_id,))
        await db.commit()
        return cur.rowcount or 0


async def set_alert_rule_enabled(rule_id: int, enabled: bool) -> dict[str, Any] | None:
    """Toggle a rule on/off. Returns the updated rule, or None if it doesn't exist."""
    async with connect() as db:
        cur = await db.execute("UPDATE alert_rules SET enabled = ? WHERE id = ?",
                               (1 if enabled else 0, rule_id))
        await db.commit()
        if not cur.rowcount:
            return None
        r = await (await db.execute(
            "SELECT id, target_mac, field, comparator, threshold, enabled "
            "FROM alert_rules WHERE id = ?", (rule_id,))).fetchone()
    return {"id": r["id"], "target_mac": r["target_mac"], "field": r["field"],
            "comparator": r["comparator"], "threshold": r["threshold"],
            "enabled": bool(r["enabled"])}


async def get_rule_states() -> dict[tuple[int, str], int]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT rule_id, mac, triggered FROM alert_rule_state")).fetchall()
    return {(r["rule_id"], r["mac"]): r["triggered"] for r in rows}


async def upsert_rule_state(rule_id: int, mac: str, triggered: int, changed_ms: int) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO alert_rule_state (rule_id, mac, triggered, changed_ms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(rule_id, mac) DO UPDATE SET
                triggered = excluded.triggered, changed_ms = excluded.changed_ms
            """,
            (rule_id, mac, triggered, changed_ms),
        )
        await db.commit()


async def register_push_token(token: str, platform: str, env: str | None) -> None:
    now = int(__import__("time").time() * 1000)
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO push_tokens (token, platform, env, created_ms, last_seen_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                platform = excluded.platform, env = excluded.env,
                last_seen_ms = excluded.last_seen_ms
            """,
            (token, platform, env, now, now),
        )
        await db.commit()


async def list_push_tokens() -> list[dict[str, Any]]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT token, platform, env FROM push_tokens")).fetchall()
    return [{"token": r["token"], "platform": r["platform"], "env": r["env"]} for r in rows]


async def remove_push_token(token: str) -> None:
    """Prune a token APNs rejected as dead (410 Unregistered / BadDeviceToken)."""
    async with connect() as db:
        await db.execute("DELETE FROM push_tokens WHERE token = ?", (token,))
        await db.commit()


async def get_push_relay() -> dict[str, Any] | None:
    """The app-managed relay config (single row), or None if unset."""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT relay_url, relay_token FROM push_relay WHERE id = 1")).fetchone()
    if row is None:
        return None
    return {"url": row["relay_url"], "token": row["relay_token"]}


async def set_push_relay(url: str | None, token: str | None) -> None:
    """Upsert the relay config. url/token = None clears that field."""
    now = int(__import__("time").time() * 1000)
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO push_relay (id, relay_url, relay_token, updated_ms)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                relay_url = excluded.relay_url,
                relay_token = excluded.relay_token,
                updated_ms = excluded.updated_ms
            """,
            (url, token, now),
        )
        await db.commit()


async def last_yearly_rain(mac: str) -> tuple[float, int] | None:
    """Most recent NON-NULL cumulative yearly-rain reading + its timestamp
    (ms) for a device. Used by the ingest glitch guard as the 'before' value
    (a dropped glitch leaves NULL, so this returns the last *good* reading)."""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT yearlyrainin, dateutc_ms FROM observations "
            "WHERE mac = ? AND yearlyrainin IS NOT NULL "
            "ORDER BY dateutc_ms DESC LIMIT 1", (mac,)
        )).fetchone()
    if not row or row["yearlyrainin"] is None:
        return None
    return (float(row["yearlyrainin"]), int(row["dateutc_ms"]))


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


def _derive_hourly_rain(rows: list[dict[str, Any]]) -> None:
    """Fill `hourlyrainin` for chart rows that don't have it.

    SDR / LilyGO sources only post the cumulative `yearlyrainin` counter, so
    the stored `hourlyrainin` column is NULL for those stations and the rain
    chart shows a flat zero even when it rained (the /current endpoint derives
    rain for the dashboard tile, but historical rows were never enriched — so
    the chart missed it). Here we reconstruct a trailing-1-hour rainfall series
    from the yearlyrainin delta: for each point at time t,
        hourlyrainin(t) = max(0, yearly(t) − yearly(at or before t − 1h)).
    Two-pointer over time-ordered rows, O(n). Rows that already carry a real
    hourlyrainin (e.g. AmbientWeather) are left untouched. Negative deltas
    (counter reset / rain-offset recalibration) clamp to 0.
    """
    HOUR_MS = 3_600_000
    j = 0
    for r in rows:
        if r.get("hourlyrainin") is not None:
            continue
        yr = r.get("yearlyrainin")
        t = r.get("dateutc")
        if yr is None or t is None:
            continue
        while j + 1 < len(rows) and (rows[j + 1].get("dateutc") or 0) <= t - HOUR_MS:
            j += 1
        # reference = cumulative yearly at/just-before (t − 1h); fall back to the
        # earliest row when the window doesn't reach back a full hour.
        ref_row = rows[j] if (rows[j].get("dateutc") or 0) <= t - HOUR_MS else rows[0]
        ref = ref_row.get("yearlyrainin")
        if ref is None:
            continue
        val = round(max(0.0, float(yr) - float(ref)), 3)
        r["hourlyrainin"] = val
        # Rain has no meaningful hi/lo band; flatten it to the derived value so
        # the chart's band renders as the line rather than a stale zero.
        if "hourlyrainin_min" in r:
            r["hourlyrainin_min"] = val
        if "hourlyrainin_max" in r:
            r["hourlyrainin_max"] = val


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

    Bucketed rows also carry `<field>_min` / `<field>_max` for the
    chartable fields. AVG() alone flattens the true extremes on 3d/7d
    windows, so charts drawn from these rows understate highs and
    overstate lows; the per-bucket range lets clients draw an honest
    hi/lo band around the averaged line. Old clients ignore the extra
    keys.
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
        parsed = [json.loads(r["data_json"]) for r in rows]
        _derive_hourly_rain(parsed)
        return parsed

    # Bucketed: GROUP BY (dateutc_ms / bucket_ms), AVG every numeric column.
    # bucket_ms is computed by us (not user input) so f-string interpolation
    # is safe here. The midpoint timestamp puts the point in the middle of
    # the bucket, which is what most chart libraries expect.
    half = bucket_ms // 2
    # Columns are restricted to exactly the set the iOS charts + dashboard
    # read from bucketed history, and they ALL live in idx_obs_chart so this
    # aggregation is served index-only (EXPLAIN: "USING COVERING INDEX").
    # That matters because every `observations` row carries a ~1 KB data_json
    # blob; touching a non-covered column forces a fetch of each of the tens
    # of thousands of fat rows in the window and turns a 7d chart into a ~9 s
    # query. Keep added columns in sync with idx_obs_chart, or the index stops
    # covering and the slowdown returns.
    sql = f"""
        SELECT
          (dateutc_ms / {bucket_ms}) * {bucket_ms} + {half} AS dateutc,
          AVG(tempf)          AS tempf,
          AVG(feels_like)     AS feelsLike,
          AVG(dew_point)      AS dewPoint,
          AVG(humidity)       AS humidity,
          AVG(baromrelin)     AS baromrelin,
          AVG(windspeedmph)   AS windspeedmph,
          AVG(winddir)        AS winddir,
          AVG(hourlyrainin)   AS hourlyrainin,
          MAX(yearlyrainin)   AS yearlyrainin,
          AVG(uv)             AS uv,
          AVG(solarradiation) AS solarradiation,
          MIN(tempf)          AS tempf_min,
          MAX(tempf)          AS tempf_max,
          MIN(feels_like)     AS feelsLike_min,
          MAX(feels_like)     AS feelsLike_max,
          MIN(dew_point)      AS dewPoint_min,
          MAX(dew_point)      AS dewPoint_max,
          MIN(humidity)       AS humidity_min,
          MAX(humidity)       AS humidity_max,
          MIN(baromrelin)     AS baromrelin_min,
          MAX(baromrelin)     AS baromrelin_max,
          MIN(windspeedmph)   AS windspeedmph_min,
          MAX(windspeedmph)   AS windspeedmph_max,
          MIN(hourlyrainin)   AS hourlyrainin_min,
          MAX(hourlyrainin)   AS hourlyrainin_max,
          MIN(solarradiation) AS solarradiation_min,
          MAX(solarradiation) AS solarradiation_max
        FROM observations
        WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?
        GROUP BY dateutc_ms / {bucket_ms}
        ORDER BY dateutc ASC
        LIMIT ?
    """
    async with connect() as db:
        rows = await (await db.execute(sql,
            (mac, start_ms, end_ms, limit))).fetchall()
    bucketed = [dict(r) for r in rows]
    _derive_hourly_rain(bucketed)
    return bucketed


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


async def _rain_col_at_or_before(mac: str, col: str, cutoff_ms: int) -> float | None:
    """Most recent value of a cumulative rain column at or before `cutoff_ms`.
    Falls back to the earliest value on file if no row sits before the cutoff
    (so a freshly-deployed sensor still gets sensible rollups). Returns None
    only if the device has zero non-null values for the column.

    `col` is an INTERNAL whitelisted column name (never user input), so the
    f-string interpolation is safe."""
    assert col in ("yearlyrainin", "monthlyrainin"), f"bad rain col: {col}"
    async with connect() as db:
        row = await (await db.execute(
            f"SELECT {col} AS v FROM observations "
            f"WHERE mac = ? AND dateutc_ms <= ? AND {col} IS NOT NULL "
            f"ORDER BY dateutc_ms DESC LIMIT 1",
            (mac, cutoff_ms),
        )).fetchone()
        if row:
            return row["v"]
        row = await (await db.execute(
            f"SELECT {col} AS v FROM observations "
            f"WHERE mac = ? AND {col} IS NOT NULL ORDER BY dateutc_ms ASC LIMIT 1",
            (mac,),
        )).fetchone()
    return row["v"] if row else None


async def yearly_rain_at_or_before(mac: str, cutoff_ms: int) -> float | None:
    """Most recent yearlyrainin at or before `cutoff_ms` (see _rain_col_at_or_before)."""
    return await _rain_col_at_or_before(mac, "yearlyrainin", cutoff_ms)


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
            "SELECT yearlyrainin, monthlyrainin FROM observations WHERE mac = ? "
            "AND (yearlyrainin IS NOT NULL OR monthlyrainin IS NOT NULL) "
            "ORDER BY dateutc_ms DESC LIMIT 1",
            (mac,),
        )).fetchone()
    if not row:
        return {"hourly_in": None, "daily_in": None,
                "weekly_in": None, "monthly_in": None}
    cur_year = None if row["yearlyrainin"] is None else float(row["yearlyrainin"])
    cur_month = None if row["monthlyrainin"] is None else float(row["monthlyrainin"])

    now_local = datetime.now(tz=tz)
    top_of_hour    = now_local.replace(minute=0, second=0, microsecond=0)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # US-meteorology convention: weeks start Sunday. Python's weekday():
    # Mon=0..Sun=6 → days since Sunday = (weekday + 1) % 7.
    start_of_week  = start_of_today - timedelta(days=(now_local.weekday() + 1) % 7)
    start_of_month = start_of_today.replace(day=1)
    start_of_month_ms = int(start_of_month.timestamp() * 1000)

    # Is the yearly counter trustworthy? The year contains the month, so a
    # correct yearlyrainin is always >= monthlyrainin. A yearly that's BELOW
    # the monthly (e.g. a Davis WeatherLink annual reset while a stale rain
    # offset clamps it to ~0) is broken — differencing it silently yields 0
    # for daily/weekly. When that happens, derive from the MONTHLY counter
    # (reliable, resets predictably at month start) instead. SDR/LilyGO
    # sensors post only the (lifetime, monotonic) yearly and no monthly, so
    # cur_month is None there and the trusted yearly path is unchanged.
    yearly_ok = cur_year is not None and (cur_month is None or cur_year + 1e-6 >= cur_month)

    out: dict[str, float | None] = {}
    for name, boundary in (("hourly_in", top_of_hour),
                            ("daily_in", start_of_today),
                            ("weekly_in", start_of_week),
                            ("monthly_in", start_of_month)):
        boundary_ms = int(boundary.timestamp() * 1000)
        if yearly_ok:
            prior = await yearly_rain_at_or_before(mac, boundary_ms)
            out[name] = None if prior is None else round(max(0.0, cur_year - prior), 3)
        else:
            out[name] = await _rollup_from_monthly(
                mac, name, boundary_ms, start_of_month_ms, cur_month)
    return out


async def _rollup_from_monthly(mac: str, name: str, boundary_ms: int,
                               start_of_month_ms: int,
                               cur_month: float | None) -> float | None:
    """Rain for a period from the MONTHLY counter, used when the yearly counter
    is unreliable (see rain_rollups). The monthly counter resets at the start
    of each month, so:
      * monthly period → the counter's current value directly.
      * boundary within the current month → simple difference.
      * a boundary before this month (only the weekly window can straddle a
        month boundary) → this month's total plus the tail of last month after
        the boundary.
    """
    if cur_month is None:
        return None
    if name == "monthly_in":
        return round(max(0.0, cur_month), 3)
    if boundary_ms >= start_of_month_ms:
        prior = await _rain_col_at_or_before(mac, "monthlyrainin", boundary_ms)
        return None if prior is None else round(max(0.0, cur_month - prior), 3)
    # Straddles the month boundary: this month's rain + last month's tail.
    prev_final = await _rain_col_at_or_before(mac, "monthlyrainin", start_of_month_ms - 1)
    prior = await _rain_col_at_or_before(mac, "monthlyrainin", boundary_ms)
    if prev_final is None or prior is None:
        return round(max(0.0, cur_month), 3)  # best effort: at least this month
    return round(max(0.0, cur_month + max(0.0, prev_final - prior)), 3)


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
