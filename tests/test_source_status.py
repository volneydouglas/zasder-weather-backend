"""Ingest-source health reporting.

Motivated by a real incident: an AcuRite Atlas went quiet and there was no
way to tell whether the station, the receiver, the network or expired API
credentials had failed — nothing recorded which leg last worked. These tests
pin the distinctions that make that answerable.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def ss(temp_env: str):
    for mod in ["app.config", "app.source_status"]:
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import source_status
    source_status.reset()
    return source_status


def by_name(snapshot, name):
    return next(s for s in snapshot if s["name"] == name)


def test_declared_but_never_run_is_not_healthy(ss):
    """A configured source that has never succeeded must not read as healthy —
    that's the state right after a bad deploy."""
    ss.declare("ambientweather", True)
    s = by_name(ss.snapshot(), "ambientweather")
    assert s["configured"] is True
    assert s["healthy"] is False
    assert s["last_success_ms"] is None


def test_unconfigured_is_distinguishable_from_broken(ss):
    """THE distinction. 'Not set up' and 'set up but failing' need entirely
    different fixes, and look identical from outside."""
    ss.declare("ambientweather", False)          # never configured
    ss.declare("davis-cloud", True)              # configured...
    ss.record_failure("davis-cloud", "401 Unauthorized")   # ...and failing
    snap = ss.snapshot()
    unconfigured = by_name(snap, "ambientweather")
    broken = by_name(snap, "davis-cloud")

    assert unconfigured["configured"] is False
    assert unconfigured["last_error"] is None
    assert broken["configured"] is True
    assert broken["last_error"] == "401 Unauthorized"
    # Both are "not healthy", but for reasons a client can tell apart.
    assert unconfigured["healthy"] is False and broken["healthy"] is False


def test_success_marks_healthy_and_records_rows(ss):
    ss.declare("ambientweather", True)
    ss.record_success("ambientweather", rows=3)
    s = by_name(ss.snapshot(), "ambientweather")
    assert s["healthy"] is True
    assert s["last_rows"] == 3
    assert s["seconds_since_success"] is not None
    assert s["consecutive_failures"] == 0


def test_success_after_failures_clears_the_error(ss):
    """A source that recovers must stop reporting a stale error, or every
    transient blip looks permanent."""
    ss.declare("davis-cloud", True)
    ss.record_failure("davis-cloud", "timeout")
    ss.record_failure("davis-cloud", "timeout")
    assert by_name(ss.snapshot(), "davis-cloud")["consecutive_failures"] == 2
    ss.record_success("davis-cloud", rows=1)
    s = by_name(ss.snapshot(), "davis-cloud")
    assert s["consecutive_failures"] == 0
    assert s["last_error"] is None
    assert s["healthy"] is True


def test_failures_accumulate(ss):
    ss.declare("davis-cloud", True)
    for _ in range(3):
        ss.record_failure("davis-cloud", "boom")
    assert by_name(ss.snapshot(), "davis-cloud")["consecutive_failures"] == 3


def test_succeeding_with_zero_rows_is_still_success_but_visible(ss):
    """Credentials fine, API answering, nothing arriving — its own failure
    mode, and one a bare healthy/unhealthy flag would hide."""
    ss.declare("davis-cloud", True)
    ss.record_success("davis-cloud", rows=0)
    s = by_name(ss.snapshot(), "davis-cloud")
    assert s["healthy"] is True
    assert s["last_rows"] == 0


def test_error_text_is_bounded(ss):
    """Upstream errors can embed enormous bodies; this ends up in an API
    response and must not become the payload."""
    ss.declare("davis-cloud", True)
    ss.record_failure("davis-cloud", "x" * 5000)
    assert len(by_name(ss.snapshot(), "davis-cloud")["last_error"]) <= 300


def test_unhealthy_sources_sort_first(ss):
    """A client renders this top-down; the broken one shouldn't be buried."""
    ss.declare("aaa-healthy", True)
    ss.record_success("aaa-healthy", rows=1)
    ss.declare("zzz-broken", True)
    ss.record_failure("zzz-broken", "nope")
    assert ss.snapshot()[0]["name"] == "zzz-broken"


def test_reset_clears_state(ss):
    """Process-global state, so tests must be able to isolate — same reason
    conftest resets the caches in main.py."""
    ss.declare("ambientweather", True)
    ss.record_success("ambientweather", rows=1)
    ss.reset()
    assert ss.snapshot() == []


# ── HTTP surface ─────────────────────────────────────────────────────────
def test_endpoint_requires_a_token(client):
    assert client.get("/api/sources").status_code == 401


def test_endpoint_lists_declared_sources(client):
    H = {"Authorization": "Bearer test-api-token"}
    r = client.get("/api/sources", headers=H)
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["sources"]}
    # Declared at startup regardless of configuration.
    assert {"ambientweather", "davis-cloud", "custom-ingest"} <= names


def test_endpoint_reports_unconfigured_sources_as_such(client):
    """conftest blanks the cloud credentials, so both cloud pollers should
    report configured=False rather than being absent."""
    H = {"Authorization": "Bearer test-api-token"}
    sources = {s["name"]: s for s in client.get("/api/sources", headers=H).json()["sources"]}
    assert sources["ambientweather"]["configured"] is False
    assert sources["davis-cloud"]["configured"] is False
    # Custom ingest is always available — it needs no credentials of ours.
    assert sources["custom-ingest"]["configured"] is True


def test_credentials_in_an_error_url_are_redacted(ss):
    """Regression: AmbientWeather passes its keys as QUERY PARAMETERS and
    httpx embeds the full URL in the exception message. Storing raw error text
    and serving it from /api/sources would have re-created the exact leak that
    ambient_client's scrubbed error was written to close."""
    ss.declare("ambientweather", True)
    ss.record_failure(
        "ambientweather",
        "Client error '401' for url 'https://rt.ambientweather.net/v1/devices"
        "?applicationKey=SUPERSECRETAPPKEY&apiKey=SUPERSECRETAPIKEY'")
    err = by_name(ss.snapshot(), "ambientweather")["last_error"]
    assert "SUPERSECRETAPPKEY" not in err
    assert "SUPERSECRETAPIKEY" not in err
    assert "redacted" in err
    # ...but it's still diagnostically useful.
    assert "401" in err and "ambientweather.net" in err


def test_non_secret_query_params_survive_redaction(ss):
    """Over-redacting would make errors useless — keep the harmless parts."""
    ss.declare("davis-cloud", True)
    ss.record_failure("davis-cloud",
                      "404 for url 'https://api.weatherlink.com/v2/current/12345"
                      "?station-id=12345&api-key=SECRETVALUE'")
    err = by_name(ss.snapshot(), "davis-cloud")["last_error"]
    assert "SECRETVALUE" not in err
    assert "station-id=12345" in err


def test_basic_auth_password_is_redacted(ss):
    """Found by probing the redaction rather than by review: a URL of the form
    scheme://user:password@host passed straight through, so any client
    configured that way would have published its password on /api/sources."""
    ss.declare("davis-cloud", True)
    ss.record_failure("davis-cloud",
                      "Connection failed for 'https://admin:HUNTER2@example.com/v1/x'")
    err = by_name(ss.snapshot(), "davis-cloud")["last_error"]
    assert "HUNTER2" not in err
    assert "redacted" in err
    # The username and host stay — without them the error says nothing.
    assert "admin" in err and "example.com" in err


def test_custom_ingest_reports_healthy_once_something_posts(client):
    """Shipped briefly reporting healthy=False while boards were actively
    posting: custom ingest is a push path, so nothing recorded success for it.
    A health endpoint that calls a working source dead is worse than none."""
    H = {"Authorization": "Bearer test-api-token"}
    before = {s["name"]: s for s in client.get("/api/sources", headers=H).json()["sources"]}
    assert before["custom-ingest"]["healthy"] is False   # nothing posted yet

    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDDEE02"},
                          "timestamp_utc": "2026-08-09T12:00:00Z",
                          "outdoor": {"tempf": 70, "humidity": 50},
                          "wind": {}, "rain": {}, "pressure": {}, "source": "t"})
    assert r.status_code == 200, r.text

    after = {s["name"]: s for s in client.get("/api/sources", headers=H).json()["sources"]}
    assert after["custom-ingest"]["healthy"] is True
    assert after["custom-ingest"]["last_rows"] == 1


# ── 2.2: who has to act, and since when ──────────────────────────────────

@pytest.mark.parametrize("error,kind", [
    ("AirGradient request failed: ReadTimeout", "upstream"),
    ("AirGradient HTTP 500 on /public/api/v1/locations/measures/current", "upstream"),
    ("HTTP 503 Service Unavailable", "upstream"),
    ("ConnectError", "upstream"),
    ("AirGradient response is not a list", "upstream"),
    ("401 Unauthorized", "credentials"),
    ("HTTP 403 Forbidden", "credentials"),
    ("WU rejected the station ID/key", "credentials"),
    ("Tempest HTTP 429 on /observations", "rate_limit"),
    ("Govee: rate limit exceeded", "rate_limit"),
    ("KeyError: 'obs'", "ours"),
    ("", "ours"),
    (None, "ours"),
])
def test_classify_sorts_errors_by_who_must_act(ss, error, kind):
    assert ss.classify(error) == kind


def test_failing_since_is_the_first_failure_of_the_streak(ss, monkeypatch):
    """"Not answering since 13:27" needs the START of the streak; last_error_ms
    is the latest retry and moves every tick."""
    clock = {"t": 1_000_000}
    monkeypatch.setattr(ss, "_now_ms", lambda: clock["t"])
    ss.declare("airgradient", True)
    ss.record_success("airgradient", rows=2)
    clock["t"] += 120_000
    ss.record_failure("airgradient", "AirGradient request failed: ReadTimeout")
    first = clock["t"]
    clock["t"] += 120_000
    ss.record_failure("airgradient", "AirGradient HTTP 500 on /x")
    s = by_name(ss.snapshot(), "airgradient")
    assert s["failing_since_ms"] == first
    assert s["last_error_ms"] == clock["t"]
    assert s["consecutive_failures"] == 2
    assert s["last_error_kind"] == "upstream"
    assert s["label"] == "AirGradient"
    ss.record_success("airgradient", rows=2)
    s = by_name(ss.snapshot(), "airgradient")
    assert s["failing_since_ms"] is None and s["last_error_kind"] is None


def test_health_for_device_joins_a_polled_device_to_its_source(ss):
    ss.declare("tempest", True)
    ss.record_failure("tempest", "ConnectTimeout")
    h = ss.health_for_device("tempest")
    assert h["name"] == "tempest" and h["label"] == "Tempest"
    assert h["healthy"] is False and h["last_error_kind"] == "upstream"
    # The WeatherLink poller stamps its own source string.
    ss.declare("davis-cloud", True)
    assert ss.health_for_device("davis-vp2-cloud")["name"] == "davis-cloud"


def test_health_for_device_is_none_for_local_and_unknown_feeds(ss):
    """A relay board, the WLL bridge or a local Ecowitt push has no vendor
    to blame; and a poller not declared this boot must not judge anything."""
    ss.declare("tempest", True)
    assert ss.health_for_device("fineoffset-wh24-lilygo") is None
    assert ss.health_for_device("davis-wll-local") is None
    assert ss.health_for_device("ecowitt") is None
    assert ss.health_for_device(None) is None
    assert ss.health_for_device("govee") is None, "govee never declared this boot"


def test_devices_endpoint_carries_source_health(client):
    """The app reads /api/devices, not /api/sources: the verdict rides on
    the device row, None for devices nobody polls for."""
    from app import source_status
    source_status.declare("tempest", True)
    source_status.record_failure("tempest", "Tempest request failed: ReadTimeout")
    hdr = {"Authorization": "Bearer test-ingest-token"}
    def obs(dev_id, source, ts, tempf):
        return {"device": {"id": dev_id, "model": "Test"},
                "timestamp_utc": ts,
                "outdoor": {"tempf": tempf, "humidity": 50},
                "wind": {}, "rain": {}, "pressure": {}, "source": source}
    # Two MACs that differ in many bits: a near-twin of a known MAC sits in
    # new-device probation (device_probation.py) and would be quarantined.
    polled = obs("AABBCCDDEE01", "tempest", "2026-09-08T06:00:00Z", 70)
    local = obs("112233445566", "fineoffset-wh24-lilygo", "2026-09-08T06:01:00Z", 64)
    for body in (polled, local):
        r = client.post("/ingest/custom", headers=hdr, json=body)
        assert r.status_code == 200 and r.json()["inserted"] == 1, r.json()
    api = {"Authorization": "Bearer test-api-token"}
    # Keyed by what the device posts as its source: the relay path files its
    # device under a synthetic MAC, so the MAC is not the stable handle here.
    rows = {(d.get("info") or {}).get("source"): d
            for d in client.get("/api/devices", headers=api).json()}
    h = rows["tempest"]["source_health"]
    assert h["label"] == "Tempest" and h["healthy"] is False
    assert h["last_error_kind"] == "upstream" and h["consecutive_failures"] == 1
    assert rows["fineoffset-wh24-lilygo"]["source_health"] is None, "a relay board has no vendor"

