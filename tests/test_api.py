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


# ───────────────── indoor block + meter (SDR pipeline) ─────────────────

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

def test_meter_ingest_and_recent_roundtrip(client):
    """POST a meter reading then read it back from /api/meters/{id}/recent."""
    reading = {"model": "Neptune-R900", "id": 1583287502,
               "consumption": 257328, "leak": 5}
    r = client.post("/ingest/meter",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json=reading)
    assert r.status_code == 200, r.text
    # List should now include this meter
    listing = client.get("/api/meters",
                         headers={"Authorization": "Bearer test-api-token"})
    assert listing.status_code == 200
    assert any(m["id"] == "1583287502" for m in listing.json()["meters"])
    # Recent should return the reading
    recent = client.get("/api/meters/1583287502/recent",
                        headers={"Authorization": "Bearer test-api-token"})
    assert recent.status_code == 200
    rows = recent.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["consumption"] == 257328

def test_meter_requires_ingest_token(client):
    r = client.post("/ingest/meter", json={"id": 1, "consumption": 1})
    assert r.status_code == 401

def test_meter_recent_requires_api_token(client):
    r = client.get("/api/meters/123/recent")
    assert r.status_code == 401


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

def test_meter_rejects_malformed_json(client):
    r = client.post("/ingest/meter",
                    headers={"Authorization": "Bearer test-ingest-token",
                             "Content-Type": "application/json"},
                    content='not json')
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
