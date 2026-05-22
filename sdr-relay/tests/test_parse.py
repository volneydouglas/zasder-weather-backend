"""Unit tests for the SDR relay's packet → backend-schema mapping.

No SDR or rtl_433 required — we feed sample rtl_433 JSON dicts (captured
from live runs) to the router and assert on the per-handler state. The
HTTP forwarder is monkey-patched so tests stay offline.
"""
from __future__ import annotations

import os
import sys

# Set required env BEFORE import so the module-level config doesn't bail.
os.environ.setdefault("BACKEND_URL", "http://test")
os.environ.setdefault("INGEST_TOKEN", "test-token")
os.environ.setdefault("ATLAS_ID", "711")
os.environ.setdefault("WH24_ID", "125")
os.environ.setdefault("WH32B_ID", "221")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sdr_relay  # noqa: E402


# ───────────────────────── helpers ─────────────────────────

class _Capture:
    """Stash the most recent POST args instead of calling the network."""
    def __init__(self):
        self.calls: list[dict] = []
    def __call__(self, url, payload):
        self.calls.append({"url": url, "payload": payload})
        return True


def _patch(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(sdr_relay, "_post", cap)
    return cap


def _reset_state():
    sdr_relay._atlas_state.clear()
    sdr_relay._wh32b_state.clear()
    sdr_relay._atlas_last_post = 0.0
    sdr_relay._rain_state.clear()
    sdr_relay._wind_window.clear()
    sdr_relay._discovery_state.clear()
    sdr_relay._discovery_last_posted.clear()
    sdr_relay._discovery_last_saved = 0.0


# ───────────────────────── conversions ─────────────────────────

def test_c_to_f():
    assert sdr_relay.c_to_f(0) == 32.0
    assert sdr_relay.c_to_f(100) == 212.0
    assert sdr_relay.c_to_f(None) is None

def test_ms_to_mph():
    assert sdr_relay.ms_to_mph(1) == 2.2  # 2.23694 rounded to 1 decimal
    assert sdr_relay.ms_to_mph(None) is None

def test_mm_to_in():
    assert sdr_relay.mm_to_in(25.4) == 1.0
    assert sdr_relay.mm_to_in(None) is None

def test_hpa_to_inhg():
    assert sdr_relay.hpa_to_inhg(1000) == 29.53
    assert sdr_relay.hpa_to_inhg(None) is None

def test_lux_to_wm2():
    assert sdr_relay.lux_to_wm2(126) == 1.0
    assert sdr_relay.lux_to_wm2(79550) == 631.3
    assert sdr_relay.lux_to_wm2(None) is None

def test_dew_point_f():
    # Sanity values for Chandler in May: ~90°F + 20% RH → mid-40s dew
    dp = sdr_relay.dew_point_f(90.0, 20.0)
    assert dp is not None and 40 < dp < 50, f"got {dp}"
    # Saturated air ⇒ dew point ≈ air temperature
    dp = sdr_relay.dew_point_f(70.0, 100.0)
    assert dp is not None and abs(dp - 70.0) < 0.5
    # Edge cases
    assert sdr_relay.dew_point_f(None, 50) is None
    assert sdr_relay.dew_point_f(70, None) is None
    assert sdr_relay.dew_point_f(70, 0) is None  # 0% RH is degenerate

def test_heat_index_f():
    # Below 80°F: returns air temp unchanged (per NWS convention)
    assert sdr_relay.heat_index_f(70.0, 50.0) == 70.0
    # 95°F + 60% RH ≈ 114°F NWS-published value
    hi = sdr_relay.heat_index_f(95.0, 60.0)
    assert hi is not None and 110 < hi < 117, f"got {hi}"
    # Low-humidity desert case (Chandler in May): Rothfusz formula
    # undershoots and can return < temp_f. We clamp to temp_f so we
    # never report "feels cooler than it is" — physically impossible.
    hi = sdr_relay.heat_index_f(91.8, 17.0)
    assert hi is not None and hi >= 91.8, f"low-humidity clamp failed: got {hi}"
    assert sdr_relay.heat_index_f(None, 50) is None


# ───────────────────────── MAC synthesis ─────────────────────────

def test_sensor_mac_atlas():
    # id=711 = 0x2C7 → low3=0x0002C7
    assert sdr_relay.sensor_mac(0x01, 711) == "5D:5D:01:00:02:C7"

def test_sensor_mac_wh24():
    assert sdr_relay.sensor_mac(0x02, 125) == "5D:5D:02:00:00:7D"

def test_sensor_mac_wh32b():
    assert sdr_relay.sensor_mac(0x03, 221) == "5D:5D:03:00:00:DD"


# ───────────────────────── Atlas coalescer ─────────────────────────

def test_atlas_state_merges_across_message_types(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    # Type 7 packet (wind+UV+lux only)
    sdr_relay.update_atlas({
        "model": "Acurite-Atlas", "id": 711, "message_type": 7,
        "wind_avg_mi_h": 7.0, "uv": 4, "lux": 79550, "battery_ok": 1,
    })
    assert sdr_relay._atlas_state["wind_avg_mph"] == 7.0
    assert sdr_relay._atlas_state["uv"] == 4
    assert sdr_relay._atlas_state["lux"] == 79550
    # No tempf yet — comes from a different message_type
    assert "tempf" not in sdr_relay._atlas_state
    # A later packet of type 3/4 carries temp + humidity; state should merge
    sdr_relay.update_atlas({
        "model": "Acurite-Atlas", "id": 711, "message_type": 3,
        "temperature_F": 92.4, "humidity": 19,
    })
    assert sdr_relay._atlas_state["tempf"] == 92.4
    assert sdr_relay._atlas_state["humidity"] == 19
    # Wind data from the earlier packet must still be there.
    assert sdr_relay._atlas_state["wind_avg_mph"] == 7.0


def test_atlas_ignores_other_sensor_ids():
    _reset_state()
    sdr_relay.update_atlas({"model": "Acurite-Atlas", "id": 999,
                            "temperature_F": 50.0})
    assert "tempf" not in sdr_relay._atlas_state


def test_atlas_flush_respects_interval(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    sdr_relay.update_atlas({
        "model": "Acurite-Atlas", "id": 711,
        "temperature_F": 92.4, "humidity": 19, "wind_avg_mi_h": 7.0,
        "uv": 4, "lux": 79550,
    })
    # First flush should fire (last_post = 0)
    sdr_relay.maybe_flush_atlas()
    assert len(cap.calls) == 1
    # Second flush immediately after must NOT fire
    sdr_relay.maybe_flush_atlas()
    assert len(cap.calls) == 1


def test_atlas_uv_lux_broken_masks_fields(monkeypatch):
    """When ATLAS_UV_LUX_BROKEN=1, the stuck uv/lux values are dropped
    from posted observations even when the sensor keeps broadcasting them."""
    _reset_state()
    cap = _patch(monkeypatch)
    monkeypatch.setattr(sdr_relay, "ATLAS_UV_LUX_BROKEN", True)
    sdr_relay.update_atlas({
        "model": "Acurite-Atlas", "id": 711,
        "temperature_F": 71.9, "humidity": 25,
        "wind_avg_mi_h": 3.0,
        "uv": 4, "lux": 79550,  # stuck-broken values
    })
    sdr_relay.maybe_flush_atlas()
    p = cap.calls[0]["payload"]
    # tempf etc. still posted
    assert p["outdoor"]["tempf"] == 71.9
    # uv and solar_wm2 dropped (not present in payload at all — None values
    # are filtered by post_observation)
    assert "uv" not in p["outdoor"]
    assert "solar_wm2" not in p["outdoor"]


def test_atlas_uv_lux_normal_when_not_broken(monkeypatch):
    """Without the flag, normal uv/lux pass through (regression check)."""
    _reset_state()
    cap = _patch(monkeypatch)
    monkeypatch.setattr(sdr_relay, "ATLAS_UV_LUX_BROKEN", False)
    sdr_relay.update_atlas({
        "model": "Acurite-Atlas", "id": 711,
        "temperature_F": 71.9, "humidity": 25,
        "wind_avg_mi_h": 3.0, "uv": 4, "lux": 79550,
    })
    sdr_relay.maybe_flush_atlas()
    p = cap.calls[0]["payload"]
    assert p["outdoor"]["uv"] == 4
    assert p["outdoor"]["solar_wm2"] == 631.3


def test_atlas_flush_payload_shape(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    sdr_relay.update_atlas({
        "model": "Acurite-Atlas", "id": 711,
        "temperature_F": 92.4, "humidity": 19, "wind_avg_mi_h": 7.0,
        "wind_dir_deg": 270, "uv": 4, "lux": 79550,
    })
    sdr_relay.maybe_flush_atlas()
    call = cap.calls[0]
    assert call["url"] == "/ingest/custom"
    p = call["payload"]
    assert p["device"]["id"] == "5D5D010002C7"  # MAC without colons (12 hex)
    assert p["source"] == "acurite-atlas-sdr"
    assert p["outdoor"]["tempf"] == 92.4
    assert p["outdoor"]["humidity"] == 19
    assert p["outdoor"]["uv"] == 4
    # 79550 lux → 631.3 W/m²
    assert p["outdoor"]["solar_wm2"] == 631.3
    assert p["wind"]["speed_mph"] == 7.0
    assert p["wind"]["direction"] == 270


# ───────────────────────── WH24 outdoor + WH32B merge ─────────────────────────

def test_wh24_posts_complete_observation(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    sdr_relay.handle_wh24({
        "model": "Fineoffset-WH24", "id": 125,
        "temperature_C": 31.2, "humidity": 20,
        "wind_avg_m_s": 1.82, "wind_max_m_s": 2.24, "wind_dir_deg": 108,
        "rain_mm": 502.8, "uvi": 0, "light_lux": 12562,
    })
    assert len(cap.calls) == 1
    p = cap.calls[0]["payload"]
    assert p["source"] == "fineoffset-wh24-sdr"
    assert p["device"]["id"] == "5D5D0200007D"  # 12 hex chars, no colons
    # Conversions
    assert p["outdoor"]["tempf"] == 88.2   # 31.2°C → 88.16 round 1
    assert p["outdoor"]["humidity"] == 20
    assert p["outdoor"]["solar_wm2"] == 99.7  # 12562 / 126 = 99.7
    assert p["wind"]["speed_mph"] == 4.1   # 1.82 m/s → 4.07 round 1
    assert p["wind"]["gust_mph"] == 5.0    # 2.24 m/s → 5.01 round 1
    assert p["wind"]["direction"] == 108
    # No baseline configured in this test → rain block is empty (yearly_in
    # is None, gets filtered out by post_observation).
    assert "rain" not in p
    # No indoor block — WH32B hasn't been seen
    assert "indoor" not in p


def test_wh24_merges_indoor_when_wh32b_present(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    sdr_relay.update_wh32b({
        "model": "Fineoffset-WH32B", "id": 221,
        "temperature_C": 22.3, "humidity": 44, "pressure_hPa": 964.1,
    })
    sdr_relay.handle_wh24({
        "model": "Fineoffset-WH24", "id": 125,
        "temperature_C": 31.2, "humidity": 20,
    })
    p = cap.calls[0]["payload"]
    assert "indoor" in p
    assert p["indoor"]["tempf"] == 72.1    # 22.3°C → 72.14 round 1
    assert p["indoor"]["humidity"] == 44
    assert p["indoor"]["pressure_inhg"] == 28.47  # 964.1 hPa × 0.02953 = 28.469 round 2


def test_wh24_ignores_other_sensor_ids(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    sdr_relay.handle_wh24({
        "model": "Fineoffset-WH24", "id": 999, "temperature_C": 20,
    })
    assert cap.calls == []


# ───────────────────────── LaCrosse-TH2 ─────────────────────────

def test_lacrosse_posts_temp_humidity_only(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    monkeypatch.setattr(sdr_relay, "LACROSSE_ID", 3404534)
    monkeypatch.setattr(sdr_relay, "LACROSSE_LOCATION", "north yard")
    sdr_relay.handle_lacrosse({
        "model": "LaCrosse-TH2", "id": 3404534,
        "temperature_C": 28.6, "humidity": 19,
    })
    assert len(cap.calls) == 1
    p = cap.calls[0]["payload"]
    assert p["source"] == "lacrosse-th2-sdr"
    assert p["device"]["location"] == "north yard"
    assert p["outdoor"]["tempf"] == 83.5  # 28.6°C → 83.48 round 1
    assert p["outdoor"]["humidity"] == 19
    # Dew point + feels-like computed
    assert "dew_point_f" in p["outdoor"]
    assert "feels_like" in p["outdoor"]
    # No wind, rain, pressure, indoor (LaCrosse-TH2 doesn't have them)
    assert "wind" not in p
    assert "rain" not in p


def test_lacrosse_ignores_other_sensor_ids(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    monkeypatch.setattr(sdr_relay, "LACROSSE_ID", 3404534)
    sdr_relay.handle_lacrosse({
        "model": "LaCrosse-TH2", "id": 999, "temperature_C": 20,
    })
    assert cap.calls == []


# ───────────────────────── rain accumulator ─────────────────────────

def test_rain_returns_none_without_baseline():
    _reset_state()
    # baseline_in=0 / None ⇒ skip yearly altogether
    assert sdr_relay.compute_yearly_rain("test", 0, 5.0) is None
    assert sdr_relay.compute_yearly_rain("test", None, 5.0) is None

def test_rain_returns_none_without_counter():
    _reset_state()
    assert sdr_relay.compute_yearly_rain("test", 0.73, None) is None

def test_rain_first_packet_matches_baseline_exactly(monkeypatch, tmp_path):
    """First packet for a sensor: yearly = baseline regardless of counter."""
    _reset_state()
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(tmp_path / "rain.json"))
    # Sensor counter is 19.795 in (lifetime), baseline = 0.73 (AWN today)
    yearly = sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)
    assert yearly == 0.73

def test_rain_subsequent_packets_track_deltas(monkeypatch, tmp_path):
    _reset_state()
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(tmp_path / "rain.json"))
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)  # baseline
    # No new rain
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795) == 0.73
    # 0.01 in of rain falls
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.805) == 0.74
    # Another 0.10 in
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.905) == 0.84

def test_rain_handles_sensor_reset(monkeypatch, tmp_path):
    """Battery change / power-cycle: counter drops to ~0 and stays there
    for many packets. After the debounce window (5 consecutive near-zero
    packets) we accept as a real reset and bank the prior counter so the
    yearly total stays continuous."""
    _reset_state()
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(tmp_path / "rain.json"))
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)  # baseline
    # Some rain accumulates
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 20.000) == 0.935
    # First 4 near-zero packets should be debounced — yearly stays at 0.935
    for _ in range(4):
        assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 0.0) == 0.935
    # 5th consecutive low packet trips the threshold ⇒ real reset
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 0.0) == 0.935
    # Light rain after reset — counter starts climbing again
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 0.05) == 0.985

def test_rain_ignores_spurious_low_packets(monkeypatch, tmp_path):
    """Fine Offset's known glitch: occasional single packet reports a
    much lower rain_mm than reality. Must not be banked as a reset
    — would inflate yearly by ~the lifetime counter on every blip."""
    _reset_state()
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(tmp_path / "rain.json"))
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)  # baseline
    # One spurious low packet (just one — below debounce threshold)
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 0.0) == 0.73
    # Counter returns to normal — should NOT trigger any banking
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795) == 0.73
    # Another single spurious below — still no banking
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 0.0) == 0.73
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795) == 0.73
    # Real rain accumulates normally
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.85) == 0.785

def test_rain_rejects_implausible_jump(monkeypatch, tmp_path):
    """Decoder glitch where rain_mm jumps to a huge value must be filtered
    out — without this check it would bake into yearly forever."""
    _reset_state()
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(tmp_path / "rain.json"))
    monkeypatch.setattr(sdr_relay, "MAX_RAIN_DELTA_IN", 5.0)
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)  # baseline
    # Plausible: 0.3 in of rain
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 20.095) == 1.03
    # Decoder glitch: jumps by 1000 in one packet → reject
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 1019.795) == 1.03
    # Counter returns to normal — accumulator picks up where it left off
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 20.100) == 1.035

def test_rain_threshold_zero_disables_check(monkeypatch, tmp_path):
    """Operator can disable the sanity-ceiling by setting MAX_RAIN_DELTA_IN=0."""
    _reset_state()
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(tmp_path / "rain.json"))
    monkeypatch.setattr(sdr_relay, "MAX_RAIN_DELTA_IN", 0.0)
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)
    # 1000-inch jump now accepted (because user opted out)
    result = sdr_relay.compute_yearly_rain("ws2000", 0.73, 1019.795)
    assert result == 1000.73  # = 1000 + baseline

def test_rain_ignores_mid_range_decrease(monkeypatch, tmp_path):
    """A mid-range drop (e.g. counter 19.8 → 15.0) can't be a sensor
    reset (would go to 0) so it's clearly a bad packet — never bank."""
    _reset_state()
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(tmp_path / "rain.json"))
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)
    # Drop to mid-range — always treated as spurious regardless of repetition
    for _ in range(10):
        assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 15.0) == 0.73
    # Real value returns
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.81) == 0.745

def test_rain_state_persists_across_calls(monkeypatch, tmp_path):
    """File round-trip — load state, write state, reload should match."""
    import json
    _reset_state()
    p = tmp_path / "rain.json"
    monkeypatch.setattr(sdr_relay, "RAIN_STATE_PATH", str(p))
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.795)
    sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.85)
    # File should exist + contain plausible state
    assert p.exists()
    data = json.loads(p.read_text())
    assert "ws2000" in data
    assert data["ws2000"]["baseline_in"] == 0.73
    assert data["ws2000"]["last_counter_in"] == 19.85
    # Simulating restart: clear in-memory, reload from disk, continue
    sdr_relay._rain_state.clear()
    sdr_relay._load_rain_state()
    assert "ws2000" in sdr_relay._rain_state
    # Picking up from reload, no new rain
    assert sdr_relay.compute_yearly_rain("ws2000", 0.73, 19.85) == 0.785


# ───────────────────────── router dispatch ─────────────────────────

# ───────────────────────── gust computation ─────────────────────────

def test_gust_empty_window_returns_none():
    _reset_state()
    assert sdr_relay.computed_gust_mph("atlas") is None

def test_gust_returns_max_of_window():
    _reset_state()
    now = __import__("time").time()
    sdr_relay._wind_window["atlas"] = sdr_relay.deque([
        (now - 100, 5.0), (now - 50, 12.5), (now - 10, 7.0),
    ])
    assert sdr_relay.computed_gust_mph("atlas") == 12.5

def test_gust_ignores_stale_samples(monkeypatch):
    """Samples older than WIND_GUST_WINDOW_S must be excluded from max."""
    _reset_state()
    monkeypatch.setattr(sdr_relay, "WIND_GUST_WINDOW_S", 600)
    now = __import__("time").time()
    sdr_relay._wind_window["atlas"] = sdr_relay.deque([
        (now - 1200, 99.0),   # 20 min old — pruned
        (now - 1000, 50.0),   # also old
        (now - 60, 7.0),
        (now - 30, 9.0),
    ])
    assert sdr_relay.computed_gust_mph("atlas") == 9.0

def test_gust_update_sample_appends_and_skips_none(monkeypatch):
    _reset_state()
    monkeypatch.setattr(sdr_relay, "WIND_GUST_WINDOW_S", 600)
    sdr_relay.update_wind_sample("atlas", 5.0)
    sdr_relay.update_wind_sample("atlas", 7.0)
    sdr_relay.update_wind_sample("atlas", None)  # no-op
    assert sdr_relay.computed_gust_mph("atlas") == 7.0


# ───────────────────────── discovery survey ─────────────────────────

def _enable_discovery_local_only(monkeypatch, tmp_path):
    """Common test setup: enable local discovery, isolate state path, and
    DISABLE cloud forwarding by default (matching the prod default)."""
    monkeypatch.setattr(sdr_relay, "DISCOVERY_ENABLED", True)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_FORWARD_TO_BACKEND", False)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_STATE_PATH",
                        str(tmp_path / "discoveries.json"))
    monkeypatch.setattr(sdr_relay, "DISCOVERY_SAVE_INTERVAL_S", 0)


def test_discovery_local_records_first_sighting(monkeypatch, tmp_path):
    _reset_state()
    cap = _patch(monkeypatch)
    _enable_discovery_local_only(monkeypatch, tmp_path)
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42, "temp": 70})
    # Local state populated
    state = sdr_relay._discovery_state
    assert "Stranger-XYZ:42" in state
    entry = state["Stranger-XYZ:42"]
    assert entry["model"] == "Stranger-XYZ"
    assert entry["id"] == "42"
    assert entry["seen_count"] == 1
    assert entry["sample"]["temp"] == 70
    # NO cloud post (forwarding disabled by default)
    assert cap.calls == []


def test_discovery_local_dedupes_and_increments(monkeypatch, tmp_path):
    _reset_state()
    cap = _patch(monkeypatch)
    _enable_discovery_local_only(monkeypatch, tmp_path)
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42, "temp": 70})
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42, "temp": 71})
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42, "temp": 72})
    entry = sdr_relay._discovery_state["Stranger-XYZ:42"]
    assert entry["seen_count"] == 3
    # sample is the FIRST packet captured — not overwritten
    assert entry["sample"]["temp"] == 70
    assert cap.calls == []


def test_discovery_treats_different_ids_separately(monkeypatch, tmp_path):
    _reset_state()
    cap = _patch(monkeypatch)
    _enable_discovery_local_only(monkeypatch, tmp_path)
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42})
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 43})
    sdr_relay.record_discovery({"model": "Other-Sensor", "id": 42})
    assert len(sdr_relay._discovery_state) == 3


def test_discovery_persists_to_file(monkeypatch, tmp_path):
    _reset_state()
    cap = _patch(monkeypatch)
    _enable_discovery_local_only(monkeypatch, tmp_path)
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42})
    p = tmp_path / "discoveries.json"
    assert p.exists()
    import json
    data = json.loads(p.read_text())
    assert "Stranger-XYZ:42" in data
    # Round-trip: clear in-memory, reload from disk
    sdr_relay._discovery_state.clear()
    sdr_relay._load_discovery_state()
    assert "Stranger-XYZ:42" in sdr_relay._discovery_state


def test_discovery_disabled_no_recording(monkeypatch, tmp_path):
    _reset_state()
    cap = _patch(monkeypatch)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_STATE_PATH",
                        str(tmp_path / "discoveries.json"))
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42})
    assert sdr_relay._discovery_state == {}
    assert cap.calls == []


def test_discovery_skips_packets_without_model(monkeypatch, tmp_path):
    _reset_state()
    cap = _patch(monkeypatch)
    _enable_discovery_local_only(monkeypatch, tmp_path)
    sdr_relay.record_discovery({"id": 42})            # no model
    sdr_relay.record_discovery({"model": "", "id": 42})  # empty model
    assert sdr_relay._discovery_state == {}
    assert cap.calls == []


def test_discovery_forward_to_backend_when_opted_in(monkeypatch, tmp_path):
    """Opt-in cloud forwarding posts each sighting (rate-limited) ON TOP
    of the always-on local recording."""
    _reset_state()
    cap = _patch(monkeypatch)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_ENABLED", True)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_FORWARD_TO_BACKEND", True)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_RATE_LIMIT_S", 60)
    monkeypatch.setattr(sdr_relay, "DISCOVERY_STATE_PATH",
                        str(tmp_path / "discoveries.json"))
    monkeypatch.setattr(sdr_relay, "DISCOVERY_SAVE_INTERVAL_S", 0)
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42})
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42})
    sdr_relay.record_discovery({"model": "Stranger-XYZ", "id": 42})
    # Local state shows all 3 sightings
    assert sdr_relay._discovery_state["Stranger-XYZ:42"]["seen_count"] == 3
    # But cloud was only hit once (rate-limited)
    assert len(cap.calls) == 1
    assert cap.calls[0]["url"] == "/ingest/discovery"


# ───────────────────────── router dispatch ─────────────────────────

def test_route_dispatches_by_model(monkeypatch):
    _reset_state()
    cap = _patch(monkeypatch)
    sdr_relay.route({"model": "Acurite-Atlas", "id": 711, "temperature_F": 90.0})
    sdr_relay.route({"model": "Fineoffset-WH24", "id": 125, "temperature_C": 30.0})
    sdr_relay.route({"model": "LaCrosse-TH2", "id": 1})  # neighbour noise, ignored
    sources = [c["payload"].get("source") or c["url"] for c in cap.calls]
    assert "acurite-atlas-sdr" in sources
    assert "fineoffset-wh24-sdr" in sources
    # LaCrosse-TH2 should NOT appear
    assert not any("lacrosse" in str(s).lower() for s in sources)
