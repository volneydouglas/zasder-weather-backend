"""Daily rollups widened in 2.2: sum/n pairs for humidity, wind and
pressure means, the air-monitor pair (pm25, co2) and indoor temperature.
Old rows read NULL for the new columns and the migration marks the
rollups dirty so a rebuild folds history in.
"""
import asyncio
import json

H = {"Authorization": "Bearer test-api-token"}


def test_rollup_params_carry_the_new_fields(client):
    from zoneinfo import ZoneInfo
    from app.insights import rollup_params
    tz = ZoneInfo("America/Phoenix")
    p = rollup_params({"dateutc": 1_756_700_000_000, "humidity": 40.0,
                       "windspeedmph": 5.0, "baromrelin": 29.9,
                       "pm25": 12.5, "co2": 820.0, "tempinf": 74.0}, tz)
    assert (p["humidity_n"], p["windspeedmph_n"], p["baromrelin_n"]) == (1, 1, 1)
    assert (p["pm25"], p["pm25_n"], p["co2"], p["co2_n"]) == (12.5, 1, 820.0, 1)
    assert p["tempinf"] == 74.0
    # A weather station without air fields folds zero counts, never 0 values.
    q = rollup_params({"dateutc": 1_756_700_000_000, "tempf": 90.0}, tz)
    assert q["pm25"] is None and q["pm25_n"] == 0
    assert q["humidity"] is None and q["humidity_n"] == 0


def test_a_day_of_air_readings_rolls_up_to_min_max_mean(client, monkeypatch):
    from app import db, insights, config
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)
    mac = "5D:5D:07:00:00:42"
    base = 1_756_700_000_000
    rows = [
        {"dateutc": base, "pm25": 10.0, "co2": 700.0, "humidity": 40.0,
         "windspeedmph": 0.0, "baromrelin": 29.80, "tempinf": 72.0},
        {"dateutc": base + 600_000, "pm25": 20.0, "co2": 900.0, "humidity": 50.0,
         "windspeedmph": 4.0, "baromrelin": 30.00, "tempinf": 76.0},
        {"dateutc": base + 1_200_000, "pm25": 30.0, "co2": 1100.0, "humidity": 60.0,
         "windspeedmph": 8.0, "baromrelin": 29.90},
    ]

    async def run():
        async with db.connect() as conn:
            await insights.update_rollups(conn, mac, rows)
            await conn.commit()
            r = await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?", (mac,))).fetchone()
        return r
    r = asyncio.run(run())
    assert (r["pm25_min"], r["pm25_max"], r["pm25_n"]) == (10.0, 30.0, 3)
    assert (r["co2_min"], r["co2_max"]) == (700.0, 1100.0)
    assert insights.rollup_mean(r, "pm25") == 20.0
    assert insights.rollup_mean(r, "co2") == 900.0
    assert insights.rollup_mean(r, "humidity") == 50.0
    assert insights.rollup_mean(r, "windspeedmph") == 4.0
    assert insights.rollup_mean(r, "baromrelin") == 29.9
    assert (r["tempinf_min"], r["tempinf_max"]) == (72.0, 76.0)
    # tempf never came: the old columns stay NULL, the mean is None.
    assert r["tempf_min"] is None and insights.rollup_mean(r, "tempf") is None


def test_rollup_mean_tolerates_pre_2_2_rows(client):
    from app import insights
    assert insights.rollup_mean({"humidity_min": 40.0}, "humidity") is None
    assert insights.rollup_mean({"co2_sum": 0.0, "co2_n": 0}, "co2") is None
    assert insights.rollup_mean({"co2_sum": 1800.0, "co2_n": 2}, "co2") == 900.0


def test_migration_adds_columns_and_marks_rollups_dirty(client):
    """A pre-2.2 table (no air columns) gains them on init and the dirty
    nonce is set so records() falls back to raw until a rebuild."""
    from app import db, insights

    async def run():
        async with db.connect() as conn:
            await conn.execute("DROP TABLE daily_rollups")
            await conn.execute(
                "CREATE TABLE daily_rollups (mac TEXT NOT NULL, day TEXT NOT NULL, "
                "tempf_min REAL, tempf_max REAL, tempf_sum REAL, tempf_n INTEGER, "
                "humidity_min REAL, humidity_max REAL, windspeedmph_max REAL, "
                "windgustmph_max REAL, baromrelin_min REAL, baromrelin_max REAL, "
                "dew_point_min REAL, dew_point_max REAL, feels_like_min REAL, "
                "feels_like_max REAL, uv_max REAL, solarradiation_max REAL, "
                "rain_total REAL, yearly_min REAL, yearly_max REAL, "
                "PRIMARY KEY (mac, day))")
            await conn.execute("DELETE FROM server_kv WHERE k = 'rollups_dirty'")
            await conn.commit()
        await db.init_db()
        async with db.connect() as conn:
            cols = {r[1] for r in await (await conn.execute(
                "PRAGMA table_info(daily_rollups)")).fetchall()}
            dirty = await (await conn.execute(
                "SELECT v FROM server_kv WHERE k = 'rollups_dirty'")).fetchone()
        return cols, dirty
    cols, dirty = asyncio.run(run())
    for col, _ in insights.ROLLUP_LATE_COLUMNS:
        assert col in cols, col
    assert dirty is not None and dirty[0]


def test_mcp_daily_summary_reports_air_and_means(client, monkeypatch):
    from app import db, insights, config, mcp
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)
    mac = "5D:5D:07:00:00:43"
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": mac, "name": "Office air"},
                      "timestamp_utc": "2026-06-01T18:00:00Z",
                      "indoor": {"tempf": 74.0, "humidity": 45.0},
                      "air": {"pm25": 8.0, "co2": 640.0}})
    day = asyncio.run(mcp._daily_summary({"mac": mac, "start_day": "2026-06-01"}, "owner"))
    assert day["count"] >= 1, day
    d = day["days"][0]
    assert d["pm25_min"] == 8.0 and d["co2_max"] == 640.0
    assert d["pm25_mean"] == 8.0 and d["co2_mean"] == 640.0
    assert d["tempinf_max"] == 74.0
    assert d["tempf_mean"] is None and d["humidity_mean"] is None


def test_rebuild_folds_air_and_indoor_from_history(client, monkeypatch):
    """The rebuild scan reads a fixed column list; the first production
    rebuild folded humidity means but left pm25/co2/tempinf NULL for every
    past day because the list predated them (2026-09-09)."""
    from app import insights, config
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)
    mac = "5D:5D:07:00:00:44"
    for i, (pm, co2) in enumerate(((6.0, 600.0), (10.0, 800.0))):
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token",
                             "Content-Type": "application/json"},
                    json={"device": {"id": mac, "name": "Rebuilt air"},
                          "timestamp_utc": f"2026-06-02T1{i}:00:00Z",
                          "indoor": {"tempf": 70.0 + i, "humidity": 40.0},
                          "air": {"pm25": pm, "co2": co2}})
    stats = asyncio.run(insights.rebuild(mac))
    assert stats["rows"] >= 2, stats
    from app import db

    async def row():
        async with db.connect() as conn:
            return await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ? AND day = '2026-06-02'",
                (mac,))).fetchone()
    r = asyncio.run(row())
    assert (r["pm25_min"], r["pm25_max"], r["pm25_n"]) == (6.0, 10.0, 2)
    assert insights.rollup_mean(r, "co2") == 700.0
    assert (r["tempinf_min"], r["tempinf_max"]) == (70.0, 71.0)
