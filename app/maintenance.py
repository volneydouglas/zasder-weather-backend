"""One-off + reusable data maintenance.

`clean_cumulative_rain`: some sources (e.g. an SDR posting a sensor's lifetime
cumulative counter) historically wrote a non-resetting value into the daily /
weekly / monthly rain columns, so those "records" are lifetime totals, not real
per-period values. A genuine rollup counter resets to ~0 each period, so a
column whose all-time MIN never drops near 0 is a cumulative artifact. This
nulls those bogus values (both the column and the data_json blob) while leaving
`yearlyrainin` — the monotonic-by-design source the backend derives real
rollups from — untouched.

Usage on the server (DATABASE_PATH from the environment):
    python -m app.maintenance            # DRY RUN — report only, no changes
    python -m app.maintenance --apply    # back up affected rows, then clean
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

        stamp = int(time.time())
        backup = f"{path}.gustfix-backup-{stamp}.json"
        dump = [{"mac": r[0], "dateutc_ms": r[1], "windgustmph": r[2],
                 "windspeedmph": r[3]}
                for r in conn.execute(
                    f"SELECT mac, dateutc_ms, windgustmph, windspeedmph "
                    f"FROM observations WHERE {where}", params)]
        with open(backup, "w") as f:
            json.dump(dump, f)
        print(f"Backed up {len(dump)} gust value(s) to {backup}")

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


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    print("== cumulative rain ==")
    clean_cumulative_rain(apply=apply)
    print("\n== glitch wind gusts ==")
    clean_glitch_gusts(apply=apply)
