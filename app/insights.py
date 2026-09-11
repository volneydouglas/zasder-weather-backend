"""Insights: server-side weather statistics over incrementally-maintained
rollup tables.

Everything here reads `daily_rollups` / `hour_rollups`, which are updated
in the same transaction breath as observation storage (one UPSERT per
stored row — microseconds, three per minute on a typical install). The
raw-history table is never scanned at insight time, so the feature costs
less CPU than the /records endpoint it resembles.

Opt-in via `INSIGHTS=1` (default off, like PUBLIC_DASHBOARD). The flag
gates BOTH maintenance and the endpoint; enabling it later on existing
data (or after a WU import while disabled) requires `rebuild()` — the
endpoint's response says so when rollups are empty but history isn't.

Generalization is deliberate: no location-specific thresholds. "Hot day"
tiers combine fixed reference lines (80/90/95/100/105/110 °F) with the
STATION'S OWN percentiles (p90/p99 of its daily highs), so the ledger is
as meaningful in Seattle as in Chandler. Anomalies compare against the
station's own monthly normals. Degree days use the standard 65 °F base.
"""
from __future__ import annotations

import logging
import asyncio
import math
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .day_rain import day_rain_in

log = logging.getLogger("zasder.insights")

# Fixed reference tiers (°F) for the heat ledger; percentile tiers ride
# alongside. Fahrenheit because storage is API-native — clients convert.
LEDGER_TIERS = (80.0, 90.0, 95.0, 100.0, 105.0, 110.0)
DEGREE_DAY_BASE_F = 65.0

# Cold ledger: days whose LOW reached at or below each tier. CLIMATE-
# ADAPTIVE since 1.7 (reviewer feedback, 2026-08-22): the fixed
# horticultural ladder (45 chill / 36 frost / 32 freeze / 28 hard freeze /
# 25 severe) is perfect for Chandler and useless for Minneapolis, where
# every winter night piles into every column while the -10° nights people
# actually remember have no column at all. Tiers are picked from the
# station's own p10-of-lows so the ledger answers "how unusually cold was
# this year HERE" everywhere. Freeze-free season stays anchored at 32°
# regardless — it measures season LENGTH, not frequency, and 32° is what
# the software actually computes (air-temperature freeze, not frost).
COLD_TIERS_WARM      = (45.0, 36.0, 32.0, 28.0, 25.0)
COLD_TIERS_TEMPERATE = (32.0, 20.0, 10.0, 0.0, -10.0)
COLD_TIERS_COLD      = (20.0, 10.0, 0.0, -10.0, -20.0)
COLD_TIERS_ARCTIC    = (0.0, -10.0, -20.0, -30.0, -40.0)
FREEZE_F = 32.0


def cold_tiers_for(p10_low: float | None) -> tuple[float, ...]:
    """Tier ladder for this station's climate, bucketed by its own
    10th-percentile low. Buckets, not a formula: the reviewer's per-climate
    sets are hand-tuned to what residents find remarkable, and a smooth
    formula loses the semantic anchors (36 frost / 32 freeze / 28 hard
    freeze) exactly where they matter. None (no history yet) reads warm —
    the shipped default, so a brand-new station changes nothing."""
    if p10_low is None or p10_low >= 32:
        return COLD_TIERS_WARM
    if p10_low >= 10:
        return COLD_TIERS_TEMPERATE
    if p10_low >= -10:
        return COLD_TIERS_COLD
    return COLD_TIERS_ARCTIC

# Rain gap: a day "rained" when it recorded at least one bucket tip. Fixed
# line, no location-specific threshold — same generalization stance as the
# ledgers (a trace day counts the same in Seattle as in Chandler).
RAIN_DAY_MIN_IN = 0.01

# ── year-to-year comparability ──────────────────────────────────────────
# THE canonical rule for "may this year be compared against that one?",
# used by the story engine's ledger baseline and published per year in the
# payload so a client never has to invent a threshold of its own.
#
# The failure it exists to prevent is the same one in two costumes. A year
# the station spent mostly offline has fewer hot days because it has fewer
# days, so letting it into a baseline manufactures records. A year the
# station joined in June has no rain before June, so reading its missing
# total as 0.00 in claims a drought it never measured. Absent is not zero,
# and the only honest answer for a year that isn't covered is to say so.
COMPARISON_MIN_DAYS = 30
COMPARISON_COVERAGE = 0.80


def comparable_to_date(days: int, reference_days: int) -> bool:
    """Did this year cover enough of the same calendar window to be quoted
    beside a reference year that covered `reference_days` of it?

    Both counts are DAYS WITH DATA up to the shared anchor. The floor is
    absolute as well as relative: 80% of a three-week reference is still
    three weeks, and three weeks is not a year.
    """
    if days <= 0 or reference_days <= 0:
        return False
    return days >= max(COMPARISON_MIN_DAYS, COMPARISON_COVERAGE * reference_days)


def window_days_to_anchor(year: int, anchor_md: str) -> int:
    """Calendar days in `year` from Jan 1 through the anchor month-day —
    the DENOMINATOR coverage is measured against.

    Counted by walking the anchor backwards to a date that exists rather
    than by arithmetic, because the one anchor that needs care is Feb 29:
    in a non-leap year the string window "everything ≤ 02-29" holds exactly
    the 59 days through Feb 28, and a client that computed 60 would mark a
    fully-covered year partial every fourth year.
    """
    try:
        month, day = int(anchor_md[:2]), int(anchor_md[3:5])
    except (ValueError, IndexError):
        return 0
    while day > 0:
        try:
            return (date(year, month, day) - date(year, 1, 1)).days + 1
        except ValueError:
            day -= 1
    return 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_rollups (
    mac        TEXT NOT NULL,
    day        TEXT NOT NULL,           -- local date YYYY-MM-DD (settings tz)
    tempf_min  REAL, tempf_max REAL, tempf_sum REAL, tempf_n INTEGER,
    humidity_min REAL, humidity_max REAL,
    windspeedmph_max REAL, windgustmph_max REAL,
    baromrelin_min REAL, baromrelin_max REAL,
    dew_point_min REAL, dew_point_max REAL,
    feels_like_min REAL, feels_like_max REAL,
    uv_max REAL, solarradiation_max REAL,
    rain_total REAL,                    -- max(dailyrainin) seen that day
    yearly_min REAL, yearly_max REAL,   -- fallback rain delta for SDR sources
    lightning_max REAL,                 -- peak strikes/hr that day (1.6; ALTERed in)
    -- 2.2 (ALTERed in; db.init_db lists them in ROLLUP_LATE_COLUMNS):
    -- sums for the long-period means the MCP analyses asked for, and the
    -- air-monitor pair so daily_summary answers for an AirGradient/Govee.
    humidity_sum REAL, humidity_n INTEGER,
    windspeedmph_sum REAL, windspeedmph_n INTEGER,
    baromrelin_sum REAL, baromrelin_n INTEGER,
    pm25_min REAL, pm25_max REAL, pm25_sum REAL, pm25_n INTEGER,
    co2_min REAL, co2_max REAL, co2_sum REAL, co2_n INTEGER,
    tempinf_min REAL, tempinf_max REAL,
    -- The yearly counter's first and last reading of the day, with their
    -- times, so "did the counter reset today" is a fact (last < first)
    -- rather than the min/max signature guess, and the day's rain from a
    -- lifetime counter is last - first (2.2, ref_rain_counters).
    yearly_first REAL, yearly_first_ms INTEGER,
    yearly_last REAL, yearly_last_ms INTEGER,
    PRIMARY KEY (mac, day)
);

-- Diurnal profile: month-of-year x hour-of-day, aggregated across years.
-- feels_* added later — db.init_db migrates older tables via ALTER.
CREATE TABLE IF NOT EXISTS hour_rollups (
    mac   TEXT NOT NULL,
    month INTEGER NOT NULL,             -- 1..12 (local)
    hour  INTEGER NOT NULL,             -- 0..23 (local)
    tempf_sum REAL NOT NULL DEFAULT 0,
    tempf_n   INTEGER NOT NULL DEFAULT 0,
    feels_sum REAL NOT NULL DEFAULT 0,
    feels_n   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mac, month, hour)
);

-- Comfort ledger (2.0): year x month x hour-of-day COUNTS of feels-like
-- readings inside, above and below the comfort band. Keyed by YEAR, which
-- hour_rollups is not, so a producer can rank this year's months against
-- the record's. Only the SHARES (comfortable_n / n) are read: a share of
-- bucket-sampled raw is unbiased, so the table rebuilds from thinned
-- history like hour_rollups does; the raw counts are never quoted as hours.
CREATE TABLE IF NOT EXISTS comfort_rollups (
    mac   TEXT NOT NULL,
    year  INTEGER NOT NULL,
    month INTEGER NOT NULL,             -- 1..12 (local)
    hour  INTEGER NOT NULL,             -- 0..23 (local)
    n             INTEGER NOT NULL DEFAULT 0,
    comfortable_n INTEGER NOT NULL DEFAULT 0,
    hot_n         INTEGER NOT NULL DEFAULT 0,
    cold_n        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mac, year, month, hour)
);
"""

# The comfort band, on FEELS-LIKE, in storage units (°F). Between these a
# reading counts as comfortable outdoors: 60°F is where most people want a
# layer, 80°F is where shade starts to matter. The band is decided at fold
# time, so moving it means a rollup rebuild (set `rollups_dirty`).
COMFORT_LOW_F = 60.0
COMFORT_HIGH_F = 80.0

# daily_rollups columns that arrived after the table shipped, with their
# DDL. db.init_db ALTERs any that are missing and marks the rollups dirty
# so the lifespan rebuild folds history into them; until then old days
# read NULL (= "no data"), never 0.
ROLLUP_LATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("lightning_max", "REAL"),
    ("humidity_sum", "REAL"), ("humidity_n", "INTEGER"),
    ("windspeedmph_sum", "REAL"), ("windspeedmph_n", "INTEGER"),
    ("baromrelin_sum", "REAL"), ("baromrelin_n", "INTEGER"),
    ("pm25_min", "REAL"), ("pm25_max", "REAL"), ("pm25_sum", "REAL"), ("pm25_n", "INTEGER"),
    ("co2_min", "REAL"), ("co2_max", "REAL"), ("co2_sum", "REAL"), ("co2_n", "INTEGER"),
    ("tempinf_min", "REAL"), ("tempinf_max", "REAL"),
    ("yearly_first", "REAL"), ("yearly_first_ms", "INTEGER"),
    ("yearly_last", "REAL"), ("yearly_last_ms", "INTEGER"),
)

# (column stem) -> the rollup_params key that feeds it, for the sum/n pairs.
MEAN_STEMS: tuple[tuple[str, str], ...] = (
    ("humidity", "humidity"), ("windspeedmph", "windspeedmph"),
    ("baromrelin", "baromrelin"), ("pm25", "pm25"), ("co2", "co2"),
)


def rollup_mean(row: Any, stem: str) -> float | None:
    """Mean of a day's readings from its sum/n pair, None when absent.
    Rows are sqlite Row or dict; a pre-2.2 row without the columns is None."""
    try:
        keys = row.keys()
        if f"{stem}_sum" not in keys or f"{stem}_n" not in keys:
            return None
        total, n = row[f"{stem}_sum"], row[f"{stem}_n"]
    except (AttributeError, KeyError, IndexError):
        return None
    if not n or total is None:
        return None
    try:
        v = float(total) / float(n)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(v, 2) if math.isfinite(v) else None

_UPSERT_DAILY = """
INSERT INTO daily_rollups (mac, day,
    tempf_min, tempf_max, tempf_sum, tempf_n,
    humidity_min, humidity_max, windspeedmph_max, windgustmph_max,
    baromrelin_min, baromrelin_max, dew_point_min, dew_point_max,
    feels_like_min, feels_like_max, uv_max, solarradiation_max,
    rain_total, yearly_min, yearly_max, lightning_max,
    humidity_sum, humidity_n, windspeedmph_sum, windspeedmph_n,
    baromrelin_sum, baromrelin_n,
    pm25_min, pm25_max, pm25_sum, pm25_n,
    co2_min, co2_max, co2_sum, co2_n,
    tempinf_min, tempinf_max,
    yearly_first, yearly_first_ms, yearly_last, yearly_last_ms)
VALUES (:mac, :day,
    :tempf, :tempf, :tempf, :tempf_n,
    :humidity, :humidity, :windspeedmph, :windgustmph,
    :baromrelin, :baromrelin, :dew_point, :dew_point,
    :feels_like, :feels_like, :uv, :solarradiation,
    :dailyrainin, :yearlyrainin, :yearlyrainin, :lightning,
    :humidity, :humidity_n, :windspeedmph, :windspeedmph_n,
    :baromrelin, :baromrelin_n,
    :pm25, :pm25, :pm25, :pm25_n,
    :co2, :co2, :co2, :co2_n,
    :tempinf, :tempinf,
    :yearlyrainin, CASE WHEN :yearlyrainin IS NULL THEN NULL ELSE :ts END,
    :yearlyrainin, CASE WHEN :yearlyrainin IS NULL THEN NULL ELSE :ts END)
ON CONFLICT(mac, day) DO UPDATE SET
    tempf_min = MIN(COALESCE(tempf_min, :tempf), COALESCE(:tempf, tempf_min)),
    tempf_max = MAX(COALESCE(tempf_max, :tempf), COALESCE(:tempf, tempf_max)),
    tempf_sum = COALESCE(tempf_sum, 0) + COALESCE(:tempf, 0),
    tempf_n   = COALESCE(tempf_n, 0) + :tempf_n,
    humidity_min = MIN(COALESCE(humidity_min, :humidity), COALESCE(:humidity, humidity_min)),
    humidity_max = MAX(COALESCE(humidity_max, :humidity), COALESCE(:humidity, humidity_max)),
    windspeedmph_max = MAX(COALESCE(windspeedmph_max, :windspeedmph), COALESCE(:windspeedmph, windspeedmph_max)),
    windgustmph_max = MAX(COALESCE(windgustmph_max, :windgustmph), COALESCE(:windgustmph, windgustmph_max)),
    baromrelin_min = MIN(COALESCE(baromrelin_min, :baromrelin), COALESCE(:baromrelin, baromrelin_min)),
    baromrelin_max = MAX(COALESCE(baromrelin_max, :baromrelin), COALESCE(:baromrelin, baromrelin_max)),
    dew_point_min = MIN(COALESCE(dew_point_min, :dew_point), COALESCE(:dew_point, dew_point_min)),
    dew_point_max = MAX(COALESCE(dew_point_max, :dew_point), COALESCE(:dew_point, dew_point_max)),
    feels_like_min = MIN(COALESCE(feels_like_min, :feels_like), COALESCE(:feels_like, feels_like_min)),
    feels_like_max = MAX(COALESCE(feels_like_max, :feels_like), COALESCE(:feels_like, feels_like_max)),
    uv_max = MAX(COALESCE(uv_max, :uv), COALESCE(:uv, uv_max)),
    solarradiation_max = MAX(COALESCE(solarradiation_max, :solarradiation), COALESCE(:solarradiation, solarradiation_max)),
    rain_total = MAX(COALESCE(rain_total, :dailyrainin), COALESCE(:dailyrainin, rain_total)),
    yearly_min = MIN(COALESCE(yearly_min, :yearlyrainin), COALESCE(:yearlyrainin, yearly_min)),
    yearly_max = MAX(COALESCE(yearly_max, :yearlyrainin), COALESCE(:yearlyrainin, yearly_max)),
    lightning_max = MAX(COALESCE(lightning_max, :lightning), COALESCE(:lightning, lightning_max)),
    humidity_sum = COALESCE(humidity_sum, 0) + COALESCE(:humidity, 0),
    humidity_n   = COALESCE(humidity_n, 0) + :humidity_n,
    windspeedmph_sum = COALESCE(windspeedmph_sum, 0) + COALESCE(:windspeedmph, 0),
    windspeedmph_n   = COALESCE(windspeedmph_n, 0) + :windspeedmph_n,
    baromrelin_sum = COALESCE(baromrelin_sum, 0) + COALESCE(:baromrelin, 0),
    baromrelin_n   = COALESCE(baromrelin_n, 0) + :baromrelin_n,
    pm25_min = MIN(COALESCE(pm25_min, :pm25), COALESCE(:pm25, pm25_min)),
    pm25_max = MAX(COALESCE(pm25_max, :pm25), COALESCE(:pm25, pm25_max)),
    pm25_sum = COALESCE(pm25_sum, 0) + COALESCE(:pm25, 0),
    pm25_n   = COALESCE(pm25_n, 0) + :pm25_n,
    co2_min = MIN(COALESCE(co2_min, :co2), COALESCE(:co2, co2_min)),
    co2_max = MAX(COALESCE(co2_max, :co2), COALESCE(:co2, co2_max)),
    co2_sum = COALESCE(co2_sum, 0) + COALESCE(:co2, 0),
    co2_n   = COALESCE(co2_n, 0) + :co2_n,
    tempinf_min = MIN(COALESCE(tempinf_min, :tempinf), COALESCE(:tempinf, tempinf_min)),
    tempinf_max = MAX(COALESCE(tempinf_max, :tempinf), COALESCE(:tempinf, tempinf_max)),
    -- Ordered by the reading's own time, not arrival: a history import
    -- or a resumed relay folds rows out of order.
    yearly_first = CASE WHEN :yearlyrainin IS NULL THEN yearly_first
                        WHEN yearly_first_ms IS NULL OR :ts < yearly_first_ms THEN :yearlyrainin
                        ELSE yearly_first END,
    yearly_first_ms = CASE WHEN :yearlyrainin IS NULL THEN yearly_first_ms
                           WHEN yearly_first_ms IS NULL OR :ts < yearly_first_ms THEN :ts
                           ELSE yearly_first_ms END,
    yearly_last = CASE WHEN :yearlyrainin IS NULL THEN yearly_last
                       WHEN yearly_last_ms IS NULL OR :ts >= yearly_last_ms THEN :yearlyrainin
                       ELSE yearly_last END,
    yearly_last_ms = CASE WHEN :yearlyrainin IS NULL THEN yearly_last_ms
                          WHEN yearly_last_ms IS NULL OR :ts >= yearly_last_ms THEN :ts
                          ELSE yearly_last_ms END
"""

_UPSERT_HOUR = """
INSERT INTO hour_rollups (mac, month, hour, tempf_sum, tempf_n, feels_sum, feels_n)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(mac, month, hour) DO UPDATE SET
    tempf_sum = tempf_sum + excluded.tempf_sum,
    tempf_n   = tempf_n + excluded.tempf_n,
    feels_sum = feels_sum + excluded.feels_sum,
    feels_n   = feels_n + excluded.feels_n
"""


def _hour_params(mac: str, month: int, hour: int,
                 p: dict[str, Any]) -> tuple | None:
    """UPSERT_HOUR params, or None when the row carries neither field.
    Explicit None checks — 0.0°F is a real reading, not falsy."""
    t, f = p["tempf"], p["feels_like"]
    if t is None and f is None:
        return None
    return (mac, month, hour,
            t if t is not None else 0.0, 1 if t is not None else 0,
            f if f is not None else 0.0, 1 if f is not None else 0)


_UPSERT_COMFORT = """
INSERT INTO comfort_rollups (mac, year, month, hour, n, comfortable_n, hot_n, cold_n)
VALUES (?, ?, ?, ?, 1, ?, ?, ?)
ON CONFLICT(mac, year, month, hour) DO UPDATE SET
    n             = n + 1,
    comfortable_n = comfortable_n + excluded.comfortable_n,
    hot_n         = hot_n + excluded.hot_n,
    cold_n        = cold_n + excluded.cold_n
"""


def _comfort_params(mac: str, year: int, month: int, hour: int,
                    p: dict[str, Any]) -> tuple | None:
    """UPSERT_COMFORT params, or None when the row has no feels-like. One
    reading lands in exactly one of the three buckets."""
    f = p["feels_like"]
    if f is None:
        return None
    if f < COMFORT_LOW_F:
        return (mac, year, month, hour, 0, 0, 1)
    if f > COMFORT_HIGH_F:
        return (mac, year, month, hour, 0, 1, 0)
    return (mac, year, month, hour, 1, 0, 0)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        return ZoneInfo("UTC")


def rollup_params(row: dict[str, Any], tz: ZoneInfo) -> dict[str, Any] | None:
    """One observation row (API field names, `dateutc` in ms) → UPSERT
    params. None when the row has no timestamp."""
    ts = row.get("dateutc")
    if not isinstance(ts, (int, float)):
        return None
    local = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(tz)

    def num(key: str) -> float | None:
        v = row.get(key)
        # isfinite: NaN/inf pass isinstance and would poison MIN/MAX sums
        # (the storage choke point scrubs them, but this path also sees
        # pre-scrub dicts from ingest callers).
        if isinstance(v, (int, float)) and math.isfinite(v):
            return float(v)
        return None

    tempf = num("tempf")
    humidity, wind, barom = num("humidity"), num("windspeedmph"), num("baromrelin")
    pm25, co2 = num("pm25"), num("co2")
    return {
        "mac": row.get("_mac"),          # filled by caller
        "day": local.strftime("%Y-%m-%d"),
        "ts": int(ts),
        "_year": local.year,
        "_month": local.month,
        "_hour": local.hour,
        "tempf": tempf,
        "tempf_n": 1 if tempf is not None else 0,
        "humidity": humidity,
        "humidity_n": 1 if humidity is not None else 0,
        "windspeedmph": wind,
        "windspeedmph_n": 1 if wind is not None else 0,
        "windgustmph": num("windgustmph"),
        "baromrelin": barom,
        "baromrelin_n": 1 if barom is not None else 0,
        # Air monitors (2.2): min/max/mean per day, so daily_summary
        # answers for an AirGradient or Govee instead of a row of nulls.
        "pm25": pm25,
        "pm25_n": 1 if pm25 is not None else 0,
        "co2": co2,
        "co2_n": 1 if co2 is not None else 0,
        "tempinf": num("tempinf"),
        "dew_point": num("dewPoint"),
        "feels_like": num("feelsLike"),
        "uv": num("uv"),
        "solarradiation": num("solarradiation"),
        "dailyrainin": num("dailyrainin"),
        "yearlyrainin": num("yearlyrainin"),
        # Trailing-hour strike count; the day's MAX is "most strikes in an
        # hour that day", which is what the records screen quotes.
        "lightning": num("lightning_last_1hr"),
    }


async def update_rollups(db, mac: str, rows: list[dict[str, Any]]) -> None:
    """Fold newly-stored rows into the rollups. Caller passes ONLY rows that
    actually inserted (a re-delivered duplicate must not double-count sums)
    and commits the surrounding transaction."""
    if not settings.insights or not rows:
        return
    tz = _tz()
    for r in rows:
        p = rollup_params(r, tz)
        if p is None:
            continue
        p["mac"] = mac
        year, month, hour = p.pop("_year"), p.pop("_month"), p.pop("_hour")
        await db.execute(_UPSERT_DAILY, p)
        if (hp := _hour_params(mac, month, hour, p)) is not None:
            await db.execute(_UPSERT_HOUR, hp)
        if (cp := _comfort_params(mac, year, month, hour, p)) is not None:
            await db.execute(_UPSERT_COMFORT, cp)


# Serializes rebuild() runs: two concurrent rebuilds interleaving DELETE +
# forward scans would double-fold rows into the ADDITIVE hour_rollups sums
# (tempf_sum/tempf_n), silently corrupting the stored aggregates. Built
# lazily (an asyncio.Lock binds to the first loop that awaits it, and the
# test suite runs asyncio.run() per test); the conftest module reload resets
# it per test — same pattern as main._PUBLIC_DASH_LOCK.
_REBUILD_LOCK: "object | None" = None


async def rebuild(mac: str | None = None) -> dict[str, int]:
    """Recompute rollups from raw history — used when enabling INSIGHTS on
    existing data. Batched scan; bounded memory.

    2.1: the scan folds into staging twins of the rollup tables and one
    short transaction swaps them in at the end (see the staging section
    below), so the live ledger keeps serving throughout and a crashed
    rebuild changes nothing. The swap also folds rows that arrived behind
    the scan cursor, so live ingest during a rebuild is counted exactly
    once. Residual caveat (known, accepted): a row that lands BELOW the
    cursor while the scan runs — a WU import backfilling old days — is
    not in the new ledger; the documented answer is to re-run the rebuild
    after an import that overlapped one. The lock below removes the
    concurrent-rebuild variant of the same corruption."""
    global _REBUILD_LOCK
    import asyncio
    if _REBUILD_LOCK is None:      # no await between test and assignment
        _REBUILD_LOCK = asyncio.Lock()
    async with _REBUILD_LOCK:
        from . import db as dbmod
        # Rebuild-in-progress guard (R11 V6): thin_history refuses while
        # rollups_dirty is set, and that refusal is the ONLY thing keeping
        # the daily retention pass from advancing the thin watermark UNDER
        # this scan — the scan snapshots the watermark once, so concurrent
        # thinning would let it fold bucket-sampled survivors into the very
        # daily rows it just cleared: permanent, silent min/max loss. If the
        # flag is already set (a repair marked it) that refusal is already
        # in force; otherwise set a nonce of our own for the scan's duration.
        # CONDITIONAL acquire, then re-read (R12 W6): a plain get→set had a
        # gap where a repair's own dirty marker landed between the two and
        # got clobbered by the guard nonce — whose success-path clear then
        # removed it, leaving stale rollups behind a clean flag. INSERT OR
        # IGNORE never overwrites; whatever the re-read returns is what the
        # conditional clear below is measured against.
        import time as _time
        nonce = f"rebuild-guard-{_time.time_ns()}"
        async with dbmod.connect() as db:
            await db.execute(
                "INSERT INTO server_kv (k, v) VALUES ('rollups_dirty', ?) "
                "ON CONFLICT(k) DO NOTHING", (nonce,))
            await db.commit()
        pre = await dbmod.get_kv("rollups_dirty")
        we_set_guard = pre == nonce
        out = await _rebuild_locked(mac)
        if pre is not None and (mac is None or we_set_guard):
            # A successful FULL rebuild is the one thing that makes dirty
            # rollups trustworthy again (records() falls back to raw scans
            # while the flag is set — R5-14/R5-15). A single-mac rebuild
            # can't clear a PRE-EXISTING marker (the flag is global and the
            # other stations' ledgers are still stale) — but it always
            # clears its own in-progress guard, which claimed nothing about
            # staleness. Conditional on the exact value so a repair that
            # sets a fresh nonce mid-rebuild survives the clear.
            async with dbmod.connect() as db:
                await db.execute(
                    "DELETE FROM server_kv WHERE k = 'rollups_dirty' "
                    "AND v = ?", (pre,))
                await db.commit()
        return out


async def _rebuild_locked(mac: str | None) -> dict[str, int]:
    from . import db as dbmod
    try:
        return await _rebuild_scan(dbmod, mac)
    except BaseException:
        # 2.1: the scan folds into STAGING tables and the live rollups are
        # replaced in one short transaction at the very end, so a crashed
        # or interrupted rebuild leaves the live ledger exactly as it was:
        # complete, if stale. Before this the scan DELETED the live rows up
        # front and a failure had to clear the half-folded remainder, which
        # left Insights empty until a re-run succeeded. All that can be left
        # behind now is the staging tables; drop them so the next run
        # starts clean (init_db does the same at boot for a rebuild killed
        # with the process). Best-effort: a shutdown may have torn the loop
        # down already.
        try:
            async with dbmod.connect() as db:
                await drop_staging(db)
                await db.commit()
            log.warning("insights rebuild failed mid-scan — live rollups "
                        "untouched, staging dropped (mac=%s); re-run the "
                        "rebuild", mac or "*")
        except Exception:
            log.exception("could not drop the rollup staging tables after "
                          "a failed rebuild")
        finally:
            _set_progress(None)
        raise


async def _thin_watermark_day(dbmod) -> str | None:
    """The thin watermark as a LOCAL day string, or None when history has
    never been thinned. daily_rollups rows for days strictly before this
    were folded from FULL-detail raw that no longer exists — every delete
    of daily_rollups anywhere in this module must be bounded by it."""
    wm_raw = await dbmod.get_kv("history_thin_before_ms")
    wm_ms = int(wm_raw) if wm_raw and str(wm_raw).isdigit() else 0
    if wm_ms <= 0:
        return None
    from datetime import datetime, timezone as _tzu
    return (datetime.fromtimestamp(wm_ms / 1000, tz=_tzu.utc)
            .astimezone(_tz()).strftime("%Y-%m-%d"))


# ───────────────────────── staging tables ─────────────────────────
#
# 2.1: a rebuild folds into twins of the three rollup tables and swaps
# them in at the end. Until then the live tables keep serving whatever
# they held (Insights cards, records, the story engine), and live ingest
# keeps folding into them as usual — those folds are superseded by the
# swap, which also catches up on rows that arrived behind the cursor.
# Before this the rebuild's FIRST statement deleted the live rows, so
# every Insights card on every station vanished for the whole run and
# came back one batch at a time.

ROLLUP_TABLES = ("daily_rollups", "hour_rollups", "comfort_rollups")
STAGING_SUFFIX = "_staging"


def staging_table(table: str) -> str:
    """The staging twin of a rollup table. Whitelist-guarded (a raise, not
    an assert: those vanish under -O) because the name is interpolated."""
    if table not in ROLLUP_TABLES:
        raise ValueError(f"not a rollup table: {table!r}")
    return table + STAGING_SUFFIX


def _upsert_into(sql: str, table: str) -> str:
    """The same upsert aimed at `table`'s staging twin."""
    head = f"\nINSERT INTO {table} ("
    if head not in sql:
        raise ValueError(f"upsert does not target {table}")
    return sql.replace(head, f"\nINSERT INTO {staging_table(table)} (", 1)


_UPSERT_DAILY_STAGING = _upsert_into(_UPSERT_DAILY, "daily_rollups")
_UPSERT_HOUR_STAGING = _upsert_into(_UPSERT_HOUR, "hour_rollups")
_UPSERT_COMFORT_STAGING = _upsert_into(_UPSERT_COMFORT, "comfort_rollups")


async def drop_staging(db) -> None:
    """Remove the staging twins if any exist. Called at the end of every
    rebuild, after a failed one, and by init_db at boot."""
    for t in ROLLUP_TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {staging_table(t)}")


async def _create_staging(db) -> None:
    """Empty twins of the three rollup tables, built from the LIVE tables'
    own DDL in sqlite_master so every ALTERed-in column (lightning_max,
    feels_*) and the primary key the upserts' ON CONFLICT clause needs
    come along. CREATE TABLE ... AS SELECT would drop the key. SQLite
    stores the DDL with IF NOT EXISTS stripped and the name right after
    CREATE TABLE, which is what the prefix check relies on."""
    await drop_staging(db)
    for t in ROLLUP_TABLES:
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (t,))
        row = await cur.fetchone()
        ddl = row[0] if row else None
        head = f"CREATE TABLE {t}"
        if not ddl or not ddl.startswith(head):
            raise RuntimeError(f"rollup table {t} is missing or its DDL is "
                               f"unexpected: {(ddl or '')[:40]!r}")
        await db.execute(f"CREATE TABLE {staging_table(t)}" + ddl[len(head):])


# ───────────────────────── pacing + progress ─────────────────────────
#
# One rebuild batch, and the pause after it. The batch bounds how long the
# write lock is held at a stretch; the PAUSE is what lets anyone else
# have it. Without the pause the loop re-took the lock the instant it
# committed, and on Volney's box (2026-09-02, the comfort ledger's first
# fold over 1,239 days) ingest, push registration and the alert tick all
# answered "database is locked" for the whole rebuild: readings a relay
# did not retry were simply gone. Two things bound that now. The batch
# is 1,000 rows, because each row is three upserts through aiosqlite's
# thread hop and a 5,000-row batch held the lock for ~8 s on Fly's shared
# CPU, close to the 10 s busy_timeout every other writer waits; a 1,000-row
# batch is under 2 s. And the loop sleeps between batches, the same yield
# the 1.9 column backfill uses ("ingest goes first").
#
# 2.1: the pause also SCALES with the batch. A fixed half second is a
# 25% duty cycle when a batch takes 2 s and an 80% one when the shared
# CPU is being stolen and the same batch takes 8 s, which is exactly when
# the other writers most need the lock. The pause is now at least
# REBUILD_IDLE_RATIO times the batch it follows, so the rebuild holds the
# writer for at most 1/(1+ratio) of wall time however slow the box is.
REBUILD_BATCH_ROWS = 1000
REBUILD_BATCH_PAUSE_S = 0.5
REBUILD_IDLE_RATIO = 2.0
# The swap transaction also folds rows that arrived behind the cursor
# while the scan ran. That is minutes of live ingest, tens of rows; the
# cap keeps a concurrent bulk import (old timestamps mostly, but not
# only) from turning the one transaction that must be short into a long
# one. Overflow marks the ledger dirty so records fall back to raw scans
# and the "run the rebuild" hint fires, which is the documented answer to
# an import that overlapped a rebuild anyway.
REBUILD_CATCHUP_MAX = 5000


def rebuild_pause_s(batch_elapsed_s: float) -> float:
    """How long to yield the writer after a batch that took this long."""
    return max(REBUILD_BATCH_PAUSE_S,
               float(batch_elapsed_s) * REBUILD_IDLE_RATIO)


# Where the running rebuild is, for the write-lock watchdog's dump
# (main.dump_all_threads). A thread dump names the FUNCTION that holds
# the lock; this names the phase, the station, the cursor and how long
# the current batch has been running. None when no rebuild is running.
_PROGRESS: dict[str, Any] | None = None


def _set_progress(p: dict[str, Any] | None) -> None:
    global _PROGRESS
    _PROGRESS = p


def in_flight() -> str | None:
    """One line describing the running rebuild, or None."""
    p = _PROGRESS
    if not p:
        return None
    import time as _time
    return ("insights rebuild in flight: phase=%s mac=%s rows=%d "
            "cursor_ms=%s current statement/batch %.1fs"
            % (p.get("phase"), p.get("mac") or "*", p.get("rows", 0),
               p.get("cursor_ms"),
               _time.monotonic() - p.get("batch_started", _time.monotonic())))


# Every column rollup_params reads. Live ingest hands it the whole
# reading; the rebuild hands it THIS list, so a rollup column fed by a
# field missing here fills from today on and never from history (the
# 2.2 air pair, found on the first production rebuild, 2026-09-09).
_SCAN_SELECT = (
    "SELECT mac, dateutc_ms, tempf, humidity, windspeedmph, "
    "windgustmph, baromrelin, dew_point, feels_like, uv, "
    "solarradiation, dailyrainin, yearlyrainin, lightning_last_1hr, "
    "pm25, co2, tempinf "
    "FROM observations WHERE mac = ? AND dateutc_ms > ? "
    "ORDER BY dateutc_ms LIMIT ?")


async def _fold_batch(db, batch, tz, wm_day: str | None) -> int:
    """Fold scanned observation rows into the STAGING tables. Returns how
    many rows folded (rows without a timestamp are skipped)."""
    folded = 0
    for b in batch:
        row = {"dateutc": b[1], "tempf": b[2], "humidity": b[3],
               "windspeedmph": b[4], "windgustmph": b[5],
               "baromrelin": b[6], "dewPoint": b[7],
               "feelsLike": b[8], "uv": b[9],
               "solarradiation": b[10], "dailyrainin": b[11],
               "yearlyrainin": b[12],
               "lightning_last_1hr": b[13],
               "pm25": b[14], "co2": b[15], "tempinf": b[16]}
        p = rollup_params(row, tz)
        if p is None:
            continue
        p["mac"] = b[0]
        year, month, hour = (p.pop("_year"), p.pop("_month"),
                             p.pop("_hour"))
        # Preserved (thinned) days: their daily rows were COPIED into
        # staging before the scan and must not be re-folded — the upsert
        # MERGES (sums add), so folding thinned raw into a full-detail row
        # would corrupt the averages it exists to protect.
        if not (wm_day and p["day"] < wm_day):
            await db.execute(_UPSERT_DAILY_STAGING, p)
        if (hp := _hour_params(b[0], month, hour, p)) is not None:
            await db.execute(_UPSERT_HOUR_STAGING, hp)
        if (cp := _comfort_params(b[0], year, month, hour, p)) is not None:
            await db.execute(_UPSERT_COMFORT_STAGING, cp)
        folded += 1
    return folded


async def _rebuild_scan(dbmod, mac: str | None) -> dict[str, int]:
    import time as _time
    tz = _tz()
    processed = 0
    # History thinning (1.9): days behind the thin watermark keep only
    # bucket-sampled raw, so their rollup rows — folded from FULL detail at
    # insert time — are the surviving source of truth for extremes. A
    # rebuild must PRESERVE those daily rows, never recompute them from
    # thinned raw. Hour rollups are month x hour-of-day AVERAGES; bucket
    # sampling leaves averages unbiased, so they rebuild from whatever raw
    # remains, full-history. Comfort shares survive sampling the same way.
    wm_day = await _thin_watermark_day(dbmod)
    scope = " AND mac = ?" if mac else ""
    scope_args: tuple = (mac,) if mac else ()
    # Highest timestamp folded per station: the swap's catch-up starts here.
    high: dict[str, int] = {}
    progress: dict[str, Any] = {"phase": "staging", "mac": mac, "rows": 0,
                                "cursor_ms": None,
                                "batch_started": _time.monotonic()}
    _set_progress(progress)
    daily_stg = staging_table("daily_rollups")
    hour_stg = staging_table("hour_rollups")
    comfort_stg = staging_table("comfort_rollups")
    async with dbmod.connect() as db:
        await _create_staging(db)
        if wm_day:
            # The rows a rebuild can never recompute ride along untouched.
            await db.execute(
                f"INSERT INTO {daily_stg} SELECT * FROM daily_rollups "
                f"WHERE day < ?{scope}", (wm_day, *scope_args))
        await db.commit()
        if mac:
            macs = [mac]
        else:
            cur = await db.execute("SELECT DISTINCT mac FROM observations")
            macs = [r[0] for r in await cur.fetchall()]
        # Page PER STATION: (mac, dateutc_ms) is the primary key, so within
        # one mac the timestamp cursor is unique and can't split a batch on
        # equal values — the all-macs single cursor skipped cross-station
        # rows sharing a timestamp at a batch boundary.
        progress["phase"] = "fold"
        for one_mac in macs:
            last = -1
            progress["mac"] = one_mac
            while True:
                started = _time.monotonic()
                progress["batch_started"] = started
                cur = await db.execute(_SCAN_SELECT,
                                       (one_mac, last, REBUILD_BATCH_ROWS))
                batch = await cur.fetchall()
                if not batch:
                    break
                processed += await _fold_batch(db, batch, tz, wm_day)
                last = batch[-1][1]
                high[one_mac] = last
                progress["rows"] = processed
                progress["cursor_ms"] = last
                # Commit PER BATCH: one giant transaction held the write
                # lock for the entire multi-minute rebuild and starved every
                # other writer (ingest, config PUTs) into "database is
                # locked" 500s. Staging makes a crashed rebuild harmless:
                # the live tables were never touched.
                await db.commit()
                # Yield the writer: see REBUILD_IDLE_RATIO.
                await asyncio.sleep(
                    rebuild_pause_s(_time.monotonic() - started))
        # ── the swap ──
        # One short transaction: catch up on rows that arrived behind the
        # cursor, replace the live rows with the staging rows, done. A
        # reader sees either the old ledger or the new one, never neither.
        progress.update(phase="swap", mac=mac, batch_started=_time.monotonic())
        await db.execute("BEGIN IMMEDIATE")
        if mac:
            macs_now = [mac]
        else:
            # A station that sent its first reading DURING the scan has
            # live rollups from ingest and nothing in staging; fold it from
            # the start so the swap does not erase it.
            cur = await db.execute("SELECT DISTINCT mac FROM observations")
            macs_now = [r[0] for r in await cur.fetchall()]
        overflow = False
        budget = REBUILD_CATCHUP_MAX
        for one_mac in macs_now:
            cur = await db.execute(_SCAN_SELECT,
                                   (one_mac, high.get(one_mac, -1), budget + 1))
            late = await cur.fetchall()
            if len(late) > budget:
                overflow = True
                late = late[:budget]
            budget -= len(late)
            processed += await _fold_batch(db, late, tz, wm_day)
            if budget <= 0:
                # Budget exactly met is not overflow: only rows still
                # waiting on THIS or a later station make the ledger
                # incomplete (2.1 pre-release review BE-10).
                for m in macs_now[macs_now.index(one_mac):]:
                    cur = await db.execute(
                        _SCAN_SELECT,
                        (m, high.get(m, -1) if m != one_mac else late[-1][1]
                         if late else high.get(m, -1), 1))
                    if await cur.fetchone():
                        overflow = True
                        break
                break
        progress["rows"] = processed
        # daily: bounded by the watermark like every delete in this module.
        # The preserved rows are ALSO in staging (copied above), so the
        # insert ignores the ones the bounded delete left in place.
        if wm_day:
            await db.execute(
                f"DELETE FROM daily_rollups WHERE day >= ?{scope}",
                (wm_day, *scope_args))
        else:
            await db.execute(
                "DELETE FROM daily_rollups" + (" WHERE mac = ?" if mac else ""),
                scope_args)
        await db.execute(
            f"INSERT OR IGNORE INTO daily_rollups SELECT * FROM {daily_stg}")
        for live, stg in (("hour_rollups", hour_stg),
                          ("comfort_rollups", comfort_stg)):
            await db.execute(
                f"DELETE FROM {live}" + (" WHERE mac = ?" if mac else ""),
                scope_args)
            await db.execute(f"INSERT INTO {live} SELECT * FROM {stg}")
        if overflow:
            # More arrived behind the cursor than the swap may fold in one
            # go; the rest is not in the ledger. Mark it so records read
            # raw and the "run the rebuild" hint fires. INSERT OR REPLACE:
            # this must outlive rebuild()'s conditional clear, which only
            # removes the exact nonce it saw before the scan.
            await db.execute(
                "INSERT OR REPLACE INTO server_kv (k, v) VALUES "
                "('rollups_dirty', ?)",
                (f"rebuild-catchup-overflow-{_time.time_ns()}",))
        await db.commit()
        await drop_staging(db)
        await db.commit()
    _set_progress(None)
    if overflow:
        log.warning("insights rebuild: more than %d rows arrived behind "
                    "the cursor during the scan (mac=%s); ledger marked "
                    "dirty — re-run the rebuild", REBUILD_CATCHUP_MAX,
                    mac or "*")
    log.info("insights rebuild: %d rows folded (mac=%s)", processed, mac or "*")
    return {"rows": processed}


# ───────────────────────── assembly ─────────────────────────

def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


async def daily_series(mac: str, days: int) -> dict[str, Any]:
    """Per-day temperature series for one station, newest-last — rollups
    only, so it's cheap enough for the app to call once per station.

    Powers the sensor-drift card: the app fetches this for each visible
    station and compares daily means, so two sensors that disagree — or
    START disagreeing — show up as a diverging line, not a hunch.

    Shape: {"mac": ..., "series": [["2026-08-01", lo, hi, mean], ...]}.
    """
    from . import db as dbmod
    async with dbmod.connect() as db:
        rows = await (await db.execute(
            "SELECT day, tempf_min, tempf_max, tempf_sum, tempf_n "
            "FROM daily_rollups WHERE mac = ? ORDER BY day DESC LIMIT ?",
            (mac, days))).fetchall()

    def clean(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    series: list[list[Any]] = []
    for r in reversed(rows):
        n = r["tempf_n"] or 0
        total = clean(r["tempf_sum"])
        mean = round(total / n, 2) if (n and total is not None) else None
        series.append([r["day"], clean(r["tempf_min"]),
                       clean(r["tempf_max"]), mean])
    return {"mac": mac, "series": series}


async def assemble(mac: str, today: date | None = None) -> dict[str, Any]:
    """The /api/insights payload — reads rollups only.

    `today` overrides the to-date anchor below. Callers that already fixed a
    "today" for themselves (the story engine does, so a story's period and
    its ledger window can't disagree across a midnight boundary) pass theirs;
    everyone else gets the station clock."""
    from . import db as dbmod
    async with dbmod.connect() as db:
        cur = await db.execute(
            "SELECT day, tempf_min, tempf_max, tempf_sum, tempf_n, "
            "rain_total, yearly_min, yearly_max FROM daily_rollups "
            "WHERE mac = ? ORDER BY day", (mac,))
        days = await cur.fetchall()
        cur = await db.execute(
            "SELECT month, hour, tempf_sum, tempf_n, feels_sum, feels_n "
            "FROM hour_rollups WHERE mac = ?", (mac,))
        hours = await cur.fetchall()

    highs = sorted(d[2] for d in days if d[2] is not None)
    p90, p99 = _percentile(highs, 0.90), _percentile(highs, 0.99)
    lows = sorted(d[1] for d in days if d[1] is not None)
    p10_low = _percentile(lows, 0.10)
    # Adaptivity needs a real climatology: a station seeded mid-winter (or
    # five test rows) would otherwise flap onto a cold ladder and flip back
    # months later. Under ~2 months of days, keep the shipped warm ladder.
    cold_tiers = cold_tiers_for(p10_low if len(lows) >= 60 else None)

    def day_rain(d) -> float | None:
        # The one rain rule (day_rain.py). None when the station never
        # measured rain that day: such a day neither wets nor dries a
        # streak, and a station with no gauge at all has no dry streak
        # rather than an ever-growing one (round-two review BE-N1).
        return day_rain_in({"day": d[0], "rain_total": d[5],
                            "yearly_min": d[6], "yearly_max": d[7]})

    # To-date anchor (2.0, story engine): the month-day that splits each
    # year into "the part comparable with the running year" and the rest.
    # Without it every partial-vs-complete year comparison is unfair by
    # construction — eight months of this year against twelve of last is
    # how a station "sets a record" by having a shorter year. Station clock,
    # like every daily_rollups.day; a UTC host is a day ahead of Phoenix for
    # part of every evening.
    anchor_md = (today or datetime.now(_tz()).date()).strftime("%m-%d")

    years: dict[str, dict[str, Any]] = {}
    # Rain gap: days iterate in date order, so the last assignment in the
    # loop below IS the most recent rain day.
    last_rain_day: str | None = None
    last_rain_amount: float | None = None
    # Does this station measure rain? Judged PER YEAR (round-three review
    # BE-F11): a day with no measurement in a year that has some (a source
    # that omits the bucket on a dry day) is a dry day; a year with none
    # (no gauge yet, or a gauge added later) has no total and no series,
    # not a flat zero.
    rain_measured_years = {d[0][:4] for d in days if day_rain(d) is not None}
    for d in days:
        y = d[0][:4]
        yr = years.setdefault(y, {
            "year": int(y), "days": 0, "days_to_date": 0,
            # Coverage (2.0): the year's own span, so a consumer can say
            # "2025 from Jun 3" instead of quoting a total for months the
            # station was not there for. Days arrive in date order, so the
            # first row seen IS the year's first day.
            "first_day": d[0], "last_day": d[0],
            "tiers": {str(int(t)): 0 for t in LEDGER_TIERS},
            "tiers_to_date": {str(int(t)): 0 for t in LEDGER_TIERS},
            "cold": {str(int(t)): 0 for t in cold_tiers},
            # The cold ladder's to-date mirror, for the same reason the heat
            # ladder has one: a running year compared against finished ones
            # is a headline the calendar wrote. It matters MORE on this side
            # — cold lands at both ends of a calendar year, so a year running
            # through August has had one winter while every year beside it
            # has had two halves of two.
            "cold_to_date": {str(int(t)): 0 for t in cold_tiers},
            # Nights at or below freezing. Counted here rather than derived
            # from `cold` because the cold ladder is climate-adaptive and a
            # cold-climate station's tiers skip 32°F entirely.
            "freezes": 0, "freezes_to_date": 0,
            "last_spring_freeze": None, "first_fall_freeze": None,
            "days_p90": 0, "longest_p90_streak": 0, "_streak": 0,
            "hottest": None, "coldest": None,
            "rain_total": 0.0, "rain_series": [],
            "longest_dry_streak": 0, "_dry_streak": 0,
            "nights_p10": 0, "longest_p10_streak": 0, "_cstreak": 0,
            "cdd": 0.0, "hdd": 0.0,
        })
        yr["days"] += 1
        yr["last_day"] = d[0]
        # Feb 29 sorts before Mar 01 as a string, which is the behaviour we
        # want: a leap day is inside the window for every year it compares
        # against, and counted only in the years that have one.
        to_date = d[0][5:] <= anchor_md
        if to_date:
            yr["days_to_date"] += 1
        hi, lo = d[2], d[1]
        if hi is not None:
            for t in LEDGER_TIERS:
                if hi >= t:
                    yr["tiers"][str(int(t))] += 1
                    if to_date:
                        yr["tiers_to_date"][str(int(t))] += 1
            if p90 is not None and hi >= p90:
                yr["days_p90"] += 1
                yr["_streak"] += 1
                yr["longest_p90_streak"] = max(yr["longest_p90_streak"], yr["_streak"])
            else:
                yr["_streak"] = 0
            if yr["hottest"] is None or hi > yr["hottest"][1]:
                yr["hottest"] = (d[0], hi)
        if lo is not None:
            if yr["coldest"] is None or lo < yr["coldest"][1]:
                yr["coldest"] = (d[0], lo)
            for t in cold_tiers:
                if lo <= t:
                    yr["cold"][str(int(t))] += 1
                    if to_date:
                        yr["cold_to_date"][str(int(t))] += 1
            # Cold streak — the P90 heat streak's mirror (same reviewer
            # round): consecutive nights at or below this station's own
            # 10th-percentile low, so it adapts to climate by construction.
            if p10_low is not None and lo <= p10_low:
                yr["nights_p10"] += 1
                yr["_cstreak"] += 1
                yr["longest_p10_streak"] = max(yr["longest_p10_streak"],
                                               yr["_cstreak"])
            else:
                yr["_cstreak"] = 0
            if lo <= FREEZE_F:
                yr["freezes"] += 1
                if to_date:
                    yr["freezes_to_date"] += 1
                # Days arrive in date order, so "last one seen in Jan–Jun"
                # and "first one seen in Jul–Dec" need no extra sorting.
                if d[0][5:7] <= "06":
                    yr["last_spring_freeze"] = d[0]
                elif yr["first_fall_freeze"] is None:
                    yr["first_fall_freeze"] = d[0]
        rain = day_rain(d)
        measured_year = y in rain_measured_years
        if rain is not None:
            yr["rain_total"] += rain
        elif measured_year:
            rain = 0.0
        if measured_year:
            yr["rain_series"].append([d[0], round(yr["rain_total"], 3)])
        # Rain gap. Streaks count consecutive ROLLUP rows (one per day with
        # data), like the p90 streak — a coverage gap doesn't inflate them.
        if rain is None:
            pass
        elif rain >= RAIN_DAY_MIN_IN:
            last_rain_day, last_rain_amount = d[0], round(rain, 3)
            yr["_dry_streak"] = 0
        else:
            yr["_dry_streak"] += 1
            yr["longest_dry_streak"] = max(yr["longest_dry_streak"],
                                           yr["_dry_streak"])
        if hi is not None and lo is not None:
            mean = (hi + lo) / 2
            yr["cdd"] += max(0.0, mean - DEGREE_DAY_BASE_F)
            yr["hdd"] += max(0.0, DEGREE_DAY_BASE_F - mean)

    for yr in years.values():
        yr.pop("_streak", None)
        yr.pop("_dry_streak", None)
        yr.pop("_cstreak", None)
        if str(yr["year"]) not in rain_measured_years:
            yr["longest_dry_streak"] = None
            yr["rain_total"] = None
            yr["rain_series"] = None
        else:
            yr["rain_total"] = round(yr["rain_total"], 3)
        yr["cdd"] = round(yr["cdd"], 1)
        yr["hdd"] = round(yr["hdd"], 1)

    # ── comparability (2.0) ─────────────────────────────────────────────
    # Every year-to-date comparison a client draws — the rain race's "2025
    # by this day", a rank among years, an anomaly against a prior season —
    # is only honest when the years being compared covered the same window.
    # The rule is published rather than left to the client because the
    # client cannot see the coverage: a year with no rain point before
    # today's day-of-year looks identical whether the station measured a dry
    # spring or was still in its box, and defaulting that to 0.00 in is the
    # zero bug this project keeps re-shipping.
    #
    # The signal is POSITIVE-ONLY and deliberately so: `comparable_to_date`
    # is true only when the server has checked and is sure. An older server
    # sends nothing at all, which must read as "not established" — never as
    # "fully covered". A consumer suppresses the baseline claim unless it
    # sees an explicit true.
    ordered = [years[y] for y in sorted(years)]
    reference = ordered[-1] if ordered else None
    reference_days = int(reference["days_to_date"]) if reference else 0
    for yr in ordered:
        window = window_days_to_anchor(int(yr["year"]), anchor_md)
        yr["window_days_to_date"] = window
        # None, not 0.0: a year with no window has no coverage FRACTION,
        # and a zero there would read as "covered none of it".
        yr["coverage_to_date"] = (round(yr["days_to_date"] / window, 4)
                                  if window > 0 else None)
        yr["comparable_to_date"] = comparable_to_date(
            int(yr["days_to_date"]), reference_days)

    # Monthly normals + per-month-year anomalies (warming stripes).
    monthly: dict[str, list[float]] = {}
    per_my: dict[str, list[float]] = {}
    for d in days:
        if d[2] is None:
            continue
        monthly.setdefault(d[0][5:7], []).append(d[2])
        per_my.setdefault(d[0][:7], []).append(d[2])
    # The "normal" here is the STATION'S OWN multi-year mean for the
    # calendar month, so a month covered by one year alone has, by
    # construction, an anomaly of exactly zero. That is not a finding; it
    # is the record being one year deep. `years` says how many years the
    # normal rests on so a reader can tell the two apart (2026-09-06, the
    # first MCP analysis flagged every anomaly reading 0.00). NOAA normals
    # feed the DAILY anomaly path (the heat ledger), not this one.
    normals = {m: round(sum(v) / len(v), 2) for m, v in monthly.items()}
    years_per_month: dict[str, set[str]] = {}
    for my in per_my:
        years_per_month.setdefault(my[5:7], set()).add(my[:4])
    anomalies = [
        {"month": my, "avg_high": round(sum(v) / len(v), 2),
         "anomaly": round(sum(v) / len(v) - normals[my[5:7]], 2),
         "years": len(years_per_month.get(my[5:7], ()))}
        for my, v in sorted(per_my.items())
    ]

    # Days-since-last-rain, in CALENDAR days (unlike the per-year streaks,
    # which count rollup rows): "how long has it been dry" must not shrink
    # because the station was offline for a week of it. 0 = it rained on the
    # newest rollup day; a record with no rain at all spans the whole record.
    # A station that never measured rain has no dry streak: absent is not
    # zero, and "dry for 400 days" on a gauge-less station is a lie.
    dry_streak_days: int | None = None
    if days and rain_measured_years:
        last_day_date = date.fromisoformat(days[-1][0])
        if last_rain_day is not None:
            dry_streak_days = (last_day_date
                               - date.fromisoformat(last_rain_day)).days
        else:
            dry_streak_days = (last_day_date
                               - date.fromisoformat(days[0][0])).days + 1

    grid = [[None] * 24 for _ in range(12)]
    feels_grid = [[None] * 24 for _ in range(12)]
    for month, hour, tsum, tn, fsum, fn in hours:
        if not (1 <= month <= 12 and 0 <= hour <= 23):
            continue
        if tn:
            grid[month - 1][hour] = round(tsum / tn, 1)
        if fn:
            feels_grid[month - 1][hour] = round(fsum / fn, 1)

    return {
        "mac": mac,
        "day_count": len(days),
        "first_day": days[0][0] if days else None,
        "last_day": days[-1][0] if days else None,
        "p90_high": p90, "p99_high": p99,
        "p10_low": p10_low,
        "ledger_tiers": [int(t) for t in LEDGER_TIERS],
        # The month-day every year's `*_to_date` counts stop at (2.0).
        "ledger_anchor": anchor_md,
        # The comparability rule, spelled out so a client can explain its
        # own rendering ("2025 from Jun 3 — not comparable") instead of
        # guessing at a threshold. `reference_year` is the year every
        # `comparable_to_date` flag was measured against.
        "comparison": {
            "anchor": anchor_md,
            "reference_year": int(reference["year"]) if reference else None,
            "reference_days_to_date": reference_days,
            "min_days": COMPARISON_MIN_DAYS,
            "min_coverage": COMPARISON_COVERAGE,
        },
        "cold_tiers": [int(t) for t in cold_tiers],
        # Rain gap (client renders the dry-streak card only when present).
        "last_rain_day": last_rain_day,
        "last_rain_amount": last_rain_amount,
        "dry_streak_days": dry_streak_days,
        "years": ordered,
        "monthly_normals": normals,
        "monthly_normals_source": "station record",
        "monthly_anomalies": anomalies,
        "diurnal_tempf": grid,
        "diurnal_feels": feels_grid,
        # Per-day highs for the calendar heatmap (client renders).
        "calendar": [[d[0], d[2]] for d in days if d[2] is not None],
        # Per-day lows for the heatmap's Low mode (app 1.5+; older apps
        # ignore the extra key).
        "calendar_lo": [[d[0], d[1]] for d in days if d[1] is not None],
    }
