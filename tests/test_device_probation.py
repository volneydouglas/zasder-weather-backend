"""The phantom-device guard.

Reproduces the 2026-08-15 incident: one 433 MHz packet from AcuRite Atlas
#711 decoded with the high byte of its station ID lost, so the relay minted
`5D:5D:01:00:00:C7` beside the real `5D:5D:01:00:02:C7`. That row went stale
and emailed a device-down alert about a station that never existed.
"""
from __future__ import annotations

import asyncio

import pytest

from app import device_probation as dp

REAL = "5D:5D:01:00:02:C7"      # AcuRite Atlas #711
PHANTOM = "5D:5D:01:00:00:C7"   # the same ID with its high byte lost


class TestSuspectDetection:
    def test_the_real_incident_is_one_bit_away(self):
        assert dp.bit_distance(PHANTOM, REAL) == 1

    def test_phantom_is_flagged_against_the_real_atlas(self):
        assert dp.suspect_of(PHANTOM, [REAL, "5D:5D:02:00:00:7D"], 2) == REAL

    def test_a_different_sensor_type_is_never_a_suspect(self):
        # Same ID, different type tag (01 Atlas vs 09 water meter). A device
        # of another kind is not a corruption of this one.
        assert dp.suspect_of("5D:5D:09:00:02:C7", [REAL], 2) is None

    def test_an_unrelated_new_station_registers_immediately(self):
        assert dp.suspect_of("5D:5D:01:00:9F:31", [REAL], 2) is None

    def test_the_closest_neighbour_is_reported(self):
        near = "5D:5D:01:00:02:C6"   # 1 bit
        far = "5D:5D:01:00:02:CF"    # 2 bits
        assert dp.suspect_of(PHANTOM, [far, REAL, near], 3) == REAL

    def test_a_malformed_mac_is_not_a_suspect(self):
        assert dp.suspect_of("not-a-mac", [REAL], 2) is None
        assert dp.bit_distance("zz:zz:zz:zz:zz:zz", REAL) is None

    def test_max_bits_zero_disables_detection(self):
        assert dp.suspect_of(PHANTOM, [REAL], 0) is None


class TestProbation:
    def _decide(self, hits, prior_ms, now_ms, needed=5, gap=45_000):
        return dp.decide(prior_hits=hits, prior_ms=prior_ms, now_ms=now_ms,
                         suspect=REAL, needed=needed, min_gap_ms=gap)

    def test_a_single_corrupt_packet_is_quarantined(self):
        v = self._decide(0, None, 1_000_000)
        assert v.quarantined and v.hits == 1

    def test_a_real_station_clears_probation(self):
        hits, prior, t = 0, None, 1_000_000
        for _ in range(5):
            v = self._decide(hits, prior, t)
            hits, prior, t = v.hits, t, t + 60_000
        assert v.admit and v.hits == 5

    def test_a_burst_of_duplicates_does_not_advance_the_counter(self):
        """Receivers emit the same frame several times within seconds. If raw
        sightings counted, one burst would clear the bar on its own — which
        is the exact thing being guarded against."""
        v = self._decide(1, 1_000_000, 1_000_000 + 5_000)
        assert not v.counted and v.hits == 1 and v.quarantined

    def test_no_suspect_means_immediate_admission(self):
        v = dp.decide(prior_hits=0, prior_ms=None, now_ms=1, suspect=None,
                      needed=5, min_gap_ms=45_000)
        assert v.admit

    def test_needed_zero_disables_the_gate(self):
        v = dp.decide(prior_hits=0, prior_ms=None, now_ms=1, suspect=REAL,
                      needed=0, min_gap_ms=45_000)
        assert v.admit


# --- End to end through the real ingest path ----------------------------
#
# The unit tests above prove the logic. These prove it is actually WIRED —
# a guard that never fires is worse than no guard, because it reads as
# protection.

IH = {"Authorization": "Bearer test-ingest-token"}
H = {"Authorization": "Bearer test-api-token"}


def _post(client, mac, tempf=102.6, ts="2026-08-15T17:08:58Z"):
    return client.post("/ingest/custom", headers=IH, json={
        "device": {"id": mac.replace(":", "")},
        "timestamp_utc": ts,
        "outdoor": {"tempf": tempf, "humidity": 27},
        "wind": {}, "rain": {}, "pressure": {},
        "source": "acurite-atlas-lilygo",
    })


def _macs(client):
    return {d["mac"] for d in client.get("/api/devices", headers=H).json()}


def test_a_genuinely_new_station_registers_immediately(client):
    """The common case must not regress: setting up a station still works on
    the first reading, with no waiting."""
    assert _post(client, REAL).status_code == 200
    assert REAL in _macs(client)


def test_the_phantom_atlas_never_becomes_a_device(client):
    """The 2026-08-15 incident, replayed. One corrupt packet after the real
    station is known must not mint a second device."""
    _post(client, REAL)
    assert REAL in _macs(client)

    r = _post(client, PHANTOM, ts="2026-08-15T17:08:59Z")
    assert r.status_code == 200, "the reading is dropped, not rejected"
    assert PHANTOM not in _macs(client), "a single bad packet minted a device"
    assert _macs(client) == {REAL}


def test_the_quarantined_reading_is_not_stored(client):
    """It must not land in observations either — an orphan reading would
    still feed rollups and records."""
    from app import db
    _post(client, REAL)
    _post(client, PHANTOM, ts="2026-08-15T17:08:59Z")
    rows = asyncio.run(db.observations_for(PHANTOM)) if hasattr(
        db, "observations_for") else None
    if rows is None:
        pending = asyncio.run(db.list_pending_devices())
        assert [p["mac"] for p in pending] == [PHANTOM]
        assert pending[0]["suspect_of"] == REAL
    else:
        assert rows == []


def test_a_persistent_neighbour_is_eventually_admitted(client, monkeypatch):
    """Probation is not rejection. A real station one bit away from an
    existing one clears the bar once it proves itself, so a legitimate
    adjacent station ID is delayed, never lost.

    Sighting spacing rides the SERVER clock (R5-25: the device-claimed
    timestamp must not be able to fabricate spacing), so the test advances
    the patchable clock, not the payload timestamps."""
    import time as _time
    from app import ingest
    _post(client, REAL)
    t0 = int(_time.time() * 1000)
    base = "2026-08-15T17:%02d:00Z"
    for minute in range(5):
        monkeypatch.setattr(ingest, "_probation_now_ms",
                            lambda m=minute: t0 + m * 60_000)
        _post(client, PHANTOM, ts=base % (10 + minute))
    assert PHANTOM in _macs(client), "a real station was permanently blocked"


def test_probation_ignores_the_device_claimed_timestamp(client, monkeypatch):
    """CODE_REVIEW_R5 R5-25: a replayed backfill carrying perfectly spaced
    dateutc values must NOT count as spaced sightings — all five posts
    arrive within the same server instant, so at most one counts."""
    import time as _time
    from app import ingest
    _post(client, REAL)
    t0 = int(_time.time() * 1000)
    monkeypatch.setattr(ingest, "_probation_now_ms", lambda: t0)
    base = "2026-08-15T17:%02d:00Z"
    for minute in range(10, 10 + 5):
        _post(client, PHANTOM, ts=base % minute)
    assert PHANTOM not in _macs(client), \
        "crafted payload timestamps minted a device with zero real-time presence"


def test_a_forgotten_suspect_is_pruned(client, monkeypatch):
    """A one-off corrupt packet must not leave a probation row behind for
    good. The table is only ever written on the quarantine path, so pruning
    there is enough to bound it."""
    import time as _time
    from app import db, ingest
    _post(client, REAL)
    # Well past the 7-day TTL — made by pushing the SERVER clock into the
    # past for this sighting (spacing rides the server clock since R5-25).
    t_past = int(_time.time() * 1000) - 80 * 86_400_000
    monkeypatch.setattr(ingest, "_probation_now_ms", lambda: t_past)
    _post(client, PHANTOM, ts="2026-06-01T17:08:59Z")
    # Re-patch to real time rather than monkeypatch.undo(): undo() reverts
    # EVERYTHING on this shared function-scoped monkeypatch, including
    # temp_env's credential blanking — after which a Settings re-read falls
    # back to .env and the suite can hit live APIs (CodeRabbit, 2026-08-20;
    # the exact trap the conftest setenv("") rule exists for).
    monkeypatch.setattr(ingest, "_probation_now_ms",
                        lambda: int(_time.time() * 1000))
    assert len(asyncio.run(db.list_pending_devices())) == 1

    # A later suspect sweeps the stale row on its way in.
    other = "5D:5D:01:00:02:C5"          # 1 bit from REAL, so also a suspect
    _post(client, other)
    macs = [p["mac"] for p in asyncio.run(db.list_pending_devices())]
    assert PHANTOM not in macs, "the stale probation row was never pruned"
    assert other in macs


# --- Security: dynamic SQL guards -------------------------------------
#
# CLAUDE.md: "Backend SQL builds some column names with f-strings, guarded by
# an internal whitelist. Any new interpolation needs the same guard, and it
# must not be an `assert` (those vanish under `python -O`)."

def test_no_sql_guard_relies_on_assert():
    """An assert-based whitelist check disappears under `python -O`, taking
    the injection guard with it. Pins the rule repo-wide so a future
    interpolation cannot reintroduce the pattern."""
    import pathlib, re
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for f in app_dir.glob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if re.match(r"\s*assert\s", line) and re.search(r"in _[A-Z_]*COL|in _COLUMNS|in _FIELD", line):
                offenders.append(f"{f.name}:{i}")
    assert not offenders, f"assert-guarded SQL whitelist(s): {offenders}"


def test_rain_col_rejects_an_unwhitelisted_column():
    """The guard must raise on a name outside the whitelist rather than
    interpolating it into the SELECT."""
    from app import db
    with pytest.raises(ValueError):
        asyncio.run(db._rain_col_at_or_before("AA:BB:CC:DD:EE:FF",
                                              "yearlyrainin; DROP TABLE observations--",
                                              1_700_000_000_000))


def test_sightings_spread_past_the_ttl_never_accumulate(client):
    """Review 2026-08-20: the pending row was read with no staleness check
    (and bump's prune ran only after decide() had already counted), so the
    same recurring bit-flip arriving every ~2 weeks still summed to
    admission over months — minting the phantom device and its eventual
    false device-down alert. A trail gone cold (>= TTL, 168h) must restart
    the count, so a corrupt packet that only ever recurs slowly NEVER clears
    the bar."""
    import datetime as dt
    _post(client, REAL)
    assert REAL in _macs(client)
    # Ten sightings, each 8 days apart (> the 168h TTL), ending yesterday —
    # twice the needed=5 hits if they were allowed to accumulate.
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(10, 0, -1):
        ts = (now - dt.timedelta(days=8 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _post(client, PHANTOM, ts=ts).status_code == 200
    assert PHANTOM not in _macs(client), \
        "slow-recurring corrupt packet accumulated to admission across TTLs"
