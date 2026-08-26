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
                "FCM_SERVICE_ACCOUNT_JSON", "FCM_SERVICE_ACCOUNT_FILE",
                # 1.6 (2026-08-20 review): the Tempest poller and guest
                # tokens joined config AFTER this list. A developer .env
                # carrying the live TEMPEST_TOKEN booted a real poller
                # against the WeatherFlow API in every client-fixture test —
                # and the token rides the polled URL, so a logged failure
                # prints it. A .env GUEST_API_TOKENS silently joined
                # valid_api_tokens in every auth test. (TEMPEST_STATION_ID
                # is typed int like WEATHERLINK_STATION_ID — blanking the
                # token alone disables the poller, and "" would fail
                # Settings validation.)
                # 1.8: AirGradient joined config — same live-poller trap
                # as TEMPEST_TOKEN, and the token also rides the polled URL.
                "TEMPEST_TOKEN", "AIRGRADIENT_TOKEN", "GUEST_API_TOKENS"):
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
        _m._PUBLIC_DASH_REFRESHING = False   # a test's leaked flag would
        _m._PUBLIC_DASH_REFRESH_TASK = None  # silently disable SWR forever
        _m._OBS_COUNT_CACHE.clear()
        _m._RECORDS_CACHE.clear()
        _m._RECORDS_LOCKS.clear()
        _m._DB_BACKUP_JOB = {"state": "idle"}
        _m._DB_BACKUP_TASK = None
    # 1.8 modules keep small process-global throttles; same isolation rule.
    _wp = sys.modules.get("app.widget_push")
    if _wp is not None:
        _wp._reset_for_tests()
    _st = sys.modules.get("app.share_targets")
    if _st is not None:
        _st._reset_for_tests()
    _nw = sys.modules.get("app.nws_watch")
    if _nw is not None:
        _nw._reset_for_tests()
    # Forecast snapshots now run on EVERY tick regardless of alert
    # transport (R7 R1) — without this stub, any tick-running test whose
    # device carries coords makes a REAL Open-Meteo call (15s timeout ×
    # many tests = the suite "hanging"). Tests that want the fetch
    # monkeypatch it back explicitly.
    import app.forecast_snapshots as _fs   # import NOW: the tick imports
    # it lazily, so a sys.modules.get here missed the first test and let
    # one real Open-Meteo call through per run (intermittent hang).

    async def _no_fetch(lat, lon):  # noqa: ANN001
        return None
    _fs._fetch_daily = _no_fetch
    _hw = sys.modules.get("app.health_watch")
    if _hw is not None:
        _hw._reset_for_tests()
    # app.db's guest last-used stamps are process-global for the same reason
    # (written by the sync auth dep, flushed on list) — a stale stamp from
    # one test must not flush into another test's database.
    _d = sys.modules.get("app.db")
    if _d is not None:
        _d._GUEST_LAST_USED.clear()
        _d._INGEST_LAST_USED.clear()
        # R5-33 rollup cache: a value computed against one test's DB must
        # not answer for the next test's — same isolation rule as above.
        _d._DAILY_ROLLUP_CACHE.clear()
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
                "app.alerts", "app.apns", "app.relay", "app.integrations",
                "app.main"]:
        if mod in importlib.sys.modules: importlib.reload(importlib.sys.modules[mod])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
