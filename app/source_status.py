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
    # When the CURRENT failing streak began (the first failure after the last
    # success). "Not responding since 13:27" is the sentence a user wants,
    # and it is not derivable from last_error_ms, which is the latest retry.
    failing_since_ms: int | None = None
    # Rows actually stored on the most recent successful tick. Zero over a long
    # run is its own kind of failure: the credentials work and the API answers,
    # but nothing new is arriving.
    last_rows: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Human names for the sources the apps show ("AirGradient's service has not
# answered…"). custom-ingest has no vendor behind it and is deliberately
# absent: its health is per device, not per source.
LABELS: dict[str, str] = {
    "ambientweather": "AmbientWeather",
    "davis-cloud": "WeatherLink",
    "tempest": "Tempest",
    "airgradient": "AirGradient",
    "airgradient-local": "AirGradient (local)",
    "ecowitt-cloud": "Ecowitt",
    "govee": "Govee",
}

# What a poller stamps into a device's `info.source` → the status name it
# reports under. Anything not listed (relay boards, the WLL bridge, the
# local Ecowitt push, custom scripts) is fed by a device on the user's own
# network and has no vendor service to blame or absolve.
DEVICE_SOURCES: dict[str, str] = {
    "airgradient": "airgradient",
    "airgradient-local": "airgradient-local",
    "tempest": "tempest",
    "ecowitt-cloud": "ecowitt-cloud",
    "govee": "govee",
    "davis-vp2-cloud": "davis-cloud",
    "ambientweather": "ambientweather",
}

# Error kinds, in the order the tests pin them. The point is the sentence
# the app can honestly show: a vendor outage is not the user's fault and not
# ours; bad credentials are the user's to fix; anything else is ours.
KIND_UPSTREAM = "upstream"
KIND_CREDENTIALS = "credentials"
KIND_RATE_LIMIT = "rate_limit"
KIND_OURS = "ours"

_CREDENTIALS = re.compile(
    r"(?i)\b(401|403)\b|unauthori[sz]ed|forbidden|rejected the|invalid (api )?key|"
    r"invalid token|bad credentials|authentication")
_RATE_LIMIT = re.compile(r"(?i)\b429\b|rate.?limit|too many requests|quota")
_UPSTREAM = re.compile(
    r"(?i)\bHTTP 5\d\d\b|\b5\d\d (bad gateway|service unavailable|gateway timeout|"
    r"internal server error)|timeout|timed out|connecterror|connectionerror|"
    r"remoteprotocolerror|request failed|dns|name or service|unreachable|"
    r"connection (refused|reset)|not a list|non-json|unexpected shape")


def classify(error: str | None) -> str:
    """Sort an upstream error message into who has to act."""
    text = error or ""
    if _CREDENTIALS.search(text):
        return KIND_CREDENTIALS
    if _RATE_LIMIT.search(text):
        return KIND_RATE_LIMIT
    if _UPSTREAM.search(text):
        return KIND_UPSTREAM
    return KIND_OURS


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
        st.failing_since_ms = None
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
        now = _now_ms()
        st.last_error = redact(error or "")[:300]   # bounded + credential-free
        st.last_error_ms = now
        if st.consecutive_failures == 0:
            st.failing_since_ms = now
        st.consecutive_failures += 1


def _health(st: SourceState, now: int) -> dict[str, Any]:
    age_s = None if st.last_success_ms is None else (now - st.last_success_ms) / 1000
    return {
        "name": st.name,
        "label": LABELS.get(st.name),
        "configured": st.configured,
        "healthy": st.configured and st.consecutive_failures == 0
                   and st.last_success_ms is not None,
        "last_success_ms": st.last_success_ms,
        "seconds_since_success": None if age_s is None else round(age_s, 1),
        "last_error": st.last_error,
        "last_error_kind": classify(st.last_error) if st.last_error else None,
        "last_error_ms": st.last_error_ms,
        "failing_since_ms": st.failing_since_ms,
        "consecutive_failures": st.consecutive_failures,
        "last_rows": st.last_rows,
    }


def health_for_device(info_source: str | None) -> dict[str, Any] | None:
    """The health of the poller behind a device, for `/api/devices`.

    None for a device nobody polls for (relay boards, the WLL bridge, local
    pushes): their health is their own last-seen. Also None for a poller the
    server has not declared this boot, so a stale row from a retired
    integration does not carry a phantom verdict.
    """
    name = DEVICE_SOURCES.get(info_source or "")
    if name is None:
        return None
    with _LOCK:
        st = _STATES.get(name)
        if st is None:
            return None
        return _health(st, _now_ms())


def snapshot() -> list[dict[str, Any]]:
    """Serializable view, newest-trouble-first so a failing source is the
    first thing a client renders."""
    now = _now_ms()
    with _LOCK:
        states = list(_STATES.values())
    out = []
    for st in states:
        row = _health(st, now)
        if st.extra:
            row["extra"] = st.extra
        out.append(row)
    out.sort(key=lambda d: (d["healthy"], not d["configured"], d["name"]))
    return out
