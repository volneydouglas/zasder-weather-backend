"""The quiet-hour window (2.0): thinning runs inside a station-local
window the operator chooses, never at boot, and the retention API carries
the window knobs plus the nightly progress document."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

H = {"Authorization": "Bearer test-api-token"}
PHX = ZoneInfo("America/Phoenix")


def _at(h, m, day=1):
    return datetime(2026, 9, day, h, m, tzinfo=PHX)


def test_parse_window_start():
    from app import maintenance
    assert maintenance.parse_thin_window_start("02:00") == (2, 0)
    assert maintenance.parse_thin_window_start("23:59") == (23, 59)
    for bad in ("2am", "24:00", "02:60", "0200", "", "2:0:0"):
        with pytest.raises(ValueError):
            maintenance.parse_thin_window_start(bad)


def test_next_window_start_is_strictly_after_now():
    """Inside tonight's window (or exactly at its start) the NEXT start is
    tomorrow's — that is what makes a boot never run the pass."""
    from app import maintenance
    nxt = maintenance.next_thin_window_start
    assert nxt(_at(1, 30), "02:00") == _at(2, 0)
    assert nxt(_at(2, 0), "02:00") == _at(2, 0, day=2)
    assert nxt(_at(2, 45), "02:00") == _at(2, 0, day=2)
    assert nxt(_at(23, 59), "02:00") == _at(2, 0, day=2)
    assert nxt(_at(23, 59), "23:59") == _at(23, 59, day=2)
    assert nxt(_at(0, 0), "00:00") == _at(0, 0, day=2)


def test_seconds_until_window_reads_the_station_clock(monkeypatch):
    """The station's zone (TIMEZONE) and the db._now_local seam, so a
    test can move the clock and an operator's '02:00' means THEIR 02:00."""
    from app import db, maintenance
    monkeypatch.setattr(maintenance.settings, "timezone", "America/Phoenix")
    monkeypatch.setattr(db, "_now_local", lambda tz: datetime(
        2026, 9, 1, 1, 0, tzinfo=tz))
    assert maintenance.seconds_until_thin_window("02:00") == 3600.0
    assert maintenance.seconds_until_thin_window("01:00") == 24 * 3600.0
    # Always strictly positive, even at the exact start.
    monkeypatch.setattr(db, "_now_local", lambda tz: datetime(
        2026, 9, 1, 2, 0, tzinfo=tz))
    assert maintenance.seconds_until_thin_window("02:00") == 24 * 3600.0


class _Stop(Exception):
    pass


def _drive_scheduler(monkeypatch, delays, eff, sleeps_allowed):
    """Run main._retention_daily with a scripted seconds_until_thin_window
    and a recording asyncio.sleep that stops the loop after N sleeps.
    Returns (sleeps, night runs). Deliberately NOT under the `client`
    fixture: a booted app runs its own _retention_daily task, whose
    sleeps would land in the same recorder."""
    from app import main, maintenance
    sleeps: list[float] = []
    runs: list[dict] = []
    seq = list(delays)

    async def fake_eff():
        return dict(eff)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= sleeps_allowed:
            raise _Stop()

    def fake_night(e):
        runs.append(e)
        return {"enabled": True, "applied": True, "rows_deleted": 1}

    monkeypatch.setattr(maintenance, "effective_retention", fake_eff)
    monkeypatch.setattr(maintenance, "seconds_until_thin_window",
                        lambda start: seq.pop(0) if seq else 24 * 3600.0)
    monkeypatch.setattr(maintenance, "run_thin_night", fake_night)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(main._retention_daily())
    return sleeps, runs


EFF = {"detail_days": 365, "json_days": 0, "thin_window_start": "02:00",
       "thin_window_minutes": 120, "thin_batch_rows": 2000}


def test_scheduler_never_runs_at_boot_and_runs_at_the_window_start(
        temp_env, monkeypatch):
    # Boot 30 minutes before the window: one sleep to the start, then the
    # night runs — nothing before that first sleep.
    sleeps, runs = _drive_scheduler(monkeypatch, [1800.0], EFF,
                                    sleeps_allowed=2)
    assert sleeps[0] == 1800.0 and len(runs) == 1
    assert runs[0]["thin_window_minutes"] == 120


def test_scheduler_rechecks_hourly_while_the_window_is_far(temp_env, monkeypatch):
    # Boot 5 h before the window: hourly re-checks (knobs are app-managed)
    # and no run until the delay fits inside the re-check span.
    from app import main
    sleeps, runs = _drive_scheduler(
        monkeypatch, [5 * 3600.0, 4 * 3600.0, 3 * 3600.0, 3599.0], EFF,
        sleeps_allowed=5)
    assert sleeps[:3] == [main._RETENTION_RECHECK_S] * 3
    assert sleeps[3] == 3599.0
    assert len(runs) == 1


@pytest.mark.parametrize("eff", [
    {**EFF, "detail_days": 0},
    {**EFF, "thin_window_minutes": 0},
    {**EFF, "detail_days": 0, "json_days": 180, "thin_window_minutes": 0},
])
def test_scheduler_skips_the_night_when_off(temp_env, monkeypatch, eff):
    sleeps, runs = _drive_scheduler(monkeypatch, [60.0, 60.0], eff,
                                    sleeps_allowed=3)
    assert runs == []


def test_scheduler_runs_the_night_for_a_json_only_trim(temp_env, monkeypatch):
    """json_days alone (Volney's 180) is enough to schedule the night —
    the trim rides the same window and budget as thinning."""
    sleeps, runs = _drive_scheduler(
        monkeypatch, [1800.0], {**EFF, "detail_days": 0, "json_days": 180},
        sleeps_allowed=2)
    assert len(runs) == 1 and runs[0]["json_days"] == 180


def test_scheduler_survives_a_failed_night(temp_env, monkeypatch):
    from app import main, maintenance
    sleeps: list[float] = []

    async def fake_eff():
        return dict(EFF)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise _Stop()

    def boom(e):
        raise RuntimeError("rollups are marked dirty")
    monkeypatch.setattr(maintenance, "effective_retention", fake_eff)
    monkeypatch.setattr(maintenance, "seconds_until_thin_window", lambda s: 5.0)
    monkeypatch.setattr(maintenance, "run_thin_night", boom)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(main._retention_daily())
    assert sleeps == [5.0, 5.0, 5.0]        # the loop went on to the next night


def test_retention_api_carries_window_knobs_and_progress(client):
    from app import db
    r = client.get("/api/history-retention", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["thin_window_start"] == "02:00"
    assert body["thin_window_start_source"] == "env"
    assert body["thin_window_minutes"] == 120
    assert body["thin_batch_rows"] == 2000
    assert body["thin_progress"] is None

    r = client.put("/api/history-retention", headers=H,
                   json={"thin_window_start": "23:30", "thin_window_minutes": 45,
                         "thin_batch_rows": 500})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thin_window_start"] == "23:30"
    assert body["thin_window_start_source"] == "app"
    assert body["thin_window_minutes"] == 45
    assert body["thin_window_minutes_source"] == "app"
    assert body["thin_batch_rows"] == 500
    assert body["thin_batch_rows_source"] == "app"

    # Validation: HH:MM only; batch floor; 0 minutes = paused is allowed.
    assert client.put("/api/history-retention", headers=H,
                      json={"thin_window_start": "25:00"}).status_code == 400
    assert client.put("/api/history-retention", headers=H,
                      json={"thin_window_start": "2am"}).status_code == 400
    assert client.put("/api/history-retention", headers=H,
                      json={"thin_batch_rows": 100}).status_code == 400
    r = client.put("/api/history-retention", headers=H,
                   json={"thin_window_minutes": 0})
    assert r.status_code == 200 and r.json()["thin_window_minutes"] == 0

    # Forget the overrides: "" for the start, -1 for the ints.
    r = client.put("/api/history-retention", headers=H,
                   json={"thin_window_start": "", "thin_window_minutes": -1,
                         "thin_batch_rows": -1})
    body = r.json()
    assert body["thin_window_start"] == "02:00"
    assert body["thin_window_start_source"] == "env"
    assert body["thin_window_minutes"] == 120
    assert body["thin_batch_rows_source"] == "env"
    # The other knobs were never touched by any of that.
    assert body["detail_days"] == 0 and body["json_days"] == 0

    # Progress rides the GET once a night has recorded it.
    doc = {"started_ms": 1, "last_run_ms": 2, "rows_deleted_total": 41200,
           "rows_slimmed_total": 9000, "window_lo_ms": 3, "window_hi_ms": 4,
           "done": False, "nights_remaining": 6, "rows_remaining": 250000}
    asyncio.run(db.set_kv("thin_progress", json.dumps(doc)))
    body = client.get("/api/history-retention", headers=H).json()
    assert body["thin_progress"] == doc


def test_effective_retention_ignores_junk_stored_window_values(client):
    """A hand-edited or older kv document with bad shapes falls back to
    env per field, never 500s the nightly scheduler."""
    from app import db, maintenance
    asyncio.run(db.set_kv(maintenance._RETENTION_KV_KEY, json.dumps({
        "thin_window_start": "soon", "thin_window_minutes": "120",
        "thin_batch_rows": 5})))
    eff = asyncio.run(maintenance.effective_retention())
    assert eff["thin_window_start"] == "02:00"
    assert eff["thin_window_start_source"] == "env"
    assert eff["thin_window_minutes"] == 120
    assert eff["thin_batch_rows"] == 2000


def test_config_rejects_a_bad_window_start(monkeypatch):
    from app.config import Settings
    with pytest.raises(ValueError, match="HH:MM"):
        Settings(history_thin_window_start="2am")
    assert Settings(history_thin_window_start="04:30").history_thin_window_start == "04:30"
    # Validated on a stripped copy, so stored stripped too: a padded value
    # in .env must not reach every consumer with its whitespace on.
    assert Settings(history_thin_window_start=" 04:30 ").history_thin_window_start == "04:30"
