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

def test_ingest_path_form_removed(client):
    """The legacy /ingest/custom/{token} URL form was removed 2026-05-21
    (tokens in URLs leak into proxy logs). The route should 404 now,
    NOT auth-check against the token in the path."""
    r = client.post("/ingest/custom/test-ingest-token", json=_good_obs())
    assert r.status_code == 404
    r = client.post("/ingest/custom/anything-at-all", json=_good_obs())
    assert r.status_code == 404

def test_ingest_rejects_missing_timestamp(client):
    bad = _good_obs(); bad.pop("timestamp_utc")
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=bad)
    assert r.status_code == 400


# ───────────────── P1: non-finite floats must not poison observations ─────────────────

def test_ingest_strips_nan_floats_to_none(client):
    """A flaky decoder occasionally emits NaN/inf. If we store those, the
    /current read path 500s on JSON serialization. Backend must coerce
    non-finite values to None at the boundary."""
    # Python's json.dumps with allow_nan=True (the default) emits literal
    # NaN, which TestClient happily sends. We send NaN via a raw JSON body
    # to bypass any client-side validation.
    raw_body = (
        '{"device":{"id":"AABBCCDDEEFF"},'
        '"timestamp_utc":"2026-05-21T12:00:00Z",'
        '"outdoor":{"tempf":NaN,"humidity":50,"feels_like":Infinity},'
        '"source":"test"}'
    )
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token",
                             "Content-Type": "application/json"},
                    content=raw_body)
    assert r.status_code == 200, r.text
    # Read path must succeed (NOT 500) — values stored as None, not NaN
    cur = client.get("/api/devices/AA:BB:CC:DD:EE:FF/current",
                     headers={"Authorization": "Bearer test-api-token"})
    assert cur.status_code == 200
    obs = cur.json()
    assert obs["tempf"] is None  # NaN was coerced
    assert obs["feelsLike"] is None  # Infinity was coerced
    assert obs["humidity"] == 50  # well-formed numbers pass through

def test_ingest_rejects_malformed_json(client):
    """Bad JSON should 400, not 500. Reproduces the reviewer's case."""
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token",
                             "Content-Type": "application/json"},
                    content='{bad')
    assert r.status_code == 400
    assert "invalid JSON" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()

def test_ingest_rejects_empty_body(client):
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token",
                             "Content-Type": "application/json"},
                    content="")
    assert r.status_code == 400


# ───────────────── indoor block (SDR pipeline) ─────────────────

def test_ingest_indoor_block_flows_through(client):
    """The SDR relay sends temp/humidity/pressure for the indoor sensor
    via an `indoor` block. Verify it lands in tempinf/humidityin/baromrelin
    on the stored observation."""
    payload = {
        "device": {"id": "5D5D02000007D0"[:12], "name": "WS-2000 (SDR)"},
        "timestamp_utc": "2026-05-17T20:00:00Z",
        "outdoor": {"tempf": 90.1, "humidity": 18},
        "indoor": {"tempf": 72.4, "humidity": 41, "pressure_inhg": 28.47},
        "source": "fineoffset-wh24-sdr",
    }
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=payload)
    assert r.status_code == 200
    mac = r.json()["mac"]
    cur = client.get(f"/api/devices/{mac}/current",
                     headers={"Authorization": "Bearer test-api-token"})
    assert cur.status_code == 200
    obs = cur.json()
    assert obs["tempinf"] == 72.4
    assert obs["humidityin"] == 41
    assert obs["baromrelin"] == 28.47


# ───────────────── PR2: security headers + malformed input handling ─────────────────

def test_security_headers_present_on_status(client):
    r = client.get("/")
    assert r.status_code == 200
    for h in ("Content-Security-Policy", "Strict-Transport-Security",
              "X-Content-Type-Options", "X-Frame-Options",
              "Referrer-Policy", "Permissions-Policy"):
        assert h in r.headers, f"missing security header: {h}"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"

def test_security_headers_present_on_api(client):
    """Security headers apply to JSON responses too, not just HTML."""
    r = client.get("/api/devices",
                   headers={"Authorization": "Bearer test-api-token"})
    assert "Content-Security-Policy" in r.headers
    assert "X-Content-Type-Options" in r.headers

def test_docs_disabled_in_production(client):
    """/docs, /redoc, and /openapi.json should 404 unless DEBUG=1 is set
    (test env doesn't set DEBUG, so they should be off)."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} should be disabled in prod"

def test_discovery_rejects_malformed_json(client):
    r = client.post("/ingest/discovery",
                    headers={"Authorization": "Bearer test-ingest-token",
                             "Content-Type": "application/json"},
                    content='{bad')
    assert r.status_code == 400


def test_aw_configured_rejects_placeholder_values(monkeypatch, temp_env):
    """`aw_configured` should be False for the literal placeholder string
    from .env.example. Otherwise a fresh deploy with the unedited template
    would start the AWN poller against bogus creds."""
    monkeypatch.setenv("AW_APPLICATION_KEY", "replace-with-application-key")
    monkeypatch.setenv("AW_API_KEY", "replace-with-api-key")
    # Re-import config so it picks up the env we just set
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    assert cfg_mod.settings.aw_configured is False

def test_captures_tolerates_malformed_jsonl(client, temp_env):
    """A truncated trailing line in the capture log should be skipped,
    not crash the read endpoint."""
    # Post a real capture so the file exists
    client.post("/ingest/capture/malformed-test",
                headers={"Authorization": "Bearer test-capture-token"},
                data="real-capture")
    # Now append a malformed line directly to the JSONL
    from app.capture import _log_path
    p = _log_path("malformed-test")
    with p.open("a") as f:
        f.write("{this is not valid json\n")
        f.write('{"valid": "yes"}\n')
    r = client.get("/api/captures/malformed-test",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    body = r.json()
    # Got 2 valid rows (the original capture + the synthetic valid one);
    # 1 malformed row was reported as skipped.
    assert body["count"] == 2
    assert body.get("skipped_malformed") == 1


# ─────────────────── discoveries (long-tail RF survey) ───────────────────

def test_discovery_upsert_dedupes_by_model_id(client):
    """Three sightings of the same (model, id) should produce ONE row with
    seen_count=3 — not three rows."""
    pkt = {"model": "TPMS-Toyota", "id": 12345, "pressure_kPa": 220}
    for _ in range(3):
        r = client.post("/ingest/discovery",
                        headers={"Authorization": "Bearer test-ingest-token"},
                        json=pkt)
        assert r.status_code == 200, r.text
    listing = client.get("/api/discoveries",
                         headers={"Authorization": "Bearer test-api-token"}).json()
    matches = [d for d in listing["rows"] if d["model"] == "TPMS-Toyota"]
    assert len(matches) == 1
    assert matches[0]["seen_count"] == 3
    assert matches[0]["id"] == "12345"
    assert matches[0]["sample"]["pressure_kPa"] == 220

def test_discovery_different_ids_separate_rows(client):
    for i in range(5):
        client.post("/ingest/discovery",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"model": "Acurite-606TX", "id": 1000 + i, "temp_C": 21})
    listing = client.get("/api/discoveries",
                         headers={"Authorization": "Bearer test-api-token"}).json()
    matches = [d for d in listing["rows"] if d["model"] == "Acurite-606TX"]
    assert len(matches) == 5

def test_discovery_requires_ingest_token(client):
    r = client.post("/ingest/discovery", json={"model": "X", "id": 1})
    assert r.status_code == 401

def test_discovery_list_requires_api_token(client):
    assert client.get("/api/discoveries").status_code == 401

def test_discovery_rejects_missing_model(client):
    r = client.post("/ingest/discovery",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"id": 1})
    assert r.status_code == 400

def test_discovery_since_hours_filter(client):
    """since_hours=0 returns everything; positive value filters by recency."""
    client.post("/ingest/discovery",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"model": "Garage-Remote", "id": 99})
    everything = client.get("/api/discoveries?since_hours=0",
                            headers={"Authorization": "Bearer test-api-token"}).json()
    last_hour = client.get("/api/discoveries?since_hours=1",
                            headers={"Authorization": "Bearer test-api-token"}).json()
    # Just posted ⇒ both should include it
    for d in (everything, last_hour):
        assert any(r["model"] == "Garage-Remote" for r in d["rows"])


# ─────────────── rain rollups (SDR-style cumulative-only data) ───────────────
# When a source posts only yearlyrainin (no pre-computed daily/hourly buckets,
# which is the SDR path), /api/devices/{mac}/current enriches the response by
# computing those buckets from historical yearlyrainin deltas at local-time
# period boundaries.

def _post_yearly_only(client, ts_iso, yearly_in):
    """POST an SDR-style observation that only carries yearly rain."""
    payload = {
        "device": {"id": "5D5D02000007D"[:12].ljust(12, "0"),
                   "name": "SDR Test Sensor"},
        "timestamp_utc": ts_iso,
        "outdoor": {"tempf": 72.0, "humidity": 30},
        "rain": {"yearly_in": yearly_in},
        "source": "fineoffset-wh24-sdr",
    }
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=payload)
    assert r.status_code == 200, r.text
    return r.json()["mac"]

def test_rain_rollups_compute_from_yearly_deltas(client):
    """Two observations: 0.50 at "midnight", 0.85 now. Daily should = 0.35."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    # An observation tagged 14 hours ago (safely before any local midnight)
    # 36 hours ago guarantees we're past today's midnight UTC regardless
    # of what wall-clock hour the test runs at.
    earlier = (now - timedelta(hours=36)).isoformat().replace("+00:00", "Z")
    _post_yearly_only(client, earlier, 0.50)
    # Now
    mac = _post_yearly_only(client, now.isoformat().replace("+00:00", "Z"), 0.85)
    r = client.get(f"/api/devices/{mac}/current",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    obs = r.json()
    # Daily should equal current - earlier (since earlier was before today's
    # midnight in any reasonable TZ — we ran with default UTC).
    assert obs["dailyrainin"] is not None
    # Allow tiny float wobble
    assert abs(obs["dailyrainin"] - 0.35) < 0.01, f"daily was {obs['dailyrainin']}"
    # Weekly may or may not be populated depending on when in the week
    # the test runs (need an observation BEFORE this week's Sunday midnight).
    # Same for monthly (need data before the 1st). Just assert: if present,
    # they must be ≥ daily — never less.
    for k in ("weeklyrainin", "monthlyrainin"):
        if obs[k] is not None:
            assert obs[k] >= obs["dailyrainin"], f"{k}={obs[k]} < daily={obs['dailyrainin']}"

def test_rain_rollups_handles_no_prior_data(client):
    """First-ever yearlyrainin observation — no historical data to diff
    against. Daily etc. should be 0 (we just got our first reading at
    this exact value, so since "midnight" nothing's changed)."""
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    mac = _post_yearly_only(client, now.isoformat().replace("+00:00", "Z"), 0.85)
    r = client.get(f"/api/devices/{mac}/current",
                   headers={"Authorization": "Bearer test-api-token"})
    obs = r.json()
    # No row exists before "today's midnight" — yearly_rain_at_or_before
    # returns None and we leave the rollup as None (don't lie about 0).
    # The query is "≤ boundary_ms" so the current observation itself counts
    # only if its timestamp is ≤ the boundary. now > boundary so it doesn't
    # → None.
    # Actually: if "now" is < 1 hour into the day, hourly might find a row
    # (the now-observation), making hourly = 0. So we only assert daily.
    assert obs["dailyrainin"] is None or obs["dailyrainin"] == 0.0

def test_rain_rollups_skipped_when_buckets_already_present(client):
    """AWN-style payload that already has dailyrainin etc. should not be
    overwritten by computed rollups."""
    payload = {
        "device": {"id": "AABBCCDDEEFF", "name": "AWN-like"},
        "timestamp_utc": "2026-05-17T15:00:00Z",
        "outdoor": {"tempf": 80, "humidity": 25},
        "rain": {
            "yearly_in": 0.85,
            "daily_in": 0.99,    # operator-set / pre-computed
            "hourly_in": 0.05,
        },
        "source": "ambient-weather",
    }
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=payload)
    assert r.status_code == 200
    mac = r.json()["mac"]
    cur = client.get(f"/api/devices/{mac}/current",
                     headers={"Authorization": "Bearer test-api-token"}).json()
    # Operator-provided values must NOT be overwritten by computed values
    assert cur["dailyrainin"] == 0.99
    assert cur["hourlyrainin"] == 0.05

def test_history_short_window_returns_raw(client):
    """Window ≤ 6h returns raw observations (no bucketing)."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    for i in range(5):
        ts = (now - timedelta(minutes=10*i)).isoformat().replace("+00:00", "Z")
        payload = {
            "device": {"id": "AABBCCDDEEFF"},
            "timestamp_utc": ts,
            "outdoor": {"tempf": 70 + i, "humidity": 50},
            "source": "test",
        }
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=payload)
    r = client.get("/api/devices/AA:BB:CC:DD:EE:FF/history?hours=2",
                   headers={"Authorization": "Bearer test-api-token"})
    body = r.json()
    # 2h window ≤ 6h ⇒ raw, returns all 5 observations with full data_json
    assert body["count"] == 5
    assert body["rows"][0]["tempf"] in (70, 71, 72, 73, 74)


def test_history_long_window_buckets_and_avoids_truncation(client):
    """A 7-day window with 1000 dense observations would normally be
    truncated by LIMIT=2000 to the first ~500 observations only. With
    auto-bucketing, the response covers the full window."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    # Post 200 observations evenly spread across 7 days
    for i in range(200):
        ts = (now - timedelta(days=7) + timedelta(hours=i * 7 * 24 / 200)).isoformat().replace("+00:00", "Z")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEE99"},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": 70.0 + (i % 30), "humidity": 50},
                          "source": "test"})
    r = client.get("/api/devices/AA:BB:CC:DD:EE:99/history?hours=168",
                   headers={"Authorization": "Bearer test-api-token"})
    body = r.json()
    # Bucketed (15-min buckets for 168h window) — should return a
    # bounded number, not all 200, and cover the full window.
    assert body["count"] > 0
    assert body["count"] <= 700  # 168h / 0.25h = 672 max buckets
    first_ts = body["rows"][0]["dateutc"]
    last_ts = body["rows"][-1]["dateutc"]
    span_h = (last_ts - first_ts) / 3_600_000
    # We posted across 7 days; bucketed response should span most of that
    assert span_h > 100, f"bucketed response only spans {span_h:.1f}h"


def test_rain_rollups_clamps_negative_to_zero(client):
    """If counter went backwards (calibration change / sensor reset bypassing
    SDR-relay offset logic), current_yearly could be less than at midnight.
    We clamp to 0 instead of returning a negative."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    # 36 hours ago guarantees we're past today's midnight UTC regardless
    # of what wall-clock hour the test runs at.
    earlier = (now - timedelta(hours=36)).isoformat().replace("+00:00", "Z")
    _post_yearly_only(client, earlier, 5.00)
    mac = _post_yearly_only(client, now.isoformat().replace("+00:00", "Z"), 0.10)
    obs = client.get(f"/api/devices/{mac}/current",
                     headers={"Authorization": "Bearer test-api-token"}).json()
    assert obs["dailyrainin"] == 0.0  # clamped, not -4.90


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

def test_capture_redacts_token_from_logs(client):
    """The capture token (both Authorization header and ?t= query param)
    must NOT end up in the JSONL log readable via /api/captures."""
    # Post via Authorization header
    client.post("/ingest/capture/redact-test",
                headers={"Authorization": "Bearer test-capture-token",
                         "X-Capture-Token": "should-also-be-redacted",
                         "Cookie": "session=secret-value"},
                data="hello-header")
    # Post via ?t= query
    client.post("/ingest/capture/redact-test?t=test-capture-token&token=also-secret",
                data="hello-query")
    r = client.get("/api/captures/redact-test",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    body = r.text  # raw string scan so we catch ANY occurrence
    # The token MUST NOT be present anywhere in the captured records
    assert "test-capture-token" not in body, "capture token leaked in /api/captures output"
    assert "secret-value" not in body, "cookie value leaked"
    assert "also-secret" not in body, "token query param leaked"
    # The literal "<redacted>" marker should be there
    assert "<redacted>" in body

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


# ───────────────────────── alert preferences API ─────────────────────────
# SMTP_HOST is unset in the test env, so effective `enabled` is always False
# (nothing to send through); these verify the prefs are stored + reflected.

_H = {"Authorization": "Bearer test-api-token"}

def _ingest_device(client, mac_compact="AABBCCDDEEFF"):
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": mac_compact, "model": "Atlas"},
                      "timestamp_utc": "2026-05-25T06:00:00Z",
                      "outdoor": {"tempf": 70}, "wind": {}, "rain": {}, "pressure": {},
                      "source": "acurite-atlas"})

def test_alerts_requires_bearer(client):
    assert client.get("/api/alerts").status_code == 401
    assert client.put("/api/alerts", json={}).status_code == 401

def test_alerts_default_shape(client):
    r = client.get("/api/alerts", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["transport_configured"] is False     # no SMTP_HOST in tests
    assert body["enabled"] is False
    assert body["default_threshold_minutes"] == 15.0  # env default
    assert body["devices"] == []

def test_alerts_put_updates_globals(client):
    r = client.put("/api/alerts", headers=_H,
                   json={"default_threshold_minutes": 8, "repeat_hours": 6,
                         "recipients": ["a@example.com", "b@example.com"]})
    assert r.status_code == 200
    body = r.json()
    assert body["default_threshold_minutes"] == 8.0
    assert body["repeat_hours"] == 6.0
    assert body["recipients"] == ["a@example.com", "b@example.com"]
    assert body["recipients_source"] == "app"
    # persisted across a fresh GET
    assert client.get("/api/alerts", headers=_H).json()["default_threshold_minutes"] == 8.0

def test_alerts_put_rejects_bad_recipient(client):
    r = client.put("/api/alerts", headers=_H, json={"recipients": ["not-an-email"]})
    assert r.status_code == 400

def test_alerts_put_validates_threshold_range(client):
    assert client.put("/api/alerts", headers=_H,
                      json={"default_threshold_minutes": 0}).status_code == 422

def test_device_alert_pref_roundtrip(client):
    _ingest_device(client)
    mac = "AA:BB:CC:DD:EE:FF"
    # turn monitoring on with a tight 10-min threshold
    r = client.put(f"/api/devices/{mac}/alert", headers=_H,
                   json={"monitor": True, "threshold_minutes": 10})
    assert r.status_code == 200
    dev = next(d for d in r.json()["devices"] if d["mac"] == mac)
    assert dev["monitor"] is True and dev["threshold_minutes"] == 10.0
    assert dev["threshold_override"] == 10.0
    # turn it off → not monitored, effective threshold None
    r = client.put(f"/api/devices/{mac}/alert", headers=_H, json={"monitor": False})
    dev = next(d for d in r.json()["devices"] if d["mac"] == mac)
    assert dev["monitor"] is False and dev["threshold_minutes"] is None

def test_device_alert_accepts_compact_mac(client):
    _ingest_device(client)
    # compact form in the path normalizes to the stored colonized MAC
    r = client.put("/api/devices/aabbccddeeff/alert", headers=_H,
                   json={"monitor": True, "threshold_minutes": 12})
    dev = next(d for d in r.json()["devices"] if d["mac"] == "AA:BB:CC:DD:EE:FF")
    assert dev["threshold_minutes"] == 12.0

def test_alerts_test_send_requires_transport(client):
    # no SMTP_HOST configured → 400, not a 500
    assert client.post("/api/alerts/test", headers=_H).status_code == 400


def test_alerts_smtp_write_only(client):
    # App sets SMTP transport; GET echoes everything EXCEPT the password.
    r = client.put("/api/alerts", headers=_H, json={
        "smtp_host": "smtp.gmail.com", "smtp_port": 587,
        "smtp_username": "me@gmail.com", "smtp_password": "secret-app-pw",
        "smtp_from": "me@gmail.com", "smtp_tls": True})
    assert r.status_code == 200
    b = r.json()
    assert b["transport_configured"] is True
    assert b["smtp_host"] == "smtp.gmail.com" and b["smtp_username"] == "me@gmail.com"
    assert b["smtp_port"] == 587 and b["smtp_password_set"] is True
    assert "smtp_password" not in b              # never returned
    assert b["smtp_source"] == "app"
    # Password persists when other fields are edited without re-sending it.
    r2 = client.put("/api/alerts", headers=_H, json={"smtp_port": 465, "smtp_ssl": True})
    b2 = r2.json()
    assert b2["smtp_password_set"] is True and b2["smtp_port"] == 465


def test_rain_glitch_rejected(client):
    ih = {"Authorization": "Bearer test-ingest-token"}
    ah = {"Authorization": "Bearer test-api-token"}
    def post(ts, yearly):
        return client.post("/ingest/custom", headers=ih, json={
            "device": {"id": "5D5D0200007D"}, "timestamp_utc": ts,
            "outdoor": {"tempf": 70, "humidity": 50}, "wind": {}, "pressure": {},
            "rain": {"yearly_in": yearly}, "source": "fineoffset-wh24"})
    assert post("2026-05-25T10:00:00Z", 3.58).status_code == 200
    # +6 inches in one minute is physically impossible → dropped as a glitch
    assert post("2026-05-25T10:01:00Z", 9.58).status_code == 200
    # a small, plausible increase a minute later is kept
    assert post("2026-05-25T10:02:00Z", 3.60).status_code == 200
    hist = client.get("/api/devices/5D:5D:02:00:00:7D/history?hours=720",
                      headers=ah).json()["rows"]
    ys = [r["yearlyrainin"] for r in hist if r.get("yearlyrainin") is not None]
    # /history auto-buckets a wide window, so don't assert exact values — just
    # that the 9.58 glitch never made it in (a stored glitch would pull any
    # bucket average far above the real ~3.6).
    assert ys and max(ys) < 5.0


def test_alert_monitor_always_started(client):
    # [P2] The monitor must start even with no env SMTP, so SMTP configured
    # later from the app (PUT /api/alerts) is picked up without a redeploy.
    from app.main import app
    assert app.state.alert_monitor is not None


def test_push_register_roundtrip(client):
    H = {"Authorization": "Bearer test-api-token"}
    assert client.post("/api/push/register", headers=H,
                       json={"token": "abcd1234efgh", "env": "sandbox"}).json()["ok"] is True
    # upsert (same token again) is fine
    assert client.post("/api/push/register", headers=H,
                       json={"token": "abcd1234efgh"}).status_code == 200
    # too-short token rejected
    assert client.post("/api/push/register", headers=H, json={"token": "x"}).status_code == 422

def test_push_register_requires_token(client):
    assert client.post("/api/push/register", json={"token": "abcd1234efgh"}).status_code == 401

def test_alert_rules_crud(client):
    H = {"Authorization": "Bearer test-api-token"}
    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "tempf", "comparator": "above", "threshold": 100})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert r.json()["field"] == "tempf" and r.json()["threshold"] == 100
    assert any(x["id"] == rid for x in client.get("/api/alerts/rules", headers=H).json())
    assert client.delete(f"/api/alerts/rules/{rid}", headers=H).status_code == 200
    assert all(x["id"] != rid for x in client.get("/api/alerts/rules", headers=H).json())

def test_alert_rule_validation(client):
    H = {"Authorization": "Bearer test-api-token"}
    assert client.post("/api/alerts/rules", headers=H,
                       json={"field": "bogus", "comparator": "above", "threshold": 1}).status_code == 400
    assert client.post("/api/alerts/rules", headers=H,
                       json={"field": "tempf", "comparator": "sideways", "threshold": 1}).status_code == 400

def test_alert_rules_requires_token(client):
    assert client.get("/api/alerts/rules").status_code == 401


# ───────────────── push relay client mode (APNS_RELAY_* / app-managed) ─────────────────

def test_apns_send_to_all_routes_via_env_relay(client, monkeypatch):
    import asyncio
    import app.apns as apns
    client.post("/api/push/register", headers={"Authorization": "Bearer test-api-token"},
                json={"token": "a" * 64, "env": "production"})
    monkeypatch.setattr(apns.settings, "apns_relay_url", "https://relay.example/api/relay/push")
    monkeypatch.setattr(apns.settings, "apns_relay_token", "rtok")
    seen = {}
    async def fake_relay(tokens, title, body, url, token):
        seen.update(tokens=list(tokens), url=url, token=token)
        return {"sent": len(tokens), "dead": [], "failed": 0}
    monkeypatch.setattr(apns, "_push_via_relay", fake_relay)
    res = asyncio.run(apns.send_to_all("Title", "Body"))
    assert seen["tokens"] == ["a" * 64]
    assert seen["url"] == "https://relay.example/api/relay/push" and seen["token"] == "rtok"
    assert res["sent"] == 1 and res["total"] == 1

def test_push_relay_config_roundtrip(client):
    H = {"Authorization": "Bearer test-api-token"}
    assert client.get("/api/push/relay", headers=H).json()["relay_configured"] is False
    r = client.put("/api/push/relay", headers=H, json={
        "relay_url": "https://weather.zasder.com/api/relay/push", "relay_token": "secret"})
    assert r.status_code == 200 and r.json()["relay_configured"] is True
    g = client.get("/api/push/relay", headers=H).json()
    assert g["relay_url"].endswith("/api/relay/push")
    assert g["relay_token_set"] is True and g["relay_configured"] is True
    assert "relay_token" not in g                    # token is write-only
    client.put("/api/push/relay", headers=H, json={"relay_token": ""})
    assert client.get("/api/push/relay", headers=H).json()["relay_configured"] is False

def test_send_to_all_uses_db_relay(client, monkeypatch):
    import asyncio
    import app.apns as apns
    H = {"Authorization": "Bearer test-api-token"}
    client.post("/api/push/register", headers=H, json={"token": "c" * 64, "env": "production"})
    client.put("/api/push/relay", headers=H, json={
        "relay_url": "https://weather.zasder.com/api/relay/push", "relay_token": "dbtok"})
    seen = {}
    async def fake_relay(tokens, title, body, url, token):
        seen.update(url=url, token=token)
        return {"sent": len(tokens), "dead": [], "failed": 0}
    monkeypatch.setattr(apns, "_push_via_relay", fake_relay)
    res = asyncio.run(apns.send_to_all("T", "B"))
    assert seen["token"] == "dbtok" and seen["url"].endswith("/api/relay/push")
    assert res["sent"] == 1


def test_alert_rule_toggle_enabled(client):
    H = {"Authorization": "Bearer test-api-token"}
    rid = client.post("/api/alerts/rules", headers=H,
                      json={"field": "tempf", "comparator": "above", "threshold": 100}).json()["id"]
    # disable
    r = client.patch(f"/api/alerts/rules/{rid}", headers=H, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.get("/api/alerts/rules", headers=H).json()[0]["enabled"] is False
    # re-enable
    assert client.patch(f"/api/alerts/rules/{rid}", headers=H,
                        json={"enabled": True}).json()["enabled"] is True
    # unknown rule → 404
    assert client.patch("/api/alerts/rules/99999", headers=H, json={"enabled": True}).status_code == 404
