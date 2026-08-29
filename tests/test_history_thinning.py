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
    r = client.get("/api/storage", headers=H)
    assert r.status_code == 200
    body = r.json()
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


def test_thin_delete_plan_stays_index_probed(client):
    """R11 V2 regression guard. The thinning keep-set predicate must probe
    the (mac, dateutc_ms) primary key once per candidate row; the tuple
    NOT IN (GROUP BY) form it replaced re-walked the whole window per row
    and held the write lock for minutes on a real archive — while a
    five-row test suite sailed through. Pin the plan shape, not the speed.
    """
    from app import db, maintenance
    _seed(db)

    async def plan():
        async with db.connect() as conn:
            cur = await conn.execute(
                "EXPLAIN QUERY PLAN DELETE FROM observations AS o "
                "WHERE o.dateutc_ms >= ? AND o.dateutc_ms < ? "
                "AND " + maintenance._THIN_NOT_BUCKET_MIN_SQL,
                (0, NOW, 300_000, 300_000))
            return [r["detail"] for r in await cur.fetchall()]

    details = asyncio.run(plan())
    assert any(d.startswith("SEARCH k USING COVERING INDEX")
               for d in details), details
    assert not any("SCAN k" in d or "LIST SUBQUERY" in d
                   for d in details), details
