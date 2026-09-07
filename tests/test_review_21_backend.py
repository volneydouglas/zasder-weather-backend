"""2.1 pre-release review, backend correctness (§4.4 and SEC-9).

Each test names the finding it pins and failed against the code before
the fix.
"""
from __future__ import annotations

import asyncio
import re
import time

import pytest

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:00:00:C1"


# ───────────── BE-1: yearly-counter stations are not dry ─────────────

def test_day_rain_is_the_yearly_delta_when_no_daily_total_and_none_when_no_gauge():
    from app.day_rain import day_rain_in, sum_or_none
    assert day_rain_in({"rain_total": 0.3, "yearly_min": 1.0, "yearly_max": 9.0}) == 0.3
    assert day_rain_in({"rain_total": None, "yearly_min": 1.0, "yearly_max": 1.25,
                        "day": "2025-10-05"}) == pytest.approx(0.25)
    # January 1 is a day like any other for a lifetime counter (round-two
    # review BE-N4): the old skip-Jan-1 rule dropped a real day. What is
    # refused is an implausible rise — a counter swapped inside the day.
    assert day_rain_in({"rain_total": None, "yearly_min": 16.0, "yearly_max": 18.0,
                        "day": "2026-01-01"}) == pytest.approx(2.0)
    assert day_rain_in({"rain_total": None, "yearly_min": 0.0, "yearly_max": 118.0,
                        "day": "2026-01-01"}) is None
    assert day_rain_in({"rain_total": None, "yearly_min": None, "yearly_max": None}) is None
    assert sum_or_none([None, None]) is None
    assert sum_or_none([None, 0.1, 0.2]) == pytest.approx(0.3)


def test_climate_reports_read_the_yearly_counter_and_dash_the_gaugeless(client, monkeypatch):
    """The repo's own LilyGO relay sends only `yearly_in`; the NOAA report
    printed 0.00 in on every line and total for it (BE-1)."""
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")

    async def seed():
        async with db.connect() as conn:
            # A yearly-only station: three days, the counter climbing.
            for day, lo, hi in (("2025-10-05", 1.0, 1.25), ("2025-10-06", 1.25, 1.25),
                                ("2025-10-07", 1.25, 2.0)):
                await conn.execute(
                    "INSERT INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                    "tempf_sum, tempf_n, rain_total, yearly_min, yearly_max) "
                    "VALUES (?, ?, 60, 80, 140, 2, NULL, ?, ?)", (MAC, day, lo, hi))
            # A station with no gauge at all.
            await conn.execute(
                "INSERT INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                "tempf_sum, tempf_n) VALUES (?, '2025-10-05', 60, 80, 140, 2)",
                ("AA:BB:CC:00:00:C2",))
            await conn.commit()
    asyncio.run(seed())

    days = asyncio.run(climate.month_days(MAC, 2025, 10))
    assert [d["rain"] for d in days] == pytest.approx([0.25, 0.0, 0.75])
    nums = asyncio.run(climate.noaa_month_numbers(MAC, 2025, 10))
    assert nums["rain_in"] == pytest.approx(1.0)
    text = asyncio.run(climate.noaa_month_report(MAC, "LilyGO", 2025, 10))
    assert "0.25" in text and "0.75" in text and "1.00" in text

    gaugeless = asyncio.run(climate.month_days("AA:BB:CC:00:00:C2", 2025, 10))
    assert gaugeless[0]["rain"] is None
    nums = asyncio.run(climate.noaa_month_numbers("AA:BB:CC:00:00:C2", 2025, 10))
    assert nums["rain_in"] is None, "no gauge is unknown, not 0.00"
    year = asyncio.run(climate.year_summary("AA:BB:CC:00:00:C2", 2025))
    assert year["totals"]["rain"] is None
    text = asyncio.run(climate.noaa_month_report("AA:BB:CC:00:00:C2", "Bare", 2025, 10))
    assert "0.00" not in text


# ───────────── BE-6: the scorecard compares one population ─────────────

def test_scorecard_comparison_uses_the_slide_rule_on_the_models_days(client, monkeypatch):
    """value and baseline must describe the same days: the slide rule's
    rate on ALL scored days beside the model's rate on ITS days invented a
    delta between two seasons (BE-6)."""
    from datetime import date, datetime, timedelta, timezone
    from app import climate, db, stories, zambretti_ledger as zl
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    today = date(2026, 8, 30)
    monkeypatch.setattr(climate, "local_today", lambda: today)

    def ms(on, hour):
        return int(datetime(on.year, on.month, on.day, hour, tzinfo=timezone.utc)
                   .timestamp() * 1000)

    async def seed():
        async with db.connect() as conn:
            await zl._ensure_table(conn)
            # 40 days: the slide rule says rain on every third day and it
            # rains on every third day, so it is right every day overall.
            # The model has a forecast on file for the first 20 days only,
            # and it is WRONG on every one of them.
            for i in range(40):
                on = today - timedelta(days=40 - i)
                rained = (i % 3) == 0
                await conn.execute(
                    "INSERT OR IGNORE INTO zambretti_calls (mac, day, issued_ms, "
                    "slp_inhg, trend, call) VALUES (?, ?, ?, 29.9, 'steady', ?)",
                    (MAC, on.isoformat(), ms(on, 9),
                     "Rain at times, worse later" if rained else "Fine weather"))
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, "
                    "tempf_max, tempf_sum, tempf_n, rain_total) VALUES "
                    "(?, ?, 70, 90, 160, 2, ?)", (MAC, on.isoformat(),
                                                    0.25 if rained else 0.0))
            await conn.commit()
        for i in range(20):
            on = today - timedelta(days=40 - i)
            rained = (i % 3) == 0
            await db.insert_forecast_snapshots(
                stories.FORECAST_PROVIDER, ms(on, 6),
                [{"valid_date": on.isoformat(), "lead_days": 0, "tmax_f": 90.0,
                  "tmin_f": 70.0, "pop": 10.0 if rained else 90.0, "precip_in": 0.0}])
    asyncio.run(seed())
    out = asyncio.run(stories.top_stories(MAC, families=[stories.FAMILY_SCIENCE],
                                          limit=12, min_score=0.0))
    card = next(s for s in out["stories"] if s["story_type"] == "barometer_scorecard")
    stats = {s["key"]: s["value"] for s in card["supporting"]}
    assert stats["days"] == 40 and stats["modern_days"] == 20
    cmp = card["comparison"]
    # On the model's 20 days the slide rule was right 100%, the model 0%.
    assert cmp["value"] == 100.0 and cmp["baseline"] == 0.0
    assert cmp["direction"] == "above" and cmp["delta"] == 100.0
    assert card["hero"]["value"] == 100


# ───────────── SEC-9: a control character in a posted name ─────────────

def test_a_name_with_a_carriage_return_is_dropped_not_stored(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "prometheus_metrics", True)
    body = {"device": {"id": "AABBCC0000C3", "name": "Evil\r\nStation"},
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outdoor": {"tempf": 75.0, "humidity": 40},
            "wind": {}, "rain": {}, "pressure": {"relative_inhg": 29.9}}
    r = client.post("/ingest/custom", headers=IH, json=body)
    assert r.status_code == 200, "the reading is never refused for its name"
    devs = client.get("/api/devices", headers=H).json()
    dev = next(d for d in devs if d["mac"].endswith("00:C3"))
    assert "\r" not in (dev.get("name") or "") and "\n" not in (dev.get("name") or "")
    # And the escaper itself, for a name that is already stored somewhere:
    from app import metrics
    assert "\r" not in metrics._esc_label("a\rb\nc")
    text = client.get("/metrics").text
    assert "\r" not in text


# ───────────── BE-9: a partial account failure is a failure ─────────────

def test_ecowitt_one_station_failing_reports_the_failure_and_keeps_the_rows(temp_env, monkeypatch):
    import importlib, sys
    for mod in ("app.config", "app.db", "app.ingest", "app.ecowitt_cloud_poller"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from app import db, source_status
    from app.ecowitt_cloud_poller import EcowittCloudPoller, SOURCE
    from tests.test_ecowitt_cloud import FakeClient, real_time_data
    asyncio.run(db.init_db())

    class OneBad(FakeClient):
        async def real_time(self, mac):
            if mac == "AA:BB:CC:DD:EE:02":
                raise RuntimeError("gateway offline")
            return await super().real_time(mac)

    good = "AA:BB:CC:DD:EE:01"
    vendor = OneBad({good: real_time_data()},
                    devices=[{"mac": good, "name": "Good"},
                             {"mac": "AA:BB:CC:DD:EE:02", "name": "Bad"}])
    poller = EcowittCloudPoller(vendor, 60, None)

    async def run():
        await poller._discover()
        await poller._tick()
        return next(s for s in source_status.snapshot() if s["name"] == SOURCE)
    st = asyncio.run(run())
    assert st["last_error"], "one dead station vanished behind a clean success"
    assert "1/2" in st["last_error"] and "gateway offline" in st["last_error"]


def test_govee_partial_failure_is_reported_and_the_error_is_never_unbound(temp_env, monkeypatch):
    import importlib, sys
    for mod in ("app.config", "app.db", "app.ingest", "app.govee_cloud_poller"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from app import db, source_status
    from app import govee_cloud_poller as gcp
    from tests.test_govee_cloud import FakeClient, LISTING, DEV_ID, state
    asyncio.run(db.init_db())

    async def fake_ingest(payload):
        return {"ok": True, "inserted": 1}
    monkeypatch.setattr(gcp.ingest, "_do_ingest", fake_ingest)
    listings = [LISTING, {**LISTING, "device": "BB:22", "deviceName": "Hall"}]
    client = FakeClient(listings, {DEV_ID: state()})   # BB:22 has no state: KeyError
    poller = gcp.GoveeCloudPoller(client, 60, None, None)

    async def run():
        await poller._discover()
        await poller._tick()
        return next(s for s in source_status.snapshot() if s["name"] == gcp.SOURCE)
    st = asyncio.run(run())
    assert st["last_error"] and "1/2" in st["last_error"]


# ───────────── BE-7: a cloudburst rate passes the band ─────────────

def test_a_forty_inch_per_hour_rate_is_kept_and_garbage_is_not():
    from app import ingest
    lo, hi = ingest._PLAUSIBLE_BANDS["hourlyrainin"]
    assert lo == 0.0 and hi >= 40.0, "a cloudburst peak rate must survive the band"
    assert hi < 1000.0, "a 3276.7 bit-flip must not"


# ───────────── BE-10 ─────────────

def test_an_exactly_met_catch_up_budget_is_not_overflow(engine_staging, monkeypatch):
    """The catch-up folded exactly its budget and marked the ledger dirty
    although nothing was left behind (off-by-one on `budget <= 0`)."""
    from app import insights
    from tests.test_rebuild_staging import _seed, _ms, MAC as M1, OTHER
    engine = engine_staging
    monkeypatch.setattr(insights, "REBUILD_BATCH_ROWS", 2)
    monkeypatch.setattr(insights, "REBUILD_CATCHUP_MAX", 3)
    state = {"first": None, "done": False, "loop": None}

    async def inject(_s):
        if asyncio.get_running_loop() is not state["loop"]:
            return
        p = insights._PROGRESS or {}
        if p.get("phase") != "fold":
            return
        if state["first"] is None:
            state["first"] = p["mac"]
        elif p["mac"] != state["first"] and not state["done"]:
            state["done"] = True
            await engine.insert_observations(state["first"], [
                {"dateutc": _ms(2025, 10, 9, h), "tempf": 80.0}
                for h in (9, 10, 11)])                 # exactly the budget
    monkeypatch.setattr(insights.asyncio, "sleep", inject)

    async def run():
        state["loop"] = asyncio.get_running_loop()
        await _seed(engine, M1)
        await _seed(engine, OTHER)
        await insights.rebuild()
        return await engine.get_kv("rollups_dirty")
    dirty = asyncio.run(run())
    assert state["done"]
    assert dirty is None, "three rows against a budget of three is complete, not overflow"


@pytest.fixture
def engine_staging(client, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "America/Phoenix")
    return db


def test_an_unreadable_event_time_is_not_a_recent_oom():
    from app import machine_advice as MA
    now = int(time.time() * 1000)
    assert MA._oom_recent({"events": [{"timestamp": "garbage", "oom_killed": True}]}, now) is False
    assert MA._oom_recent({"events": [{"timestamp": now - 1000, "oom_killed": True}]}, now) is True


def test_capture_redacts_the_ecowitt_passkey_and_writes_off_the_loop(client, temp_env):
    from pathlib import Path
    from app import capture
    assert capture._redact_body("PASSKEY=ABCDEF0123&stationtype=GW3000&tempf=71.2") \
        == "PASSKEY=<redacted>&stationtype=GW3000&tempf=71.2"
    assert capture._redact_body('{"PASSKEY": "ABCDEF", "tempf": 71}') \
        == '{"PASSKEY": "<redacted>", "tempf": 71}'
    r = client.post("/ingest/capture/gw-test",
                    headers={"Authorization": "Bearer test-capture-token",
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data="PASSKEY=SECRET123&tempf=71.2")
    assert r.status_code == 200
    logged = (Path(temp_env).parent / "captures" / "gw-test.jsonl").read_text()
    assert "SECRET123" not in logged and "<redacted>" in logged


def test_reports_written_in_the_same_millisecond_list_in_a_stable_order(client):
    from app import db, reports as rp
    ts = 1_780_000_000_000

    async def run():
        ids = []
        for i in range(3):
            ids.append(await db.insert_report(
                kind=rp.KIND_STORM, mac=MAC, ts_ms=ts, for_date="2026-06-01",
                title=f"storm {i}", summary=None, payload={"i": i},
                dedupe=f"storm:{MAC}:{i}"))
        rows = await db.list_reports(limit=10)
        return ids, [r["id"] for r in rows]
    ids, listed = asyncio.run(run())
    assert listed == sorted(ids, reverse=True)


def test_the_storm_backfill_opens_a_fixed_number_of_connections(client, monkeypatch):
    from app import alerts, db
    opened = {"n": 0}
    real_connect = db.connect

    def counting_connect():
        opened["n"] += 1
        return real_connect()
    monkeypatch.setattr(db, "connect", counting_connect)

    async def run():
        for i in range(5):
            await db.record_storm(MAC, {
                "started_ms": 1_770_000_000_000 + i * 3_600_000,
                "ended_ms": 1_770_000_000_000 + i * 3_600_000 + 600_000,
                "total_in": 0.2, "peak_rate_in_hr": 1.0, "max_gust_mph": 20.0,
                "min_tempf": 60.0, "max_tempf": 70.0})
        opened["n"] = 0
        added = await alerts.backfill_storm_reports()
        return added, opened["n"]
    added, n = asyncio.run(run())
    assert added == 5
    # Eight, none of them per storm: the backfill-done flag read, the
    # device list, the storm history, ONE batch insert for every pending
    # report (BE-10), the retention pair, the prune, and the flag write. A
    # ninth means a storm row started opening its own. (Round two, I4: the
    # old `<= 8` under a name that claimed "one" could not tell.)
    assert n == 8, f"the backfill opened {n} connections for five storms"


def test_the_two_relays_agree_on_the_lux_divisor():
    """Monorepo only: the SDR relay is private and the public mirror
    flattens backend/ to its root, so neither path resolves there. Skip
    rather than fail the mirror's CI (2026-09-06)."""
    import pytest
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    sdr_path = root / "sdr-relay" / "sdr_relay.py"
    cpp_path = root / "lilygo-relay" / "src" / "zasder_post.cpp"
    if not (sdr_path.exists() and cpp_path.exists()):
        pytest.skip("relay sources are not beside this checkout (public mirror)")
    sdr = sdr_path.read_text()
    cpp = cpp_path.read_text()
    assert "lux / 126.7" in sdr and "lux / 126.7f" in cpp
    assert "126.0" not in sdr
