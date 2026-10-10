"""2.6: the cache epoch (CodeRabbit on PR #50).

Popping a deleted station's cache entries is half the fix. A producer that
was awaiting the database when the DELETE ran resumes with the station's
pre-delete rows and writes them into the cache it was asked to fill; the
re-added MAC is then served the old station's count, strip, checks, records
or rain for a TTL. Every per-MAC producer now reads the epoch before its
await and stores only while it is unchanged; `db.delete_device` and a
restore bump it (one process-wide integer: a per-MAC map would grow with
every unknown MAC a token holder deletes, PR #54 review).

Each race below lets the producer finish its database read FIRST, deletes
the station while the producer is parked, then lets it resume: without the
token check the stale answer lands in the cache, with it the cache stays
empty. The positive test proves the check is not simply always false.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

IH = {"Authorization": "Bearer test-ingest-token", "Content-Type": "application/json"}
MAC = "AA:BB:CC:00:00:C6"


def _post(client, minutes_ago: int) -> None:
    ts = datetime.fromtimestamp(time.time() - minutes_ago * 60, timezone.utc)
    r = client.post("/ingest/custom", headers=IH, json={
        "device": {"id": MAC, "name": "Yard"},
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdoor": {"tempf": 71.0},
        "rain": {"daily_in": 0.12, "yearly_in": 4.5}})
    assert r.status_code in (200, 201, 202), r.text


def _park(monkeypatch, module, name):
    """Replace `module.name` with a twin that does the real read, then
    parks until the test says go. Returns (reached, release)."""
    real = getattr(module, name)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def slow(*a, **k):
        result = await real(*a, **k)       # the PRE-delete rows
        reached.set()
        await release.wait()               # the delete lands here
        return result

    monkeypatch.setattr(module, name, slow)
    return reached, release


def _race(client, monkeypatch, module, name, producer, main):
    """Run `producer()` with `module.name` parked, delete the station in
    the gap, let the producer finish. Returns the producer's result."""
    _post(client, 5)
    _post(client, 2)

    async def go():
        reached, release = _park(monkeypatch, module, name)
        task = asyncio.create_task(producer())
        await reached.wait()
        await main.delete_device(MAC)      # the route: rows, pops, bump
        release.set()
        return await task
    return asyncio.run(go())


def _main():
    import sys
    return sys.modules["app.main"]


def test_an_observation_count_in_flight_is_not_cached_after_the_delete(client, monkeypatch):
    from app import db
    main = _main()
    n = _race(client, monkeypatch, db, "observation_count",
              lambda: main._cached_observation_count(MAC), main)
    assert n == 2, "the producer still answers its own caller"
    assert MAC not in main._OBS_COUNT_CACHE


def test_a_stale_recount_task_is_not_cached_after_the_delete(client, monkeypatch):
    """The warm path: an expired entry is served and ONE background recount
    runs; that task is a producer too."""
    from app import db
    main = _main()
    _post(client, 5)
    main._OBS_COUNT_CACHE[MAC] = (time.time() - main._OBS_COUNT_TTL_S - 1, 99)

    async def go():
        reached, release = _park(monkeypatch, db, "observation_count")
        served = await main._cached_observation_count(MAC)     # spawns the recount
        await reached.wait()
        await main.delete_device(MAC)
        release.set()
        await asyncio.gather(*main._OBS_COUNT_TASKS.values())
        return served
    assert asyncio.run(go()) == 99, "the stale value is what the warm path serves"
    assert MAC not in main._OBS_COUNT_CACHE


def test_an_arrival_strip_in_flight_is_not_cached_after_the_delete(client, monkeypatch):
    from app import db
    main = _main()
    now_ms = int(time.time() * 1000)
    _race(client, monkeypatch, db, "arrival_slots",
          lambda: main._arrival_day(MAC, [], now_ms), main)
    assert MAC not in main._ARRIVAL_CACHE


def test_sensor_checks_in_flight_are_not_cached_after_the_delete(client, monkeypatch):
    from app import db
    main = _main()
    _race(client, monkeypatch, db, "accepted_counts",
          lambda: main.get_sensor_health(MAC, days=30), main)
    assert not [k for k in main._SENSOR_HEALTH_CACHE if k[0] == MAC]


def test_records_in_flight_are_not_cached_after_the_delete(client, monkeypatch):
    from app import db
    main = _main()
    _race(client, monkeypatch, db, "records",
          lambda: main._warm_records(MAC), main)
    assert MAC not in main._RECORDS_CACHE


def test_rain_rollups_in_flight_are_not_cached_after_the_delete(client, monkeypatch):
    from app import db
    main = _main()
    _race(client, monkeypatch, db, "_rain_ledger_periods",
          lambda: db._rollups_from_daily(MAC, ZoneInfo("UTC")), main)
    assert not [k for k in db._DAILY_ROLLUP_CACHE if k[0] == MAC]


def test_a_rain_memo_in_flight_is_not_cached_after_the_delete(client, monkeypatch):
    from app import db
    main = _main()
    _post(client, 5)
    key = (MAC, 1_700_000_000_000)

    async def go():
        reached = asyncio.Event()
        release = asyncio.Event()

        async def compute():
            reached.set()
            await release.wait()
            return 4.5

        task = asyncio.create_task(db._cached_rain_lookup(key, True, compute))
        await reached.wait()
        await main.delete_device(MAC)
        release.set()
        return await task
    assert asyncio.run(go()) == 4.5
    assert key not in db._YEAR_PRIOR_CACHE


def test_the_same_producers_do_cache_when_nothing_was_deleted(client, monkeypatch):
    """The guard is a comparison, not a constant: with no delete in the
    gap every store lands."""
    from app import db
    main = _main()
    _post(client, 5)
    _post(client, 2)
    now_ms = int(time.time() * 1000)

    async def go():
        await main._cached_observation_count(MAC)
        await main._arrival_day(MAC, [], now_ms)
        await main.get_sensor_health(MAC, days=30)
        await main._warm_records(MAC)
        await db._rollups_from_daily(MAC, ZoneInfo("UTC"))
        await db._cached_rain_lookup((MAC, 1), True, _four)
    asyncio.run(go())
    assert main._OBS_COUNT_CACHE[MAC][1] == 2
    assert MAC in main._ARRIVAL_CACHE
    assert [k for k in main._SENSOR_HEALTH_CACHE if k[0] == MAC]
    assert MAC in main._RECORDS_CACHE
    assert [k for k in db._DAILY_ROLLUP_CACHE if k[0] == MAC]
    assert (MAC, 1) in db._YEAR_PRIOR_CACHE


async def _four() -> float:
    return 4.0


def test_a_restore_invalidates_every_token(client):
    """The reconcile-after-restore path bumps the epoch before it clears
    the caches, so a producer mid-await against the OLD database cannot
    refill them (R25-09's remaining gap)."""
    from app import db
    before = db.cache_token()
    db.invalidate_caches()
    assert not db.cache_token_current(before)


def test_a_delete_evicts_the_rain_memos_a_re_added_mac_would_be_served(client):
    """PR #54 review (both reviewers): the write guard cannot help an entry
    that is already there, and `_cached_rain_lookup` serves a hit before it
    computes anything. The delete evicts this MAC's rollup and year-prior
    entries and leaves the neighbour's alone."""
    from app import db
    main = _main()
    _post(client, 5)
    other = "AA:BB:CC:00:00:C7"
    exp = time.monotonic() + 3600
    db._YEAR_PRIOR_CACHE[(MAC, 1, "prior")] = (exp, 4.5)
    db._YEAR_PRIOR_CACHE[(other, 1, "prior")] = (exp, 1.0)
    db._DAILY_ROLLUP_CACHE[(MAC, "2026-10-04")] = (exp, {"daily_in": 0.5})
    db._DAILY_ROLLUP_CACHE[(other, "2026-10-04")] = (exp, {"daily_in": 0.1})
    asyncio.run(main.delete_device(MAC))
    assert [k for k in db._YEAR_PRIOR_CACHE if k[0] == MAC] == []
    assert [k for k in db._DAILY_ROLLUP_CACHE if k[0] == MAC] == []
    assert (other, 1, "prior") in db._YEAR_PRIOR_CACHE
    assert (other, "2026-10-04") in db._DAILY_ROLLUP_CACHE


def test_deleting_unknown_macs_bumps_the_epoch_and_keeps_no_per_mac_state(client):
    """The path parameter is any string a token holder types; the state a
    delete leaves behind must not grow with it (PR #54 review)."""
    from app import db
    before = db.cache_token()
    for i in range(50):
        asyncio.run(db.delete_device(f"AA:BB:CC:FF:00:{i:02X}"))
    assert db.cache_token() == before + 50
    assert not hasattr(db, "_CACHE_GEN")


def test_the_restore_path_bumps_the_epoch_before_clearing():
    """Source pin: the order matters (epoch first, clears second), and a
    future reshuffle of the reconcile block must keep it."""
    import inspect
    import app.main as main
    src = inspect.getsource(main)
    i = src.index("db.invalidate_caches()")
    j = src.index("db._DAILY_ROLLUP_CACHE.clear()", i)
    k = src.index("_reconcile_after_restore", 0)
    assert k < i < j, "invalidate_caches runs inside the reconcile, before the clears"
