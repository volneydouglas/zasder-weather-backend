"""Options read outside Settings, with the same `.env` fallback Settings has.

pydantic-settings loads `.env` into the Settings object only; it never
exports the file to os.environ. Docker hides that (compose's env_file puts
every line in the process environment), but a bare install (uvicorn from a
checkout) that wrote UPDATE_REQUEST_FILE, ALLOWED_HOSTS or DEBUG into
`.env` had it silently ignored by every module that read os.environ
directly (mirror issue #5, adam8833). `env_value` reads the process
environment first, then the same file, so both installs behave alike.

No Settings import here: app.limits and friends read their option before
anything constructs Settings, and must stay importable on their own.
"""
from __future__ import annotations

import os
import sys

# The file Settings reads (app/config.py passes this same name), relative to
# the server's cwd exactly as pydantic resolves it.
ENV_FILE = ".env"

_CACHE: dict[str, str] | None = None


def _dotenv() -> dict[str, str]:
    """The `.env` file's values, read once per process. Empty under pytest:
    a developer's own `.env` must not reach the suite (the conftest rule for
    Settings); a test that wants the file clears `_CACHE` and `_UNDER_TEST`."""
    global _CACHE
    if _CACHE is None:
        if _UNDER_TEST():
            _CACHE = {}
        else:
            try:
                from dotenv import dotenv_values
                _CACHE = {k: v for k, v in dotenv_values(ENV_FILE).items()
                          if v is not None}
            except Exception:   # unreadable file: behave as if absent
                _CACHE = {}
    return _CACHE


def _under_pytest() -> bool:
    return "pytest" in sys.modules


_UNDER_TEST = _under_pytest


def env_value(name: str, default: str | None = "") -> str | None:
    """os.environ[name] when set (an empty value counts as set, the way
    pydantic treats it), else the `.env` line, else `default`."""
    v = os.environ.get(name)
    if v is not None:
        return v
    return _dotenv().get(name, default)
