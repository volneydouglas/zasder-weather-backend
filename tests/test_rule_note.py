"""2.3 (Doren's Automower 115H): a note the owner types rides in the
threshold push, and the push names the rule so the phone can open what
it keeps for it. The push never carries a URL."""
import asyncio

H = {"Authorization": "Bearer test-api-token"}


def test_note_round_trips_and_is_cleaned(client):
    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "hourlyrainin", "comparator": "above",
                          "threshold": 0.1, "severity": "urgent",
                          "note": "  Park the\n115H\t now  "})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["note"] == "Park the 115H now"
    listed = [x for x in client.get("/api/alerts/rules", headers=H).json() if x["id"] == rid][0]
    assert listed["note"] == "Park the 115H now"
    # Patch changes it; "" clears; an omitted note leaves it alone.
    r = client.patch(f"/api/alerts/rules/{rid}", headers=H, json={"note": "x" * 200})
    assert r.status_code == 200 and len(r.json()["note"]) == 120
    r = client.patch(f"/api/alerts/rules/{rid}", headers=H, json={"threshold": 0.2})
    assert r.json()["note"] is not None and r.json()["threshold"] == 0.2
    r = client.patch(f"/api/alerts/rules/{rid}", headers=H, json={"note": ""})
    assert r.json()["note"] is None
    r = client.patch(f"/api/alerts/rules/{rid}", headers=H, json={"enabled": False})
    assert r.json()["note"] is None and r.json()["enabled"] is False
    # A rule made without one reads null, not "".
    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "tempf", "comparator": "above", "threshold": 100})
    assert r.json()["note"] is None
    # Too long for the model is a 422, not a truncation surprise.
    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "tempf", "comparator": "above", "threshold": 100,
                          "note": "y" * 401})
    assert r.status_code == 422


def test_the_message_carries_the_note_after_the_reading(client):
    from app import alerts
    t, b = alerts.build_threshold_message("Chaucer Drive", "hourlyrainin", 0.12,
                                          "above", 0.1, note="Park the 115H")
    assert t == "Chaucer Drive: Rain Rate alert"
    assert b == "Rain Rate is 0.12 in/hr (> 0.1 in/hr). Park the 115H"
    _, plain = alerts.build_threshold_message("D", "tempf", 81, "above", 80)
    assert plain == "Temperature is 81°F (> 80°F)"
    assert alerts.clean_rule_note("\x07 hi\x00 there ") == "hi there"
    assert alerts.clean_rule_note("   ") is None and alerts.clean_rule_note(None) is None


def test_a_firing_rule_pushes_its_note_and_its_route(client, monkeypatch):
    from app import alerts, db, apns
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:41", "name": "Chaucer"},
                      "timestamp_utc": "2026-09-11T20:00:00Z",
                      "outdoor": {"tempf": 70}, "rain": {"hourly_in": 0.12}})
    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "hourlyrainin", "comparator": "above",
                          "threshold": 0.1, "severity": "urgent", "note": "Park the 115H"})
    rid = r.json()["id"]
    pushes = []

    async def fake_configured():
        return True

    async def fake_send_to_all(title, body, interruption_level=None, route=None, **kw):
        pushes.append((title, body, interruption_level, route))
        return {"sent": 1, "total": 1}
    monkeypatch.setattr(apns, "push_configured", fake_configured)
    monkeypatch.setattr(apns, "send_to_all", fake_send_to_all)

    async def run():
        cfg = await alerts.effective_config()
        devs = await db.list_devices()
        await alerts.AlertMonitor()._check_threshold_rules(cfg, devs, 1789156800000)
    asyncio.run(run())
    assert len(pushes) == 1
    title, body, level, route = pushes[0]
    assert title == "Chaucer: Rain Rate alert"
    assert body.endswith("Park the 115H") and level == "time-sensitive"
    assert route == f"rule/{rid}"
    assert apns.valid_route(route) == route
    assert apns.valid_route("automower://park") is None


def test_the_note_survives_a_config_backup(client):
    client.post("/api/alerts/rules", headers=H,
                json={"field": "tempf", "comparator": "above", "threshold": 100,
                      "note": "Bring the dog in"})
    exported = client.get("/api/config/backup", headers=H).json()
    rules = exported["alert_rules"]
    assert any(r.get("note") == "Bring the dog in" for r in rules)
    r = client.post("/api/config/restore", headers=H,
                    json={**exported, "replace_rules": True})
    assert r.status_code == 200, r.text
    back = client.get("/api/alerts/rules", headers=H).json()
    assert [x["note"] for x in back if x["field"] == "tempf"] == ["Bring the dog in"]
