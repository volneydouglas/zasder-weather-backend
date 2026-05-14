"""SQLite roundtrip tests — insert / count / dedup.

Uses the same temp_env fixture as the API tests but skips the TestClient
since these go straight at the db module."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def db_module(temp_env: str):
    # Force re-import so config.settings picks up our temp DATABASE_PATH.
    for mod in ["app.config", "app.db"]:
        if mod in importlib.sys.modules: importlib.reload(importlib.sys.modules[mod])
    from app import db
    return db


@pytest.mark.asyncio
async def test_insert_dedup(db_module):
    db = db_module
    await db.init_db()
    rows = [{"dateutc": 1000, "tempf": 70.0}, {"dateutc": 2000, "tempf": 71.0}]
    n = await db.insert_observations("AA:BB:CC:DD:EE:FF", rows)
    assert n == 2
    # Same MAC + same dateutc_ms is a primary-key conflict → silently ignored.
    n2 = await db.insert_observations("AA:BB:CC:DD:EE:FF", rows)
    assert n2 == 0
    assert await db.observation_count("AA:BB:CC:DD:EE:FF") == 2


@pytest.mark.asyncio
async def test_observation_count_isolated_per_mac(db_module):
    db = db_module
    await db.init_db()
    await db.insert_observations("AA:11", [{"dateutc": 1, "tempf": 70}])
    await db.insert_observations("BB:22", [{"dateutc": 1, "tempf": 80},
                                            {"dateutc": 2, "tempf": 81}])
    assert await db.observation_count("AA:11") == 1
    assert await db.observation_count("BB:22") == 2
    assert await db.observation_count("CC:33") == 0


@pytest.mark.asyncio
async def test_history_returns_chronological(db_module):
    db = db_module
    await db.init_db()
    await db.insert_observations("AA:11", [
        {"dateutc": 3000, "tempf": 73},
        {"dateutc": 1000, "tempf": 71},
        {"dateutc": 2000, "tempf": 72},
    ])
    rows = await db.history("AA:11", 0, 10000)
    temps = [r.get("tempf") for r in rows]
    # history is ORDER BY dateutc_ms ASC
    assert temps == [71, 72, 73]
