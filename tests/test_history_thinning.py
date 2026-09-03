"""History thinning + the import ledger + the storage breakdown (1.9).

The invariants that matter:
- thinning keeps first-per-bucket, drops old data_json, never touches the
  retention window, and is watermark-idempotent;
- the daily rollups behind the watermark are PRESERVED through a rebuild
  (they carry the true extremes the thinned raw no longer holds);
- ledgered import days are skipped (no API call), force re-imports;
- both guards (no rollups, dirty rollups) refuse loudly.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:77"
DAY_MS = 86_400_000
NOW = int(time.time() * 1000)


def _seed(db, *, old_day_offset=400):
    """Rows on an OLD day (past any cutoff): three in one 5-min bucket,
    one in the next; plus one recent row."""
    base = NOW - old_day_offset * DAY_MS
    base -= base % 300_000                     # bucket-align for stable counts
    rows = [
        {"dateutc": base,           "tempf": 70.0, "windgustmph": 10.0},
        {"dateutc": base + 60_000,  "tempf": 71.0, "windgustmph": 55.0},  # spike
        {"dateutc": base + 120_000, "tempf": 72.0, "windgustmph": 12.0},
        {"dateutc": base + 300_000, "tempf": 73.0, "windgustmph": 11.0},
        {"dateutc": NOW - 3_600_000, "tempf": 90.0, "windgustmph": 20.0},
    ]
    asyncio.run(db.insert_observations(MAC, rows))
    return base


def test_thin_keeps_first_per_bucket_and_drops_old_json(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    base = _seed(db)

    out = maintenance.thin_history(apply=True, detail_days=365,
                                   keep_minutes=5, now_ms=NOW)
    assert out["applied"] is True
    assert out["rows_deleted"] == 2            # bucket of 3 → keep first

    async def rows():
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT dateutc_ms, tempf, data_json FROM observations "
                "WHERE mac = ? ORDER BY dateutc_ms", (MAC,))
            return await cur.fetchall()

    kept = asyncio.run(rows())
    assert [r["tempf"] for r in kept] == [70.0, 73.0, 90.0]
    assert kept[0]["dateutc_ms"] == base       # first of bucket, not last
    # Old rows slimmed to '{}'; the recent row keeps its payload.
    assert kept[0]["data_json"] == "{}"
    assert kept[1]["data_json"] == "{}"
    assert json.loads(kept[2]["data_json"])["tempf"] == 90.0

    # Idempotent: watermark says done — a second pass deletes nothing.
    again = maintenance.thin_history(apply=True, detail_days=365,
                                     keep_minutes=5, now_ms=NOW)
    assert again.get("rows_deleted", 0) == 0


def test_dry_run_counts_without_deleting(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed(db)
    out = maintenance.thin_history(apply=False, detail_days=365,
                                   keep_minutes=5, now_ms=NOW)
    assert out["applied"] is False
    assert out["rows_would_delete"] == 2
    assert out["json_bytes_would_drop"] > 0

    async def count():
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM observations WHERE mac = ?", (MAC,))
            return (await cur.fetchone())["n"]
    assert asyncio.run(count()) == 5


def test_thin_refuses_without_rollups_or_with_dirty_ones(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    _seed(db)
    with pytest.raises(RuntimeError, match="INSIGHTS"):
        maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5)
    monkeypatch.setattr(settings, "insights", True)
    asyncio.run(db.set_kv("rollups_dirty", "nonce-1"))
    with pytest.raises(RuntimeError, match="dirty"):
        maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5)


def test_disabled_is_a_noop():
    from app import maintenance
    assert maintenance.thin_history(apply=True, detail_days=0) == {
        "enabled": False, "applied": False}


def test_rebuild_preserves_rollups_behind_the_watermark(client, monkeypatch):
    """The point of the whole design: the old day's gust spike lives in its
    daily rollup, the spike's raw row is thinned away, and a full rebuild
    must NOT flatten the rollup back to the thinned raw."""
    from app import db, insights, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed(db)

    async def rollup_gust():
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT MAX(windgustmph_max) AS g FROM daily_rollups "
                "WHERE mac = ?", (MAC,))
            return (await cur.fetchone())["g"]

    # insert_observations folded rollups live (insights on) — the spike
    # is in the old day's rollup.
    assert asyncio.run(rollup_gust()) == 55.0

    maintenance.thin_history(apply=True, detail_days=365,
                             keep_minutes=5, now_ms=NOW)
    asyncio.run(insights.rebuild())
    assert asyncio.run(rollup_gust()) == 55.0, \
        "rebuild recomputed a preserved day from thinned raw"


def test_wu_import_ledger_skips_and_force_refetches(client, monkeypatch):
    from app import db, wu_import

    fetched: list[str] = []

    async def fake_fetch(client_, station_id, day, api_key):
        fetched.append(day)
        return [{"obsTimeUtc": f"{day[:4]}-{day[4:6]}-{day[6:]}T12:00:00Z",
                 "imperial": {"tempAvg": 75.0}}]

    monkeypatch.setattr(wu_import, "_fetch_day", fake_fetch)
    start, end = date(2024, 3, 1), date(2024, 3, 3)

    async def run(force=False):
        fetched.clear()
        await wu_import._run(MAC, "KAZCHAND100", "k" * 16,
                             start, end, dry_run=False, force=force)
        return dict(wu_import.status())

    s1 = asyncio.run(run())
    assert len(fetched) == 3 and s1["skipped_days"] == 0
    assert asyncio.run(db.imported_days(MAC, "wu")) == {
        "2024-03-01", "2024-03-02", "2024-03-03"}

    # Re-run: every day ledgered → zero API calls, all skipped.
    s2 = asyncio.run(run())
    assert fetched == [] and s2["skipped_days"] == 3
    assert s2["done_days"] == 3

    # Force forgets the ledger and fetches again.
    s3 = asyncio.run(run(force=True))
    assert len(fetched) == 3 and s3["skipped_days"] == 0


def test_storage_breakdown_endpoint(client):
    from app import db
    _seed(db)
    # 2.0: the measurement is a background job; the first GET answers
    # "measuring" at once and the report follows (test_storage_and_snapshots
    # covers the states — this is the 1.9 shape check).
    r = client.get("/api/storage", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "measuring" and body["db_bytes"] > 0
    for _ in range(100):
        body = client.get("/api/storage", headers=H).json()
        if body["state"] == "ready":
            break
        time.sleep(0.05)
    assert body["state"] == "ready" and body["measured_ms"] > 0
    assert body["db_bytes"] > 0
    obs = body["observations"]
    assert obs["rows"] == 5
    assert obs["data_json_bytes"] > 0
    assert obs["per_station"] == [{"mac": MAC, "rows": 5}]
    tables = {t["table"]: t for t in body["tables"]}
    assert tables["observations"]["rows"] == 5
    assert body["thinning"]["enabled"] is False
    # Auth: operator-only.
    assert client.get("/api/storage").status_code == 401

# ── 1.9 column backfill + data_json trimming + retention API ────────────

def test_colfill_backfills_columns_from_json(client):
    """Rows stored before the 1.9 columns existed carry the fields only
    in data_json — the chunked backfill promotes them, typeof-gated
    (junk stays NULL, absent stays NULL), and clears its flag."""
    from app import db

    async def run():
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO observations (mac, dateutc_ms, data_json) "
                "VALUES (?, ?, ?)",
                (MAC, NOW - 10 * DAY_MS, json.dumps(
                    {"tempf": 70.0, "battout": 1, "batt1": 0,
                     "soilhum1": 33.0, "leak1": 0,
                     "lightning_last_3hr": 4,
                     "temp1f": "junk"})))
            await conn.execute(
                "INSERT INTO server_kv (k, v) VALUES "
                "('colfill_1_9_pending', '1'), ('colfill_1_9_cursor', '0')")
            await conn.commit()
        while await db.backfill_extra_columns_chunk(rows_per_chunk=10):
            pass
        # Flag cleared by the final call.
        assert await db.colfill_pending() is False
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT battout, batt1, soilhum1, leak1, "
                "lightning_last_3hr, temp1f, batt2 FROM observations "
                "WHERE mac = ?", (MAC,))
            return await cur.fetchone()

    row = asyncio.run(run())
    assert row["battout"] == 1 and row["batt1"] == 0
    assert row["soilhum1"] == 33.0 and row["leak1"] == 0
    assert row["lightning_last_3hr"] == 4
    assert row["temp1f"] is None          # junk string never casts to 0
    assert row["batt2"] is None           # absent is not zero


def test_trim_json_blanks_old_keeps_rows(client):
    from app import db, maintenance
    _seed(db)
    out = maintenance.trim_json(apply=True, json_days=90, now_ms=NOW)
    assert out["applied"] is True and out["rows_slimmed"] == 4

    async def rows():
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT tempf, data_json FROM observations WHERE mac = ? "
                "ORDER BY dateutc_ms", (MAC,))
            return await cur.fetchall()

    kept = asyncio.run(rows())
    assert len(kept) == 5                  # every row kept
    assert all(r["data_json"] == "{}" for r in kept[:4])
    assert json.loads(kept[4]["data_json"])["tempf"] == 90.0
    # Typed columns untouched — the raw-history path still serves them.
    assert [r["tempf"] for r in kept] == [70.0, 71.0, 72.0, 73.0, 90.0]

    again = maintenance.trim_json(apply=True, json_days=90, now_ms=NOW)
    assert again["rows_slimmed"] == 0      # watermark-idempotent


def test_trim_json_refuses_while_colfill_pending(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed(db)
    asyncio.run(db.set_kv("colfill_1_9_pending", "1"))
    with pytest.raises(RuntimeError, match="backfill"):
        maintenance.trim_json(apply=True, json_days=90, now_ms=NOW)
    with pytest.raises(RuntimeError, match="backfill"):
        maintenance.thin_history(apply=True, detail_days=365,
                                 keep_minutes=5, now_ms=NOW)


def test_raw_history_window_serves_thinned_rows_from_columns(client, monkeypatch):
    """The deep-zoom fix: a ≤6h window over trimmed history must serve
    rows from typed columns — the blanked JSON used to return nothing."""
    from app import db, maintenance
    base = _seed(db)
    maintenance.trim_json(apply=True, json_days=90, now_ms=NOW)

    rows = asyncio.run(db.history(MAC, base - 1, base + 400_000, 100))
    assert [r["tempf"] for r in rows] == [70.0, 71.0, 72.0, 73.0]
    assert all(r["dateutc"] for r in rows)


def test_retention_api_merge_floor_and_effective(client):
    r = client.get("/api/history-retention", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["detail_days"] == 0 and body["detail_source"] == "env"
    assert body["colfill_pending"] is False

    # Floors: destructive knobs refuse fat-finger values.
    r = client.put("/api/history-retention", headers=H,
                   json={"detail_days": 30})
    assert r.status_code == 400
    r = client.put("/api/history-retention", headers=H,
                   json={"json_days": 3})
    assert r.status_code == 400

    r = client.put("/api/history-retention", headers=H,
                   json={"detail_days": 365, "json_days": 90})
    assert r.status_code == 200
    body = r.json()
    assert body["detail_days"] == 365 and body["detail_source"] == "app"
    assert body["json_days"] == 90 and body["json_source"] == "app"

    # Merge-per-field: touching one leaves the other; -1 reverts to env.
    r = client.put("/api/history-retention", headers=H,
                   json={"json_days": -1})
    body = r.json()
    assert body["json_days"] == 0 and body["json_source"] == "env"
    assert body["detail_days"] == 365 and body["detail_source"] == "app"

    assert client.put("/api/history-retention",
                      json={"detail_days": 365}).status_code == 401


def test_force_subrange_keeps_ledger_outside_the_range(client, monkeypatch):
    """CodeRabbit PR #33: force over a SUBRANGE must not unledger the
    rest of the station's days — a later normal import would refetch
    (quota) and refill what thinning removed."""
    from app import db, wu_import

    fetched: list[str] = []

    async def fake_fetch(client_, station_id, day, api_key):
        fetched.append(day)
        return []

    monkeypatch.setattr(wu_import, "_fetch_day", fake_fetch)

    async def run(start, end, force=False):
        fetched.clear()
        await wu_import._run(MAC, "KAZCHAND100", "k" * 16,
                             start, end, dry_run=False, force=force)

    asyncio.run(run(date(2024, 3, 1), date(2024, 3, 3)))
    assert len(fetched) == 3
    # Force just one day of the three.
    asyncio.run(run(date(2024, 3, 2), date(2024, 3, 2), force=True))
    assert fetched == ["20240302"]
    # The OTHER two days stay ledgered: a normal re-run fetches nothing.
    asyncio.run(run(date(2024, 3, 1), date(2024, 3, 3)))
    assert fetched == []


def test_failed_rebuild_preserves_watermarked_rollups(client, monkeypatch):
    """CodeRabbit PR #33 critical: a rebuild that CRASHES mid-scan must
    not wipe the pre-watermark daily rollups — for thinned days they are
    the only record of the extremes, and the re-run cannot restore them."""
    from app import db, insights, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed(db)
    maintenance.thin_history(apply=True, detail_days=365,
                             keep_minutes=5, now_ms=NOW)

    async def boom(dbmod, mac):
        raise RuntimeError("simulated mid-scan crash")

    monkeypatch.setattr(insights, "_rebuild_scan", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(insights.rebuild())

    async def gust():
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT MAX(windgustmph_max) AS g FROM daily_rollups "
                "WHERE mac = ?", (MAC,))
            return (await cur.fetchone())["g"]

    assert asyncio.run(gust()) == 55.0, \
        "the failure-path clear wiped preserved rollups"


def test_ecowitt_scrub_redacts_token_from_scope(client):
    """The access-log scrub (CodeRabbit PR #33): uvicorn formats its log
    line from the scope this middleware mutates — the token must leave
    the query string and arrive as the header the route prefers."""
    from app.main import EcowittTokenScrub

    seen: dict = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    scope = {"type": "http", "path": "/ingest/ecowitt",
             "query_string": b"token=test-ingest-token&PASSKEY=x",
             "headers": [(b"host", b"example")]}
    asyncio.run(EcowittTokenScrub(inner)(scope, None, None))
    assert b"test-ingest-token" not in seen["query_string"]
    assert b"token=REDACTED" in seen["query_string"]
    assert b"PASSKEY=x" in seen["query_string"]
    assert (b"x-ingest-token", b"test-ingest-token") in seen["headers"]

    # Trailing slash (R11): the gateway's 307 redirect logs the ORIGINAL
    # query string — the scrub must cover the sloppy configuration too.
    seen.clear()
    scope = {"type": "http", "path": "/ingest/ecowitt/",
             "query_string": b"token=test-ingest-token", "headers": []}
    asyncio.run(EcowittTokenScrub(inner)(scope, None, None))
    assert b"test-ingest-token" not in seen["query_string"]

    # Other paths pass through untouched.
    seen.clear()
    scope = {"type": "http", "path": "/api/version",
             "query_string": b"token=whatever", "headers": []}
    asyncio.run(EcowittTokenScrub(inner)(scope, None, None))
    assert seen["query_string"] == b"token=whatever"


def test_thin_candidate_plans_stay_index_ranges(client):
    """R11 V2 / 2026-09-02 regression guard. The 1.9 DELETE's outer range
    on dateutc_ms ALONE had no leading index — a full index walk of the
    whole archive per chunk, minutes of write lock on a 2 GB file. Every
    candidate query now binds `mac` first; pin that BOTH paths are index
    ranges (SEARCH), never a SCAN, and that the probe path's inner EXISTS
    stays covered. Plan shape, not speed: a five-row suite cannot tell.
    """
    from app import db, maintenance

    async def plan(sql, params):
        async with db.connect() as conn:
            cur = await conn.execute("EXPLAIN QUERY PLAN " + sql, params)
            return [r["detail"] for r in await cur.fetchall()]

    win = asyncio.run(plan(maintenance._THIN_CANDIDATES_WINDOW_SQL,
                           (300_000, MAC, 0, NOW, 100)))
    assert any(d.startswith("SEARCH observations USING COVERING INDEX")
               for d in win), win
    assert not any(d.startswith("SCAN observations") for d in win), win

    probe = asyncio.run(plan(maintenance._THIN_CANDIDATES_PROBE_SQL,
                             (MAC, 0, NOW, 300_000, 300_000, 100)))
    assert any(d.startswith("SEARCH o USING COVERING INDEX") for d in probe), probe
    assert any(d.startswith("SEARCH k") and "COVERING INDEX" in d
               for d in probe), probe
    assert not any("SCAN o" in d or "SCAN k" in d or "LIST SUBQUERY" in d
                   for d in probe), probe

    slim = asyncio.run(plan(maintenance._THIN_SLIM_CANDIDATES_SQL,
                            (MAC, 0, NOW, 100)))
    assert any(d.startswith("SEARCH observations USING INDEX") for d in slim), slim
    assert not any(d.startswith("SCAN observations") for d in slim), slim


# ── nightly bounded batches (2.0, the 2026-09-02 redesign) ──────────────
# One 3-day chunk of a 2 GB archive held the writer for minutes; the pass
# is now rows-bounded batches inside a quiet-hour budget. These pin: both
# candidate paths delete the same rows; the batch halves on slow
# transactions to a 200-row floor and grows back slowly; a deadline stops
# the night mid-pass and the next night resumes from the watermark; a
# stuck lock backs off and ends the night; the chart-index rebuild refuses
# a batch; the progress document has its shape and estimate.

HOUR_MS = 3_600_000
MAC2 = "AA:BB:CC:00:00:78"


def _bulk(db, rows):
    """Direct inserts (mac, dateutc_ms, data_json): no rollup folding, so
    thousands of rows seed in milliseconds."""
    import sqlite3
    from app.config import settings
    conn = sqlite3.connect(settings.database_path)
    conn.executemany(
        "INSERT OR IGNORE INTO observations (mac, dateutc_ms, data_json) "
        "VALUES (?, ?, ?)",
        [(m, t, json.dumps({"tempf": 70.0, "pad": "x" * 40})) for m, t in rows])
    conn.commit()
    conn.close()


def _cutoff(maintenance):
    return maintenance.thin_history(apply=False, detail_days=365,
                                    keep_minutes=5, now_ms=NOW)["cutoff_ms"]


def _seed_days(db, cutoff, days, per_bucket=(3, 1, 2), macs=(MAC,),
               start_offset_days=None):
    """`days` days ending at the cutoff; each 5-min bucket of each hour's
    first `len(per_bucket)` buckets gets that many rows (the rest empty).
    Returns the expected survivors {(mac, ms)} — first of every bucket."""
    start = cutoff - (start_offset_days or days) * DAY_MS
    step = min(20_000, 300_000 // max(per_bucket))    # rows stay in-bucket
    rows, keep = [], set()
    for mac in macs:
        for d in range(days):
            for h in range(24):
                hour = start + d * DAY_MS + h * HOUR_MS
                for b, n in enumerate(per_bucket):
                    bucket = hour + b * 300_000
                    for i in range(n):
                        rows.append((mac, bucket + i * step))
                    keep.add((mac, bucket))
    _bulk(db, rows)
    return keep, len(rows)


def _survivors(db):
    import sqlite3
    from app.config import settings
    conn = sqlite3.connect(settings.database_path)
    out = {(m, t): j for m, t, j in conn.execute(
        "SELECT mac, dateutc_ms, data_json FROM observations")}
    conn.close()
    return out


def test_window_fn_and_probe_paths_delete_identical_rows(client, monkeypatch):
    """The SQLite < 3.25 fallback must be exactly the window-function
    path's keep-set: same survivors, first-in-bucket, JSON blanked."""
    import shutil
    import sqlite3
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    assert maintenance._THIN_USE_WINDOW_FN, "suite sqlite lacks window functions"
    cutoff = _cutoff(maintenance)
    keep, total = _seed_days(db, cutoff, days=2, per_bucket=(4, 1, 2, 3),
                             macs=(MAC, MAC2))
    # A bucket straddling the cutoff: one row before, two after — the
    # after-rows are inside the retention window and must survive.
    edge = (cutoff // 300_000) * 300_000
    _bulk(db, [(MAC, edge), (MAC, edge + 60_000), (MAC, edge + 120_000)])
    keep |= {(MAC, edge), (MAC, edge + 60_000), (MAC, edge + 120_000)}

    src = settings.database_path
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    copy = src + ".probe"
    shutil.copy(src, copy)

    a = maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                 now_ms=NOW, batch_rows=500)
    monkeypatch.setattr(maintenance, "_THIN_USE_WINDOW_FN", False)
    b = maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                 now_ms=NOW, batch_rows=500, db_path=copy)
    assert a["rows_deleted"] == b["rows_deleted"] == total - len(keep) + 3
    assert a["done"] and b["done"]

    left_a = _survivors(db)
    conn = sqlite3.connect(copy)
    left_b = {(m, t): j for m, t, j in conn.execute(
        "SELECT mac, dateutc_ms, data_json FROM observations")}
    conn.close()
    assert set(left_a) == set(left_b) == keep
    for k, j in left_a.items():
        assert left_b[k] == j
        # Blanked behind the cutoff; the post-cutoff edge rows keep theirs.
        assert (j == "{}") == (k[1] < cutoff), k


class _Spy:
    """Records the LIMIT of every candidate SELECT (the batch size the
    loop chose) and every DELETE/UPDATE transaction, via _connect."""

    def __init__(self, monkeypatch, maintenance):
        import sqlite3
        self.limits: list[int] = []
        self.deletes = 0
        spy = self

        class Conn(sqlite3.Connection):
            def execute(self, sql, params=()):
                if sql.startswith("SELECT rid FROM (") or \
                        sql.startswith("SELECT o.rowid FROM observations"):
                    spy.limits.append(params[-1])
                if sql.startswith("DELETE FROM observations WHERE rowid IN"):
                    spy.deletes += 1
                return super().execute(sql, params)

        def connect(path):
            conn = sqlite3.connect(path, factory=Conn)
            conn.execute("PRAGMA busy_timeout = 10000")
            return conn
        monkeypatch.setattr(maintenance, "_connect", connect)


class _FakeTime:
    """maintenance.time stand-in: monotonic advances `step` per read, sleep
    records instead of waiting, time() is real (the cutoff math)."""

    def __init__(self, step=0.0):
        self.now = 1_000.0
        self.step = step
        self.sleeps: list[float] = []

    def monotonic(self):
        self.now += self.step
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)

    def time(self):
        return time.time()


def _script_took(monkeypatch, maintenance, tooks, default):
    seq = list(tooks)
    monkeypatch.setattr(maintenance, "_txn_took",
                        lambda t0: seq.pop(0) if seq else default)


def test_batch_halves_on_slow_transactions_to_the_floor(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    # One day, 60 rows per bucket × 12 buckets: ~700 candidates in one
    # (mac, window), enough batches to walk 2000 → 200.
    _seed_days(db, cutoff, days=1, per_bucket=(60,) * 12)
    spy = _Spy(monkeypatch, maintenance)
    monkeypatch.setattr(maintenance, "time", _FakeTime())
    _script_took(monkeypatch, maintenance, [],
                 default=maintenance._THIN_TXN_BUDGET_S * 3)

    out = maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                   now_ms=NOW, batch_rows=2000)
    assert out["done"] and out["rows_deleted"] == 12 * 24 * 59
    # Every transaction "ran long": 2000 → 1000 → 500 → 250 → 200, and 200
    # is the floor however slow the disk is.
    assert spy.limits[:5] == [2000, 1000, 500, 250, 200]
    assert all(n == maintenance._THIN_BATCH_FLOOR for n in spy.limits[5:])
    assert len(spy.limits) > 6
    # The loop yields between transactions, never re-grabs the lock at once.
    assert maintenance._THIN_YIELD_S in maintenance.time.sleeps


def test_batch_grows_back_slowly_after_fast_transactions(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    _seed_days(db, cutoff, days=1, per_bucket=(60,) * 12)
    spy = _Spy(monkeypatch, maintenance)
    monkeypatch.setattr(maintenance, "time", _FakeTime())
    budget = maintenance._THIN_TXN_BUDGET_S
    # slow, slow, middling (hold), then fast: +ceiling/8 a step, capped.
    _script_took(monkeypatch, maintenance,
                 [budget * 2, budget * 2, budget / 2], default=budget / 10)

    maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                             now_ms=NOW, batch_rows=2000)
    assert spy.limits[:8] == [2000, 1000, 500, 500, 750, 1000, 1250, 1500]
    assert max(spy.limits) == 2000


def test_floor_batch_rows_rejected(client, monkeypatch):
    from app import maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    with pytest.raises(ValueError, match="batch_rows"):
        maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                 now_ms=NOW, batch_rows=50)


def test_deadline_stops_mid_pass_and_next_night_resumes_from_watermark(
        client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    keep, total = _seed_days(db, cutoff, days=6)
    lo0 = cutoff - 6 * DAY_MS
    clock = _FakeTime(step=1.0)              # every monotonic read = 1 s
    monkeypatch.setattr(maintenance, "time", clock)
    monkeypatch.setattr(maintenance, "_txn_took", lambda t0: 0.01)

    # Night 1: a budget worth a handful of reads — stops before the cutoff.
    night1 = maintenance.thin_history(
        apply=True, detail_days=365, keep_minutes=5, now_ms=NOW,
        batch_rows=2000, deadline=clock.now + 12)
    assert night1["ended_early"] == "deadline" and night1["done"] is False
    wm = night1["watermark_ms"]
    assert lo0 < wm < cutoff and (wm - lo0) % DAY_MS == 0, \
        "the watermark only advances past a fully finished window"
    assert 0 < night1["windows"] < 6

    async def kv(k):
        return await db.get_kv(k)
    assert int(asyncio.run(kv("history_thin_before_ms"))) == wm
    prog = json.loads(asyncio.run(kv("thin_progress")))
    assert prog["done"] is False and prog["window_lo_ms"] == wm
    assert prog["window_hi_ms"] == cutoff
    assert prog["ended_early"] == "deadline"
    assert isinstance(prog["nights_remaining"], int) and prog["nights_remaining"] >= 1
    # rows_remaining is exactly what is still ahead of the watermark.
    ahead = sum(1 for (m, t) in _survivors(db) if wm <= t < cutoff)
    assert prog["rows_remaining"] == ahead
    # Rows behind the watermark are finished; rows ahead untouched.
    left = _survivors(db)
    behind = {k for k in left if k[1] < wm}
    assert behind == {k for k in keep if k[1] < wm}
    assert all(left[k] == "{}" for k in behind)
    assert sum(1 for k in left if k[1] >= wm) == ahead

    # Night 2, no budget: resumes at the watermark, finishes, estimate 0.
    night2 = maintenance.thin_history(apply=True, detail_days=365,
                                      keep_minutes=5, now_ms=NOW, batch_rows=2000)
    assert night2["done"] and night2["ended_early"] is None
    assert night1["rows_deleted"] + night2["rows_deleted"] == total - len(keep)
    assert set(_survivors(db)) == keep
    prog = json.loads(asyncio.run(kv("thin_progress")))
    assert prog["done"] is True and prog["nights_remaining"] == 0
    assert prog["rows_remaining"] == 0
    assert prog["rows_deleted_total"] == total - len(keep)
    assert prog["started_ms"] == NOW and prog["last_run_ms"] == NOW
    assert set(prog) >= {"started_ms", "last_run_ms", "rows_deleted_total",
                         "rows_slimmed_total", "window_lo_ms",
                         "window_hi_ms", "done"}


def test_deadline_already_past_deletes_nothing_and_keeps_watermark(
        client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    _, total = _seed_days(db, cutoff, days=1)
    clock = _FakeTime(step=1.0)
    monkeypatch.setattr(maintenance, "time", clock)
    out = maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                   now_ms=NOW, deadline=clock.now - 1)
    assert out["ended_early"] == "deadline" and out["rows_deleted"] == 0
    assert len(_survivors(db)) == total
    assert asyncio.run(db.get_kv("history_thin_before_ms")) is None


def _locking_connect(monkeypatch, maintenance, fail_times):
    """_connect whose DELETEs raise 'database is locked' `fail_times`
    times, then behave. Returns the attempt counter."""
    import sqlite3
    state = {"fails": 0}

    class Conn(sqlite3.Connection):
        def execute(self, sql, params=()):
            if sql.startswith("DELETE FROM observations WHERE rowid IN") \
                    and state["fails"] < fail_times:
                state["fails"] += 1
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, params)

    def connect(path):
        conn = sqlite3.connect(path, factory=Conn)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn
    monkeypatch.setattr(maintenance, "_connect", connect)
    return state


def test_lock_backoff_retries_then_ends_the_night(client, monkeypatch, caplog):
    import logging
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    _, total = _seed_days(db, cutoff, days=1)
    clock = _FakeTime()
    monkeypatch.setattr(maintenance, "time", clock)
    state = _locking_connect(monkeypatch, maintenance, fail_times=99)
    caplog.set_level(logging.WARNING, logger="maintenance")

    out = maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                   now_ms=NOW)
    assert out["ended_early"] == "locked" and out["done"] is False
    assert out["rows_deleted"] == 0 and len(_survivors(db)) == total
    # Initial try + 3 retries, a 30 s back-off before each retry.
    assert state["fails"] == maintenance._THIN_LOCK_RETRIES + 1
    backoffs = [s for s in clock.sleeps if s == maintenance._THIN_LOCK_BACKOFF_S]
    assert len(backoffs) == maintenance._THIN_LOCK_RETRIES
    assert asyncio.run(db.get_kv("history_thin_before_ms")) is None
    prog = json.loads(asyncio.run(db.get_kv("thin_progress")))
    assert prog["ended_early"] == "locked" and prog["done"] is False
    assert any("ending tonight's run early" in r.getMessage()
               for r in caplog.records)


def test_lock_backoff_recovers_when_the_lock_clears(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    keep, total = _seed_days(db, cutoff, days=1)
    clock = _FakeTime()
    monkeypatch.setattr(maintenance, "time", clock)
    state = _locking_connect(monkeypatch, maintenance, fail_times=2)

    out = maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                   now_ms=NOW)
    assert out["done"] and out["ended_early"] is None
    assert state["fails"] == 2
    assert clock.sleeps.count(maintenance._THIN_LOCK_BACKOFF_S) == 2
    assert set(_survivors(db)) == keep


def test_non_lock_error_is_not_retried(client, monkeypatch):
    import sqlite3
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    _seed_days(db, cutoff, days=1)
    clock = _FakeTime()
    monkeypatch.setattr(maintenance, "time", clock)

    class Conn(sqlite3.Connection):
        def execute(self, sql, params=()):
            if sql.startswith("DELETE FROM observations WHERE rowid IN"):
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, params)

    def connect(path):
        return sqlite3.connect(path, factory=Conn)
    monkeypatch.setattr(maintenance, "_connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="I/O"):
        maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                 now_ms=NOW)
    assert clock.sleeps == []


def test_chart_index_rebuild_refuses_a_batch(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    _, total = _seed_days(db, cutoff, days=1)
    monkeypatch.setattr(db, "chart_index_rebuild_in_progress", lambda: True)
    out = maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                                   now_ms=NOW)
    assert out["ended_early"] == "chart_index" and out["rows_deleted"] == 0
    assert len(_survivors(db)) == total
    assert asyncio.run(db.get_kv("history_thin_before_ms")) is None


def test_zero_is_off_for_days_and_for_the_window(client, monkeypatch):
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    cutoff = _cutoff(maintenance)
    _, total = _seed_days(db, cutoff, days=1)
    off = {"enabled": False, "applied": False}
    assert maintenance.run_thin_night({"detail_days": 0, "thin_window_minutes": 120,
                                       "thin_batch_rows": 2000}) == off
    assert maintenance.run_thin_night({"detail_days": 365, "thin_window_minutes": 0,
                                       "thin_batch_rows": 2000}) == off
    assert len(_survivors(db)) == total
    assert asyncio.run(db.get_kv("thin_progress")) is None


def test_run_thin_night_hands_both_jobs_one_deadline(client, monkeypatch):
    """run_thin_night is the scheduler's entry: thin first, then trim,
    the SAME deadline (now + the window's minutes) and batch for both."""
    from app import maintenance
    seen: list[tuple] = []

    def fake_thin(apply, db_path, detail_days, batch_rows, deadline):
        seen.append(("thin", detail_days, batch_rows, deadline))
        return {"enabled": True, "applied": True, "rows_deleted": 0,
                "rows_slimmed": 0, "done": True, "watermark_ms": NOW,
                "rows_remaining": 0, "rows_covered": 1, "minutes": 1.0}

    def fake_trim(apply, db_path, json_days, batch_rows, deadline):
        seen.append(("trim", json_days, batch_rows, deadline))
        return {"enabled": True, "applied": True, "rows_slimmed": 0,
                "done": True, "watermark_ms": NOW, "rows_remaining": 0,
                "rows_covered": 1, "minutes": 1.0}
    monkeypatch.setattr(maintenance, "thin_history", fake_thin)
    monkeypatch.setattr(maintenance, "trim_json", fake_trim)
    t0 = time.monotonic()
    out = maintenance.run_thin_night({"detail_days": 400, "json_days": 180,
                                      "thin_window_minutes": 90,
                                      "thin_batch_rows": 700})
    assert [s[:3] for s in seen] == [("thin", 400, 700), ("trim", 180, 700)]
    assert seen[0][3] == seen[1][3]
    assert 90 * 60 - 5 <= seen[0][3] - t0 <= 90 * 60 + 5
    assert out["done"] is True and out["nights_remaining"] == 0


# ── the JSON trim, bounded the same way (2.0) ────────────────────────────
# Volney had json_days=180: on the night retention comes back, the trim's
# old 3-day UPDATE chunks would have held the writer exactly as the old
# thin did. Same batches, same budget, same watermark discipline.

def _json_left(db):
    return {k for k, j in _survivors(db).items() if j != "{}"}


def _trim_cutoff(maintenance, days=180):
    return maintenance.trim_json(apply=False, json_days=days,
                                 now_ms=NOW)["cutoff_ms"]


def test_trim_deadline_stops_mid_pass_and_resumes_from_its_watermark(
        client, monkeypatch):
    from app import db, maintenance
    cutoff = _trim_cutoff(maintenance)
    _, total = _seed_days(db, cutoff, days=6)
    lo0 = cutoff - 6 * DAY_MS
    clock = _FakeTime(step=1.0)
    monkeypatch.setattr(maintenance, "time", clock)
    monkeypatch.setattr(maintenance, "_txn_took", lambda t0: 0.01)

    night1 = maintenance.trim_json(apply=True, json_days=180, now_ms=NOW,
                                   batch_rows=2000, deadline=clock.now + 8)
    assert night1["ended_early"] == "deadline" and night1["done"] is False
    wm = night1["watermark_ms"]
    assert lo0 < wm < cutoff and (wm - lo0) % DAY_MS == 0
    assert int(asyncio.run(db.get_kv("history_json_before_ms"))) == wm
    # Rows behind the trim watermark are blank, every row still exists.
    fat = _json_left(db)
    assert all(t >= wm for (_, t) in fat) and len(_survivors(db)) == total
    prog = json.loads(asyncio.run(db.get_kv("thin_progress")))["trim"]
    assert prog["watermark_ms"] == wm and prog["cutoff_ms"] == cutoff
    assert prog["done"] is False and prog["ended_early"] == "deadline"
    assert prog["rows_remaining"] == sum(1 for (_, t) in fat if t < cutoff)
    assert prog["rows_slimmed_total"] == night1["rows_slimmed"]

    night2 = maintenance.trim_json(apply=True, json_days=180, now_ms=NOW,
                                   batch_rows=2000)
    assert night2["done"] and night2["ended_early"] is None
    assert night1["rows_slimmed"] + night2["rows_slimmed"] == total
    assert _json_left(db) == set() and len(_survivors(db)) == total
    prog = json.loads(asyncio.run(db.get_kv("thin_progress")))["trim"]
    assert prog["done"] is True and prog["rows_remaining"] == 0
    assert prog["rows_slimmed_total"] == total


def test_trim_batch_halves_to_the_floor_and_grows_back(client, monkeypatch):
    import sqlite3
    from app import db, maintenance
    cutoff = _trim_cutoff(maintenance)
    _seed_days(db, cutoff, days=1, per_bucket=(60,) * 12)     # 17,280 rows
    limits: list[int] = []

    class Conn(sqlite3.Connection):
        def execute(self, sql, params=()):
            if sql.startswith("SELECT rowid, dateutc_ms FROM observations"):
                limits.append(params[-1])
            return super().execute(sql, params)

    def connect(path):
        conn = sqlite3.connect(path, factory=Conn)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn
    monkeypatch.setattr(maintenance, "_connect", connect)
    monkeypatch.setattr(maintenance, "time", _FakeTime())
    budget = maintenance._THIN_TXN_BUDGET_S
    _script_took(monkeypatch, maintenance,
                 [budget * 2] * 4 + [budget / 2], default=budget / 10)

    out = maintenance.trim_json(apply=True, json_days=180, now_ms=NOW,
                                batch_rows=2000)
    assert out["done"] and out["rows_slimmed"] == 17_280
    # slow ×4: 2000 → 1000 → 500 → 250 → 200 (floor); middling: hold;
    # fast: +250 a step back toward the ceiling.
    assert limits[:9] == [2000, 1000, 500, 250, 200, 200, 450, 700, 950]
    assert maintenance._THIN_YIELD_S in maintenance.time.sleeps


def test_trim_lock_backoff_then_ends_the_night(client, monkeypatch):
    import sqlite3
    from app import db, maintenance
    cutoff = _trim_cutoff(maintenance)
    _, total = _seed_days(db, cutoff, days=1)
    clock = _FakeTime()
    monkeypatch.setattr(maintenance, "time", clock)

    class Conn(sqlite3.Connection):
        def execute(self, sql, params=()):
            if sql.startswith("UPDATE observations SET data_json"):
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, params)

    def connect(path):
        conn = sqlite3.connect(path, factory=Conn)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn
    monkeypatch.setattr(maintenance, "_connect", connect)
    out = maintenance.trim_json(apply=True, json_days=180, now_ms=NOW)
    assert out["ended_early"] == "locked" and out["rows_slimmed"] == 0
    assert clock.sleeps.count(maintenance._THIN_LOCK_BACKOFF_S) == \
        maintenance._THIN_LOCK_RETRIES
    assert asyncio.run(db.get_kv("history_json_before_ms")) is None
    assert len(_json_left(db)) == total


def test_trim_refuses_a_batch_during_chart_index_rebuild(client, monkeypatch):
    from app import db, maintenance
    cutoff = _trim_cutoff(maintenance)
    _, total = _seed_days(db, cutoff, days=1)
    monkeypatch.setattr(db, "chart_index_rebuild_in_progress", lambda: True)
    out = maintenance.trim_json(apply=True, json_days=180, now_ms=NOW)
    assert out["ended_early"] == "chart_index" and out["rows_slimmed"] == 0
    assert len(_json_left(db)) == total
    assert asyncio.run(db.get_kv("history_json_before_ms")) is None


def test_trim_starts_behind_nothing_thinning_already_blanked(client, monkeypatch):
    """Rows behind the thin watermark are blank already; the trim's first
    walk starts there instead of at the archive's first row."""
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    thin_cut = _cutoff(maintenance)                       # 365 d
    trim_cut = _trim_cutoff(maintenance)                  # 180 d
    _seed_days(db, thin_cut, days=2)                      # old, thinned
    _seed_days(db, trim_cut, days=2)                      # trim-only span
    maintenance.thin_history(apply=True, detail_days=365, keep_minutes=5,
                             now_ms=NOW)
    out = maintenance.trim_json(apply=True, json_days=180, now_ms=NOW)
    assert out["done"]
    # Two days of trim-only span → two one-day windows, not ~185.
    assert out["windows"] == 2
    assert _json_left(db) == set()


def test_trim_plan_stays_an_index_range(client):
    """The trim's candidate read binds mac first and walks an index
    range; a range on dateutc_ms alone was the 2026-09-02 full scan."""
    from app import db, maintenance

    async def plan():
        async with db.connect() as conn:
            cur = await conn.execute(
                "EXPLAIN QUERY PLAN " + maintenance._THIN_SLIM_CANDIDATES_SQL,
                (MAC, 0, NOW, 100))
            return [r["detail"] for r in await cur.fetchall()]
    details = asyncio.run(plan())
    assert any(d.startswith("SEARCH observations USING INDEX") and "mac=?" in d
               for d in details), details
    assert not any(d.startswith("SCAN") for d in details), details


def test_run_thin_night_shares_the_budget_and_logs_one_line(
        client, monkeypatch, caplog):
    """The real thing end to end: thinning eats the budget, the trim gets
    whatever is left, ONE progress document carries both, ONE line."""
    import logging
    import re
    from app import db, maintenance
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    thin_cut = _cutoff(maintenance)
    trim_cut = _trim_cutoff(maintenance)
    _seed_days(db, thin_cut, days=4)
    _seed_days(db, trim_cut, days=30)
    clock = _FakeTime(step=1.0)
    monkeypatch.setattr(maintenance, "time", clock)
    monkeypatch.setattr(maintenance, "_txn_took", lambda t0: 0.01)
    caplog.set_level(logging.INFO, logger="maintenance")
    # 1 "minute" of budget = 60 fake reads: thin finishes its 4 windows
    # (~5 reads each) and the trim starts but cannot finish 30 windows.
    eff = {"detail_days": 365, "json_days": 180, "thin_window_minutes": 1,
           "thin_batch_rows": 2000}

    def one_night():
        caplog.clear()
        out = maintenance.run_thin_night(eff)
        lines = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith("thinning:")]
        assert len(lines) == 1, lines
        return out, lines[0]

    out, line = one_night()
    prog = json.loads(asyncio.run(db.get_kv("thin_progress")))
    assert out["thin"]["done"] is True
    assert prog["thin_done"] is True and prog["done"] is False
    assert prog["trim"]["done"] is False
    assert prog["last_run_rows_slimmed"] == out["rows_slimmed"]
    assert isinstance(prog["nights_remaining"], int) and prog["nights_remaining"] >= 1
    assert re.fullmatch(
        r"thinning: [\d,]+ rows deleted, [\d,]+ slimmed in \d+ min, "
        r"watermark at \d{4}-\d{2}-\d{2}, trim at \d{4}-\d{2}-\d{2}, "
        r"~\d+ nights? to go", line), line

    # Following nights: the trim resumes from its watermark until done.
    for _ in range(10):
        out, line = one_night()
        if out["done"]:
            break
    assert out["done"] and line.endswith("done to the cutoff"), line
    prog = json.loads(asyncio.run(db.get_kv("thin_progress")))
    assert prog["done"] is True and prog["nights_remaining"] == 0
    assert prog["trim"]["done"] is True
    assert _json_left(db) == set()
