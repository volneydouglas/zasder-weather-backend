"""Reports, the 2.1 follow-ups: retention as a setting, the storm-history
backfill, the runnable NOAA kinds, and the disagreement block. The wire
these pin is the same wire the Swift decoders read
(bin/tests/test_report_wire_parity.py)."""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import reports as rp  # noqa: E402

AUTH = {"Authorization": "Bearer test-api-token"}
MAC = "5D:5D:05:00:00:01"


def _station(name, **kw):
    from app import digest as dg
    base = dict(name=name, tmax_f=100.0, tmin_f=75.0, feels_max_f=None,
                rain_in=0.0, gust_mph=20.0, humidity_lo=10.0,
                humidity_hi=40.0, uv_max=8.0)
    base.update(kw)
    return dg.StationDay(**base)


def _report(stations):
    from app import digest as dg
    return dg.Report(date_label="Thursday, September 3", stations=stations)


async def _seed_rollups(db, mac=MAC, year=2025, month=10, days=3):
    async with db.connect() as conn:
        for d in range(1, days + 1):
            await conn.execute(
                "INSERT INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                "tempf_sum, tempf_n, rain_total, windgustmph_max) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mac, f"{year}-{month:02d}-{d:02d}", 60.0 + d, 90.0 + d,
                 150.0, 2, 0.1 * d, 20.0 + d))
        await conn.commit()


# ── the disagreement block (item 6) ─────────────────────────────────────

def test_one_station_has_no_spread():
    from app import digest as dg
    assert dg.compute_spread([_station("Roof")]) is None
    assert rp.morning_payload(_report([_station("Roof")]))["spread"] is None


def test_the_spread_is_the_gap_between_stations_never_a_mean():
    from app import digest as dg
    sp = dg.compute_spread([
        _station("Roof", tmax_f=104.7, tmin_f=80.0, gust_mph=31.0, rain_in=0.10),
        _station("Yard", tmax_f=98.2, tmin_f=78.0, gust_mph=22.0, rain_in=0.14),
        _station("Shed", tmax_f=101.3, tmin_f=79.0, gust_mph=25.0, rain_in=0.12),
    ])
    assert sp["station_count"] == 3
    hi = next(f for f in sp["fields"] if f["key"] == "tmax_f")
    assert hi["min"] == 98.2 and hi["min_station"] == "Yard"
    assert hi["max"] == 104.7 and hi["max_station"] == "Roof"
    assert hi["spread"] == 6.5
    # Median, not mean: the mean would be 101.4, a number no sensor read.
    assert hi["consensus"] == 101.3
    gust = next(f for f in sp["fields"] if f["key"] == "gust_mph")
    assert gust["consensus"] == 31.0, "gust consensus is the MAX"
    rain = next(f for f in sp["fields"] if f["key"] == "rain_in")
    assert rain["consensus"] == 0.14, "the highest gauge wins for rain"
    assert sp["headline"] == "Your three sensors spread 6.5°F on yesterday's high"


def test_a_station_missing_a_sensor_drops_out_of_that_field_only():
    from app import digest as dg
    sp = dg.compute_spread([
        _station("A", tmax_f=100.0, gust_mph=None),
        _station("B", tmax_f=95.0, gust_mph=None),
    ])
    keys = [f["key"] for f in sp["fields"]]
    assert "tmax_f" in keys and "gust_mph" not in keys


def test_the_spread_reaches_the_mail_and_the_stored_payload():
    from app import digest as dg
    r = _report([_station("Roof", tmax_f=104.7), _station("Yard", tmax_f=98.2)])
    text = dg.build_text(r)
    assert "Your two sensors spread 6.5°F on yesterday's high." in text
    assert "High: 98°F at Yard to 105°F at Roof, 6.5°F apart" in text
    html_body = dg.build_html(r)
    assert "YOUR SENSORS DISAGREE" in html_body
    payload = rp.morning_payload(r)
    assert payload["spread"]["fields"][0]["spread"] == 6.5
    assert payload["spread"]["station_count"] == 2
    import json
    json.dumps(payload, allow_nan=False)


def test_ten_or_more_stations_use_digits_in_the_headline():
    from app import digest as dg
    sp = dg.compute_spread([_station(f"S{i}", tmax_f=90.0 + i)
                            for i in range(11)])
    assert sp["headline"].startswith("Your 11 sensors spread 10.0°F")


# ── retention as a setting (item 11) ────────────────────────────────────

def test_retention_defaults_then_env_then_app(client, monkeypatch):
    from app import db
    from app.config import settings
    # REPORTS_MAX_ROWS is a Settings field since the 2.1 pre-release review
    # (it used to be read straight from os.environ and ignored .env).
    monkeypatch.setattr(settings, "reports_max_rows", None)
    assert asyncio.run(db.effective_report_retention()) \
        == {"max_rows": 900, "source": "default"}
    monkeypatch.setattr(settings, "reports_max_rows", 400)
    assert asyncio.run(db.effective_report_retention()) \
        == {"max_rows": 400, "source": "env"}
    r = client.put("/api/reports/retention", headers=AUTH,
                   json={"max_rows": 120})
    assert r.status_code == 200, r.text
    assert r.json()["max_rows"] == 120 and r.json()["source"] == "app"
    assert r.json()["floor"] == rp.MIN_ROWS
    # -1 forgets the app value: env takes over again.
    r = client.put("/api/reports/retention", headers=AUTH,
                   json={"max_rows": -1})
    assert r.json() ["source"] == "env" and r.json()["max_rows"] == 400
    g = client.get("/api/reports/retention", headers=AUTH)
    assert g.status_code == 200 and g.json()["count"] == 0


def test_retention_is_clamped_and_prunes_at_once(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "reports_max_rows", None)

    async def seed():
        for i in range(40):
            await db.insert_report(
                kind="morning", mac=None, ts_ms=1000 + i, for_date=None,
                title=f"r{i}", summary=None, payload={}, dedupe=f"k{i}")
    asyncio.run(seed())
    # 5 is below the floor: clamped up to 30, and the table prunes NOW.
    r = client.put("/api/reports/retention", headers=AUTH,
                   json={"max_rows": 5})
    assert r.json()["max_rows"] == rp.MIN_ROWS
    assert r.json()["count"] == rp.MIN_ROWS
    assert asyncio.run(db.count_reports()) == rp.MIN_ROWS
    r = client.put("/api/reports/retention", headers=AUTH,
                   json={"max_rows": 99_999})
    assert r.json()["max_rows"] == rp.MAX_ROWS_CEILING
    assert client.put("/api/reports/retention", headers=AUTH,
                      json={"max_rows": "lots"}).status_code == 422
    assert client.get("/api/reports/retention").status_code == 401


def test_insert_prunes_to_the_effective_setting(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "reports_max_rows", 30)

    async def run():
        for i in range(35):
            await db.insert_report(
                kind="morning", mac=None, ts_ms=1000 + i, for_date=None,
                title=f"r{i}", summary=None, payload={}, dedupe=f"k{i}")
        return await db.count_reports()
    assert asyncio.run(run()) == 30


def test_a_bad_env_value_falls_back_to_the_default(client, monkeypatch):
    """A non-numeric value is refused by Settings at boot now (pydantic);
    what reaches the reader is an int or None. A stored kv that has gone
    bad still falls back, which is the case worth keeping."""
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "reports_max_rows", None)
    asyncio.run(db.set_kv("reports_max_rows", "nine hundred"))
    assert asyncio.run(db.effective_report_retention())["source"] == "default"


# ── the storm-history backfill (item 12) ────────────────────────────────

def test_storm_history_becomes_reports_once(client):
    from app import alerts, db

    async def run():
        await db.upsert_device(MAC, {"name": "Chaucer Drive", "info": {}})
        for i in range(3):
            await db.record_storm(MAC, {
                "started_ms": 1_788_400_000_000 + i * 10_000_000,
                "ended_ms": 1_788_403_600_000 + i * 10_000_000,
                "total_in": 0.5 + i, "peak_rate_in_hr": 0.3,
                "max_gust_mph": 30.0, "min_tempf": 70.0, "max_tempf": 90.0,
                "temp_drop_f": 12.5})
        first = await alerts.backfill_storm_reports()
        second = await alerts.backfill_storm_reports()
        rows = await db.list_reports(kind=rp.KIND_STORM)
        return first, second, rows
    first, second, rows = asyncio.run(run())
    assert first == 3 and second == 0
    assert len(rows) == 3
    assert all(r["mac"] == MAC for r in rows)
    assert "Chaucer Drive" in rows[0]["title"]
    detail = client.get(f"/api/reports/{rows[0]['id']}", headers=AUTH).json()
    assert detail["report"]["payload"]["temp_drop_f"] == 12.5
    assert detail["report"]["payload"]["station"] == "Chaucer Drive"


def test_the_backfill_never_overwrites_a_report_the_live_path_wrote(client):
    from app import alerts, db

    async def run():
        await db.upsert_device(MAC, {"name": "Chaucer Drive", "info": {}})
        started = 1_788_400_000_000
        await db.record_storm(MAC, {
            "started_ms": started, "ended_ms": started + 3_600_000,
            "total_in": 0.97})
        # The live path already reported this storm, with its own stamp.
        await db.insert_report(
            kind=rp.KIND_STORM, mac=MAC, ts_ms=started + 5_400_000,
            for_date="2026-09-03", title="LIVE TITLE", summary="live",
            payload={"station": "Chaucer Drive"},
            dedupe=rp.storm_key(MAC, started))
        added = await alerts.backfill_storm_reports()
        rows = await db.list_reports(kind=rp.KIND_STORM)
        return added, rows
    added, rows = asyncio.run(run())
    assert added == 0
    assert len(rows) == 1 and rows[0]["title"] == "LIVE TITLE"
    assert rows[0]["ts_ms"] == 1_788_400_000_000 + 5_400_000


def test_backfill_is_wired_into_boot(client):
    """The lifespan schedules it; a test client boots the app, so the task
    handle exists (it sleeps 20 s before doing anything)."""
    from app.main import app
    task = getattr(app.state, "report_backfill_task", None)
    assert task is not None


# ── runnable NOAA kinds (item 13) ───────────────────────────────────────

@pytest.fixture
def insights_on(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    return client


def test_kinds_include_the_noaa_reports_and_are_self_described(client):
    r = client.get("/api/reports", headers=AUTH)
    assert r.json()["kinds"] == list(rp.KINDS)
    assert "noaa_month" in rp.KINDS and "noaa_year" in rp.KINDS
    assert set(rp.RUNNABLE_KINDS) <= set(rp.KINDS)


def test_run_stores_a_month_report_and_a_rerun_updates_its_row(insights_on):
    from app import db
    client = insights_on
    asyncio.run(db.upsert_device(MAC, {"name": "Chaucer Drive", "info": {}}))
    asyncio.run(_seed_rollups(db))
    r = client.post("/api/reports/run", headers=AUTH,
                    json={"kind": "noaa_month", "mac": MAC,
                          "year": 2025, "month": 10})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["kind"] == "noaa_month" and rep["mac"] == MAC
    assert rep["title"] == "Chaucer Drive · October 2025 climate report"
    p = rep["payload"]
    assert "MONTHLY CLIMATOLOGICAL SUMMARY for October 2025" in p["text"]
    assert p["days"] == 3 and p["high_f"] == 93.0 and p["low_f"] == 61.0
    assert p["high_day"] == "2025-10-03" and p["low_day"] == "2025-10-01"
    assert p["rain_in"] == pytest.approx(0.6)
    assert p["gust_mph"] == 23.0 and p["month"] == 10
    assert rep["summary"].startswith("High 93, low 61")
    # Run it again (the month is still in progress): same row, not a copy.
    r2 = client.post("/api/reports/run", headers=AUTH,
                     json={"kind": "noaa_month", "mac": MAC,
                           "year": 2025, "month": 10})
    assert r2.json()["report"]["id"] == rep["id"]
    assert asyncio.run(db.count_reports()) == 1
    listed = client.get("/api/reports?kind=noaa_month", headers=AUTH).json()
    assert [x["id"] for x in listed["reports"]] == [rep["id"]]


def test_run_stores_a_year_report(insights_on):
    from app import db
    client = insights_on
    asyncio.run(db.upsert_device(MAC, {"name": "Chaucer Drive", "info": {}}))
    asyncio.run(_seed_rollups(db, month=3))
    asyncio.run(_seed_rollups(db, month=7))
    r = client.post("/api/reports/run", headers=AUTH,
                    json={"kind": "noaa_year", "mac": MAC, "year": 2025,
                          "month": 4})       # ignored by the yearly kind
    assert r.status_code == 200, r.text
    p = r.json()["report"]["payload"]
    assert p["month"] is None and p["days"] == 6
    assert "YEARLY CLIMATOLOGICAL SUMMARY for 2025" in p["text"]
    assert p["high_f"] == 93.0 and p["rain_in"] == pytest.approx(1.2)
    assert r.json()["report"]["title"] == "Chaucer Drive · 2025 climate report"


def test_run_refuses_what_it_cannot_build(insights_on, monkeypatch):
    from app import db
    from app.config import settings
    client = insights_on
    asyncio.run(db.upsert_device(MAC, {"name": "Chaucer Drive", "info": {}}))
    assert client.post("/api/reports/run", headers=AUTH,
                       json={"kind": "morning", "mac": MAC, "year": 2025}
                       ).status_code == 400
    assert client.post("/api/reports/run", headers=AUTH,
                       json={"kind": "noaa_month", "mac": MAC, "year": 2025}
                       ).status_code == 400, "month is required"
    assert client.post("/api/reports/run", headers=AUTH,
                       json={"kind": "noaa_year", "mac": "00:00:00:00:00:99",
                             "year": 2025}).status_code == 404
    assert client.post("/api/reports/run",
                       json={"kind": "noaa_year", "mac": MAC, "year": 2025}
                       ).status_code == 401
    monkeypatch.setattr(settings, "insights", False)
    assert client.post("/api/reports/run", headers=AUTH,
                       json={"kind": "noaa_year", "mac": MAC, "year": 2025}
                       ).status_code == 404


def test_an_empty_period_is_a_report_that_says_so(insights_on):
    from app import db
    client = insights_on
    asyncio.run(db.upsert_device(MAC, {"name": "Chaucer Drive", "info": {}}))
    r = client.post("/api/reports/run", headers=AUTH,
                    json={"kind": "noaa_month", "mac": MAC,
                          "year": 2019, "month": 2})
    assert r.status_code == 200
    p = r.json()["report"]["payload"]
    assert p["days"] == 0 and p["high_f"] is None and p["rain_in"] is None
    assert r.json()["report"]["summary"] == "No rollup data for this period"


def test_noaa_keys_are_one_per_station_and_period():
    assert rp.noaa_key("noaa_month", MAC, 2025, 3) != rp.noaa_key("noaa_month", MAC, 2025, 4)
    assert rp.noaa_key("noaa_year", MAC, 2025, 3) == rp.noaa_key("noaa_year", MAC, 2025, None)
    assert rp.noaa_key("noaa_year", MAC, 2025, None) != rp.noaa_key("noaa_month", MAC, 2025, None)


def test_paging_across_a_same_millisecond_tie_returns_every_row_once(client):
    """Round-three review BE-F4 (round two T17): five reports at one
    timestamp with limit 2 paged `[5, 4]` and then nothing, because the
    cursor was `ts_ms <` alone. The keyset (`before_ms`, `before_id`)
    walks the tie; `before_ms` alone keeps its old meaning."""
    import asyncio
    from app import db
    ts = 1_780_000_000_000

    async def seed():
        for n in range(5):
            await db.insert_report(kind=rp.KIND_STORM, mac=MAC, ts_ms=ts, for_date="2026-06-01",
                                   title=f"Storm {n}", summary="s", payload={"n": n},
                                   dedupe=f"storm:tie:{n}")
    asyncio.run(seed())
    seen, before_ms, before_id = [], None, None
    for _ in range(10):
        q = "/api/reports?limit=2"
        if before_ms is not None:
            q += f"&before_ms={before_ms}&before_id={before_id}"
        page = client.get(q, headers=AUTH).json()["reports"]
        if not page:
            break
        seen.extend(r["id"] for r in page)
        before_ms, before_id = page[-1]["ts_ms"], page[-1]["id"]
    assert len(seen) == 5 and len(set(seen)) == 5, seen
    assert seen == sorted(seen, reverse=True)
    # The old cursor alone still pages by time.
    assert client.get(f"/api/reports?before_ms={ts}", headers=AUTH).json()["reports"] == []
    assert len(client.get(f"/api/reports?before_ms={ts + 1}", headers=AUTH)
               .json()["reports"]) == 5
