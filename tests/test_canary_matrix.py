"""R5-47: the content-canary matrix.

The auth matrix (test_security_invariants) proves limited tokens can't REACH
write/operator routes, and it is systematic — a new route is scanned without
anyone remembering to add a test. But the "what a limited token CAN reach
must not LEAK" layer was hand-written for exactly two endpoints, so a future
PII-echoing read route ships green.

This module closes that: seed distinctive canary values into every sensitive
store, then walk EVERY GET route the scanner finds — as an app-minted share
token AND unauthenticated — and assert no canary string appears in any
response body, regardless of status code. New endpoints join the sweep
automatically, the same way they join the auth matrix.

Coordinates are the one nuance: since the 1.7 rounding change, limited reads
legitimately carry TOWN-ROUNDED coords, so the needles here are the precise
values — chosen so their rounded forms (33.4 / -111.7) share no substring
with them.
"""
import os
import re

os.environ.setdefault("API_TOKEN", "test-api-token")

import httpx
import pytest

# The tests directory is not a package, so the sibling's route scanner is
# loaded by file path — one scanner, two matrices, no drift.
import importlib.util as _ilu
import pathlib as _pl

_spec = _ilu.spec_from_file_location(
    "sec_invariants", _pl.Path(__file__).with_name("test_security_invariants.py"))
_sec = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_sec)
_routes = _sec._routes

H = {"Authorization": "Bearer test-api-token"}

CANARY_MAC = "AA:BB:CC:DD:09:11"

# Precise-value needles. Every one is seeded below; none may appear in any
# GET response served to a limited or anonymous caller.
NEEDLES = [
    "33.4242", "-111.6868",            # precise home coords (rounded ok)
    "Canary House",                    # location label
    "canary-smtp.example.net",         # SMTP transport identity
    "canary-user@example.net",
    "CANARY-SMTP-PASS",                # write-only secret
    "canary-from@example.net",
    "canary-rcpt@example.net",         # alert recipient
    "CANARYWUKEY0123456789",           # WU/TWC key
    "CANARYTEMPESTTOKEN",              # integration secret
    "CANARYRELAYTOKEN",                # push-relay credential
    "test-api-token",                  # the operator bearer itself
    "test-ingest-token",
]


class _NoNetwork:
    """httpx.AsyncClient stand-in: any outbound call fails fast. Proxy
    routes (forecast, normals, nowcast) then answer with error bodies —
    which the sweep still checks — instead of touching the network."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        raise RuntimeError("no network inside the canary sweep")

    async def post(self, *a, **k):
        raise RuntimeError("no network inside the canary sweep")

    async def aclose(self):
        return None


@pytest.fixture()
def seeded(client, monkeypatch):
    """Plant every canary, mint a share token, return its header."""
    import asyncio
    from app import db, integrations
    from app.config import settings

    monkeypatch.setattr(httpx, "AsyncClient", _NoNetwork)
    # The integrations PUT normally probes upstream and (re)starts pollers —
    # both are network. The stored values are what the sweep cares about.
    async def _no_probe(provider):
        return None

    async def _no_apply(self, provider):
        return None

    monkeypatch.setattr(integrations, "probe", _no_probe)
    monkeypatch.setattr(integrations.IntegrationManager, "apply", _no_apply,
                        raising=False)
    monkeypatch.setattr(settings, "public_dashboard", True)

    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDD0911",
                                     "name": "CanaryStation"},
                          "timestamp_utc": "2026-08-23T12:00:00Z",
                          "outdoor": {"tempf": 88.0}})
    assert r.status_code == 200
    assert client.put(f"/api/devices/{CANARY_MAC}/location", headers=H,
                      json={"lat": 33.4242, "lon": -111.6868,
                            "label": "Canary House"}).status_code == 200
    assert client.put("/api/alerts", headers=H, json={
        "smtp_host": "canary-smtp.example.net",
        "smtp_username": "canary-user@example.net",
        "smtp_password": "CANARY-SMTP-PASS",
        "smtp_from": "canary-from@example.net",
        "recipients": ["canary-rcpt@example.net"],
    }).status_code == 200
    assert client.put("/api/config/wu-key", headers=H,
                      json={"api_key": "CANARYWUKEY0123456789"}
                      ).status_code == 200
    r = client.put("/api/integrations/tempest", headers=H,
                   json={"token": "CANARYTEMPESTTOKEN", "station_id": "1"})
    assert r.status_code == 200, r.text
    asyncio.run(db.set_push_relay("https://relay.example.net",
                                  "CANARYRELAYTOKEN"))

    minted = client.post("/api/guest-tokens", headers=H,
                         json={"label": "canary"}).json()["token"]
    return {"Authorization": f"Bearer {minted}"}, minted


def _get_routes():
    """Every GET in the app, path params filled with plausible values."""
    fills = {"mac": CANARY_MAC.replace(":", ""), "rule_id": "1",
             "token_id": "zwg_00000000", "provider": "tempest"}
    out = []
    for guard, method, path, _f in _routes():
        if method != "GET" or "{" not in path and not path.startswith("/"):
            continue
        if method != "GET":
            continue
        filled = re.sub(r"\{([^}:]+)[^}]*\}",
                        lambda m: fills.get(m.group(1), "1"), path)
        out.append(filled)
    return sorted(set(out))


def test_no_canary_reaches_a_limited_or_anonymous_reader(seeded, client):
    guest_header, minted = seeded
    needles = NEEDLES + [minted]     # the full share token is a secret too
    swept = 0
    for path in _get_routes():
        for label, headers in (("guest", guest_header), ("anon", {})):
            try:
                r = client.get(path, headers=headers)
            except Exception:
                # raise_server_exceptions surfaces handler crashes (e.g. the
                # no-network stub) as exceptions — no response body exists,
                # so nothing leaked on this path.
                continue
            swept += 1
            for needle in needles:
                assert needle not in r.text, (
                    f"{label} GET {path} ({r.status_code}) leaked {needle!r}")
    # Vacuous-guard check, same discipline as the auth matrix: a broken
    # route scan must fail loudly, not sweep nothing and pass.
    assert swept > 30, f"canary sweep only covered {swept} responses"


def test_owner_still_sees_their_own_config(seeded, client):
    """Sanity for the fixture: the canaries really are stored and readable
    by the operator — otherwise the sweep above proves nothing."""
    body = client.get("/api/alerts", headers=H).text
    assert "canary-smtp.example.net" in body
    devs = client.get("/api/devices", headers=H).text
    assert "33.4242" in devs and "Canary House" in devs
