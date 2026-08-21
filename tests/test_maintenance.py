"""Destructive maintenance routines (R3-136; R3-92 streamed gust backup).

These run `--apply` against production data with the backup file as the only
recovery artifact — previously zero tests imported app.maintenance.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3

import pytest


@pytest.fixture
def db_mod(temp_env: str):
    """Fresh app.db bound to the per-test DATABASE_PATH, schema created."""
    for mod in ["app.config", "app.db"]:
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import db
    asyncio.run(db.init_db())
    return db


def _col(path: str, mac: str, ts: int, col: str):
    con = sqlite3.connect(path)
    val, blob = con.execute(
        f"SELECT {col}, data_json FROM observations "
        f"WHERE mac = ? AND dateutc_ms = ?", (mac, ts)).fetchone()
    con.close()
    return val, (json.loads(blob) if blob else {})


# ───────────────────────── clean_cumulative_rain ─────────────────────────

def test_clean_cumulative_rain_nulls_non_resetting_column(db_mod, temp_env):
    """A column whose all-time MIN never drops near 0 is a lifetime counter
    stored as a rollup — null it (column AND data_json); a genuinely
    resetting column must be untouched."""
    from app import maintenance
    db = db_mod
    bogus = "AA:AA:AA:AA:AA:01"
    good = "AA:AA:AA:AA:AA:02"
    asyncio.run(db.insert_observations(bogus, [
        {"dateutc": 1_000, "dailyrainin": 6.0, "tempf": 70.0},
        {"dateutc": 2_000, "dailyrainin": 7.5, "tempf": 71.0}]))
    asyncio.run(db.insert_observations(good, [
        {"dateutc": 1_000, "dailyrainin": 0.0},
        {"dateutc": 2_000, "dailyrainin": 6.5}]))

    dry = maintenance.clean_cumulative_rain(apply=False, db_path=temp_env)
    assert dry["applied"] is False and dry["cleaned"] == 0
    assert f"{bogus}:dailyrainin" in dry["findings"]
    v, _ = _col(temp_env, bogus, 1_000, "dailyrainin")
    assert v == 6.0, "dry run must not mutate"

    res = maintenance.clean_cumulative_rain(apply=True, db_path=temp_env)
    assert res["applied"] is True and res["cleaned"] == 2
    for ts in (1_000, 2_000):
        v, blob = _col(temp_env, bogus, ts, "dailyrainin")
        assert v is None and "dailyrainin" not in blob
    # The resetting column keeps its real values.
    v, blob = _col(temp_env, good, 2_000, "dailyrainin")
    assert v == 6.5 and blob["dailyrainin"] == 6.5


# ───────────────────────── clean_glitch_gusts (R3-92) ─────────────────────────

def test_clean_glitch_gusts_streams_backup_and_spares_calm_stations(db_mod, temp_env):
    from app import maintenance
    db = db_mod
    mac = "BB:BB:BB:BB:BB:01"
    asyncio.run(db.insert_observations(mac, [
        # Glitch: 80 mph gust vs 5 mph sustained (> 4x, above the 30 floor).
        {"dateutc": 1_000, "windgustmph": 80.0, "windspeedmph": 5.0},
        # Calm-station squall: sustained 0 → must NOT be nulled.
        {"dateutc": 2_000, "windgustmph": 45.0, "windspeedmph": 0.0},
        # Plausible gust: within factor.
        {"dateutc": 3_000, "windgustmph": 40.0, "windspeedmph": 20.0}]))

    res = maintenance.clean_glitch_gusts(apply=True, db_path=temp_env)
    assert res["applied"] is True and res["cleaned"] == 1
    # Backup is STREAMED JSONL (the in-memory list was the OOM pattern the
    # yearly repair's docstring documents) with exactly the affected row.
    assert res["backup"].endswith(".jsonl")
    with open(res["backup"]) as f:
        lines = [json.loads(line) for line in f]
    assert lines == [{"mac": mac, "dateutc_ms": 1_000,
                      "windgustmph": 80.0, "windspeedmph": 5.0}]
    v, blob = _col(temp_env, mac, 1_000, "windgustmph")
    assert v is None and "windgustmph" not in blob
    v, _ = _col(temp_env, mac, 2_000, "windgustmph")
    assert v == 45.0
    v, _ = _col(temp_env, mac, 3_000, "windgustmph")
    assert v == 40.0


# ───────────────────────── clean_implausible ─────────────────────────

def test_clean_implausible_nulls_only_out_of_band_fields(db_mod, temp_env):
    """Retro-apply the bands to stored history, field by field.

    The 2026-08-15 case: a WU archive imported ~1,200 rows of 255 mph wind
    (0xFF, the anemometer-dropout sentinel) because the importer applied no
    bands at all. The repair must null the bad FIELD and leave the rest of
    the row alone — the day still has good temperature and rain.
    """
    from app import maintenance
    db = db_mod
    mac = "DD:DD:DD:DD:DD:01"
    asyncio.run(db.insert_observations(mac, [
        # The sentinel row: wind is garbage, everything else is real.
        {"dateutc": 1_000, "windgustmph": 255.0, "windspeedmph": 255.0,
         "tempf": 48.0, "humidity": 71.0, "dailyrainin": 0.22},
        # A real windy reading just under the world record — must survive.
        {"dateutc": 2_000, "windgustmph": 253.0, "tempf": 50.0},
        # An ordinary reading — untouched.
        {"dateutc": 3_000, "windgustmph": 22.0, "tempf": 51.0}]))

    dry = maintenance.clean_implausible(apply=False, db_path=temp_env)
    assert dry["applied"] is False and dry["cleaned"] == 0
    assert dry["by_field"] == {"windgustmph": 1, "windspeedmph": 1}
    v, _ = _col(temp_env, mac, 1_000, "windgustmph")
    assert v == 255.0, "dry run must not mutate"

    res = maintenance.clean_implausible(apply=True, db_path=temp_env)
    assert res["applied"] is True and res["cleaned"] == 2

    # Sentinel wind gone from BOTH the column and data_json (the /current
    # composite reads data_json, records read the column).
    for col in ("windgustmph", "windspeedmph"):
        v, blob = _col(temp_env, mac, 1_000, col)
        assert v is None and col not in blob
    # ...but the rest of that row is intact — the day is not deleted.
    v, blob = _col(temp_env, mac, 1_000, "tempf")
    assert v == 48.0 and blob["tempf"] == 48.0
    v, _ = _col(temp_env, mac, 1_000, "dailyrainin")
    assert v == 0.22
    # In-band readings untouched, including the world-record-adjacent one.
    v, _ = _col(temp_env, mac, 2_000, "windgustmph")
    assert v == 253.0
    v, _ = _col(temp_env, mac, 3_000, "windgustmph")
    assert v == 22.0

    with open(res["backup"]) as f:
        lines = [json.loads(line) for line in f]
    assert {(l["field"], l["value"]) for l in lines} == {
        ("windgustmph", 255.0), ("windspeedmph", 255.0)}


# ───────────────────────── repair_yearly_rain_offsets ─────────────────────────

_MAC_R = "CC:CC:CC:CC:CC:01"


def _seed_offset_history(db):
    """Era-0 row (unknown earlier offset), a positive offset-adjusted row, a
    clamped zero, and one raw post-cutoff row."""
    asyncio.run(db.insert_observations(_MAC_R, [
        {"dateutc": 100,    "yearlyrainin": 0.5},   # era-0 (before 1000)
        {"dateutc": 2_000,  "yearlyrainin": 2.0},   # offset-adjusted
        {"dateutc": 3_000,  "yearlyrainin": 0.0},   # clamped by the offset
        {"dateutc": 10_000, "yearlyrainin": 18.5},  # raw, post-cutoff
    ]))


_SPEC = {_MAC_R: {"offset": 16.03, "cutoff_ms": 5_000, "null_before_ms": 1_000}}


def test_repair_yearly_offsets_applies_offset_null_and_era0(db_mod, temp_env):
    from app import maintenance
    _seed_offset_history(db_mod)
    res = maintenance.repair_yearly_rain_offsets(_SPEC, apply=True,
                                                 db_path=temp_env)
    assert res["applied"] is True
    assert res["macs"][_MAC_R] == {"add_offset_rows": 1, "null_rows": 1,
                                   "era0_null_rows": 1}
    # Backup JSONL holds every pre-cutoff non-null row, BEFORE the UPDATE.
    with open(res["backup"]) as f:
        lines = [json.loads(line) for line in f]
    assert {(r["dateutc_ms"], r["yearlyrainin"]) for r in lines} == \
        {(100, 0.5), (2_000, 2.0), (3_000, 0.0)}
    v, blob = _col(temp_env, _MAC_R, 100, "yearlyrainin")
    assert v is None and "yearlyrainin" not in blob      # era-0 → NULL
    v, blob = _col(temp_env, _MAC_R, 2_000, "yearlyrainin")
    assert v == pytest.approx(18.03) and blob["yearlyrainin"] == pytest.approx(18.03)
    v, blob = _col(temp_env, _MAC_R, 3_000, "yearlyrainin")
    assert v is None and "yearlyrainin" not in blob      # clamped zero → NULL
    v, _ = _col(temp_env, _MAC_R, 10_000, "yearlyrainin")
    assert v == 18.5                                     # raw rows untouched


def test_repair_yearly_offsets_second_run_is_refused(db_mod, temp_env):
    """The monotonicity guard: repaired history + offset would exceed the
    first raw value — a second run (or a wrong offset) must skip the MAC."""
    from app import maintenance
    _seed_offset_history(db_mod)
    assert maintenance.repair_yearly_rain_offsets(
        _SPEC, apply=True, db_path=temp_env)["applied"] is True
    res2 = maintenance.repair_yearly_rain_offsets(_SPEC, apply=True,
                                                  db_path=temp_env)
    assert res2["applied"] is False
    assert "monotonicity" in res2["macs"][_MAC_R]["skipped"]
    # The already-repaired value must be unchanged (not double-offset).
    v, _ = _col(temp_env, _MAC_R, 2_000, "yearlyrainin")
    assert v == pytest.approx(18.03)


def test_repair_yearly_offsets_skips_malformed_entries_without_aborting(
        db_mod, temp_env):
    from app import maintenance
    _seed_offset_history(db_mod)
    spec = {
        "M1": {"offset": None, "cutoff_ms": 5_000},
        "M2": {"offset": "abc", "cutoff_ms": 5_000},
        "M3": {"offset": float("nan"), "cutoff_ms": 5_000},
        "M4": {"offset": -2.0, "cutoff_ms": 5_000},
        "M5": {"cutoff_ms": 5_000},                        # offset missing
        "M6": {"offset": 1.0, "cutoff_ms": 100, "null_before_ms": 200},
        _MAC_R: {"offset": 16.03, "cutoff_ms": 5_000,
                 "null_before_ms": 1_000},                 # the one valid entry
    }
    res = maintenance.repair_yearly_rain_offsets(spec, apply=True,
                                                 db_path=temp_env)
    assert res["applied"] is True                          # valid MAC repaired
    for bad in ("M1", "M2", "M3", "M4", "M5", "M6"):
        assert "skipped" in res["macs"][bad], f"{bad} must be skipped"
    v, _ = _col(temp_env, _MAC_R, 2_000, "yearlyrainin")
    assert v == pytest.approx(18.03)


def test_repair_yearly_offsets_requires_post_cutoff_rows(db_mod, temp_env):
    """No raw rows after the cutoff = nothing to reconcile the repair
    against — the MAC must be skipped, not blindly rewritten."""
    from app import maintenance
    db = db_mod
    mac = "DD:DD:DD:DD:DD:01"
    asyncio.run(db.insert_observations(mac, [
        {"dateutc": 2_000, "yearlyrainin": 2.0}]))
    res = maintenance.repair_yearly_rain_offsets(
        {mac: {"offset": 16.03, "cutoff_ms": 5_000}}, apply=True,
        db_path=temp_env)
    assert res["applied"] is False
    assert res["macs"][mac]["skipped"] == "no post-cutoff raw rows"
    v, _ = _col(temp_env, mac, 2_000, "yearlyrainin")
    assert v == 2.0


# ────────────────── anemometer orphans + wind consistency ──────────────────

def test_clean_implausible_condemns_in_band_anemometer_orphan(db_mod, temp_env):
    """Regression for Doren's 55 mph Peak Wind (2026-08-16).

    Live ingest condemns the whole anemometer set when the bands reject any
    one of its channels. This retro path did not, so sweeping a 255 mph gust
    left the sustained value from the SAME dropout sitting on the row — and at
    51-55 mph it is comfortably in-band, so re-running the bands forever would
    never have found it. One of those survivors then owned the all-time Peak
    Wind record, above a Peak Gust of 51, which is physically impossible.
    """
    from app import maintenance
    db = db_mod
    mac = "DD:DD:DD:DD:DD:09"
    asyncio.run(db.insert_observations(mac, [
        # The dropout: 0xFF gust, and a sustained value that survives the
        # bands on its own but is the same faulting sensor.
        {"dateutc": 1_000, "windgustmph": 255.0, "windspeedmph": 55.2,
         "winddir": 336.0, "tempf": 48.0, "dailyrainin": 0.22},
        # A real windy reading — the sweep must not reach it.
        {"dateutc": 2_000, "windgustmph": 51.0, "windspeedmph": 15.0,
         "tempf": 50.0}]))

    res = maintenance.clean_implausible(apply=True, db_path=temp_env)
    assert res["applied"] is True
    assert res["anemometer_orphans"] == 1, "in-band sibling left behind"

    # Both channels gone from column AND data_json.
    for col in ("windgustmph", "windspeedmph"):
        v, blob = _col(temp_env, mac, 1_000, col)
        assert v is None and col not in blob, col
    # The vane is a separate sensor, and the rest of the reading is real.
    v, _ = _col(temp_env, mac, 1_000, "winddir")
    assert v == 336.0
    v, _ = _col(temp_env, mac, 1_000, "tempf")
    assert v == 48.0
    v, _ = _col(temp_env, mac, 1_000, "dailyrainin")
    assert v == 0.22
    # The genuine reading is untouched.
    v, _ = _col(temp_env, mac, 2_000, "windgustmph")
    assert v == 51.0
    v, _ = _col(temp_env, mac, 2_000, "windspeedmph")
    assert v == 15.0


def test_clean_wind_inconsistent_dry_run_then_apply(db_mod, temp_env):
    """Retro version of the sustained-vs-gust check: same rule and the same
    thresholds as ingest, with a dry run and a streamed backup first."""
    from app import maintenance
    db = db_mod
    mac = "DD:DD:DD:DD:DD:0A"
    asyncio.run(db.insert_observations(mac, [
        # Contradiction: 1.84x the gust and 21 mph above it.
        {"dateutc": 1_000, "windspeedmph": 46.0, "windgustmph": 25.0,
         "maxdailygust": 30.0, "winddir": 90.0, "tempf": 40.0},
        # Ordinary reading — sustained under gust.
        {"dateutc": 2_000, "windspeedmph": 12.0, "windgustmph": 22.0},
        # Window slack: over the gust, but only just.
        {"dateutc": 3_000, "windspeedmph": 12.0, "windgustmph": 11.0}]))

    dry = maintenance.clean_wind_inconsistent(apply=False, db_path=temp_env)
    assert dry["applied"] is False and dry["bad_rows"] == 1
    v, _ = _col(temp_env, mac, 1_000, "windspeedmph")
    assert v == 46.0, "dry run must not mutate"

    res = maintenance.clean_wind_inconsistent(apply=True, db_path=temp_env)
    assert res["applied"] is True and res["rows_touched"] == 1
    # All three speed channels cleared on the contradicting reading only.
    for col in ("windspeedmph", "windgustmph", "maxdailygust"):
        v, blob = _col(temp_env, mac, 1_000, col)
        assert v is None and col not in blob, col
    v, _ = _col(temp_env, mac, 1_000, "winddir")
    assert v == 90.0
    v, _ = _col(temp_env, mac, 1_000, "tempf")
    assert v == 40.0
    for ts in (2_000, 3_000):
        v, _ = _col(temp_env, mac, ts, "windspeedmph")
        assert v == 12.0, ts

    with open(res["backup"]) as f:
        lines = [json.loads(line) for line in f]
    assert {(l["field"], l["value"]) for l in lines} == {
        ("windspeedmph", 46.0), ("windgustmph", 25.0), ("maxdailygust", 30.0)}


def test_clean_wind_inconsistent_noop_on_clean_history(db_mod, temp_env):
    from app import maintenance
    db = db_mod
    mac = "DD:DD:DD:DD:DD:0B"
    asyncio.run(db.insert_observations(mac, [
        {"dateutc": 1_000, "windspeedmph": 10.0, "windgustmph": 18.0}]))
    res = maintenance.clean_wind_inconsistent(apply=True, db_path=temp_env)
    assert res["bad_rows"] == 0 and res["cleaned"] == 0
    assert res["backup"] is None
