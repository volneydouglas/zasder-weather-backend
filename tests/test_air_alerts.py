"""Air-monitor threshold rules (2.2): CO2 and PM2.5 join the rule fields.

Doren, 2026-09-09: "Any plans to add a variable for air monitoring
alerts?" The pollers already store the columns; these pin that a rule
can be made on them, reads them, and stays quiet on a weather station.
"""
H = {"Authorization": "Bearer test-api-token"}

# Every test takes `client`, and the imports live inside them: Settings is
# built on first import and only the fixture blanks the credential env.


def test_every_rule_field_has_label_unit_and_deadband(client):
    from app.alerts import (AIR_FIELDS, THRESHOLD_FIELDS, _FIELD_LABELS,
                            _FIELD_UNITS, _REARM_MARGIN)
    for f in THRESHOLD_FIELDS:
        assert f in _FIELD_LABELS, f
        assert f in _FIELD_UNITS, f
        assert f in _REARM_MARGIN, f
    assert AIR_FIELDS <= THRESHOLD_FIELDS


def test_air_message_copy(client):
    from app.alerts import build_threshold_message
    title, body = build_threshold_message("Office Govee", "co2", 1250, "above", 1000)
    assert title == "Office Govee: CO2 alert"
    assert body == "CO2 is 1250 ppm (> 1000 ppm)"
    _, body = build_threshold_message("AirGradient", "pm25", 41.5, "above", 35)
    assert body == "PM2.5 is 41.5 µg/m³ (> 35 µg/m³)"


def test_air_deadbands_hold_a_drifting_sensor(client):
    from app.alerts import _REARM_MARGIN, rule_cleared
    # 1000 ppm rule: 990 is inside the 50 ppm deadband, 940 clears it.
    assert not rule_cleared("above", 1000, 990, _REARM_MARGIN["co2"])
    assert rule_cleared("above", 1000, 940, _REARM_MARGIN["co2"])
    assert not rule_cleared("above", 35, 33, _REARM_MARGIN["pm25"])
    assert rule_cleared("above", 35, 31, _REARM_MARGIN["pm25"])


def test_air_rules_are_accepted_by_the_api(client):
    for field, threshold in (("co2", 1000), ("pm25", 35)):
        r = client.post("/api/alerts/rules", headers=H,
                        json={"field": field, "comparator": "above", "threshold": threshold})
        assert r.status_code == 200, r.text
        assert r.json()["field"] == field
        assert client.delete(f"/api/alerts/rules/{r.json()['id']}", headers=H).status_code == 200


def test_air_rule_fires_on_the_monitor_and_skips_the_station(client, monkeypatch):
    """The check reads lastData by field name: a station with no co2 column
    is skipped (None), the monitor over the line fires once."""
    import asyncio
    from app import alerts, db
    from app.alerts import AlertMonitor, effective_config
    delivered = []

    async def fake_deliver(cfg, subject, text, title, body, **kw):
        delivered.append((kw.get("mac"), title, body))
        return True
    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    devices = [
        {"mac": "AA:11", "name": "Station", "lastData": {"tempf": 80.0}},
        {"mac": "5D:5D:07:00:00:01", "name": "Govee",
         "lastData": {"co2": 1180.0, "pm25": 4.0}},
    ]

    async def run():
        rule = await db.create_alert_rule(None, "co2", "above", 1000.0)
        try:
            cfg = await effective_config()
            mon = AlertMonitor()
            await mon._check_threshold_rules(cfg, devices, 1_700_000_000_000)
            first = list(delivered)
            # Still over the line next tick: edge-triggered, no repeat.
            await mon._check_threshold_rules(cfg, devices, 1_700_000_060_000)
            return first, len(delivered)
        finally:
            await db.delete_alert_rule(rule["id"])
    first, total = asyncio.run(run())
    assert first == [("5D:5D:07:00:00:01", "Govee: CO2 alert",
                      "CO2 is 1180 ppm (> 1000 ppm)")]
    assert total == 1
