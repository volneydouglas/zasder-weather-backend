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
    that's torn down at the end of the test."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)  # let init_db create it fresh
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("API_TOKEN", "test-api-token")
    monkeypatch.setenv("INGEST_TOKEN", "test-ingest-token")
    monkeypatch.setenv("CAPTURE_TOKEN", "test-capture-token")
    # AWN keys intentionally LEFT UNSET — most tests don't need the poller
    # and we want to exercise the AcuRite-only path.
    yield db_path
    if os.path.exists(db_path): os.unlink(db_path)


@pytest.fixture
def client(temp_env: str):
    """FastAPI TestClient with a freshly-imported app + isolated DB."""
    # Force re-import so settings + app pick up the env we just set.
    for mod in ["app.config", "app.db", "app.capture", "app.ingest", "app.main"]:
        if mod in importlib.sys.modules: importlib.reload(importlib.sys.modules[mod])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
