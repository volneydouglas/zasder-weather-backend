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

# Cold ledger: days whose LOW reached at or below each tier. The lines are
# the horticultural ones (45 chill / 36 frost likely / 32 freeze / 28 hard
# freeze / 25 severe); the station's own p10-of-lows rides alongside for
# the same Seattle-vs-Chandler reason as the heat ledger's p90.
COLD_TIERS = (45.0, 36.0, 32.0, 28.0, 25.0)
FREEZE_F = 32.0

# Rain gap: a day "rained" when it recorded at least one bucket tip. Fixed
# line, no location-specific threshold — same generalization stance as the
# ledgers (a trace day counts the same in Seattle as in Chandler).
RAIN_DAY_MIN_IN = 0.01

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
"""

_UPSERT_DAILY = """
INSERT INTO daily_rollups (mac, day,
    tempf_min, tempf_max, tempf_sum, tempf_n,
    humidity_min, humidity_max, windspeedmph_max, windgustmph_max,
    baromrelin_min, baromrelin_max, dew_point_min, dew_point_max,
    feels_like_min, feels_like_max, uv_max, solarradiation_max,
    rain_total, yearly_min, yearly_max)
VALUES (:mac, :day,
    :tempf, :tempf, :tempf, :tempf_n,
    :humidity, :humidity, :windspeedmph, :windgustmph,
    :baromrelin, :baromrelin, :dew_point, :dew_point,
    :feels_like, :feels_like, :uv, :solarradiation,
    :dailyrainin, :yearlyrainin, :yearlyrainin)
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
    yearly_max = MAX(COALESCE(yearly_max, :yearlyrainin), COALESCE(:yearlyrainin, yearly_max))
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
        month, hour = p.pop("_month"), p.pop("_hour")
        await db.execute(_UPSERT_DAILY, p)
        if (hp := _hour_params(mac, month, hour, p)) is not None:
            await db.execute(_UPSERT_HOUR, hp)


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
        return await _rebuild_locked(mac)


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
            async with dbmod.connect() as db:
                if mac:
                    await db.execute("DELETE FROM daily_rollups WHERE mac = ?", (mac,))
                    await db.execute("DELETE FROM hour_rollups WHERE mac = ?", (mac,))
                else:
                    await db.execute("DELETE FROM daily_rollups")
                    await db.execute("DELETE FROM hour_rollups")
                await db.commit()
            log.warning("insights rebuild failed mid-scan — partial rollups "
                        "cleared (mac=%s); re-run the rebuild", mac or "*")
        except Exception:
            log.exception("could not clear partial rollups after a failed rebuild")
        raise


async def _rebuild_scan(dbmod, mac: str | None) -> dict[str, int]:
    tz = _tz()
    processed = 0
    async with dbmod.connect() as db:
        if mac:
            await db.execute("DELETE FROM daily_rollups WHERE mac = ?", (mac,))
            await db.execute("DELETE FROM hour_rollups WHERE mac = ?", (mac,))
        else:
            await db.execute("DELETE FROM daily_rollups")
            await db.execute("DELETE FROM hour_rollups")
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
                "solarradiation, dailyrainin, yearlyrainin "
                "FROM observations WHERE mac = ? AND dateutc_ms > ? "
                "ORDER BY dateutc_ms LIMIT 5000",
                (one_mac, last))
            batch = await cur.fetchall()
            if not batch:
                break
            for b in batch:
                row = {"dateutc": b[1], "tempf": b[2], "humidity": b[3],
                       "windspeedmph": b[4], "windgustmph": b[5],
                       "baromrelin": b[6], "dewPoint": b[7],
                       "feelsLike": b[8], "uv": b[9],
                       "solarradiation": b[10], "dailyrainin": b[11],
                       "yearlyrainin": b[12]}
                p = rollup_params(row, tz)
                if p is None:
                    continue
                p["mac"] = b[0]
                month, hour = p.pop("_month"), p.pop("_hour")
                await db.execute(_UPSERT_DAILY, p)
                if (hp := _hour_params(b[0], month, hour, p)) is not None:
                    await db.execute(_UPSERT_HOUR, hp)
                processed += 1
            last = batch[-1][1]
            # Commit PER BATCH: one giant transaction held the write lock
            # for the entire multi-minute rebuild and starved every other
            # writer (ingest, config PUTs) into "database is locked" 500s.
            # Batch commits keep each lock hold to a few thousand upserts;
            # a crashed rebuild resumes cleanly since it starts with a
            # DELETE and rebuild is idempotent.
            await db.commit()
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


async def assemble(mac: str) -> dict[str, Any]:
    """The /api/insights payload — reads rollups only."""
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

    def day_rain(d) -> float:
        if d[5] is not None:
            return d[5]
        # Yearly-counter fallback; Jan 1 resets make the delta a lie there.
        if d[6] is not None and d[7] is not None and not d[0].endswith("-01-01"):
            return max(0.0, d[7] - d[6])
        return 0.0

    years: dict[str, dict[str, Any]] = {}
    # Rain gap: days iterate in date order, so the last assignment in the
    # loop below IS the most recent rain day.
    last_rain_day: str | None = None
    last_rain_amount: float | None = None
    for d in days:
        y = d[0][:4]
        yr = years.setdefault(y, {
            "year": int(y), "days": 0,
            "tiers": {str(int(t)): 0 for t in LEDGER_TIERS},
            "cold": {str(int(t)): 0 for t in COLD_TIERS},
            "last_spring_freeze": None, "first_fall_freeze": None,
            "days_p90": 0, "longest_p90_streak": 0, "_streak": 0,
            "hottest": None, "coldest": None,
            "rain_total": 0.0, "rain_series": [],
            "longest_dry_streak": 0, "_dry_streak": 0,
            "cdd": 0.0, "hdd": 0.0,
        })
        yr["days"] += 1
        hi, lo = d[2], d[1]
        if hi is not None:
            for t in LEDGER_TIERS:
                if hi >= t:
                    yr["tiers"][str(int(t))] += 1
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
            for t in COLD_TIERS:
                if lo <= t:
                    yr["cold"][str(int(t))] += 1
            if lo <= FREEZE_F:
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
        yr["rain_total"] = round(yr["rain_total"], 3)
        yr["cdd"] = round(yr["cdd"], 1)
        yr["hdd"] = round(yr["hdd"], 1)

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
        "cold_tiers": [int(t) for t in COLD_TIERS],
        # Rain gap (client renders the dry-streak card only when present).
        "last_rain_day": last_rain_day,
        "last_rain_amount": last_rain_amount,
        "dry_streak_days": dry_streak_days,
        "years": [years[y] for y in sorted(years)],
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
