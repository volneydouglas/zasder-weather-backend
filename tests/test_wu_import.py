"""WU historical importer — transform mapping, run loop, API validation."""
import asyncio
import datetime as dt

import pytest

# Set lazily by the autouse fixture below: app modules read env at import
# time, so the import must happen AFTER the client fixture has set the test
# env (see conftest's module docstring). A top-level `from app import
# wu_import` passes in full-suite runs by collection-order luck and fails
# when this file runs alone.
wu_import = None

_H = {"Authorization": "Bearer test-api-token"}
_ING = {"Authorization": "Bearer test-ingest-token"}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, client):
    """Import the module under test AFTER `client` sets the env, and keep its
    module-global state from leaking between tests; drop the inter-day sleep
    so run-loop tests finish instantly."""
    global wu_import
    from app import wu_import as _m
    wu_import = _m
    wu_import._state.clear()
    wu_import._state["running"] = False
    monkeypatch.setattr(wu_import, "WU_CALL_GAP_S", 0)
    monkeypatch.setattr(wu_import, "_RETRY_TRANSPORT_SLEEP_S", 0)
    yield
    wu_import._state.clear()
    wu_import._state["running"] = False


def _wu_obs(epoch: int, temp=95.0, precip_total=0.13) -> dict:
    return {
        "stationID": "KAZCHAND668", "epoch": epoch,
        "humidityAvg": 40.0, "winddirAvg": 180.0,
        "solarRadiationHigh": 800.0, "uvHigh": 7.0,
        "imperial": {
            "tempAvg": temp, "dewptAvg": 55.0,
            "windspeedAvg": 5.0, "windgustHigh": 12.0,
            "pressureMax": 29.95, "pressureMin": 29.85,
            "heatindexAvg": 99.0, "windchillAvg": temp,
            "precipRate": 0.02, "precipTotal": precip_total,
        },
    }


# ───────────────────────── transform ─────────────────────────

def test_transform_maps_api_native_fields():
    row = wu_import.transform_observation(_wu_obs(1_680_912_294), "KAZCHAND668")
    assert row["dateutc"] == 1_680_912_294_000
    assert row["tempf"] == 95.0
    assert row["humidity"] == 40.0
    assert row["windspeedmph"] == 5.0
    assert row["windgustmph"] == 12.0
    assert row["winddir"] == 180.0
    assert row["baromrelin"] == pytest.approx(29.90)   # mid of max/min
    assert row["dewPoint"] == 55.0
    assert row["feelsLike"] == 99.0                    # ≥80°F → heat index
    assert row["solarradiation"] == 800.0
    assert row["uv"] == 7.0
    # R3-88: precipRate is an INSTANTANEOUS rate; it must NOT land in
    # hourlyrainin (which means trailing-1h ACCUMULATION everywhere else).
    # It survives in data_json under its own key for provenance.
    assert "hourlyrainin" not in row
    assert row["precipRate"] == 0.02
    assert row["dailyrainin"] == 0.13
    assert "yearlyrainin" not in row                   # never synthesized
    assert row["source"] == "wu-import"


def test_transform_applies_plausibility_bands():
    """The importer was the one write path into `observations` with no QC.

    WU serves 0xFF (255) as a literal wind speed when a station's anemometer
    drops out. Those rows imported as fact and owned the all-time wind records
    (2026-08-15). The bands must run here exactly as they do on live ingest,
    and field-level, so the row keeps its good readings.
    """
    o = _wu_obs(1_680_912_294)
    o["imperial"]["windgustHigh"] = 255.0
    o["imperial"]["windspeedAvg"] = 255.0
    row = wu_import.transform_observation(o, "KAZCHAND668")
    assert row["windgustmph"] is None
    assert row["windspeedmph"] is None
    # everything else on the row survives — this is not a dropped reading
    assert row["tempf"] == 95.0
    assert row["humidity"] == 40.0
    assert row["dailyrainin"] == 0.13
    assert row["source"] == "wu-import"


def test_transform_feels_like_branches():
    hot = _wu_obs(1, temp=95.0)
    assert wu_import.transform_observation(hot, "X")["feelsLike"] == 99.0
    cold = _wu_obs(2, temp=40.0)
    cold["imperial"]["windchillAvg"] = 33.0
    assert wu_import.transform_observation(cold, "X")["feelsLike"] == 33.0
    mild = _wu_obs(3, temp=70.0)
    assert wu_import.transform_observation(mild, "X")["feelsLike"] == 70.0


def test_transform_missing_readings_stay_none():
    row = wu_import.transform_observation(
        {"epoch": 1_680_912_294, "imperial": {"tempAvg": 72.0}}, "X")
    assert row["tempf"] == 72.0
    assert row["solarradiation"] is None
    assert row["uv"] is None
    assert row["dailyrainin"] is None
    assert row["baromrelin"] is None


def test_transform_rejects_missing_epoch():
    assert wu_import.transform_observation({"imperial": {"tempAvg": 72.0}}, "X") is None
    assert wu_import.transform_observation({"epoch": "nope"}, "X") is None


# ───────────────────────── run loop ─────────────────────────

def _seed_device(client):
    client.post("/ingest/custom", headers=_ING,
                json={"device": {"id": "AABBCCDDEEFF", "name": "Davis"},
                      "timestamp_utc":
                          dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "outdoor": {"tempf": 90.0, "humidity": 30},
                      "wind": {}, "rain": {},
                      "pressure": {"relative_inhg": 29.9}, "source": "test"})


def _run_import(monkeypatch, days_payload, mac="AA:BB:CC:DD:EE:FF",
                dry_run=False, start="2023-04-08", end="2023-04-09"):
    async def fake_fetch(client, station_id, day, api_key):
        assert api_key == "k" * 16          # threaded through, never mutated
        return days_payload.get(day, [])
    monkeypatch.setattr(wu_import, "_fetch_day", fake_fetch)
    asyncio.run(wu_import._run(mac, "KAZCHAND668", "k" * 16,
                               dt.date.fromisoformat(start),
                               dt.date.fromisoformat(end), dry_run))
    return wu_import.status()


def test_import_inserts_and_is_idempotent(client, monkeypatch):
    _seed_device(client)
    days = {"20230408": [_wu_obs(1_680_912_294), _wu_obs(1_680_912_594)],
            "20230409": [_wu_obs(1_680_998_694)]}
    st = _run_import(monkeypatch, days)
    assert st["error"] is None
    assert st["done_days"] == 2 and st["rows_seen"] == 3
    assert st["rows_inserted"] == 3

    # Same import again: INSERT OR IGNORE — nothing added.
    st2 = _run_import(monkeypatch, days)
    assert st2["rows_seen"] == 3 and st2["rows_inserted"] == 0

    # The imported rows are queryable through ranged /history.
    end_ms = (1_680_998_694 + 3600) * 1000
    rows = client.get("/api/devices/AA:BB:CC:DD:EE:FF/history"
                      f"?hours=48&end_ms={end_ms}", headers=_H).json()["rows"]
    temps = [r["tempf"] for r in rows if r.get("tempf") is not None]
    assert temps and all(t == 95.0 for t in temps)


def test_import_dry_run_counts_without_inserting(client, monkeypatch):
    _seed_device(client)
    st = _run_import(monkeypatch, {"20230408": [_wu_obs(1_680_912_294)]},
                     dry_run=True, end="2023-04-08")
    assert st["rows_seen"] == 1 and st["rows_inserted"] == 0
    end_ms = (1_680_912_294 + 3600) * 1000
    rows = client.get("/api/devices/AA:BB:CC:DD:EE:FF/history"
                      f"?hours=24&end_ms={end_ms}", headers=_H).json()["rows"]
    assert rows == []


def test_import_records_empty_days_and_auth_error(client, monkeypatch):
    _seed_device(client)
    st = _run_import(monkeypatch, {}, end="2023-04-09")
    assert st["empty_days"] == 2 and st["error"] is None

    async def denied(client_, station_id, day, api_key):
        raise PermissionError("WU API key rejected (401)")
    monkeypatch.setattr(wu_import, "_fetch_day", denied)
    asyncio.run(wu_import._run("AA:BB:CC:DD:EE:FF", "X", "k" * 16,
                               dt.date(2023, 4, 8), dt.date(2023, 4, 8), False))
    st = wu_import.status()
    assert st["error"] == "WU API key rejected (401)"
    assert st["running"] is False
    assert "k" * 16 not in str(st)          # the key never enters the snapshot


def test_import_stops_at_daily_budget_with_resume_point(client, monkeypatch):
    _seed_device(client)
    monkeypatch.setattr(wu_import, "WU_DAILY_CALL_BUDGET", 2)
    days = {"20230408": [_wu_obs(1_680_912_294)],
            "20230409": [_wu_obs(1_680_998_694)],
            "20230410": [_wu_obs(1_681_085_094)],
            "20230411": [_wu_obs(1_681_171_494)]}
    st = _run_import(monkeypatch, days, end="2023-04-11")
    assert st["error"] is None
    assert st["calls_made"] == 2 and st["done_days"] == 2
    assert st["resume_from"] == "2023-04-10"
    assert st["rows_inserted"] == 2

    # Resuming from the recorded day finishes the range — and duplicate
    # safety means an overlapping resume would also have been fine.
    monkeypatch.setattr(wu_import, "WU_DAILY_CALL_BUDGET", 1400)
    st2 = _run_import(monkeypatch, days, start="2023-04-10", end="2023-04-11")
    assert st2["rows_inserted"] == 2 and st2["resume_from"] is None


def test_transient_failure_sets_resume_point(client, monkeypatch):
    """R3-13: a PERSISTENT ConnectError mid-run (first try AND the single
    retry) must leave the same clean resume state quota exhaustion does —
    re-running a multi-year import from the start re-burns hundreds of quota
    calls just to reach the day that failed."""
    import httpx
    _seed_device(client)
    calls = {"n": 0}
    async def flaky(client_, station_id, day, api_key):
        calls["n"] += 1
        if calls["n"] in (2, 3):        # day 2: original call AND its retry
            raise httpx.ConnectError("boom https://api.weather.com?apiKey=" + api_key)
        return [_wu_obs(1_680_912_294 + calls["n"])]
    monkeypatch.setattr(wu_import, "_fetch_day", flaky)
    asyncio.run(wu_import._run("AA:BB:CC:DD:EE:FF", "KAZCHAND668", "k" * 16,
                               dt.date(2023, 4, 8), dt.date(2023, 4, 10), False))
    st = wu_import.status()
    assert st["running"] is False
    assert st["resume_from"] == "2023-04-09"    # the day that failed
    assert st["done_days"] == 1
    # The error is diagnosable but carries neither the URL nor the key.
    assert st["error"] and "ConnectError" in st["error"]
    assert "k" * 16 not in str(st) and "http" not in st["error"]
    # Exactly three calls: day 1, day 2, and day 2's single retry — without
    # this, removing the retry entirely would still pass (CodeRabbit).
    assert calls["n"] == 3

    # Resuming from the recorded day completes the range.
    days = {"20230409": [_wu_obs(1_680_998_694)],
            "20230410": [_wu_obs(1_681_085_094)]}
    st2 = _run_import(monkeypatch, days, start="2023-04-09", end="2023-04-10")
    assert st2["error"] is None and st2["done_days"] == 2
    assert st2["resume_from"] is None


def test_transient_failure_retries_once_and_continues(client, monkeypatch):
    """A single network blip must NOT park a multi-year import: the same day
    is retried once (after a short sleep) and the run continues. The state
    snapshot stays free of the key and the URL."""
    import httpx
    _seed_device(client)
    calls = {"n": 0}
    seen_days: list[str] = []
    async def flaky_once(client_, station_id, day, api_key):
        calls["n"] += 1
        seen_days.append(day)
        if calls["n"] == 2:             # day 2, first attempt only
            raise httpx.ReadTimeout("boom https://api.weather.com?apiKey=" + api_key)
        return [_wu_obs(1_680_912_294 + calls["n"])]
    monkeypatch.setattr(wu_import, "_fetch_day", flaky_once)
    asyncio.run(wu_import._run("AA:BB:CC:DD:EE:FF", "KAZCHAND668", "k" * 16,
                               dt.date(2023, 4, 8), dt.date(2023, 4, 10), False))
    st = wu_import.status()
    assert st["error"] is None and st["resume_from"] is None
    assert st["done_days"] == 3
    assert st["calls_made"] == 4        # 3 days + the one retry
    assert st["rows_inserted"] == 3
    assert "k" * 16 not in str(st)
    # The retry must target the SAME day, in order — a retry that skipped to
    # the next day would also produce 4 calls and 3 done days (CodeRabbit).
    assert seen_days == ["20230408", "20230409", "20230409", "20230410"]


def test_retry_respects_call_budget_boundary(client, monkeypatch):
    """When the failed call consumed the FINAL budget slot, the retry must
    not run: the import parks at the day with the quota pause, staying
    within WU_DAILY_CALL_BUDGET."""
    import httpx
    _seed_device(client)
    monkeypatch.setattr(wu_import, "WU_DAILY_CALL_BUDGET", 2)
    calls = {"n": 0}
    async def flaky_at_budget(client_, station_id, day, api_key):
        calls["n"] += 1
        if calls["n"] == 2:             # day 2 = the final budgeted call
            raise httpx.ConnectError("boom")
        return [_wu_obs(1_680_912_294 + calls["n"])]
    monkeypatch.setattr(wu_import, "_fetch_day", flaky_at_budget)
    asyncio.run(wu_import._run("AA:BB:CC:DD:EE:FF", "KAZCHAND668", "k" * 16,
                               dt.date(2023, 4, 8), dt.date(2023, 4, 10), False))
    st = wu_import.status()
    assert calls["n"] == 2              # the retry never fired
    assert st["calls_made"] == 2        # never exceeds the budget
    assert st["resume_from"] == "2023-04-09"
    assert st["done_days"] == 1


def test_cancel_sets_resume_point(client, monkeypatch):
    """A graceful cancel records the first unprocessed day, so the iOS
    resume affordance works after a user-initiated stop too."""
    _seed_device(client)
    async def cancel_after_first(client_, station_id, day, api_key):
        wu_import._state["cancelled"] = True
        return [_wu_obs(1_680_912_294)]
    monkeypatch.setattr(wu_import, "_fetch_day", cancel_after_first)
    asyncio.run(wu_import._run("AA:BB:CC:DD:EE:FF", "X", "k" * 16,
                               dt.date(2023, 4, 8), dt.date(2023, 4, 10), False))
    st = wu_import.status()
    assert st["done_days"] == 1
    assert st["resume_from"] == "2023-04-09"


def test_import_429_exhaustion_pauses_with_resume_point(client, monkeypatch):
    _seed_device(client)
    calls = {"n": 0}
    async def flaky(client_, station_id, day, api_key):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise wu_import._QuotaExhausted()
        return [_wu_obs(1_680_912_294)]
    monkeypatch.setattr(wu_import, "_fetch_day", flaky)
    asyncio.run(wu_import._run("AA:BB:CC:DD:EE:FF", "X", "k" * 16,
                               dt.date(2023, 4, 8), dt.date(2023, 4, 10), False))
    st = wu_import.status()
    assert st["error"] is None                  # a pause, not a failure
    assert st["resume_from"] == "2023-04-09"
    assert st["done_days"] == 1


# ───────────────────────── API surface ─────────────────────────

def test_import_api_validation(client):
    _seed_device(client)
    base = {"api_key": "k" * 16, "start_date": "2023-04-08"}
    # Unknown device.
    r = client.post("/api/import/wu", headers=_H,
                    json={**base, "mac": "00:00:00:00:00:00",
                          "wu_station_id": "KAZCHAND668"})
    assert r.status_code == 404
    # No station given and none associated.
    r = client.post("/api/import/wu", headers=_H,
                    json={**base, "mac": "AA:BB:CC:DD:EE:FF"})
    assert r.status_code == 400
    # Dates must be ordered.
    r = client.post("/api/import/wu", headers=_H,
                    json={**base, "mac": "AA:BB:CC:DD:EE:FF",
                          "wu_station_id": "KAZCHAND668",
                          "start_date": "2024-01-02", "end_date": "2024-01-01"})
    assert r.status_code == 400
    # Write-token-gated; status is readable with the plain token.
    assert client.post("/api/import/wu", json={**base, "mac": "x"}).status_code == 401
    assert client.get("/api/import/wu/status", headers=_H).status_code == 200


def test_start_import_claims_slot_synchronously(client, monkeypatch):
    """Two rapid start calls before the event loop runs the first task must
    not both succeed — the running flag is claimed at call time, not when
    the task first executes (CodeRabbit finding on the check-then-act race)."""
    async def slow_run(*a, **k):
        await asyncio.sleep(0)
        wu_import._state["running"] = False
    monkeypatch.setattr(wu_import, "_run", slow_run)

    async def race():
        first = wu_import.start_import("AA:BB:CC:DD:EE:FF", "X", "k" * 16,
                                       dt.date(2023, 4, 8), dt.date(2023, 4, 8),
                                       False)
        second = wu_import.start_import("AA:BB:CC:DD:EE:FF", "X", "k" * 16,
                                        dt.date(2023, 4, 8), dt.date(2023, 4, 8),
                                        False)
        await asyncio.sleep(0.01)
        return first, second
    first, second = asyncio.run(race())
    assert first is True and second is False


def test_start_import_snapshot_is_coherent_before_task_runs(client, monkeypatch):
    """R3-55: the synchronous claim must be the FULL state snapshot — a
    status poll landing between start_import() and the task's first run must
    not see running=True merged with the PREVIOUS import's fields."""
    async def never_runs(*a, **k):
        wu_import._state["running"] = False
    monkeypatch.setattr(wu_import, "_run", never_runs)
    # Residue from a previous, finished import.
    wu_import._state.update({"running": False, "mac": "OLD:MAC",
                             "wu_station_id": "KOLD1", "done_days": 7,
                             "rows_inserted": 1234, "error": "old failure",
                             "resume_from": "2020-01-01"})

    async def scenario():
        ok = wu_import.start_import("AA:BB:CC:DD:EE:FF", "KAZCHAND668",
                                    "k" * 16, dt.date(2023, 4, 8),
                                    dt.date(2023, 4, 9), False)
        # Snapshot BEFORE the created task has had a chance to run.
        return ok, wu_import.status()
    ok, st = asyncio.run(scenario())
    assert ok is True
    assert st["running"] is True
    assert st["mac"] == "AA:BB:CC:DD:EE:FF"
    assert st["wu_station_id"] == "KAZCHAND668"
    assert st["done_days"] == 0 and st["rows_inserted"] == 0
    assert st["error"] is None and st["resume_from"] is None
    assert st["total_days"] == 2


def test_import_api_conflict_while_running(client):
    _seed_device(client)
    wu_import._state["running"] = True      # simulate an active run
    r = client.post("/api/import/wu", headers=_H,
                    json={"mac": "AA:BB:CC:DD:EE:FF",
                          "wu_station_id": "KAZCHAND668",
                          "api_key": "k" * 16, "start_date": "2023-04-08"})
    assert r.status_code == 409
    assert client.post("/api/import/wu/cancel", headers=_H).json()["ok"] is True
    assert wu_import._state["cancelled"] is True


def test_millisecond_epoch_normalized():
    """WU's pre-2019 archive sends epoch in MILLISECONDS (seen live on 2015
    data); unnormalized it produced year-47000 timestamps and a ValueError
    for every day of an old import."""
    from app import wu_import as wi
    row = wi.transform_observation({
        "epoch": 1427860860000,          # 2015-04-01T04:01:00Z, in ms
        "humidityAvg": 80.0,
        "imperial": {"tempAvg": 34.7},
    }, "KPAIRWIN10")
    assert row is not None
    assert row["dateutc"] == 1427860860000
    assert row["tempf"] == 34.7
