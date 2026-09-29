"""Rules that duplicate a built-in watch (2.5, C5).

Doren has 28 rules, written before the smart watches existed; a cold
night pushes him twice. The history decides, not a guess.
"""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import db, rule_overlap  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "5D:5D:05:00:00:01"
HOUR = 3_600_000


def test_which_rules_look_like_which_watch():
    k = rule_overlap.overlapping_kinds
    assert "frost" in k("tempf", "below", 33)
    assert k("tempf", "below", 70) == set(), "a comfort rule is not a frost rule"
    assert k("tempf", "above", 105) == {"heat"}
    assert k("windgustmph", "above", 40) == {"wind_ramp"}
    assert k("humidity", "above", 90) == set()


def _seed(frost_nights: int, other_rule: bool = True):
    from app.alerts import build_threshold_message

    async def go():
        now = int(time.time() * 1000)
        cold = await db.create_alert_rule(None, "tempf", "below", 33.0)
        comfy = await db.create_alert_rule(None, "tempf", "below", 70.0) if other_rule else None
        for n in range(frost_nights):
            t = now - (n + 1) * 86_400_000
            title, body = build_threshold_message("Chaucer Drive", "tempf", 31.5,
                                                  "below", 33.0)
            await db.log_alert(t, "rule", MAC, title, body, True)
            await db.log_alert(t + 20 * 60_000, "frost", MAC, "Chaucer Drive: Frost",
                               "Frost is possible.", True)
        # A rule row far from any watch never counts.
        title, body = build_threshold_message("Chaucer Drive", "tempf", 30, "below", 33.0)
        await db.log_alert(now - 40 * 86_400_000, "rule", MAC, title, body, True)
        return cold["id"], comfy["id"] if comfy else None
    return asyncio.run(go())


def _smart(client, on: bool):
    r = client.put("/api/alerts", headers=H, json={"smart_alerts": on})
    assert r.status_code == 200, r.text


def test_a_rule_firing_beside_the_frost_watch_is_offered_for_retirement(client):
    _smart(client, True)
    cold_id, _ = _seed(frost_nights=3)
    r = client.get("/api/alerts/rules/overlaps", headers=H)
    assert r.status_code == 200, r.text
    got = r.json()["overlaps"]
    assert [o["rule_id"] for o in got] == [cold_id]
    assert got[0]["watch_kind"] == "frost" and got[0]["together"] == 3
    assert got[0]["rule_fires"] == 4
    assert got[0]["watch_label"] == "Frost watch"


def test_one_coincidence_is_not_a_pattern(client):
    _smart(client, True)   # so the rule, not the switch, decides
    _seed(frost_nights=1)
    assert client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"] == []


def test_a_disabled_rule_is_not_offered(client):
    _smart(client, True)   # so the rule, not the switch, decides
    cold_id, _ = _seed(frost_nights=3)
    asyncio.run(db.update_alert_rule(cold_id, enabled=False))
    assert client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"] == []


def test_overlaps_need_the_token(client):
    assert client.get("/api/alerts/rules/overlaps").status_code == 401


def test_a_rule_is_never_offered_for_retirement_to_a_watch_that_is_off(client):
    """R25-A04 (the 2.5 additional review): with smart alerts off, the
    frost rule was still offered for retirement in favour of the frost
    watch, and one tap left the owner with no frost alert at all."""
    _smart(client, False)
    _seed(frost_nights=3)
    assert client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"] == []
    _smart(client, True)
    assert client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"] != []


def test_active_kinds_follow_the_switches():
    class Cfg:
        smart_alerts = False
        rain_start = True
        storm_summary = False
    assert rule_overlap.active_kinds(Cfg()) == {"rain_start"}
    Cfg.smart_alerts = True
    assert "frost" in rule_overlap.active_kinds(Cfg()) and "storm" not in rule_overlap.active_kinds(Cfg())


def test_a_station_with_its_storm_summary_muted_offers_no_storm_retirement(client):
    """Greptile, PR #52: the active-watch filter checked the server-wide
    storm switch but not a station's own storm mute, which the storm tick
    honours; retiring that station's rain rule would leave it with no rain
    alert at all."""
    from app.alerts import build_threshold_message
    client.put("/api/alerts", headers=H, json={"storm_summary": True, "rain_start": False})

    async def seed():
        now = int(time.time() * 1000)
        rule = await db.create_alert_rule(None, "hourlyrainin", "above", 0.5)
        for n in range(3):
            t = now - (n + 1) * 86_400_000
            title, body = build_threshold_message("Chaucer Drive", "hourlyrainin", 0.8,
                                                  "above", 0.5)
            await db.log_alert(t, "rule", MAC, title, body, True)
            await db.log_alert(t + 30 * 60_000, "storm", MAC, "Storm", "0.8 in", True)
        return rule["id"]
    rid = asyncio.run(seed())
    got = client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"]
    assert [o["rule_id"] for o in got] == [rid]
    asyncio.run(db.set_device_storm_summary(MAC, False))
    assert client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"] == []
