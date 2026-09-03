"""One-off + reusable data maintenance.

`clean_cumulative_rain`: some sources (e.g. an SDR posting a sensor's lifetime
cumulative counter) historically wrote a non-resetting value into the daily /
weekly / monthly rain columns, so those "records" are lifetime totals, not real
per-period values. A genuine rollup counter resets to ~0 each period, so a
column whose all-time MIN never drops near 0 is a cumulative artifact. This
nulls those bogus values (both the column and the data_json blob) while leaving
`yearlyrainin` — the monotonic-by-design source the backend derives real
rollups from — untouched.

`repair_yearly_rain_offsets`: after the INGEST_YEARLY_RAIN_OFFSETS feature was
removed (2026-08-11), raw yearly counters started flowing through untouched —
but the history written while an offset was active still carries the
offset-adjusted values. Rollups difference current-vs-historical yearlyrainin,
so every daily/weekly/monthly window straddling the removal reports the offset
itself as rainfall (a 16.03 offset showed as "16 inches of rain today"). This
adds the offset back to pre-cutoff rows so history matches the raw counter,
and nulls rows the offset had clamped to 0 (their true value is unknowable).

Usage on the server (DATABASE_PATH from the environment):
    python -m app.maintenance            # DRY RUN — report only, no changes
    python -m app.maintenance --apply    # back up affected rows, then clean
    python -m app.maintenance repair-yearly-offsets \
        --spec '{"AA:BB:...":{"offset":16.03,"cutoff_ms":1786423350000}}' \
        [--apply]
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .config import settings

log = logging.getLogger("maintenance")

# Rollup columns that SHOULD reset each period. yearlyrainin is intentionally
# excluded — it is monotonic by design and is the SDR's real rain source.
_RESET_COLS = ["dailyrainin", "weeklyrainin", "monthlyrainin"]
# All-time MIN above this ⇒ the counter never resets ⇒ cumulative artifact.
_RESET_FLOOR_IN = 5.0


def _connect(path: str) -> sqlite3.Connection:
    """Maintenance writers share the database file with live ingest; without
    a busy_timeout a briefly-held ingest write lock surfaces as an instant
    "database is locked" instead of a short wait (R11 V9). Same 10s budget
    ingest itself uses."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _analyze(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[float, int]]:
    """{(mac, col): (all_time_min, bogus_row_count)} for non-resetting columns."""
    findings: dict[tuple[str, str], tuple[float, int]] = {}
    macs = [r[0] for r in conn.execute("SELECT DISTINCT mac FROM observations")]
    for mac in macs:
        for col in _RESET_COLS:
            mn, n = conn.execute(
                f"SELECT MIN({col}), COUNT({col}) FROM observations WHERE mac = ?",
                (mac,)).fetchone()
            if mn is not None and n and mn > _RESET_FLOOR_IN:
                bad = conn.execute(
                    f"SELECT COUNT(*) FROM observations WHERE mac = ? AND {col} > ?",
                    (mac, _RESET_FLOOR_IN)).fetchone()[0]
                findings[(mac, col)] = (mn, bad)
    return findings


def clean_cumulative_rain(apply: bool = False, db_path: str | None = None) -> dict:
    """Detect (and, if apply=True, null) cumulative rain-rollup values.

    Returns a summary dict. When applying, affected rows are first dumped to
    `<db>.rainfix-backup-<ts>.json` next to the database so the change is
    recoverable.
    """
    path = db_path or settings.database_path
    conn = _connect(path)
    try:
        findings = _analyze(conn)
        summary = {
            "findings": {f"{m}:{c}": {"min": mn, "bad_rows": bad}
                         for (m, c), (mn, bad) in findings.items()},
            "applied": False,
            "cleaned": 0,
            "backup": None,
        }
        if not findings:
            print("No cumulative rain-rollup columns found. Nothing to clean.")
            return summary

        print("Cumulative (non-resetting) rain columns detected:")
        for (mac, col), (mn, bad) in findings.items():
            print(f"  {mac}  {col}: all-time min={mn:.3f}, {bad} bogus row(s)")

        if not apply:
            print("\nDRY RUN — re-run with --apply to back up + null these values.")
            return summary

        # Back up the affected values first.
        stamp = int(time.time())
        backup = f"{path}.rainfix-backup-{stamp}.json"
        dump = []
        for (mac, col), _ in findings.items():
            for r in conn.execute(
                f"SELECT mac, dateutc_ms, {col} FROM observations "
                f"WHERE mac = ? AND {col} > ?", (mac, _RESET_FLOOR_IN)):
                dump.append({"mac": r[0], "dateutc_ms": r[1],
                             "col": col, "value": r[2]})
        with open(backup, "w") as f:
            json.dump(dump, f)
        print(f"\nBacked up {len(dump)} value(s) to {backup}")

        # Null the column value AND strip it from the data_json blob.
        total = 0
        for (mac, col), _ in findings.items():
            cur = conn.execute(
                f"UPDATE observations "
                f"SET {col} = NULL, data_json = json_remove(data_json, '$.{col}') "
                f"WHERE mac = ? AND {col} > ?", (mac, _RESET_FLOOR_IN))
            total += cur.rowcount or 0
        conn.commit()
        print(f"Cleaned {total} bogus value(s) across {len(findings)} column(s).")
        summary["applied"] = True
        summary["cleaned"] = total
        summary["backup"] = backup
        return summary
    finally:
        conn.close()


def clean_glitch_gusts(apply: bool = False, db_path: str | None = None,
                       min_mph: float = 30.0, max_factor: float = 4.0) -> dict:
    """Null historical spurious wind gusts — a gust above `min_mph` that also
    exceeds `max_factor` × the concurrent sustained wind speed (same rule the
    ingest guard applies going forward). Backs up affected rows first. Applies
    across all devices.
    """
    path = db_path or settings.database_path
    conn = _connect(path)
    try:
        # windspeedmph > 0 mirrors the ingest guard: a calm-station squall front
        # legitimately reads 0 sustained with a real high gust, and `gust > 0 * f`
        # would destructively null every one of them.
        where = ("windgustmph > ? AND windspeedmph IS NOT NULL "
                 "AND windspeedmph > 0 AND windgustmph > windspeedmph * ?")
        params = (min_mph, max_factor)
        n = conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE {where}", params).fetchone()[0]
        summary = {"bad_rows": n, "applied": False, "cleaned": 0, "backup": None}
        if not n:
            print("No glitch wind gusts found. Nothing to clean.")
            return summary
        print(f"Spurious wind gusts detected: {n} row(s) "
              f"(gust > {min_mph} mph and > {max_factor}x sustained).")
        if not apply:
            print("DRY RUN — re-run with --apply to back up + null these gusts.")
            return summary

        # STREAMED, one JSON line per row — accumulating the rows in a Python
        # list first is the exact pattern that OOM-killed
        # repair_yearly_rain_offsets at ~236k rows beside uvicorn on a small
        # Fly machine (see its docstring). Nanosecond stamp for the same
        # two-runs-in-one-second reason as the yearly repair.
        stamp = time.time_ns()
        backup = f"{path}.gustfix-backup-{stamp}.jsonl"
        n_backed = 0
        with open(backup, "w") as f:
            for r in conn.execute(
                    f"SELECT mac, dateutc_ms, windgustmph, windspeedmph "
                    f"FROM observations WHERE {where}", params):
                f.write(json.dumps({"mac": r[0], "dateutc_ms": r[1],
                                    "windgustmph": r[2],
                                    "windspeedmph": r[3]}) + "\n")
                n_backed += 1
        print(f"Backed up {n_backed} gust value(s) to {backup}")

        cur = conn.execute(
            f"UPDATE observations SET windgustmph = NULL, "
            f"data_json = json_remove(data_json, '$.windgustmph') WHERE {where}",
            params)
        conn.commit()
        print(f"Cleaned {cur.rowcount} spurious gust(s).")
        summary.update(applied=True, cleaned=cur.rowcount or 0, backup=backup)
        return summary
    finally:
        conn.close()


def trim_head(mac: str, before_ms: int, apply: bool = False,
              db_path: str | None = None) -> dict:
    """Drop a station's readings from before `before_ms`, then rebuild its
    rollups. For the sensor that spent its first hour indoors.

    Every new outdoor sensor is first powered up on a desk: the Ecowitt
    added 2026-09-01 read 76°F against 89°F outside for its first ninety
    minutes and warmed into the other stations' track from about 1:20 am
    (Volney: "so it doesn't contain indoor temperatures for outdoor
    graphs"). Those rows are honest measurements of the wrong air, and a
    chart, a record and the comfort ledger all read them as weather.

    Same contract as the other repairs: dry run by default, `--apply`
    backs the FULL rows up as one JSON line each beside the database and
    then deletes them in one transaction. The station's rollups are then
    rebuilt for that mac alone (paced, a few seconds for a young station);
    if that rebuild fails the rollups are marked dirty so the boot heal
    does it. `before_ms` is exclusive and in ms since the epoch, UTC."""
    path = db_path or settings.database_path
    conn = _connect(path)
    try:
        where = "mac = ? AND dateutc_ms < ?"
        params = (mac, int(before_ms))
        n, lo, hi = conn.execute(
            f"SELECT COUNT(*), MIN(dateutc_ms), MAX(dateutc_ms) "
            f"FROM observations WHERE {where}", params).fetchone()
        summary = {"rows": n, "applied": False, "removed": 0, "backup": None,
                   "first_ms": lo, "last_ms": hi}
        if not n:
            print(f"No rows for {mac} before {before_ms}. Nothing to trim.")
            return summary
        print(f"{n} row(s) for {mac} from {lo} to {hi} (before {before_ms}).")
        if not apply:
            print("DRY RUN — re-run with --apply to back up + delete them.")
            return summary
        stamp = time.time_ns()
        backup = f"{path}.trimhead-backup-{stamp}.jsonl"
        n_backed = 0
        with open(backup, "w") as f:
            cur = conn.execute(f"SELECT * FROM observations WHERE {where} "
                               f"ORDER BY dateutc_ms", params)
            cols = [d[0] for d in cur.description]
            for r in cur:
                f.write(json.dumps(dict(zip(cols, r))) + "\n")
                n_backed += 1
        print(f"Backed up {n_backed} full row(s) to {backup}")
        cur = conn.execute(f"DELETE FROM observations WHERE {where}", params)
        conn.commit()
        removed = cur.rowcount or 0
        print(f"Removed {removed} row(s).")
        summary.update(applied=True, removed=removed, backup=backup)
    finally:
        conn.close()
    # The rollups for this station folded the removed rows at insert time
    # (minima cannot go back up on their own): refold from what remains.
    try:
        import asyncio
        from . import insights
        stats = asyncio.run(insights.rebuild(mac))
        print(f"Rollups rebuilt for {mac}: {stats['rows']} row(s) folded.")
        summary["rebuilt"] = True
    except Exception as e:  # noqa: BLE001 — a repair must not leave stale rollups silently
        print(f"Per-station rebuild failed ({e}); marking rollups dirty for the boot heal.")
        conn = _connect(path)
        try:
            _mark_rollups_dirty(conn)
            conn.commit()
        finally:
            conn.close()
        summary["rebuilt"] = False
    return summary


def clean_implausible(apply: bool = False, db_path: str | None = None) -> dict:
    """Retro-apply the ingest plausibility bands to already-stored history.

    The bands (`ingest._PLAUSIBLE_BANDS`) run on live ingest and, since
    2026-08-15, on the WU importer too — but the importer had no QC before
    that, so an archive's garbage is already on disk. This nulls every stored
    value that today's bands would have rejected at write time, field by
    field, exactly as `_apply_plausibility_bands` does. Rows and days are
    NEVER deleted: a reading with one bad field keeps its good fields.

    Both the column and the matching `data_json` key are cleared, because
    `/current` composes from `data_json` while records/history read columns —
    clearing only one leaves the two disagreeing.

    Callers must run `insights.rebuild()` afterwards: the daily rollups carry
    their own per-field maxima and do not notice the observation edit.
    """
    from .db import _FIELD_MAP
    from .ingest import _PLAUSIBLE_BANDS

    path = db_path or settings.database_path
    api_to_col = {v: k for k, v in _FIELD_MAP.items()}
    conn = _connect(path)
    try:
        # (column, api_name, lo, hi) for every banded field that is stored.
        targets = [(api_to_col[f], f, lo, hi)
                   for f, (lo, hi) in _PLAUSIBLE_BANDS.items()
                   if f in api_to_col]

        # Column/JSON-key names are interpolated into SQL below, so they get
        # the same whitelist guard the rest of the backend uses. Not an
        # `assert` — those vanish under `python -O`.
        for col, api, _lo, _hi in targets:
            if col not in _FIELD_MAP or _FIELD_MAP[col] != api:
                raise ValueError(
                    f"refusing to interpolate unknown column/key {col!r}/{api!r}")

        counts: dict[str, int] = {}
        for col, _api, lo, hi in targets:
            n = conn.execute(
                f"SELECT COUNT(*) FROM observations "
                f"WHERE {col} IS NOT NULL AND ({col} < ? OR {col} > ?)",
                (lo, hi)).fetchone()[0]
            if n:
                counts[col] = n

        total = sum(counts.values())
        summary = {"bad_values": total, "by_field": counts,
                   "applied": False, "cleaned": 0, "backup": None}
        if not total:
            print("No implausible stored values found. Nothing to clean.")
            return summary

        print(f"Implausible stored values: {total} across {len(counts)} field(s)")
        for col, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lo, hi = next((l, h) for c, _a, l, h in targets if c == col)
            print(f"  {col:<15} {n:>7}  (band {lo:g}..{hi:g})")
        if not apply:
            print("DRY RUN — re-run with --apply to back up + null these values.")
            return summary

        # Streamed one-JSON-line-per-row backup, same rationale as
        # clean_glitch_gusts: materialising the rows OOM-kills a small Fly
        # machine. Nanosecond stamp so two runs in one second cannot collide.
        stamp = time.time_ns()
        backup = f"{path}.bandfix-backup-{stamp}.jsonl"
        n_backed = 0
        with open(backup, "w") as f:
            for col, _api, lo, hi in targets:
                if col not in counts:
                    continue
                for r in conn.execute(
                        f"SELECT mac, dateutc_ms, {col} FROM observations "
                        f"WHERE {col} IS NOT NULL AND ({col} < ? OR {col} > ?)",
                        (lo, hi)):
                    f.write(json.dumps({"mac": r[0], "dateutc_ms": r[1],
                                        "field": col, "value": r[2]}) + "\n")
                    n_backed += 1
        print(f"Backed up {n_backed} value(s) to {backup}")

        # The rows whose anemometer this sweep is about to break up. Collected
        # BEFORE the update, because afterwards the out-of-band value is gone
        # and there is nothing left to identify them by. Bounded by the count
        # printed above (a sensor fault, not a fraction of history).
        anemometer_rows: set[tuple[str, int]] = set()
        for col in _ANEMOMETER_COLUMNS:
            if col not in counts:
                continue
            lo, hi = next((l, h) for c, _a, l, h in targets if c == col)
            for r in conn.execute(
                    f"SELECT mac, dateutc_ms FROM observations "
                    f"WHERE {col} IS NOT NULL AND ({col} < ? OR {col} > ?)",
                    (lo, hi)):
                anemometer_rows.add((r[0], r[1]))

        cleaned = 0
        for col, api, lo, hi in targets:
            if col not in counts:
                continue
            cur = conn.execute(
                f"UPDATE observations SET {col} = NULL, "
                f"data_json = json_remove(data_json, '$.{api}') "
                f"WHERE {col} IS NOT NULL AND ({col} < ? OR {col} > ?)",
                (lo, hi))
            cleaned += cur.rowcount or 0

        # Live ingest condemns the whole anemometer set when the bands reject
        # any one of its channels; this retro path did not, and that asymmetry
        # is exactly how Doren's archive ended up with three "sustained" winds
        # of 51-55 mph sitting on readings whose 255 mph gust had just been
        # swept (his real maximum is 32). Those survivors are in-band, so no
        # amount of re-running the bands would ever have found them, and one
        # of them owned the all-time Peak Wind record for a day.
        orphaned = _condemn_anemometer_rows(conn, anemometer_rows)
        cleaned += orphaned
        _mark_rollups_dirty(conn)
        conn.commit()
        print(f"Cleaned {cleaned} implausible value(s).")
        if orphaned:
            print(f"  (of which {orphaned} were in-band anemometer channels "
                  f"condemned alongside a rejected sibling)")
        print("NOW RUN insights.rebuild() — daily rollups still hold the old maxima.")
        summary.update(applied=True, cleaned=cleaned, backup=backup,
                       anemometer_orphans=orphaned)
        return summary
    finally:
        conn.close()


def _mark_rollups_dirty(conn: sqlite3.Connection) -> None:
    """Repairs invalidate the fold-forward rollups (maxima can't go down),
    and since 1.6 records() serves long periods FROM those rollups — a
    repaired spike would persist as a displayed record indefinitely. This
    flag makes records() fall back to raw scans until a successful FULL
    insights.rebuild() clears it, so the printed "NOW RUN …" instruction is
    a reminder, not the only safeguard (CODE_REVIEW_R5 R5-15 / 1.6-REC
    §1.5). Caller commits.

    The value is a NONCE, not a constant: rebuild() clears the flag only
    when it still holds the value seen at scan start, so a repair landing
    MID-rebuild (its rows already behind the scan cursor) survives the
    clear and the next rebuild picks it up (CodeRabbit, 2026-08-20)."""
    conn.execute(
        "INSERT INTO server_kv (k, v) VALUES ('rollups_dirty', ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (str(time.time_ns()),))
    # History thinning (1.9): the rebuild this flag demands PRESERVES
    # rollup days behind the thin watermark — for those days the rollups
    # hold insert-time values and the thinned raw could not re-derive
    # them anyway. A repair editing rows back there therefore reaches raw
    # (and exports) but never the rollups. Say so once, loudly, instead
    # of letting the operator believe the rebuild reconciled everything
    # (CodeRabbit, PR #33).
    wm = conn.execute("SELECT v FROM server_kv "
                      "WHERE k = 'history_thin_before_ms'").fetchone()
    if wm is not None:
        print("NOTE: history thinning is active. Rollups for days before "
              "the thin watermark keep their insert-time values — repairs "
              "to rows that old apply to raw/exports only, and the rebuild "
              "will not (and must not) recompute those days from thinned "
              "raw.")


# Storage columns for the anemometer's speed channels — the `observations`
# spelling of ingest._ANEMOMETER_FIELDS, which names them API-side.
_ANEMOMETER_COLUMNS = ("windspeedmph", "windgustmph", "maxdailygust")


def _condemn_anemometer_rows(conn: sqlite3.Connection,
                             rows: set[tuple[str, int]]) -> int:
    """Null every surviving anemometer channel on `rows`. Returns the number
    of READINGS touched (each UPDATE counts one row, whatever mix of the
    three wind columns it nulled — R5-29). Caller commits."""
    if not rows:
        return 0
    from .db import _FIELD_MAP

    sets = []
    for col in _ANEMOMETER_COLUMNS:
        # Same whitelist guard as the rest of the module's interpolation, and
        # deliberately not an `assert` (those vanish under `python -O`).
        if col not in _FIELD_MAP:
            raise ValueError(f"refusing to interpolate unknown column {col!r}")
        sets.append(f"{col} = NULL")
    json_keys = ", ".join(f"'$.{_FIELD_MAP[c]}'" for c in _ANEMOMETER_COLUMNS)
    any_left = " OR ".join(f"{c} IS NOT NULL" for c in _ANEMOMETER_COLUMNS)
    sql = (f"UPDATE observations SET {', '.join(sets)}, "
           f"data_json = json_remove(data_json, {json_keys}) "
           f"WHERE mac = ? AND dateutc_ms = ? AND ({any_left})")

    cleared = 0
    for mac, ms in rows:
        cur = conn.execute(sql, (mac, ms))
        cleared += cur.rowcount or 0
    return cleared


def clean_wind_inconsistent(apply: bool = False, db_path: str | None = None,
                            ratio: float | None = None,
                            abs_mph: float | None = None) -> dict:
    """Retro-apply the ingest internal-consistency check to stored history.

    Sustained wind cannot exceed the gust measured over the same window, so a
    reading that claims otherwise is the anemometer contradicting itself and
    the whole speed set goes — the same rule, and the same thresholds, that
    `ingest._apply_wind_consistency` applies at write time. Rows and days are
    never deleted; the reading keeps its temperature, rain and pressure.

    Callers must run `insights.rebuild()` afterwards, for the same reason
    `clean_implausible` says so.
    """
    from .ingest import _WIND_CONSISTENCY_ABS_MPH, _WIND_CONSISTENCY_RATIO

    ratio = _WIND_CONSISTENCY_RATIO if ratio is None else ratio
    abs_mph = _WIND_CONSISTENCY_ABS_MPH if abs_mph is None else abs_mph

    path = db_path or settings.database_path
    conn = _connect(path)
    try:
        where = ("windspeedmph IS NOT NULL AND windgustmph IS NOT NULL "
                 "AND windspeedmph > windgustmph * ? "
                 "AND windspeedmph - windgustmph > ?")
        params = (ratio, abs_mph)
        n = conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE {where}", params).fetchone()[0]
        summary = {"bad_rows": n, "applied": False, "cleaned": 0, "backup": None}
        if not n:
            print("No self-contradicting wind readings found. Nothing to clean.")
            return summary
        print(f"Self-contradicting wind readings: {n} row(s) "
              f"(sustained > {ratio:g}x gust and > {abs_mph:g} mph above it).")
        if not apply:
            print("DRY RUN — re-run with --apply to back up + null these winds.")
            return summary

        # Streamed backup, same rationale as clean_glitch_gusts.
        stamp = time.time_ns()
        backup = f"{path}.windfix-backup-{stamp}.jsonl"
        n_backed = 0
        rows: set[tuple[str, int]] = set()
        with open(backup, "w") as f:
            for r in conn.execute(
                    f"SELECT mac, dateutc_ms, windspeedmph, windgustmph, "
                    f"maxdailygust FROM observations WHERE {where}", params):
                rows.add((r[0], r[1]))
                for field, value in (("windspeedmph", r[2]),
                                     ("windgustmph", r[3]),
                                     ("maxdailygust", r[4])):
                    if value is not None:
                        f.write(json.dumps({"mac": r[0], "dateutc_ms": r[1],
                                            "field": field,
                                            "value": value}) + "\n")
                        n_backed += 1
        print(f"Backed up {n_backed} wind value(s) to {backup}")

        cleaned = _condemn_anemometer_rows(conn, rows)
        _mark_rollups_dirty(conn)
        conn.commit()
        print(f"Cleaned the wind channels on {cleaned} reading(s).")
        print("NOW RUN insights.rebuild() — daily rollups still hold the old maxima.")
        summary.update(applied=True, cleaned=cleaned, backup=backup,
                       rows_touched=len(rows))
        return summary
    finally:
        conn.close()


# How close the repaired pre-cutoff maximum must sit to the first post-cutoff
# raw value. The counter is monotonic, so repaired history may not exceed the
# raw readings that follow it — beyond this tolerance the offset/cutoff pair
# is wrong (or the repair already ran), and the MAC is skipped untouched.
_REPAIR_TOLERANCE_IN = 0.25


def repair_yearly_rain_offsets(spec: dict[str, dict], apply: bool = False,
                               db_path: str | None = None) -> dict:
    """Rewrite pre-cutoff yearlyrainin history to raw-counter values after an
    ingest offset was removed.

    `spec` maps MAC -> {"offset": inches, "cutoff_ms": epoch-ms of the first
    RAW (post-removal) reading}. For rows strictly before the cutoff:
      * value > 0  -> value + offset   (undo the subtraction)
      * value == 0 -> NULL             (the clamp destroyed the true value;
                                        a genuine pre-offset zero is
                                        indistinguishable, and NULL just makes
                                        rollups fall back to the earliest
                                        surviving value)

    Optional per-MAC "null_before_ms": rows before THAT boundary are NULLed
    regardless of value. This handles an earlier era recorded under a
    *different* (unknown) offset — e.g. the first minutes after install,
    before the final offset was configured — whose true values are just as
    unknowable as the clamped zeros. Must be <= cutoff_ms.

    Safety: because the raw counter is monotonic, the repaired pre-cutoff
    maximum must not exceed the minimum post-cutoff raw value (within
    tolerance). A violation — including a second run over already-repaired
    history — skips that MAC untouched. A MAC with no post-cutoff raw rows is
    also skipped: there is nothing to reconcile the repair against.

    When applying, affected rows are streamed to
    `<db>.yearlyfix-backup-<ts>.jsonl` first (JSON Lines, one row per line —
    NOT accumulated in memory: a six-figure row count OOM-killed this next to
    uvicorn on a small Fly machine).
    """
    path = db_path or settings.database_path
    conn = _connect(path)
    try:
        summary: dict = {"macs": {}, "applied": False, "backup": None}
        plans: dict[str, dict] = {}
        for mac, entry in spec.items():
            # A malformed entry (JSON null, a non-numeric string, a
            # non-finite value) must skip THIS mac, not abort the whole
            # run with a traceback mid-repair.
            try:
                offset = float(entry["offset"])
                cutoff_ms = int(entry["cutoff_ms"])
                null_before_ms = int(entry.get("null_before_ms", 0))
            except (KeyError, TypeError, ValueError, OverflowError) as e:
                summary["macs"][mac] = {"skipped": f"malformed spec entry: {e!r}"}
                print(f"  {mac}: SKIPPED — malformed spec entry: {e!r}")
                continue
            # A sign typo in the operator-supplied spec must not LOWER history:
            # the monotonicity check below can't catch a negative offset
            # (repaired values only sink further below post_min). NaN fails
            # this comparison too.
            if not offset > 0:
                summary["macs"][mac] = {"skipped": f"invalid offset {offset!r} "
                                                   f"(must be > 0)"}
                print(f"  {mac}: SKIPPED — invalid offset {offset!r} (must be > 0)")
                continue
            if cutoff_ms <= 0:
                summary["macs"][mac] = {"skipped": f"invalid cutoff_ms {cutoff_ms!r}"}
                print(f"  {mac}: SKIPPED — invalid cutoff_ms {cutoff_ms!r}")
                continue
            if not 0 <= null_before_ms <= cutoff_ms:
                summary["macs"][mac] = {
                    "skipped": f"invalid null_before_ms {null_before_ms!r} "
                               f"(must be within [0, cutoff_ms])"}
                print(f"  {mac}: SKIPPED — invalid null_before_ms {null_before_ms!r}")
                continue
            era0, = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE mac = ? "
                "AND dateutc_ms < ? AND yearlyrainin IS NOT NULL",
                (mac, null_before_ms)).fetchone()
            pos, zero = conn.execute(
                "SELECT COUNT(CASE WHEN yearlyrainin > 0 THEN 1 END), "
                "       COUNT(CASE WHEN yearlyrainin <= 0 THEN 1 END) "
                "FROM observations WHERE mac = ? "
                "AND dateutc_ms >= ? AND dateutc_ms < ? "
                "AND yearlyrainin IS NOT NULL",
                (mac, null_before_ms, cutoff_ms)).fetchone()
            if not pos and not zero and not era0:
                summary["macs"][mac] = {"skipped": "no pre-cutoff rows"}
                print(f"  {mac}: nothing to repair (no pre-cutoff yearlyrainin rows)")
                continue
            # Era-0 rows are excluded here: they get NULLed, not offset, so
            # their (differently-offset) values must not trip the guard.
            pre_max, = conn.execute(
                "SELECT MAX(yearlyrainin) FROM observations "
                "WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms < ? "
                "AND yearlyrainin > 0",
                (mac, null_before_ms, cutoff_ms)).fetchone()
            post_min, = conn.execute(
                "SELECT MIN(yearlyrainin) FROM observations "
                "WHERE mac = ? AND dateutc_ms >= ? AND yearlyrainin IS NOT NULL",
                (mac, cutoff_ms)).fetchone()
            if post_min is None:
                summary["macs"][mac] = {"skipped": "no post-cutoff raw rows"}
                print(f"  {mac}: SKIPPED — no raw rows after the cutoff to "
                      f"reconcile against")
                continue
            if pos and pre_max + offset > post_min + _REPAIR_TOLERANCE_IN:
                summary["macs"][mac] = {
                    "skipped": f"monotonicity check failed: "
                               f"{pre_max:.3f} + {offset:.3f} > {post_min:.3f} "
                               f"+ {_REPAIR_TOLERANCE_IN} (wrong offset/cutoff, "
                               f"or already repaired)"}
                print(f"  {mac}: SKIPPED — repaired max {pre_max + offset:.3f} "
                      f"would exceed first raw value {post_min:.3f} "
                      f"(wrong offset/cutoff, or already repaired)")
                continue
            plans[mac] = {"offset": offset, "cutoff_ms": cutoff_ms,
                          "null_before_ms": null_before_ms}
            summary["macs"][mac] = {"add_offset_rows": pos, "null_rows": zero,
                                    "era0_null_rows": era0}
            print(f"  {mac}: +{offset} on {pos} row(s), NULL {zero} clamped "
                  f"zero row(s) before {cutoff_ms}, NULL {era0} earlier-era "
                  f"row(s) before {null_before_ms}")

        if not plans:
            print("Nothing to repair.")
            return summary
        if not apply:
            print("\nDRY RUN — re-run with --apply to back up + repair.")
            return summary

        # Nanosecond stamp: this backup is the only recovery artifact for a
        # destructive UPDATE, and a second run within the same second (e.g.
        # separate specs for different MACs) must not overwrite the first.
        stamp = time.time_ns()
        backup = f"{path}.yearlyfix-backup-{stamp}.jsonl"
        # STREAMED, one JSON line per row. Accumulating the rows in a Python
        # list first got this process OOM-killed at ~236k rows running beside
        # uvicorn on a small Fly machine — and the kill landed between the
        # plan print and the backup write, i.e. before any UPDATE ran, which
        # is the only acceptable place to die but still a failed repair.
        n_backed = 0
        with open(backup, "w") as f:
            for mac, p in plans.items():
                for r in conn.execute(
                    "SELECT mac, dateutc_ms, yearlyrainin FROM observations "
                    "WHERE mac = ? AND dateutc_ms < ? AND yearlyrainin IS NOT NULL",
                    (mac, p["cutoff_ms"])):
                    f.write(json.dumps({"mac": r[0], "dateutc_ms": r[1],
                                        "yearlyrainin": r[2]}) + "\n")
                    n_backed += 1
        print(f"\nBacked up {n_backed} value(s) to {backup}")

        for mac, p in plans.items():
            conn.execute(
                "UPDATE observations SET yearlyrainin = NULL, "
                "data_json = json_remove(data_json, '$.yearlyrainin') "
                "WHERE mac = ? AND dateutc_ms < ? AND yearlyrainin IS NOT NULL",
                (mac, p["null_before_ms"]))
            conn.execute(
                "UPDATE observations SET yearlyrainin = yearlyrainin + ?, "
                "data_json = json_set(data_json, '$.yearlyrainin', "
                "                     yearlyrainin + ?) "
                "WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms < ? "
                "AND yearlyrainin > 0",
                (p["offset"], p["offset"], mac, p["null_before_ms"],
                 p["cutoff_ms"]))
            conn.execute(
                "UPDATE observations SET yearlyrainin = NULL, "
                "data_json = json_remove(data_json, '$.yearlyrainin') "
                "WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms < ? "
                "AND yearlyrainin <= 0",
                (mac, p["null_before_ms"], p["cutoff_ms"]))
        conn.commit()
        print(f"Repaired {len(plans)} device(s).")
        summary["applied"] = True
        summary["backup"] = backup
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "trim-head":
        import argparse
        ap = argparse.ArgumentParser(prog="app.maintenance trim-head")
        ap.add_argument("--mac", required=True)
        ap.add_argument("--before-ms", type=int, required=True,
                        help="exclusive cutoff, ms since the epoch (UTC)")
        ap.add_argument("--apply", action="store_true")
        ns = ap.parse_args(sys.argv[2:])
        print("== trim head ==")
        result = trim_head(ns.mac, ns.before_ms, apply=ns.apply)
        if ns.apply and not result["applied"]:
            sys.exit(1)
    elif len(sys.argv) > 1 and sys.argv[1] == "repair-yearly-offsets":
        import argparse
        ap = argparse.ArgumentParser(prog="app.maintenance repair-yearly-offsets")
        ap.add_argument("--spec", required=True,
                        help='JSON: {"MAC": {"offset": 16.03, "cutoff_ms": ..., '
                             '"null_before_ms": <optional earlier-era boundary>}}')
        ap.add_argument("--apply", action="store_true")
        ns = ap.parse_args(sys.argv[2:])
        try:
            parsed_spec = json.loads(ns.spec)
        except json.JSONDecodeError as e:
            ap.error(f"--spec is not valid JSON: {e}")
        if not (isinstance(parsed_spec, dict) and parsed_spec and all(
                isinstance(v, dict) and {"offset", "cutoff_ms"} <= v.keys()
                for v in parsed_spec.values())):
            ap.error('--spec must be {"MAC": {"offset": ..., "cutoff_ms": ...}, ...}')
        print("== yearly rain offset repair ==")
        result = repair_yearly_rain_offsets(parsed_spec, apply=ns.apply)
        # Scripted callers must see a failed --apply (every MAC skipped) as a
        # failure, not a silent success. A dry run always exits 0.
        if ns.apply and not result["applied"]:
            sys.exit(1)
    else:
        apply = "--apply" in sys.argv
        print("== cumulative rain ==")
        clean_cumulative_rain(apply=apply)
        print("\n== glitch wind gusts ==")
        clean_glitch_gusts(apply=apply)
        # Part of the default sweep since 1.6-RC2: the changelog advertises
        # this operator tool, but it was reachable only via a REPL import
        # (CODE_REVIEW_R5 R5-30). Same dry-run/--apply contract as the rest.
        print("\n== wind internal consistency ==")
        clean_wind_inconsistent(apply=apply)


# ── history thinning (1.9; nightly bounded batches 2.0) ─────────────────
#
# 2026-09-02, the first real pass over 2.5 years of a 2 GB archive: every
# 3-day chunk's DELETE held SQLite's single writer for MINUTES on Fly's
# disk (the outer range on dateutc_ms alone had no leading index and was
# a full index scan per chunk, and the correlated keep-set probe ran per
# row on top), every ingest 503'd, and each boot restarted the pass two
# minutes in. The redesign, in one sentence: a batch process of smaller
# steps in the off hours over a longer time.
#
# - Every statement is bounded by ROWS, not days: candidates are SELECTed
#   (a read, no write lock) for one (mac, one-day scan window) with a
#   LIMIT, then deleted BY ROWID in one short transaction; the JSON
#   blanking is its own bounded UPDATE. Each transaction is measured; one
#   past _THIN_TXN_BUDGET_S halves the batch (floor _THIN_BATCH_FLOOR),
#   fast ones grow it back slowly.
# - Every query binds `mac` first: the observations indexes all lead with
#   mac, so the scan window is an index range, never a table walk.
# - The watermark only advances past a fully finished scan window, so a
#   night ended by its budget (or a crash) resumes at exactly that edge;
#   re-running a half-done window is idempotent (first-of-bucket stays).
# - The pass runs inside the operator's quiet-hour window (config /
#   /api/history-retention), never at boot, and records `thin_progress`
#   so the app can say "thinning: 6 nights remaining".

_THIN_WATERMARK_KEY = "history_thin_before_ms"
_THIN_PROGRESS_KEY = "thin_progress"
# Per-transaction budget: a DELETE or UPDATE running past this shrinks the
# next batch; one under a quarter of it grows the batch back. One second
# keeps a waiting ingest far inside its 10 s busy_timeout even on a slow
# disk. The yield between transactions hands the lock to a waiting writer
# instead of re-grabbing it at once.
_THIN_TXN_BUDGET_S = 1.0
_THIN_BATCH_FLOOR = 200
_THIN_YIELD_S = 0.25
# Scan window per watermark step (one day). Each batch re-runs the
# candidate query over the window, so it stays small enough that even a
# 1 s station's day (86k index rows) is a few milliseconds of read.
_THIN_WINDOW_MS = 86_400_000
# Rowids per DELETE statement — under the 999 bound-parameter limit of a
# pre-3.32 SQLite; every statement of a batch shares one transaction.
_THIN_IN_CHUNK = 500
# "database is locked" on a batch: back off, retry, then give the night up.
_THIN_LOCK_BACKOFF_S = 30.0
_THIN_LOCK_RETRIES = 3
# Window functions (ROW_NUMBER() OVER ...) arrived in SQLite 3.25.0
# (2018-09). python:3.12-slim ships far newer (Debian bookworm 3.40.1,
# trixie 3.46.1); the probe fallback exists for a self-hoster on an old
# distro Python, and the equivalence test pins that both delete the same
# rows.
_THIN_WINDOW_FN_MIN = (3, 25, 0)
_THIN_USE_WINDOW_FN = sqlite3.sqlite_version_info >= _THIN_WINDOW_FN_MIN

# A row ages out iff an EARLIER row exists in its own (mac, bucket) — i.e.
# it is not the bucket minimum. The correlated EXISTS probes the
# (mac, dateutc_ms) primary key once per row (R11 V2). Kept as the
# window-function fallback and for the dry-run count.
# Parameters: bucket_ms, bucket_ms.
_THIN_NOT_BUCKET_MIN_SQL = (
    "EXISTS (SELECT 1 FROM observations AS k "
    "  WHERE k.mac = o.mac "
    "  AND k.dateutc_ms >= (o.dateutc_ms / ?) * ? "
    "  AND k.dateutc_ms < o.dateutc_ms)")

# Candidate rowids, window-function form: number each bucket's rows in
# time order over ONE mac's scan window (an index range on idx_obs_mac_date),
# keep the first, everything else is a candidate. No per-row probe.
# Parameters: bucket_ms, mac, lo, hi, limit.
_THIN_CANDIDATES_WINDOW_SQL = (
    "SELECT rid FROM ("
    "  SELECT rowid AS rid, "
    "    ROW_NUMBER() OVER (PARTITION BY dateutc_ms / ? "
    "                       ORDER BY dateutc_ms) AS rn "
    "  FROM observations WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms < ?"
    ") WHERE rn > 1 LIMIT ?")

# Candidate rowids, probe form (SQLite < 3.25): the 1.9 predicate, bounded
# the same way. Parameters: mac, lo, hi, bucket_ms, bucket_ms, limit.
_THIN_CANDIDATES_PROBE_SQL = (
    "SELECT o.rowid FROM observations AS o "
    "WHERE o.mac = ? AND o.dateutc_ms >= ? AND o.dateutc_ms < ? "
    "AND " + _THIN_NOT_BUCKET_MIN_SQL + " LIMIT ?")

# JSON-blanking candidates: a read, cursor-driven (dateutc_ms is unique
# within a mac, so `>= cursor` never re-reads a fat row already blanked),
# then the UPDATE by rowid. Parameters: mac, cursor, hi, limit.
_THIN_SLIM_CANDIDATES_SQL = (
    "SELECT rowid, dateutc_ms FROM observations "
    "WHERE mac = ? AND dateutc_ms >= ? AND dateutc_ms < ? "
    "AND LENGTH(data_json) > 2 ORDER BY dateutc_ms LIMIT ?")

# Distinct macs by index skip-scan: O(macs × log n) instead of a walk of
# the whole mac index once a night.
_THIN_MACS_SQL = (
    "WITH RECURSIVE m(mac) AS ("
    "  SELECT MIN(mac) FROM observations "
    "  UNION ALL "
    "  SELECT (SELECT MIN(mac) FROM observations WHERE mac > m.mac) "
    "  FROM m WHERE m.mac IS NOT NULL) "
    "SELECT mac FROM m WHERE mac IS NOT NULL")


class _ThinLocked(Exception):
    """A batch lost the writer to a long lock even after the retries."""


def _txn_took(t0: float) -> float:
    """Seconds since `t0` (monotonic). A seam: the batch-adaptation tests
    script transaction durations through it instead of a fake clock."""
    return time.monotonic() - t0


def _thin_candidate_sql(use_window_fn: bool) -> str:
    return (_THIN_CANDIDATES_WINDOW_SQL if use_window_fn
            else _THIN_CANDIDATES_PROBE_SQL)


def _thin_candidates(conn: sqlite3.Connection, mac: str, lo: int, hi: int,
                     bucket_ms: int, limit: int) -> list[int]:
    if _THIN_USE_WINDOW_FN:
        params = (bucket_ms, mac, lo, hi, limit)
    else:
        params = (mac, lo, hi, bucket_ms, bucket_ms, limit)
    return [r[0] for r in conn.execute(
        _thin_candidate_sql(_THIN_USE_WINDOW_FN), params)]


def _thin_txn(conn: sqlite3.Connection, fn) -> float:
    """Run `fn(conn)` inside one BEGIN IMMEDIATE … COMMIT and return how
    long it held the writer. A "database is locked" backs off
    _THIN_LOCK_BACKOFF_S and retries up to _THIN_LOCK_RETRIES times, then
    raises _ThinLocked so the night ends early instead of queueing behind
    whatever holds the lock."""
    from . import db as _db
    attempt = 0
    while True:
        t0 = time.monotonic()
        try:
            conn.execute("BEGIN IMMEDIATE")
            fn(conn)
            conn.commit()
            return _txn_took(t0)
        except sqlite3.OperationalError as e:
            conn.rollback()
            if not _db.is_lock_error(e):
                raise
            attempt += 1
            if attempt > _THIN_LOCK_RETRIES:
                raise _ThinLocked(str(e)) from e
            log.warning("thinning batch hit '%s' — backing off %.0fs "
                        "(retry %d/%d)", e, _THIN_LOCK_BACKOFF_S, attempt,
                        _THIN_LOCK_RETRIES)
            time.sleep(_THIN_LOCK_BACKOFF_S)


def _adapt_batch(batch: int, took: float, ceiling: int) -> int:
    """Halve on a slow transaction (floor _THIN_BATCH_FLOOR), grow back by
    an eighth of the configured size on a fast one, hold in between."""
    if took > _THIN_TXN_BUDGET_S:
        return max(_THIN_BATCH_FLOOR, batch // 2)
    if took < _THIN_TXN_BUDGET_S / 4 and batch < ceiling:
        return min(ceiling, batch + max(ceiling // 8, _THIN_BATCH_FLOOR))
    return batch


def _stop_reason(deadline: float | None) -> str | None:
    """Why the next batch must not start: the night's budget is spent, or
    the chart-index rebuild holds the writer. None = go ahead."""
    from . import db as _db
    if deadline is not None and time.monotonic() >= deadline:
        return "deadline"
    if _db.chart_index_rebuild_in_progress():
        return "chart_index"
    return None


def _slim_mac_window(conn: sqlite3.Connection, mac: str, lo: int, hi: int,
                     batch: int, ceiling: int,
                     deadline: float | None) -> tuple[int, int, str | None]:
    """Blank data_json for one (mac, window) in bounded batches: a read for
    the rowids (cursor-driven, never re-reading a blanked row), then one
    UPDATE ... WHERE rowid IN (...) transaction, measured and adapted, a
    yield between. Shared by thin_history (step c) and trim_json.
    Returns (rows_slimmed, batch, ended_early)."""
    slimmed = 0
    cursor = lo
    while True:
        why = _stop_reason(deadline)
        if why:
            return slimmed, batch, why
        limit = batch
        found = conn.execute(_THIN_SLIM_CANDIDATES_SQL,
                             (mac, cursor, hi, limit)).fetchall()
        if not found:
            return slimmed, batch, None
        rowids = [r[0] for r in found]

        def run(c):
            nonlocal slimmed
            for i in range(0, len(rowids), _THIN_IN_CHUNK):
                part = rowids[i:i + _THIN_IN_CHUNK]
                cur = c.execute(
                    "UPDATE observations SET data_json = '{}' "
                    "WHERE rowid IN (%s)" % ",".join("?" * len(part)), part)
                slimmed += cur.rowcount or 0
        took = _thin_txn(conn, run)
        cursor = found[-1][1] + 1
        batch = _adapt_batch(batch, took, ceiling)
        time.sleep(_THIN_YIELD_S)
        if len(found) < limit:
            return slimmed, batch, None


def _first_row_at_or_after(conn: sqlite3.Connection, macs: list[str],
                           ms: int) -> int | None:
    """Earliest dateutc_ms >= ms across the macs, one index range per mac
    (a MIN over dateutc_ms alone has no leading index and walks the
    whole archive). None when nothing is that recent."""
    lows = []
    for mac in macs:
        v = conn.execute(
            "SELECT MIN(dateutc_ms) FROM observations "
            "WHERE mac = ? AND dateutc_ms >= ?", (mac, ms)).fetchone()[0]
        if v is not None:
            lows.append(v)
    return min(lows) if lows else None


def _load_progress(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT v FROM server_kv WHERE k = ?",
                       (_THIN_PROGRESS_KEY,)).fetchone()
    if row:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


def _save_progress(conn: sqlite3.Connection, progress: dict) -> None:
    """Inside the caller's transaction — committed with the watermark."""
    conn.execute(
        "INSERT INTO server_kv (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (_THIN_PROGRESS_KEY, json.dumps(progress)))


async def thin_progress() -> dict | None:
    """The stored `thin_progress` document, or None before the first
    night. Shape: started_ms, last_run_ms, rows_deleted_total,
    rows_slimmed_total, window_lo_ms (the watermark, i.e. the resume
    point), window_hi_ms (the cutoff the pass thins toward), done,
    rows_remaining, nights_remaining (None until a night measured a
    rate; 0 once done), last_run_minutes, last_run_rows_deleted,
    ended_early (None | "deadline" | "locked" | "chart_index")."""
    from . import db as _db
    raw = await _db.get_kv(_THIN_PROGRESS_KEY)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def thin_history(apply: bool = False, db_path: str | None = None,
                 detail_days: int | None = None,
                 keep_minutes: int | None = None,
                 now_ms: int | None = None,
                 batch_rows: int | None = None,
                 deadline: float | None = None) -> dict:
    """Age out raw detail: rows older than `detail_days` are thinned to one
    per `keep_minutes` bucket (first-of-bucket) and the kept rows' data_json
    payload is dropped — that JSON is only ever read for a device's LATEST
    rows, so on old rows it is pure dead weight, and it is most of the
    per-row bytes.

    What survives an aged year, by design:
    - one full observation row per bucket (charts bucket coarser than this
      anyway at year zoom);
    - the daily rollups, UNTOUCHED — they were folded from FULL-detail raw
      at insert time and carry every day's true min/max/sums, so records
      and year charts keep their fidelity forever. insights.rebuild()
      preserves rollup days behind the thin watermark for the same reason.

    Guards, both hard errors:
    - INSIGHTS must be on — without rollups, records/summaries would fall
      back to scanning the (now thinned) raw and lose real extremes.
    - rollups_dirty must be clear — dirty rollups get rebuilt FROM RAW, so
      raw must still be full-detail until they're rebuilt.

    Only COMPLETE LOCAL DAYS are thinned (the cutoff aligns down to local
    midnight) and the watermark kv records how far thinning has reached, so
    every consumer can reason about one clean boundary.

    `deadline` (a time.monotonic() instant) is the nightly budget: the pass
    stops before the batch that would cross it and the next night resumes
    from the watermark. None runs to the cutoff (CLI, tests). `batch_rows`
    is the starting/ceiling batch; the loop adapts below it.

    Deleted pages go to SQLite's freelist: the FILE does not shrink, but it
    stops growing — new readings reuse the freed space. Reclaiming the file
    itself needs a VACUUM, which wants free disk equal to the database and
    is deliberately left to the operator.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    detail_days = settings.history_detail_days if detail_days is None else detail_days
    keep_minutes = (settings.history_keep_interval_minutes
                    if keep_minutes is None else keep_minutes)
    ceiling = (settings.history_thin_batch_rows if batch_rows is None
               else batch_rows)
    if detail_days <= 0:
        return {"enabled": False, "applied": False}
    if keep_minutes <= 0:
        raise ValueError("keep_minutes must be positive")
    if ceiling < _THIN_BATCH_FLOOR:
        raise ValueError(f"batch_rows must be at least {_THIN_BATCH_FLOOR}")
    if not settings.insights:
        raise RuntimeError(
            "history thinning needs INSIGHTS=1 — the rollups are what "
            "preserve each day's true extremes once raw detail is thinned")

    path = db_path or settings.database_path
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    bucket_ms = keep_minutes * 60_000

    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    # Cutoff: local midnight at the start of the retention window — only
    # complete local days age out, so the watermark is always a day edge.
    local_cut = datetime.fromtimestamp(
        (now_ms - detail_days * 86_400_000) / 1000, tz)
    cutoff_ms = int(local_cut.replace(hour=0, minute=0, second=0,
                                      microsecond=0).timestamp() * 1000)

    conn = _connect(path)
    conn.row_factory = sqlite3.Row
    try:
        dirty = conn.execute(
            "SELECT v FROM server_kv WHERE k = 'rollups_dirty'").fetchone()
        if dirty is not None:
            raise RuntimeError(
                "rollups are marked dirty — run insights.rebuild() first; "
                "a rebuild recomputes rollups FROM RAW and must see full "
                "detail")
        if _colfill_is_pending(conn):
            raise RuntimeError(
                "the 1.9 column backfill is still running — it reads the "
                "JSON this thin would blank; try again once it finishes")
        row = conn.execute("SELECT v FROM server_kv WHERE k = ?",
                           (_THIN_WATERMARK_KEY,)).fetchone()
        watermark = int(row["v"]) if row and str(row["v"]).isdigit() else 0
        if watermark >= cutoff_ms:
            return {"enabled": True, "applied": False, "cutoff_ms": cutoff_ms,
                    "watermark_ms": watermark, "rows_deleted": 0,
                    "rows_slimmed": 0, "done": True,
                    "note": "already thinned to cutoff"}

        macs = [r[0] for r in conn.execute(_THIN_MACS_SQL)]
        span_lo = _first_row_at_or_after(conn, macs, watermark)
        if span_lo is None or span_lo >= cutoff_ms:
            if apply:
                progress = _load_progress(conn)
                progress.update(window_lo_ms=cutoff_ms, window_hi_ms=cutoff_ms,
                                done=True, rows_remaining=0,
                                nights_remaining=0, last_run_ms=now_ms)
                progress.setdefault("started_ms", now_ms)
                progress.setdefault("rows_deleted_total", 0)
                progress.setdefault("rows_slimmed_total", 0)
                conn.execute(
                    "INSERT INTO server_kv (k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (_THIN_WATERMARK_KEY, str(cutoff_ms)))
                _save_progress(conn, progress)
                conn.commit()
            return {"enabled": True, "applied": apply, "cutoff_ms": cutoff_ms,
                    "watermark_ms": cutoff_ms, "rows_deleted": 0,
                    "rows_slimmed": 0, "done": True,
                    "note": "nothing old enough"}

        if not apply:
            would_del = json_bytes = 0
            for mac in macs:
                would_del += conn.execute(
                    "SELECT COUNT(*) FROM observations AS o "
                    "WHERE o.mac = ? AND o.dateutc_ms >= ? "
                    "AND o.dateutc_ms < ? AND " + _THIN_NOT_BUCKET_MIN_SQL,
                    (mac, span_lo, cutoff_ms, bucket_ms, bucket_ms)
                ).fetchone()[0]
                json_bytes += conn.execute(
                    "SELECT COALESCE(SUM(LENGTH(data_json)), 0) "
                    "FROM observations WHERE mac = ? AND dateutc_ms >= ? "
                    "AND dateutc_ms < ? AND LENGTH(data_json) > 2",
                    (mac, span_lo, cutoff_ms)).fetchone()[0]
            return {"enabled": True, "applied": False, "cutoff_ms": cutoff_ms,
                    "watermark_ms": watermark,
                    "rows_would_delete": would_del,
                    "json_bytes_would_drop": json_bytes}

        # ── the nightly pass ─────────────────────────────────────────
        t_start = time.monotonic()
        progress = _load_progress(conn)
        if progress.get("done") or "started_ms" not in progress:
            # A fresh multi-night pass (first ever, or the cutoff moved on
            # past a finished one): new start stamp, fresh per-pass totals.
            progress = {"started_ms": now_ms, "rows_deleted_total": 0,
                        "rows_slimmed_total": 0}
        # Rows still ahead of the watermark, counted ONCE per night (an
        # index range per mac); the rate this night measures against it
        # gives the nights-remaining estimate.
        rows_remaining = 0
        for mac in macs:
            rows_remaining += conn.execute(
                "SELECT COUNT(*) FROM observations WHERE mac = ? "
                "AND dateutc_ms >= ? AND dateutc_ms < ?",
                (mac, max(watermark, span_lo), cutoff_ms)).fetchone()[0]
        progress.update(window_lo_ms=watermark, window_hi_ms=cutoff_ms,
                        done=False, rows_remaining=rows_remaining,
                        last_run_ms=now_ms)
        progress.pop("ended_early", None)

        deleted = slimmed = covered = 0
        base_deleted = int(progress.get("rows_deleted_total") or 0)
        base_slimmed = int(progress.get("rows_slimmed_total") or 0)
        batch = ceiling
        ended_early: str | None = None
        windows_done = 0
        reached = watermark          # the kv watermark, advanced only by a
        lo = max(watermark, (span_lo // bucket_ms) * bucket_ms)  # committed step

        def _delete_rowids(rowids: list[int]):
            def run(c):
                nonlocal deleted
                for i in range(0, len(rowids), _THIN_IN_CHUNK):
                    part = rowids[i:i + _THIN_IN_CHUNK]
                    cur = c.execute(
                        "DELETE FROM observations WHERE rowid IN (%s)"
                        % ",".join("?" * len(part)), part)
                    deleted += cur.rowcount or 0
            return run

        try:
            while lo < cutoff_ms and ended_early is None:
                hi = min(lo + _THIN_WINDOW_MS, cutoff_ms)
                # Scan from the bucket edge below `lo`: a bucket straddling
                # the previous window's end already kept its first row, and
                # seeing it again is what keeps the rest of that bucket from
                # being counted as "first" here.
                scan_lo = (lo // bucket_ms) * bucket_ms
                window_rows = 0
                for mac in macs:
                    window_rows += conn.execute(
                        "SELECT COUNT(*) FROM observations WHERE mac = ? "
                        "AND dateutc_ms >= ? AND dateutc_ms < ?",
                        (mac, lo, hi)).fetchone()[0]
                    # (a)+(b): candidates by a read, then delete by rowid.
                    while True:
                        ended_early = _stop_reason(deadline)
                        if ended_early:
                            break
                        limit = batch
                        rowids = _thin_candidates(conn, mac, scan_lo, hi,
                                                  bucket_ms, limit)
                        if not rowids:
                            break
                        took = _thin_txn(conn, _delete_rowids(rowids))
                        batch = _adapt_batch(batch, took, ceiling)
                        time.sleep(_THIN_YIELD_S)
                        if len(rowids) < limit:
                            break
                    if ended_early:
                        break
                    # (c): blank data_json, same shape — a read for the
                    # rowids, then one bounded UPDATE (shared with trim).
                    n, batch, ended_early = _slim_mac_window(
                        conn, mac, lo, hi, batch, ceiling, deadline)
                    slimmed += n
                    if ended_early:
                        break
                if ended_early:
                    break
                # (e): the window is fully done — advance the watermark, and
                # the progress with it, in one write. The next window starts
                # at the next ROW, not the next day: an empty span (a gap in
                # the archive, or a sparse station) is skipped in one probe
                # instead of a yield per empty day; with nothing left before
                # the cutoff the same write marks the pass done.
                covered += window_rows
                nxt = _first_row_at_or_after(conn, macs, hi)
                mark = cutoff_ms if nxt is None or nxt >= cutoff_ms else hi
                progress.update(window_lo_ms=mark,
                                rows_deleted_total=base_deleted + deleted,
                                rows_slimmed_total=base_slimmed + slimmed)

                def _advance(c, mark=mark):
                    c.execute(
                        "INSERT INTO server_kv (k, v) VALUES (?, ?) "
                        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                        (_THIN_WATERMARK_KEY, str(mark)))
                    _save_progress(c, progress)
                _thin_txn(conn, _advance)
                reached = mark
                windows_done += 1
                lo = mark if mark == cutoff_ms else nxt
                if lo < cutoff_ms:
                    time.sleep(_THIN_YIELD_S)
        except _ThinLocked as e:
            ended_early = "locked"
            progress["window_lo_ms"] = reached    # an advance that never landed
            log.warning("thinning: the database stayed locked through %d "
                        "retries (%s) — ending tonight's run early; the "
                        "next night resumes from the watermark",
                        _THIN_LOCK_RETRIES, e)

        done = reached >= cutoff_ms
        progress.update(rows_deleted_total=base_deleted + deleted,
                        rows_slimmed_total=base_slimmed + slimmed)
        minutes = max((time.monotonic() - t_start) / 60.0, 1e-6)
        rows_left = max(rows_remaining - covered, 0)
        nights: int | None
        if done:
            nights = 0
        elif deadline is not None and covered > 0:
            budget_min = max((deadline - t_start) / 60.0, 1e-6)
            per_night = covered / minutes * budget_min
            nights = max(1, math.ceil(rows_left / per_night))
        else:
            nights = None
        progress.update(done=done, rows_remaining=rows_left,
                        nights_remaining=nights,
                        last_run_minutes=round(minutes, 1),
                        last_run_rows_deleted=deleted,
                        ended_early=ended_early)
        try:
            _thin_txn(conn, lambda c: _save_progress(c, progress))
        except _ThinLocked:
            log.warning("thinning: could not record tonight's progress "
                        "(database locked); the watermark is already saved")

        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return {"enabled": True, "applied": True, "cutoff_ms": cutoff_ms,
                "watermark_ms": reached, "rows_deleted": deleted,
                "rows_slimmed": slimmed, "done": done,
                "windows": windows_done, "ended_early": ended_early,
                "nights_remaining": nights,
                "rows_remaining": rows_left, "rows_covered": covered,
                "minutes": minutes,
                "reusable_bytes": free_pages * page_size}
    finally:
        conn.close()


# ── the quiet-hour window (2.0) ─────────────────────────────────────────

def parse_thin_window_start(value: str) -> tuple[int, int]:
    """'HH:MM' → (hour, minute); ValueError otherwise."""
    parts = str(value).strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"window start {value!r} is not HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"window start {value!r} is not a time of day")
    return h, m


def next_thin_window_start(now_local, start_hhmm: str):
    """The next window start STRICTLY after `now_local` (station-local,
    tz-aware): today's if it is still ahead, else tomorrow's. Strictly
    after, so a boot inside tonight's window waits for tomorrow's — the
    pass never starts at boot (2026-09-02: every restart resumed the
    heavy pass two minutes in)."""
    from datetime import timedelta
    h, m = parse_thin_window_start(start_hhmm)
    candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def seconds_until_thin_window(start_hhmm: str, tz=None) -> float:
    """Seconds from now (station-local clock via db._now_local) to the next
    window start. Always > 0."""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    from . import db as _db
    if tz is None:
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            tz = timezone.utc
    now_local = _db._now_local(tz)
    nxt = next_thin_window_start(now_local, start_hhmm)
    return max((nxt - now_local).total_seconds(), 1.0)


def run_thin_night(eff: dict, db_path: str | None = None) -> dict:
    """One night of history aging inside the budget from `eff` (the
    effective_retention() document): the worker-thread entry the
    scheduler calls at the window start. Row thinning first (the
    destructive, bigger win), then the JSON trim with whatever minutes
    remain — ONE deadline for both, one progress document, one summary
    line. 0 minutes = off; both retention knobs 0 = off."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    minutes = int(eff.get("thin_window_minutes") or 0)
    detail_days = int(eff.get("detail_days") or 0)
    json_days = int(eff.get("json_days") or 0)
    if minutes <= 0 or (detail_days <= 0 and json_days <= 0):
        return {"enabled": False, "applied": False}
    batch_rows = eff.get("thin_batch_rows")
    t0 = time.monotonic()
    deadline = t0 + minutes * 60.0
    thin = trim = None
    if detail_days > 0:
        thin = thin_history(apply=True, db_path=db_path,
                            detail_days=detail_days, batch_rows=batch_rows,
                            deadline=deadline)
    if json_days > 0:
        trim = trim_json(apply=True, db_path=db_path, json_days=json_days,
                         batch_rows=batch_rows, deadline=deadline)

    # Combined estimate: minutes each job still needs at the rate it
    # measured tonight (a job that never got a turn borrows the other's
    # rate), over the nightly budget.
    def rate(job):
        if job and job.get("applied") and job.get("rows_covered", 0) > 0:
            return job["rows_covered"] / max(job.get("minutes", 0), 1e-6)
        return None
    thin_rate, trim_rate = rate(thin), rate(trim)
    left = [(j.get("rows_remaining", 0) if j and j.get("applied") else 0, r)
            for j, r in ((thin, thin_rate or trim_rate),
                         (trim, trim_rate or thin_rate))]
    thin_done = thin is None or bool(thin.get("done"))
    trim_done = trim is None or bool(trim.get("done"))
    done = thin_done and trim_done
    nights: int | None
    if done:
        nights = 0
    elif any(n > 0 and r is None for n, r in left):
        nights = None
    else:
        need_min = sum(n / r for n, r in left if n > 0 and r)
        nights = max(1, math.ceil(need_min / minutes))

    deleted = (thin or {}).get("rows_deleted", 0)
    slimmed = (thin or {}).get("rows_slimmed", 0) + (trim or {}).get("rows_slimmed", 0)
    elapsed = (time.monotonic() - t0) / 60.0
    conn = _connect(db_path or settings.database_path)
    try:
        progress = _load_progress(conn)
        progress.update(done=done, thin_done=thin_done, trim_done=trim_done,
                        nights_remaining=nights,
                        last_run_minutes=round(elapsed, 1),
                        last_run_rows_deleted=deleted,
                        last_run_rows_slimmed=slimmed)
        try:
            _thin_txn(conn, lambda c: _save_progress(c, progress))
        except _ThinLocked:
            log.warning("thinning: could not record tonight's progress "
                        "(database locked); the watermarks are already saved")
    finally:
        conn.close()

    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc

    def day(ms):
        return datetime.fromtimestamp(ms / 1000, tz).date().isoformat()
    marks = []
    if thin and thin.get("applied") is not None and "watermark_ms" in thin:
        marks.append(f"watermark at {day(thin['watermark_ms'])}")
    if trim and "watermark_ms" in trim:
        marks.append(f"trim at {day(trim['watermark_ms'])}")
    if done:
        tail = "done to the cutoff"
    elif nights is None:
        tail = "nights to go: unknown"
    else:
        tail = f"~{nights} night{'s' if nights != 1 else ''} to go"
    early = {j.get("ended_early") for j in (thin, trim) if j} - {None, "deadline"}
    if early:
        tail += " (ended early: %s)" % ", ".join(sorted(early))
    log.info("thinning: %s rows deleted, %s slimmed in %.0f min, %s, %s",
             f"{deleted:,}", f"{slimmed:,}", elapsed,
             ", ".join(marks) or "no watermark", tail)
    return {"enabled": True, "applied": True, "thin": thin, "trim": trim,
            "rows_deleted": deleted, "rows_slimmed": slimmed,
            "done": done, "nights_remaining": nights}


# Wall-clock budget for one full storage measurement. This no longer has
# to fit a request: /api/storage runs the scan as a background job and
# serves the cached report (2.0, 2026-09-01, second attempt — see
# storage_breakdown). 15 minutes is a safety net against a pathological
# statement, not a target; the progress handler stops the scan cleanly at
# the deadline and the report says `partial: true`.
_STORAGE_BUDGET_S = 15 * 60.0
# The last full report, as JSON, so a restart or a second replica answers
# from the cache instead of re-walking 2 GB. `measured_ms` rides inside.
_STORAGE_CACHE_KEY = "storage.breakdown"
# VM instructions between progress-handler calls. Small enough that a
# runaway statement is caught within milliseconds; a test sets it to 1.
_STORAGE_PROGRESS_EVERY = 10_000

_DBSTAT_AGGREGATE_SQL = ("SELECT name, SUM(pgsize) AS b FROM dbstat "
                         "WHERE aggregate = TRUE GROUP BY name")
_DBSTAT_CELL_SQL = "SELECT name, SUM(pgsize) AS b FROM dbstat GROUP BY name"


class _OutOfTime(Exception):
    """The storage scan's budget ran out; unwind and answer partial."""


def storage_skeleton(db_path: str | None = None) -> dict:
    """The cheap part of the storage report — three stat() calls — plus
    every scan-derived key at its "not measured" value. Every /api/storage
    answer is at least this, including "measuring" before the first full
    scan lands: the app decodes `db_bytes` as a required field, the file
    sizes are the number most people want, and a key that is sometimes
    absent is the shape of bug the client decoder catches last."""
    import os as _os

    path = db_path or settings.database_path
    out: dict = {"database_path": path}
    for label, p in (("db_bytes", path), ("wal_bytes", path + "-wal"),
                     ("shm_bytes", path + "-shm")):
        try:
            out[label] = _os.stat(p).st_size
        except OSError:
            out[label] = 0
    out.update(page_size=None, freelist_bytes=None, dbstat_mode=None,
               tables=[], observations=None, thinning=None, partial=False)
    return out


def storage_cache_get(db_path: str | None = None) -> dict | None:
    """The last full report storage_breakdown cached, or None when there
    is none (or it doesn't parse — a hand-edited kv row must not 500 the
    route). Sync sqlite: the route calls it via to_thread, the job from
    its own thread. A missing server_kv table (a bare file handed to the
    unit tests) reads as "no cache"."""
    path = db_path or settings.database_path
    try:
        conn = _connect(path)
        try:
            row = conn.execute("SELECT v FROM server_kv WHERE k = ?",
                               (_STORAGE_CACHE_KEY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        report = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if (not isinstance(report, dict)
            or not isinstance(report.get("measured_ms"), int)):
        return None
    return report


def storage_cache_put(report: dict, db_path: str | None = None,
                      now_ms: int | None = None) -> dict:
    """Stamp `measured_ms` and store the report under the kv key. Returns
    the stamped dict (the same object) so the job can hand it straight to
    the next GET."""
    path = db_path or settings.database_path
    report["measured_ms"] = (int(time.time() * 1000) if now_ms is None
                             else int(now_ms))
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT INTO server_kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (_STORAGE_CACHE_KEY, json.dumps(report)))
        conn.commit()
    finally:
        conn.close()
    return report


def storage_breakdown(db_path: str | None = None,
                      detail_days: int | None = None,
                      keep_minutes: int | None = None,
                      budget_s: float | None = None) -> dict:
    """Where the database's bytes actually live (1.9): per-table sizes when
    the sqlite build exposes dbstat, per-table row counts always, and the
    observations-specific split that matters for thinning decisions — how
    much is data_json payload, and how much of everything is older than
    the retention window. Read-only.

    Not a request-time function (2.0, 2026-09-01, second attempt). On
    Volney's 2.07 GB box this hung past Fly's 60 s proxy and the app showed
    "Couldn't measure storage". The first fix asked dbstat for its
    aggregated mode (`WHERE aggregate = TRUE`, SQLite >= 3.32; page
    headers instead of every cell) under a 25 s progress-handler budget
    — and STILL exceeded 60 s live. In aggregate mode dbstat returns one
    row per b-tree and walks the whole b-tree inside a single xNext call
    in C, so the progress handler never fires during the observations
    walk; and the `SUM(LENGTH(data_json))` split is a full 2 GB read
    regardless. No per-request budget bounds that. So /api/storage now
    runs this as a background job and serves the last report from the kv
    cache (storage_cache_get / storage_cache_put); the budget here is a
    15-minute safety net, and a report cut short says `partial: true`
    with nulls for what was never measured.
    """
    path = db_path or settings.database_path
    # Every key a client reads is present even when the scan is cut short.
    out: dict = storage_skeleton(path)

    # Resolved (app-over-env) values when the caller passed them — the
    # /api/storage report must describe the retention policy actually
    # APPLIED, not the env fallback (CodeRabbit, PR #33).
    eff_detail = (settings.history_detail_days
                  if detail_days is None else detail_days)
    eff_keep = (settings.history_keep_interval_minutes
                if keep_minutes is None else keep_minutes)
    cutoff_days = eff_detail or 365
    cutoff_ms = int(time.time() * 1000) - cutoff_days * 86_400_000

    budget = _STORAGE_BUDGET_S if budget_s is None else budget_s
    deadline = time.monotonic() + budget
    aborted = False

    def _tick() -> int:
        nonlocal aborted
        if time.monotonic() >= deadline:
            aborted = True
            return 1            # non-zero ⇒ sqlite raises "interrupted"
        return 0

    def _q(sql: str, params: tuple = ()) -> list:
        """Run one statement under the budget. An interrupt from the
        handler becomes _OutOfTime; any other sqlite error propagates
        for the caller to classify."""
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            if aborted:
                raise _OutOfTime()
            raise

    # URI-quote the path (R11): a DATABASE_PATH containing '?' or '#'
    # would silently truncate at the reserved character. Operator-
    # controlled, but the quote costs nothing.
    from urllib.parse import quote
    conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(_tick, _STORAGE_PROGRESS_EVERY)
    tables: list[str] = []
    idx_of: dict[str, str] = {}
    sizes: dict[str, int] | None = None
    table_rows: list[dict] = []
    counted: dict[str, int] = {}
    try:
        try:
            page_size = _q("PRAGMA page_size")[0][0]
            out["page_size"] = page_size
            out["freelist_bytes"] = (
                _q("PRAGMA freelist_count")[0][0] * page_size)

            for r in _q("SELECT name, type, tbl_name FROM sqlite_master "
                        "WHERE type IN ('table', 'index') "
                        "AND name NOT LIKE 'sqlite_%'"):
                if r["type"] == "table":
                    tables.append(r["name"])
                else:
                    idx_of[r["name"]] = r["tbl_name"]
            tables.sort()

            # Cheap and useful first: the thinning state is one kv read.
            wm = _q("SELECT v FROM server_kv WHERE k = ?",
                    (_THIN_WATERMARK_KEY,))
            wm_v = wm[0]["v"] if wm else None
            out["thinning"] = {
                "enabled": eff_detail > 0,
                "detail_days": eff_detail,
                "keep_interval_minutes": eff_keep,
                "watermark_ms": (int(wm_v) if wm_v is not None
                                 and str(wm_v).isdigit() else None),
            }

            # Bytes per table+its indexes, when this sqlite has the dbstat
            # virtual table. Aggregated mode first (page headers only);
            # per-cell mode on an older sqlite; sizes stay null without
            # dbstat at all, and row counts still tell most of the story.
            try:
                sizes = {r["name"]: r["b"] for r in _q(_DBSTAT_AGGREGATE_SQL)}
                out["dbstat_mode"] = "aggregate"
            except sqlite3.Error:          # _OutOfTime is not one; it unwinds
                try:
                    sizes = {r["name"]: r["b"] for r in _q(_DBSTAT_CELL_SQL)}
                    out["dbstat_mode"] = "cell"
                except sqlite3.Error:
                    sizes = None

            for t in tables:
                # t comes from sqlite_master, not an internal whitelist —
                # the repo's SQL-interpolation rule applies. Doubling
                # embedded quotes makes the identifier quoting
                # injection-proof.
                quoted = '"' + t.replace('"', '""') + '"'
                counted[t] = _q(f"SELECT COUNT(*) FROM {quoted}")[0][0]

            obs = _q(
                "SELECT COUNT(*) AS rows_total, "
                "COALESCE(SUM(LENGTH(data_json)), 0) AS json_bytes, "
                "SUM(dateutc_ms < ?) AS rows_old, "
                "COALESCE(SUM(CASE WHEN dateutc_ms < ? "
                "  THEN LENGTH(data_json) END), 0) AS json_bytes_old "
                "FROM observations", (cutoff_ms, cutoff_ms))[0]
            per_mac = [{"mac": r["mac"], "rows": r["n"]} for r in _q(
                "SELECT mac, COUNT(*) AS n FROM observations "
                "GROUP BY mac ORDER BY n DESC")]
            out["observations"] = {
                "rows": obs["rows_total"],
                "data_json_bytes": obs["json_bytes"],
                "older_than_days": cutoff_days,
                "rows_older": obs["rows_old"] or 0,
                "data_json_bytes_older": obs["json_bytes_old"],
                "per_station": per_mac,
            }
        except _OutOfTime:
            out["partial"] = True
            log.warning("storage breakdown cut short after %.0fs budget "
                        "(%s); answering with what was measured",
                        budget, path)
        finally:
            conn.set_progress_handler(None, 0)
    finally:
        conn.close()

    for t in tables:
        b = sizes.get(t) if sizes is not None else None
        ib = (sum(v for k, v in sizes.items() if idx_of.get(k) == t)
              if sizes is not None else None)
        table_rows.append({"table": t, "rows": counted.get(t),
                           "bytes": b, "index_bytes": ib})
    table_rows.sort(key=lambda d: -(d["bytes"] or 0))
    out["tables"] = table_rows
    return out


# ── Orphaned database snapshots (2.0) ───────────────────────────────────

_SNAPSHOT_GLOBS = (".dbbackup-*.db", ".dbbackup-*.db-journal")


def sweep_stale_snapshots(now_ms: int, keep: Path | None = None, *,
                          max_age_ms: int | None = None,
                          db_path: str | None = None) -> dict:
    """Delete leftover `.dbbackup-*.db` snapshots (and their `-journal`
    sidecars) next to the database and in the tempdir.

    The backup job VACUUMs into one of these and only the FileResponse's
    background task deleted it — after a SUCCESSFUL download. A client
    that timed out, an app closed mid-poll, a snapshot nobody fetched in
    the fresh window, a restart mid-VACUUM: each left a database-sized
    file forever. 2026-09-01, Volney's Fly box: three of them (5.3 GB)
    plus two journals beside a 2.07 GB live database on a 10.5 GB volume,
    and the next backup refused with 507.

    `keep`: the current job's file, never touched (with its journal).
    `max_age_ms`: only files whose mtime is older than this go; None
    means any age (boot — nothing can be in progress in this process).
    Every deletion is logged with its size. Returns
    {'deleted': [paths], 'bytes_freed': int}."""
    src = Path(db_path or settings.database_path)
    keep_set: set[Path] = set()
    if keep is not None:
        kp = Path(keep)
        keep_set = {kp, Path(str(kp) + "-journal")}
    dirs: list[Path] = []
    for d in (src.parent, Path(tempfile.gettempdir())):
        if d not in dirs:
            dirs.append(d)
    deleted: list[str] = []
    freed = 0
    for d in dirs:
        for pattern in _SNAPSHOT_GLOBS:
            try:
                found = sorted(d.glob(pattern))
            except OSError:
                continue
            for p in found:
                if p in keep_set:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                age_ms = now_ms - int(st.st_mtime * 1000)
                if max_age_ms is not None and age_ms < max_age_ms:
                    continue
                try:
                    p.unlink()
                except OSError as e:
                    log.warning("could not remove stale snapshot %s: %s", p, e)
                    continue
                freed += st.st_size
                deleted.append(str(p))
                log.info("removed orphaned database snapshot %s "
                         "(%d MB, %.1f h old)", p, st.st_size // 2**20,
                         max(age_ms, 0) / 3_600_000)
    return {"deleted": deleted, "bytes_freed": freed}


# ── data_json trimming + effective retention (1.9) ──────────────────────

_JSON_WATERMARK_KEY = "history_json_before_ms"
_RETENTION_KV_KEY = "history_retention"


async def effective_retention() -> dict:
    """The knobs the nightly pass actually runs with: app-stored values
    (set from Settings, kept in server_kv) win over env, the alert-prefs
    precedence rule. Returns detail_days / json_days plus the 2.0 window
    knobs thin_window_start ('HH:MM'), thin_window_minutes (0 = paused)
    and thin_batch_rows, each with a `*_source` of 'app' or 'env'."""
    from . import db
    stored: dict = {}
    raw = await db.get_kv(_RETENTION_KV_KEY)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                stored = parsed
        except ValueError:
            pass
    out: dict = {}
    for key, env_val in (("detail_days", settings.history_detail_days),
                         ("json_days", settings.history_json_detail_days)):
        v = stored.get(key)
        if isinstance(v, int) and v >= 0:
            out[key] = v
            out[key.split("_")[0] + "_source"] = "app"
        else:
            out[key] = env_val
            out[key.split("_")[0] + "_source"] = "env"
    v = stored.get("thin_window_start")
    try:
        parse_thin_window_start(v)
        out["thin_window_start"], out["thin_window_start_source"] = v, "app"
    except (ValueError, TypeError):
        out["thin_window_start"] = settings.history_thin_window_start
        out["thin_window_start_source"] = "env"
    v = stored.get("thin_window_minutes")
    if isinstance(v, int) and 0 <= v <= 1440:
        out["thin_window_minutes"], out["thin_window_minutes_source"] = v, "app"
    else:
        out["thin_window_minutes"] = settings.history_thin_window_minutes
        out["thin_window_minutes_source"] = "env"
    v = stored.get("thin_batch_rows")
    if isinstance(v, int) and _THIN_BATCH_FLOOR <= v <= 20000:
        out["thin_batch_rows"], out["thin_batch_rows_source"] = v, "app"
    else:
        out["thin_batch_rows"] = settings.history_thin_batch_rows
        out["thin_batch_rows_source"] = "env"
    return out


def _colfill_is_pending(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM server_kv WHERE k = 'colfill_1_9_pending'"
    ).fetchone() is not None


def trim_json(apply: bool = False, db_path: str | None = None,
              json_days: int | None = None,
              now_ms: int | None = None,
              batch_rows: int | None = None,
              deadline: float | None = None) -> dict:
    """Blank data_json on rows older than `json_days` while KEEPING every
    row — the non-destructive half of history aging. The JSON is only
    read for a device's newest rows (throttle + /current composite), it
    is the majority of each row's bytes, and every charted/recorded field
    now has a typed column — so past the window it is pure payload
    weight. What it costs: fields that never earned a column lose their
    history past the window, and any FUTURE column promotion can only
    backfill as far as the JSON still reaches.

    Refuses while the 1.9 column backfill is pending — that backfill
    reads the very JSON this would blank. No rollup requirement: JSON
    feeds neither rollups nor records. Complete local days only, same
    watermark discipline as thin_history — and, since 2.0, the same
    bounded batches: rowids by (mac, range, LENGTH > 2, LIMIT) on the
    mac-leading index, UPDATE by rowid in one short transaction, the 1 s
    budget with halve/grow, the yield, the lock backoff, the chart-index
    refusal, and a watermark that only advances past a finished one-day
    window. `deadline` is the night's budget (shared with thin_history by
    run_thin_night). Its state rides the `thin_progress` document under
    "trim"."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    json_days = (settings.history_json_detail_days
                 if json_days is None else json_days)
    if json_days <= 0:
        return {"enabled": False, "applied": False}
    ceiling = (settings.history_thin_batch_rows if batch_rows is None
               else batch_rows)
    if ceiling < _THIN_BATCH_FLOOR:
        raise ValueError(f"batch_rows must be at least {_THIN_BATCH_FLOOR}")

    path = db_path or settings.database_path
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    local_cut = datetime.fromtimestamp(
        (now_ms - json_days * 86_400_000) / 1000, tz)
    cutoff_ms = int(local_cut.replace(hour=0, minute=0, second=0,
                                      microsecond=0).timestamp() * 1000)

    conn = _connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if _colfill_is_pending(conn):
            raise RuntimeError(
                "the 1.9 column backfill is still running — it reads the "
                "JSON this trim would blank; try again once it finishes")
        row = conn.execute("SELECT v FROM server_kv WHERE k = ?",
                           (_JSON_WATERMARK_KEY,)).fetchone()
        watermark = int(row["v"]) if row and str(row["v"]).isdigit() else 0
        # Everything behind the THIN watermark is already blanked by
        # thinning — no point walking it (a first trim on a thinned
        # multi-year archive would otherwise be ~900 empty windows).
        row = conn.execute("SELECT v FROM server_kv WHERE k = ?",
                           (_THIN_WATERMARK_KEY,)).fetchone()
        thin_wm = int(row["v"]) if row and str(row["v"]).isdigit() else 0
        if watermark >= cutoff_ms:
            return {"enabled": True, "applied": False, "cutoff_ms": cutoff_ms,
                    "watermark_ms": watermark, "rows_slimmed": 0,
                    "done": True, "note": "already trimmed to cutoff"}
        if not apply:
            r = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(data_json)), 0) "
                "FROM observations WHERE dateutc_ms >= ? AND dateutc_ms < ? "
                "AND LENGTH(data_json) > 2", (watermark, cutoff_ms)).fetchone()
            return {"enabled": True, "applied": False, "cutoff_ms": cutoff_ms,
                    "watermark_ms": watermark, "rows_would_slim": r[0],
                    "json_bytes_would_drop": r[1]}

        t_start = time.monotonic()
        macs = [r[0] for r in conn.execute(_THIN_MACS_SQL)]
        # Resume at the first row on or after the resume point, not at the
        # point itself: a first run would otherwise walk from the epoch,
        # and a trim behind a thinned span would walk months of empty
        # one-day windows (a yield each) before its first candidate.
        first = _first_row_at_or_after(conn, macs, max(watermark, thin_wm))
        lo = cutoff_ms if first is None else min(first, cutoff_ms)
        rows_remaining = 0
        for mac in macs:
            rows_remaining += conn.execute(
                "SELECT COUNT(*) FROM observations WHERE mac = ? "
                "AND dateutc_ms >= ? AND dateutc_ms < ?",
                (mac, lo, cutoff_ms)).fetchone()[0]

        progress = _load_progress(conn)
        tp = progress.get("trim") if isinstance(progress.get("trim"), dict) else {}
        if tp.get("done") or "started_ms" not in tp:
            tp = {"started_ms": now_ms, "rows_slimmed_total": 0}
        base_slimmed = int(tp.get("rows_slimmed_total") or 0)
        tp.update(watermark_ms=lo, cutoff_ms=cutoff_ms, done=False,
                  rows_remaining=rows_remaining, last_run_ms=now_ms,
                  ended_early=None)
        progress["trim"] = tp

        slimmed = covered = 0
        batch = ceiling
        ended_early: str | None = None
        reached = lo
        windows_done = 0
        try:
            while lo < cutoff_ms:
                hi = min(lo + _THIN_WINDOW_MS, cutoff_ms)
                window_rows = 0
                for mac in macs:
                    window_rows += conn.execute(
                        "SELECT COUNT(*) FROM observations WHERE mac = ? "
                        "AND dateutc_ms >= ? AND dateutc_ms < ?",
                        (mac, lo, hi)).fetchone()[0]
                    n, batch, ended_early = _slim_mac_window(
                        conn, mac, lo, hi, batch, ceiling, deadline)
                    slimmed += n
                    if ended_early:
                        break
                if ended_early:
                    break
                covered += window_rows
                # Next window at the next ROW (see thin_history's step e).
                nxt = _first_row_at_or_after(conn, macs, hi)
                mark = cutoff_ms if nxt is None or nxt >= cutoff_ms else hi
                tp.update(watermark_ms=mark,
                          rows_slimmed_total=base_slimmed + slimmed)

                def _advance(c, mark=mark):
                    c.execute(
                        "INSERT INTO server_kv (k, v) VALUES (?, ?) "
                        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                        (_JSON_WATERMARK_KEY, str(mark)))
                    _save_progress(c, progress)
                _thin_txn(conn, _advance)
                reached = mark
                windows_done += 1
                lo = mark if mark == cutoff_ms else nxt
                if lo < cutoff_ms:
                    time.sleep(_THIN_YIELD_S)
        except _ThinLocked as e:
            ended_early = "locked"
            tp["watermark_ms"] = reached
            log.warning("json trim: the database stayed locked through %d "
                        "retries (%s) — ending tonight's run early; the "
                        "next night resumes from the watermark",
                        _THIN_LOCK_RETRIES, e)

        done = reached >= cutoff_ms
        minutes = max((time.monotonic() - t_start) / 60.0, 1e-6)
        rows_left = max(rows_remaining - covered, 0)
        tp.update(done=done, rows_remaining=rows_left,
                  rows_slimmed_total=base_slimmed + slimmed,
                  ended_early=ended_early)
        try:
            _thin_txn(conn, lambda c: _save_progress(c, progress))
        except _ThinLocked:
            log.warning("json trim: could not record tonight's progress "
                        "(database locked); the watermark is already saved")
        return {"enabled": True, "applied": True, "cutoff_ms": cutoff_ms,
                "watermark_ms": reached, "rows_slimmed": slimmed,
                "done": done, "windows": windows_done,
                "ended_early": ended_early, "rows_remaining": rows_left,
                "rows_covered": covered, "minutes": minutes}
    finally:
        conn.close()
