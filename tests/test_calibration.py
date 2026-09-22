"""Per sensor calibration (2.4, item 7).

Every station in a real backyard reads a little wrong somewhere. What is
pinned here is the part that could quietly rewrite somebody's weather: a
correction that reaches a field it was never meant to, one that turns an
absent reading into a number, or one so large it is a typo rather than a
calibration.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import calibration as cal  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
ING = {"Authorization": "Bearer test-ingest-token"}


_CLOCK = [0]


def _post(client, device_id, **outdoor):
    """Each post gets its OWN minute: two readings with one timestamp is
    a duplicate row, and a test that could not tell those apart would
    pass while the correction did nothing."""
    _CLOCK[0] += 1
    return client.post("/ingest/custom", headers=ING, json={
        "device": {"id": device_id, "model": "Calibrated"},
        "timestamp_utc": f"2026-05-14T06:{_CLOCK[0]:02d}:00Z",
        "outdoor": {"humidity": 50, **outdoor},
        "source": "test"})


def test_only_correctable_readings_are_correctable():
    assert cal.clean({"tempf": 1.5})["tempf"] == 1.5
    # A derived field is not correctable: correcting its inputs already
    # corrected it, and correcting both would double the correction.
    assert cal.clean({"dewPoint": 1.0}) == {}
    assert cal.clean({"feelsLike": 1.0}) == {}
    # Wind direction is a mounting problem, not a calibration.
    assert cal.clean({"winddir": 10}) == {}
    assert cal.clean({"nonsense": 1}) == {}
    assert cal.clean("not a table") == {}


def test_a_correction_that_is_a_typo_is_dropped_not_clamped():
    """Clamping would store something nobody asked for and then apply it
    to every reading forever."""
    assert cal.clean({"tempf": 400}) == {}
    assert cal.clean({"tempf": float("nan")}) == {}
    assert cal.clean({"rainratein": 12}) == {}       # scale, way out
    assert cal.clean({"rainratein": 1.1})["rainratein"] == 1.1
    # A correction of nothing is not a correction.
    assert cal.clean({"tempf": 0}) == {}
    assert cal.clean({"rainratein": 1.0}) == {}


def test_offsets_add_and_scales_multiply():
    flat = {"tempf": 70.0, "rainratein": 1.00, "humidity": 44.0}
    applied = cal.apply(flat, {"tempf": -1.5, "rainratein": 1.05})
    assert flat["tempf"] == 68.5
    assert round(flat["rainratein"], 4) == 1.05
    assert flat["humidity"] == 44.0          # untouched
    assert applied == {"tempf": -1.5, "rainratein": 1.05}


def test_a_correction_never_invents_a_reading():
    """An offset on a sensor that reported nothing must not turn absence
    into a number ([[absent is not zero]])."""
    flat = {"tempf": 70.0}
    cal.apply(flat, {"solarradiation": 1.2, "soilhum1": 3})
    assert "solarradiation" not in flat and "soilhum1" not in flat


def test_ingest_stores_the_corrected_reading_and_says_what_it_did(client):
    _post(client, "AABBCC000071", tempf=70.0)
    r = client.put("/api/devices/AA:BB:CC:00:00:71/calibration", headers=H,
                   json={"calibration": {"tempf": -1.5}})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == ["tempf -1.5"]

    _post(client, "AABBCC000071", tempf=70.0)
    obs = client.get("/api/devices/AA:BB:CC:00:00:71/current",
                     headers=H).json()
    assert obs["tempf"] == 68.5
    # And the row carries what was done to it, so a reading can always be
    # turned back into what the sensor actually said.
    assert obs.get("calibration") == {"tempf": -1.5}


def test_the_route_refuses_a_typo_out_loud(client):
    _post(client, "AABBCC000072", tempf=70.0)
    r = client.put("/api/devices/AA:BB:CC:00:00:72/calibration", headers=H,
                   json={"calibration": {"tempf": 400}})
    assert r.status_code == 400 and "±25" in r.json()["detail"]
    r = client.put("/api/devices/AA:BB:CC:00:00:72/calibration", headers=H,
                   json={"calibration": {"dewPoint": 1}})
    assert r.status_code == 400 and "not a correctable" in r.json()["detail"]
    # An unknown station is a 404, not a correction stored against nothing.
    r = client.put("/api/devices/AA:BB:CC:99:99:99/calibration", headers=H,
                   json={"calibration": {"tempf": 1}})
    assert r.status_code == 404


def test_corrections_can_be_cleared_which_is_the_only_way_to_undo_one(client):
    _post(client, "AABBCC000073", tempf=70.0)
    client.put("/api/devices/AA:BB:CC:00:00:73/calibration", headers=H,
               json={"calibration": {"tempf": 2.0}})
    r = client.put("/api/devices/AA:BB:CC:00:00:73/calibration", headers=H,
                   json={"calibration": {}})
    assert r.status_code == 200 and r.json()["calibration"] == {}
    _post(client, "AABBCC000073", tempf=70.0)
    obs = client.get("/api/devices/AA:BB:CC:00:00:73/current",
                     headers=H).json()
    assert obs["tempf"] == 70.0
    assert "calibration" not in obs


def test_the_route_explains_itself(client):
    _post(client, "AABBCC000074", tempf=70.0)
    body = client.get("/api/devices/AA:BB:CC:00:00:74/calibration",
                      headers=H).json()
    assert body["offset_limit"] == cal.OFFSET_LIMIT
    assert {"field": "tempf", "kind": "offset"} in body["fields"]
    assert {"field": "rainratein", "kind": "scale"} in body["fields"]
    # A counter is not on the menu (R24-01).
    assert not any(f["field"] == "dailyrainin" for f in body["fields"])
    # The one thing somebody can be surprised by is said on the route.
    assert "from now on" in body["note"]


def test_a_percentage_is_clamped_rather_than_lost():
    """A hygrometer that reads low is exactly the one being corrected
    upward, and saturation is when the correction matters. 98 + 3 must
    store as 100, not fall out of the plausibility band as 101 and take
    the dew point with it."""
    flat = {"humidity": 98.0, "humidityin": 1.0, "soilhum3": 99.0}
    cal.apply(flat, {"humidity": 3.0, "humidityin": -5.0, "soilhum3": 4.0})
    assert flat["humidity"] == 100.0
    assert flat["humidityin"] == 0.0
    assert flat["soilhum3"] == 100.0


def test_a_consoles_own_dew_point_follows_the_corrected_temperature(client):
    """AWN, Tempest and Davis send a dew point computed from the RAW
    temperature. After an offset the stored tempf and the stored dew
    point disagreed, and the docstring's "correcting an input already
    corrects it" was false for everything a console derives itself."""
    dev = "AABBCC000072"
    # No correction: the console's own number is kept, wrong as it is.
    _post(client, dev, tempf=70.0, dew_point_f=60.0)
    obs = client.get("/api/devices/AA:BB:CC:00:00:72/current",
                     headers=H).json()
    assert obs["dewPoint"] == 60.0
    r = client.put("/api/devices/AA:BB:CC:00:00:72/calibration", headers=H,
                   json={"calibration": {"tempf": -1.5}})
    assert r.status_code == 200, r.text
    _post(client, dev, tempf=70.0, dew_point_f=60.0)
    obs = client.get("/api/devices/AA:BB:CC:00:00:72/current",
                     headers=H).json()
    assert obs["tempf"] == 68.5
    # Re-derived from the corrected pair, not carried over from the raw one.
    from app import derived
    assert obs["dewPoint"] == round(derived.dew_point_f(68.5, 50), 1)
    assert obs["dewPoint"] < 55


def test_the_cloud_poller_stores_the_corrected_reading_too(client):
    """Calibration lived in /ingest only. The AmbientWeather poller and
    the Ecowitt bootstrap write through db.insert_observations directly,
    so a correction on a cloud station answered 200 and did nothing."""
    import asyncio
    import time
    from app import db, poller as pmod

    mac = "AA:BB:CC:00:00:73"
    clock = [int(time.time() * 1000) - 120_000]

    class Client:
        async def list_devices(self):
            clock[0] += 60_000
            return [{"macAddress": mac,
                     "lastData": {"dateutc": clock[0], "tempf": 70.0,
                                  "humidity": 50, "dewPoint": 60.0}}]

    async def go():
        p = pmod.Poller(Client())
        await p._tick()                       # creates the device row
        assert await db.set_calibration(mac, {"tempf": -1.5})
        await p._tick()

    asyncio.run(go())
    obs = client.get(f"/api/devices/{mac}/current", headers=H).json()
    assert obs["tempf"] == 68.5
    assert obs.get("calibration") == {"tempf": -1.5}
    from app import derived
    assert obs["dewPoint"] == round(derived.dew_point_f(68.5, 50), 1)


def test_a_database_from_the_previous_release_gains_the_columns(tmp_path,
                                                                monkeypatch):
    """The 2.4 columns, migrated onto a 2.3 database WITH DATA. Every
    other calibration test starts from an empty file, which is the one
    shape that cannot catch a migration bug (ref_map_directory_outage)."""
    import asyncio
    import sqlite3
    from app import config, db as dbmod

    import re

    def previous_release(table: str, *without: str) -> str:
        """The current CREATE TABLE for `table`, minus the 2.4 columns:
        what a 2.3 database has. Built from SCHEMA so the fixture cannot
        drift from the real column list the way a hand-typed one would."""
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
                      dbmod.SCHEMA, re.S)
        assert m, table
        lines = [ln for ln in m.group(1).splitlines()
                 if not ln.strip().startswith("--")
                 and not any(re.match(rf"\s*{w}\b", ln) for w in without)]
        body = "\n".join(lines).rstrip().rstrip(",")
        return f"CREATE TABLE {table} ({body})"

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(previous_release("devices", "calibration_json"))
    con.execute(previous_release(
        "observations", *[f"soilhum{i}" for i in range(5, 9)],
        *[f"soiltemp{i}f" for i in range(5, 9)]))
    for i in range(5, 9):
        for col in (f"soilhum{i}", f"soiltemp{i}f"):
            assert col not in {r[1] for r in con.execute(
                "PRAGMA table_info(observations)")}
    assert "calibration_json" not in {
        r[1] for r in con.execute("PRAGMA table_info(devices)")}
    con.execute("INSERT INTO devices (mac, name) VALUES (?, ?)",
                ("AA:BB:CC:00:00:74", "Old"))
    con.execute("INSERT INTO observations (mac, dateutc_ms, data_json, tempf)"
                " VALUES (?, ?, ?, ?)",
                ("AA:BB:CC:00:00:74", 1_700_000_000_000, "{}", 70.0))
    con.commit()
    con.close()

    monkeypatch.setattr(config.settings, "database_path", str(path))
    monkeypatch.setattr(dbmod.settings, "database_path", str(path))
    asyncio.run(dbmod.init_db())

    con = sqlite3.connect(path)
    devices = {r[1] for r in con.execute("PRAGMA table_info(devices)")}
    obs = {r[1] for r in con.execute("PRAGMA table_info(observations)")}
    con.close()
    assert "calibration_json" in devices
    for i in range(5, 9):
        assert f"soilhum{i}" in obs and f"soiltemp{i}f" in obs


def test_a_rederived_feels_like_answers_to_the_bands_as_ours(client):
    """A console that sends its own feels-like has it re-derived after a
    correction. That makes it OUR derivation, and the band rule that
    refuses a derivation whose input was just nulled must see it as
    ours: with the flag left false, a corrected station posting one
    out-of-band humidity stored a feels-like computed from that garbage
    (CodeRabbit, PR #40). Read from the stored row: /current carries
    the last good humidity forward and would hide the question."""
    import asyncio
    from app import db
    dev = "AABBCC000074"
    _post(client, dev, tempf=70.0, feels_like=80.0)
    r = client.put("/api/devices/AA:BB:CC:00:00:74/calibration", headers=H,
                   json={"calibration": {"tempf": -1.5}})
    assert r.status_code == 200, r.text
    _post(client, dev, tempf=70.0, feels_like=80.0, humidity=150)
    rows = asyncio.run(db.observation_rows("AA:BB:CC:00:00:74", 0,
                                           4_000_000_000_000))
    assert len(rows) == 2
    first, second = rows
    assert first["feels_like"] == 80.0          # uncorrected: theirs, kept
    assert second["tempf"] == 68.5
    assert second["humidity"] is None           # banded
    assert second["feels_like"] is None         # ours now, and its input died


def test_a_rain_counter_is_never_correctable():
    """A scale on a cumulative counter is an ingest offset in disguise:
    the moment the scale changes, the stored counter steps with no rain
    falling, and the ledger reads the step as rain. R24-01 (2.4 release
    review). The rate and the trailing-hour total are not folded as
    counters and stay correctable."""
    for field in ("dailyrainin", "eventrainin", "weeklyrainin",
                  "monthlyrainin", "yearlyrainin", "totalrainin"):
        assert cal.clean({field: 1.1}) == {}, field
    assert cal.clean({"rainratein": 1.1}) == {"rainratein": 1.1}
    assert cal.clean({"hourlyrainin": 1.1}) == {"hourlyrainin": 1.1}


def test_changing_a_counter_scale_cannot_create_rain(client, monkeypatch):
    """R24-01: an unchanged raw yearly counter of 2.0 in, with a scale of
    1.1 saved between two posts, credited 0.20 in to the day ledger. The
    route refuses the field now, and the ledger stays dry."""
    import asyncio
    from app import db
    from app.day_rain import day_rain_in
    monkeypatch.setattr(db.settings, "insights", True)
    dev = "AABBCC000075"

    def post(minute):
        r = client.post("/ingest/custom", headers=ING, json={
            "device": {"id": dev, "model": "Calibrated"},
            "timestamp_utc": f"2026-09-21T12:{minute:02d}:00Z",
            "rain": {"yearly_in": 2.0}, "source": "test"})
        assert r.status_code == 200, r.text
    post(0)
    r = client.put("/api/devices/AA:BB:CC:00:00:75/calibration", headers=H,
                   json={"calibration": {"yearlyrainin": 1.1}})
    assert r.status_code == 400
    assert "yearlyrainin" in r.text
    post(1)

    async def ledger():
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac=? AND day='2026-09-21'",
                ("AA:BB:CC:00:00:75",))).fetchone()
        return day_rain_in(dict(row)) if row else None
    assert (asyncio.run(ledger()) or 0.0) == 0.0
