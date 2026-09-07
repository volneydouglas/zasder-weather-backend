"""The 2026-09-06 release review's restore findings (R21-01 to R21-05,
R21-07), converted from its defect probes into pins of the DESIRED
behaviour: the maintenance lease that closes the database for the swap,
candidate validation before anything live moves, rollback on every
post-cutover failure, boot recovery from an interrupted swap, service
reconciliation after a successful restore, and a config restore that
never trades working alert rules for unusable ones.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_restore import _seed, _snapshot_of, _rows, H

MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def _fresh_restore_state(temp_env):
    from app import restore
    restore.JOB.clear()
    restore.JOB["state"] = "idle"
    restore._CHALLENGE = None
    restore._TASK = None
    yield
    restore.JOB.clear()
    restore.JOB["state"] = "idle"
    restore._CHALLENGE = None
    restore._TASK = None


def _columns(path: str | Path, table: str = "observations") -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _pre_copies(live: Path) -> list[Path]:
    from app import restore
    return sorted(live.parent.glob(f"{live.name}{restore.PRE_RESTORE_SUFFIX}-*.db"))


# ───────────────────────── R21-02: validate before, roll back after ──────────

def test_an_incomplete_schema_is_refused_before_anything_live_moves(client, temp_env):
    """The reviewer's two-column `observations` passed the subset check,
    replaced the live file and then failed init_db with the unusable file
    left live. Now the candidate is migrated and measured against the
    schema a fresh database has, and refused while the live one is
    untouched."""
    from app import restore
    _seed(client, 3)
    live = Path(temp_env)
    upload = live.parent / "incomplete.db"
    with sqlite3.connect(upload) as con:
        con.executescript("CREATE TABLE observations(dateutc_ms INTEGER);"
                          "CREATE TABLE server_kv(k TEXT PRIMARY KEY, v TEXT);")
    before = _columns(live)
    asyncio.run(restore._validate_and_swap(upload, live))
    st = restore.status()
    # Refused either by the migration itself (an ALTER that names a column
    # the file lacks) or by the schema comparison; both are before cutover.
    assert st["state"] == "error", st
    assert "schema" in st["error"] or "missing" in st["error"], st
    assert "mac" in st["error"], st
    assert _columns(live) == before and _rows(temp_env) == 3
    assert not _pre_copies(live), "the live database must not have moved"
    assert not restore._marker(live).exists()
    assert not upload.exists(), "a refused upload is not kept"


def test_a_failed_second_rename_puts_the_old_database_back(client, temp_env, monkeypatch):
    """Injected failure of the upload→live rename used to leave the live
    path ABSENT with the original only under its pre-restore name (the
    next boot would have created an empty database)."""
    from app import restore
    _seed(client, 3)
    live = Path(temp_env)
    upload = live.parent / "valid.db"
    _snapshot_of(temp_env, upload)
    real_replace = restore.os.replace

    def fail_upload(src, dest):
        if Path(src) == upload:
            raise OSError("injected second-rename failure")
        return real_replace(src, dest)
    monkeypatch.setattr(restore.os, "replace", fail_upload)
    asyncio.run(restore._validate_and_swap(upload, live))
    assert restore.status()["state"] == "error"
    assert live.exists() and _rows(temp_env) == 3
    assert not _pre_copies(live), "rolled back: the old file is live again, not a copy"
    assert not restore._marker(live).exists()
    # And the server still serves it.
    assert client.get("/api/devices", headers=H).status_code == 200


def test_an_init_failure_after_the_swap_rolls_back(client, temp_env, monkeypatch):
    """The candidate passed preparation but init_db on the live path
    fails (a migration that only bites in place): the previous database
    returns under its own name before anyone can connect to the bad one."""
    from app import db, restore
    _seed(client, 3)
    live = Path(temp_env)
    other = live.parent / "other.db"
    _snapshot_of(temp_env, other)
    with sqlite3.connect(other) as con:
        con.execute("DELETE FROM observations")
        con.commit()
    real_init = db.init_db

    async def init_that_fails_live(path=None):
        if path is None:
            raise RuntimeError("injected init failure on the live path")
        return await real_init(path)
    monkeypatch.setattr(db, "init_db", init_that_fails_live)
    asyncio.run(restore._validate_and_swap(other, live))
    st = restore.status()
    assert st["state"] == "error" and "previous database is back" in st["error"], st
    assert _rows(temp_env) == 3, "the old data is live again"
    assert not _pre_copies(live) and not restore._marker(live).exists()


def test_boot_recovery_reinstalls_the_pre_restore_copy(client, temp_env):
    """Process death between the two renames: the marker names the
    pre-restore copy and the live path is empty. Boot puts the copy back
    and NEVER creates an empty database under the vanished name."""
    from app import db, restore
    _seed(client, 4)
    live = Path(temp_env)
    pre = live.with_name(f"{live.name}{restore.PRE_RESTORE_SUFFIX}-dead.db")
    restore._marker(live).write_text(json.dumps({"pre": str(pre)}))
    restore._checkpoint(live)            # as swap_in does before its rename
    restore.os.replace(live, pre)
    assert not live.exists()
    note = restore.recover_at_boot(live)
    assert note and "back in place" in note
    assert live.exists() and not pre.exists() and not restore._marker(live).exists()
    asyncio.run(db.init_db())
    assert _rows(temp_env) == 4
    # A marker beside a USABLE database (killed after the commit point but
    # before the unlink) is simply cleared.
    restore._marker(live).write_text(json.dumps({"pre": str(pre)}))
    assert "usable" in (restore.recover_at_boot(live) or "")
    assert _rows(temp_env) == 4 and not restore._marker(live).exists()
    assert restore.recover_at_boot(live) is None


def test_earlier_pre_restore_copies_survive_until_the_commit(client, temp_env, monkeypatch):
    """Pruning used to run inside swap_in; a failure after it had no net.
    Now the older copy is pruned only once the restored file proved
    usable, and a failed restore leaves it where it was."""
    from app import db, restore
    _seed(client, 2)
    live = Path(temp_env)
    older = live.with_name(f"{live.name}{restore.PRE_RESTORE_SUFFIX}-older.db")
    older.write_bytes(b"older copy")
    upload = live.parent / "valid.db"
    _snapshot_of(temp_env, upload)
    real_init = db.init_db

    async def init_that_fails_live(path=None):
        if path is None:
            raise RuntimeError("boom")
        return await real_init(path)
    monkeypatch.setattr(db, "init_db", init_that_fails_live)
    asyncio.run(restore._validate_and_swap(upload, live))
    assert restore.status()["state"] == "error"
    assert older.exists(), "an earlier safety net must outlive a failed restore"
    monkeypatch.setattr(db, "init_db", real_init)
    _snapshot_of(temp_env, upload)
    asyncio.run(restore._validate_and_swap(upload, live))
    assert restore.status()["state"] == "done"
    assert not older.exists() and len(_pre_copies(live)) == 1


# ───────────────────────── R21-01: the lease ─────────────────────────────

def test_the_lease_drains_open_connections_and_parks_new_ones(client, monkeypatch):
    """Holding the gate: a connection open BEFORE the lease delays it
    until closed (nothing can commit into the file about to be renamed),
    a write started DURING the lease waits and lands afterwards."""
    from app import db

    async def scenario():
        held = db.connect()
        conn = await held.__aenter__()
        await conn.execute("SELECT 1")
        lease_entered = asyncio.Event()

        async def hold_lease():
            async with db.maintenance_lease(drain_timeout_s=5.0):
                lease_entered.set()
                await asyncio.sleep(0.2)
        holder = asyncio.create_task(hold_lease())
        await asyncio.sleep(0.05)
        assert not lease_entered.is_set(), "the lease must wait for the open connection"
        assert not db.is_open()
        await held.__aexit__(None, None, None)
        await asyncio.wait_for(lease_entered.wait(), 2.0)
        # A write during the lease waits, then lands.
        writer = asyncio.create_task(db.set_kv("during_lease", "landed"))
        await asyncio.sleep(0.05)
        assert not writer.done()
        await holder
        await asyncio.wait_for(writer, 2.0)
        assert db.is_open()
        return await db.get_kv("during_lease")
    assert asyncio.run(scenario()) == "landed"


def test_a_lease_that_cannot_drain_gives_up_and_reopens(client):
    from app import db

    async def scenario():
        async with db.connect() as conn:
            await conn.execute("SELECT 1")
            with pytest.raises(db.LeaseBusy):
                async with db.maintenance_lease(drain_timeout_s=0.1):
                    pass
            assert db.is_open()
        await db.set_kv("after", "ok")
        return await db.get_kv("after")
    assert asyncio.run(scenario()) == "ok"


def test_requests_during_the_swap_get_a_503_with_retry_after(client, temp_env, monkeypatch):
    """A real restore with a slow swap: an API request that cannot get a
    connection inside the wait is told to retry (never served from the
    wrong file); the same request succeeds once the swap is done."""
    from app import db, restore
    _seed(client, 2)
    live = Path(temp_env)
    upload = live.parent / "valid.db"
    _snapshot_of(temp_env, upload)
    monkeypatch.setattr(db, "GATE_WAIT_S", 0.05)
    real_swap = restore.swap_in

    def slow_swap(u, l, stamp=None):
        import time as _t
        _t.sleep(0.6)
        return real_swap(u, l, stamp)
    monkeypatch.setattr(restore, "swap_in", slow_swap)

    async def scenario():
        job = asyncio.create_task(restore._validate_and_swap(upload, live))
        while restore.status()["state"] != "swapping":
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        r = await asyncio.to_thread(client.get, "/api/devices", headers=H)
        await job
        r2 = await asyncio.to_thread(client.get, "/api/devices", headers=H)
        return r, r2
    r, r2 = asyncio.run(scenario())
    assert r.status_code == 503 and r.headers.get("retry-after") == "5", r.text
    assert r2.status_code == 200
    assert restore.status()["state"] == "done"


def test_the_write_lock_probe_does_not_call_a_held_lease_stuck(client, monkeypatch):
    from app import db, main
    monkeypatch.setattr(db, "_GATE_CLOSED", True)
    try:
        assert main._probe_write_lock() is True
    finally:
        monkeypatch.setattr(db, "_GATE_CLOSED", False)


# ───────────────────────── R21-03: reconcile ─────────────────────────────

class _FakeClient:
    def __init__(self, *a, **k):
        self.args = a

    async def aclose(self):
        pass


class _FakePoller:
    instances: list["_FakePoller"] = []

    def __init__(self, client, *a, **k):
        self.client = client
        self.started = False
        self.stopped = False
        _FakePoller.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def _stub_awn(monkeypatch):
    from app import ambient_client, poller
    _FakePoller.instances.clear()
    monkeypatch.setattr(ambient_client, "AmbientWeatherClient", _FakeClient)
    monkeypatch.setattr(poller, "Poller", _FakePoller)


def test_a_restore_starts_the_pollers_the_restored_file_configures(client, temp_env, monkeypatch):
    """The reviewer's probe inverted: a fresh server with AWN off restores
    a snapshot carrying AWN credentials and comes out with the AWN poller
    running, without a restart or a settings edit."""
    from app import db, integrations, restore
    _stub_awn(monkeypatch)
    live = Path(temp_env)
    upload = live.parent / "configured.db"

    async def set_credentials():
        await db.set_kv(integrations._kv_key("awn", "application_key"), "review-app-key")
        await db.set_kv(integrations._kv_key("awn", "api_key"), "review-api-key")
    asyncio.run(set_credentials())
    _snapshot_of(temp_env, upload)
    asyncio.run(integrations.clear("awn"))
    manager = client.app.state.integration_manager
    assert "awn" not in manager._pollers
    from app import main as _main
    _main._RECORDS_CACHE["stale"] = (0.0, {})
    asyncio.run(restore._validate_and_swap(upload, live))
    st = restore.status()
    assert st["state"] == "done" and "warning" not in st, st
    assert asyncio.run(integrations.effective("awn"))["api_key"] == "review-api-key"
    poller = manager._pollers.get("awn")
    assert isinstance(poller, _FakePoller) and poller.started
    assert poller.client.args[:2] == ("review-app-key", "review-api-key")
    assert "stale" not in _main._RECORDS_CACHE, "process caches drop with the file"


def test_a_restore_stops_and_replaces_pollers_to_match_the_file(client, temp_env, monkeypatch):
    """A changed credential over an active server is a new poller; a
    snapshot without the provider stops the running one."""
    from app import db, integrations, restore
    _stub_awn(monkeypatch)
    live = Path(temp_env)
    manager = client.app.state.integration_manager
    blank = live.parent / "blank.db"
    _snapshot_of(temp_env, blank)              # no AWN at all

    async def configure(key):
        await db.set_kv(integrations._kv_key("awn", "application_key"), "app")
        await db.set_kv(integrations._kv_key("awn", "api_key"), key)
        await manager.apply("awn")
    asyncio.run(configure("first"))
    first = manager._pollers["awn"]
    assert isinstance(first, _FakePoller) and first.client.args[1] == "first"
    changed = live.parent / "changed.db"
    asyncio.run(db.set_kv(integrations._kv_key("awn", "api_key"), "second"))
    _snapshot_of(temp_env, changed)
    asyncio.run(db.set_kv(integrations._kv_key("awn", "api_key"), "first"))
    asyncio.run(restore._validate_and_swap(changed, live))
    assert restore.status()["state"] == "done"
    second = manager._pollers["awn"]
    assert second is not first and first.stopped and second.started
    assert second.client.args[1] == "second"
    asyncio.run(restore._validate_and_swap(blank, live))
    assert restore.status()["state"] == "done"
    assert "awn" not in manager._pollers and second.stopped


def test_a_failing_reconcile_hook_is_reported_not_hidden(client, temp_env, monkeypatch):
    from app import restore
    _seed(client, 1)
    live = Path(temp_env)
    upload = live.parent / "valid.db"
    _snapshot_of(temp_env, upload)

    async def bad_hook():
        raise RuntimeError("poller refused to start")
    monkeypatch.setattr(restore, "POST_SWAP_HOOKS", [bad_hook])
    asyncio.run(restore._validate_and_swap(upload, live))
    st = restore.status()
    assert st["state"] == "done" and "poller refused to start" in st["warning"]


# ───────────────────────── R21-05: config restore ────────────────────────

def _rules(client):
    return client.get("/api/alerts/rules", headers=H).json()


def test_an_invalid_rule_file_leaves_the_existing_rules_alone(client):
    from app import config_backup, db

    async def scenario():
        await db.create_alert_rule(None, "tempf", "above", 95)
        bad = await config_backup.import_config({"version": 1, "alert_rules": [
            {"field": "typo_temperature", "comparator": "invalid", "threshold": 10}
        ], "alert_prefs": {"repeat_hours": 0}})
        mixed = await config_backup.import_config({"version": 1, "alert_rules": [
            {"field": "tempf", "comparator": "below", "threshold": 32},
            {"field": "tempf", "comparator": "sideways", "threshold": 1},
        ], "alert_prefs": {"repeat_hours": 0}})
        # A file that carries nothing but bad rules fails with the reason,
        # not "is it a backup?".
        with pytest.raises(config_backup.RestoreError, match="not valid"):
            await config_backup.import_config({"version": 1, "alert_rules": [
                {"field": "nope", "comparator": "above", "threshold": 1}]})
        return bad, mixed, await db.list_alert_rules()
    bad, mixed, rules = asyncio.run(scenario())
    assert bad["alert_rules"] == 0 and len(bad["alert_rules_rejected"]) == 1
    assert "unknown field" in bad["alert_rules_rejected"][0]
    assert mixed["alert_rules"] == 0 and "unknown comparator" in mixed["alert_rules_rejected"][0]
    assert "left as they were" in mixed["alert_rules_error"]
    assert len(rules) == 1 and rules[0]["field"] == "tempf" and rules[0]["comparator"] == "above"


def test_a_valid_rule_file_replaces_the_set_and_an_empty_list_clears_it(client):
    from app import config_backup, db

    async def scenario():
        await db.create_alert_rule(None, "tempf", "above", 95)
        ok = await config_backup.import_config({"version": 1, "alert_rules": [
            {"field": "humidity", "comparator": "below", "threshold": 20, "enabled": False,
             "severity": "major"},
            {"field": "windspeedmph", "comparator": "above", "threshold": 30},
        ]})
        after = await db.list_alert_rules()
        cleared = await config_backup.import_config({"version": 1, "alert_rules": [],
                                                     "alert_prefs": {"repeat_hours": 0}})
        return ok, after, cleared, await db.list_alert_rules()
    ok, after, cleared, none = asyncio.run(scenario())
    assert ok["alert_rules"] == 2 and "alert_rules_rejected" not in ok
    assert {(r["field"], r["comparator"], bool(r["enabled"])) for r in after} == {
        ("humidity", "below", False), ("windspeedmph", "above", True)}
    assert cleared["alert_rules"] == 0 and none == []


def test_a_malformed_device_threshold_or_location_is_skipped(client):
    from app import config_backup, db

    async def scenario():
        out = await config_backup.import_config({"version": 1,
            "device_alert_prefs": {MAC: {"monitor": True, "threshold_min": "lots"},
                                   "BB:BB:BB:BB:BB:BB": {"monitor": True, "threshold_min": 12}},
            "device_locations": {MAC: {"lat": 133.3, "lon": -111.9},
                                 "BB:BB:BB:BB:BB:BB": {"lat": 33.3, "lon": -111.9}}})
        return out, await db.get_device_alert_prefs(), await db.device_locations()
    out, prefs, locs = asyncio.run(scenario())
    assert out["device_alert_prefs"] == 1 and out["device_locations"] == 1
    assert MAC not in prefs and MAC not in locs


# ───────────────────────── R21-07: unknown is not zero ───────────────────

def test_the_challenge_says_whether_the_row_count_is_known(client, monkeypatch):
    from app import restore
    r = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert r["rows_known"] is True and r["current_rows"] == 0

    async def boom():
        raise RuntimeError("no table")
    monkeypatch.setattr(restore, "current_rows", boom)
    r = client.post("/api/backup/database/restore/challenge", headers=H).json()
    assert r["rows_known"] is False and r["current_rows"] is None
