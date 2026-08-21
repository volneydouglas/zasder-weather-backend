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
    conn = sqlite3.connect(path)
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
    conn = sqlite3.connect(path)
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
    conn = sqlite3.connect(path)
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
    conn = sqlite3.connect(path)
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
    conn = sqlite3.connect(path)
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
