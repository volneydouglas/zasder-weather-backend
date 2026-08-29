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
import sqlite3
import sys
import time

from .config import settings

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
    if len(sys.argv) > 1 and sys.argv[1] == "repair-yearly-offsets":
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


# ── history thinning (1.9) ──────────────────────────────────────────────

_THIN_WATERMARK_KEY = "history_thin_before_ms"

# A row ages out iff an EARLIER row exists in its own (mac, bucket) — i.e.
# it is not the bucket minimum. The correlated EXISTS probes the
# (mac, dateutc_ms) primary key once per row; the old tuple
# NOT IN (GROUP BY) form re-walked the whole window per row and held the
# write lock for minutes per chunk (R11 V2; measured 64s → 0.23s on a
# synthetic 324k-row window, identical survivors). Module-level so the
# plan-shape test can pin that the probe stays index-served.
# Parameters: bucket_ms, bucket_ms.
_THIN_NOT_BUCKET_MIN_SQL = (
    "EXISTS (SELECT 1 FROM observations AS k "
    "  WHERE k.mac = o.mac "
    "  AND k.dateutc_ms >= (o.dateutc_ms / ?) * ? "
    "  AND k.dateutc_ms < o.dateutc_ms)")


def thin_history(apply: bool = False, db_path: str | None = None,
                 detail_days: int | None = None,
                 keep_minutes: int | None = None,
                 now_ms: int | None = None) -> dict:
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
    if detail_days <= 0:
        return {"enabled": False, "applied": False}
    if keep_minutes <= 0:
        raise ValueError("keep_minutes must be positive")
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
                    "rows_slimmed": 0, "note": "already thinned to cutoff"}

        lo_row = conn.execute(
            "SELECT MIN(dateutc_ms) AS lo FROM observations "
            "WHERE dateutc_ms >= ?", (watermark,)).fetchone()
        span_lo = lo_row["lo"] if lo_row and lo_row["lo"] is not None else None
        if span_lo is None or span_lo >= cutoff_ms:
            if apply:
                conn.execute(
                    "INSERT INTO server_kv (k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (_THIN_WATERMARK_KEY, str(cutoff_ms)))
                conn.commit()
            return {"enabled": True, "applied": apply, "cutoff_ms": cutoff_ms,
                    "watermark_ms": cutoff_ms, "rows_deleted": 0,
                    "rows_slimmed": 0, "note": "nothing old enough"}

        if not apply:
            would_del = conn.execute(
                "SELECT COUNT(*) FROM observations AS o "
                "WHERE o.dateutc_ms >= ? AND o.dateutc_ms < ? "
                "AND " + _THIN_NOT_BUCKET_MIN_SQL,
                (span_lo, cutoff_ms, bucket_ms, bucket_ms)).fetchone()[0]
            json_bytes = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(data_json)), 0) FROM observations "
                "WHERE dateutc_ms >= ? AND dateutc_ms < ? "
                "AND LENGTH(data_json) > 2",
                (span_lo, cutoff_ms)).fetchone()[0]
            return {"enabled": True, "applied": False, "cutoff_ms": cutoff_ms,
                    "watermark_ms": watermark,
                    "rows_would_delete": would_del,
                    "json_bytes_would_drop": json_bytes}

        # Chunked by 3-day windows: bounded transactions, so live ingest
        # never waits long on the write lock (the insights-rebuild lesson).
        # 3 days, not 30 — the per-chunk JSON blanking measured ~1.8s per
        # 30-day window on a fast machine, plausibly 5-10s on a small Fly
        # box, flirting with ingest's 10s busy_timeout (R11 V9).
        deleted = slimmed = 0
        chunk_ms = 3 * 86_400_000
        lo = max(watermark, (span_lo // bucket_ms) * bucket_ms)
        while lo < cutoff_ms:
            hi = min(lo + chunk_ms, cutoff_ms)
            cur = conn.execute(
                "DELETE FROM observations AS o "
                "WHERE o.dateutc_ms >= ? AND o.dateutc_ms < ? "
                "AND " + _THIN_NOT_BUCKET_MIN_SQL,
                (lo, hi, bucket_ms, bucket_ms))
            deleted += cur.rowcount or 0
            cur = conn.execute(
                "UPDATE observations SET data_json = '{}' "
                "WHERE dateutc_ms >= ? AND dateutc_ms < ? "
                "AND LENGTH(data_json) > 2", (lo, hi))
            slimmed += cur.rowcount or 0
            conn.execute(
                "INSERT INTO server_kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_THIN_WATERMARK_KEY, str(hi)))
            conn.commit()
            lo = hi
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return {"enabled": True, "applied": True, "cutoff_ms": cutoff_ms,
                "watermark_ms": cutoff_ms, "rows_deleted": deleted,
                "rows_slimmed": slimmed,
                "reusable_bytes": free_pages * page_size}
    finally:
        conn.close()


def storage_breakdown(db_path: str | None = None,
                      detail_days: int | None = None,
                      keep_minutes: int | None = None) -> dict:
    """Where the database's bytes actually live (1.9): per-table sizes when
    the sqlite build exposes dbstat, per-table row counts always, and the
    observations-specific split that matters for thinning decisions — how
    much is data_json payload, and how much of everything is older than
    the retention window. Read-only; a COUNT/SUM pass over a multi-GB
    database takes seconds, which is fine for an operator-triggered look.
    """
    import os as _os

    path = db_path or settings.database_path
    out: dict = {"database_path": path}
    for label, p in (("db_bytes", path), ("wal_bytes", path + "-wal"),
                     ("shm_bytes", path + "-shm")):
        try:
            out[label] = _os.stat(p).st_size
        except OSError:
            out[label] = 0

    # URI-quote the path (R11): a DATABASE_PATH containing '?' or '#'
    # would silently truncate at the reserved character. Operator-
    # controlled, but the quote costs nothing.
    from urllib.parse import quote
    conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        out["page_size"] = page_size
        out["freelist_bytes"] = (
            conn.execute("PRAGMA freelist_count").fetchone()[0] * page_size)

        # Bytes per table+its indexes, when this sqlite has the dbstat
        # virtual table. Absent it, sizes stay null and row counts still
        # tell most of the story.
        sizes: dict[str, int] = {}
        try:
            for r in conn.execute(
                    "SELECT name, SUM(pgsize) AS b FROM dbstat GROUP BY name"):
                sizes[r["name"]] = r["b"]
        except sqlite3.Error:
            sizes = {}
        idx_of: dict[str, str] = {}
        tables: list[str] = []
        for r in conn.execute(
                "SELECT name, type, tbl_name FROM sqlite_master "
                "WHERE type IN ('table', 'index') "
                "AND name NOT LIKE 'sqlite_%'"):
            if r["type"] == "table":
                tables.append(r["name"])
            else:
                idx_of[r["name"]] = r["tbl_name"]
        table_rows = []
        for t in sorted(tables):
            # t comes from sqlite_master, not an internal whitelist — the
            # repo's SQL-interpolation rule applies. Doubling embedded
            # quotes makes the identifier quoting injection-proof.
            quoted = '"' + t.replace('"', '""') + '"'
            n = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            b = sizes.get(t)
            ib = sum(v for k, v in sizes.items() if idx_of.get(k) == t)
            table_rows.append({"table": t, "rows": n,
                               "bytes": b,
                               "index_bytes": ib if sizes else None})
        table_rows.sort(key=lambda d: -(d["bytes"] or 0))
        out["tables"] = table_rows

        # Resolved (app-over-env) values when the caller passed them — the
        # /api/storage report must describe the retention policy actually
        # APPLIED, not the env fallback (CodeRabbit, PR #33).
        eff_detail = (settings.history_detail_days
                      if detail_days is None else detail_days)
        eff_keep = (settings.history_keep_interval_minutes
                    if keep_minutes is None else keep_minutes)
        cutoff_days = eff_detail or 365
        cutoff_ms = int(time.time() * 1000) - cutoff_days * 86_400_000
        obs = conn.execute(
            "SELECT COUNT(*) AS rows_total, "
            "COALESCE(SUM(LENGTH(data_json)), 0) AS json_bytes, "
            "SUM(dateutc_ms < ?) AS rows_old, "
            "COALESCE(SUM(CASE WHEN dateutc_ms < ? "
            "  THEN LENGTH(data_json) END), 0) AS json_bytes_old "
            "FROM observations", (cutoff_ms, cutoff_ms)).fetchone()
        per_mac = [{"mac": r["mac"], "rows": r["n"]} for r in conn.execute(
            "SELECT mac, COUNT(*) AS n FROM observations "
            "GROUP BY mac ORDER BY n DESC")]
        wm = conn.execute("SELECT v FROM server_kv WHERE k = ?",
                          (_THIN_WATERMARK_KEY,)).fetchone()
        out["observations"] = {
            "rows": obs["rows_total"],
            "data_json_bytes": obs["json_bytes"],
            "older_than_days": cutoff_days,
            "rows_older": obs["rows_old"] or 0,
            "data_json_bytes_older": obs["json_bytes_old"],
            "per_station": per_mac,
        }
        out["thinning"] = {
            "enabled": eff_detail > 0,
            "detail_days": eff_detail,
            "keep_interval_minutes": eff_keep,
            "watermark_ms": int(wm["v"]) if wm and str(wm["v"]).isdigit() else None,
        }
        return out
    finally:
        conn.close()


# ── data_json trimming + effective retention (1.9) ──────────────────────

_JSON_WATERMARK_KEY = "history_json_before_ms"
_RETENTION_KV_KEY = "history_retention"


async def effective_retention() -> dict:
    """The knobs the daily pass actually runs with: app-stored values (set
    from Settings, kept in server_kv) win over env, the alert-prefs
    precedence rule. Returns {'detail_days', 'json_days', 'detail_source',
    'json_source'}."""
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
    return out


def _colfill_is_pending(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM server_kv WHERE k = 'colfill_1_9_pending'"
    ).fetchone() is not None


def trim_json(apply: bool = False, db_path: str | None = None,
              json_days: int | None = None,
              now_ms: int | None = None) -> dict:
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
    watermark discipline as thin_history."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    json_days = (settings.history_json_detail_days
                 if json_days is None else json_days)
    if json_days <= 0:
        return {"enabled": False, "applied": False}

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
        if watermark >= cutoff_ms:
            return {"enabled": True, "applied": False, "cutoff_ms": cutoff_ms,
                    "watermark_ms": watermark, "rows_slimmed": 0,
                    "note": "already trimmed to cutoff"}
        if not apply:
            r = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(data_json)), 0) "
                "FROM observations WHERE dateutc_ms >= ? AND dateutc_ms < ? "
                "AND LENGTH(data_json) > 2", (watermark, cutoff_ms)).fetchone()
            return {"enabled": True, "applied": False, "cutoff_ms": cutoff_ms,
                    "watermark_ms": watermark, "rows_would_slim": r[0],
                    "json_bytes_would_drop": r[1]}
        slimmed = 0
        # 3-day windows for the same reason as thin_history's loop: a 30-day
        # JSON-blanking UPDATE measured ~1.8s on a fast machine and could
        # flirt with ingest's 10s busy_timeout on a small Fly box (R11 V9).
        chunk_ms = 3 * 86_400_000
        lo = watermark
        if lo == 0:
            # First run: start at the archive's first row, not the epoch —
            # 3-day chunks from 1970 would be ~6,800 empty commit cycles.
            first = conn.execute(
                "SELECT MIN(dateutc_ms) FROM observations").fetchone()[0]
            lo = cutoff_ms if first is None else min(first, cutoff_ms)
        while lo < cutoff_ms:
            hi = min(lo + chunk_ms, cutoff_ms)
            cur = conn.execute(
                "UPDATE observations SET data_json = '{}' "
                "WHERE dateutc_ms >= ? AND dateutc_ms < ? "
                "AND LENGTH(data_json) > 2", (lo, hi))
            slimmed += cur.rowcount or 0
            conn.execute(
                "INSERT INTO server_kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_JSON_WATERMARK_KEY, str(hi)))
            conn.commit()
            lo = hi
        return {"enabled": True, "applied": True, "cutoff_ms": cutoff_ms,
                "watermark_ms": cutoff_ms, "rows_slimmed": slimmed}
    finally:
        conn.close()
