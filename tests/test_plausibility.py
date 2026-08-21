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


def test_wind_255_sentinel_is_rejected_but_world_record_survives():
    """0xFF (255) is WU's 'no reading' sentinel for wind, not a wind speed.

    Regression for the 2026-08-15 find: the ceiling was 260, so ~1,200 rows of
    255 mph imported from a WU archive became the all-time peak wind AND peak
    gust on a station whose real peak gust is 51. The ceiling must sit between
    the 253 mph world record and 255.
    """
    flat = {"windspeedmph": 255.0, "windgustmph": 255.0, "maxdailygust": 255.0}
    dropped = ingest._apply_plausibility_bands(flat)
    assert flat["windspeedmph"] is None
    assert flat["windgustmph"] is None
    assert flat["maxdailygust"] is None
    assert sorted(d.split("=")[0] for d in dropped) == [
        "maxdailygust", "windgustmph", "windspeedmph"]

    # 253 mph (Barrow Island, 1996) is the strongest gust ever measured. The
    # band exists to catch decode garbage and must never clip a real reading.
    real = {"windspeedmph": 253.0, "windgustmph": 253.0, "maxdailygust": 254.0}
    assert ingest._apply_plausibility_bands(real) == []
    assert real["windgustmph"] == 253.0
    assert real["maxdailygust"] == 254.0


def test_banded_wind_condemns_its_sibling_speed_channels():
    """A banded wind value means the anemometer faulted — its siblings go too.

    Nulling only the out-of-band field leaves an orphan the bands can never
    catch later, because the orphan is physically in-range. That is exactly
    what happened on 2026-08-15: 255 mph gusts were nulled and 89.7-213.3 mph
    'sustained' winds were left behind on the same rows.
    """
    flat = {"windgustmph": 255.0, "windspeedmph": 213.3, "maxdailygust": 180.0,
            "tempf": 41.0, "winddir": 270.0}
    dropped = ingest._apply_plausibility_bands(flat)
    assert flat["windgustmph"] is None
    assert flat["windspeedmph"] is None, "orphaned sustained wind must go too"
    assert flat["maxdailygust"] is None
    # Non-anemometer fields are untouched — this is not a dropped reading, and
    # winddir is a separate vane channel.
    assert flat["tempf"] == 41.0
    assert flat["winddir"] == 270.0
    assert any("anemometer" in d for d in dropped)


def test_good_wind_is_never_condemned():
    """The companion rule must not fire when nothing was out of band."""
    flat = {"windgustmph": 51.0, "windspeedmph": 22.0, "maxdailygust": 51.0,
            "tempf": 41.0}
    assert ingest._apply_plausibility_bands(flat) == []
    assert flat["windgustmph"] == 51.0
    assert flat["windspeedmph"] == 22.0
    assert flat["maxdailygust"] == 51.0


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


# ─────────────────── internal consistency: sustained vs gust ───────────────────

def test_sustained_above_gust_condemns_the_anemometer():
    """Doren, 2026-08-16: "it's impossible to have a steady wind be more
    powerful than a gust, right?" — right, and his Records proved the app
    could show it anyway (Peak Wind 55 over Peak Gust 51).

    The gust is the peak inside the window the sustained average is taken
    over, so sustained > gust means the sensor contradicted itself and the
    whole speed set goes. The bands cannot catch this: both numbers here are
    perfectly ordinary Pennsylvania weather taken one at a time.
    """
    flat = {"windspeedmph": 55.2, "windgustmph": 40.0, "maxdailygust": 44.0,
            "winddir": 336.0, "tempf": 48.0}
    dropped = ingest._apply_plausibility_bands(flat)
    assert flat["windspeedmph"] is None
    assert flat["windgustmph"] is None
    assert flat["maxdailygust"] is None
    # The vane is a separate channel and the rest of the reading is real.
    assert flat["winddir"] == 336.0
    assert flat["tempf"] == 48.0
    assert sorted(d.split("=")[0] for d in dropped) == [
        "maxdailygust", "windgustmph", "windspeedmph"]
    assert all("wind-inconsistent" in d for d in dropped)


def test_gust_reporting_window_slack_survives():
    """Live stations report sustained and gust over DIFFERENT windows, so a
    small overshoot is normal and must not cost the reading its wind. Sized
    against Doren's archive, where 863 of 1.09 M readings sit above their gust
    and every one of them is a harmless artifact of WU's 5-minute buckets."""
    for speed, gust in ((12.0, 11.0),      # 1 mph over: rounding
                        (7.0, 6.0),        # ratio trips, absolute gap does not
                        (25.0, 22.0),      # 3 mph over at a real windy moment
                        (0.0, 0.0)):       # calm
        flat = {"windspeedmph": speed, "windgustmph": gust}
        ingest._apply_plausibility_bands(flat)
        assert flat["windspeedmph"] == speed, (speed, gust)
        assert flat["windgustmph"] == gust, (speed, gust)


def test_wind_consistency_needs_both_thresholds():
    """Either threshold alone is a false-positive machine. A big ratio at low
    wind (2.0 → 6.0) is noise; a big absolute gap at a proportionate ratio
    (40 → 46) is an ordinary squall. Only both together condemn."""
    ratio_only = {"windspeedmph": 6.0, "windgustmph": 2.0}   # 3x, but +4 mph
    ingest._apply_plausibility_bands(ratio_only)
    assert ratio_only["windspeedmph"] == 6.0

    gap_only = {"windspeedmph": 46.0, "windgustmph": 40.0}   # +6 mph, 1.15x
    ingest._apply_plausibility_bands(gap_only)
    assert gap_only["windspeedmph"] == 46.0

    both = {"windspeedmph": 46.0, "windgustmph": 30.0}       # 1.53x and +16
    ingest._apply_plausibility_bands(both)
    assert both["windspeedmph"] is None and both["windgustmph"] is None


def test_wind_consistency_ignores_missing_and_nonfinite():
    """A reading with no gust is normal (WU often omits it) and must not be
    condemned — only a gust that is PRESENT and contradicted counts."""
    orphan = {"windspeedmph": 20.0, "windgustmph": None}
    ingest._apply_plausibility_bands(orphan)
    assert orphan["windspeedmph"] == 20.0

    nan = {"windspeedmph": float("nan"), "windgustmph": 4.0}
    ingest._apply_plausibility_bands(nan)
    assert nan["windgustmph"] == 4.0


# ─────────────── level-shift confirmation vs colliding neighbors ───────────────
# Regression family for 2026-08-19 (Crestview): the guard runs on every ~16s
# relay POST while storage is throttled to 60s, and the relay serves rain from
# a per-field cache — so a colliding neighbor that monopolized the cache for
# ~2 minutes primed a pending rejection on a throttled post and "confirmed" it
# 90s later, storing the neighbor's 20.27 yearly counter on a station at
# 17.16. Twice in one day.


def test_rain_collision_monopoly_under_5min_cannot_confirm(client):
    """A repeat sighting 2 minutes after the rejection — enough under the old
    90s gap to self-confirm — must stay nulled: a collision episode has to
    hold the cache for a full 5 minutes before it can rebaseline anything."""
    ingest._rain_reject.clear()
    ingest._rain_tombstone.clear()
    assert _post(client, _ts(10), rain={"daily_in": 0.02}).status_code == 200
    assert _post(client, _ts(8), rain={"daily_in": 3.20}).status_code == 200
    assert _post(client, _ts(6), rain={"daily_in": 3.20}).status_code == 200
    dailies = [r.get("dailyrainin") for r in _rows(client)]
    assert dailies[-3:] == [0.02, None, None], dailies


def test_rain_real_level_shift_confirms_after_the_full_gap(client):
    """The rebaseline path must survive the longer gap: a persistent new
    level (counter swap, offset removal) still confirms once it has held for
    5 minutes — the anti-lockout contract the pending map exists for."""
    ingest._rain_reject.clear()
    ingest._rain_tombstone.clear()
    assert _post(client, _ts(20), rain={"daily_in": 0.00}).status_code == 200
    assert _post(client, _ts(10), rain={"daily_in": 2.00}).status_code == 200
    # 6 minutes after the rejection, still at the new level: accepted.
    assert _post(client, _ts(4), rain={"daily_in": 2.02}).status_code == 200
    dailies = [r.get("dailyrainin") for r in _rows(client)]
    assert dailies[-3:] == [0.00, None, 2.02], dailies


def test_fallen_back_level_is_tombstoned_and_never_confirms(client):
    """Once a pending level FALLS BACK to the old baseline it has proven
    itself a glitch — a real level shift never reverts — so the same value
    must not confirm a later episode however long that episode lasts. This
    is the recurring-neighbor killer: their counter barely moves, so every
    episode replays the same number."""
    ingest._rain_reject.clear()
    ingest._rain_tombstone.clear()
    assert _post(client, _ts(60), rain={"daily_in": 0.02}).status_code == 200
    assert _post(client, _ts(50), rain={"daily_in": 3.20}).status_code == 200
    # Back at the station's own level: the 3.20 candidate fell back.
    assert _post(client, _ts(40), rain={"daily_in": 0.02}).status_code == 200
    # Second episode, same neighbor value, corroborated 20 minutes apart —
    # far past the confirmation gap. Without the tombstone this rebaselines.
    assert _post(client, _ts(30), rain={"daily_in": 3.20}).status_code == 200
    assert _post(client, _ts(10), rain={"daily_in": 3.20}).status_code == 200
    dailies = [r.get("dailyrainin") for r in _rows(client)]
    assert dailies[-5:] == [0.02, None, 0.02, None, None], dailies


def test_lightning_bands_null_garbage_keep_real_storm():
    """Decode garbage in the lightning fields must be nulled before it lands
    in records — but a violent real storm (the live 2026-08-19 cell peaked at
    ~1,200 strikes/hr, nearest 0.6 mi) sails through untouched."""
    garbage = {"lightningcount": -3, "lightning_last_1hr": 65535.0,
               "lightning_distance_mi": 400.0}
    dropped = ingest._apply_plausibility_bands(garbage)
    assert garbage["lightningcount"] is None
    assert garbage["lightning_last_1hr"] is None
    assert garbage["lightning_distance_mi"] is None
    assert sorted(d.split("=")[0] for d in dropped) == [
        "lightning_distance_mi", "lightning_last_1hr", "lightningcount"]
    real = {"lightningcount": 23, "lightning_last_1hr": 1208,
            "lightning_distance_mi": 0.6}
    assert ingest._apply_plausibility_bands(real) == []
    assert real["lightning_last_1hr"] == 1208


def test_tombstone_expires_even_while_the_level_keeps_posting(client, monkeypatch):
    """Review 2026-08-20: refreshing the tombstone on every same-level
    sighting made a FALSE tombstone immortal — one stale old-baseline
    reading during a genuine level shift, and every real reading thereafter
    sits at the tombstoned level, re-arming it forever (months of nulled
    rain). The TTL must run from CREATION: after it lapses, the persistent
    new level confirms normally."""
    ingest._rain_reject.clear()
    ingest._rain_tombstone.clear()
    monkeypatch.setattr(ingest, "_RAIN_TOMBSTONE_TTL_MS", 600_000)  # 10 min
    assert _post(client, _ts(40), rain={"daily_in": 0.02}).status_code == 200
    assert _post(client, _ts(30), rain={"daily_in": 2.00}).status_code == 200  # pending
    # One stale-cache reading at the old baseline: tombstones 2.00.
    assert _post(client, _ts(28), rain={"daily_in": 0.02}).status_code == 200
    # The REAL new level keeps posting.
    assert _post(client, _ts(26), rain={"daily_in": 2.00}).status_code == 200
    assert _post(client, _ts(20), rain={"daily_in": 2.00}).status_code == 200
    # 12 minutes after the tombstone was created (> the 10-min TTL): the
    # shadow has lapsed and the level confirms. With refresh-on-sighting the
    # 20-minutes-ago post re-armed it and this stays nulled forever.
    assert _post(client, _ts(16), rain={"daily_in": 2.00}).status_code == 200
    dailies = [r.get("dailyrainin") for r in _rows(client)]
    assert dailies[-1] == 2.00, dailies
