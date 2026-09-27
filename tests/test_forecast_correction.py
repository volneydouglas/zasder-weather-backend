"""The backyard correction (2.5, C1): the scorecard's bias, turned around."""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import forecast_skill as fs  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}


def _lead(lead, hi, lo, enough=True, n=20):
    return {"lead_days": lead, "n": n, "enough": enough,
            "high": {"bias_f": hi}, "low": {"bias_f": lo}}


def test_the_offset_is_the_bias_turned_around():
    card = {"leads": [_lead(1, -3.2, -0.7), _lead(3, -3.7, 6.6)]}
    got = fs.corrections_from(card)
    # Lead 1: the model ran 3.2 cold on highs → add 3.2; its low bias is
    # under a degree, which is noise.
    assert got[0] == {"lead_days": 1, "n": 20, "high_offset_f": 3.2,
                      "low_offset_f": None}
    # Lead 3: Chandler's desert-low finding, +6.6 warm → subtract 6.6.
    assert got[1]["low_offset_f"] == -6.6


def test_a_lead_without_enough_days_learns_nothing():
    assert fs.corrections_from({"leads": [_lead(2, -5, 5, enough=False)]}) == []
    assert fs.corrections_from({"leads": [_lead(2, 0.4, -0.3)]}) == []


def test_the_route_is_empty_on_a_new_server(client):
    r = client.get("/api/devices/AA:00:00:00:00:01/forecast-correction", headers=H)
    assert r.status_code == 200 and r.json()["leads"] == []
    assert client.get("/api/devices/AA:00:00:00:00:01/forecast-correction").status_code == 401
