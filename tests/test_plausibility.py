"""Ingest plausibility bands + station-relative spike guards (1.5 records QC).

The bands null physically impossible values (decode garbage) field-by-field;
the daily-rain and temperature guards catch in-band values that jump
impossibly fast from the device's own last reading — the "3.58-inch day"
class that reached records untouched by the yearly-rain guard.
"""

import datetime as dt
import os

os.environ.setdefault("API_TOKEN", "test-api-token")

from app import ingest  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token",
      "Content-Type": "application/json"}
MAC = "AA:BB:CC:DD:0B:20"


def _ts(minutes_ago: float) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _post(client, ts, outdoor=None, rain=None):
    body = {"device": {"id": "AABBCCDD0B20"}, "timestamp_utc": ts,
            "source": "t"}
    if outdoor is not None:
        body["outdoor"] = outdoor
    if rain is not None:
        body["rain"] = rain
    return client.post("/ingest/custom", headers=IH, json=body)


def _rows(client):
    return client.get(f"/api/devices/{MAC}/history?hours=24",
                      headers=H).json()["rows"]


# ───────────────────────── pure: absolute bands ─────────────────────────

def test_bands_null_garbage_keep_good():
    flat = {"tempf": 3276.7, "humidity": 41.0, "windgustmph": 3000.0,
            "dailyrainin": -0.4, "winddir": 720.0, "baromrelin": 29.92,
            "uv": 5.0}
    dropped = ingest._apply_plausibility_bands(flat)
    assert flat["tempf"] is None
    assert flat["windgustmph"] is None
    assert flat["dailyrainin"] is None
    assert flat["winddir"] is None
    assert flat["humidity"] == 41.0
    assert flat["baromrelin"] == 29.92
    assert flat["uv"] == 5.0
    assert sorted(d.split("=")[0] for d in dropped) == [
        "dailyrainin", "tempf", "winddir", "windgustmph"]


def test_bands_boundaries_and_nones_survive():
    # Exactly-on-the-line values are legitimate; None and non-numerics pass.
    flat = {"tempf": -90.0, "humidity": 100.0, "winddir": 360.0,
            "solarradiation": 0.0, "dewPoint": None, "uv": "junk"}
    assert ingest._apply_plausibility_bands(flat) == []
    assert flat["tempf"] == -90.0
    assert flat["humidity"] == 100.0
    assert flat["winddir"] == 360.0
    assert flat["uv"] == "junk"   # non-numeric is _coerce_num's job, not ours


def test_confirms_temp_level_semantics():
    key = "XX|tempf"
    ingest._rain_reject.clear()
    ingest._record_rain_rejection(key, 125.0, 1_000_000.0)
    # Same timestamp (a retry) never confirms.
    assert not ingest._confirms_temp_level(key, 125.0, 1_000_000.0)
    # Inside the burst-dedup gap never confirms.
    assert not ingest._confirms_temp_level(key, 125.0, 1_000_000.0 + 10_000)
    # Distinct later sighting within tolerance confirms.
    assert ingest._confirms_temp_level(key, 126.0, 1_000_000.0 + 120_000)
    # Far from the rejected level does not.
    assert not ingest._confirms_temp_level(key, 80.0, 1_000_000.0 + 120_000)
    ingest._rain_reject.clear()


# ───────────────────── integration: /ingest/custom ─────────────────────

def test_garbage_field_nulled_but_reading_kept(client):
    assert _post(client, _ts(5), outdoor={"tempf": 3276.7,
                                          "humidity": 44}).status_code == 200
    row = _rows(client)[-1]
    assert row["tempf"] is None
    assert row["humidity"] == 44


def test_daily_rain_spike_dropped_one_shot(client):
    ingest._rain_reject.clear()
    assert _post(client, _ts(30), outdoor={"tempf": 70},
                 rain={"daily_in": 0.02}).status_code == 200
    # +3.56" in 10 minutes — the glitch class this guard exists for.
    assert _post(client, _ts(20), outdoor={"tempf": 70},
                 rain={"daily_in": 3.58}).status_code == 200
    assert _post(client, _ts(10), outdoor={"tempf": 70},
                 rain={"daily_in": 0.03}).status_code == 200
    dailies = [r.get("dailyrainin") for r in _rows(client)]
    assert dailies[-3:] == [0.02, None, 0.03], dailies


def test_daily_rain_cloudburst_confirms_and_rebaselines(client):
    ingest._rain_reject.clear()
    assert _post(client, _ts(40), outdoor={"tempf": 70},
                 rain={"daily_in": 0.00}).status_code == 200
    # Real cloudburst: first sighting is indistinguishable from a glitch.
    assert _post(client, _ts(20), outdoor={"tempf": 70},
                 rain={"daily_in": 2.00}).status_code == 200
    # Second consecutive sighting at the new level: accepted.
    assert _post(client, _ts(10), outdoor={"tempf": 70},
                 rain={"daily_in": 2.05}).status_code == 200
    dailies = [r.get("dailyrainin") for r in _rows(client)]
    assert dailies[-3:] == [0.00, None, 2.05], dailies


def test_daily_rain_midnight_reset_is_not_a_glitch(client):
    ingest._rain_reject.clear()
    assert _post(client, _ts(30), outdoor={"tempf": 70},
                 rain={"daily_in": 1.20}).status_code == 200
    # Counter reset to (nearly) zero — negative jump, always allowed.
    assert _post(client, _ts(10), outdoor={"tempf": 70},
                 rain={"daily_in": 0.01}).status_code == 200
    dailies = [r.get("dailyrainin") for r in _rows(client)]
    assert dailies[-2:] == [1.20, 0.01], dailies


def test_temp_spike_dropped_one_shot(client):
    ingest._rain_reject.clear()
    assert _post(client, _ts(10), outdoor={"tempf": 70.0,
                                           "humidity": 40}).status_code == 200
    # +55 °F in 5 minutes: in-band (so the absolute limits don't see it) but
    # impossible weather. Humidity from the same reading survives.
    assert _post(client, _ts(5), outdoor={"tempf": 125.0,
                                          "humidity": 41}).status_code == 200
    assert _post(client, _ts(1), outdoor={"tempf": 70.4,
                                          "humidity": 42}).status_code == 200
    rows = _rows(client)
    temps = [r.get("tempf") for r in rows]
    assert temps[-3:] == [70.0, None, 70.4], temps
    assert [r.get("humidity") for r in rows][-3:] == [40, 41, 42]


def test_temp_level_shift_confirms_on_second_sighting(client):
    ingest._rain_reject.clear()
    assert _post(client, _ts(10), outdoor={"tempf": 70.0}).status_code == 200
    # Swapped/colliding sensor at a persistently different level.
    assert _post(client, _ts(5), outdoor={"tempf": 125.0}).status_code == 200
    assert _post(client, _ts(1), outdoor={"tempf": 125.3}).status_code == 200
    temps = [r.get("tempf") for r in _rows(client)]
    assert temps[-3:] == [70.0, None, 125.3], temps


def test_temp_slow_change_and_offline_gap_allowed(client):
    ingest._rain_reject.clear()
    # 30 °F over 8 hours (a frontal passage; also an offline gap) accrues
    # allowance and must never trip the guard.
    assert _post(client, _ts(8 * 60), outdoor={"tempf": 95.0}).status_code == 200
    assert _post(client, _ts(1), outdoor={"tempf": 65.0}).status_code == 200
    temps = [r.get("tempf") for r in _rows(client)]
    assert temps[-2:] == [95.0, 65.0], temps


def test_high_elevation_absolute_pressure_survives_bands(client, monkeypatch):
    """Ordering regression guard (CodeRabbit): a configured absolute-pressure
    station above ~6,000 ft legitimately reads under the 24 inHg sea-level
    band. The elevation correction must run BEFORE the bands, so the
    corrected sea-level value is what gets judged — banding first nulled the
    reading and the correction never saw it."""
    from app import config
    monkeypatch.setattr(config.settings, "station_elevation_ft", 7000.0)
    monkeypatch.setattr(config.settings, "pressure_absolute_macs", MAC)
    body = {"device": {"id": "AABBCCDD0B20"}, "timestamp_utc": _ts(5),
            "pressure": {"relative_inhg": 23.10}, "source": "t"}
    assert client.post("/ingest/custom", headers=IH, json=body).status_code == 200
    # 23.10 inHg absolute at 7,000 ft ≈ 29.94 inHg sea-level. /current
    # carries baromabsin too (history rows don't).
    cur = client.get(f"/api/devices/{MAC}/current", headers=H).json()
    assert cur["baromrelin"] is not None, "band nulled the reading before correction"
    assert 29.5 < cur["baromrelin"] < 30.4
    assert cur["baromabsin"] == 23.10   # true absolute kept (15 inHg floor)


def test_temp_level_shift_confirms_with_jittery_subgap_cadence(client):
    """R4-02: a swapped sensor posting faster than the 90 s confirm gap AND
    jittering more than the rain guard's 0.05 same-level tolerance must still
    rebaseline. Pre-fix, every jittery reading re-recorded the pending
    rejection with a fresh timestamp, so the 90 s window never elapsed and
    the new level was nulled forever."""
    ingest._rain_reject.clear()
    assert _post(client, _ts(5.0), outdoor={"tempf": 70.0}).status_code == 200
    # New persistent level, posts ~18-36 s apart, jitter 0.1-0.3 °F.
    assert _post(client, _ts(4.0), outdoor={"tempf": 125.0}).status_code == 200
    assert _post(client, _ts(3.7), outdoor={"tempf": 125.2}).status_code == 200
    assert _post(client, _ts(3.4), outdoor={"tempf": 124.9}).status_code == 200
    # 108 s after the FIRST rejection (72 s after the last): must confirm.
    assert _post(client, _ts(2.2), outdoor={"tempf": 125.1}).status_code == 200
    # The 60 s write-throttle may drop some of the sub-minute rows, so pin
    # the shape, not the exact count: baseline stored, rejected readings
    # nulled, and the final new-level reading STORED (pre-fix: also null).
    temps = [r.get("tempf") for r in _rows(client)]
    assert temps[0] == 70.0 and None in temps and temps[-1] == 125.1, temps


def test_daily_rain_out_of_order_across_midnight_not_glitch(client):
    """R4-31: a delayed pre-midnight packet arriving after the post-reset row
    must not be judged against the NEWER baseline (jump +2.28 over ~-9 min
    read as a spike) — out-of-order readings skip the spike guards; the
    plausibility bands still apply."""
    ingest._rain_reject.clear()
    assert _post(client, _ts(1.0), rain={"daily_in": 0.02}).status_code == 200
    # Delayed packet with an OLDER device timestamp and yesterday's total.
    assert _post(client, _ts(10.0), rain={"daily_in": 2.3}).status_code == 200
    rains = [r.get("dailyrainin") for r in _rows(client)]
    assert rains[-2:] == [2.3, 0.02], rains
