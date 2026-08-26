import json
import logging
import math
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from .config import settings

log = logging.getLogger("zasder.db")

# Columns covered by idx_obs_chart. MUST stay in sync with the bucketed SELECT in
# db.history() — a column selected there but missing here drops the query off the
# covering index and back to fetching every fat data_json row.
_CHART_INDEX_COLS = [
    "mac", "dateutc_ms", "tempf", "feels_like", "humidity", "baromrelin", "uv",
    "windspeedmph", "dew_point", "solarradiation", "hourlyrainin", "winddir",
    "yearlyrainin", "windgustmph", "tempinf", "humidityin", "dailyrainin",
    "lightning_last_1hr",
    # Air quality (1.8, AirGradient). Adding them here + the SCHEMA CREATE
    # triggers the init_db rebuild probe, so existing DBs pay one index
    # rebuild at the upgrade boot (the lightning-backfill precedent).
    "pm25", "pm10", "co2", "tvoc_index", "nox_index",
]

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
    -- Lightning (Tempest; AcuRite Atlas can report it too). INTEGER on the
    -- counts so bucketed history serializes them as JSON ints — the app
    -- decodes strikes as Int? and a 731.0 would fail the whole row's decode.
    lightningcount        INTEGER,
    lightning_last_1hr    INTEGER,
    lightning_distance_mi REAL,
    -- Air quality (AirGradient, 1.8; AmbientWeather PM-capable stations can
    -- feed these too). µg/m³ for PM, ppm for CO₂, Sensirion index values
    -- for TVOC/NOx — no display conversion exists, stored as reported.
    pm1            REAL,
    pm25           REAL,
    pm10           REAL,
    co2            REAL,
    tvoc_index     REAL,
    nox_index      REAL,
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
                     winddir, yearlyrainin, windgustmph, tempinf, humidityin,
                     dailyrainin, lightning_last_1hr,
                     pm25, pm10, co2, tvoc_index, nox_index);

-- Records (db.records) do MIN/MAX + first-occurrence lookups per metric over
-- the full per-mac history. windgustmph + dailyrainin aren't in idx_obs_chart,
-- so those two records fell to full heap scans of the fat data_json rows
-- (~1 KB each) — 60s+ on a season of data. This covering index keeps the
-- peak-gust / wettest-day records index-only like the rest. Additive
-- (new name) so it builds once and doesn't disturb idx_obs_chart.
CREATE INDEX IF NOT EXISTS idx_obs_records
    ON observations (mac, dateutc_ms, windgustmph, dailyrainin);

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

-- Weather Underground station association: which WU station ID holds this
-- device's history. Drives the WU historical importer (and names its import
-- target); one WU station per device. 1.5 adds live upload (app/wu_upload.py):
-- upload_key is the WU *station* key (write-only over the API, like the WU
-- API key and the SMTP password — never returned by any endpoint), and
-- upload_enabled turns forwarding on. Stored on the volume rather than a
-- secret store for the same reason as alert_prefs.smtp_password: revocable,
-- single-tenant, and app-managed without a redeploy.
CREATE TABLE IF NOT EXISTS wu_station_map (
    mac            TEXT PRIMARY KEY,
    wu_station_id  TEXT NOT NULL,
    updated_ms     INTEGER,
    upload_key     TEXT,
    upload_enabled INTEGER
);

-- Small app-managed server config (write-only secrets like the WU API
-- key). DB value wins over the matching env var; NULL/absent = env.
CREATE TABLE IF NOT EXISTS server_kv (
    k TEXT PRIMARY KEY,
    v TEXT
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
    smtp_ssl              INTEGER,
    -- Storm summary (storm.py). NULL = inherit the env default, same
    -- convention as the SMTP block above, so an operator who configures it
    -- server-side is not overridden by an app that never set it.
    storm_summary         INTEGER,
    rain_start            INTEGER,
    storm_quiet_minutes   REAL,
    storm_min_total_in    REAL,
    -- 'all' (default, NULL = all) or 'device_down': which alert kinds may
    -- EMAIL. Push always delivers everything — this only scopes the email
    -- channel (user ask: device-down emails without threshold-rule emails).
    email_scope           TEXT,
    -- 1.7: 'push' | 'email' | 'both' — which channels carry STORM SUMMARIES
    -- specifically. NULL = legacy behavior (push always, email iff
    -- email_scope='all'), so nobody's delivery changes until they pick.
    storm_channels        TEXT
);

CREATE TABLE IF NOT EXISTS device_alert_prefs (
    mac           TEXT PRIMARY KEY,
    monitor       INTEGER NOT NULL DEFAULT 1,   -- 0 = don't watch this device
    threshold_min REAL,                         -- NULL = use default threshold
    -- 1.7: 0 = no storm summaries for THIS station, NULL/1 = summaries on
    -- (when the global toggle is on). Doren's ask: his haptic Tempest sits
    -- feet from the Davis and reported every storm twice.
    storm_summary INTEGER
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

-- Live Activity push-to-start tokens (1.7, nowcast phase 2). One row per
-- install, exactly like push_tokens — the app re-registers whenever iOS
-- rotates the token. kind is future-proofing: v1 stores only 'start'
-- (the rain-start Activity counts down client-side and auto-dismisses via
-- stale/dismissal dates, so no per-activity update tokens are needed yet).
CREATE TABLE IF NOT EXISTS live_activity_tokens (
    token        TEXT PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'start',
    env          TEXT,
    -- 1.8: kind 'update' rows carry which Activity they update ('rain',
    -- 'storm', 'heat'); NULL for 'start' rows (push-to-start tokens are
    -- app-wide, one per device, regardless of attribute type).
    activity     TEXT,
    created_ms   INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL
);

-- 1.8 outbound webhooks (Pillar B): alert events POSTed to user URLs,
-- HMAC-signed. last_ok_ms/last_error surface per-target health in the
-- API (the diagnostics-nobody-ships rule).
CREATE TABLE IF NOT EXISTS webhooks (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    secret      TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_ms  INTEGER NOT NULL,
    last_ok_ms  INTEGER,
    last_error  TEXT
);

-- 1.8 forecast snapshots (Pillar C): forecasts AS ISSUED, one row per
-- (provider, issue run, valid local day), so a later scorecard can
-- measure each provider's skill per lead time against the station's own
-- readings. Verification is impossible retroactively — that is the
-- entire reason this table ships a release before its UI.
CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,
    issued_ms  INTEGER NOT NULL,
    valid_date TEXT NOT NULL,     -- local YYYY-MM-DD
    lead_days  INTEGER NOT NULL,
    tmax_f     REAL,
    tmin_f     REAL,
    pop        REAL,              -- precipitation probability, %
    precip_in  REAL
);
CREATE INDEX IF NOT EXISTS idx_fsnap_valid
    ON forecast_snapshots (provider, valid_date);

-- 1.8 Storm Watch Live Activity bookkeeping, one row per open episode:
-- which episode the Activity was started for (so a restart never
-- push-starts a duplicate) and when we last pushed an update (throttle).
-- Separate from storm_state on purpose: that row is rewritten wholesale
-- by upsert_storm_state at every transition.
CREATE TABLE IF NOT EXISTS storm_watch_la (
    mac                TEXT PRIMARY KEY,
    episode_started_ms INTEGER NOT NULL,
    last_push_ms       INTEGER NOT NULL
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
    created_ms  INTEGER NOT NULL,
    -- 1.8 per-rule urgency: 'minor' | 'standard' | 'urgent' (Volney:
    -- users pick how loud each rule is). NULL = minor, the default.
    severity    TEXT
);

CREATE TABLE IF NOT EXISTS alert_rule_state (
    rule_id    INTEGER NOT NULL,
    mac        TEXT NOT NULL,
    triggered  INTEGER NOT NULL DEFAULT 0,
    changed_ms INTEGER,
    -- Re-arm dwell clock (1.7): when a triggered rule's value first cleared
    -- the deadband, or NULL when it hasn't / the clearance broke. The rule
    -- re-arms only after the value stays clear for the whole dwell window —
    -- the value-only deadband couldn't hold against wind's sample-to-sample
    -- noise (Doren, 2026-08-23: wind alerts every ~8 min all afternoon).
    clear_since_ms INTEGER,
    PRIMARY KEY (rule_id, mac)
);

-- Edge-trigger state for the built-in smart alerts (frost / heat / pressure
-- drop). kind = the smart-alert type; mirrors alert_rule_state so each fires
-- once on clear→triggered and re-arms when the condition clears.
-- Storm-summary tracker (storm.py). Deliberately tiny: only enough to know
-- whether an event is open and when rain was last seen. Every number in the
-- summary is computed from `observations` when the storm closes, so this row
-- cannot drift out of step with the history.
CREATE TABLE IF NOT EXISTS storm_state (
    mac            TEXT PRIMARY KEY,
    started_ms     INTEGER,
    last_rain_ms   INTEGER,
    -- Last cumulative counter reading, so the next tick can tell whether rain
    -- fell. Stored with its field name because yearly and daily are different
    -- scales and comparing across them would fabricate a huge increment.
    counter_field  TEXT,
    counter_value  REAL,
    -- Timestamp of that counter reading. A storm opens when the counter
    -- RISES, which is one reading after the rain actually began — without
    -- this the window would start late and drop the very increment that
    -- opened the event.
    counter_ms     INTEGER
);

-- New devices on probation. A MAC that looks like a bit-flipped twin of an
-- existing device (see app/device_probation.py) accumulates sightings here
-- instead of going straight into `devices`. A real station clears the bar in
-- minutes; a one-off corrupt packet never does, and its row is pruned.
CREATE TABLE IF NOT EXISTS pending_devices (
    mac         TEXT PRIMARY KEY,
    first_ms    INTEGER NOT NULL,
    -- Last sighting that COUNTED, not the last one seen. Receivers emit
    -- duplicates seconds apart, so the gap is measured against counted
    -- sightings or one burst would clear the bar on its own.
    last_ms     INTEGER NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 1,
    -- The known MAC this looks like a corruption of, kept for the log and
    -- for /api/devices diagnostics.
    suspect_of  TEXT
);

CREATE TABLE IF NOT EXISTS smart_alert_state (
    mac        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    triggered  INTEGER NOT NULL DEFAULT 0,
    changed_ms INTEGER,
    PRIMARY KEY (mac, kind)
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

-- App-minted read-only share tokens (Settings "Share read-only access").
-- Complements the GUEST_API_TOKENS env secret with the same trust model:
-- stored on the volume like alert_prefs.smtp_password and
-- wu_station_map.upload_key — revocable one by one, single-tenant, and
-- app-managed without a redeploy. Read-only is enforced where the env
-- guests are: membership in valid tokens, never in write_tokens.
CREATE TABLE IF NOT EXISTS guest_tokens (
    token        TEXT PRIMARY KEY,
    label        TEXT,
    created_ms   INTEGER,
    -- Best-effort "when did this share last authenticate" (1.7). NULL =
    -- never seen (or never seen since the column was added) — the UI says
    -- "never used", not "used at epoch 0". Stamped in memory on auth and
    -- flushed when the owner lists tokens; also in the ALTER list below.
    last_used_ms INTEGER
);

-- App-minted per-device ingest tokens (1.7). The single INGEST_TOKEN env
-- secret is shared by every board and poller, so rotating it silently
-- unpairs ALL of them at once — recovery needs physical access to each
-- board's setup key (security review 2026-08-19, finding 4). These are the
-- write-side siblings of guest_tokens: mint one per device ("915 board",
-- "WLL poller"), revoke one without touching the rest. Valid ONLY where
-- the env INGEST_TOKEN is valid (/ingest/custom, discovery) — never as a
-- read or write API credential.
CREATE TABLE IF NOT EXISTS ingest_tokens (
    token        TEXT PRIMARY KEY,
    label        TEXT,
    created_ms   INTEGER,
    last_used_ms INTEGER
);

-- Token auto-upgrade registry (1.7): one per-device assignment per station.
-- A device that authenticates with the SHARED ingest token and sends
-- X-Token-Upgrade is handed a freshly minted per-device token IN THE INGEST
-- RESPONSE — the one channel it already trusts and is already authenticated
-- on. The row remembers the assignment so re-delivery is idempotent (same
-- token every time until adopted, and again if the device loses its NVS and
-- falls back to the shared token). adopted_ms flips when the device first
-- authenticates WITH its assigned token.
CREATE TABLE IF NOT EXISTS ingest_token_assignments (
    mac        TEXT PRIMARY KEY,
    token      TEXT NOT NULL,
    created_ms INTEGER,
    adopted_ms INTEGER
);

-- Alert history (1.7, Doren: "Many times I accidentally swipe away or hit X
-- to clear the notifications ... now I have no idea what notifications I had
-- just gotten"). One row per HANDLED alert, written at the _deliver funnel so
-- every kind lands here — device-down, threshold rules, smart alerts, storm
-- summaries, rain-start nowcasts. Pruned to the newest rows on insert;
-- delivered=0 marks an alert that fired with no willing channel (muted by
-- scope / nothing configured) — it still happened, the app still shows it.
CREATE TABLE IF NOT EXISTS alert_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms     INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    mac       TEXT,
    title     TEXT NOT NULL,
    body      TEXT,
    delivered INTEGER NOT NULL DEFAULT 1
);
"""


def _ensure_dir() -> None:
    parent = Path(settings.database_path).parent
    parent.mkdir(parents=True, exist_ok=True)


def _tolerant_float(v: Any) -> float | None:
    """float(v) if it converts to a finite number, else None. REAL-affinity
    columns can hold TEXT (the poller path stores upstream JSON verbatim,
    with no coercion) — one junk row must degrade to "no value", not raise
    out of a read path. A ValueError escaping last_yearly_rain, for example,
    500'd every subsequent /ingest/custom for that MAC.

    Bools are junk too: a stored True is not a 1.0-inch reading. (This is
    the module's ONE finite-float sanitizer — a twin named _as_float grew
    850 lines away and the two drifted on exactly the bool rule; the
    2026-08-20 review merged them.)"""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_json_col(raw: Any) -> dict[str, Any]:
    """Parse a JSON TEXT column defensively, {} on NULL/corrupt/non-object.
    One corrupt row (manual sqlite edit, a restored/truncated backup) must
    degrade to a skipped row, not 500 every endpoint whose window contains
    it — the guarded pattern list_discoveries shares."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    # json.loads accepts the NaN/Infinity literals json.dumps(allow_nan=True)
    # wrote before _scrub_nonfinite guarded every writer — so a pre-fix row
    # would round-trip a non-finite float straight back into a JSONResponse
    # (allow_nan=False) and 500 it. Scrub on the way OUT too.
    return _scrub_nonfinite(parsed) if isinstance(parsed, dict) else {}


def _index_columns(create_sql: str) -> set[str]:
    """Column names parsed from a stored `CREATE INDEX ... (a, b, c)`
    statement. Used by the idx_obs_chart rebuild probe in init_db."""
    m = re.search(r"\(([^)]*)\)", create_sql)
    if not m:
        return set()
    return {c.strip().strip('"`[]') for c in m.group(1).split(",") if c.strip()}


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
    "lightningcount": "lightningcount",
    "lightning_last_1hr": "lightning_last_1hr",
    "lightning_distance_mi": "lightning_distance_mi",
    "pm1": "pm1",
    "pm25": "pm25",
    "pm10": "pm10",
    "co2": "co2",
    "tvoc_index": "tvoc_index",
    "nox_index": "nox_index",
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
        # Migrate observations BEFORE the schema script runs: SCHEMA also
        # creates idx_obs_chart, which lists lightning_last_1hr — on a
        # pre-1.6 database that is missing that index, the CREATE would
        # reference a column the table doesn't have yet and fail the boot.
        # (CREATE TABLE IF NOT EXISTS never adds columns, so this ALTER is
        # the only path that reaches an existing database — the alert_prefs
        # lesson.) Fresh databases skip this: the table doesn't exist yet
        # and SCHEMA below creates it with the columns in place.
        lightning_backfilled = False
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'")
        if await cur.fetchone():
            cur = await db.execute("PRAGMA table_info(observations)")
            existing = {r[1] for r in await cur.fetchall()}
            added_lightning = False
            for col, decl in (
                ("lightningcount", "INTEGER"),
                ("lightning_last_1hr", "INTEGER"),
                ("lightning_distance_mi", "REAL"),
            ):
                if col not in existing:
                    await db.execute(
                        f"ALTER TABLE observations ADD COLUMN {col} {decl}")
                    added_lightning = True
            # Air quality (1.8, AirGradient). No backfill: these fields
            # never rode data_json before the poller existed.
            for col in ("pm1", "pm25", "pm10", "co2",
                        "tvoc_index", "nox_index"):
                if col not in existing:
                    await db.execute(
                        f"ALTER TABLE observations ADD COLUMN {col} REAL")
            if added_lightning:
                # One-time backfill from data_json: the poller captured
                # lightning into the blob before these columns existed
                # (deliberately — the counters are interval-scoped, so a
                # storm that passes unrecorded is gone forever). Without
                # this, every strike stored before the upgrade would be
                # invisible to records/charts even though we hold the data.
                # json_extract of a missing key is NULL and CAST(NULL) is
                # NULL, so rows without a given key stay NULL — absent is
                # not zero. The LIKE bounds the rewrite to rows that carry
                # lightning at all (one full-table read, once, at boot).
                # typeof-gated: SQLite CAST('junk' AS INTEGER) is 0, so an
                # unguarded cast would backfill a non-numeric stored value
                # as a REAL zero-strike reading — a station that never
                # measured lightning would grow a "0" lightning record
                # (CodeRabbit, 2026-08-20; the read path's
                # _normalize_lightning degrades the same junk to None).
                bf = await db.execute(
                    """
                    UPDATE observations SET
                      lightningcount        = CASE WHEN typeof(json_extract(data_json, '$.lightningcount')) IN ('integer','real')
                                                   THEN CAST(json_extract(data_json, '$.lightningcount') AS INTEGER) END,
                      lightning_last_1hr    = CASE WHEN typeof(json_extract(data_json, '$.lightning_last_1hr')) IN ('integer','real')
                                                   THEN CAST(json_extract(data_json, '$.lightning_last_1hr') AS INTEGER) END,
                      lightning_distance_mi = CASE WHEN typeof(json_extract(data_json, '$.lightning_distance_mi')) IN ('integer','real')
                                                   THEN json_extract(data_json, '$.lightning_distance_mi') END
                    WHERE data_json LIKE '%"lightning%'
                    """)
                lightning_backfilled = (bf.rowcount or 0) > 0
        await db.executescript(SCHEMA)
        # Insights rollup tables (see app/insights.py). Created even when
        # the INSIGHTS flag is off — empty tables cost nothing and let the
        # flag flip on without a schema step.
        from .insights import SCHEMA as INSIGHTS_SCHEMA
        await db.executescript(INSIGHTS_SCHEMA)
        if lightning_backfilled:
            # The backfill rewrote observations, but daily_rollups' new
            # lightning_max column stays NULL until a rebuild folds history
            # in — and records() serves long periods from rollups, so an
            # upgraded INSIGHTS install would show "Most Lightning" under
            # Today and under no other period (CODE_REVIEW_R5 R5-14). The
            # flag makes records() fall back to raw until a full rebuild
            # clears it; lifespan kicks that rebuild off in the background.
            # Written AFTER executescript: a pre-1.5 database gets server_kv
            # from SCHEMA on this same boot.
            # Nonce value (not a constant) — rebuild()'s conditional clear
            # depends on it; see maintenance._mark_rollups_dirty.
            await db.execute(
                "INSERT INTO server_kv (k, v) VALUES "
                "('rollups_dirty', lower(hex(randomblob(8)))) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v")
        # Migrate older DBs: add any alert_prefs columns the schema gained
        # after the table was first created (SQLite CREATE IF NOT EXISTS
        # won't add columns to an existing table).
        cur = await db.execute("PRAGMA table_info(alert_prefs)")
        existing = {r[1] for r in await cur.fetchall()}
        for col, decl in (
            ("smtp_host", "TEXT"), ("smtp_port", "INTEGER"),
            ("smtp_username", "TEXT"), ("smtp_password", "TEXT"),
            ("smtp_from", "TEXT"), ("smtp_tls", "INTEGER"), ("smtp_ssl", "INTEGER"),
            ("email_scope", "TEXT"),
            # 1.6 storm summary. Adding these to the CREATE above is not
            # enough: every existing database already HAS alert_prefs, so
            # only this ALTER path reaches them — and without it
            # get_alert_prefs' SELECT raises "no such column" and takes the
            # whole alert system down on upgrade.
            ("storm_summary", "INTEGER"), ("storm_quiet_minutes", "REAL"),
            ("storm_min_total_in", "REAL"),
            # 1.7 rain-start nowcast. Same ALTER-list rule as the storm
            # columns above it: CREATE alone never reaches existing DBs.
            ("rain_start", "INTEGER"),
            # 1.7 storm-summary channel choice (push/email/both).
            ("storm_channels", "TEXT"),
            # 1.8 heat-day Live Activity (opt-in) + its trigger threshold.
            ("heat_day", "INTEGER"), ("heat_day_threshold_f", "REAL"),
            # 1.8 quiet hours + daily digest.
            ("quiet_start_min", "INTEGER"), ("quiet_end_min", "INTEGER"),
            ("digest_hour", "INTEGER"),
        ):
            if col not in existing:
                await db.execute(f"ALTER TABLE alert_prefs ADD COLUMN {col} {decl}")
        # 1.8: update-token discriminator, after the table shipped in 1.7.
        cur = await db.execute("PRAGMA table_info(live_activity_tokens)")
        existing = {r[1] for r in await cur.fetchall()}
        if "activity" not in existing:
            await db.execute(
                "ALTER TABLE live_activity_tokens ADD COLUMN activity TEXT")
        # 1.8 per-rule urgency + severity riding the alert history.
        cur = await db.execute("PRAGMA table_info(alert_rules)")
        existing = {r[1] for r in await cur.fetchall()}
        if "severity" not in existing:
            await db.execute(
                "ALTER TABLE alert_rules ADD COLUMN severity TEXT")
        cur = await db.execute("PRAGMA table_info(alert_log)")
        existing = {r[1] for r in await cur.fetchall()}
        if "severity" not in existing:
            await db.execute(
                "ALTER TABLE alert_log ADD COLUMN severity TEXT")
        # Same migration for device_alert_prefs: storm_summary (1.7 per-
        # station mute) came after the table shipped in 1.4. NULL = on.
        cur = await db.execute("PRAGMA table_info(device_alert_prefs)")
        existing = {r[1] for r in await cur.fetchall()}
        if "storm_summary" not in existing:
            await db.execute(
                "ALTER TABLE device_alert_prefs ADD COLUMN storm_summary INTEGER")
        # Same migration for alert_rule_state: the re-arm dwell clock (1.7)
        # came after the table shipped in 1.6. NULL = no clearance being
        # timed, which is exactly the pre-dwell behavior until a rule next
        # clears.
        cur = await db.execute("PRAGMA table_info(alert_rule_state)")
        existing = {r[1] for r in await cur.fetchall()}
        if "clear_since_ms" not in existing:
            await db.execute(
                "ALTER TABLE alert_rule_state ADD COLUMN clear_since_ms INTEGER")
        # Same migration for daily_rollups: lightning_max came after the
        # table shipped (1.6). Existing days read NULL (= "no data") until a
        # rebuild folds history in — never 0, a station with no detector
        # must stay absent from lightning records.
        cur = await db.execute("PRAGMA table_info(daily_rollups)")
        existing = {r[1] for r in await cur.fetchall()}
        if "lightning_max" not in existing:
            await db.execute(
                "ALTER TABLE daily_rollups ADD COLUMN lightning_max REAL")
        # Same migration for hour_rollups: feels_* came after the table
        # shipped. Existing rows get 0/0 (= "no data"), so the feels-like
        # diurnal grid stays empty until a rebuild folds history in.
        cur = await db.execute("PRAGMA table_info(hour_rollups)")
        existing = {r[1] for r in await cur.fetchall()}
        for col, decl in (
            ("feels_sum", "REAL NOT NULL DEFAULT 0"),
            ("feels_n", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in existing:
                await db.execute(f"ALTER TABLE hour_rollups ADD COLUMN {col} {decl}")
        # Same migration for wu_station_map: the 1.5 live-upload columns came
        # after the table shipped in 1.4. NULL upload_key / upload_enabled read
        # as "forwarding not configured", so existing associations keep their
        # import-only behavior.
        cur = await db.execute("PRAGMA table_info(wu_station_map)")
        existing = {r[1] for r in await cur.fetchall()}
        for col, decl in (
            ("upload_key", "TEXT"),
            ("upload_enabled", "INTEGER"),
        ):
            if col not in existing:
                await db.execute(f"ALTER TABLE wu_station_map ADD COLUMN {col} {decl}")
        # Same migration for guest_tokens: last_used_ms came after the table
        # shipped in 1.6. Existing shares read NULL = "never used" until
        # they next authenticate (absent is not zero — no epoch-0 stamps).
        cur = await db.execute("PRAGMA table_info(guest_tokens)")
        existing = {r[1] for r in await cur.fetchall()}
        if "last_used_ms" not in existing:
            await db.execute(
                "ALTER TABLE guest_tokens ADD COLUMN last_used_ms INTEGER")
        # idx_obs_chart gained windgustmph so the bucketed chart query can serve
        # wind gust index-only. SQLite won't alter an existing index, so rebuild
        # it once if the stored definition predates the column (one-time cost at
        # boot; the app isn't serving yet).
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_obs_chart'")
        row = await cur.fetchone()
        # Rebuild whenever ANY expected column is missing, rather than testing one
        # hard-coded name — the previous form would silently skip the rebuild for
        # the next column added to the bucketed SELECT, re-introducing the
        # full-row fetch it exists to avoid. Compare PARSED column names, not
        # substrings: "tempf" is a substring of "tempinf" (and "humidity" of
        # "humidityin"), so a stale index missing tempf passed the probe.
        if row and row[0] and not set(_CHART_INDEX_COLS) <= _index_columns(row[0]):
            await db.execute("DROP INDEX idx_obs_chart")
            await db.execute("CREATE INDEX idx_obs_chart ON observations ("
                             + ", ".join(_CHART_INDEX_COLS) + ")")
        await db.commit()

        # Probe for SQLite's math functions once — they drive the circular mean
        # used for bucketed wind direction (see _winddir_expr).
        global _HAS_MATH_FUNCS
        try:
            await db.execute("SELECT DEGREES(ATAN2(1.0, 1.0))")
            _HAS_MATH_FUNCS = True
        except Exception:
            _HAS_MATH_FUNCS = False
            log.warning("SQLite lacks math functions; bucketed wind direction "
                        "falls back to a (circularly incorrect) arithmetic mean")

    # Load app-minted share tokens into the auth cache. Here rather than in
    # the lifespan hook so every entry point that prepares the DB (app boot,
    # tests, maintenance scripts) gets a coherent auth view.
    await refresh_guest_token_cache()
    await refresh_ingest_token_cache()
    await refresh_ingest_assignment_cache()


# In-process mirror of the guest_tokens table for the auth gate:
# require_token is a sync dependency and cannot await a query per request.
# Refreshed at startup (init_db) and after every mint/revoke — this is a
# single-process app, so those are the only writers. Empty until loaded,
# which fails closed (an unknown token is rejected, never accepted).
_GUEST_TOKEN_CACHE: set[str] = set()

# The first 12 chars ("zwg_" + 8 hex) identify a token in list/revoke
# responses without shipping the whole credential back and forth.
GUEST_TOKEN_ID_LEN = 12


def guest_token_cache() -> set[str]:
    return _GUEST_TOKEN_CACHE


async def refresh_guest_token_cache() -> None:
    global _GUEST_TOKEN_CACHE
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT token FROM guest_tokens")).fetchall()
    _GUEST_TOKEN_CACHE = {r["token"] for r in rows}


async def add_guest_token(token: str, label: str | None, now_ms: int) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO guest_tokens (token, label, created_ms) "
            "VALUES (?, ?, ?)", (token, label, now_ms))
        await db.commit()
    await refresh_guest_token_cache()


async def list_guest_tokens() -> list[dict[str, Any]]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT token, label, created_ms, last_used_ms FROM guest_tokens "
            "ORDER BY created_ms")).fetchall()
    return [dict(r) for r in rows]


# In-memory last-used stamps, keyed by full token. The auth dependency is a
# sync function on the request hot path, so it only writes this dict (a GIL
# dict store — no lock, and a lost race just re-stamps the same second);
# flush_guest_last_used() persists from an async context. The tradeoff is
# documented on the endpoint: stamps not yet flushed die with the process,
# so last_used is best-effort, refreshed whenever the owner looks at it.
_GUEST_LAST_USED: dict[str, int] = {}


def touch_guest_token(token: str) -> None:
    """Record that a guest token just authenticated. Sync + memory-only on
    purpose — callable from the sync auth dependency. The caller has already
    digest-verified membership in guest_token_cache()."""
    _GUEST_LAST_USED[token] = int(time.time() * 1000)


async def flush_guest_last_used() -> None:
    """Persist pending stamps. Take the dict's items atomically first — the
    auth dep may stamp mid-flush, and those newer stamps must survive for
    the next flush rather than being cleared unwritten."""
    if not _GUEST_LAST_USED:
        return
    pending = list(_GUEST_LAST_USED.items())
    async with connect() as db:
        for token, ms in pending:
            await db.execute(
                "UPDATE guest_tokens SET last_used_ms = ? "
                "WHERE token = ? AND (last_used_ms IS NULL OR last_used_ms < ?)",
                (ms, token, ms))
        await db.commit()
    for token, ms in pending:
        # Drop only stamps we actually wrote; a concurrent newer stamp for
        # the same token stays queued.
        if _GUEST_LAST_USED.get(token) == ms:
            _GUEST_LAST_USED.pop(token, None)


async def get_guest_token(token_id: str) -> dict[str, Any] | None:
    """Full row (token value included) by short id — the re-share path."""
    async with connect() as db:
        row = await (await db.execute(
            f"SELECT token, label, created_ms, last_used_ms FROM guest_tokens "
            f"WHERE substr(token, 1, {GUEST_TOKEN_ID_LEN}) = ?",
            (token_id,))).fetchone()
    return dict(row) if row else None


async def rename_guest_token(token_id: str, label: str | None) -> int:
    async with connect() as db:
        cur = await db.execute(
            f"UPDATE guest_tokens SET label = ? "
            f"WHERE substr(token, 1, {GUEST_TOKEN_ID_LEN}) = ?",
            (label, token_id))
        await db.commit()
        return cur.rowcount


async def delete_guest_token(token_id: str) -> int:
    async with connect() as db:
        cur = await db.execute(
            f"DELETE FROM guest_tokens WHERE substr(token, 1, {GUEST_TOKEN_ID_LEN}) = ?",
            (token_id,))
        await db.commit()
        n = cur.rowcount
    await refresh_guest_token_cache()
    return n


# ── App-minted per-device ingest tokens ──────────────────────────────────
# Structurally a carbon copy of the guest-token block above, for the same
# reasons: the ingest auth check is sync and per-request (every ~8s per
# LilyGO board), so validity lives in an in-process cache and last-used
# stamps ride memory until an async context flushes them.

_INGEST_TOKEN_CACHE: set[str] = set()

# "zwi_" + 8 hex — same shape contract as GUEST_TOKEN_ID_LEN.
INGEST_TOKEN_ID_LEN = 12


def ingest_token_cache() -> set[str]:
    return _INGEST_TOKEN_CACHE


async def refresh_ingest_token_cache() -> None:
    global _INGEST_TOKEN_CACHE
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT token FROM ingest_tokens")).fetchall()
    _INGEST_TOKEN_CACHE = {r["token"] for r in rows}


async def add_ingest_token(token: str, label: str | None, now_ms: int) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO ingest_tokens (token, label, created_ms) "
            "VALUES (?, ?, ?)", (token, label, now_ms))
        await db.commit()
    await refresh_ingest_token_cache()


async def list_ingest_tokens() -> list[dict[str, Any]]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT token, label, created_ms, last_used_ms FROM ingest_tokens "
            "ORDER BY created_ms")).fetchall()
    return [dict(r) for r in rows]


_INGEST_LAST_USED: dict[str, int] = {}


def touch_ingest_token(token: str) -> None:
    """Record that a minted ingest token just authenticated. Sync + memory-
    only on purpose — called from the request hot path after the caller has
    digest-verified membership in ingest_token_cache()."""
    _INGEST_LAST_USED[token] = int(time.time() * 1000)


async def flush_ingest_last_used() -> None:
    """Same atomic-snapshot contract as flush_guest_last_used: stamps that
    arrive mid-flush survive for the next one."""
    if not _INGEST_LAST_USED:
        return
    pending = list(_INGEST_LAST_USED.items())
    async with connect() as db:
        for token, ms in pending:
            await db.execute(
                "UPDATE ingest_tokens SET last_used_ms = ? "
                "WHERE token = ? AND (last_used_ms IS NULL OR last_used_ms < ?)",
                (ms, token, ms))
        await db.commit()
    for token, ms in pending:
        if _INGEST_LAST_USED.get(token) == ms:
            _INGEST_LAST_USED.pop(token, None)


async def get_ingest_token(token_id: str) -> dict[str, Any] | None:
    """Full row (token value included) by short id — the re-provisioning
    path: a wiped board needs the full value pasted back in."""
    async with connect() as db:
        row = await (await db.execute(
            f"SELECT token, label, created_ms, last_used_ms FROM ingest_tokens "
            f"WHERE substr(token, 1, {INGEST_TOKEN_ID_LEN}) = ?",
            (token_id,))).fetchone()
    return dict(row) if row else None


async def rename_ingest_token(token_id: str, label: str | None) -> int:
    async with connect() as db:
        cur = await db.execute(
            f"UPDATE ingest_tokens SET label = ? "
            f"WHERE substr(token, 1, {INGEST_TOKEN_ID_LEN}) = ?",
            (label, token_id))
        await db.commit()
        return cur.rowcount


async def delete_ingest_token(token_id: str) -> int:
    async with connect() as db:
        cur = await db.execute(
            f"DELETE FROM ingest_tokens WHERE substr(token, 1, {INGEST_TOKEN_ID_LEN}) = ?",
            (token_id,))
        await db.commit()
        n = cur.rowcount
    await refresh_ingest_token_cache()
    return n


# ── Token auto-upgrade assignments ───────────────────────────────────────
# In-memory mirror of the UNADOPTED assignments so the ingest hot path can
# detect adoption ("this minted token is the one we handed this mac") with
# a dict lookup instead of a query per post.

_UNADOPTED_ASSIGNMENTS: dict[str, str] = {}


def unadopted_assignment_token(mac: str) -> str | None:
    return _UNADOPTED_ASSIGNMENTS.get(mac)


async def refresh_ingest_assignment_cache() -> None:
    global _UNADOPTED_ASSIGNMENTS
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, token FROM ingest_token_assignments "
            "WHERE adopted_ms IS NULL")).fetchall()
    _UNADOPTED_ASSIGNMENTS = {r["mac"]: r["token"] for r in rows}


async def get_device_name(mac: str) -> str | None:
    """The station's display name, for labeling its auto-minted token."""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT name FROM devices WHERE mac = ?", (mac,))).fetchone()
    return row["name"] if row and row["name"] else None


async def count_ingest_assignments() -> int:
    async with connect() as db:
        row = await (await db.execute(
            "SELECT COUNT(*) AS n FROM ingest_token_assignments")).fetchone()
    return int(row["n"])


async def get_ingest_assignment(mac: str) -> dict[str, Any] | None:
    async with connect() as db:
        row = await (await db.execute(
            "SELECT mac, token, created_ms, adopted_ms "
            "FROM ingest_token_assignments WHERE mac = ?", (mac,))).fetchone()
    return dict(row) if row else None


async def set_ingest_assignment(mac: str, token: str, now_ms: int) -> None:
    """Record (or replace — the re-mint path after a revoked assignment) the
    token handed to this mac. Resets adoption: a fresh assignment hasn't
    been seen in use yet."""
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO ingest_token_assignments (mac, token, created_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                token = excluded.token, created_ms = excluded.created_ms,
                adopted_ms = NULL
            """,
            (mac, token, now_ms))
        await db.commit()
    await refresh_ingest_assignment_cache()


async def mark_ingest_assignment_adopted(mac: str, now_ms: int) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE ingest_token_assignments SET adopted_ms = ? "
            "WHERE mac = ? AND adopted_ms IS NULL", (now_ms, mac))
        await db.commit()
    await refresh_ingest_assignment_cache()


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    # daemon BEFORE the await that starts the thread: aiosqlite's
    # Connection IS a Thread, and a task abandoned on a dying event loop
    # (fire-and-forget webhook dispatch in a test's TestClient, a cancelled
    # background job) never runs this context manager's cleanup — the
    # orphaned non-daemon thread then blocks interpreter exit forever
    # (the 2026-08-26 suite exit-hang). Normal paths still close cleanly;
    # daemonizing only changes what an ABANDONED connection can hold up.
    conn = aiosqlite.connect(settings.database_path)
    conn.daemon = True
    async with conn as db:
        db.row_factory = aiosqlite.Row
        # Writers WAIT for the lock instead of failing instantly. Zero
        # timeout survived while every write was one fast statement; the
        # insights transactions (BEGIN IMMEDIATE spans check+insert+rollup)
        # hold the write lock long enough that a second writer used to get
        # an immediate "database is locked" 500 (seen in production the
        # night INSIGHTS=1 first went live).
        await db.execute("PRAGMA busy_timeout = 10000")
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
    # The UPDATE arm below is monotonic on purpose. A backfilled or
    # out-of-order reading (SDR replay, a source posting history in a batch)
    # used to drag devices.last_seen_ms *backwards* and rewrite lastData with
    # older values — the device list then showed a stale "current" reading and
    # the regressed last_seen_ms could fire a false device-down alert until the
    # next fresh post. Observations are already protected by their (mac,
    # dateutc_ms) primary key; the device row wasn't. `>=` not `>` so a
    # same-timestamp repost from a second source still merges. A NULL incoming
    # last_seen (a caller with no lastData) can't be ordered, so it refreshes
    # the row but never regresses the timestamp — hence the MAX/COALESCE pair.
    # `name` sits behind the same guard: an out-of-order post carrying an
    # explicit device.name used to rename the device even while its older
    # location/info were correctly rejected.
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO devices (mac, name, location, info_json, last_seen_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                name = CASE
                    WHEN excluded.last_seen_ms IS NULL
                      OR devices.last_seen_ms IS NULL
                      OR excluded.last_seen_ms >= devices.last_seen_ms
                    THEN COALESCE(?, devices.name, excluded.name)
                    ELSE devices.name END,
                location = CASE
                    WHEN excluded.last_seen_ms IS NULL
                      OR devices.last_seen_ms IS NULL
                      OR excluded.last_seen_ms >= devices.last_seen_ms
                    THEN excluded.location ELSE devices.location END,
                info_json = CASE
                    WHEN excluded.last_seen_ms IS NULL
                      OR devices.last_seen_ms IS NULL
                      OR excluded.last_seen_ms >= devices.last_seen_ms
                    THEN excluded.info_json ELSE devices.info_json END,
                last_seen_ms = MAX(
                    COALESCE(devices.last_seen_ms, excluded.last_seen_ms),
                    COALESCE(excluded.last_seen_ms, devices.last_seen_ms))
            """,
            (mac, insert_name, location, json.dumps(info), last_seen_ms,
             explicit_name),
        )
        await db.commit()


def _scrub_nonfinite(v: Any) -> Any:
    """Recursively replace NaN/±inf floats with None. Single choke point for
    every writer (AWN poller, WeatherLink poller, /ingest): ingest scrubs its
    own numeric blocks, but the pollers insert upstream JSON verbatim, and a
    non-finite float survives into data_json as an invalid-JSON literal
    (json.dumps defaults to allow_nan=True) that later 500s every response
    containing the row (JSONResponse serializes with allow_nan=False)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _scrub_nonfinite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_nonfinite(x) for x in v]
    return v


async def insert_observations(mac: str, rows: list[dict[str, Any]]) -> int:
    """Insert observations, ignoring duplicates by (mac, dateutc). Returns rows added."""
    if not rows:
        return 0
    payload = []
    scrubbed_by_ts: dict[Any, dict[str, Any]] = {}
    for r in rows:
        # Scrub BEFORE reading dateutc: a poller row with dateutc=NaN would
        # otherwise bind a non-finite timestamp into SQLite; scrubbed first it
        # becomes None and the row is skipped like any other timestamp-less row.
        r = _scrub_nonfinite(r)
        ts = r.get("dateutc")
        if ts is None:
            continue
        # In-batch duplicate timestamps: SQLite keeps the FIRST under
        # INSERT OR IGNORE; mirror that here so the rollups below fold each
        # stored row exactly once (CodeRabbit: double-counted sums).
        if ts in scrubbed_by_ts:
            continue
        scrubbed_by_ts[ts] = r
        values = [r.get(_FIELD_MAP[c]) for c in _COLUMNS]
        payload.append((mac, ts, json.dumps(r), *values))
    async with connect() as db:
        # Which timestamps are actually NEW? INSERT OR IGNORE silently skips
        # duplicates, and the insights rollups must not double-count a
        # re-delivered row's values — so diff against what exists first.
        # (Chunked IN() well under SQLite's default 999-variable limit.)
        new_ts: set[int] | None = None
        from .insights import update_rollups
        from .config import settings as _settings
        if _settings.insights:
            # Take the WRITE lock before the existence check: two concurrent
            # deliveries of the same timestamp could otherwise both classify
            # it as new — the loser's INSERT is ignored but its rollup fold
            # would double-count (CodeRabbit). BEGIN IMMEDIATE serializes
            # check+insert+rollup; the commit below (or connection teardown
            # on error) releases it.
            await db.execute("BEGIN IMMEDIATE")
            new_ts = set()
            ts_list = [p[1] for p in payload]
            for i in range(0, len(ts_list), 500):
                chunk = ts_list[i:i + 500]
                cur = await db.execute(
                    f"SELECT dateutc_ms FROM observations WHERE mac = ? "
                    f"AND dateutc_ms IN ({','.join('?' * len(chunk))})",
                    [mac, *chunk])
                existing = {r[0] for r in await cur.fetchall()}
                new_ts |= set(chunk) - existing
        cur = await db.executemany(
            f"""
            INSERT OR IGNORE INTO observations
              (mac, dateutc_ms, data_json, {", ".join(_COLUMNS)})
            VALUES (?, ?, ?, {", ".join("?" for _ in _COLUMNS)})
            """,
            payload,
        )
        if new_ts is not None:
            # Scrubbed + batch-deduped rows only — exactly what SQLite stored.
            fresh = [scrubbed_by_ts[ts] for ts in new_ts if ts in scrubbed_by_ts]
            await update_rollups(db, mac, fresh)
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


async def get_kv(key: str) -> str | None:
    async with connect() as db:
        row = await (await db.execute(
            "SELECT v FROM server_kv WHERE k = ?", (key,))).fetchone()
    return row[0] if row else None


async def set_kv(key: str, value: str | None) -> None:
    """Set (or with None, clear back to env fallback) a server config value."""
    async with connect() as db:
        if value is None:
            await db.execute("DELETE FROM server_kv WHERE k = ?", (key,))
        else:
            await db.execute(
                "INSERT INTO server_kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))
        await db.commit()


# "Leave this column unchanged" sentinel for set_wu_station's partial
# updates — None already means "clear", so it can't double as "unchanged".
_WU_UNSET: Any = object()


async def get_wu_station(mac: str) -> dict[str, Any] | None:
    """The device's WU association, or None if there is none. upload_key is
    the raw stored station key — INTERNAL callers only (the uploader); API
    responses must surface upload_key_set, never the key itself."""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT wu_station_id, upload_key, upload_enabled "
            "FROM wu_station_map WHERE mac = ?", (mac,)
        )).fetchone()
    if row is None:
        return None
    return {"station_id": row["wu_station_id"],
            "upload_key": row["upload_key"],
            "upload_enabled": bool(row["upload_enabled"])}


async def list_wu_stations() -> list[dict[str, Any]]:
    """Every WU association (same shape as get_wu_station, plus mac). Feeds
    the /api/sources wu_upload health block; upload_key stays internal."""
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, wu_station_id, upload_key, upload_enabled "
            "FROM wu_station_map")).fetchall()
    return [{"mac": r["mac"], "station_id": r["wu_station_id"],
             "upload_key": r["upload_key"],
             "upload_enabled": bool(r["upload_enabled"])} for r in rows]


async def set_wu_station(mac: str, station_id: Any = _WU_UNSET,
                         now_ms: int = 0, *,
                         upload_key: Any = _WU_UNSET,
                         upload_enabled: Any = _WU_UNSET) -> None:
    """Partial-update the WU association. Each argument: _WU_UNSET = leave
    unchanged, None = clear, value = set. Clearing the station ID deletes the
    whole row — the upload key and enabled flag are meaningless without a
    station, and a leftover key for a re-associated station would silently
    upload to the wrong place."""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT wu_station_id, upload_key, upload_enabled "
            "FROM wu_station_map WHERE mac = ?", (mac,))).fetchone()
        new_sid = row["wu_station_id"] if row else None
        new_key = row["upload_key"] if row else None
        new_en = bool(row["upload_enabled"]) if row else False
        if station_id is not _WU_UNSET:
            new_sid = station_id
        if upload_key is not _WU_UNSET:
            new_key = upload_key
        if upload_enabled is not _WU_UNSET:
            new_en = bool(upload_enabled)
        if new_sid is None:
            await db.execute("DELETE FROM wu_station_map WHERE mac = ?", (mac,))
        else:
            await db.execute(
                """INSERT INTO wu_station_map
                     (mac, wu_station_id, updated_ms, upload_key, upload_enabled)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET
                     wu_station_id  = excluded.wu_station_id,
                     updated_ms     = excluded.updated_ms,
                     upload_key     = excluded.upload_key,
                     upload_enabled = excluded.upload_enabled""",
                (mac, new_sid, now_ms, new_key, 1 if new_en else 0))
        await db.commit()


async def device_locations() -> dict[str, dict[str, Any]]:
    """All operator-set locations, keyed by MAC."""
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, lat, lon, label FROM device_location"
        )).fetchall()
    return {r["mac"]: {"lat": r["lat"], "lon": r["lon"], "label": r["label"]}
            for r in rows}


def is_air_monitor(mac: str) -> bool:
    """True for air-quality monitors (AirGradient synth-MAC family
    5D:5D:07). They are real DEVICES (stored, alertable for device-down,
    chartable) but not weather STATIONS — every single-subject picker
    (share uploads, heat-day, nowcast, forecast location, NWS polling,
    weather smart alerts) must skip them or the monitor fights the real
    station for the job (Volney, 2026-08-26)."""
    return str(mac or "").upper().replace(":", "").startswith("5D5D07")


def is_air_monitor_device(device: dict[str, Any]) -> bool:
    """Device-aware classifier — THE predicate the backend pickers use,
    matching the iOS app's (R8 S3: three surfaces had three predicates).
    Prefix first; else the heuristic: air metrics present with no wind,
    no rain, AND no pressure. The pressure guard keeps a real station
    whose anemometer array died from flipping to "monitor" off one
    composite snapshot (R8 S4) — weather stations carry a barometer."""
    if is_air_monitor(device.get("mac", "")):
        return True
    last = device.get("lastData") or {}
    if not isinstance(last, dict):
        return False
    return ((last.get("pm25") is not None or last.get("co2") is not None)
            and last.get("windspeedmph") is None
            and last.get("dailyrainin") is None
            and last.get("hourlyrainin") is None
            and last.get("baromrelin") is None)


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
        info = _parse_json_col(r["info_json"])
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
        # device_location + smart_alert_state are keyed by MAC too. Leaving
        # them meant a re-registered MAC silently inherited the old operator-
        # set location, and a stale smart_alert_state.triggered=1 suppressed
        # the "new" device's first frost/heat alert (the edge-trigger never
        # sees a clear→triggered transition).
        n_loc   = await _del("DELETE FROM device_location     WHERE mac = ?")
        # R6: storm_state and pending_devices are mac-keyed too — a
        # re-registered MAC inherited an OPEN STORM tracker (delete→recreate
        # within the 6h baseline window judged the new device's counters
        # against the old baseline and could fabricate a storm) and skipped
        # probation. Same inheritance class as every row above. The token
        # auto-upgrade assignment goes with them: a fresh device must not
        # inherit its predecessor's credential.
        n_storm = await _del("DELETE FROM storm_state          WHERE mac = ?")
        n_pend  = await _del("DELETE FROM pending_devices      WHERE mac = ?")
        n_asgn  = await _del("DELETE FROM ingest_token_assignments WHERE mac = ?")
        n_smart = await _del("DELETE FROM smart_alert_state   WHERE mac = ?")
        # 1.4's MAC-keyed tables — same inheritance bug class: a leftover
        # wu_station_map row makes a re-registered MAC default to the OLD
        # station's WU association (an import then pulls the wrong archive),
        # and surviving rollup rows keep serving /api/insights for a deleted
        # device, then UPSERT-fold the new device's readings onto the stale
        # additive sums. init_db always creates these tables.
        n_wu    = await _del("DELETE FROM wu_station_map      WHERE mac = ?")
        n_daily = await _del("DELETE FROM daily_rollups       WHERE mac = ?")
        n_hour  = await _del("DELETE FROM hour_rollups        WHERE mac = ?")
        await db.commit()
    return {"devices": n_devs, "observations": n_obs,
            "alert_prefs": n_pref, "alert_state": n_state,
            "rule_state": n_rule, "location": n_loc,
            "smart_alert_state": n_smart, "wu_station": n_wu,
            "daily_rollups": n_daily, "hour_rollups": n_hour}


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


# History cap: ~a month of normal alerting. Pruned by id (insert order), not
# ts_ms, so a device with a wrong clock can't evict everyone else's history.
_ALERT_LOG_KEEP = 200


async def log_alert(ts_ms: int, kind: str, mac: str | None, title: str,
                    body: str | None, delivered: bool,
                    severity: str | None = None) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO alert_log (ts_ms, kind, mac, title, body, delivered, "
            "severity) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts_ms, kind, mac, title, body, 1 if delivered else 0, severity),
        )
        await db.execute(
            "DELETE FROM alert_log WHERE id NOT IN "
            "(SELECT id FROM alert_log ORDER BY id DESC LIMIT ?)",
            (_ALERT_LOG_KEEP,),
        )
        await db.commit()


async def recent_alerts(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first alert history for GET /api/alerts/recent."""
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT id, ts_ms, kind, mac, title, body, delivered, severity "
            "FROM alert_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )).fetchall()
    return [dict(r) for r in rows]


_ALERT_PREF_COLS = ("enabled", "default_threshold_min", "repeat_hours", "recipients",
                    "smtp_host", "smtp_port", "smtp_username", "smtp_password",
                    "smtp_from", "smtp_tls", "smtp_ssl", "email_scope",
                    "storm_summary", "storm_quiet_minutes", "storm_min_total_in",
                    "rain_start", "storm_channels",
                    "heat_day", "heat_day_threshold_f",
                    "quiet_start_min", "quiet_end_min", "digest_hour")


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
            "SELECT mac, monitor, threshold_min, storm_summary "
            "FROM device_alert_prefs"
        )).fetchall()
    return {
        r["mac"]: {
            "monitor": bool(r["monitor"]),
            "threshold_min": r["threshold_min"],
            # None = never set = summaries on. Kept tri-state so the UI can
            # tell an explicit choice from the default.
            "storm_summary": (None if r["storm_summary"] is None
                              else bool(r["storm_summary"])),
        }
        for r in rows
    }


async def set_device_storm_summary(mac: str, on: bool) -> None:
    """Per-station storm-summary switch (1.7). Deliberately its own writer:
    the monitor/threshold upsert must not clobber this column and vice
    versa — the two settings are edited from different screens."""
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO device_alert_prefs (mac, storm_summary)
            VALUES (?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                storm_summary = excluded.storm_summary
            """,
            (mac, 1 if on else 0),
        )
        await db.commit()


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
                            comparator: str, threshold: float,
                            severity: str = "minor") -> dict[str, Any]:
    now = int(__import__("time").time() * 1000)
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO alert_rules (target_mac, field, comparator, threshold, "
            "enabled, created_ms, severity) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (target_mac, field, comparator, threshold, now, severity),
        )
        await db.commit()
        rid = cur.lastrowid
    return {"id": rid, "target_mac": target_mac, "field": field,
            "comparator": comparator, "threshold": threshold, "enabled": True,
            "severity": severity}


async def list_alert_rules(enabled_only: bool = False) -> list[dict[str, Any]]:
    """Rules, each carrying its current firing state.

    `triggered`/`changed_ms` come from alert_rule_state and exist so a CLIENT
    can edge-detect a firing the same way the server does. Without them the
    macOS app had no way to learn a threshold rule had tripped — it only ever
    saw device stale/recovered — so a Mac showed nothing while the same
    account's iPhone got the push (Doren, 2026-08-17).

    A rule with no state row has never been evaluated: reported as triggered
    = False rather than omitted, so the client sees a complete list.
    """
    sql = ("SELECT r.id, r.target_mac, r.field, r.comparator, r.threshold, "
           "r.enabled, r.severity, MAX(COALESCE(s.triggered, 0)) AS triggered, "
           "MAX(COALESCE(s.changed_ms, 0)) AS changed_ms "
           "FROM alert_rules r LEFT JOIN alert_rule_state s ON s.rule_id = r.id")
    if enabled_only:
        sql += " WHERE r.enabled = 1"
    # Grouped because a rule with target_mac NULL applies to EVERY device and
    # therefore has one state row per MAC. MAX() reports it as firing when it
    # is firing anywhere, which is what a notification cares about.
    sql += " GROUP BY r.id ORDER BY r.id"
    async with connect() as db:
        rows = await (await db.execute(sql)).fetchall()
    return [{"id": r["id"], "target_mac": r["target_mac"], "field": r["field"],
             "comparator": r["comparator"], "threshold": r["threshold"],
             "enabled": bool(r["enabled"]),
             # NULL = minor: the default Volney chose for existing rules.
             "severity": r["severity"] or "minor",
             "triggered": bool(r["triggered"]),
             "changed_ms": r["changed_ms"] or None} for r in rows]


async def delete_alert_rule(rule_id: int) -> int:
    async with connect() as db:
        cur = await db.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
        await db.execute("DELETE FROM alert_rule_state WHERE rule_id = ?", (rule_id,))
        await db.commit()
        return cur.rowcount or 0


async def update_alert_rule(rule_id: int, *, enabled: bool | None = None,
                            threshold: float | None = None,
                            target_mac: str | None = None,
                            set_target: bool = False,
                            severity: str | None = None) -> dict[str, Any] | None:
    """Partial rule edit (1.7 — before this, changing a rule's threshold or
    station meant delete-and-recreate, and retargeting Doren's 28 rules took
    raw sqlite on his box). `set_target` distinguishes "leave the scope
    alone" from "set it to target_mac" (None there = all devices).

    A threshold or scope change clears the rule's trigger state on purpose:
    a rule stuck triggered=1 under its OLD threshold would swallow the
    first crossing of the new one, and a retargeted rule's per-mac states
    describe stations it no longer watches."""
    sets: list[str] = []
    args: list[Any] = []
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(1 if enabled else 0)
    if threshold is not None:
        sets.append("threshold = ?")
        args.append(float(threshold))
    if set_target:
        sets.append("target_mac = ?")
        args.append(target_mac)
    if severity is not None:
        sets.append("severity = ?")
        args.append(severity)
    async with connect() as db:
        if sets:
            cur = await db.execute(
                f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = ?",
                (*args, rule_id))
            if not cur.rowcount:
                return None
            if threshold is not None or set_target:
                await db.execute(
                    "DELETE FROM alert_rule_state WHERE rule_id = ?",
                    (rule_id,))
            await db.commit()
        r = await (await db.execute(
            "SELECT id, target_mac, field, comparator, threshold, enabled, "
            "severity FROM alert_rules WHERE id = ?", (rule_id,))).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "target_mac": r["target_mac"], "field": r["field"],
            "comparator": r["comparator"], "threshold": r["threshold"],
            "enabled": bool(r["enabled"]),
            "severity": r["severity"] or "minor"}


async def set_alert_rule_enabled(rule_id: int, enabled: bool) -> dict[str, Any] | None:
    """Toggle a rule on/off. Returns the updated rule, or None if it doesn't exist."""
    async with connect() as db:
        cur = await db.execute("UPDATE alert_rules SET enabled = ? WHERE id = ?",
                               (1 if enabled else 0, rule_id))
        await db.commit()
        if not cur.rowcount:
            return None
        r = await (await db.execute(
            "SELECT id, target_mac, field, comparator, threshold, enabled, "
            "severity FROM alert_rules WHERE id = ?", (rule_id,))).fetchone()
    return {"id": r["id"], "target_mac": r["target_mac"], "field": r["field"],
            "comparator": r["comparator"], "threshold": r["threshold"],
            "enabled": bool(r["enabled"]),
            "severity": r["severity"] or "minor"}


async def get_rule_states() -> dict[tuple[int, str], int]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT rule_id, mac, triggered FROM alert_rule_state")).fetchall()
    return {(r["rule_id"], r["mac"]): r["triggered"] for r in rows}


async def get_rule_states_full() -> dict[tuple[int, str], tuple[int, int | None]]:
    """(triggered, clear_since_ms) per (rule, device) — the monitor's view.
    Separate from get_rule_states so the API surface (and every test built
    on it) keeps its plain triggered map."""
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT rule_id, mac, triggered, clear_since_ms "
            "FROM alert_rule_state")).fetchall()
    return {(r["rule_id"], r["mac"]): (r["triggered"], r["clear_since_ms"])
            for r in rows}


async def upsert_rule_state(rule_id: int, mac: str, triggered: int, changed_ms: int) -> None:
    """Record a real transition (fire or re-arm). Resets the dwell clock —
    both transitions invalidate any clearance being timed."""
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO alert_rule_state (rule_id, mac, triggered, changed_ms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(rule_id, mac) DO UPDATE SET
                triggered = excluded.triggered, changed_ms = excluded.changed_ms,
                clear_since_ms = NULL
            """,
            (rule_id, mac, triggered, changed_ms),
        )
        await db.commit()


async def set_rule_clear_since(rule_id: int, mac: str,
                               clear_since_ms: int | None) -> None:
    """Dwell-clock bookkeeping only. Deliberately does NOT touch changed_ms:
    the client renders "triggered since" from it, and a clock tick is not a
    state change."""
    async with connect() as db:
        await db.execute(
            "UPDATE alert_rule_state SET clear_since_ms = ? "
            "WHERE rule_id = ? AND mac = ?",
            (clear_since_ms, rule_id, mac),
        )
        await db.commit()


async def get_smart_alert_states() -> dict[tuple[str, str], int]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, kind, triggered FROM smart_alert_state")).fetchall()
    return {(r["mac"], r["kind"]): r["triggered"] for r in rows}


async def upsert_smart_alert_state(mac: str, kind: str, triggered: int,
                                   changed_ms: int) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO smart_alert_state (mac, kind, triggered, changed_ms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(mac, kind) DO UPDATE SET
                triggered = excluded.triggered, changed_ms = excluded.changed_ms
            """,
            (mac, kind, triggered, changed_ms),
        )
        await db.commit()


async def get_storm_state(mac: str) -> dict[str, Any] | None:
    async with connect() as db:
        row = await (await db.execute(
            "SELECT * FROM storm_state WHERE mac = ?", (mac,))).fetchone()
    return dict(row) if row else None


async def upsert_storm_state(mac: str, started_ms: int | None,
                             last_rain_ms: int | None,
                             counter_field: str | None,
                             counter_value: float | None,
                             counter_ms: int | None = None) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO storm_state (mac, started_ms, last_rain_ms, "
            "counter_field, counter_value, counter_ms) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(mac) DO UPDATE SET started_ms = excluded.started_ms, "
            "last_rain_ms = excluded.last_rain_ms, "
            "counter_field = excluded.counter_field, "
            "counter_value = excluded.counter_value, "
            "counter_ms = excluded.counter_ms",
            (mac, started_ms, last_rain_ms, counter_field, counter_value,
             counter_ms))
        await db.commit()


# The storm-summary reads used a local twin of _tolerant_float; keep the
# old name alive for its call sites, pointing at the one sanitizer.
_as_float = _tolerant_float


async def list_device_macs() -> list[str]:
    """Just the MACs. The probation check runs on every ingest, so it reads
    this rather than the full device rows with their JSON blobs."""
    async with connect() as db:
        rows = await (await db.execute("SELECT mac FROM devices")).fetchall()
        return [r[0] for r in rows]


async def get_pending_device(mac: str) -> dict[str, Any] | None:
    async with connect() as db:
        row = await (await db.execute(
            "SELECT mac, first_ms, last_ms, hits, suspect_of "
            "FROM pending_devices WHERE mac = ?", (mac,))).fetchone()
        return dict(row) if row else None


async def bump_pending_device(mac: str, now_ms: int, hits: int,
                              suspect_of: str | None,
                              advanced: bool, ttl_ms: int = 0) -> None:
    """Record a sighting. `advanced` false means the counter did not move, so
    `last_ms` must NOT be touched — it anchors the spacing rule.

    Prunes expired rows in the same transaction. This is the only path that
    ever writes the table, so pruning here is enough to bound it: a noisy
    neighbourhood would otherwise leave a row per corrupt packet forever.
    """
    async with connect() as db:
        if ttl_ms > 0:
            await db.execute("DELETE FROM pending_devices WHERE last_ms < ?",
                             (now_ms - ttl_ms,))
        if advanced:
            await db.execute(
                "INSERT INTO pending_devices (mac, first_ms, last_ms, hits, "
                "suspect_of) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(mac) DO UPDATE SET last_ms = excluded.last_ms, "
                "hits = excluded.hits, suspect_of = excluded.suspect_of",
                (mac, now_ms, now_ms, hits, suspect_of))
        else:
            await db.execute(
                "INSERT INTO pending_devices (mac, first_ms, last_ms, hits, "
                "suspect_of) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(mac) DO NOTHING",
                (mac, now_ms, now_ms, hits, suspect_of))
        await db.commit()


async def clear_pending_device(mac: str) -> None:
    async with connect() as db:
        await db.execute("DELETE FROM pending_devices WHERE mac = ?", (mac,))
        await db.commit()


async def prune_pending_devices(before_ms: int) -> int:
    """Drop probation rows that stopped being seen. Without this a noisy
    neighbourhood accumulates a row per corrupt packet forever."""
    async with connect() as db:
        cur = await db.execute(
            "DELETE FROM pending_devices WHERE last_ms < ?", (before_ms,))
        await db.commit()
        return cur.rowcount or 0


async def list_pending_devices() -> list[dict[str, Any]]:
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT mac, first_ms, last_ms, hits, suspect_of "
            "FROM pending_devices ORDER BY last_ms DESC")).fetchall()
        return [dict(r) for r in rows]


from . import storm as _storm  # noqa: E402  (cycle-safe: storm imports no db)


async def max_windgust_in_window(mac: str, start_ms: int, end_ms: int) -> float | None:
    """Max windgustmph in [start, end] — the storm summary's gust-front lead
    (alerts._check_storm_summaries): the day's headline gust often arrives
    minutes BEFORE the first bucket tip opens the storm window. Fixed column
    on purpose — nothing to interpolate, nothing to whitelist."""
    async with connect() as db:
        row = await (await db.execute(
            "SELECT MAX(CASE WHEN typeof(windgustmph) IN ('integer','real') "
            "            THEN windgustmph END) "
            "FROM observations WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?",
            (mac, start_ms, end_ms))).fetchone()
    return row[0]


async def storm_window_stats(mac: str, start_ms: int, end_ms: int,
                             counter_col: str) -> dict[str, Any]:
    """Everything the storm summary reports, computed from stored history.

    The rain total is a sum of POSITIVE deltas rather than end-minus-start:
    a counter that resets mid-window (yearly at New Year, daily at midnight)
    would otherwise produce a negative or wildly wrong total. Summing the
    increments is correct either way.
    """
    if counter_col not in _FIELD_MAP:
        raise ValueError(f"refusing to interpolate unknown column {counter_col!r}")
    async with connect() as db:
        # Every aggregate is filtered by typeof(). SQLite does not enforce a
        # column's declared type, and its ordering puts TEXT above every
        # number — so one garbled reading stored as 'bad' becomes MAX(tempf),
        # and dropping it in Python afterwards loses the real maximum with
        # it. Excluding non-numeric rows inside the aggregate keeps the rest.
        row = await (await db.execute(
            "SELECT MIN(CASE WHEN typeof(tempf) IN ('integer','real') "
            "            THEN tempf END) AS min_t, "
            "       MAX(CASE WHEN typeof(tempf) IN ('integer','real') "
            "            THEN tempf END) AS max_t, "
            "       MAX(CASE WHEN typeof(windgustmph) IN ('integer','real') "
            "            THEN windgustmph END) AS max_gust, "
            "       MAX(CASE WHEN typeof(hourlyrainin) IN ('integer','real') "
            "            THEN hourlyrainin END) AS peak_rate "
            "FROM observations WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?",
            (mac, start_ms, end_ms))).fetchone()
        cur = await db.execute(
            f"SELECT {counter_col} FROM observations WHERE mac = ? "
            f"AND dateutc_ms BETWEEN ? AND ? AND {counter_col} IS NOT NULL "
            f"ORDER BY dateutc_ms", (mac, start_ms, end_ms))
        total = 0.0
        # High-water mark, not the previous reading: a source that revises its
        # daily total downward and then climbs back would otherwise have the
        # re-climb counted as fresh rain. See storm.counter_progress for the
        # WeatherFlow sequence that exposed this.
        peak: float | None = None
        async for r in cur:
            # `insert_observations` stores poller values uncoerced and SQLite
            # happily keeps text in a REAL column, so a garbled reading can
            # arrive here as a string. `float()` would raise and abort the
            # whole alert-monitor tick — one bad row would stop every alert
            # for every device. Skip it the way _derive_hourly_rain does, and
            # leave `prev` alone so the next good row still measures against
            # the last good one (the 2.0 cap covers the bridged gap).
            v = _as_float(r[0])
            if v is None:
                continue
            inc, peak = _storm.counter_progress(peak, v)
            total += inc
    # Coerced for the same reason: SQLite's MIN/MAX order text ABOVE every
    # number, so a single text row makes MAX() return a string, and
    # `build_storm_message` formats these with `:.0f`.
    return {
        "total_in": total,
        "peak_rate_in_hr": _as_float(row["peak_rate"]) if row else None,
        "min_tempf": _as_float(row["min_t"]) if row else None,
        "max_tempf": _as_float(row["max_t"]) if row else None,
        "max_gust_mph": _as_float(row["max_gust"]) if row else None,
    }


async def value_at_or_before(mac: str, field: str, cutoff_ms: int,
                             max_age_ms: int | None = None) -> float | None:
    """Latest stored value of an API field at or before a timestamp (used by
    smart alerts for pressure tendency). Resolves the API field name to its
    column, then reuses the rain helper's at-or-before lookup.

    `max_age_ms` is the freshness floor (R7 R4): a fixed-window delta needs
    its anchor NEAR the window edge — after a station outage the newest
    row before the cutoff can be days older, and the "1h" delta silently
    spans the whole gap (a false storm-outflow alert, live-proven). With a
    floor set, an anchor older than cutoff − max_age_ms returns None and
    the caller skips the delta instead of lying."""
    col = {v: k for k, v in _FIELD_MAP.items()}.get(field)
    if col is None:
        return None
    if max_age_ms is not None:
        if field not in QUERYABLE_FIELDS:
            raise ValueError(f"field {field!r} not allowed")
        async with connect() as db:
            row = await (await db.execute(
                f"SELECT {col} FROM observations "
                "WHERE mac = ? AND dateutc_ms <= ? AND dateutc_ms >= ? "
                f"AND {col} IS NOT NULL "
                "ORDER BY dateutc_ms DESC LIMIT 1",
                (mac, cutoff_ms, cutoff_ms - max_age_ms))).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None
    # Strict: no earliest-row fallback (see _rain_col_at_or_before). Smart alerts
    # use this for a fixed-window delta, where a fallback fabricates the window.
    return await _rain_col_at_or_before(mac, col, cutoff_ms,
                                        fallback_earliest=False)


async def delete_live_activity_token(token: str) -> bool:
    """Best-effort unregister for one ActivityKit token (R10 U2 — the
    app's Disconnect & reset sends every token it remembers minting, so
    the old server stops starting Live Activities on a device that
    moved on)."""
    async with connect() as db:
        cur = await db.execute(
            "DELETE FROM live_activity_tokens WHERE token = ?", (token,))
        await db.commit()
        return cur.rowcount > 0


async def delete_kv_prefix(prefix: str) -> int:
    """Delete every server_kv row whose key starts with `prefix` — the
    NWS legacy-key retirement uses this so seen-keys of since-DELETED
    devices don't linger as cruft (R10 U4). LIKE with an escaped literal
    prefix; callers pass internal constants, never user input."""
    async with connect() as db:
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cur = await db.execute(
            "DELETE FROM server_kv WHERE k LIKE ? ESCAPE '\\'",
            (esc + "%",))
        await db.commit()
        return cur.rowcount


async def delete_push_token(token: str) -> bool:
    """Best-effort unregister (1.8 — the app's Disconnect & reset calls
    this so an abandoned install stops receiving the old server's alert
    pushes). Returns whether a row existed."""
    async with connect() as db:
        cur = await db.execute("DELETE FROM push_tokens WHERE token = ?",
                               (token,))
        await db.commit()
        return cur.rowcount > 0


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


async def register_live_activity_token(token: str, kind: str,
                                       env: str | None,
                                       activity: str | None = None) -> None:
    """Push-to-start / per-activity update registration, same idempotent
    shape as push_tokens — iOS rotates these and the app re-posts whatever
    it currently holds. `activity` names which Activity the token belongs
    to (push-to-start tokens are ALSO per-attributes-type); NULL = a 1.7
    app's rain-start registration."""
    now = int(time.time() * 1000)
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO live_activity_tokens (token, kind, env, activity,
                                              created_ms, last_seen_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                kind = excluded.kind, env = excluded.env,
                activity = excluded.activity,
                last_seen_ms = excluded.last_seen_ms
            """,
            (token, kind, env, activity, now, now),
        )
        await db.commit()


async def list_live_activity_tokens(kind: str = "start",
                                    activity: str | None = None
                                    ) -> list[dict[str, Any]]:
    """Tokens for one kind, optionally scoped to one Activity. ActivityKit
    mints push-to-start tokens PER ATTRIBUTES TYPE, so a storm start must
    never fan out to a token minted for the rain Activity. Legacy rows
    (1.7 apps) registered before the discriminator existed are all
    rain-start tokens — activity IS NULL matches only 'rain'."""
    async with connect() as db:
        if activity is None:
            rows = await (await db.execute(
                "SELECT token, env FROM live_activity_tokens WHERE kind = ?",
                (kind,))).fetchall()
        elif activity == "rain":
            rows = await (await db.execute(
                "SELECT token, env FROM live_activity_tokens "
                "WHERE kind = ? AND (activity = ? OR activity IS NULL)",
                (kind, activity))).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT token, env FROM live_activity_tokens "
                "WHERE kind = ? AND activity = ?",
                (kind, activity))).fetchall()
    return [{"token": r["token"], "env": r["env"]} for r in rows]


_OBS_EXPORT_COLS: list[str] | None = None


async def observation_columns() -> list[str]:
    """The observations table's real value columns (everything except the
    key pair and the raw blob), cached per process — the CSV export's
    header, derived from the schema so new columns join automatically."""
    global _OBS_EXPORT_COLS
    if _OBS_EXPORT_COLS is None:
        async with connect() as db:
            info = await (await db.execute(
                "PRAGMA table_info(observations)")).fetchall()
        # Guarded interpolation (CLAUDE.md): the names come from the
        # schema's own PRAGMA, but they still pass the identifier gate
        # before ever reaching an f-string SELECT — and it's a raise,
        # not an assert, so python -O keeps the guard.
        cols = [r[1] for r in info
                if r[1] not in ("mac", "dateutc_ms", "data_json")]
        for c in cols:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c):
                raise ValueError(f"unsafe column name from schema: {c!r}")
        _OBS_EXPORT_COLS = cols
    return _OBS_EXPORT_COLS


async def observation_rows(mac: str, start_ms: int, end_ms: int,
                           limit: int = 5000) -> list[dict[str, Any]]:
    """One export batch, ascending — the caller pages by advancing past
    the last row's timestamp."""
    cols = await observation_columns()
    async with connect() as db:
        rows = await (await db.execute(
            f"SELECT dateutc_ms, {', '.join(cols)} FROM observations "
            "WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms <= ? "
            "ORDER BY dateutc_ms LIMIT ?",
            (mac, start_ms, end_ms, limit))).fetchall()
    return [dict(r) for r in rows]


_HEALTH_FIELDS = {"humidity", "windgustmph", "windspeedmph", "tempf",
                  "solarradiation", "baromrelin", "uv"}


async def field_min_max(mac: str, field: str, start_ms: int,
                        end_ms: int) -> tuple[float, float, int] | None:
    """(min, max, earliest_ms) of a NUMERIC field over a window; None when
    the window holds no finite readings. earliest_ms exists so a flatline
    claim can require the window to actually be COVERED — a station online
    for ten minutes in fog satisfies min==max==100 from two rows
    (CodeRabbit, PR #32). Guarded interpolation (whitelist raise — never
    an assert): health_watch's flatline checks."""
    if field not in _HEALTH_FIELDS:
        raise ValueError(f"field {field!r} not allowed")
    async with connect() as db:
        row = await (await db.execute(
            f"SELECT MIN(CASE WHEN typeof({field}) IN ('integer','real') "
            f"THEN {field} END), "
            f"MAX(CASE WHEN typeof({field}) IN ('integer','real') "
            f"THEN {field} END), "
            f"MIN(CASE WHEN typeof({field}) IN ('integer','real') "
            f"THEN dateutc_ms END) "
            "FROM observations WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?",
            (mac, start_ms, end_ms))).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return float(row[0]), float(row[1]), int(row[2])


async def alerts_since(ts_ms: int) -> list[dict[str, Any]]:
    """Alert-log rows newer than ts_ms, oldest first — digest material."""
    async with connect() as db:
        rows = await (await db.execute(
            "SELECT ts_ms, kind, mac, title, body, delivered FROM alert_log "
            "WHERE ts_ms > ? ORDER BY ts_ms", (ts_ms,))).fetchall()
    return [dict(r) for r in rows]


async def set_webhook_enabled(hook_id: str, enabled: bool) -> bool:
    """Pause/resume one webhook. The column always existed and dispatch
    always filtered on it — 1.8.x adds the missing way to change it
    (R7: 'dead state the API advertises')."""
    async with connect() as db:
        cur = await db.execute(
            "UPDATE webhooks SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, hook_id))
        await db.commit()
        return cur.rowcount > 0


async def create_webhook(url: str) -> dict[str, Any]:
    import secrets as _secrets
    wid = _secrets.token_hex(8)
    secret = _secrets.token_hex(24)
    now = int(time.time() * 1000)
    async with connect() as db:
        await db.execute(
            "INSERT INTO webhooks (id, url, secret, enabled, created_ms) "
            "VALUES (?, ?, ?, 1, ?)", (wid, url, secret, now))
        await db.commit()
    return {"id": wid, "url": url, "secret": secret, "created_ms": now}


async def list_webhooks(enabled_only: bool = False) -> list[dict[str, Any]]:
    q = "SELECT * FROM webhooks"
    if enabled_only:
        q += " WHERE enabled = 1"
    async with connect() as db:
        rows = await (await db.execute(q)).fetchall()
    return [dict(r) for r in rows]


async def delete_webhook(wid: str) -> bool:
    async with connect() as db:
        cur = await db.execute("DELETE FROM webhooks WHERE id = ?", (wid,))
        await db.commit()
        return cur.rowcount > 0


async def stamp_webhook(wid: str, ok_ms: int | None, error: str | None) -> None:
    async with connect() as db:
        if ok_ms is not None:
            await db.execute(
                "UPDATE webhooks SET last_ok_ms = ?, last_error = NULL "
                "WHERE id = ?", (ok_ms, wid))
        else:
            await db.execute(
                "UPDATE webhooks SET last_error = ? WHERE id = ?",
                (error, wid))
        await db.commit()


async def insert_forecast_snapshots(provider: str, issued_ms: int,
                                    rows: list[dict], keep_days: int = 400
                                    ) -> None:
    """One forecast run's daily rows; prunes beyond keep_days on write so
    the table can never grow unbounded on a forgotten server."""
    cutoff = issued_ms - keep_days * 86_400_000
    async with connect() as db:
        await db.executemany(
            """
            INSERT INTO forecast_snapshots
                (provider, issued_ms, valid_date, lead_days,
                 tmax_f, tmin_f, pop, precip_in)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(provider, issued_ms, r["valid_date"], r["lead_days"],
              r.get("tmax_f"), r.get("tmin_f"), r.get("pop"),
              r.get("precip_in")) for r in rows])
        await db.execute(
            "DELETE FROM forecast_snapshots WHERE issued_ms < ?", (cutoff,))
        await db.commit()


async def get_storm_watch_la(mac: str) -> dict[str, Any] | None:
    async with connect() as db:
        row = await (await db.execute(
            "SELECT episode_started_ms, last_push_ms FROM storm_watch_la "
            "WHERE mac = ?", (mac,))).fetchone()
    if row is None:
        return None
    return {"episode_started_ms": row["episode_started_ms"],
            "last_push_ms": row["last_push_ms"]}


async def set_storm_watch_la(mac: str, episode_started_ms: int,
                             last_push_ms: int) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO storm_watch_la (mac, episode_started_ms, last_push_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                episode_started_ms = excluded.episode_started_ms,
                last_push_ms = excluded.last_push_ms
            """, (mac, episode_started_ms, last_push_ms))
        await db.commit()


async def clear_storm_watch_la(mac: str) -> None:
    async with connect() as db:
        await db.execute("DELETE FROM storm_watch_la WHERE mac = ?", (mac,))
        await db.commit()


async def remove_live_activity_token(token: str) -> None:
    """Prune a token APNs rejected as dead — same rule as push_tokens."""
    async with connect() as db:
        await db.execute(
            "DELETE FROM live_activity_tokens WHERE token = ?", (token,))
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


# Columns the ingest glitch guards may ask for. `last_metric_value`
# interpolates the name into SQL, so it MUST come from this fixed set —
# never caller input. A plain `if` (not assert: those vanish under
# `python -O`), same rule as every other interpolated column here.
_GLITCH_GUARD_COLUMNS = frozenset({"yearlyrainin", "dailyrainin", "tempf"})


async def last_metric_value(mac: str, column: str) -> tuple[float, int] | None:
    """Most recent NON-NULL value of one metric column + its timestamp (ms)
    for a device. Used by the ingest glitch guards as the 'before' value
    (a dropped glitch leaves NULL, so this returns the last *good* reading)."""
    if column not in _GLITCH_GUARD_COLUMNS:
        raise ValueError(f"column not allowed for glitch guard: {column!r}")
    async with connect() as db:
        row = await (await db.execute(
            f"SELECT {column} AS v, dateutc_ms FROM observations "
            "WHERE mac = ? AND " + column + " IS NOT NULL "
            "ORDER BY dateutc_ms DESC LIMIT 1", (mac,)
        )).fetchone()
    if not row or row["v"] is None:
        return None
    # TEXT-in-REAL tolerance: a junk stored value must read as "no prior",
    # not ValueError out of the ingest glitch guard (see _tolerant_float).
    val = _tolerant_float(row["v"])
    if val is None:
        return None
    return (val, int(row["dateutc_ms"]))


async def last_yearly_rain(mac: str) -> tuple[float, int] | None:
    """Most recent NON-NULL cumulative yearly-rain reading + its timestamp
    (ms) for a device — the rain glitch guard's 'before' value."""
    return await last_metric_value(mac, "yearlyrainin")


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
    out: dict[str, Any] = _parse_json_col(freshest_row["data_json"])
    if not out:
        # Corrupt freshest row — still expose its timestamp so the composite
        # fill below has an anchor and the caller sees SOMETHING, not a 500.
        out = {"dateutc": freshest_row["dateutc_ms"]}
    for r in recent_rows[1:]:
        older = _parse_json_col(r["data_json"])
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
        async with connect() as db:
            src_row = await (await db.execute(
                "SELECT data_json FROM observations WHERE mac = ? "
                "AND baromrelin IS NOT NULL ORDER BY dateutc_ms DESC LIMIT 1",
                (src_mac,),
            )).fetchone()
        if src_row:
            src = _parse_json_col(src_row["data_json"])
            for k in ("baromrelin", "baromabsin"):
                if out.get(k) is None and src.get(k) is not None:
                    out[k] = src[k]
            for k in ("tempinf", "humidityin"):
                if out.get(k) is None and src.get(k) is not None:
                    out[k] = src[k]
    _normalize_lightning(out)
    return out


def _normalize_lightning(d: dict[str, Any]) -> dict[str, Any]:
    """The app decodes strike counts as Int? (synthesized Codable), so a
    REAL-typed 731.0 or a numeric-string "7" from a custom /ingest poster
    fails the WHOLE row's decode — dashboard and short-window charts blank
    until clean rows arrive (CODE_REVIEW_R5 R5-09). The bucketed history
    path CASTs in SQL for exactly this reason; the raw paths echo
    data_json verbatim, so they normalize here. Non-numeric junk degrades
    to None (absent), never 0."""
    for k in ("lightningcount", "lightning_last_1hr"):
        if k not in d:
            continue
        v = d[k]
        if v is None or isinstance(v, bool):
            d[k] = None
        elif isinstance(v, (int, float)):
            # math.isfinite, not just int(): json.loads accepts the bare
            # Infinity/NaN literals, and int(inf) raises OverflowError —
            # a single such stored row 500'd /current and every raw
            # history window containing it (CodeRabbit, 2026-08-20).
            d[k] = int(v) if math.isfinite(v) else None
        elif isinstance(v, str):
            try:
                d[k] = int(float(v))
            except (ValueError, OverflowError):
                # OverflowError too: float("inf") parses fine and then
                # int() blows up — same CodeRabbit finding.
                d[k] = None
        else:
            d[k] = None
    return d


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
        try:
            # data_json is source-controlled: a non-numeric yearlyrainin (the
            # poller path stores upstream JSON verbatim) must skip this row,
            # not 500 every chart window containing it.
            val = round(max(0.0, float(yr) - float(ref)), 3)
        except (TypeError, ValueError):
            continue
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


# Does this SQLite build have the math functions (SIN/COS/ATAN2/RADIANS)?
# Probed once in init_db. Built in by default since SQLite 3.35, but we don't
# control every self-hoster's build, so degrade instead of 500ing.
_HAS_MATH_FUNCS: bool = False


def _winddir_expr() -> str:
    """Bucket-average wind direction.

    Direction is MODULAR: the arithmetic mean of 355° and 5° is 180° — due
    SOUTH for a steady north wind. That's what AVG(winddir) did, so every wind
    rose on a bucketed (>6h) window could point the wrong way. The circular
    (vector) mean averages the unit vectors instead, which is correct across the
    0/360 wrap. Falls back to the old AVG only where the math functions are
    missing — wrong, but no worse than before and better than an error.
    """
    if not _HAS_MATH_FUNCS:
        return "AVG(winddir)"
    # NOT `(... + 360.0) % 360.0`: SQLite's % casts its operands to INTEGER
    # (224.7 % 360.0 → 224.0), which truncated the circular mean to whole
    # degrees. ATAN2's output is in (−180, 180], so one conditional +360 is
    # a fraction-preserving mod.
    vec = ("DEGREES(ATAN2(AVG(SIN(RADIANS(winddir))), "
           "AVG(COS(RADIANS(winddir)))))")
    return (f"CASE WHEN COUNT(winddir) = 0 THEN NULL "
            f"WHEN {vec} < 0.0 THEN {vec} + 360.0 "
            f"ELSE {vec} END")


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
            # DESC + reverse, not ASC: when a short window has more rows than
            # `limit` (two sources at ~16s fill 2000 rows in ~6h), ASC drops the
            # NEWEST rows, so the chart just ends hours early with no hint. Keep
            # the most recent `limit` rows and hand them back oldest-first.
            rows = await (await db.execute(
                """
                SELECT data_json FROM observations
                WHERE mac = ? AND dateutc_ms BETWEEN ? AND ?
                ORDER BY dateutc_ms DESC
                LIMIT ?
                """,
                (mac, start_ms, end_ms, limit),
            )).fetchall()
        parsed = [_normalize_lightning(p) for r in reversed(rows)
                  if (p := _parse_json_col(r["data_json"]))]
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
          AVG(tempinf)        AS tempinf,
          AVG(humidityin)     AS humidityin,
          AVG(feels_like)     AS feelsLike,
          AVG(dew_point)      AS dewPoint,
          AVG(humidity)       AS humidity,
          AVG(baromrelin)     AS baromrelin,
          AVG(windspeedmph)   AS windspeedmph,
          AVG(windgustmph)    AS windgustmph,
          {_winddir_expr()}   AS winddir,
          AVG(hourlyrainin)   AS hourlyrainin,
          MAX(yearlyrainin)   AS yearlyrainin,
          MAX(dailyrainin)    AS dailyrainin,
          AVG(uv)             AS uv,
          AVG(solarradiation) AS solarradiation,
          -- MAX, not AVG: the trailing-hour strike count is what the bucket
          -- peaked at, and averaging a storm's ramp against its tail hides
          -- the number that mattered. CAST because the app decodes strikes
          -- as Int? (synthesized Codable) — a REAL-typed 731.0 in the JSON
          -- would fail the whole row's decode, not just this field.
          CAST(MAX(lightning_last_1hr) AS INTEGER) AS lightning_last_1hr,
          -- Air quality (1.8): AVG for the level lines; CO2/PM peaks matter
          -- for "how bad did it get", so carry the bucket MAX for the two
          -- health-relevant series alongside.
          AVG(pm25)           AS pm25,
          AVG(pm10)           AS pm10,
          AVG(co2)            AS co2,
          AVG(tvoc_index)     AS tvoc_index,
          AVG(nox_index)      AS nox_index,
          MAX(pm25)           AS pm25_max,
          MAX(co2)            AS co2_max,
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
          MIN(windgustmph)    AS windgustmph_min,
          MAX(windgustmph)    AS windgustmph_max,
          MIN(hourlyrainin)   AS hourlyrainin_min,
          MAX(hourlyrainin)   AS hourlyrainin_max,
          MAX(uv)             AS uv_max,
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
        # _parse_json_col, not bare json.loads: upsert_discovery stores with
        # json.dumps(allow_nan=True), so a payload carrying 1e999 persists as
        # an `Infinity` literal that round-trips to inf — surviving today only
        # because Pydantic's serializer emits null. Scrub on the way out.
        sample = _parse_json_col(r["sample_json"]) or None
        out.append({
            "model": r["model"],
            "id": r["sensor_id"],
            "first_seen_ms": r["first_seen_ms"],
            "last_seen_ms": r["last_seen_ms"],
            "seen_count": r["seen_count"],
            "sample": sample,
        })
    return out


async def _rain_col_at_or_before(mac: str, col: str, cutoff_ms: int,
                                 fallback_earliest: bool = True) -> float | None:
    """Most recent value of a cumulative rain column at or before `cutoff_ms`.
    Falls back to the earliest value on file if no row sits before the cutoff
    (so a freshly-deployed sensor still gets sensible rollups). Returns None
    only if the device has zero non-null values for the column.

    Also used for non-rain columns via value_at_or_before (smart alerts read
    baromrelin here for pressure tendency) — the whitelist is the full observation
    column set, which is what keeps the f-string interpolation injection-safe.
    It previously allowed only the two rain columns, so the pressure-tendency
    lookup raised AssertionError on every alert tick and took the whole smart-alert
    pass (frost + heat included) down with it.

    `col` is an INTERNAL whitelisted column name (never user input), so the
    f-string interpolation is safe.

    The whitelist check is a `raise`, NOT an `assert`. Asserts are stripped by
    `python -O` / `PYTHONOPTIMIZE=1`, which would silently delete the only
    thing standing between this f-string and SQL injection if a caller ever
    started passing an unresolved name. The image does not use -O today, so
    this was never exploitable — but a guard that a build flag can remove is
    not a guard. CLAUDE.md states this rule; this line predated it."""
    if col not in _COLUMNS:
        raise ValueError(f"refusing to interpolate unknown column {col!r}")
    async with connect() as db:
        row = await (await db.execute(
            f"SELECT {col} AS v FROM observations "
            f"WHERE mac = ? AND dateutc_ms <= ? AND {col} IS NOT NULL "
            f"ORDER BY dateutc_ms DESC LIMIT 1",
            (mac, cutoff_ms),
        )).fetchone()
        if row:
            return row["v"]
        if not fallback_earliest:
            # Callers measuring a CHANGE OVER A FIXED WINDOW must not get the
            # earliest row on file: on a young device that turns a "3h pressure
            # delta" into "delta since we started recording 10 minutes ago",
            # which fires bogus storm alerts. No qualifying row = not computable.
            return None
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
        # No yearly and no monthly counter, ever. Tempest is this shape: the
        # WeatherFlow REST response carries a per-day accumulator and a
        # trailing hour, nothing longer — so derive the longer periods from
        # the stored DAILY counters instead (tier 3 below).
        return await _rollups_from_daily(mac, tz)
    # _tolerant_float: TEXT stored in these REAL columns must degrade to
    # "not computable" (all-None skeleton below), not raise.
    cur_year = _tolerant_float(row["yearlyrainin"])
    cur_month = _tolerant_float(row["monthlyrainin"])

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
            prior = _tolerant_float(await yearly_rain_at_or_before(mac, boundary_ms))
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
        prior = _tolerant_float(
            await _rain_col_at_or_before(mac, "monthlyrainin", boundary_ms))
        return None if prior is None else round(max(0.0, cur_month - prior), 3)
    # Straddles the month boundary: this month's rain + last month's tail.
    prev_final = _tolerant_float(
        await _rain_col_at_or_before(mac, "monthlyrainin", start_of_month_ms - 1))
    prior = _tolerant_float(
        await _rain_col_at_or_before(mac, "monthlyrainin", boundary_ms))
    if prev_final is None or prior is None:
        return round(max(0.0, cur_month), 3)  # best effort: at least this month
    return round(max(0.0, cur_month + max(0.0, prev_final - prior)), 3)


async def _rollups_from_daily(mac: str, tz) -> dict[str, float | None]:
    """Week/month/year rain for a source whose ONLY counter is the per-day
    one (Tempest: `precip_accum_local_day`; no monthly, no yearly).

    A day's total is the day's MAX of the daily counter — the high-water
    choice storm.counter_progress already made, because WeatherFlow revises
    the accumulator DOWNWARD mid-day and then climbs again; taking the last
    value instead would drop a day whose final reading was a low revision,
    and summing increments re-counts every re-climb. Periods are then sums
    of per-day totals.

    One index-only scan (dailyrainin lives in idx_obs_chart), grouped into
    local days in SQL and summed per boundary here — at a 60s cadence the
    year window is ~500k index rows collapsing to ≤366. Day bucketing uses
    a fixed local-midnight anchor, so in a DST zone the two shift nights a
    year misassign one hour of rain to a neighbouring day; the counter is
    station-local anyway, and this stays exact in fixed-offset zones.

    `hourly_in`/`daily_in` stay None on purpose: a source in this tier posts
    those itself, and its own (revisable) current value is the truth — a MAX
    here could contradict the number the station is showing right now.
    `yearly_in` is a true calendar YTD, unlike the sensor-native lifetime
    counters tier 1 works from — which is why only this tier reports it.

    Cached ~60s per (mac, local day) — R5-33, measured on Doren's guest
    2026-08-23: at 1.15M rows the two scans below cost 0.5–3.0s on a cold
    page cache (shared-CPU/256MB can't keep the index hot), and /current
    runs this for EVERY request to a tier-3 station — every client's 60s
    poll, every widget, every public-page build. A Tempest at its 1-minute
    cadence reaches that row count in about a year. The cached values are
    week/month/year sums only, so 60s of staleness is invisible; hourly/
    daily stay None (the station's own numbers are the truth for those)."""
    from datetime import datetime, timedelta

    now_local = datetime.now(tz=tz)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    cache_key = (mac, start_of_today.date().isoformat())
    now_mono = time.monotonic()
    hit = _DAILY_ROLLUP_CACHE.get(cache_key)
    if hit is not None and now_mono < hit[0]:
        return dict(hit[1])

    start_of_week = start_of_today - timedelta(days=(now_local.weekday() + 1) % 7)
    start_of_month = start_of_today.replace(day=1)
    start_of_year = start_of_today.replace(month=1, day=1)
    # The earliest boundary anchors the scan: in the first week of January
    # the week starts in December, and anchoring at Jan 1 would silently
    # drop those days from the weekly total.
    anchor_ms = int(min(start_of_week, start_of_year).timestamp() * 1000)

    out: dict[str, float | None] = {"hourly_in": None, "daily_in": None,
                                    "weekly_in": None, "monthly_in": None,
                                    "yearly_in": None}
    day_ms = 86_400_000
    async with connect() as db:
        # Same cumulative-counter judgment records() makes: a REAL per-day
        # counter touches ~0 at some reset; a lifetime counter stored in
        # dailyrainin never does, and summing its daily maxima reports
        # 17.10 + 17.16 = 34.26" across two days instead of the 0.06"
        # increment (CodeRabbit, 2026-08-20). Not computable → all-None.
        # Bounded to the anchor window (R5-33's other half): the judgment
        # only needs to hold for the rows being summed, a real per-day
        # counter touches ~0 on any dry midnight inside the window, and a
        # lifetime counter stays high in ANY window — same verdict, one
        # year of index instead of the station's whole history.
        floor_row = await (await db.execute(
            "SELECT MIN(dailyrainin) AS lo FROM observations "
            "WHERE mac = ? AND dateutc_ms >= ?",
            (mac, anchor_ms))).fetchone()
        lo = _tolerant_float(floor_row["lo"]) if floor_row else None
        if lo is not None and lo > _DAILY_RAIN_RESET_FLOOR_IN:
            _rollup_cache_store(cache_key, out, now_mono)
            return out
        rows = await (await db.execute(
            """
            SELECT (dateutc_ms - ?) / ? AS day, MAX(dailyrainin) AS m
            FROM observations
            WHERE mac = ? AND dateutc_ms >= ? AND dailyrainin IS NOT NULL
            GROUP BY day
            """,
            (anchor_ms, day_ms, mac, anchor_ms))).fetchall()
    if not rows:
        _rollup_cache_store(cache_key, out, now_mono)
        return out
    days = [(r["day"], _tolerant_float(r["m"])) for r in rows]
    for name, boundary in (("weekly_in", start_of_week),
                           ("monthly_in", start_of_month),
                           ("yearly_in", start_of_year)):
        # ROUND, don't floor: boundary and anchor are both local midnights,
        # but in a DST zone they can sit an hour apart (EDT boundary vs EST
        # anchor), so the difference is N days ± 1h. Flooring N - 1h yields
        # N-1 and the period absorbed an ENTIRE extra prior day — a week of
        # rain reported as eight days for the whole DST half of the year
        # (2026-08-20 review). Rounding recovers N exactly and bounds the
        # residual error to the docstring's acknowledged single hour.
        first_day = ((int(boundary.timestamp() * 1000) - anchor_ms
                      + day_ms // 2) // day_ms)
        total = sum(m for d, m in days if d >= first_day and m is not None)
        out[name] = round(max(0.0, total), 3)
    _rollup_cache_store(cache_key, out, now_mono)
    return out


# R5-33: (mac, local-day) → (expires_monotonic, result). Keyed by day so a
# midnight rollover always recomputes with fresh boundaries even inside the
# TTL; stale-day entries are pruned on store, so the dict stays a handful of
# entries. TTL is a module var, not a constant, so tests can zero it.
_DAILY_ROLLUP_CACHE: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
_DAILY_ROLLUP_TTL_S = 60.0


def _rollup_cache_store(key: tuple[str, str], value: dict[str, float | None],
                        now_mono: float) -> None:
    stale = [k for k in _DAILY_ROLLUP_CACHE if k[1] != key[1]]
    for k in stale:
        _DAILY_ROLLUP_CACHE.pop(k, None)
    _DAILY_ROLLUP_CACHE[key] = (now_mono + _DAILY_ROLLUP_TTL_S, dict(value))


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
              MIN(CASE WHEN typeof({col}) IN ('integer','real') THEN {col} END) AS lo,
              MAX(CASE WHEN typeof({col}) IN ('integer','real') THEN {col} END) AS hi,
              AVG(CASE WHEN typeof({col}) IN ('integer','real') THEN {col} END) AS avg,
              COUNT(CASE WHEN typeof({col}) IN ('integer','real') THEN {col} END) AS n
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


async def field_series(
    mac: str, field: str, start_ms: int, end_ms: int, points: int
) -> list[float | None]:
    """Bucket-averaged series for one field: `points` equal buckets across
    [start_ms, end_ms), None where a bucket holds no rows (a data gap must
    stay a gap — never zero). Built for widget/complication sparklines,
    where the full bucketed /history payload is too heavy.

    `field` is the public API name; the column it resolves to is
    interpolated into SQL, so it MUST come from _FIELD_MAP and nowhere
    else (same guard as aggregate(), and a real raise, not an assert).
    """
    inverse = {v: k for k, v in _FIELD_MAP.items()}
    if field not in inverse:
        raise ValueError(f"unknown field {field!r}")
    col = inverse[field]
    span = end_ms - start_ms
    if span <= 0 or points <= 0:
        return []
    # Ceil so the last observation's bucket index stays < points; a floored
    # width made the tail spill into an index that was then dropped.
    bucket = -(-span // points)
    async with connect() as db:
        rows = await (await db.execute(
            f"""
            SELECT CAST((dateutc_ms - ?) / ? AS INTEGER) AS b, AVG({col}) AS v
            FROM observations
            WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms < ?
              AND {col} IS NOT NULL
            GROUP BY b ORDER BY b
            """,
            (start_ms, bucket, mac, start_ms, end_ms),
        )).fetchall()
    out: list[float | None] = [None] * points
    for r in rows:
        if 0 <= r["b"] < points:
            out[r["b"]] = r["v"]
    return out


# Curated metrics for the records screen. API field names (see _FIELD_MAP).
RECORD_FIELDS = [
    "tempf", "feelsLike", "dewPoint", "humidity", "baromrelin",
    "windspeedmph", "windgustmph", "uv", "solarradiation", "dailyrainin",
    # Trailing-hour strike count — "most strikes in an hour" is the record
    # shape people quote. COUNT()=0 for a station with no detector, so the
    # field is omitted from the response and the app shows no card (absent
    # is not zero). The per-interval lightningcount is deliberately NOT a
    # record: its magnitude depends on the source's reporting interval.
    "lightning_last_1hr",
]

# "Wettest day" comes from dailyrainin.max. A real daily-rain counter resets to
# ~0 every local midnight, so its all-time MIN is near 0. Some sources (e.g. an
# SDR posting a sensor's lifetime cumulative counter) historically stored a
# non-resetting value in dailyrainin — its "max" is then a lifetime total, not a
# single-day record. If a period's MIN never drops near this floor, the counter
# isn't resetting and its max is dropped as unreliable.
_DAILY_RAIN_RESET_FLOOR_IN = 5.0


# Which daily_rollups columns back each record field on the long periods.
# (min_col, max_col); a None min means the rollups keep only the day's peak —
# which is also the only half the records screen shows for those metrics.
_ROLLUP_RECORD_COLS: dict[str, tuple[str | None, str]] = {
    "tempf":              ("tempf_min", "tempf_max"),
    "feelsLike":          ("feels_like_min", "feels_like_max"),
    "dewPoint":           ("dew_point_min", "dew_point_max"),
    "humidity":           ("humidity_min", "humidity_max"),
    "baromrelin":         ("baromrelin_min", "baromrelin_max"),
    "windspeedmph":       (None, "windspeedmph_max"),
    "windgustmph":        (None, "windgustmph_max"),
    "uv":                 (None, "uv_max"),
    "solarradiation":     (None, "solarradiation_max"),
    "dailyrainin":        (None, "rain_total"),
    "lightning_last_1hr": (None, "lightning_max"),
}


async def records(mac: str, tz_name: str = "UTC",
                  fields: list[str] | None = None) -> dict[str, Any]:
    """Per-metric high/low over today / this month / this year / all-time.

    TODAY is answered from raw observations (one bounded local day), keeping
    exact record times. The LONG periods are answered from `daily_rollups`
    when they cover the period — a few thousand pre-folded rows instead of a
    full per-mac history scan, which measured 110 s cold on a 1.09M-row
    archive (Doren, 2026-08-20) against an app timeout well under that. The
    UI shows date-only for every period except Today, so serving long-period
    record times at day precision (local noon of the record's day) changes
    nothing visible.

    Rollups must actually COVER the period to be trusted: INSIGHTS enabled
    late (or never rebuilt) leaves them starting after the archive does, and
    answering all-time from partial rollups would silently erase the oldest
    records. Coverage is judged once per call against the first observation;
    uncovered periods fall back to the raw scan, as does everything when
    rollups are absent entirely.

    Long-period caveats of the rollup path: `min`/`avg` are None for the
    peak-only metrics (wind, gust, UV, solar, rain, lightning) — the screen
    only quotes their peaks — and rollups are fold-forward accumulators, so
    a data repair must still `insights.rebuild()` (the established lesson)
    for long-period records to heal."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fields = fields or RECORD_FIELDS
    inverse = {v: k for k, v in _FIELD_MAP.items()}
    cols = [(f, inverse[f]) for f in fields if f in inverse]
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now_local = datetime.now(tz=tz)
    day0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    periods = {
        "today": int(day0.timestamp() * 1000),
        "month": int(day0.replace(day=1).timestamp() * 1000),
        "year":  int(day0.replace(month=1, day=1).timestamp() * 1000),
        "all":   0,
    }
    end_ms = int(now_local.timestamp() * 1000)

    out: dict[str, Any] = {"mac": mac, "generated_ms": end_ms, "periods": {}}
    async with connect() as db:
        # Is dailyrainin a real per-day counter, or a lifetime cumulative one?
        # Judge ONCE over ALL history: a counter that resets necessarily touches
        # ~0 at some point. Testing each period's own MIN instead would suppress
        # a LEGITIMATE record — a station that came online mid-downpour has every
        # reading that day above the floor.
        _dr = await (await db.execute(
            "SELECT MIN(dailyrainin) AS lo FROM observations WHERE mac = ?",
            (mac,))).fetchone()
        daily_is_cumulative = (_dr is not None and _dr["lo"] is not None
                               and _dr["lo"] > _DAILY_RAIN_RESET_FLOOR_IN)

        # Repairs and column backfills poison fold-forward rollups in ways
        # depth/recency can't see (maxima never go down; a new column stays
        # NULL) — maintenance helpers and the lightning backfill set this
        # flag, and a successful full insights.rebuild() clears it. While
        # set, every long period takes the raw scan: slow but never wrong
        # (CODE_REVIEW_R5 R5-14/R5-15).
        dirty_row = await (await db.execute(
            "SELECT v FROM server_kv WHERE k = 'rollups_dirty'")).fetchone()
        rollups_dirty = (dirty_row is not None
                         and dirty_row["v"] not in (None, "", "0"))

        # Rollup coverage check (see docstring). All index-only lookups.
        cov = await (await db.execute(
            "SELECT MIN(day) AS lo, MAX(day) AS hi FROM daily_rollups "
            "WHERE mac = ?", (mac,))).fetchone()
        first_rollup_day: str | None = cov["lo"] if cov else None
        last_rollup_day: str | None = cov["hi"] if cov else None
        span = await (await db.execute(
            "SELECT MIN(dateutc_ms) AS lo, MAX(dateutc_ms) AS hi "
            "FROM observations WHERE mac = ?", (mac,))).fetchone()
        first_obs_day = last_obs_day = None
        if span and span["lo"] is not None:
            first_obs_day = (datetime.fromtimestamp(span["lo"] / 1000, tz=tz)
                             .strftime("%Y-%m-%d"))
            last_obs_day = (datetime.fromtimestamp(span["hi"] / 1000, tz=tz)
                            .strftime("%Y-%m-%d"))

        def rollups_cover(start_ms: int) -> bool:
            if first_rollup_day is None or first_obs_day is None:
                return False
            # Rollups must also be CURRENT, not just deep: INSIGHTS turned
            # off after a rebuild freezes daily_rollups while observations
            # keep landing, and answering from the frozen table would pin
            # month/year records at the disable date forever (CodeRabbit,
            # 2026-08-20). Live folding stamps the last observation's local
            # day on every insert, so equality is the healthy state.
            if last_rollup_day is None or (last_obs_day is not None
                                           and last_rollup_day < last_obs_day):
                return False
            period_day = (datetime.fromtimestamp(start_ms / 1000, tz=tz)
                          .strftime("%Y-%m-%d") if start_ms else "0000-00-00")
            # Trusted when the rollups reach back at least as far as the
            # period needs — the period start, or the archive's own first
            # day for all-time. Lexicographic compare is safe: the day
            # column is zero-padded ISO dates.
            return first_rollup_day <= max(period_day, first_obs_day)

        for pname, start_ms in periods.items():
            if pname == "today" or rollups_dirty or not rollups_cover(start_ms):
                pfields = await _raw_period_fields(db, mac, cols,
                                                   start_ms, end_ms)
            else:
                pfields = await _rollup_period_fields(db, mac, fields,
                                                       start_ms, tz)
            # Drop a "wettest day" that's really a non-resetting cumulative
            # counter (verdict computed once above, not per period).
            dr = pfields.get("dailyrainin")
            if dr and daily_is_cumulative:
                dr["max"] = None
                dr["maxAt"] = None
            out["periods"][pname] = {"start_ms": start_ms, "fields": pfields}
    return out


async def _raw_period_fields(db, mac: str, cols: list[tuple[str, str]],
                             start_ms: int, end_ms: int) -> dict[str, Any]:
    """The original raw-observation path: exact record times, full scan of
    the period window. Kept for Today (one bounded day) and as the fallback
    when rollups are absent or don't cover the period."""
    pfields: dict[str, Any] = {}
    for fname, col in cols:
        # col is one of our own _FIELD_MAP keys — never user input —
        # so the f-string interpolation is safe (same as aggregate()).
        # typeof() filter (R6): SQLite orders TEXT above every number, so
        # one garbled reading stored as 'bad' in a REAL column became the
        # displayed all-time MAX, and AVG coerced it to 0.0 and dragged the
        # mean. storm_window_stats grew this guard first; records was the
        # screen it mattered most on and didn't have it.
        num = f"CASE WHEN typeof({col}) IN ('integer','real') THEN {col} END"
        row = await (await db.execute(
            f"SELECT MIN({num}) AS lo, MAX({num}) AS hi, "
            f"AVG({num}) AS avg, COUNT({num}) AS n FROM observations "
            f"WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms <= ?",
            (mac, start_ms, end_ms),
        )).fetchone()
        if row is None or not row["n"]:
            continue

        async def _at(val: Any) -> int | None:
            if val is None:
                return None
            r = await (await db.execute(
                f"SELECT dateutc_ms FROM observations WHERE mac = ? "
                f"AND dateutc_ms >= ? AND dateutc_ms <= ? AND {col} = ? "
                f"ORDER BY dateutc_ms ASC LIMIT 1",
                (mac, start_ms, end_ms, val),
            )).fetchone()
            return r["dateutc_ms"] if r else None

        pfields[fname] = {
            "min": row["lo"], "max": row["hi"],
            "avg": round(row["avg"], 2) if row["avg"] is not None else None,
            "count": row["n"],
            "minAt": await _at(row["lo"]),
            "maxAt": await _at(row["hi"]),
        }
    return pfields


async def _rollup_period_fields(db, mac: str, fields: list[str],
                                start_ms: int, tz) -> dict[str, Any]:
    """Long-period records from daily_rollups: one aggregate scan over the
    period's day rows plus a tiny day lookup per extreme. `count` is the
    number of DAYS with data (the raw path counts samples; nothing reads the
    number as a sample count, and days-with-data keeps the omit-when-empty
    contract that hides absent sensors)."""
    from datetime import datetime, timedelta

    start_day = (datetime.fromtimestamp(start_ms / 1000, tz=tz)
                 .strftime("%Y-%m-%d") if start_ms else "0000-00-00")
    wanted = [(f, *_ROLLUP_RECORD_COLS[f]) for f in fields
              if f in _ROLLUP_RECORD_COLS]
    parts = []
    for fname, min_col, max_col in wanted:
        if min_col:
            parts.append(f"MIN({min_col}) AS {min_col}")
        parts.append(f"MAX({max_col}) AS {max_col}")
        parts.append(f"COUNT({max_col}) AS n_{max_col}")
    # avg only where the rollups carry sums (temperature).
    parts.append("SUM(tempf_sum) AS t_sum")
    parts.append("SUM(tempf_n) AS t_n")
    agg = await (await db.execute(
        f"SELECT {', '.join(parts)} FROM daily_rollups "
        f"WHERE mac = ? AND day >= ?",
        (mac, start_day))).fetchone()
    if agg is None:
        return {}

    async def _at_day(col: str, val: Any) -> int | None:
        """Local NOON of the first day holding the extreme — the UI renders
        long-period record times date-only, so noon just keeps the date
        stable against small timezone shifts in the client."""
        if val is None:
            return None
        r = await (await db.execute(
            f"SELECT day FROM daily_rollups WHERE mac = ? AND day >= ? "
            f"AND {col} = ? ORDER BY day ASC LIMIT 1",
            (mac, start_day, val))).fetchone()
        if not r:
            return None
        d = datetime.strptime(r["day"], "%Y-%m-%d").replace(tzinfo=tz)
        return int((d + timedelta(hours=12)).timestamp() * 1000)

    pfields: dict[str, Any] = {}
    for fname, min_col, max_col in wanted:
        n = agg[f"n_{max_col}"]
        if not n:
            continue
        lo = agg[min_col] if min_col else None
        hi = agg[max_col]
        avg = None
        if fname == "tempf" and agg["t_n"]:
            avg = round(agg["t_sum"] / agg["t_n"], 2)
        pfields[fname] = {
            "min": lo, "max": hi, "avg": avg, "count": n,
            "minAt": await _at_day(min_col, lo) if min_col else None,
            "maxAt": await _at_day(max_col, hi),
        }
    return pfields
