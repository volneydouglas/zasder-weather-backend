"""Every forecast source, graded against the yard (2.7).

The archive has held TWC beside Open-Meteo since 2.6, and three of
Open-Meteo's individual models since 2.7. These tests pin the collector
(one models request a beat, a model missing a field, a refused TWC key
backed off and never a crash), the migration from a database that
predates every 2.6/2.7 change, the grader's parity with the pre-2.7
scorecard on the same data, per-source scoring on a mixed archive, the
minimum-days rule, the best-source rule and the endpoint's additive shape.

No test reaches the network: the fetchers are stubbed by conftest, and
where a test wants the real fetcher back it runs against an
httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from tests.forecast_sources_fixture import HERE, MAC, TODAY, build  # noqa: E402

AUTH = {"Authorization": "Bearer test-api-token"}
GOLDEN = Path(__file__).parent / "fixtures" / "forecast_skill_golden.json"
DEVICES = [{"mac": "AA", "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]


@pytest.fixture()
def box(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)

    async def seed():
        await db.upsert_device(MAC, {"name": "Yard", "info": {
            "coords": {"coords": {"lat": HERE[0], "lon": HERE[1]}}}})
    asyncio.run(seed())
    return db


@pytest.fixture()
def mixed(box):
    """The untidy three-source archive the golden file was captured on."""
    asyncio.run(build(box))
    return box


# ── The collector ────────────────────────────────────────────────────────

def _models_body(drop: dict[str, list[str]] | None = None) -> dict:
    """A multi-model daily block as Open-Meteo answers it (key suffixed by
    model). `drop` removes fields per model."""
    drop = drop or {}
    times = ["2026-10-15", "2026-10-16", "2026-10-17"]
    daily: dict = {"time": times}
    base = {"gfs_seamless": 80.0, "ecmwf_ifs025": 81.0, "icon_seamless": 82.0}
    for model, hi in base.items():
        cols = {"temperature_2m_max": [hi, hi + 1, hi + 2],
                "temperature_2m_min": [60.0, 61.0, None],
                "precipitation_probability_max": [0, 40, 70],
                "precipitation_sum": [0.0, 0.02, 0.3]}
        for field, vals in cols.items():
            if field not in drop.get(model, []):
                daily[f"{field}_{model}"] = vals
    return {"daily": daily}


def _mock_httpx(monkeypatch, module, handler):
    """Point `module.httpx.AsyncClient` at a MockTransport, for that
    module only."""
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(module, "httpx", SimpleNamespace(
        AsyncClient=factory, HTTPStatusError=httpx.HTTPStatusError))


async def _rows(db):
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT provider, valid_date, lead_days, tmax_f, tmin_f, pop, precip_in "
            "FROM forecast_snapshots ORDER BY provider, valid_date")).fetchall()
    return [tuple(r) for r in rows]


def test_conftest_keeps_every_fetcher_off_the_network(client):
    """The models fetcher is a third live call on the alert tick; a test
    that forgot it would reach Open-Meteo (it did, while this was built)."""
    from app import forecast_snapshots as fs
    assert fs._fetch_models is not fs._fetch_models_live
    assert fs._fetch_twc is not fs._fetch_twc_live


def test_one_request_archives_every_model_and_a_missing_field_is_null(client, monkeypatch, caplog):
    from app import db, forecast_snapshots as fs
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # ecmwf files no probability; icon files no temperature at all.
        return httpx.Response(200, json=_models_body({
            "ecmwf_ifs025": ["precipitation_probability_max"],
            "icon_seamless": ["temperature_2m_max", "temperature_2m_min"]}))
    _mock_httpx(monkeypatch, fs, handler)
    monkeypatch.setattr(fs, "_fetch_models", fs._fetch_models_live)
    caplog.set_level(logging.INFO, logger="forecast-snap")
    asyncio.run(fs.check(DEVICES, int(time.time() * 1000)))

    assert len(seen) == 1                       # ONE request for three models
    q = seen[0].url.params
    assert q["models"] == "gfs_seamless,ecmwf_ifs025,icon_seamless"
    assert q["temperature_unit"] == "fahrenheit" and q["precipitation_unit"] == "inch"
    assert "precipitation_probability_max" in q["daily"]

    rows = asyncio.run(_rows(db))
    by = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    assert set(by) == {"open-meteo-gfs", "open-meteo-ecmwf"}   # icon had no temps
    assert [r[3] for r in by["open-meteo-gfs"]] == [80.0, 81.0, 82.0]
    assert [r[5] for r in by["open-meteo-gfs"]] == [0.0, 40.0, 70.0]
    # A missing probability is NULL, not 0 % (absent is not zero), and the
    # temperatures beside it are still archived.
    assert [r[5] for r in by["open-meteo-ecmwf"]] == [None, None, None]
    assert [r[3] for r in by["open-meteo-ecmwf"]] == [81.0, 82.0, 83.0]
    assert by["open-meteo-gfs"][2][4] is None   # the null low stays null
    # One line says which sources archived.
    line = [r.getMessage() for r in caplog.records if "forecast archive:" in r.getMessage()]
    assert len(line) == 1
    assert "open-meteo-gfs=3" in line[0] and "open-meteo-ecmwf=3" in line[0]
    assert "open-meteo-icon=skipped" in line[0] and "twc=skipped(no key)" in line[0]


@pytest.mark.parametrize("status", [429, 500])
def test_an_upstream_refusal_skips_the_models_without_raising(client, monkeypatch, status):
    from app import db, forecast_snapshots as fs

    def handler(request):
        return httpx.Response(status, json={"error": True, "reason": "x"})
    _mock_httpx(monkeypatch, fs, handler)
    monkeypatch.setattr(fs, "_fetch_models", fs._fetch_models_live)
    asyncio.run(fs.check(DEVICES, int(time.time() * 1000)))
    assert asyncio.run(_rows(db)) == []


def test_a_malformed_models_answer_files_nothing():
    from app import forecast_snapshots as fs
    assert fs._split_models(None) == {}
    assert fs._split_models({"time": "nope"}) == {}
    # Unsuffixed keys (a single-model answer) are not any model's.
    assert fs._split_models({"time": ["2026-10-15"], "temperature_2m_max": [80]}) == {}


def _twc_401(monkeypatch, status=401):
    from app import forecast_twc
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"errors": [{"error": {"code": "CPF-0001"}}]})
    _mock_httpx(monkeypatch, forecast_twc, handler)
    return calls


def test_a_refused_twc_key_is_skipped_quietly_and_backed_off(client, monkeypatch, caplog):
    from app import db, forecast_snapshots as fs
    key = "k" * 32
    asyncio.run(db.set_kv("wu_api_key", key))
    monkeypatch.setattr(fs, "_fetch_twc", fs._fetch_twc_live)
    calls = _twc_401(monkeypatch)
    caplog.set_level(logging.INFO)
    now = int(time.time() * 1000)

    asyncio.run(fs.check(DEVICES, now))          # no exception
    assert len(calls) == 1
    assert asyncio.run(fs.twc_status(now)) == fs.STATUS_KEY_REFUSED
    assert [r[0] for r in asyncio.run(_rows(db))] == []
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP 401" in text and "twc=skipped(key refused)" in text
    assert key not in text                        # the key rides the URL
    assert key not in (asyncio.run(db.get_kv(fs._KV_TWC_REFUSED)) or "")

    # The next beat does not ask again.
    asyncio.run(db.set_kv(fs._KV_LAST, "0"))
    asyncio.run(fs.check(DEVICES, now + fs._INTERVAL_MS))
    assert len(calls) == 1

    # A new key is tried at once.
    asyncio.run(db.set_kv("wu_api_key", "n" * 32))
    asyncio.run(db.set_kv(fs._KV_LAST, "0"))
    asyncio.run(fs.check(DEVICES, now + 2 * fs._INTERVAL_MS))
    assert len(calls) == 2


def test_the_back_off_ends_and_a_working_key_clears_it(client, monkeypatch):
    from app import db, forecast_snapshots as fs, forecast_twc
    key = "k" * 32
    asyncio.run(db.set_kv("wu_api_key", key))
    monkeypatch.setattr(fs, "_fetch_twc", fs._fetch_twc_live)
    then = int(time.time() * 1000) - fs.TWC_REFUSED_BACKOFF_MS - 1
    asyncio.run(db.set_kv(fs._KV_TWC_REFUSED, json.dumps(
        {"ms": then, "key": fs._key_mark(key), "status": 401})))
    assert asyncio.run(fs.twc_status()) == fs.STATUS_ARCHIVING   # back-off over

    async def ok(lat, lon, api_key):
        return {"daily": {"time": ["2026-10-15"], "temperature_2m_max": [90.0],
                          "temperature_2m_min": [70.0]}}
    monkeypatch.setattr(forecast_twc, "fetch", ok)
    asyncio.run(fs.check(DEVICES, int(time.time() * 1000)))
    assert asyncio.run(db.get_kv(fs._KV_TWC_REFUSED)) is None
    assert [r[0] for r in asyncio.run(_rows(db))] == ["twc"]


def test_a_twc_outage_is_not_a_refused_key(client, monkeypatch):
    """Only 401/403 back off: a 500 is WU having a bad hour, and the next
    beat should try again."""
    from app import db, forecast_snapshots as fs
    asyncio.run(db.set_kv("wu_api_key", "k" * 32))
    monkeypatch.setattr(fs, "_fetch_twc", fs._fetch_twc_live)
    calls = _twc_401(monkeypatch, status=500)
    now = int(time.time() * 1000)
    asyncio.run(fs.check(DEVICES, now))
    assert asyncio.run(fs.twc_status(now)) == fs.STATUS_ARCHIVING
    asyncio.run(db.set_kv(fs._KV_LAST, "0"))
    asyncio.run(fs.check(DEVICES, now + fs._INTERVAL_MS))
    assert len(calls) == 2


# ── Migration from the old schema ────────────────────────────────────────

def test_a_18_era_archive_migrates_and_grades_as_open_meteo(box):
    """A forecast_snapshots table as 1.8 created it (no sky columns, no
    issue index) with rows filed then: init_db brings it forward, and the
    old rows are Open-Meteo's, graded and dated in the sources block."""
    db = box
    first = TODAY - timedelta(days=30)

    async def go():
        async with db.connect() as conn:
            await conn.execute("DROP TABLE forecast_snapshots")
            await conn.execute("""CREATE TABLE forecast_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                issued_ms INTEGER NOT NULL, valid_date TEXT NOT NULL,
                lead_days INTEGER NOT NULL, tmax_f REAL, tmin_f REAL, pop REAL,
                precip_in REAL)""")
            for i in range(30):
                valid = first + timedelta(days=i)
                for lead in (1, 2):
                    issued = valid - timedelta(days=lead)
                    ms = int(time.mktime(issued.timetuple()) * 1000) + 12 * 3_600_000
                    await conn.execute(
                        "INSERT INTO forecast_snapshots (provider, issued_ms, valid_date, "
                        "lead_days, tmax_f, tmin_f, pop) VALUES ('open-meteo', ?, ?, ?, ?, ?, ?)",
                        (ms, valid.isoformat(), lead, 100.0, 70.0, 10.0))
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                    "rain_total, obs_first_ms, obs_last_ms) VALUES (?,?,?,?,?,?,?)",
                    (MAC, valid.isoformat(), 68.0, 97.0, 0.0, 0, 23 * 3_600_000))
            await conn.commit()
        await db.init_db()
        async with db.connect() as conn:
            cols = {r[1] for r in await (await conn.execute(
                "PRAGMA table_info(forecast_snapshots)")).fetchall()}
            idx = {r[1] for r in await (await conn.execute(
                "PRAGMA index_list(forecast_snapshots)")).fetchall()}
        from app import forecast_skill
        body = await forecast_skill.accuracy(MAC, days=90, today=TODAY)
        return cols, idx, body
    cols, idx, body = asyncio.run(go())
    assert {"lat", "lon"} <= cols and "idx_fsnap_issued" in idx
    items = {s["id"]: s for s in body["sources"]["items"]}
    om = items["open-meteo"]
    assert om["available"] and om["enough"] and om["days_until_enough"] == 0
    assert om["collecting_since"] == (first - timedelta(days=2)).isoformat()
    lead1 = {r["lead_days"]: r for r in om["leads"]}[1]
    assert lead1["n"] == 30 and lead1["high"]["bias_f"] == 3.0
    assert body["leads"] == om["leads"]       # the top level is still Open-Meteo's
    for sid in ("twc", "open-meteo-gfs", "open-meteo-ecmwf", "open-meteo-icon"):
        assert items[sid]["available"] is False and items[sid]["runs"] == 0
        assert items[sid]["collecting_since"] is None


# ── Grading parity ───────────────────────────────────────────────────────

def test_the_refactored_grader_matches_the_pre_27_scorecard(mixed):
    """fixtures/forecast_skill_golden.json was written by the 2.6
    scorecard on this exact archive, before the grader learned to take a
    source; every provider and window must come out byte for byte."""
    from app import forecast_skill as fsk
    golden = json.loads(GOLDEN.read_text())

    async def go():
        out = {}
        for key in golden:
            p, days = key.split("|")
            out[key] = await fsk.scorecard(MAC, provider=p, days=int(days), today=TODAY)
        return out
    assert json.loads(json.dumps(asyncio.run(go()))) == golden


def test_accuracy_is_the_scorecard_plus_sources(mixed):
    from app import forecast_skill as fsk
    golden = json.loads(GOLDEN.read_text())
    for provider in ("open-meteo", "twc", "open-meteo-gfs"):
        body = json.loads(json.dumps(asyncio.run(
            fsk.accuracy(MAC, provider=provider, days=90, today=TODAY))))
        sources = body.pop("sources")
        assert body == golden[f"{provider}|90"]
        items = {s["id"]: s for s in sources["items"]}
        # Every source's leads ARE that source's scorecard leads.
        for sid in ("open-meteo", "twc", "open-meteo-gfs"):
            assert items[sid]["leads"] == golden[f"{sid}|90"]["leads"]


# ── Per-source scoring on a mixed archive ────────────────────────────────

def test_each_source_is_scored_on_its_own_calls(mixed):
    from app import forecast_skill as fsk
    body = asyncio.run(fsk.accuracy(MAC, days=90, today=TODAY, selected="twc"))
    src = body["sources"]
    items = {s["id"]: s for s in src["items"]}
    assert [s["id"] for s in src["items"]] == [
        "open-meteo", "twc", "open-meteo-gfs", "open-meteo-ecmwf", "open-meteo-icon"]
    assert src["selected"] == "twc"
    assert [s["id"] for s in src["items"] if s["selected"]] == ["twc"]
    # TWC files leads 0-4; its leads 5 and 6 have nothing, while
    # Open-Meteo's do. Nothing bleeds across providers.
    twc = {r["lead_days"]: r for r in items["twc"]["leads"]}
    om = {r["lead_days"]: r for r in items["open-meteo"]["leads"]}
    assert twc[5]["n"] == 0 and om[5]["n"] > 0
    assert twc[1]["high"]["mae_f"] != om[1]["high"]["mae_f"]
    assert items["twc"]["max_lead_days"] == 4
    # GFS ran warm by design in the fixture; its bias says so.
    gfs1 = {r["lead_days"]: r for r in items["open-meteo-gfs"]["leads"]}[1]
    assert gfs1["high"]["bias_f"] > om[1]["high"]["bias_f"]
    # A source never archived here reports collecting, not zeros.
    icon = items["open-meteo-icon"]
    assert icon["available"] is False and icon["runs"] == 0
    assert icon["status"] == "archiving"
    assert icon["days_until_enough"] == fsk.MIN_DAYS + 2
    # TWC without a key on this server says so (the archive above was
    # filed by hand).
    assert items["twc"]["status"] == "no_key"
    assert items["twc"]["days_until_enough"] == 0      # it has enough already


def test_collecting_since_ignores_runs_for_another_sky(box):
    from app import forecast_skill as fsk
    db = box
    far_ms = int(time.mktime((TODAY - timedelta(days=40)).timetuple()) * 1000)
    here_ms = int(time.mktime((TODAY - timedelta(days=5)).timetuple()) * 1000)
    row = [{"valid_date": TODAY.isoformat(), "lead_days": 1, "tmax_f": 90.0, "tmin_f": 70.0}]
    asyncio.run(db.insert_forecast_snapshots("open-meteo-gfs", far_ms, row, lat=40.0, lon=-105.0))
    asyncio.run(db.insert_forecast_snapshots("open-meteo-gfs", here_ms, row, lat=HERE[0], lon=HERE[1]))
    body = asyncio.run(fsk.accuracy(MAC, days=90, today=TODAY))
    gfs = {s["id"]: s for s in body["sources"]["items"]}["open-meteo-gfs"]
    assert gfs["collecting_since_ms"] == here_ms and gfs["runs"] == 1
    assert gfs["last_archived_ms"] == here_ms


# ── The minimum-days rule ────────────────────────────────────────────────

async def _yard(db, n: int, *, providers: dict[str, float], lead_bias=None,
                start_offset: int = 0,
                low_bias: dict[str, float] | None = None) -> None:
    """n measured days ending yesterday (100/70), and each provider's
    lead-1 and lead-2 calls off by its bias on every one of them (the low
    by `low_bias[provider]` instead, when given)."""
    for i in range(n, 0, -1):
        d = TODAY - timedelta(days=i)
        async with db.connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                "rain_total, obs_first_ms, obs_last_ms) VALUES (?,?,?,?,?,?,?)",
                (MAC, d.isoformat(), 70.0, 100.0, 0.0, 0, 23 * 3_600_000))
            await conn.commit()
        for provider, bias in providers.items():
            for lead in (1, 2):
                issued = d - timedelta(days=lead)
                ms = int(time.mktime(issued.timetuple()) * 1000) + 12 * 3_600_000
                await db.insert_forecast_snapshots(provider, ms, [{
                    "valid_date": d.isoformat(), "lead_days": lead,
                    "tmax_f": 100.0 + bias,
                    "tmin_f": 70.0 - (low_bias or {}).get(provider, bias), "pop": 0.0}],
                    lat=HERE[0], lon=HERE[1])


@pytest.mark.parametrize("n,enough", [(9, False), (10, True)])
def test_a_source_needs_min_days_at_leads_1_and_2(box, n, enough):
    from app import forecast_skill as fsk
    assert fsk.MIN_DAYS == 10
    asyncio.run(_yard(box, n, providers={"open-meteo": 1.0, "open-meteo-gfs": 2.0}))
    src = asyncio.run(fsk.accuracy(MAC, days=90, today=TODAY))["sources"]
    items = {s["id"]: s for s in src["items"]}
    for sid in ("open-meteo", "open-meteo-gfs"):
        assert items[sid]["available"] is True
        assert items[sid]["enough"] is enough
    if enough:
        assert items["open-meteo"]["days_until_enough"] == 0
        assert src["best"]["source"] == "open-meteo" and src["best_reason"] is None
    else:
        # Too few days: no ranking, and "check back" in the days it lacks.
        assert src["best"] is None and src["best_reason"] == "too_few_sources"
        assert items["open-meteo"]["days_until_enough"] == 1


def test_the_compared_high_and_low_are_each_their_own_side(box):
    """Asymmetric misses through the real grader: highs off by 4, lows by
    1. The head-to-head reports 4 as the high MAE and 1 as the low, never
    the other way round."""
    from app import forecast_skill as fsk
    asyncio.run(_yard(box, fsk.MIN_DAYS, providers={"open-meteo": 4.0, "open-meteo-gfs": 2.0},
                      low_bias={"open-meteo": -1.0, "open-meteo-gfs": 2.0}))
    best = asyncio.run(fsk.accuracy(MAC, days=90, today=TODAY))["sources"]["best"]
    compared = {c["source"]: c for c in best["compared"]}
    assert (compared["open-meteo"]["high_mae_f"], compared["open-meteo"]["low_mae_f"]) == (4.0, 1.0)
    assert (compared["open-meteo-gfs"]["high_mae_f"], compared["open-meteo-gfs"]["low_mae_f"]) == (2.0, 2.0)
    assert compared["open-meteo"]["score_f"] == 2.5 and best["source"] == "open-meteo-gfs"


def test_enough_at_lead_1_alone_is_not_enough(box):
    """Both leads of the rule must clear the floor: a source with twelve
    lead-1 days and nine lead-2 days is still collecting."""
    from app import forecast_skill as fsk
    db = box
    asyncio.run(_yard(db, 9, providers={"open-meteo-ecmwf": 1.0}))

    async def more():
        for i in (10, 11, 12):
            d = TODAY - timedelta(days=i)
            async with db.connect() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                    "rain_total, obs_first_ms, obs_last_ms) VALUES (?,?,?,?,?,?,?)",
                    (MAC, d.isoformat(), 70.0, 100.0, 0.0, 0, 23 * 3_600_000))
                await conn.commit()
            ms = int(time.mktime((d - timedelta(days=1)).timetuple()) * 1000)
            await db.insert_forecast_snapshots("open-meteo-ecmwf", ms, [{
                "valid_date": d.isoformat(), "lead_days": 1, "tmax_f": 101.0,
                "tmin_f": 69.0}], lat=HERE[0], lon=HERE[1])
    asyncio.run(more())
    items = {s["id"]: s for s in asyncio.run(
        fsk.accuracy(MAC, days=90, today=TODAY))["sources"]["items"]}
    ecmwf = items["open-meteo-ecmwf"]
    by = {r["lead_days"]: r["n"] for r in ecmwf["leads"]}
    assert (by[1], by[2]) == (12, 9)
    assert ecmwf["enough"] is False and ecmwf["days_until_enough"] == 1


def test_days_until_enough_takes_the_later_of_the_calendar_and_the_count():
    from app import forecast_skill as fsk
    from app.forecast_snapshots import STATUS_ARCHIVING, STATUS_KEY_REFUSED

    def leads(n1, n2):
        return [{"lead_days": 1, "n": n1}, {"lead_days": 2, "n": n2}]
    first = int(time.mktime(TODAY.timetuple()) * 1000) + 12 * 3_600_000   # started today
    # Started today: the first lead-2 call is gradable in three days, and
    # it takes MIN_DAYS of them.
    assert fsk.days_until_enough(leads(0, 0), first, TODAY, 90, STATUS_ARCHIVING) == 12
    # Started 20 days ago but the station only covered 4 of them: the count wins.
    long_ago = first - 20 * 86_400_000
    assert fsk.days_until_enough(leads(4, 4), long_ago, TODAY, 90, STATUS_ARCHIVING) == 6
    # A refused key will not fill; neither will a window under MIN_DAYS.
    assert fsk.days_until_enough(leads(0, 0), first, TODAY, 90, STATUS_KEY_REFUSED) is None
    assert fsk.days_until_enough(leads(0, 0), first, TODAY, 7, STATUS_ARCHIVING) is None
    assert fsk.days_until_enough(leads(10, 10), None, TODAY, 7, STATUS_KEY_REFUSED) == 0
    assert fsk.days_until_enough(leads(0, 0), None, TODAY, 90, STATUS_ARCHIVING) == 12


# ── The best-source rule ─────────────────────────────────────────────────

def _scored(errors: dict[str, float], lo: dict[str, float] | None = None):
    lo = lo if lo is not None else errors
    return {lead: {d: (e, lo[d]) for d, e in errors.items()} for lead in (1, 2)}


def _days(n, start=0):
    return [(date(2026, 8, 1) + timedelta(days=start + i)).isoformat() for i in range(n)]


def test_the_lowest_typical_miss_wins():
    from app.forecast_skill import best_source
    d = _days(12)
    best, why = best_source([("a", _scored({x: 2.0 for x in d})),
                             ("b", _scored({x: -1.0 for x in d}))])
    assert why is None and best["source"] == "b" and best["score_f"] == 1.0
    assert best["shared_days"] == [{"lead_days": 1, "n": 12}, {"lead_days": 2, "n": 12}]
    assert [c["source"] for c in best["compared"]] == ["a", "b"]
    assert best["compared"][0]["high_mae_f"] == 2.0


def test_a_high_and_low_are_both_in_the_score():
    """Score is the mean of high AND low MAE: a source perfect on highs and
    awful on lows does not win on highs alone."""
    from app.forecast_skill import best_source
    d = _days(12)
    best, _ = best_source([
        ("highs-only", _scored({x: 0.0 for x in d}, {x: 6.0 for x in d})),
        ("steady", _scored({x: 2.0 for x in d}, {x: 2.0 for x in d}))])
    assert best["source"] == "steady"
    assert best["compared"][0]["score_f"] == 3.0 and best["compared"][1]["score_f"] == 2.0


def test_a_tie_goes_to_the_source_listed_first():
    from app.forecast_skill import best_source
    d = _days(12)
    best, _ = best_source([("first", _scored({x: 1.0 for x in d})),
                           ("second", _scored({x: -1.0 for x in d}))])
    assert best["source"] == "first"


def test_one_source_is_not_a_ranking():
    from app.forecast_skill import best_source
    assert best_source([("only", _scored({x: 1.0 for x in _days(30)}))]) == (None, "too_few_sources")
    assert best_source([]) == (None, "too_few_sources")


def test_sources_are_compared_on_the_days_they_share():
    """A long-running source with a bad early stretch is compared with a
    newcomer on the newcomer's days only, where it was better."""
    from app.forecast_skill import best_source
    old = _days(40)
    new = old[-12:]
    veteran = {x: (8.0 if x not in new else 0.5) for x in old}   # 40 days, 6.x overall
    newcomer = {x: 1.0 for x in new}
    best, _ = best_source([("veteran", _scored(veteran)), ("newcomer", _scored(newcomer))])
    assert best["source"] == "veteran" and best["score_f"] == 0.5
    assert best["shared_days"][0]["n"] == 12


def test_a_late_source_is_left_out_when_overlap_is_short():
    """Three sources: two share 20 days, the third shares only 5 with them
    (it started late). The late one is left out, the other two compared."""
    from app.forecast_skill import best_source
    span = _days(25)
    a = {x: 2.0 for x in span[:20]}
    b = {x: 1.5 for x in span[:20]}
    late = {x: 0.1 for x in span[15:]}
    best, why = best_source([("a", _scored(a)), ("b", _scored(b)), ("late", _scored(late))])
    assert why is None and best["source"] == "b"
    assert [c["source"] for c in best["compared"]] == ["a", "b"]
    # Two sources that never overlapped enough: no best, and the reason.
    best, why = best_source([("a", _scored({x: 1.0 for x in _days(12)})),
                             ("z", _scored({x: 1.0 for x in _days(12, start=8)}))])
    assert best is None and why == "too_few_shared_days"



def test_a_viable_pair_is_not_broken_up_by_a_longer_running_third():
    """A and B share 12 scored days; C has 30 days of its own that overlap
    neither. Setting aside the fewest-days source first dropped A (then B)
    and reported too_few_shared_days with a comparable pair right there
    (CodeRabbit, PR #98). The largest group that shares MIN_DAYS wins."""
    from app.forecast_skill import best_source
    ab = _days(12)
    c = _days(30, start=40)
    best, why = best_source([("a", _scored({x: 2.0 for x in ab})),
                             ("b", _scored({x: 1.0 for x in ab})),
                             ("c", _scored({x: 0.1 for x in c}))])
    assert why is None and best["source"] == "b"
    assert [x["source"] for x in best["compared"]] == ["a", "b"]
    assert best["shared_days"] == [{"lead_days": 1, "n": 12}, {"lead_days": 2, "n": 12}]
    # Listed in any order, the same pair is found.
    best, why = best_source([("c", _scored({x: 0.1 for x in c})),
                             ("a", _scored({x: 2.0 for x in ab})),
                             ("b", _scored({x: 1.0 for x in ab}))])
    assert why is None and [x["source"] for x in best["compared"]] == ["a", "b"]


def test_more_sources_beat_more_shared_days_then_more_days_then_order():
    """Prefer the biggest comparison, then the one on the most shared
    days, then the sources listed first."""
    from app.forecast_skill import best_source
    span = _days(40)
    a = {x: 1.0 for x in span}                 # 40 days
    b = {x: 1.0 for x in span[:30]}            # 30, all shared with a
    c = {x: 1.0 for x in span[18:]}            # 22; a, b and c share 12
    best, _ = best_source([("a", _scored(a)), ("b", _scored(b)), ("c", _scored(c))])
    assert [x["source"] for x in best["compared"]] == ["a", "b", "c"]
    assert best["shared_days"][0]["n"] == 12
    # No triple shares enough: of the pairs, a+b share 30, a+c 22, b+c 12.
    c2 = {x: 1.0 for x in span[25:]}           # 15; a, b, c2 share 5
    best, _ = best_source([("a", _scored(a)), ("b", _scored(b)), ("c", _scored(c2))])
    assert [x["source"] for x in best["compared"]] == ["a", "b"]
    assert best["shared_days"][0]["n"] == 30
    # Two pairs on equal days: the one listed first.
    p, q = _days(12), _days(12, start=20)
    best, _ = best_source([("x", _scored({d: 1.0 for d in p})),
                           ("y", _scored({d: 1.0 for d in q})),
                           ("z", _scored({d: 1.0 for d in p})),
                           ("w", _scored({d: 1.0 for d in q}))])
    assert [x["source"] for x in best["compared"]] == ["x", "z"]


# ── The endpoint: additive ───────────────────────────────────────────────

ITEM_KEYS = {"id", "name", "provider", "model", "max_lead_days", "selectable",
             "selected", "status", "collecting_since", "collecting_since_ms",
             "last_archived_ms", "runs", "available", "reason", "enough",
             "days_until_enough", "leads"}
BLOCK_KEYS = {"selected", "min_days", "best_leads", "best_rule", "best",
              "best_reason", "items"}


def test_the_endpoint_keeps_every_old_field_and_adds_sources(mixed, client):
    golden = json.loads(GOLDEN.read_text())
    body = client.get(f"/api/devices/{MAC}/forecast-accuracy?days=90", headers=AUTH).json()
    src = body.pop("sources")
    # Every pre-2.7 field, unchanged, Open-Meteo's when not asked.
    assert body == golden["open-meteo|90"]
    assert set(src) == BLOCK_KEYS
    assert src["selected"] == "open-meteo"           # no WU key: the session rule
    assert src["best_leads"] == [1, 2] and src["min_days"] == 10
    for item in src["items"]:
        assert set(item) == ITEM_KEYS
    # The mixed archive has three sources past the minimum; the best is
    # named, with the comparison behind it.
    assert src["best"] is not None and src["best_reason"] is None
    assert set(src["best"]) == {"source", "score_f", "shared_days", "compared"}
    assert {c["source"] for c in src["best"]["compared"]} == {"open-meteo", "twc", "open-meteo-gfs"}
    assert src["best"]["score_f"] == min(c["score_f"] for c in src["best"]["compared"])


def test_selected_follows_the_app_then_the_server(mixed, client):
    from app import db
    asyncio.run(db.set_kv("wu_api_key", "k" * 32))
    body = client.get(f"/api/devices/{MAC}/forecast-accuracy?days=90", headers=AUTH).json()
    # A WU key on the server and no provider asked: TWC is what the
    # read-only apps show, but the top-level card is still Open-Meteo's.
    assert body["provider"] == "open-meteo" and body["sources"]["selected"] == "twc"
    body = client.get(f"/api/devices/{MAC}/forecast-accuracy?days=90&provider=open-meteo-gfs",
                      headers=AUTH).json()
    assert body["provider"] == "open-meteo-gfs"
    assert body["sources"]["selected"] == "open-meteo-gfs"
    items = {s["id"]: s for s in body["sources"]["items"]}
    assert body["leads"] == items["open-meteo-gfs"]["leads"]


def test_the_endpoint_still_refuses_what_it_refused(mixed, client, monkeypatch):
    from app.config import settings
    r = client.get(f"/api/devices/{MAC}/forecast-accuracy?provider=Bad_Name", headers=AUTH)
    assert r.status_code == 422
    assert client.get(f"/api/devices/{MAC}/forecast-accuracy").status_code == 401
    monkeypatch.setattr(settings, "insights", False)
    assert client.get(f"/api/devices/{MAC}/forecast-accuracy", headers=AUTH).status_code == 404


def test_a_server_without_a_location_says_so_per_source(client, monkeypatch):
    from app import climate
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    body = client.get(f"/api/devices/{MAC}/forecast-accuracy", headers=AUTH).json()
    assert body["available"] is False and body["reason"] == "no measured days yet"
    src = body["sources"]
    assert {s["status"] for s in src["items"]} == {"no_location"}
    assert {s["days_until_enough"] for s in src["items"]} == {None}
    assert src["best"] is None and src["best_reason"] == "too_few_sources"
