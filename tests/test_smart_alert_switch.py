"""The smart-alert master switch gets a home outside the environment (2.4).

`SMART_ALERTS` was an env var, it defaults to off, and no app could set
it. So everything behind that gate — lightning proximity and its 30
minute all clear, frost, heat, rapid pressure drop, first frost of the
season, and the battery / sensor-quiet health alerts — had never fired
for a single user. Doren asked for lightning notifications on
2026-09-21 not knowing the server had shipped them in 1.8.

The rules this pins down:
  · the row wins when it is set, the env decides when it is NULL, so an
    operator who configured the env keeps exactly what they configured;
  · GET reports the EFFECTIVE value, because the app draws a switch
    from it now rather than a read-only fact about the server;
  · the column reaches a LIVE database through the ALTER list, which is
    the only path that touches a table that already exists.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

HEADERS = {"Authorization": "Bearer test-api-token"}


def _get(client) -> dict:
    r = client.get("/api/alerts", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_env_still_decides_when_nobody_has_set_the_switch(client):
    assert _get(client)["smart_alerts_enabled"] is False


def test_the_env_shows_through_when_an_operator_set_it(client, monkeypatch):
    from app import config, main
    monkeypatch.setattr(config.settings, "smart_alerts", True)
    monkeypatch.setattr(main.settings, "smart_alerts", True)
    assert _get(client)["smart_alerts_enabled"] is True


def test_the_app_can_turn_it_on_and_off(client):
    r = client.put("/api/alerts", json={"smart_alerts": True}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _get(client)["smart_alerts_enabled"] is True

    r = client.put("/api/alerts", json={"smart_alerts": False}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _get(client)["smart_alerts_enabled"] is False


def test_off_in_the_row_beats_on_in_the_env(client, monkeypatch):
    """The row is the owner's decision and the env is the default it
    overrides. A switch that could not turn the feature OFF again would
    be a one-way door."""
    from app import config, main
    monkeypatch.setattr(config.settings, "smart_alerts", True)
    monkeypatch.setattr(main.settings, "smart_alerts", True)
    client.put("/api/alerts", json={"smart_alerts": False}, headers=HEADERS)
    assert _get(client)["smart_alerts_enabled"] is False


def test_another_write_never_clears_the_switch(client):
    """Every field on this route is omit-to-leave-alone. Saving the quiet
    hours must not silently turn the smart alerts off."""
    client.put("/api/alerts", json={"smart_alerts": True}, headers=HEADERS)
    r = client.put("/api/alerts",
                   json={"quiet_start_min": 1320, "quiet_end_min": 420},
                   headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _get(client)["smart_alerts_enabled"] is True


def test_the_monitor_reads_the_row_not_the_env(client, monkeypatch):
    """The gate in alerts.py used `settings.smart_alerts` directly. If it
    still did, the switch would change the API's answer and nothing
    else."""
    from app import alerts, config
    monkeypatch.setattr(config.settings, "smart_alerts", False)
    client.put("/api/alerts", json={"smart_alerts": True}, headers=HEADERS)
    cfg = asyncio.run(alerts.effective_config())
    assert cfg.smart_alerts is True


def test_a_guest_token_cannot_flip_it(client):
    r = client.put("/api/alerts", json={"smart_alerts": True},
                   headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_the_column_reaches_a_database_that_already_exists(client):
    """CREATE IF NOT EXISTS never adds a column to a live table, so the
    ALTER list is the only path onto an upgrading server — and without
    it get_alert_prefs' SELECT raises "no such column" and takes the
    whole alert system down on boot."""
    from app import db as dbmod
    from app.config import settings

    path = settings.database_path
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE alert_prefs DROP COLUMN smart_alerts")
    cols = {r[1] for r in raw.execute("PRAGMA table_info(alert_prefs)")}
    assert "smart_alerts" not in cols, "precondition: the column is gone"
    raw.commit()
    raw.close()

    asyncio.run(dbmod.init_db())

    raw = sqlite3.connect(path)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(alert_prefs)")}
    raw.close()
    assert "smart_alerts" in cols
    # And the server answers again rather than 500ing on the SELECT.
    assert _get(client)["smart_alerts_enabled"] is False


def test_the_migration_is_idempotent(client):
    """init_db runs on every boot; the second one must be a no-op rather
    than a duplicate-column error that crash-loops the server."""
    from app import db as dbmod
    asyncio.run(dbmod.init_db())
    asyncio.run(dbmod.init_db())
    assert _get(client)["smart_alerts_enabled"] is False


def test_the_switch_survives_a_backup_and_restore(client):
    """R22-06 in its loudest form. A restore that dropped this would
    switch the whole derived-alert pillar back off without saying so."""
    client.put("/api/alerts", json={"smart_alerts": True}, headers=HEADERS)
    backup = client.get("/api/config/backup", headers=HEADERS).json()
    assert backup["alert_prefs"]["smart_alerts"] == 1

    client.put("/api/alerts", json={"smart_alerts": False}, headers=HEADERS)
    assert _get(client)["smart_alerts_enabled"] is False

    r = client.post("/api/config/restore", json=backup, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _get(client)["smart_alerts_enabled"] is True


def test_a_fired_edge_does_not_outlive_the_switch(client, monkeypatch):
    """With the switch off nothing records a clearance, so a triggered
    row sat there until re-enable and the next crossing read as already
    fired (CodeRabbit, PR #40). Off, the monitor forgets the edges; on,
    it leaves them alone."""
    from app import alerts, db

    async def no_deliver(*a, **k):
        return True
    monkeypatch.setattr(alerts, "_deliver", no_deliver)

    async def push_on():
        return True
    # The tick stands down entirely when no channel is open.
    monkeypatch.setattr(alerts.apns, "push_configured", push_on)
    mac = "AA:BB:CC:00:00:01"

    async def seed_and_tick():
        await db.upsert_smart_alert_state(mac, "frost", 1, 1)
        await alerts.AlertMonitor()._tick()
        return await db.get_smart_alert_states()

    client.put("/api/alerts", json={"smart_alerts": False}, headers=HEADERS)
    assert asyncio.run(seed_and_tick()) == {}
    client.put("/api/alerts", json={"smart_alerts": True}, headers=HEADERS)
    assert asyncio.run(seed_and_tick()).get((mac, "frost")) == 1
