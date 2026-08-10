"""Health of each configured ingest source.

A cloud poller that stops working is invisible today: the station simply
stops updating and looks like dead hardware. That has already cost real
debugging time — an Atlas that went quiet was indistinguishable from a
receiver problem, a network problem, or expired API credentials, because
nothing recorded *which* leg of the chain last succeeded.

This keeps a small in-process record per source so `/api/sources` can answer
"is my AmbientWeather poller still working, and if not, what did it say?".

In-memory on purpose: it describes the health of *this* process's pollers, is
worthless after a restart, and writing it to SQLite would mean a DB write on
every poll tick for data nobody reads between restarts.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass
class SourceState:
    name: str
    configured: bool = False
    last_success_ms: int | None = None
    last_error: str | None = None
    last_error_ms: int | None = None
    consecutive_failures: int = 0
    # Rows actually stored on the most recent successful tick. Zero over a long
    # run is its own kind of failure: the credentials work and the API answers,
    # but nothing new is arriving.
    last_rows: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Process-global, like the other caches in main.py, and reset the same way in
# tests — a leaked state between tests makes assertions order-dependent.
_STATES: dict[str, SourceState] = {}
_LOCK = Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def reset() -> None:
    """Test hook. See tests/conftest.py."""
    with _LOCK:
        _STATES.clear()


def declare(name: str, configured: bool, **extra: Any) -> None:
    """Register a source at startup, whether or not it's configured.

    Declaring the unconfigured ones matters: "AmbientWeather isn't set up" and
    "AmbientWeather is set up but failing" look identical from the outside,
    and they need very different fixes.
    """
    with _LOCK:
        st = _STATES.setdefault(name, SourceState(name=name))
        st.configured = configured
        if extra:
            st.extra.update(extra)


def record_success(name: str, rows: int | None = None) -> None:
    with _LOCK:
        st = _STATES.setdefault(name, SourceState(name=name))
        st.last_success_ms = _now_ms()
        st.consecutive_failures = 0
        st.last_error = None
        if rows is not None:
            st.last_rows = rows


# Upstream errors routinely embed the request URL, and AmbientWeather takes
# its credentials as QUERY PARAMETERS — the same leak that put both AWN keys
# into the logs before ambient_client started raising a scrubbed error. This
# text is served over /api/sources, so it gets the same treatment.
_URL_QUERY = re.compile(r"([?&])([^=&\s]+)=([^&\s]*)")
_SECRETISH = re.compile(r"(?i)(key|secret|token|password|sig|auth)")
# scheme://user:password@host — the password never belongs in a stored error.
_URL_USERINFO = re.compile(r"(\w+://)([^/@\s:]+):([^/@\s]*)@")


def redact(text: str) -> str:
    """Best-effort scrub of credentials from an upstream error message.

    Covers the two shapes our clients can actually produce: credentials as
    query parameters (AmbientWeather passes both keys that way) and
    credentials in basic-auth userinfo.

    It is a denylist and therefore not a guarantee — a secret embedded in a
    PATH segment, or in a query parameter with an innocuous-looking name,
    would survive. That's accepted rather than overlooked: no client here puts
    credentials in a path, and redacting more aggressively would strip the
    station IDs and endpoints that make these errors worth reporting at all.
    """
    out = _URL_USERINFO.sub(r"\1\2:<redacted>@", text or "")

    def _sub(m: "re.Match[str]") -> str:
        sep, key, _val = m.group(1), m.group(2), m.group(3)
        return f"{sep}{key}=<redacted>" if _SECRETISH.search(key) else m.group(0)
    return _URL_QUERY.sub(_sub, out)


def record_failure(name: str, error: str) -> None:
    with _LOCK:
        st = _STATES.setdefault(name, SourceState(name=name))
        st.last_error = redact(error or "")[:300]   # bounded + credential-free
        st.last_error_ms = _now_ms()
        st.consecutive_failures += 1


def snapshot() -> list[dict[str, Any]]:
    """Serializable view, newest-trouble-first so a failing source is the
    first thing a client renders."""
    now = _now_ms()
    with _LOCK:
        states = list(_STATES.values())
    out = []
    for st in states:
        age_s = None if st.last_success_ms is None else (now - st.last_success_ms) / 1000
        out.append({
            "name": st.name,
            "configured": st.configured,
            "healthy": st.configured and st.consecutive_failures == 0
                       and st.last_success_ms is not None,
            "last_success_ms": st.last_success_ms,
            "seconds_since_success": None if age_s is None else round(age_s, 1),
            "last_error": st.last_error,
            "last_error_ms": st.last_error_ms,
            "consecutive_failures": st.consecutive_failures,
            "last_rows": st.last_rows,
            **({"extra": st.extra} if st.extra else {}),
        })
    out.sort(key=lambda d: (d["healthy"], not d["configured"], d["name"]))
    return out
