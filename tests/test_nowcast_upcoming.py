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


def test_an_expected_onset_is_served_until_an_hour_past(client):
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
