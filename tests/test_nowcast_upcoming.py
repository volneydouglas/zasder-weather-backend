"""The nowcast's current prediction for the dashboard line (2.5, C12)."""
from __future__ import annotations

import asyncio
import json
import os
import time

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import db, nowcast  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}


def test_nothing_expected_is_null(client):
    assert client.get("/api/nowcast", headers=H).json() == {"rain": None}


def _rain_start(client, on: bool):
    r = client.put("/api/alerts", headers=H, json={"rain_start": on})
    assert r.status_code == 200, r.text


def test_an_expected_onset_is_served_until_an_hour_past(client):
    _rain_start(client, True)
    now = int(time.time() * 1000)
    asyncio.run(db.set_kv(nowcast._KV_STATE, json.dumps(
        {"alerted_at_ms": now, "start_ms": now + 40 * 60_000, "total_in": 0.12})))
    assert client.get("/api/nowcast", headers=H).json()["rain"] == {
        "start_ms": now + 40 * 60_000, "total_in": 0.12}
    asyncio.run(db.set_kv(nowcast._KV_STATE, json.dumps(
        {"start_ms": now - 2 * 3_600_000, "total_in": 0.1})))
    assert client.get("/api/nowcast", headers=H).json()["rain"] is None


def test_an_ended_event_is_not_expected(client):
    now = int(time.time() * 1000)
    asyncio.run(db.set_kv(nowcast._KV_STATE, json.dumps(
        {"start_ms": now + 10 * 60_000, "ended_ms": now})))
    assert client.get("/api/nowcast", headers=H).json()["rain"] is None


def test_the_nowcast_needs_the_token(client):
    assert client.get("/api/nowcast").status_code == 401


def test_nothing_is_expected_while_rain_start_is_off(client):
    """R25-12 (the 2.5 detailed review): a prediction stored before the
    owner switched Rain Start off kept driving the next-hours line."""
    _rain_start(client, True)
    now = int(time.time() * 1000)
    asyncio.run(db.set_kv(nowcast._KV_STATE, json.dumps(
        {"start_ms": now + 40 * 60_000, "total_in": 0.12})))
    assert client.get("/api/nowcast", headers=H).json()["rain"] is not None
    _rain_start(client, False)
    assert client.get("/api/nowcast", headers=H).json()["rain"] is None
