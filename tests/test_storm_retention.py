"""storm_history's per-station cap is a setting (2.1; the 2.0 pre-flight
review asked for it). Same shape as the reports bound: app value through
/api/storms/retention, STORM_HISTORY_MAX in the env, then the default."""
import asyncio

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:5D"


def _storm(i: int) -> dict:
    return {"started_ms": 1_000_000 + i * 10_000, "ended_ms": 1_005_000 + i * 10_000,
            "total_in": 0.1, "peak_rate_in_hr": 0.5, "max_gust_mph": 20.0,
            "min_tempf": 60.0, "max_tempf": 70.0}


async def _count(db, mac=MAC) -> int:
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) FROM storm_history WHERE mac = ?", (mac,))).fetchone()
    return row[0]


def test_the_default_is_two_hundred_and_record_storm_prunes_to_it(client):
    from app import db
    assert db.STORM_HISTORY_DEFAULT == 200

    async def run():
        eff = await db.effective_storm_retention()
        for i in range(205):
            await db.record_storm(MAC, _storm(i))
        return eff, await _count(db)
    eff, n = asyncio.run(run())
    assert eff == {"max_per_station": 200, "source": "default"}
    assert n == 200


def test_the_route_sets_clamps_and_prunes_at_once(client):
    from app import db

    async def seed():
        for i in range(30):
            await db.record_storm(MAC, _storm(i))
        for i in range(30):
            await db.record_storm("AA:BB:CC:00:00:5E", _storm(i))
    asyncio.run(seed())
    r = client.get("/api/storms/retention", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["max_per_station"] == 200 and body["source"] == "default"
    assert body["count"] == 60 and body["floor"] == 10 and body["ceiling"] == 1000

    r = client.put("/api/storms/retention", headers=H, json={"max_per_station": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["max_per_station"] == 10, "clamped to the floor"
    assert body["source"] == "app"
    assert body["count"] == 20, "both stations pruned at once"

    r = client.put("/api/storms/retention", headers=H, json={"max_per_station": -1})
    assert r.json()["source"] == "default"
    # T9: the ceiling is enforced, not merely advertised.
    r = client.put("/api/storms/retention", headers=H, json={"max_per_station": 5000})
    assert r.status_code == 200 and r.json()["max_per_station"] == 1000


def test_the_env_is_the_fallback_and_the_app_wins(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "storm_history_max", 25)   # a Settings field since 2.1
    assert asyncio.run(db.effective_storm_retention()) == {
        "max_per_station": 25, "source": "env"}
    asyncio.run(db.set_storm_retention(40))
    assert asyncio.run(db.effective_storm_retention()) == {
        "max_per_station": 40, "source": "app"}


def test_the_route_needs_the_write_token(client):
    assert client.get("/api/storms/retention").status_code == 401
    assert client.put("/api/storms/retention",
                      json={"max_per_station": 10}).status_code == 401


def test_the_zambretti_table_is_in_the_schema_and_the_ledger_agrees(client):
    from app import db, zambretti_ledger as zl
    assert "CREATE TABLE IF NOT EXISTS zambretti_calls" in db.SCHEMA
    assert zl._ddl().startswith("CREATE TABLE IF NOT EXISTS zambretti_calls")

    async def run():
        async with db.connect() as conn:
            await conn.execute("DROP TABLE zambretti_calls")
            await conn.commit()
            await zl._ensure_table(conn)
            await conn.commit()
            row = await (await conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'zambretti_calls'")).fetchone()
            return row is not None
    assert asyncio.run(run())
