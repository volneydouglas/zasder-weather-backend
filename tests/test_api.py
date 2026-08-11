"""End-to-end endpoint tests via FastAPI's TestClient.

Each test gets a fresh app + DB via the `client` fixture in conftest.py.
Covers the security boundaries the reviewer flagged: token auth on read +
write, capture endpoint gate, status-page HTML escaping."""
from __future__ import annotations

import json


# ───────────────────────── liveness + read auth ─────────────────────────

def test_healthz_open(client):
    from app.version import __version__
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}

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


def test_captures_reject_reviewer_token(client, temp_env):
    """The read-only reviewer/demo token must NOT be able to read raw
    captured request bodies + headers (they can carry other sources'
    secrets). Only the primary api_token may."""
    client.post("/ingest/capture/reviewer-test",
                headers={"Authorization": "Bearer test-capture-token"},
                data="secret-body")
    # reviewer token → rejected
    r = client.get("/api/captures/reviewer-test",
                   headers={"Authorization": "Bearer test-reviewer-token"})
    assert r.status_code == 403
    # primary api token → allowed
    r = client.get("/api/captures/reviewer-test",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200


def test_oversize_content_length_rejected(client):
    """A Content-Length above the global cap is rejected with 413 before the
    body is read (unauthenticated memory-exhaustion guard)."""
    r = client.post(
        "/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json",
                 "Content-Length": str(5 * 1024 * 1024)},
        content=b'{"device":{"id":"AABBCCDDEEFF"}}',
    )
    assert r.status_code == 413


def test_oversize_streamed_body_bounded(client):
    """A large body with no oversized Content-Length still can't sail
    through — the route rejects it (bounded-memory guard did its job)."""
    # A generator body makes httpx use Transfer-Encoding: chunked and omit
    # Content-Length. That matters: passing `content=<bytes>` sets
    # Content-Length, which trips the middleware's fast path and returns 413
    # WITHOUT ever running the streaming counter this test is named for. The
    # mid-stream truncation branch had no coverage at all.
    def _chunks():
        yield b'{"junk":"'
        for _ in range(32):
            yield b'x' * (64 * 1024)
        yield b'"}'

    r = client.post(
        "/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        content=_chunks(),
    )
    # 400 specifically, not `in (400, 413)`: 413 is the Content-Length fast
    # path, so accepting it would let this test pass while the streaming
    # branch stayed unexercised — which is exactly what it did before.
    # Verified: chunked -> 400, fixed Content-Length -> 413.
    assert r.status_code == 400, (
        f"expected the streaming counter to truncate the body (400), got "
        f"{r.status_code} — a 413 means the Content-Length fast path ran "
        f"instead and this test is not covering what it claims")


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
    # Device name still renders (escaped) — raw payload must NOT appear.
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    # Location label is no longer published on the public status page (it can
    # name a home) — it must not appear in ANY form, raw or escaped.
    assert "<img src=x onerror=" not in page
    assert "&lt;img src=x onerror=alert(2)&gt;" not in page
    assert "onerror" not in page


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
    from datetime import datetime, timedelta, timezone
    ih = {"Authorization": "Bearer test-ingest-token"}
    ah = {"Authorization": "Bearer test-api-token"}
    # Timestamps relative to now so the data always lands inside the history
    # window queried below — hardcoded calendar dates silently fall out of the
    # 720h window as real time passes.
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    def iso(mins): return (base + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
    def post(ts, yearly):
        return client.post("/ingest/custom", headers=ih, json={
            "device": {"id": "5D5D0200007D"}, "timestamp_utc": ts,
            "outdoor": {"tempf": 70, "humidity": 50}, "wind": {}, "pressure": {},
            "rain": {"yearly_in": yearly}, "source": "fineoffset-wh24"})
    assert post(iso(0), 3.58).status_code == 200
    # +6 inches in one minute is physically impossible → dropped as a glitch
    assert post(iso(1), 9.58).status_code == 200
    # a small, plausible increase a minute later is kept
    assert post(iso(2), 3.60).status_code == 200
    hist = client.get("/api/devices/5D:5D:02:00:00:7D/history?hours=720",
                      headers=ah).json()["rows"]
    ys = [r["yearlyrainin"] for r in hist if r.get("yearlyrainin") is not None]
    # /history auto-buckets a wide window, so don't assert exact values — just
    # that the 9.58 glitch never made it in (a stored glitch would pull any
    # bucket average far above the real ~3.6).
    assert ys and max(ys) < 5.0


def test_history_write_throttle(client, monkeypatch):
    from app import config
    ih = {"Authorization": "Bearer test-ingest-token"}

    def post(ts, temp, wind=None):
        return client.post("/ingest/custom", headers=ih, json={
            "device": {"id": "5D5D0200007D"}, "timestamp_utc": ts,
            "outdoor": {"tempf": temp, "humidity": 50},
            "wind": wind or {}, "pressure": {}, "rain": {},
            "source": "fineoffset-wh24"}).json()

    # Throttle off (default): every reading is stored.
    monkeypatch.setattr(config.settings, "ingest_min_interval_seconds", 0)
    assert post("2026-05-25T10:00:00Z", 70)["inserted"] == 1
    assert post("2026-05-25T10:00:10Z", 70)["inserted"] == 1

    # Throttle at 60s: a reading within 60s of the last STORED row (10:00:10),
    # changing only existing values, is coalesced away.
    monkeypatch.setattr(config.settings, "ingest_min_interval_seconds", 60)
    r = post("2026-05-25T10:00:40Z", 71)
    assert r["inserted"] == 0 and r["throttled"] is True

    # A reading past the interval is stored again (80s after 10:00:10).
    assert post("2026-05-25T10:01:30Z", 72)["inserted"] == 1

    # Inside the window but contributing a field the last stored row lacked
    # (windspeed) → kept, so multi-source composite posts aren't dropped.
    assert post("2026-05-25T10:01:50Z", 72, wind={"speed_mph": 5})["inserted"] == 1


def test_device_location_override(client):
    ih = {"Authorization": "Bearer test-ingest-token"}
    ah = {"Authorization": "Bearer test-api-token"}
    client.post("/ingest/custom", headers=ih, json={
        "device": {"id": "5D5D0200007D"}, "timestamp_utc": "2026-05-25T10:00:00Z",
        "outdoor": {"tempf": 70, "humidity": 50}, "wind": {}, "pressure": {},
        "rain": {}, "source": "fineoffset-wh24"})
    r = client.put("/api/devices/5D:5D:02:00:00:7D/location", headers=ah,
                   json={"lat": 33.3004, "lon": -111.9378, "label": "Home"})
    assert r.status_code == 200
    devs = client.get("/api/devices", headers=ah).json()
    d = next(x for x in devs if x["mac"] == "5D:5D:02:00:00:7D")
    coords = d["info"]["coords"]["coords"]
    assert abs(coords["lat"] - 33.3004) < 1e-6
    assert abs(coords["lon"] - (-111.9378)) < 1e-6
    # out-of-range is rejected
    assert client.put("/api/devices/5D:5D:02:00:00:7D/location", headers=ah,
                      json={"lat": 200, "lon": 0}).status_code == 400


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

def test_push_register_android_platform(client):
    """Android app registers an FCM token with platform=android; stored + listed."""
    import asyncio
    from app import db, fcm
    H = {"Authorization": "Bearer test-api-token"}
    assert client.post("/api/push/register", headers=H,
                       json={"token": "fcm-token-abcdef123456", "platform": "android"}).json()["ok"] is True
    # asyncio.run, like the other 16 sync tests here — get_event_loop() no
    # longer creates a loop on demand, so the old idiom raises "no current
    # event loop" on any current Python.
    toks = asyncio.run(db.list_push_tokens())
    row = next(t for t in toks if t["token"] == "fcm-token-abcdef123456")
    assert row["platform"] == "android"
    # FCM is unconfigured in tests → configured() is False (no-op path), no crash.
    assert fcm.fcm_configured() is False

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


# ───── reviewer P2: threshold alert state must not advance until delivery succeeds ─────

def test_threshold_alert_retries_when_delivery_fails(client, monkeypatch):
    """Repro of reviewer P2: with delivery failing, the rule's state must NOT
    advance to triggered=1 — otherwise the next tick won't retry."""
    import asyncio
    import app.alerts as alerts
    from app.alerts import AlertMonitor, effective_config
    from app import db
    H = {"Authorization": "Bearer test-api-token"}
    # crossing observation: tempf=105 > rule threshold 100
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": "AA:BB:CC:DD:EE:FF", "name": "Yard"},
              "timestamp_utc": "2026-06-01T12:00:00Z",
              "outdoor": {"tempf": 105}})
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above", "threshold": 100})
    calls = {"n": 0}
    async def fake_deliver(cfg, subj, body, ptitle, pbody):
        calls["n"] += 1
        return False                      # delivery always fails
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)

    async def two_ticks():
        cfg = await effective_config()
        mon = AlertMonitor()
        devs = await db.list_devices()
        await mon._check_threshold_rules(cfg, devs, 1_000)
        s1 = await db.get_rule_states()
        await mon._check_threshold_rules(cfg, devs, 2_000)
        s2 = await db.get_rule_states()
        return s1, s2
    s1, s2 = asyncio.run(two_ticks())
    assert calls["n"] == 2, "delivery must be retried while it keeps failing"
    assert all(v == 0 for v in s1.values()), "state must not advance on failed delivery"
    assert all(v == 0 for v in s2.values())

def test_threshold_alert_state_advances_on_successful_delivery(client, monkeypatch):
    """When delivery succeeds, state flips to 1 and a second tick does NOT
    re-fire (edge-triggered — that part is preserved)."""
    import asyncio
    import app.alerts as alerts
    from app.alerts import AlertMonitor, effective_config
    from app import db
    H = {"Authorization": "Bearer test-api-token"}
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": "AA:BB:CC:DD:EE:FF", "name": "Yard"},
              "timestamp_utc": "2026-06-01T12:00:00Z",
              "outdoor": {"tempf": 105}})
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above", "threshold": 100})
    calls = {"n": 0}
    async def fake_deliver(*a, **kw):
        calls["n"] += 1
        return True
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)

    async def two_ticks():
        cfg = await effective_config()
        mon = AlertMonitor()
        devs = await db.list_devices()
        await mon._check_threshold_rules(cfg, devs, 1_000)
        await mon._check_threshold_rules(cfg, devs, 2_000)
        return await db.get_rule_states()
    states = asyncio.run(two_ticks())
    assert calls["n"] == 1                # edge-trigger: only fires on the crossing
    assert any(v == 1 for v in states.values())


# ───── reviewer P3: relay URL must reject SSRF-able destinations ─────

def test_relay_url_rejects_unsafe_schemes_and_hosts(client):
    H = {"Authorization": "Bearer test-api-token"}
    bad = [
        "http://weather.zasder.com/api/relay/push",     # http (not https)
        "https://localhost/x",                          # loopback name
        "https://127.0.0.1/x",                          # loopback IP4
        "https://[::1]/x",                              # loopback IP6
        "https://10.0.0.1/x",                           # RFC1918
        "https://192.168.1.1/x",                        # RFC1918
        "https://169.254.169.254/x",                    # link-local (AWS metadata)
        "https:///nohostpath",                          # missing host
        "ftp://weather.zasder.com/x",                   # wrong scheme
    ]
    for u in bad:
        r = client.put("/api/push/relay", headers=H, json={"relay_url": u})
        assert r.status_code == 400, f"{u} should 400, got {r.status_code}: {r.text}"
    # public https hostname is OK
    assert client.put("/api/push/relay", headers=H, json={
        "relay_url": "https://weather.zasder.com/api/relay/push",
        "relay_token": "x"}).status_code == 200


def test_delete_device_removes_observations_and_state(client):
    H = {"Authorization": "Bearer test-api-token"}
    # seed a device + observation
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": "AA:BB:CC:DD:EE:FF", "name": "Test"},
              "timestamp_utc": "2026-06-02T12:00:00Z",
              "outdoor": {"tempf": 75}})
    devs = client.get("/api/devices", headers=H).json()
    assert any(d["mac"] == "AA:BB:CC:DD:EE:FF" for d in devs)
    # delete
    r = client.delete("/api/devices/AA:BB:CC:DD:EE:FF", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["devices"] == 1 and body["observations"] == 1
    # gone
    assert not any(d["mac"] == "AA:BB:CC:DD:EE:FF"
                   for d in client.get("/api/devices", headers=H).json())
    # MAC normalization works (compact form)
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": "112233445566"},
              "timestamp_utc": "2026-06-02T12:00:00Z",
              "outdoor": {"tempf": 70}})
    assert client.delete("/api/devices/112233445566", headers=H).status_code == 200
    # unknown → 404, no auth → 401
    assert client.delete("/api/devices/00:11:22:33:44:55", headers=H).status_code == 404
    assert client.delete("/api/devices/AA:BB:CC:DD:EE:FF").status_code == 401


# ───── reviewer token is READ-only (P1 from reviewer-private-2026-06-02) ─────

REVIEWER = {"Authorization": "Bearer test-reviewer-token"}
WRITER   = {"Authorization": "Bearer test-api-token"}

def test_reviewer_token_allowed_on_reads(client):
    # the core read surface a reviewer would walk
    for path in ("/api/devices", "/api/alerts", "/api/alerts/rules",
                 "/api/push/relay"):
        r = client.get(path, headers=REVIEWER)
        assert r.status_code == 200, f"reviewer GET {path} → {r.status_code}: {r.text[:80]}"

def test_reviewer_token_rejected_on_writes(client):
    """The reviewer/demo token must not be able to mutate anything. Every
    POST/PUT/PATCH/DELETE under /api/* should refuse it."""
    rule_body = {"field": "tempf", "comparator": "above", "threshold": 100}
    cases = [
        ("POST",   "/api/alerts/rules",                rule_body),
        ("PATCH",  "/api/alerts/rules/1",              {"enabled": False}),
        ("DELETE", "/api/alerts/rules/1",              None),
        ("PUT",    "/api/alerts",                      {"enabled": False}),
        ("POST",   "/api/alerts/test",                 {}),
        ("PUT",    "/api/devices/AA:BB:CC:DD:EE:FF/alert", {"monitor": False}),
        ("POST",   "/api/push/register",               {"token": "a" * 64}),
        ("PUT",    "/api/push/relay",                  {"relay_token": "x"}),
        ("DELETE", "/api/devices/AA:BB:CC:DD:EE:FF",   None),
    ]
    for method, path, body in cases:
        kwargs = {"headers": REVIEWER}
        if body is not None: kwargs["json"] = body
        r = client.request(method, path, **kwargs)
        assert r.status_code == 403, (
            f"reviewer {method} {path} should be 403, got {r.status_code} {r.text[:80]}")

def test_primary_token_still_can_write(client):
    """Sanity — the primary api_token writes are not collateral damage."""
    r = client.post("/api/alerts/rules", headers=WRITER,
                    json={"field": "tempf", "comparator": "above", "threshold": 100})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert client.patch(f"/api/alerts/rules/{rid}", headers=WRITER,
                        json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/alerts/rules/{rid}", headers=WRITER).status_code == 200


def test_healthz_reports_version(client):
    from app.version import __version__
    j = client.get("/healthz").json()
    assert j["status"] == "ok"
    assert j["version"] == __version__


def test_api_version_open_and_shaped(client):
    from app.version import __version__
    r = client.get("/api/version")  # open, no auth
    assert r.status_code == 200
    j = r.json()
    assert j["version"] == __version__
    assert "update_available" in j and "latest" in j and "enabled" in j


def test_public_dashboard_off_by_default(client):
    """Default: status page shows the app screenshots, not the live dashboard."""
    page = client.get("/").text
    assert 'class="hero-shots"' in page  # screenshots markup present
    assert "app-cta" not in page         # dashboard CTA absent


def test_public_dashboard_on_renders_charts(client, monkeypatch):
    """PUBLIC_DASHBOARD=1 → live dashboard + App Store link replace the shots."""
    import datetime as _dt
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    # Ingest a couple of recent readings (within the 24h chart window).
    now = _dt.datetime.now(_dt.timezone.utc)
    for mins, temp in ((120, 88.0), (60, 91.0)):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEEFF", "name": "Davis Vantage Pro 2"},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": temp, "humidity": 40},
                          "wind": {"speed_mph": 3}, "rain": {}, "pressure": {"relative_inhg": 29.9},
                          "source": "test"})
    page = client.get("/").text
    assert "app-cta" in page and "Get the iOS app" in page
    assert 'class="hero-shots"' not in page       # screenshots markup replaced
    assert "Davis Vantage Pro 2" in page          # station shown
    assert "Temperature" in page and "<svg" in page  # charts rendered
    assert 'http-equiv="refresh"' in page         # auto-refresh on


def test_public_dashboard_explicit_mac_selects_station(client, monkeypatch):
    """PUBLIC_DASHBOARD_MACS pins a specific station. The selector must match
    the device whether the operator wrote the MAC compact or colonized (this
    path once NameError'd on an undefined _format_mac)."""
    import datetime as _dt
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    # Two stations; pin the second one via a COMPACT (colon-less) MAC.
    now = _dt.datetime.now(_dt.timezone.utc)
    ts = (now - _dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for dev_id, name, temp in (("AABBCCDDEEFF", "Crestview SDR", 87.0),
                               ("5D5D05000001", "Davis Vantage Pro 2", 91.0)):
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": dev_id, "name": name},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": temp, "humidity": 40},
                          "wind": {"speed_mph": 3}, "rain": {}, "pressure": {"relative_inhg": 29.9},
                          "source": "test"})
    monkeypatch.setattr(settings, "public_dashboard_macs", "5d5d05000001")  # compact, lowercase
    page = client.get("/").text
    assert page[:20] != "Internal Server Erro"     # no 500 (the _format_mac regression)
    # Assert on the dashboard's current-conditions header (cc-name), not the
    # page — both stations still appear in the lower device-stats table.
    assert 'class="cc-name">Davis Vantage Pro 2' in page   # pinned station rendered
    assert 'class="cc-name">Crestview SDR' not in page     # other one filtered out


def test_records_endpoint(client):
    """Records returns per-metric all-time/today highs & lows with timestamps."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    for mins, temp, gust in ((90, 88.0, 12.0), (60, 91.5, 20.0), (30, 85.0, 8.0)):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEEFF", "name": "Davis"},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": temp, "humidity": 40},
                          "wind": {"speed_mph": 3, "gust_mph": gust},
                          "rain": {}, "pressure": {"relative_inhg": 29.9},
                          "source": "test"})
    # Auth required
    assert client.get("/api/devices/AA:BB:CC:DD:EE:FF/records").status_code == 401
    r = client.get("/api/devices/AA:BB:CC:DD:EE:FF/records",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 200
    body = r.json()
    alltime = body["periods"]["all"]["fields"]
    assert alltime["tempf"]["max"] == 91.5 and alltime["tempf"]["min"] == 85.0
    assert alltime["tempf"]["maxAt"] is not None      # timestamp of the record
    assert alltime["windgustmph"]["max"] == 20.0      # peak gust tracked
    assert body["periods"]["today"]["fields"]["tempf"]["count"] >= 1


def test_metrics_endpoint_off_by_default(client):
    assert client.get("/metrics").status_code == 404   # opt-in

def test_metrics_endpoint_on(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "prometheus_metrics", True)
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF", "name": "Davis"},
                      "timestamp_utc": "2026-07-15T06:00:00Z",
                      "outdoor": {"tempf": 88.0, "humidity": 40},
                      "wind": {}, "rain": {}, "pressure": {"relative_inhg": 29.9},
                      "source": "test"})
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "zasder_temperature_fahrenheit" in r.text and "88" in r.text


def test_records_drops_cumulative_wettest_day(client):
    """A non-resetting (cumulative) dailyrainin counter must NOT surface as a
    'wettest day' record; a real resetting daily counter keeps it."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def post(dev_id, mins, daily):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": dev_id, "name": dev_id},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": 80, "humidity": 30},
                          "rain": {"daily_in": daily}, "source": "test"})

    # Cumulative counter (never resets near 0): min 16 > floor → max dropped.
    for mins, d in ((120, 16.0), (60, 18.0), (30, 19.8)):
        post("AAAAAAAAAAAA", mins, d)
    # Real resetting daily counter: min ~0 → kept.
    for mins, d in ((120, 0.0), (60, 0.5), (30, 0.0)):
        post("BBBBBBBBBBBB", mins, d)

    hdr = {"Authorization": "Bearer test-api-token"}
    cum = client.get("/api/devices/AA:AA:AA:AA:AA:AA/records", headers=hdr).json()
    assert cum["periods"]["all"]["fields"]["dailyrainin"]["max"] is None   # dropped
    real = client.get("/api/devices/BB:BB:BB:BB:BB:BB/records", headers=hdr).json()
    assert real["periods"]["all"]["fields"]["dailyrainin"]["max"] == 0.5   # kept


def test_maintenance_cleans_cumulative_rain(client, temp_env):
    """clean_cumulative_rain nulls a non-resetting (cumulative) rain column and
    leaves a real resetting counter — and the whole thing dry-runs by default."""
    import datetime as _dt
    import sqlite3
    from app import maintenance
    now = _dt.datetime.now(_dt.timezone.utc)

    def post(dev_id, mins, daily):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": dev_id}, "timestamp_utc": ts,
                          "outdoor": {"tempf": 80}, "rain": {"daily_in": daily},
                          "source": "test"})

    for mins, d in ((90, 18.0), (60, 19.0), (30, 19.8)):   # cumulative
        post("AAAAAAAAAAAA", mins, d)
    for mins, d in ((90, 0.0), (60, 0.4), (30, 0.0)):      # resetting
        post("BBBBBBBBBBBB", mins, d)

    dry = maintenance.clean_cumulative_rain(apply=False, db_path=temp_env)
    assert "AA:AA:AA:AA:AA:AA:dailyrainin" in dry["findings"]
    assert not any(k.startswith("BB:BB") for k in dry["findings"])   # resetting not flagged
    assert dry["applied"] is False

    res = maintenance.clean_cumulative_rain(apply=True, db_path=temp_env)
    assert res["applied"] and res["cleaned"] == 3 and res["backup"]

    conn = sqlite3.connect(temp_env)
    a = conn.execute("SELECT COUNT(*) FROM observations WHERE mac='AA:AA:AA:AA:AA:AA' "
                     "AND dailyrainin IS NOT NULL").fetchone()[0]
    a_json = conn.execute("SELECT COUNT(*) FROM observations WHERE mac='AA:AA:AA:AA:AA:AA' "
                          "AND json_extract(data_json, '$.dailyrainin') IS NOT NULL").fetchone()[0]
    b = conn.execute("SELECT MAX(dailyrainin) FROM observations "
                     "WHERE mac='BB:BB:BB:BB:BB:BB'").fetchone()[0]
    conn.close()
    assert a == 0 and a_json == 0     # cumulative nulled in column AND data_json
    assert b == 0.4                   # resetting device untouched


def test_ingest_drops_glitch_gust(client):
    """A gust wildly above the concurrent sustained wind is nulled at ingest."""
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEEFF"},
                          "timestamp_utc": "2026-07-18T20:34:00Z",
                          "outdoor": {"tempf": 95},
                          "wind": {"speed_mph": 4.56, "gust_mph": 58.0},
                          "source": "test"})
    assert r.status_code == 200
    cur = client.get("/api/devices/AA:BB:CC:DD:EE:FF/current",
                     headers={"Authorization": "Bearer test-api-token"}).json()
    assert cur["windgustmph"] is None          # glitch dropped
    assert cur["windspeedmph"] == 4.56         # sustained wind kept

def test_ingest_keeps_real_gust(client):
    """A plausible gust (within the factor of sustained wind) is kept."""
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEE11"},
                      "timestamp_utc": "2026-07-18T21:00:00Z",
                      "outdoor": {"tempf": 95},
                      "wind": {"speed_mph": 12.0, "gust_mph": 28.0},
                      "source": "test"})
    cur = client.get("/api/devices/AA:BB:CC:DD:EE:11/current",
                     headers={"Authorization": "Bearer test-api-token"}).json()
    assert cur["windgustmph"] == 28.0


def test_maintenance_cleans_glitch_gusts(client, temp_env):
    """clean_glitch_gusts nulls a historical spurious gust, keeps real ones."""
    import sqlite3
    from app import maintenance
    # NB: the ingest guard would drop the glitch on the way in, so insert the
    # bad gust directly to simulate pre-guard historical data.
    conn = sqlite3.connect(temp_env)
    conn.execute("INSERT INTO observations (mac, dateutc_ms, data_json, windgustmph, windspeedmph) "
                 "VALUES ('AA:AA:AA:AA:AA:AA', 1000, '{\"windgustmph\":58.0,\"windspeedmph\":4.5}', 58.0, 4.5)")
    conn.execute("INSERT INTO observations (mac, dateutc_ms, data_json, windgustmph, windspeedmph) "
                 "VALUES ('AA:AA:AA:AA:AA:AA', 2000, '{\"windgustmph\":28.0,\"windspeedmph\":12.0}', 28.0, 12.0)")
    conn.commit(); conn.close()

    dry = maintenance.clean_glitch_gusts(apply=False, db_path=temp_env)
    assert dry["bad_rows"] == 1 and dry["applied"] is False
    res = maintenance.clean_glitch_gusts(apply=True, db_path=temp_env)
    assert res["applied"] and res["cleaned"] == 1 and res["backup"]

    conn = sqlite3.connect(temp_env)
    gusts = [r[0] for r in conn.execute(
        "SELECT windgustmph FROM observations WHERE mac='AA:AA:AA:AA:AA:AA' ORDER BY dateutc_ms")]
    conn.close()
    assert gusts == [None, 28.0]     # glitch nulled, real gust kept


def _insert_yearly(conn, mac, ts_ms, yearly):
    conn.execute(
        "INSERT INTO observations (mac, dateutc_ms, data_json, yearlyrainin) "
        "VALUES (?, ?, ?, ?)",
        (mac, ts_ms, json.dumps({"yearlyrainin": yearly}), yearly))


def test_repair_yearly_rain_offsets(client, temp_env):
    """Regression for the '16 inches of rain today' ghost: removing an ingest
    yearly-rain offset stepped the stored counter up by the offset, and every
    rollup window straddling the step reported the offset as rainfall. The
    repair rewrites pre-cutoff history to raw-counter values (value + offset),
    nulls values the offset had clamped to 0, and rollups stop seeing a step."""
    import asyncio
    import sqlite3
    from app import db, maintenance
    cutoff = 1786423000000
    conn = sqlite3.connect(temp_env)
    # Offset station (Crestview shape): clamped zero early, adjusted values,
    # then raw values after the offset removal at `cutoff`.
    _insert_yearly(conn, "CC:CC:CC:CC:CC:CC", cutoff - 300_000, 0.0)
    _insert_yearly(conn, "CC:CC:CC:CC:CC:CC", cutoff - 200_000, 1.0)
    _insert_yearly(conn, "CC:CC:CC:CC:CC:CC", cutoff - 100_000, 1.09)
    _insert_yearly(conn, "CC:CC:CC:CC:CC:CC", cutoff + 100_000, 17.12)
    # Fully-clamped station (Davis shape): every pre-cutoff value is 0 because
    # the offset exceeded the raw counter — the true values are unknowable.
    _insert_yearly(conn, "DD:DD:DD:DD:DD:DD", cutoff - 200_000, 0.0)
    _insert_yearly(conn, "DD:DD:DD:DD:DD:DD", cutoff - 100_000, 0.0)
    _insert_yearly(conn, "DD:DD:DD:DD:DD:DD", cutoff + 100_000, 0.34)
    conn.commit()

    spec = {"CC:CC:CC:CC:CC:CC": {"offset": 16.03, "cutoff_ms": cutoff},
            "DD:DD:DD:DD:DD:DD": {"offset": 6.57, "cutoff_ms": cutoff}}

    # The bug being repaired: rollups difference the current counter against
    # yearly_rain_at_or_before(window start). With a window starting just
    # before the step, that delta reports the offset itself as rainfall.
    # (Asserted via the boundary lookup, not rain_rollups, because rollup
    # windows come from the wall clock and would make this time-dependent.)
    prior = asyncio.run(
        db.yearly_rain_at_or_before("CC:CC:CC:CC:CC:CC", cutoff - 50_000))
    assert round(17.12 - prior, 2) == 16.03

    dry = maintenance.repair_yearly_rain_offsets(spec, apply=False,
                                                 db_path=temp_env)
    assert dry["applied"] is False
    assert dry["macs"]["CC:CC:CC:CC:CC:CC"] == {"add_offset_rows": 2,
                                                "null_rows": 1,
                                                "era0_null_rows": 0}
    vals = [r[0] for r in conn.execute(
        "SELECT yearlyrainin FROM observations WHERE mac='CC:CC:CC:CC:CC:CC' "
        "ORDER BY dateutc_ms")]
    assert vals == [0.0, 1.0, 1.09, 17.12]        # dry run changed nothing

    res = maintenance.repair_yearly_rain_offsets(spec, apply=True,
                                                 db_path=temp_env)
    assert res["applied"] and res["backup"]

    vals = [tuple(r) for r in conn.execute(
        "SELECT yearlyrainin, json_extract(data_json, '$.yearlyrainin') "
        "FROM observations WHERE mac='CC:CC:CC:CC:CC:CC' ORDER BY dateutc_ms")]
    # Clamped zero nulled (column AND data_json), adjusted values raised to
    # raw, post-cutoff raw untouched.
    assert vals == [(None, None), (17.03, 17.03), (17.12, 17.12),
                    (17.12, 17.12)]
    vals = [r[0] for r in conn.execute(
        "SELECT yearlyrainin FROM observations WHERE mac='DD:DD:DD:DD:DD:DD' "
        "ORDER BY dateutc_ms")]
    assert vals == [None, None, 0.34]

    # The ghost is gone: the same boundary lookup now returns raw-equivalent
    # history, so the window delta is real rainfall (none), not the offset.
    prior = asyncio.run(
        db.yearly_rain_at_or_before("CC:CC:CC:CC:CC:CC", cutoff - 50_000))
    assert round(17.12 - prior, 2) == 0.0

    # Idempotency: a second run must refuse (repaired max would exceed the
    # first raw value) and leave everything untouched.
    res2 = maintenance.repair_yearly_rain_offsets(spec, apply=True,
                                                  db_path=temp_env)
    assert "monotonicity" in res2["macs"]["CC:CC:CC:CC:CC:CC"]["skipped"]
    vals = [r[0] for r in conn.execute(
        "SELECT yearlyrainin FROM observations WHERE mac='CC:CC:CC:CC:CC:CC' "
        "ORDER BY dateutc_ms")]
    assert vals == [None, 17.03, 17.12, 17.12]
    conn.close()


def test_repair_yearly_rain_offsets_refuses_bad_spec(client, temp_env):
    """A wrong offset/cutoff pair must not corrupt history: the monotonic
    check skips the MAC, and a MAC with no post-cutoff raw rows is skipped
    because there is nothing to reconcile the repair against."""
    import sqlite3
    from app import maintenance
    cutoff = 1786423000000
    conn = sqlite3.connect(temp_env)
    # Cutoff set too late: a raw row (17.12) is on the "pre" side, so adding
    # the offset again would push history above the raw counter.
    _insert_yearly(conn, "EE:EE:EE:EE:EE:EE", cutoff - 100_000, 17.12)
    _insert_yearly(conn, "EE:EE:EE:EE:EE:EE", cutoff + 100_000, 17.12)
    # No rows after the cutoff at all.
    _insert_yearly(conn, "FF:FF:FF:FF:FF:FF", cutoff - 100_000, 1.09)
    conn.commit()

    res = maintenance.repair_yearly_rain_offsets(
        {"EE:EE:EE:EE:EE:EE": {"offset": 16.03, "cutoff_ms": cutoff},
         "FF:FF:FF:FF:FF:FF": {"offset": 16.03, "cutoff_ms": cutoff}},
        apply=True, db_path=temp_env)
    assert res["applied"] is False                 # nothing passed the checks
    assert "monotonicity" in res["macs"]["EE:EE:EE:EE:EE:EE"]["skipped"]
    assert "no post-cutoff" in res["macs"]["FF:FF:FF:FF:FF:FF"]["skipped"]

    # A sign typo (or zero, or NaN) in the operator-typed spec would LOWER
    # history, and the monotonicity check can't see it — sunk values stay
    # below post_min. Must be refused up front.
    for bad in (-16.03, 0, float("nan")):
        res = maintenance.repair_yearly_rain_offsets(
            {"EE:EE:EE:EE:EE:EE": {"offset": bad, "cutoff_ms": cutoff}},
            apply=True, db_path=temp_env)
        assert res["applied"] is False, bad
        assert "invalid offset" in res["macs"]["EE:EE:EE:EE:EE:EE"]["skipped"], bad
    res = maintenance.repair_yearly_rain_offsets(
        {"EE:EE:EE:EE:EE:EE": {"offset": 16.03, "cutoff_ms": 0}},
        apply=True, db_path=temp_env)
    assert "invalid cutoff_ms" in res["macs"]["EE:EE:EE:EE:EE:EE"]["skipped"]
    res = maintenance.repair_yearly_rain_offsets(
        {"EE:EE:EE:EE:EE:EE": {"offset": 16.03, "cutoff_ms": cutoff,
                               "null_before_ms": cutoff + 1}},
        apply=True, db_path=temp_env)
    assert "invalid null_before_ms" in res["macs"]["EE:EE:EE:EE:EE:EE"]["skipped"]

    # A malformed entry (JSON null / non-numeric string / missing key) must
    # skip that MAC — not abort the whole run with a traceback. The healthy
    # MAC in the same spec must still be processed (proven by its own
    # refusal reason, which requires the loop to have reached it).
    for bad_entry in ({"offset": None, "cutoff_ms": cutoff},
                      {"offset": "abc", "cutoff_ms": cutoff},
                      {"offset": 16.03, "cutoff_ms": cutoff,
                       "null_before_ms": "x"},
                      {"cutoff_ms": cutoff}):
        res = maintenance.repair_yearly_rain_offsets(
            {"EE:EE:EE:EE:EE:EE": bad_entry,
             "FF:FF:FF:FF:FF:FF": {"offset": 16.03, "cutoff_ms": cutoff}},
            apply=True, db_path=temp_env)
        assert "malformed spec entry" in res["macs"]["EE:EE:EE:EE:EE:EE"]["skipped"]
        assert "no post-cutoff" in res["macs"]["FF:FF:FF:FF:FF:FF"]["skipped"]

    vals = [r[0] for r in conn.execute(
        "SELECT yearlyrainin FROM observations ORDER BY mac, dateutc_ms")]
    conn.close()
    assert vals == [17.12, 17.12, 1.09]            # untouched


def test_repair_yearly_rain_offsets_era0_boundary(client, temp_env):
    """The Crestview production shape: the first ~45 minutes after install
    were recorded under a DIFFERENT (unknown) offset, so those values can't
    be repaired by adding the final offset — without `null_before_ms` the
    monotonic guard refuses the whole MAC; with it, the earlier-era rows are
    NULLed (unknowable, like clamped zeros) and the main era repairs cleanly.
    Streaming-backup content is asserted as JSON Lines."""
    import json as _json
    import sqlite3
    from app import maintenance
    cutoff = 1786421263000
    era0_end = cutoff - 1_000_000
    conn = sqlite3.connect(temp_env)
    # Era 0 (install hour, unknown offset): values ABOVE the later adjusted
    # era — these are what tripped the guard in production (3.765 + 16.03
    # would exceed the first raw 17.12).
    _insert_yearly(conn, "AB:AB:AB:AB:AB:AB", era0_end - 200, 3.70)
    _insert_yearly(conn, "AB:AB:AB:AB:AB:AB", era0_end - 100, 3.765)
    # Main offset era: adjusted values, one clamped zero.
    _insert_yearly(conn, "AB:AB:AB:AB:AB:AB", era0_end + 100, 0.0)
    _insert_yearly(conn, "AB:AB:AB:AB:AB:AB", era0_end + 200, 1.09)
    # Raw era after the offset removal.
    _insert_yearly(conn, "AB:AB:AB:AB:AB:AB", cutoff + 100, 17.12)
    conn.commit()

    # Without null_before_ms: refused (era-0 values overshoot the counter).
    res = maintenance.repair_yearly_rain_offsets(
        {"AB:AB:AB:AB:AB:AB": {"offset": 16.03, "cutoff_ms": cutoff}},
        apply=True, db_path=temp_env)
    assert "monotonicity" in res["macs"]["AB:AB:AB:AB:AB:AB"]["skipped"]

    res = maintenance.repair_yearly_rain_offsets(
        {"AB:AB:AB:AB:AB:AB": {"offset": 16.03, "cutoff_ms": cutoff,
                               "null_before_ms": era0_end}},
        apply=True, db_path=temp_env)
    assert res["applied"]
    assert res["macs"]["AB:AB:AB:AB:AB:AB"] == {
        "add_offset_rows": 1, "null_rows": 1, "era0_null_rows": 2}

    vals = [tuple(r) for r in conn.execute(
        "SELECT yearlyrainin, json_extract(data_json, '$.yearlyrainin') "
        "FROM observations WHERE mac='AB:AB:AB:AB:AB:AB' ORDER BY dateutc_ms")]
    conn.close()
    assert vals == [(None, None), (None, None),      # era 0 nulled
                    (None, None),                    # clamped zero nulled
                    (17.12, 17.12),                  # 1.09 + 16.03
                    (17.12, 17.12)]                  # raw untouched
    # Backup is JSON Lines and covers every pre-cutoff non-null row.
    with open(res["backup"]) as backup_file:
        lines = [_json.loads(line) for line in backup_file]
    assert len(lines) == 4
    assert {round(line["yearlyrainin"], 3) for line in lines} == {
        3.70, 3.765, 0.0, 1.09}


async def _ingest(client, mac, ts, **outdoor):
    return client.post("/ingest/custom",
                       headers={"Authorization": "Bearer test-ingest-token"},
                       json={"device": {"id": mac}, "timestamp_utc": ts, **outdoor})


def test_value_at_or_before_supports_non_rain_columns(client):
    """Regression: the smart-alert pressure lookup asserted on a rain-only
    whitelist, so every alert tick raised and frost/heat/pressure never fired."""
    import asyncio
    from app import db
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF"},
                      "timestamp_utc": "2026-07-20T00:00:00Z",
                      "outdoor": {"tempf": 80}, "pressure": {"relative_inhg": 29.90},
                      "source": "test"})
    v = asyncio.run(
        db.value_at_or_before("AA:BB:CC:DD:EE:FF", "baromrelin", 4102444800000))
    assert v == 29.90          # previously raised AssertionError

def test_smart_alerts_tick_runs_without_error(client, monkeypatch):
    """End-to-end: the smart-alert pass must complete for a device that reports
    pressure (the path that used to blow up every 60s)."""
    import asyncio
    from app.config import settings
    from app.alerts import AlertMonitor, EffectiveAlertConfig
    monkeypatch.setattr(settings, "smart_alerts", True)
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF", "name": "Davis"},
                      "timestamp_utc": "2026-07-20T00:00:00Z",
                      "outdoor": {"tempf": 30.0}, "pressure": {"relative_inhg": 29.5},
                      "source": "test"})
    from app import db
    devices = asyncio.run(db.list_devices())
    cfg = EffectiveAlertConfig(enabled=False, transport_configured=False, recipients=[],
                               default_threshold_min=15, repeat_hours=0, smtp_host=None,
                               smtp_port=587, smtp_username=None, smtp_password=None,
                               smtp_from=None, smtp_tls=True, smtp_ssl=False)
    # Must not raise (delivery is a no-op with no transport configured).
    asyncio.run(AlertMonitor()._check_smart_alerts(cfg, devices, 1784500000000))


def test_bucketed_winddir_uses_circular_mean(client):
    """Direction is modular: AVG(355,5) = 180 (due SOUTH) for a north wind.
    Bucketed history must use the vector mean instead."""
    import asyncio, datetime as _dt
    from app import db
    now = _dt.datetime.now(_dt.timezone.utc)
    # Pin BOTH readings inside the SAME 1-minute bucket (bucket = ms // 60000),
    # otherwise each bucket holds one value and even a broken AVG looks right.
    base = (now - _dt.timedelta(hours=1)).replace(second=0, microsecond=0)
    for sec, wd in ((10, 355.0), (40, 5.0)):
        ts = (base + _dt.timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEEFF"}, "timestamp_utc": ts,
                          "outdoor": {"tempf": 80},
                          "wind": {"speed_mph": 10, "dir_deg": wd}, "source": "test"})
    end = int(now.timestamp() * 1000)
    rows = asyncio.run(db.history("AA:BB:CC:DD:EE:FF", end - 24*3600*1000, end))
    dirs = [r["winddir"] for r in rows if r.get("winddir") is not None]
    assert dirs, "expected a bucketed row with a direction"
    # Circular mean of 355 and 5 is ~0/360 (north), NOT 180 (south).
    assert any(d < 15 or d > 345 for d in dirs), f"got {dirs} — south means AVG() is back"

def test_raw_history_keeps_newest_rows_when_limited(client):
    """ASC+LIMIT dropped the NEWEST rows, so a busy short window's chart ended
    hours early. The most recent rows must survive truncation."""
    import asyncio, datetime as _dt
    from app import db
    now = _dt.datetime.now(_dt.timezone.utc)
    for i in range(6):                     # 6 readings, 1 min apart
        ts = (now - _dt.timedelta(minutes=6 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEE22"}, "timestamp_utc": ts,
                          "outdoor": {"tempf": 70.0 + i}, "source": "test"})
    end = int(now.timestamp() * 1000)
    rows = asyncio.run(db.history("AA:BB:CC:DD:EE:22", end - 3600*1000, end, limit=3))
    temps = [r.get("tempf") for r in rows]
    assert temps == sorted(temps), "rows must come back oldest-first"
    assert 75.0 in temps, f"newest reading dropped: {temps}"


def test_records_concurrent_callers_all_get_data(client):
    """Concurrent callers must WAIT for the in-flight compute. The old dedupe
    short-circuited to {} and handed them a 200 with an empty body."""
    import asyncio
    from app import main as _m
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF"},
                      "timestamp_utc": "2026-07-20T00:00:00Z",
                      "outdoor": {"tempf": 88.0}, "source": "test"})
    _m._RECORDS_CACHE.clear()

    async def three():
        return await asyncio.gather(*[_m._cached_records("AA:BB:CC:DD:EE:FF")
                                      for _ in range(3)])
    results = asyncio.run(three())
    assert all(r.get("periods") for r in results), \
        f"a concurrent caller got an empty payload: {[bool(r) for r in results]}"

def test_public_dashboard_concurrent_misses_build_once(client, monkeypatch):
    """A burst on `/` with a COLD cache must run one build, not one per caller.

    The TTL only flattens load once the cache is warm; before this was
    serialized every request arriving during a build started its own full 24h
    aggregation per device — on the only unauthenticated compute path.
    """
    import asyncio
    from app import main as _m
    _m._PUBLIC_DASH_CACHE = None
    _m._PUBLIC_DASH_LOCK = None
    builds = 0

    async def _slow_build(devices, now_ms):
        nonlocal builds
        builds += 1
        await asyncio.sleep(0.05)       # hold the build in flight
        return "<div>dash</div>"
    monkeypatch.setattr(_m, "_build_public_dashboard", _slow_build)

    async def burst():
        return await asyncio.gather(
            *[_m._cached_public_dashboard([{"mac": "AA:BB:CC:DD:EE:FF"}], 0)
              for _ in range(5)])
    results = asyncio.run(burst())
    assert builds == 1, f"cache stampede: {builds} concurrent builds"
    assert results == ["<div>dash</div>"] * 5


def test_records_unknown_mac_404s_and_is_not_cached(client):
    from app import main as _m
    _m._RECORDS_CACHE.clear()
    r = client.get("/api/devices/DE:AD:BE:EF:00:01/records",
                   headers={"Authorization": "Bearer test-api-token"})
    assert r.status_code == 404
    assert _m._RECORDS_CACHE == {}          # no unbounded growth on bogus MACs

def test_records_cache_is_bounded(client):
    """Even if entries are added directly, the cache must not grow forever."""
    from app import main as _m
    _m._RECORDS_CACHE.clear()
    import time as _t
    for i in range(_m._RECORDS_MAX_ENTRIES + 20):
        _m._RECORDS_CACHE[f"MAC{i}"] = (_t.time(), {"periods": {}})
    _m._prune_records_cache()
    assert len(_m._RECORDS_CACHE) <= _m._RECORDS_MAX_ENTRIES

def test_metrics_masks_mac(client, monkeypatch):
    """/metrics is open when enabled — it must mask MACs like the status page."""
    from app.config import settings
    monkeypatch.setattr(settings, "prometheus_metrics", True)
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF", "name": "Davis"},
                      "timestamp_utc": "2026-07-20T00:00:00Z",
                      "outdoor": {"tempf": 88.0}, "source": "test"})
    body = client.get("/metrics").text
    assert "AA:BB:CC:DD:EE:FF" not in body      # full hardware address withheld
    assert "EE:FF" in body                       # last 2 bytes still identify it


def test_value_at_or_before_does_not_fall_back_to_earliest(client):
    """A fixed-window delta must not silently use the earliest row on file: on a
    young device that turns a '3h pressure delta' into 'since we started 10
    minutes ago' and fires bogus storm alerts."""
    import asyncio, datetime as _dt
    from app import db
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF"},
                      "timestamp_utc": "2026-07-20T12:00:00Z",
                      "outdoor": {"tempf": 80}, "pressure": {"relative_inhg": 29.9},
                      "source": "test"})
    ms = int(_dt.datetime(2026, 7, 20, 12, 0, tzinfo=_dt.timezone.utc)
             .timestamp() * 1000)
    # Cutoff BEFORE any data exists -> not computable, must be None.
    assert asyncio.run(db.value_at_or_before("AA:BB:CC:DD:EE:FF", "baromrelin",
                                             ms - 3 * 3600 * 1000)) is None
    # Cutoff at/after the reading -> returns it.
    assert asyncio.run(db.value_at_or_before("AA:BB:CC:DD:EE:FF", "baromrelin",
                                             ms + 1000)) == 29.9
    # Rain rollups still WANT the earliest-row fallback, and that asymmetry
    # with value_at_or_before above is the whole point of this test.
    #
    # This assertion used to read `... is None or True`, which is True for
    # every possible value — and the test posted no rain data at all, so it
    # could not have failed either way. Post rain, then pin the fallback: a
    # cutoff BEFORE any reading exists must still return the earliest yearly
    # total, because a fresh sensor's "rain since Jan 1" is legitimately the
    # first number it ever reported.
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF"},
                      "timestamp_utc": "2026-07-20T12:05:00Z",
                      "outdoor": {"tempf": 80},
                      "rain": {"yearly_in": 12.5},
                      "source": "test"})
    assert asyncio.run(db.yearly_rain_at_or_before(
        "AA:BB:CC:DD:EE:FF", ms - 3 * 3600 * 1000)) == 12.5

def test_records_keeps_legit_wettest_day_in_short_period(client):
    """The cumulative-counter heuristic is judged over ALL history, so a real
    heavy-rain day isn't suppressed just because every reading in that period
    happens to be above the floor (station came online mid-downpour)."""
    import asyncio, datetime as _dt
    from app import db
    now = _dt.datetime.now(_dt.timezone.utc)
    # Early reading with the counter at ~0 (proves it resets), then a big day.
    for mins, daily in ((600, 0.0), (120, 6.2), (60, 6.4)):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEE33"}, "timestamp_utc": ts,
                          "outdoor": {"tempf": 75}, "rain": {"daily_in": daily},
                          "source": "test"})
    recs = asyncio.run(db.records("AA:BB:CC:DD:EE:33", "UTC"))
    allf = recs["periods"]["all"]["fields"]["dailyrainin"]
    assert allf["max"] == 6.4, f"legit wettest day suppressed: {allf}"


def test_bucketed_history_carries_indoor_fields(client):
    """The dashboard asks for 24h — which is BUCKETED — and renders indoor temp
    + humidity (with a sparkline). Those columns were missing from the bucketed
    SELECT, so indoor data only ever appeared when some other screen happened to
    load a raw (<=6h) window into the shared cache."""
    import asyncio, datetime as _dt
    from app import db
    now = _dt.datetime.now(_dt.timezone.utc)
    for mins in (200, 150, 100, 50):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEE44"}, "timestamp_utc": ts,
                          "outdoor": {"tempf": 88.0, "humidity": 30},
                          "indoor": {"tempf": 72.5, "humidity": 41},
                          "source": "test"})
    end = int(now.timestamp() * 1000)
    rows = asyncio.run(db.history("AA:BB:CC:DD:EE:44", end - 24 * 3600 * 1000, end))
    assert rows, "expected bucketed rows"
    assert any(r.get("tempinf") is not None for r in rows), \
        "bucketed history dropped tempinf — dashboard indoor sparkline goes blank"
    assert any(r.get("humidityin") is not None for r in rows)


# ───────────────────────── /api/forecast (R2-22) ─────────────────────────
# The round-1 fix: 400 on bad coords, 502 (not 500) when Open-Meteo is down.
# All upstream traffic is routed through httpx.MockTransport — the suite must
# never hit the real API.

def _mock_forecast_upstream(monkeypatch, handler):
    """Replace httpx.AsyncClient (as used inside get_forecast) with one bound
    to a MockTransport. TestClient itself is a sync httpx.Client — unaffected."""
    import httpx
    real = httpx.AsyncClient
    def factory(*a, **kw):
        return real(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_forecast_rejects_out_of_range_without_upstream_call(client, monkeypatch):
    """?lat=91 must 400 locally. Open-Meteo answers out-of-range with its own
    400, which used to surface here as a bare 500 — and the guard must run
    BEFORE any upstream request is made."""
    import httpx
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("out-of-range coords must not reach the upstream")
    _mock_forecast_upstream(monkeypatch, handler)
    for q in ("lat=91&lon=0", "lat=0&lon=181", "lat=-90.5&lon=0"):
        r = client.get(f"/api/forecast?{q}", headers=_H)
        assert r.status_code == 400, f"{q} → {r.status_code}"
        assert "out of range" in r.json()["detail"]


def test_forecast_502_when_upstream_errors(client, monkeypatch):
    """An Open-Meteo 500/timeout is routine, not our bug: the app needs 502
    ("forecast unavailable"), never a bare 500 ("server error")."""
    import httpx
    _mock_forecast_upstream(monkeypatch,
                            lambda req: httpx.Response(500, text="boom"))
    r = client.get("/api/forecast?lat=33.3&lon=-111.9", headers=_H)
    assert r.status_code == 502
    assert r.json()["detail"] == "forecast upstream unavailable"


def test_forecast_502_when_upstream_returns_non_json(client, monkeypatch):
    """A CDN/maintenance HTML page with a 200 status must also map to 502."""
    import httpx
    _mock_forecast_upstream(monkeypatch,
                            lambda req: httpx.Response(200, text="<html>maintenance</html>"))
    r = client.get("/api/forecast?lat=33.3&lon=-111.9", headers=_H)
    assert r.status_code == 502
    assert r.json()["detail"] == "forecast upstream unavailable"


def test_forecast_falls_back_to_first_device_coords(client, monkeypatch):
    """No query params + no FORECAST_LAT/LON env → the first device's stored
    info.coords must be used."""
    import httpx
    from app.config import settings
    monkeypatch.setattr(settings, "forecast_lat", None)
    monkeypatch.setattr(settings, "forecast_lon", None)
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF"},
                      "timestamp_utc": "2026-06-01T12:00:00Z",
                      "outdoor": {"tempf": 80},
                      "coords": {"lat": 33.3004, "lon": -111.9378},
                      "source": "test"})
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"latitude": 33.3004, "daily": {}})
    _mock_forecast_upstream(monkeypatch, handler)
    r = client.get("/api/forecast", headers=_H)
    assert r.status_code == 200, r.text
    assert float(seen["latitude"]) == 33.3004
    assert float(seen["longitude"]) == -111.9378


def test_forecast_400_when_no_coords_anywhere(client, monkeypatch):
    """No params, no env, no devices with coords → a clear 400, not a crash
    or an upstream call with garbage."""
    import httpx
    from app.config import settings
    monkeypatch.setattr(settings, "forecast_lat", None)
    monkeypatch.setattr(settings, "forecast_lon", None)
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no coords → no upstream call")
    _mock_forecast_upstream(monkeypatch, handler)
    r = client.get("/api/forecast", headers=_H)
    assert r.status_code == 400
    assert "no lat/lon" in r.json()["detail"]


# ───────────────── /api/devices/{mac}/summary (R2-23 + R2-169) ─────────────────

def _seed_summary_device(client):
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    for mins, temp in ((120, 70.0), (60, 90.0)):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEEFF"}, "timestamp_utc": ts,
                          "outdoor": {"tempf": temp, "humidity": 50},
                          "source": "test"})


def test_summary_returns_aggregate_for_valid_field(client):
    _seed_summary_device(client)
    r = client.get("/api/devices/AA:BB:CC:DD:EE:FF/summary?field=tempf&hours=24",
                   headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["field"] == "tempf"
    assert body["count"] == 2
    assert body["min"] == 70.0 and body["max"] == 90.0 and body["avg"] == 80.0
    assert body["minAt"] is not None and body["maxAt"] is not None
    assert body["minAt"] < body["maxAt"]       # 70 was posted before 90


def test_summary_rejects_unknown_field(client):
    """db.aggregate interpolates the column name into SQL guarded only by its
    whitelist; the ValueError→400 bridge in get_summary IS the SQL-injection
    guard for user-supplied `field` — it must hold for anything not in the map."""
    _seed_summary_device(client)
    for bad in ("bogus", "tempf;DROP TABLE observations", "data_json",
                "tempf--", "mac"):
        r = client.get("/api/devices/AA:BB:CC:DD:EE:FF/summary",
                       params={"field": bad}, headers=_H)
        assert r.status_code == 400, f"field={bad!r} → {r.status_code}"
    # ...and the table is still there.
    assert client.get("/api/devices/AA:BB:CC:DD:EE:FF/summary?field=tempf",
                      headers=_H).status_code == 200


def test_summary_validates_hours_bounds_and_auth(client):
    _seed_summary_device(client)
    assert client.get("/api/devices/AA:BB:CC:DD:EE:FF/summary?hours=0",
                      headers=_H).status_code == 422
    assert client.get("/api/devices/AA:BB:CC:DD:EE:FF/summary?hours=721",
                      headers=_H).status_code == 422
    assert client.get("/api/devices/AA:BB:CC:DD:EE:FF/summary").status_code == 401


# ───────────────── PUT /api/push/relay update/clear semantics (R2-163) ─────────────────

def test_push_relay_partial_updates_preserve_and_empty_string_clears(client):
    """Omitting a field must PRESERVE it; "" must CLEAR it. A regression here
    silently breaks push delivery (e.g. every token edit wiping the URL)."""
    H = {"Authorization": "Bearer test-api-token"}
    U = "https://weather.zasder.com/api/relay/push"
    # Set both.
    assert client.put("/api/push/relay", headers=H,
                      json={"relay_url": U, "relay_token": "s1"}).status_code == 200
    g = client.get("/api/push/relay", headers=H).json()
    assert g == {"relay_url": U, "relay_token_set": True, "relay_configured": True}
    # Token-only update: URL preserved.
    client.put("/api/push/relay", headers=H, json={"relay_token": "s2"})
    g = client.get("/api/push/relay", headers=H).json()
    assert g["relay_url"] == U and g["relay_token_set"] is True
    assert g["relay_configured"] is True
    # URL cleared with "": token STAYS set, but configured flips off.
    client.put("/api/push/relay", headers=H, json={"relay_url": ""})
    g = client.get("/api/push/relay", headers=H).json()
    assert g["relay_url"] is None
    assert g["relay_token_set"] is True
    assert g["relay_configured"] is False
    # URL-only update re-arms without re-sending the token.
    client.put("/api/push/relay", headers=H, json={"relay_url": U})
    g = client.get("/api/push/relay", headers=H).json()
    assert g["relay_configured"] is True and g["relay_token_set"] is True
    # Token cleared with "": URL preserved, configured off.
    client.put("/api/push/relay", headers=H, json={"relay_token": ""})
    g = client.get("/api/push/relay", headers=H).json()
    assert g["relay_url"] == U
    assert g["relay_token_set"] is False and g["relay_configured"] is False


# ───────────── rain-glitch guard: backfill + exact post-glitch resume (R2-168) ─────────────

def test_rain_glitch_guard_allows_backfill_and_resumes_after_glitch(client, monkeypatch):
    """(a) A backfilled (older-timestamp) reading with a LOWER cumulative value
    is legitimate history and must be stored untouched — negative deltas are
    the counter-reset path, never a "spike". (b) After a dropped glitch the
    next real reading resumes from the last GOOD value (the glitch was stored
    NULL, so the guard compares 3.60 to 3.58, not to 9.58)."""
    from datetime import datetime, timedelta, timezone
    from app import config
    monkeypatch.setattr(config.settings, "ingest_max_rain_rate_in_per_hr", 2.0)
    ih = {"Authorization": "Bearer test-ingest-token"}
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    def post(mins, yearly):
        r = client.post("/ingest/custom", headers=ih, json={
            "device": {"id": "5D5D0200007D"},
            "timestamp_utc": (base + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outdoor": {"tempf": 70}, "rain": {"yearly_in": yearly},
            "source": "fineoffset-wh24"})
        assert r.status_code == 200, r.text
    post(60, 3.58)            # good baseline
    post(120, 9.58)           # +6" in an hour → glitch, dropped
    post(150, 3.60)           # resumes cleanly from 3.58, must be stored
    post(0, 2.00)             # backfill: OLDER ts, lower value → stored untouched
    hist = client.get("/api/devices/5D:5D:02:00:00:7D/history?hours=5",
                      headers=_H).json()["rows"]          # ≤6h → raw rows
    ys = [r.get("yearlyrainin") for r in hist]
    assert ys == [2.00, 3.58, None, 3.60], f"got {ys}"


# ───────────── write-throttle exact-boundary pin (`<` not `<=`) (R2-177) ─────────────

def test_history_write_throttle_exact_interval_boundary(client, monkeypatch):
    """A reading at EXACTLY ingest_min_interval_seconds after the last stored
    row is OUTSIDE the window and must be stored (the comparison is strict
    `<`). An off-by-one to `<=` silently halves storage density for sources
    posting exactly on the interval — pin the intended side."""
    from app import config
    ih = {"Authorization": "Bearer test-ingest-token"}
    def post(ts, temp):
        return client.post("/ingest/custom", headers=ih, json={
            "device": {"id": "5D5D0200007D"}, "timestamp_utc": ts,
            "outdoor": {"tempf": temp, "humidity": 50},
            "source": "fineoffset-wh24"}).json()
    monkeypatch.setattr(config.settings, "ingest_min_interval_seconds", 60)
    assert post("2026-05-25T10:00:00Z", 70)["inserted"] == 1
    # 59 s after the stored row: inside the window → coalesced.
    r59 = post("2026-05-25T10:00:59Z", 71)
    assert r59["inserted"] == 0 and r59["throttled"] is True
    # Exactly 60 s after the stored row: NOT "recent" → stored.
    r60 = post("2026-05-25T10:01:00Z", 72)
    assert r60["inserted"] == 1 and r60["throttled"] is False


# ───────── reviewer token: discovery reads + location write (R2-176) ─────────

def test_reviewer_token_rejected_on_discovery_reads(client):
    """GET /api/discoveries is gated on the PRIMARY token only — the RF survey
    is not part of the weather views a demo/reviewer token needs. A refactor
    to require_token (read set) would silently hand it to the demo token."""
    ih = {"Authorization": "Bearer test-ingest-token"}
    client.post("/ingest/discovery", headers=ih,
                json={"model": "TPMS-Toyota", "id": 7})
    for path in ("/api/discoveries",):
        r = client.get(path, headers=REVIEWER)
        # 401 here, not 403: meter/discovery reads gate on the primary token via
        # a dep that does not special-case the reviewer token (by design —
        # these routes are invisible to it, not "insufficient permission").
        assert r.status_code == 401, f"reviewer GET {path} → {r.status_code}"
        r = client.get(path, headers=WRITER)
        assert r.status_code == 200, f"primary GET {path} → {r.status_code}"


def test_reviewer_token_rejected_on_location_put(client):
    """PUT /api/devices/{mac}/location mutates stored coords (a home address
    class of data) — the read-only reviewer token must be refused."""
    _ingest_device(client)
    body = {"lat": 33.3, "lon": -111.9, "label": "Home"}
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/location",
                   headers=REVIEWER, json=body)
    assert r.status_code == 403
    assert client.put("/api/devices/AA:BB:CC:DD:EE:FF/location",
                      headers=WRITER, json=body).status_code == 200


# ───── device-stale alert must retry after failed delivery (R2-173, companion to R2-07) ─────

def test_stale_alert_retries_when_delivery_fails(client, monkeypatch):
    """Mirror of test_threshold_alert_retries_when_delivery_fails for the
    device-down path: when the INITIAL ok→stale event fails on every channel,
    the state must NOT advance to 'stale' — decide() only fires on a state
    change, so persisting it would drop the most important alert forever."""
    import asyncio
    import app.alerts as alerts
    from app.alerts import AlertMonitor
    from app import db
    mac = "AA:BB:CC:DD:EE:FF"
    # A device whose last report is hours in the past (>> the 15-min default).
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEEFF", "name": "Yard"},
                      "timestamp_utc": "2026-06-01T12:00:00Z",
                      "outdoor": {"tempf": 75}})
    calls = {"n": 0}
    async def failing_deliver(cfg, subj, body, ptitle, pbody):
        calls["n"] += 1
        return False
    monkeypatch.setattr(alerts, "_deliver", failing_deliver)
    # _tick returns early unless SOME channel exists; pretend push does.
    async def push_on():
        return True
    monkeypatch.setattr(alerts.apns, "push_configured", push_on)

    async def two_failing_ticks():
        # Seed a prior 'ok' state so this tick sees the ok→stale TRANSITION
        # (first sight only baselines and never alerts).
        await db.upsert_alert_state(mac, "ok", None, 0, None)
        mon = AlertMonitor()
        await mon._tick()
        s1 = await db.get_alert_states()
        await mon._tick()
        s2 = await db.get_alert_states()
        return s1, s2
    s1, s2 = asyncio.run(two_failing_ticks())
    assert calls["n"] == 2, "the stale alert must be retried while delivery fails"
    assert s1[mac]["state"] == "ok", "state advanced on a failed delivery"
    assert s2[mac]["state"] == "ok"
    assert s1[mac]["notified_ms"] is None

    # Delivery recovers → the alert goes out once, state + notify clock advance.
    async def good_deliver(cfg, subj, body, ptitle, pbody):
        calls["n"] += 1
        return True
    monkeypatch.setattr(alerts, "_deliver", good_deliver)
    async def one_good_tick():
        await AlertMonitor()._tick()
        return await db.get_alert_states()
    s3 = asyncio.run(one_good_tick())
    assert calls["n"] == 3
    assert s3[mac]["state"] == "stale"
    assert s3[mac]["notified_ms"] is not None
