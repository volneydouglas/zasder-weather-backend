"""Pytest fixtures.

The app and config modules read environment variables at import time, so
every fixture sets env vars *before* the first import.  Tests that need a
fresh app (with new env / fresh DB) use the `client` fixture which builds
a TestClient against a fully-reloaded app instance.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from collections.abc import Iterator

import pytest


@pytest.fixture
def temp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set the env vars the app needs and point DATABASE_PATH at a temp file
    inside a per-test directory that's torn down at the end of the test.
    Per-test directory isolation matters for siblings of the DB file like
    /captures (capture logs) — they're
    derived from DATABASE_PATH's parent."""
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="zw-test-")
    db_path = os.path.join(tmpdir, "weather.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("API_TOKEN", "test-api-token")
    monkeypatch.setenv("INGEST_TOKEN", "test-ingest-token")
    monkeypatch.setenv("CAPTURE_TOKEN", "test-capture-token")
    monkeypatch.setenv("REVIEWER_API_TOKEN", "test-reviewer-token")
    # Deterministic local-time math (insights rollups, records periods):
    # without this a developer's .env TIMEZONE leaks in and moves values
    # across day/hour boundaries between machines.
    monkeypatch.setenv("TIMEZONE", "UTC")
    # Disable the AWN poller. Must be setenv("") NOT delenv(): Settings reads
    # env_file=".env", so merely deleting the process env let pydantic fall back
    # to the developer's real .env keys — every test then booted the poller and
    # hit the live AWN API (slow, rate-limited, flaky, and it printed the keys
    # into pytest output on a 429). An explicit empty value overrides the file
    # and reads as "not configured" (see Settings.aw_configured).
    monkeypatch.setenv("AW_APPLICATION_KEY", "")
    monkeypatch.setenv("AW_API_KEY", "")
    # Same reasoning for the other credential-bearing pollers/integrations, so
    # the suite never reaches the network.
    # (WEATHERLINK_STATION_ID is typed int — blanking the key/secret is enough
    # to disable that poller, and "" would fail Settings validation.)
    for var in ("WEATHERLINK_API_KEY", "WEATHERLINK_API_SECRET", "MQTT_HOST",
                "ALERT_EMAIL_TO", "SMTP_HOST",
                # Push: without these a developer .env would let a test run send
                # REAL notifications to real devices via APNs/FCM or the relay.
                "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY_P8", "APNS_TOPIC",
                "APNS_RELAY_URL", "APNS_RELAY_TOKEN",
                "FCM_SERVICE_ACCOUNT_JSON", "FCM_SERVICE_ACCOUNT_FILE"):
        monkeypatch.setenv(var, "")
    # Module-level caches must not leak across tests (the public dashboard HTML
    # and the per-MAC records cache are process-global by design). Reset only a
    # module that is ALREADY imported: a module nobody imported yet holds no
    # state to leak, and importing it here would bind app.main to this test's
    # env ahead of the deliberate re-import in the `client` fixture. Deliberately
    # unguarded — renaming a cache should fail the suite loudly, not silently
    # stop isolating tests.
    _m = sys.modules.get("app.main")
    if _m is not None:
        _m._PUBLIC_DASH_CACHE = None
        _m._PUBLIC_DASH_LOCK = None     # rebound to each test's event loop
        _m._RECORDS_CACHE.clear()
        _m._RECORDS_LOCKS.clear()
    yield db_path
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def client(temp_env: str):
    """FastAPI TestClient with a freshly-imported app + isolated DB."""
    # Force re-import so settings + app pick up the env we just set.
    # app.apns and app.relay hold `from .config import settings` at import
    # time — leaving them out kept them bound to the FIRST test's Settings
    # instance (a stale-object trap that only passed because every test uses
    # identical tokens). app.fcm reads env at call time and needs no reload.
    # app.wu_upload holds process-global throttle/health dicts (and must be
    # reloaded BEFORE app.ingest, which imports it at module top) — the
    # app.insights precedent for modules with per-test state.
    for mod in ["app.config", "app.db", "app.insights", "app.wu_upload",
                "app.capture", "app.ingest", "app.discovery",
                "app.alerts", "app.apns", "app.relay", "app.main"]:
        if mod in importlib.sys.modules: importlib.reload(importlib.sys.modules[mod])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
