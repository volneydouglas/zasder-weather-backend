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


def test_a_rule_firing_beside_the_frost_watch_is_offered_for_retirement(client):
    cold_id, _ = _seed(frost_nights=3)
    r = client.get("/api/alerts/rules/overlaps", headers=H)
    assert r.status_code == 200, r.text
    got = r.json()["overlaps"]
    assert [o["rule_id"] for o in got] == [cold_id]
    assert got[0]["watch_kind"] == "frost" and got[0]["together"] == 3
    assert got[0]["rule_fires"] == 4
    assert got[0]["watch_label"] == "Frost watch"


def test_one_coincidence_is_not_a_pattern(client):
    _seed(frost_nights=1)
    assert client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"] == []


def test_a_disabled_rule_is_not_offered(client):
    cold_id, _ = _seed(frost_nights=3)
    asyncio.run(db.update_alert_rule(cold_id, enabled=False))
    assert client.get("/api/alerts/rules/overlaps", headers=H).json()["overlaps"] == []


def test_overlaps_need_the_token(client):
    assert client.get("/api/alerts/rules/overlaps").status_code == 401
