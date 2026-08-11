"""Backend configuration backup / restore.

Exists because a self-hoster's backend crash-looped this week and would have
taken every hand-configured alert with it. The tests that matter here are
about what must NOT be in the file, and about restore refusing rather than
silently doing nothing.
"""
from __future__ import annotations

import pytest

H = {"Authorization": "Bearer test-api-token"}
READ_ONLY = {"Authorization": "Bearer test-reviewer-token"}


def test_backup_requires_a_token(client):
    assert client.get("/api/config/backup").status_code == 401


def test_backup_has_the_expected_shape(client):
    r = client.get("/api/config/backup", headers=H)
    assert r.status_code == 200
    b = r.json()
    for key in ("version", "alert_prefs", "device_alert_prefs",
                "alert_rules", "device_locations"):
        assert key in b, f"missing {key}"


def test_backup_carries_no_credentials(client):
    """THE test. This file is meant to be safe to keep, unlike the per-device
    settings export — if a token or the SMTP password leaks into it, that
    property is gone and nobody would notice.

    Stores a real password first: asserting on key names would pass even if
    the value leaked under some other key."""
    secret = "hunter2-app-password-do-not-leak"
    r = client.put("/api/alerts", headers=H,
                   json={"smtp_host": "mail.example.com", "smtp_port": 465,
                         "smtp_username": "alerts@example.com",
                         "smtp_password": secret,
                         "recipients": ["me@example.com"]})
    assert r.status_code in (200, 204), r.text
    # The password really is stored (otherwise this test proves nothing).
    assert client.get("/api/alerts", headers=H).json()["smtp_password_set"] is True

    body = client.get("/api/config/backup", headers=H).text
    assert secret not in body, "SMTP password leaked into the config backup"
    assert "test-api-token" not in body
    assert "test-ingest-token" not in body

    b = client.get("/api/config/backup", headers=H).json()
    assert b["smtp_password_included"] is False
    # No key named smtp_password anywhere in the document.
    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert "smtp_password" not in set(keys(b))
    # ...but the non-secret transport settings DID survive, or the backup
    # would be useless for its actual purpose.
    assert b["alert_prefs"]["smtp_host"] == "mail.example.com"
    assert b["alert_prefs"]["smtp_username"] == "alerts@example.com"


def test_backup_warns_inside_the_file(client):
    b = client.get("/api/config/backup", headers=H).json()
    assert "_WARNING" in b
    assert "token" in b["_WARNING"].lower()


def test_backup_is_write_gated_because_it_carries_operator_pii(client):
    """The read-only reviewer/demo token must NOT be able to read this.

    The file contains home coordinates, alert recipient addresses and SMTP
    identifiers. /api/alerts redacts those for the reviewer token; this
    endpoint handed over the unredacted originals, so the redaction there was
    decorative. Read-gating was the bug — a backup is not a read of weather
    data, it is a dump of the operator's configuration.
    """
    r = client.get("/api/config/backup", headers=READ_ONLY)
    assert r.status_code == 401, "reviewer token could read operator PII"
    # ...and the primary token still can, or the feature is dead.
    assert client.get("/api/config/backup", headers=H).status_code == 200


def test_restore_is_write_gated(client):
    """The read-only reviewer token must not be able to reconfigure alerting
    for every client of this backend."""
    r = client.post("/api/config/restore", headers=READ_ONLY, json={"version": 1})
    assert r.status_code == 401


def test_restore_rejects_a_foreign_file(client):
    r = client.post("/api/config/restore", headers=H, json={"some": "other json"})
    assert r.status_code == 400
    assert "configuration backup" in r.json()["detail"]


def test_restore_rejects_a_newer_format(client):
    r = client.post("/api/config/restore", headers=H,
                    json={"version": 99, "alert_prefs": {}})
    assert r.status_code == 400
    assert "newer" in r.json()["detail"]


def test_restore_refuses_when_nothing_would_change(client):
    """A restore that silently does nothing is worse than one that fails —
    the user walks away believing their config is back."""
    r = client.post("/api/config/restore", headers=H, json={"version": 1})
    assert r.status_code == 400
    assert "nothing" in r.json()["detail"]


def test_round_trip_restores_alert_rules(client):
    # Create a rule, back up, delete it, restore, and confirm it returns.
    made = client.post("/api/alerts/rules", headers=H,
                       json={"field": "tempf", "comparator": "above",
                             "threshold": 100.0})
    assert made.status_code in (200, 201), made.text
    backup = client.get("/api/config/backup", headers=H).json()
    assert len(backup["alert_rules"]) == 1

    rule_id = client.get("/api/alerts/rules", headers=H).json()[0]["id"]
    client.delete(f"/api/alerts/rules/{rule_id}", headers=H)
    assert client.get("/api/alerts/rules", headers=H).json() == []

    r = client.post("/api/config/restore", headers=H, json=backup)
    assert r.status_code == 200, r.text
    assert r.json()["restored"]["alert_rules"] == 1
    restored = client.get("/api/alerts/rules", headers=H).json()
    assert len(restored) == 1
    assert restored[0]["field"] == "tempf"
    assert restored[0]["threshold"] == 100.0


def test_restore_replaces_rules_rather_than_duplicating(client):
    """Rules have no stable identity across servers, so a second restore must
    not stack another copy of everything."""
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above", "threshold": 100.0})
    backup = client.get("/api/config/backup", headers=H).json()
    client.post("/api/config/restore", headers=H, json=backup)
    client.post("/api/config/restore", headers=H, json=backup)
    assert len(client.get("/api/alerts/rules", headers=H).json()) == 1


def test_restore_says_the_smtp_password_is_missing(client):
    """It can't be restored — the API never returns it. Saying so beats
    letting someone find out when an alert fails to send."""
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above", "threshold": 1.0})
    backup = client.get("/api/config/backup", headers=H).json()
    r = client.post("/api/config/restore", headers=H, json=backup)
    assert "SMTP password" in r.json()["note"]


def test_a_malformed_rule_does_not_sink_the_restore(client):
    """One bad entry in a hand-edited file shouldn't discard the good ones."""
    payload = {
        "version": 1,
        "alert_rules": [
            {"field": "tempf", "comparator": "above", "threshold": 90.0},
            {"field": "tempf"},                       # missing comparator
            {"comparator": "below", "threshold": "x"},  # unparseable
        ],
    }
    r = client.post("/api/config/restore", headers=H, json=payload)
    assert r.status_code == 200
    assert r.json()["restored"]["alert_rules"] == 1


def test_device_locations_actually_restore(client):
    """Regression: set_device_location takes a now_ms the restore never
    passed, and the except swallowed the TypeError — so locations silently
    never came back while the changelog claimed they did. No test covered it."""
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEE01"},
                      "timestamp_utc": "2026-08-09T12:00:00Z",
                      "outdoor": {"tempf": 70, "humidity": 50},
                      "wind": {}, "rain": {}, "pressure": {}, "source": "t"})
    mac = "AA:BB:CC:DD:EE:01"
    assert client.put(f"/api/devices/{mac}/location", headers=H,
                      json={"lat": 33.3, "lon": -111.9,
                            "label": "Home"}).status_code == 200

    backup = client.get("/api/config/backup", headers=H).json()
    assert mac in backup["device_locations"], "location missing from backup"

    # Move it somewhere else first. Restoring on top of the identical value
    # would pass even if restore silently skipped the write.
    assert client.put(f"/api/devices/{mac}/location", headers=H,
                      json={"lat": 0.0, "lon": 0.0,
                            "label": "Moved"}).status_code == 200

    r = client.post("/api/config/restore", headers=H, json=backup)
    assert r.status_code == 200, r.text
    assert r.json()["restored"]["device_locations"] == 1, \
        "locations reported as restored"

    devs = client.get("/api/devices", headers=H).json()
    d = next(x for x in devs if x["mac"] == mac)
    coords = d["info"]["coords"]["coords"]
    # The ORIGINAL coordinates are back, not the ones we moved it to.
    assert abs(coords["lat"] - 33.3) < 1e-6, "restore did not write the location"
    assert abs(coords["lon"] - (-111.9)) < 1e-6


def test_all_rules_malformed_does_not_wipe_existing_rules(client):
    """Regression: restore deleted every rule BEFORE validating the
    replacements, so a file whose rules were all malformed left the server
    with none — the worst outcome a 'restore' can produce."""
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above", "threshold": 90.0})
    assert len(client.get("/api/alerts/rules", headers=H).json()) == 1

    bad = {"version": 1, "alert_rules": [{"field": "tempf"},
                                         {"comparator": "below"}]}
    client.post("/api/config/restore", headers=H, json=bad)
    assert len(client.get("/api/alerts/rules", headers=H).json()) == 1, \
        "existing rules were destroyed by an unusable payload"


def test_an_explicitly_empty_rule_list_still_clears(client):
    """...but "I have no rules" must remain a legitimate thing to restore."""
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above", "threshold": 90.0})
    payload = {"version": 1, "alert_rules": [], "alert_prefs": {"repeat_hours": 6}}
    r = client.post("/api/config/restore", headers=H, json=payload)
    assert r.status_code == 200, r.text
    assert client.get("/api/alerts/rules", headers=H).json() == []


def test_reviewer_token_cannot_read_station_coordinates(client):
    """/api/devices exposed the operator's HOME coordinates to the demo token.

    The reviewer credential ships in App Store Connect, so anyone reviewing the
    app could read where the developer lives. The weather data itself is the
    point of the demo; the location is not.
    """
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDDEE09"},
                      "timestamp_utc": "2026-08-10T12:00:00Z",
                      "outdoor": {"tempf": 70, "humidity": 50},
                      "wind": {}, "rain": {}, "pressure": {}, "source": "t"})
    mac = "AA:BB:CC:DD:EE:09"
    assert client.put(f"/api/devices/{mac}/location", headers=H,
                      json={"lat": 33.3062, "lon": -111.8413,
                            "label": "Home"}).status_code == 200

    # The primary token still sees everything — the app needs coords for the
    # sun dial and the forecast.
    full = next(d for d in client.get("/api/devices", headers=H).json()
                if d["mac"] == mac)
    assert (full.get("info") or {}).get("coords") is not None

    body = client.get("/api/devices", headers=READ_ONLY).text
    assert "33.3062" not in body, "reviewer token could read home latitude"
    assert "-111.8413" not in body, "reviewer token could read home longitude"
    # The LABEL is PII too. Checking only the coordinates would pass a
    # regression that exposed info.location ("Home") while stripping coords.
    assert "Home" not in body, "reviewer token could read the location label"
    redacted = client.get("/api/devices", headers=READ_ONLY).json()
    row = next(d for d in redacted if d["mac"] == mac)
    assert row.get("location") is None
    info = row.get("info") or {}
    assert info.get("coords") is None and info.get("location") is None
    # ...but the station is still listed, or the demo shows an empty app.
    seen = client.get("/api/devices", headers=READ_ONLY).json()
    assert any(d["mac"] == mac for d in seen)
