"""Pytest fixtures.

The app and config modules read environment variables at import time, so
every fixture sets env vars *before* the first import.  Tests that need a
fresh app (with new env / fresh DB) use the `client` fixture which builds
a TestClient against a fully-reloaded app instance.
"""
from __future__ import annotations

import importlib
import os
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
    # AWN keys intentionally LEFT UNSET — most tests don't need the poller.
    # Also actively unset them in case the host shell has them, so the
    # poller stays disabled (it tries to hit the real AWN API otherwise).
    monkeypatch.delenv("AW_APPLICATION_KEY", raising=False)
    monkeypatch.delenv("AW_API_KEY", raising=False)
    yield db_path
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def client(temp_env: str):
    """FastAPI TestClient with a freshly-imported app + isolated DB."""
    # Force re-import so settings + app pick up the env we just set.
    for mod in ["app.config", "app.db", "app.capture", "app.ingest",
                "app.discovery", "app.alerts", "app.main"]:
        if mod in importlib.sys.modules: importlib.reload(importlib.sys.modules[mod])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
