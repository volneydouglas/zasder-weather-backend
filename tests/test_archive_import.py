"""Importing somebody else's archive (2.4, item 4).

WeeWX and CSV, the two doors people actually arrive with. What is pinned
here is the unit conversion, which is the half that goes wrong silently:
a file in metric imported as if it were Fahrenheit does not error, it
just quietly rewrites somebody's climate.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import archive_import as ai  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}


def _make_device(client, device_id: str) -> None:
    """A station has to exist before anything can be imported into it —
    an import that CREATED one would make a typo in a mac a new station
    full of somebody else's weather."""
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": device_id, "model": "Imported"},
                          "timestamp_utc": "2026-05-14T06:00:00Z",
                          "outdoor": {"tempf": 70.0, "humidity": 40},
                          "source": "test"})
    assert r.status_code == 200, r.text


def _weewx_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE archive (dateTime INTEGER PRIMARY KEY, "
                 "usUnits INTEGER, interval INTEGER, outTemp REAL, "
                 "outHumidity REAL, windSpeed REAL, windGust REAL, "
                 "windDir REAL, barometer REAL, dewpoint REAL, "
                 "radiation REAL, UV REAL, rain REAL, rainRate REAL, "
                 "inTemp REAL)")
    conn.executemany(
        "INSERT INTO archive (dateTime, usUnits, outTemp, windSpeed, "
        "barometer, rain, outHumidity) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_a_us_row_needs_no_conversion():
    row = ai.weewx_row({"dateTime": 1_700_000_000, "usUnits": ai.US,
                        "outTemp": 72.5, "windSpeed": 8.0,
                        "barometer": 29.92, "outHumidity": 41})
    assert row["dateutc"] == 1_700_000_000_000
    assert row["tempf"] == 72.5
    assert row["windspeedmph"] == 8.0
    assert row["baromrelin"] == 29.92
    assert row["humidity"] == 41


def test_metric_and_metricwx_differ_by_wind_which_is_the_trap():
    """16 is km/h and 17 is m/s. Reading one as the other triples
    somebody's gusts and nothing anywhere says a word about it."""
    metric = ai.weewx_row({"dateTime": 1_700_000_000, "usUnits": ai.METRIC,
                           "outTemp": 20.0, "windSpeed": 16.09,
                           "barometer": 1013.25, "rain": 2.54})
    wx = ai.weewx_row({"dateTime": 1_700_000_000, "usUnits": ai.METRICWX,
                       "outTemp": 20.0, "windSpeed": 16.09,
                       "barometer": 1013.25, "rain": 25.4})
    assert round(metric["tempf"], 1) == 68.0 and round(wx["tempf"], 1) == 68.0
    assert round(metric["windspeedmph"], 1) == 10.0      # km/h
    assert round(wx["windspeedmph"], 1) == 36.0          # m/s
    assert round(metric["baromrelin"], 2) == 29.92
    # METRIC rain is centimetres, METRICWX is millimetres. Both are one inch.
    assert round(metric["intervalRainIn"], 2) == 1.0
    assert round(wx["intervalRainIn"], 2) == 1.0


def test_the_rows_own_unit_system_wins():
    """A database that changed system part way through its life, which
    happens when somebody edits weewx.conf, has to convert correctly on
    both sides of the change."""
    us = ai.weewx_row({"dateTime": 1, "usUnits": ai.US, "outTemp": 50.0})
    metric = ai.weewx_row({"dateTime": 2, "usUnits": ai.METRIC, "outTemp": 10.0})
    assert us["tempf"] == 50.0 and round(metric["tempf"], 1) == 50.0


def test_interval_rain_never_becomes_a_rain_column():
    """WeeWX's `rain` is this interval's accumulation, which is not what
    any rain column here means. It rides in data_json instead of being
    written into a column it would be a lie in."""
    row = ai.weewx_row({"dateTime": 1, "usUnits": ai.US, "rain": 0.04})
    assert row["intervalRainIn"] == 0.04
    for lying in ("dailyrainin", "totalrainin", "hourlyrainin", "eventrainin"):
        assert lying not in row


def test_a_missing_sensor_imports_as_missing():
    """A station with no solar head must not import as a year of
    midnight ([[absent is not zero]])."""
    row = ai.weewx_row({"dateTime": 1, "usUnits": ai.US, "outTemp": 60.0,
                        "radiation": None, "UV": ""})
    assert "solarradiation" not in row and "uv" not in row
    assert row["tempf"] == 60.0


def test_a_row_with_no_time_is_not_a_row():
    for junk in ({}, {"dateTime": 0}, {"dateTime": "later"},
                 {"dateTime": None}):
        assert ai.weewx_row(junk) is None


def test_reading_a_real_file(tmp_path):
    path = str(tmp_path / "weewx.sdb")
    _weewx_db(path, [(1_700_000_000, ai.US, 70.0, 5.0, 29.9, 0.0, 40),
                     (1_700_000_300, ai.US, 71.0, 6.0, 29.9, 0.01, 41),
                     (1_700_000_600, ai.METRIC, 21.0, 8.0, 1013.0, 0.0, 42)])
    rows = list(ai.read_weewx(path))
    assert len(rows) == 3
    assert [r["dateutc"] for r in rows] == [1_700_000_000_000,
                                            1_700_000_300_000,
                                            1_700_000_600_000]
    assert round(rows[2]["tempf"], 1) == 69.8
    summary = ai.weewx_summary(path)
    assert summary["rows"] == 3
    assert summary["first_ms"] == 1_700_000_000_000


def test_a_csv_goes_through_the_mapping_it_was_given():
    text = ("when,temp,rh,gust\n"
            "2026-09-20 14:05:00,72.5,41,18.0\n"
            "2026-09-20 14:10:00,73.0,40,21.0\n")
    rows = list(ai.csv_rows(text, {"temp": "tempf", "rh": "humidity",
                                   "gust": "windgustmph"},
                            time_column="when",
                            time_format="%Y-%m-%d %H:%M:%S"))
    assert len(rows) == 2
    assert rows[0]["tempf"] == 72.5 and rows[0]["humidity"] == 41
    assert rows[1]["windgustmph"] == 21.0
    # Times without an offset are the station's own clock, not UTC.
    assert rows[1]["dateutc"] > rows[0]["dateutc"]


def test_a_csv_column_we_have_no_home_for_is_refused_not_guessed():
    text = "when,temp,soil_moisture\n1700000000,70,32\n"
    rows = list(ai.csv_rows(text, {"temp": "tempf",
                                   "soil_moisture": "soilmoisture1"},
                            time_column="when"))
    assert rows[0]["tempf"] == 70
    assert "soilmoisture1" not in rows[0]


def test_an_epoch_in_milliseconds_is_not_the_year_47000():
    rows = list(ai.csv_rows("when,temp\n1700000000000,70\n",
                            {"temp": "tempf"}, time_column="when"))
    assert rows[0]["dateutc"] == 1_700_000_000_000


def test_the_import_is_paced_and_can_be_cancelled(client):
    async def run():
        rows = [{"dateutc": 1_700_000_000_000 + i * 60_000, "tempf": 70.0}
                for i in range(1200)]
        out = await ai.run_import("AA:BB:CC:00:00:01", iter(rows),
                                  kind="weewx")
        assert out["state"] == "done"
        assert out["read"] == 1200
        assert out["inserted"] == 1200
        # Re-running the same file costs nothing: the insert is INSERT OR
        # IGNORE on (mac, dateutc).
        again = await ai.run_import("AA:BB:CC:00:00:01", iter(rows),
                                    kind="weewx")
        assert again["inserted"] == 0
        # A dry run reads everything and writes nothing.
        dry = await ai.run_import("AA:BB:CC:00:00:02", iter(rows),
                                  kind="weewx", dry_run=True)
        assert dry["read"] == 1200 and dry["inserted"] == 0
        # And cancelled means cancelled: a stop request mid-file ends the
        # job with the rows so far and no more (2.4 review — this test's
        # name promised it and never asked).
        big = ({"dateutc": 1_700_000_000_000 + i * 60_000, "tempf": 70.0}
               for i in range(20_000))
        job = asyncio.create_task(
            ai.run_import("AA:BB:CC:00:00:03", big, kind="weewx"))
        # The task has not started until this coroutine yields, and the
        # previous job's dict still reads "done" until it does.
        while not (ai.status().get("mac") == "AA:BB:CC:00:00:03"
                   and ai.status().get("read", 0) >= ai.BATCH_ROWS):
            await asyncio.sleep(0.005)
        assert ai.cancel() is True
        out = await job
        assert out["state"] == "cancelled"
        assert 0 < out["read"] < 20_000
        assert out["inserted"] <= out["read"]
    asyncio.run(run())


def test_the_csv_route_imports_and_refuses_a_field_we_cannot_store(client):
    _make_device(client, "AABBCC000044")
    body = {"mac": "AA:BB:CC:00:00:44",
            "csv": "when,temp,rh\n1700100000,71.5,44\n1700100300,71.8,43\n",
            "mapping": {"temp": "tempf", "rh": "humidity"},
            "time_column": "when"}
    r = client.post("/api/import/csv", headers=H, json=body)
    assert r.status_code == 200, r.text

    import time as _t
    for _ in range(50):
        st = client.get("/api/import/archive/status", headers=H).json()
        if st.get("state") in ("done", "error"):
            break
        _t.sleep(0.05)
    assert st["state"] == "done", st
    assert st["read"] == 2 and st["inserted"] == 2

    # A mapping naming something this server has no column for is a 400
    # rather than a quietly thinner import.
    bad = dict(body, mapping={"temp": "tempf", "rh": "soil_moisture_7"})
    r = client.post("/api/import/csv", headers=H, json=bad)
    assert r.status_code == 400 and "soil_moisture_7" in r.json()["detail"]

    # And it refuses to import into a device that does not exist.
    gone = dict(body, mac="AA:BB:CC:99:99:99")
    assert client.post("/api/import/csv", headers=H, json=gone).status_code == 404


def test_the_weewx_route_reads_the_file_it_was_handed(client, tmp_path):
    _make_device(client, "AABBCC000045")
    path = str(tmp_path / "weewx.sdb")
    _weewx_db(path, [(1_700_200_000 + i * 300, ai.US, 70.0 + i, 5.0,
                      29.9, 0.0, 40) for i in range(5)])
    with open(path, "rb") as f:
        r = client.post("/api/import/weewx?mac=AA:BB:CC:00:00:45",
                        headers=H, content=f.read())
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 5
    assert r.json()["first_ms"] == 1_700_200_000_000

    import time as _t
    for _ in range(50):
        st = client.get("/api/import/archive/status", headers=H).json()
        if st.get("state") in ("done", "error"):
            break
        _t.sleep(0.05)
    assert st["state"] == "done" and st["inserted"] == 5

    # Something that is not a database at all is a 400 that says so.
    r = client.post("/api/import/weewx?mac=AA:BB:CC:00:00:45",
                    headers=H, content=b"this is not a sqlite file")
    assert r.status_code == 400 and "weewx.sdb" in r.json()["detail"]


def test_legitimate_archives_over_one_mib_are_accepted(client, tmp_path):
    """R24-02 (2.4 release review): a 1.2 MB CSV and a 1.2 MB weewx.sdb
    both came back 413 from the global 1 MiB cap. Both doors have their
    own bounded cap now."""
    _make_device(client, "AABBCC000046")
    csv = "when,temp\n" + "1700000000,70.0\n" * 75000
    assert 1024 * 1024 < len(csv) < 16 * 1024 * 1024
    r = client.post("/api/import/csv", headers=H, json={
        "mac": "AA:BB:CC:00:00:46", "csv": csv, "mapping": {"temp": "tempf"},
        "time_column": "when", "dry_run": True})
    assert r.status_code == 200, r.text
    import time as _t
    for _ in range(200):
        if ai.status().get("state") != "running":
            break
        _t.sleep(0.05)
    path = tmp_path / "weewx.sdb"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE archive (dateTime INTEGER PRIMARY KEY, "
                 "usUnits INTEGER, outTemp REAL)")
    conn.executemany("INSERT INTO archive VALUES (?,1,70)",
                     [(1700000000 + i * 300,) for i in range(90000)])
    conn.commit(); conn.close()
    assert path.stat().st_size > 1024 * 1024
    r = client.post("/api/import/weewx?mac=AA:BB:CC:00:00:46&dry_run=true",
                    headers=H, content=path.read_bytes())
    assert r.status_code == 200, r.text
    for _ in range(400):
        if ai.status().get("state") != "running":
            break
        _t.sleep(0.05)


def test_an_anonymous_oversized_csv_is_refused_before_it_is_parsed(client):
    """The CSV door's cap is 16 MiB and is enforced at the ASGI layer, so
    an anonymous 20 MB body is refused on its Content-Length without a
    byte being read or a token being checked (R24-02)."""
    body = b'{"mac":"x","csv":"' + b"a" * (20 * 1024 * 1024) + b'"}'
    r = client.post("/api/import/csv", content=body,
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    # And an unrelated route still caps at 1 MiB.
    r = client.post("/api/alerts", headers=H, content=b"{" + b" " * (2 * 1024 * 1024))
    assert r.status_code == 413


def test_weewx_interval_rain_reaches_the_rain_ledger(client, monkeypatch):
    """R24-03 (2.4 release review): four 0.05 in intervals imported
    fine, the JSON kept 0.20 in of intervalRainIn, and the day ledger's
    rain was None. The importer now synthesises the day counter the
    station would have posted, a running sum per local day, so the
    ledger folds it like any other tier-three station's day."""
    from app import db
    from app.day_rain import day_rain_in
    from datetime import datetime, timezone
    monkeypatch.setattr(db.settings, "insights", True)
    _make_device(client, "AABBCC000047")
    mac = "AA:BB:CC:00:00:47"
    base = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp())

    async def run():
        rows = [ai.weewx_row({"dateTime": base + i * 300, "usUnits": 1,
                              "rain": 0.05, "outTemp": 70}) for i in range(4)]
        # A second day starts its own sum.
        rows += [ai.weewx_row({"dateTime": base + 86400 + i * 300,
                               "usUnits": 1, "rain": 0.10, "outTemp": 70})
                 for i in range(2)]
        out = await ai.run_import(mac, iter(rows), kind="weewx")
        assert out["inserted"] == 6
        async with db.connect() as conn:
            d1 = dict(await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac=? AND day='2026-09-21'",
                (mac,))).fetchone())
            d2 = dict(await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac=? AND day='2026-09-22'",
                (mac,))).fetchone())
            stored = await (await conn.execute(
                "SELECT dailyrainin, data_json FROM observations WHERE mac=? "
                "AND dateutc_ms >= ? ORDER BY dateutc_ms", (mac, base * 1000))).fetchall()
        assert day_rain_in(d1) == pytest.approx(0.20)
        assert day_rain_in(d2) == pytest.approx(0.20)
        assert [r["dailyrainin"] for r in stored] == pytest.approx(
            [0.05, 0.10, 0.15, 0.20, 0.10, 0.20])
        import json as _json
        assert _json.loads(stored[0]["data_json"])["intervalRainIn"] == 0.05
        # Re-import is still free.
        again = await ai.run_import(mac, iter(rows), kind="weewx")
        assert again["inserted"] == 0
    asyncio.run(run())


def test_csv_interval_rain_is_mappable_and_summed_per_day(client, monkeypatch):
    """The CSV door gets the same treatment: a column mapped to
    intervalRainIn is summed into the day counter, and rows out of order
    in the file are sorted first (R24-03)."""
    from app import db
    from app.day_rain import day_rain_in
    monkeypatch.setattr(db.settings, "insights", True)
    _make_device(client, "AABBCC000048")
    mac = "AA:BB:CC:00:00:48"
    text = ("when,rain\n"
            "1789993200,0.02\n"   # 2026-09-21 22:20Z, out of order on purpose
            "1789992600,0.03\n"   # 22:10Z
            "1789992000,0.05\n")  # 22:00Z
    r = client.post("/api/import/csv", headers=H, json={
        "mac": mac, "csv": text, "mapping": {"rain": "intervalRainIn"},
        "time_column": "when"})
    assert r.status_code == 200, r.text
    import time as _t
    for _ in range(200):
        if ai.status().get("state") != "running":
            break
        _t.sleep(0.05)
    assert ai.status()["inserted"] == 3

    async def ledger():
        async with db.connect() as conn:
            row = dict(await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac=? AND day='2026-09-21'",
                (mac,))).fetchone())
            stored = await (await conn.execute(
                "SELECT dailyrainin FROM observations WHERE mac=? "
                "ORDER BY dateutc_ms", (mac,))).fetchall()
        return day_rain_in(row), [s[0] for s in stored]
    total, counters = asyncio.run(ledger())
    assert total == pytest.approx(0.10)
    assert counters[-3:] == pytest.approx([0.05, 0.08, 0.10])


def test_the_csv_field_catalogue_is_served_not_copied(client):
    """The mapping screen lists what it can map from this route, so the
    app never keeps its own copy of FIELD_KINDS (2.4 item 4, app half)."""
    H = {"Authorization": "Bearer test-api-token"}
    r = client.get("/api/import/csv/fields", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    served = {f["field"]: f["kind"] for f in body["fields"]}
    assert served == ai.FIELD_KINDS
    assert set(body["units"]) == set(ai.UNIT_SYSTEMS)
    assert "time" in body["note"]
    assert client.get("/api/import/csv/fields").status_code == 401


# ── the CSV file door (2.5, a 2.4 review follow-up) ──────────────────────

def _wait_import():
    import time as _t
    for _ in range(400):
        if ai.status().get("state") != "running":
            break
        _t.sleep(0.05)
    return ai.status()


def test_the_file_door_streams_sorts_on_disk_and_sums_interval_rain(client, monkeypatch):
    """The same out-of-order file as the JSON door's test, sent as the file
    itself: rows are read one at a time and sorted through a SQLite file
    beside the upload, and the day counter comes out the same."""
    import json as _json
    from app import db
    from app.day_rain import day_rain_in
    monkeypatch.setattr(db.settings, "insights", True)
    _make_device(client, "AABBCC000049")
    mac = "AA:BB:CC:00:00:49"
    text = ("﻿when,rain,temp\n"      # a spreadsheet's BOM
            "1789993200,0.02,71\n"
            "1789992600,0.03,72\n"
            "1789992000,0.05,73\n")
    r = client.post("/api/import/csv/file", headers=H, content=text.encode(),
                    params={"mac": mac, "time_column": "when",
                            "mapping": _json.dumps({"rain": "intervalRainIn",
                                                    "temp": "tempf"})})
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == 3
    st = _wait_import()
    assert st["state"] == "done" and st["inserted"] == 3

    async def ledger():
        async with db.connect() as conn:
            row = dict(await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac=? AND day='2026-09-21'",
                (mac,))).fetchone())
            stored = await (await conn.execute(
                "SELECT dailyrainin, tempf FROM observations WHERE mac=? "
                "ORDER BY dateutc_ms", (mac,))).fetchall()
        return day_rain_in(row), [tuple(s) for s in stored]
    total, rows = asyncio.run(ledger())
    assert total == pytest.approx(0.10)
    assert [c for c, _ in rows[-3:]] == pytest.approx([0.05, 0.08, 0.10])
    assert [t for _, t in rows[-3:]] == [73, 72, 71]
    # Nothing left behind: the upload and the sort file are both gone.
    import glob, tempfile
    assert not glob.glob(os.path.join(tempfile.gettempdir(), ".csvsort-*"))


def test_the_file_door_refuses_what_it_cannot_read(client):
    import json as _json
    _make_device(client, "AABBCC000050")
    mac = "AA:BB:CC:00:00:50"
    no_time = client.post("/api/import/csv/file", headers=H, content=b"a,b\n1,2\n",
                          params={"mac": mac, "time_column": "when",
                                  "mapping": _json.dumps({"a": "tempf"})})
    assert no_time.status_code == 400 and "when" in no_time.json()["detail"]
    bad_field = client.post("/api/import/csv/file", headers=H, content=b"when,a\n1,2\n",
                            params={"mac": mac, "time_column": "when",
                                    "mapping": _json.dumps({"a": "rain_total"})})
    assert bad_field.status_code == 400
    bad_json = client.post("/api/import/csv/file", headers=H, content=b"when\n1\n",
                           params={"mac": mac, "time_column": "when", "mapping": "[1]"})
    assert bad_json.status_code == 400
    empty = client.post("/api/import/csv/file", headers=H, content=b"",
                        params={"mac": mac, "time_column": "when", "mapping": "{}"})
    assert empty.status_code == 400
    assert client.post("/api/import/csv/file", content=b"when\n1\n",
                       params={"mac": mac, "time_column": "when",
                               "mapping": "{}"}).status_code == 401


def test_the_on_disk_sort_keeps_a_file_order_tie_stable(tmp_path):
    rows = [{"dateutc": 2, "n": "b"}, {"dateutc": 1, "n": "a"}, {"dateutc": 2, "n": "c"}]
    out = list(ai.sorted_on_disk(iter(rows), str(tmp_path), chunk=2))
    assert [r["n"] for r in out] == ["a", "b", "c"]
    assert not list(tmp_path.iterdir())


def test_the_catalogue_names_the_file_doors_limit(client):
    body = client.get("/api/import/csv/fields", headers=H).json()
    from app.limits import WEEWX_IMPORT_MAX
    assert body["file_door_max_bytes"] == WEEWX_IMPORT_MAX


def test_the_sort_runs_off_the_event_loop(client, tmp_path):
    """Greptile, PR #48: the first row out of an on-disk sort needs the
    whole file parsed and indexed, and that ran on the event loop, so a
    512 MiB CSV held live ingest for the whole sort. The rows are now
    consumed by the spill in a worker thread."""
    import threading
    _make_device(client, "AABBCC000051")
    seen: set[int] = set()

    def rows():
        for t in (3, 1, 2):
            seen.add(threading.get_ident())
            yield {"dateutc": 1789990000000 + t * 60_000, "tempf": 70 + t}

    async def go():
        loop_thread = threading.get_ident()
        st = await ai.run_import("AA:BB:CC:00:00:51", rows(), kind="csv",
                                 ordered=False, spill_dir=str(tmp_path))
        return loop_thread, st
    loop_thread, st = asyncio.run(go())
    assert st["state"] == "done" and st["inserted"] == 3
    assert seen and loop_thread not in seen
    assert not list(tmp_path.iterdir()), "the sort file is removed"


def test_a_sort_that_would_fill_the_disk_stops_and_cleans_up(client, tmp_path, monkeypatch):
    """Greptile, PR #48: the upload's room check could not see the sort's
    second copy. The spill checks the floor as it goes, stops with a
    readable error and leaves nothing behind."""
    _make_device(client, "AABBCC000052")
    monkeypatch.setattr(ai, "_free_bytes", lambda _p: ai.SORT_MIN_FREE_BYTES - 1)
    rows = ({"dateutc": 1789990000000 + k * 60_000, "tempf": 70} for k in range(10))
    st = asyncio.run(ai.run_import("AA:BB:CC:00:00:52", rows, kind="csv",
                                   ordered=False, spill_dir=str(tmp_path)))
    assert st["state"] == "error" and "free disk" in st["error"]
    assert not list(tmp_path.iterdir())


def test_the_file_door_reserves_room_for_the_sort(client, monkeypatch):
    """An upload with room for itself but not for the sort's copy is
    refused before a byte lands."""
    import json as _json
    import shutil
    from collections import namedtuple
    _make_device(client, "AABBCC000053")
    body = b"when,temp\n" + b"1789990000,71\n" * 1000
    Usage = namedtuple("Usage", "total used free")
    room = len(body) * 2 + 64 * 1024 * 1024       # the upload twice, not three times
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(0, 0, room))
    r = client.post("/api/import/csv/file", headers=H, content=body,
                    params={"mac": "AA:BB:CC:00:00:53", "time_column": "when",
                            "mapping": _json.dumps({"temp": "tempf"})})
    assert r.status_code == 507, r.text


def test_one_import_slot_is_taken_before_the_upload_is_awaited(client):
    """CodeRabbit, PR #48: status() is a copy of JOB, and the file doors
    await the whole upload between checking it and starting the task, so
    two uploads could both pass. The slot is reserved first; a refused
    request gives it back."""
    import json as _json
    _make_device(client, "AABBCC000054")
    mac = "AA:BB:CC:00:00:54"
    assert ai.reserve() is True
    assert ai.reserve() is False, "a second taker is refused"
    busy = client.post("/api/import/csv/file", headers=H, content=b"when,temp\n1789990000,71\n",
                       params={"mac": mac, "time_column": "when",
                               "mapping": _json.dumps({"temp": "tempf"})})
    assert busy.status_code == 409
    ai.release()
    bad = client.post("/api/import/csv/file", headers=H, content=b"a\n1\n",
                      params={"mac": mac, "time_column": "when",
                              "mapping": _json.dumps({"a": "tempf"})})
    assert bad.status_code == 400
    assert ai.reserve() is True, "the refused request released the slot"
    ai.release()


def test_the_index_build_is_checked_against_the_floor(tmp_path, monkeypatch):
    """CodeRabbit, PR #48: the per-batch floor could not see the index,
    built after the last batch. Room for every batch but not the index is
    refused before CREATE INDEX, and the file is removed."""
    monkeypatch.setattr(ai, "_free_bytes",
                        lambda _p: ai.SORT_MIN_FREE_BYTES + 5 * ai.INDEX_BYTES_PER_ROW)
    rows = ({"dateutc": k, "tempf": 70} for k in range(10))
    with pytest.raises(ai.SortSpaceError, match="index"):
        ai.spill_sorted(rows, str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_a_cancelled_import_still_removes_the_sort_file(client, tmp_path):
    """CodeRabbit, PR #48: cancelling the import task cannot stop the
    spill's worker thread; whatever it writes is removed when it ends."""
    import threading
    import time as _t
    started, finish = threading.Event(), threading.Event()

    def rows():
        yield {"dateutc": 2, "tempf": 70}
        started.set()
        finish.wait(5)
        yield {"dateutc": 1, "tempf": 71}

    async def go():
        task = asyncio.ensure_future(ai.run_import(
            "AA:BB:CC:00:00:55", rows(), kind="csv", ordered=False,
            spill_dir=str(tmp_path)))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        finish.set()
        assert ai.status()["state"] == "cancelled", "not left reading running"
        assert ai.reserve() is True, "the slot is free again"
        ai.release()
        for _ in range(200):
            await asyncio.sleep(0.02)
            if not list(tmp_path.iterdir()):
                break
    asyncio.run(go())
    assert not list(tmp_path.iterdir())


def test_an_upload_longer_than_it_declared_is_refused_and_removed(tmp_path, monkeypatch):
    """CodeRabbit, PR #48: disk room is reserved from Content-Length, so a
    body that sends more than it declared must not be written past it."""
    import tempfile
    from fastapi import HTTPException
    from app import main
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    class Req:
        headers = {"content-length": "10"}

        async def stream(self):
            yield b"0123456789"
            yield b"more than declared"

    with pytest.raises(HTTPException) as e:
        asyncio.run(main._stream_upload_to_temp(Req(), suffix=".csv", prefix=".csv-"))
    assert e.value.status_code == 400
    assert not list(tmp_path.iterdir()), "the partial file is removed"


@pytest.mark.parametrize("mapping, needle", [
    ({}, "at least one"),
    ({"Temprature": "tempf"}, "Temprature"),
    ({"when": "tempf"}, "time column"),
])
def test_a_mapping_that_imports_nothing_is_refused_by_both_doors(client, mapping, needle):
    """R25-02 (the 2.5 detailed review): an empty mapping, a misspelled
    column or the time column mapped as a reading reported success and
    stored timestamp-only rows, which INSERT OR IGNORE then kept against
    the corrected import. Both doors refuse before the job starts."""
    import json as _json
    _make_device(client, "AABBCC000060")
    mac = "AA:BB:CC:00:00:60"
    csv = "when,Temperature\n1700100000,71.5\n"
    j = client.post("/api/import/csv", headers=H, json={
        "mac": mac, "csv": csv, "mapping": mapping, "time_column": "when"})
    assert j.status_code == 400 and needle in j.json()["detail"], j.text
    f = client.post("/api/import/csv/file", headers=H, content=csv.encode(),
                    params={"mac": mac, "time_column": "when",
                            "mapping": _json.dumps(mapping)})
    assert f.status_code == 400 and needle in f.json()["detail"], f.text
    assert ai.reserve() is True, "a refusal gives the slot back"
    ai.release()


def test_a_duplicated_header_is_refused():
    assert "Temperature" in ai.mapping_problem(
        {"Temperature": "tempf"}, ["when", "Temperature", "Temperature"], "when")


def test_a_row_with_no_readings_is_not_an_observation():
    assert ai.csv_row({"t": "1700000000", "Temp": ""}, {"Temp": "tempf"},
                      time_column="t") is None
    assert ai.csv_row({"t": "1700000000", "Temp": "70"}, {"Temp": "tempf"},
                      time_column="t")["tempf"] == 70


def test_a_corrected_import_after_a_refused_one_stores_the_values(client):
    """The review's missing repair case: nothing from the refused request
    may hold the timestamp against the good one."""
    _make_device(client, "AABBCC000061")
    mac = "AA:BB:CC:00:00:61"
    csv = "when,Temperature\n1700100000,71.5\n1700100300,\n"
    bad = client.post("/api/import/csv", headers=H, json={
        "mac": mac, "csv": csv, "mapping": {"Temprature": "tempf"}, "time_column": "when"})
    assert bad.status_code == 400
    good = client.post("/api/import/csv", headers=H, json={
        "mac": mac, "csv": csv, "mapping": {"Temperature": "tempf"}, "time_column": "when"})
    assert good.status_code == 200, good.text
    st = _wait_import()
    assert st["state"] == "done" and st["inserted"] == 1, st   # the blank row is dropped
    from app import db

    async def stored():
        async with db.connect() as conn:
            return [tuple(r) for r in await (await conn.execute(
                "SELECT dateutc_ms, tempf FROM observations WHERE mac=? "
                "AND dateutc_ms BETWEEN 1700000000000 AND 1701000000000",
                (mac,))).fetchall()]   # _make_device's own live reading is not ours
    assert asyncio.run(stored()) == [(1700100000000, 71.5)]


def test_a_bom_csv_imports_through_the_json_door_too():
    """PR #50 review: the header check dropped the BOM and the row parser
    did not, so a spreadsheet export passed validation and imported none."""
    t = "﻿when,Temperature\n1700100000,71.5\n"
    assert ai.mapping_problem({"Temperature": "tempf"}, ai.csv_header(t), "when") is None
    rows = list(ai.csv_rows(t, {"Temperature": "tempf"}, time_column="when"))
    assert [r["tempf"] for r in rows] == [71.5]


def test_a_wide_header_is_checked_in_one_pass():
    import time as _t
    header = [f"c{i}" for i in range(20000)]
    t0 = _t.perf_counter()
    assert ai.mapping_problem({"c1": "tempf"}, header, "c0") is None
    assert _t.perf_counter() - t0 < 0.2
