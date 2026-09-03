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

_UPSERT_DAILY = """
INSERT INTO daily_rollups (mac, day,
    tempf_min, tempf_max, tempf_sum, tempf_n,
    humidity_min, humidity_max, windspeedmph_max, windgustmph_max,
    baromrelin_min, baromrelin_max, dew_point_min, dew_point_max,
    feels_like_min, feels_like_max, uv_max, solarradiation_max,
    rain_total, yearly_min, yearly_max, lightning_max)
VALUES (:mac, :day,
    :tempf, :tempf, :tempf, :tempf_n,
    :humidity, :humidity, :windspeedmph, :windgustmph,
    :baromrelin, :baromrelin, :dew_point, :dew_point,
    :feels_like, :feels_like, :uv, :solarradiation,
    :dailyrainin, :yearlyrainin, :yearlyrainin, :lightning)
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
    lightning_max = MAX(COALESCE(lightning_max, :lightning), COALESCE(:lightning, lightning_max))
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
    return {
        "mac": row.get("_mac"),          # filled by caller
        "day": local.strftime("%Y-%m-%d"),
        "_year": local.year,
        "_month": local.month,
        "_hour": local.hour,
        "tempf": tempf,
        "tempf_n": 1 if tempf is not None else 0,
        "humidity": num("humidity"),
        "windspeedmph": num("windspeedmph"),
        "windgustmph": num("windgustmph"),
        "baromrelin": num("baromrelin"),
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

    Residual caveat (known, accepted): the scan commits per batch so live
    ingest keeps working, which means a row ingested (or backfilled by a WU
    import) WHILE a rebuild runs can be folded twice — once by the live
    insert's update_rollups and once by the scan — or missed if it lands
    behind the cursor. Every current assemble() consumer is duplication-
    insensitive (min/max/idempotent; means divide doubled sums by doubled
    counts), and the fix is the documented one: re-run the rebuild after an
    import that overlapped one. The lock below removes the concurrent-
    rebuild variant of the same corruption."""
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
        # A crashed/interrupted rebuild commits per batch, so it would leave
        # a silently TRUNCATED ledger — plausible-looking rollups covering
        # only part of history, with the endpoint's "run rebuild" hint never
        # firing (it needs day_count == 0). Clear the partial tables so the
        # empty-state hint fires instead. Best-effort: a shutdown may have
        # torn the loop down already.
        try:
            # Bounded by the thin watermark, same as the scan's own clear
            # (CodeRabbit, PR #33): for thinned days the rollups are the
            # ONLY remaining record of those days' extremes — an unbounded
            # clear here would destroy what the re-run can never rebuild.
            wm_day = await _thin_watermark_day(dbmod)
            async with dbmod.connect() as db:
                if mac:
                    if wm_day:
                        await db.execute(
                            "DELETE FROM daily_rollups WHERE mac = ? "
                            "AND day >= ?", (mac, wm_day))
                    else:
                        await db.execute(
                            "DELETE FROM daily_rollups WHERE mac = ?", (mac,))
                    await db.execute("DELETE FROM hour_rollups WHERE mac = ?", (mac,))
                    await db.execute("DELETE FROM comfort_rollups WHERE mac = ?", (mac,))
                else:
                    if wm_day:
                        await db.execute(
                            "DELETE FROM daily_rollups WHERE day >= ?",
                            (wm_day,))
                    else:
                        await db.execute("DELETE FROM daily_rollups")
                    await db.execute("DELETE FROM hour_rollups")
                    # A half-folded comfort ledger would rank months from
                    # part of the record with nothing marking it so
                    # (CodeRabbit, PR #35).
                    await db.execute("DELETE FROM comfort_rollups")
                await db.commit()
            log.warning("insights rebuild failed mid-scan — partial rollups "
                        "cleared (mac=%s); re-run the rebuild", mac or "*")
        except Exception:
            log.exception("could not clear partial rollups after a failed rebuild")
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
# the 1.9 column backfill uses ("ingest goes first"). A 360k-row rebuild
# takes a few minutes longer and costs nobody a reading.
REBUILD_BATCH_ROWS = 1000
REBUILD_BATCH_PAUSE_S = 0.5


async def _rebuild_scan(dbmod, mac: str | None) -> dict[str, int]:
    tz = _tz()
    processed = 0
    # History thinning (1.9): days behind the thin watermark keep only
    # bucket-sampled raw, so their rollup rows — folded from FULL detail at
    # insert time — are the surviving source of truth for extremes. A
    # rebuild must PRESERVE those daily rows, never recompute them from
    # thinned raw. Hour rollups are month x hour-of-day AVERAGES; bucket
    # sampling leaves averages unbiased, so they rebuild from whatever raw
    # remains, full-history.
    wm_day = await _thin_watermark_day(dbmod)
    async with dbmod.connect() as db:
        if mac:
            if wm_day:
                await db.execute(
                    "DELETE FROM daily_rollups WHERE mac = ? AND day >= ?",
                    (mac, wm_day))
            else:
                await db.execute("DELETE FROM daily_rollups WHERE mac = ?",
                                 (mac,))
            await db.execute("DELETE FROM hour_rollups WHERE mac = ?", (mac,))
            await db.execute("DELETE FROM comfort_rollups WHERE mac = ?", (mac,))
        else:
            if wm_day:
                await db.execute("DELETE FROM daily_rollups WHERE day >= ?",
                                 (wm_day,))
            else:
                await db.execute("DELETE FROM daily_rollups")
            await db.execute("DELETE FROM hour_rollups")
            # Shares survive bucket sampling like the hour averages do, so
            # the comfort ledger rebuilds full-history too.
            await db.execute("DELETE FROM comfort_rollups")
        if mac:
            macs = [mac]
        else:
            cur = await db.execute("SELECT DISTINCT mac FROM observations")
            macs = [r[0] for r in await cur.fetchall()]
        # Page PER STATION: (mac, dateutc_ms) is the primary key, so within
        # one mac the timestamp cursor is unique and can't split a batch on
        # equal values — the all-macs single cursor skipped cross-station
        # rows sharing a timestamp at a batch boundary.
        for one_mac in macs:
          last = -1
          while True:
            cur = await db.execute(
                "SELECT mac, dateutc_ms, tempf, humidity, windspeedmph, "
                "windgustmph, baromrelin, dew_point, feels_like, uv, "
                "solarradiation, dailyrainin, yearlyrainin, "
                "lightning_last_1hr "
                "FROM observations WHERE mac = ? AND dateutc_ms > ? "
                "ORDER BY dateutc_ms LIMIT ?",
                (one_mac, last, REBUILD_BATCH_ROWS))
            batch = await cur.fetchall()
            if not batch:
                break
            for b in batch:
                row = {"dateutc": b[1], "tempf": b[2], "humidity": b[3],
                       "windspeedmph": b[4], "windgustmph": b[5],
                       "baromrelin": b[6], "dewPoint": b[7],
                       "feelsLike": b[8], "uv": b[9],
                       "solarradiation": b[10], "dailyrainin": b[11],
                       "yearlyrainin": b[12],
                       "lightning_last_1hr": b[13]}
                p = rollup_params(row, tz)
                if p is None:
                    continue
                p["mac"] = b[0]
                year, month, hour = (p.pop("_year"), p.pop("_month"),
                                     p.pop("_hour"))
                # Preserved (thinned) days: their daily rows survived the
                # delete above and must not be re-folded — the upsert MERGES
                # (sums add), so folding thinned raw into a full-detail row
                # would corrupt the averages it exists to protect.
                if not (wm_day and p["day"] < wm_day):
                    await db.execute(_UPSERT_DAILY, p)
                if (hp := _hour_params(b[0], month, hour, p)) is not None:
                    await db.execute(_UPSERT_HOUR, hp)
                if (cp := _comfort_params(b[0], year, month, hour, p)) is not None:
                    await db.execute(_UPSERT_COMFORT, cp)
                processed += 1
            last = batch[-1][1]
            # Commit PER BATCH: one giant transaction held the write lock
            # for the entire multi-minute rebuild and starved every other
            # writer (ingest, config PUTs) into "database is locked" 500s.
            # Batch commits keep each lock hold to a few thousand upserts;
            # a crashed rebuild resumes cleanly since it starts with a
            # DELETE and rebuild is idempotent.
            await db.commit()
            # Yield the writer: see REBUILD_BATCH_PAUSE_S.
            await asyncio.sleep(REBUILD_BATCH_PAUSE_S)
        await db.commit()
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

    def day_rain(d) -> float:
        if d[5] is not None:
            return d[5]
        # Yearly-counter fallback; Jan 1 resets make the delta a lie there.
        if d[6] is not None and d[7] is not None and not d[0].endswith("-01-01"):
            return max(0.0, d[7] - d[6])
        return 0.0

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
        yr["rain_total"] += rain
        yr["rain_series"].append([d[0], round(yr["rain_total"], 3)])
        # Rain gap. Streaks count consecutive ROLLUP rows (one per day with
        # data), like the p90 streak — a coverage gap doesn't inflate them.
        if rain >= RAIN_DAY_MIN_IN:
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
    normals = {m: round(sum(v) / len(v), 2) for m, v in monthly.items()}
    anomalies = [
        {"month": my, "avg_high": round(sum(v) / len(v), 2),
         "anomaly": round(sum(v) / len(v) - normals[my[5:7]], 2)}
        for my, v in sorted(per_my.items())
    ]

    # Days-since-last-rain, in CALENDAR days (unlike the per-year streaks,
    # which count rollup rows): "how long has it been dry" must not shrink
    # because the station was offline for a week of it. 0 = it rained on the
    # newest rollup day; a record with no rain at all spans the whole record.
    dry_streak_days: int | None = None
    if days:
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
        "monthly_anomalies": anomalies,
        "diurnal_tempf": grid,
        "diurnal_feels": feels_grid,
        # Per-day highs for the calendar heatmap (client renders).
        "calendar": [[d[0], d[2]] for d in days if d[2] is not None],
        # Per-day lows for the heatmap's Low mode (app 1.5+; older apps
        # ignore the extra key).
        "calendar_lo": [[d[0], d[1]] for d in days if d[1] is not None],
    }
