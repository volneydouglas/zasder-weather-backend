"""Sensor checks (2.5, C4): what the plausibility bands refused, counted.

Refusals used to be a log line only, so a gust sensor sending 255 mph all
week looked, in the app, like a quiet week.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token", "Content-Type": "application/json"}
MAC = "AA:BB:CC:00:00:F4"


def _post(client, minutes_ago, gust):
    ts = datetime.fromtimestamp(time.time() - minutes_ago * 60, timezone.utc)
    r = client.post("/ingest/custom", headers=IH, json={
        "device": {"id": MAC, "name": "Yard"},
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdoor": {"tempf": 71.0},
        "wind": {"speed_mph": 5.0, "gust_mph": gust}})
    assert r.status_code in (200, 201, 202), r.text


def test_refusals_are_counted_per_field_and_flushed(client):
    from app import db
    for k in range(6):
        _post(client, 60 - k * 5, 255.0 if k % 2 else 12.0)
    body = client.get(f"/api/devices/{MAC}/sensor-health", headers=H).json()
    rows = {r["field"]: r for r in body["fields"]}
    # Three gusts of 255 refused; the sibling speed channel went with them
    # (the anemometer rule), so it carries refusals too.
    assert rows["windgustmph"]["rejected"] == 3
    assert rows["windgustmph"]["accepted"] == 3
    assert rows["windspeedmph"]["rejected"] == 3
    assert rows["tempf"]["rejected"] == 0 and rows["tempf"]["accepted"] == 6
    assert body["fields"][0]["rejected"] >= body["fields"][-1]["rejected"]
    # Counted before a flush; still counted after it, exactly once.
    asyncio.run(db.flush_qc_rejections())
    assert db._QC_PENDING == {}
    again = {r["field"]: r for r in client.get(
        f"/api/devices/{MAC}/sensor-health", headers=H).json()["fields"]}
    assert again["windgustmph"]["rejected"] == 3


def test_a_clean_station_reports_no_refusals(client):
    _post(client, 10, 12.0)
    rows = client.get(f"/api/devices/{MAC}/sensor-health", headers=H).json()["fields"]
    assert rows and all(r["rejected"] == 0 for r in rows)


def test_sensor_checks_need_the_token(client):
    assert client.get(f"/api/devices/{MAC}/sensor-health").status_code == 401


def test_a_failed_flush_keeps_its_counts_for_the_next_tick(client, monkeypatch):
    """Greptile, PR #48: the counts left memory before the write, so a
    locked database lost them and the sensor checks undercounted."""
    import contextlib
    from app import db
    db._QC_PENDING.clear()
    db.note_rejections("AA:BB:CC:00:00:77", ["windgustmph=255"])
    real = db.connect

    @contextlib.asynccontextmanager
    async def broken():
        raise RuntimeError("database is locked")
        yield  # pragma: no cover
    monkeypatch.setattr(db, "connect", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(db.flush_qc_rejections())
    db.note_rejections("AA:BB:CC:00:00:77", ["windgustmph=255"])
    assert sum(db._QC_PENDING.values()) == 2, "the failed flush's count is back"
    monkeypatch.setattr(db, "connect", real)
    asyncio.run(db.flush_qc_rejections())
    assert not db._QC_PENDING
