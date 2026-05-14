"""End-to-end endpoint tests via FastAPI's TestClient.

Each test gets a fresh app + DB via the `client` fixture in conftest.py.
Covers the security boundaries the reviewer flagged: token auth on read +
write, capture endpoint gate, status-page HTML escaping."""
from __future__ import annotations

import json


# ───────────────────────── liveness + read auth ─────────────────────────

def test_healthz_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

def test_devices_requires_bearer(client):
    assert client.get("/api/devices").status_code == 401

def test_devices_accepts_bearer(client):
    r = client.get("/api/devices", headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    assert r.json() == []  # empty DB


# ───────────────────────── /ingest/custom (header form) ─────────────────────────

def _good_obs(tempf=72.5):
    return {
        "device": {"id": "AABBCCDDEEFF", "model": "Atlas"},
        "timestamp_utc": "2026-05-14T06:00:00Z",
        "outdoor": {"tempf": tempf, "humidity": 50},
        "wind": {}, "rain": {}, "pressure": {},
        "source": "acurite-atlas",
    }

def test_ingest_header_bearer_accepted(client):
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=_good_obs())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mac"] == "AA:BB:CC:DD:EE:FF"
    assert body["inserted"] == 1

def test_ingest_header_x_token_accepted(client):
    """X-Ingest-Token is supported as an alternative to Authorization."""
    r = client.post("/ingest/custom",
                    headers={"X-Ingest-Token": "test-ingest-token"},
                    json=_good_obs(tempf=80))
    assert r.status_code == 200

def test_ingest_header_bad_bearer_rejected(client):
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer wrong"},
                    json=_good_obs())
    assert r.status_code == 401

def test_ingest_header_no_auth_rejected(client):
    r = client.post("/ingest/custom", json=_good_obs())
    assert r.status_code == 401

def test_ingest_path_form_still_works(client):
    """Backwards compat: token-in-URL form remains accepted for older relays."""
    r = client.post("/ingest/custom/test-ingest-token", json=_good_obs())
    assert r.status_code == 200

def test_ingest_path_form_rejects_bad_token(client):
    r = client.post("/ingest/custom/wrong", json=_good_obs())
    assert r.status_code == 401

def test_ingest_rejects_missing_timestamp(client):
    bad = _good_obs(); bad.pop("timestamp_utc")
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=bad)
    assert r.status_code == 400


# ───────────────────────── /ingest/capture (token-gated) ─────────────────────────

def test_capture_anonymous_returns_404(client):
    """No CAPTURE_TOKEN header → 404 (not 401, so a port scanner can't tell
    the route exists)."""
    r = client.post("/ingest/capture/anything", data="x")
    assert r.status_code == 404

def test_capture_with_bearer_accepted(client):
    r = client.post("/ingest/capture/test-slug",
                    headers={"Authorization": "Bearer test-capture-token"},
                    data="hello")
    assert r.status_code == 200

def test_capture_with_query_token_accepted(client):
    """For stations that can't set headers, ?t=<token> works too."""
    r = client.post("/ingest/capture/test-slug?t=test-capture-token",
                    data="hello")
    assert r.status_code == 200

def test_capture_oversized_body_413(client):
    """A station accidentally (or maliciously) POSTing >64 KB is rejected."""
    huge = b"x" * (64 * 1024 + 1)
    r = client.post("/ingest/capture/test-slug",
                    headers={"Authorization": "Bearer test-capture-token",
                             "Content-Type": "application/octet-stream"},
                    data=huge)
    assert r.status_code == 413


# ───────────────────────── /status (XSS escaping) ─────────────────────────

def test_status_escapes_device_name(client):
    """Operator-controlled device.name is rendered through html.escape so
    a malicious payload can't become stored XSS on the public status page."""
    payload = {
        "device": {"id": "AABBCCDDEEFF",
                   "name": "<script>alert(1)</script>",
                   "location": "<img src=x onerror=alert(2)>"},
        "timestamp_utc": "2026-05-14T06:00:00Z",
        "outdoor": {"tempf": 70},
        "wind": {}, "rain": {}, "pressure": {},
        "source": "acurite-atlas",
    }
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=payload)
    assert r.status_code == 200

    page = client.get("/status").text
    # Raw payload must NOT appear; the escaped form must.
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x onerror=" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=alert(2)&gt;" in page
