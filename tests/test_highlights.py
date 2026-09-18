"""Today's highlights (2.3): rankings against the station's own record."""
import asyncio
from datetime import date, timedelta

H = {"Authorization": "Bearer test-api-token"}


def _window(years=8, base_high=95.0, base_low=70.0, gust=20.0, rain=0.0, hum=40.0):
    rows = []
    for y in range(2026 - years, 2026):
        for d in range(5, 20):
            rows.append({"day": f"{y}-09-{d:02d}", "tempf_max": base_high + (d % 10),
                         "tempf_min": base_low + (d % 3), "windgustmph_max": gust + (d % 4),
                         "humidity_sum": (hum + (d % 6)) * 100, "humidity_n": 100,
                         "rain_total": rain if d != 12 else 0.4, "yearly_min": None, "yearly_max": None})
    return rows


def test_window_and_label(client):
    from app import highlights as hl
    keys = hl.window_keys(date(2026, 9, 12))
    assert keys[0] == "09-05" and keys[-1] == "09-19" and len(keys) == 15
    assert hl.window_keys(date(2026, 1, 3))[0] == "12-27"
    assert hl.window_label(date(2026, 9, 3)) == "early-September"
    assert hl.window_label(date(2026, 9, 12)) == "mid-September"
    assert hl.window_label(date(2026, 9, 28)) == "late-September"


def test_records_and_ranks(client):
    from app import highlights as hl
    today = {"tempf_max": 104.0, "tempf_min": 73.0, "gust_mph": 31.0, "rain_in": 0.0, "humidity_avg": 44.0}
    out = hl.compute(today=today, window=_window(), last_rain_day="2026-07-01", today_day=date(2026, 9, 12),
                     covered_days=72)
    ids = [h["id"] for h in out["highlights"]]
    assert out["years"] == 8 and out["window"] == "mid-September"
    assert out["last_rain_day"] == "2026-07-01"
    assert ids[0] == "high-record" and "Hottest mid-September day in 8 years: 104°F" == out["highlights"][0]["text"]
    assert "low-record" in ids and "gust-record" in ids and "dry-streak" in ids
    assert all(0 <= h["score"] <= 1 for h in out["highlights"])
    assert len(out["highlights"]) <= hl.MAX_LINES
    # Percentile phrasing, not a record: 103.5 beats nine highs in ten.
    mild = hl.compute(today={"tempf_max": 103.5, "tempf_min": 71.0, "gust_mph": 10.0,
                             "rain_in": 0.0, "humidity_avg": 41.0},
                      window=_window(), last_rain_day="2026-09-10", today_day=date(2026, 9, 12))
    assert [h["id"] for h in mild["highlights"]] == ["high-rank"]
    assert mild["highlights"][0]["text"].startswith("Hotter than ")


def test_an_ordinary_day_has_no_highlights_and_one_year_is_no_record(client):
    from app import highlights as hl
    plain = hl.compute(today={"tempf_max": 99.5, "tempf_min": 71.0, "gust_mph": 12.0,
                              "rain_in": 0.0, "humidity_avg": 42.0},
                       window=_window(), last_rain_day="2026-09-10", today_day=date(2026, 9, 12))
    assert plain["highlights"] == []
    one = hl.compute(today={"tempf_max": 120.0, "tempf_min": 90.0, "gust_mph": 60.0,
                            "rain_in": 2.0, "humidity_avg": 99.0},
                     window=_window(years=1), last_rain_day=None, today_day=date(2026, 9, 12))
    assert one["highlights"] == [] and one["years"] == 1
    # Absent readings rank nothing, never as zero.
    absent = hl.compute(today={"tempf_max": None, "tempf_min": None, "gust_mph": None,
                               "rain_in": None, "humidity_avg": None},
                        window=_window(), last_rain_day="2026-09-10", today_day=date(2026, 9, 12))
    assert absent["highlights"] == []


def test_rain_ends_a_streak_and_humidity_is_a_z_score(client):
    from app import highlights as hl
    wet = hl.compute(today={"tempf_max": 96.0, "tempf_min": 71.0, "gust_mph": 10.0,
                            "rain_in": 0.62, "humidity_avg": 80.0},
                     window=_window(), last_rain_day="2026-07-20", today_day=date(2026, 9, 12),
                     covered_days=53)
    ids = [h["id"] for h in wet["highlights"]]
    assert "rain-record" in ids and "streak-ends" in ids and "humid" in ids
    assert any("First rain in 54 days" == h["text"] for h in wet["highlights"])


def test_the_endpoint_answers_empty_for_a_station_without_record(client):
    from app import highlights as hl
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:51", "name": "New"},
                      "timestamp_utc": "2026-09-12T15:00:00Z",
                      "outdoor": {"tempf": 99}})
    r = client.get("/api/devices/AA:BB:CC:DD:EE:51/highlights", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["highlights"] == [] and body["years"] == 0 and body["window"]
    assert client.get("/api/devices/AA:BB:CC:DD:EE:51/highlights").status_code == 401


def test_the_day_must_have_happened_before_it_is_ranked(client):
    from app import highlights as hl
    hot = {"tempf_max": 104.0, "tempf_min": 73.0, "gust_mph": 31.0, "rain_in": 0.3, "humidity_avg": 80.0}
    # 00:40: the "high" is a night reading, the low is not in, the humidity
    # mean is a few samples. Only what only grows is ranked.
    night = hl.compute(today=hot, window=_window(), last_rain_day="2026-07-01",
                       today_day=date(2026, 9, 12), local_hour=0, covered_days=72)
    ids = {h["id"] for h in night["highlights"]}
    assert "high-record" not in ids and "low-record" not in ids and "humid" not in ids
    assert "gust-record" in ids and "streak-ends" in ids
    # 09:00: the overnight low is in, the high is not.
    morning = hl.compute(today=hot, window=_window(), last_rain_day="2026-07-01",
                         today_day=date(2026, 9, 12), local_hour=9)
    ids = {h["id"] for h in morning["highlights"]}
    assert "low-record" in ids and "high-record" not in ids
    # 16:00: everything.
    evening = hl.compute(today=hot, window=_window(), last_rain_day="2026-07-01",
                         today_day=date(2026, 9, 12), local_hour=16)
    assert {"high-record", "low-record", "humid"} <= {h["id"] for h in evening["highlights"]}


def test_a_cold_day_is_a_record_too(client):
    from app import highlights as hl
    cold = hl.compute(today={"tempf_max": 80.0, "tempf_min": 60.0, "gust_mph": 5.0,
                             "rain_in": 0.0, "humidity_avg": 42.0},
                      window=_window(), last_rain_day="2026-09-10", today_day=date(2026, 9, 12))
    ids = [h["id"] for h in cold["highlights"]]
    assert "cool-record" in ids and "low-record" in ids
    assert any(h["text"].startswith("Coolest mid-September day in 8 years") for h in cold["highlights"])
    assert not any("100%" in h["text"] for h in cold["highlights"])


def _rollup_rows(db, mac, rows):
    """Insert daily_rollups rows directly: (day, rain_total, yearly_rise, tempf_max)."""
    async def go():
        async with db.connect() as conn:
            for day, total, rise, tmax in rows:
                await conn.execute(
                    "INSERT INTO daily_rollups (mac, day, rain_total, yearly_rise, "
                    "yearly_first, yearly_last, tempf_max, tempf_min) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (mac, day, total, rise, 0.0, rise, tmax, tmax - 25))
            await conn.commit()
    asyncio.run(go())


def test_a_counter_only_station_raining_today_is_not_a_dry_streak(client, monkeypatch):
    """R23: a WH24 or Atlas reports only a lifetime counter, so the day's
    dailyrainin aggregate is None and `or 0.0` printed "No rain in 42
    days" while it rained. Today's rain comes from today's own rollup row
    the one way the repo reads a day's rain (yearly_rise here)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app import config, db, highlights as hl
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    monkeypatch.setattr(hl.settings, "timezone", "America/Phoenix")
    mac = "AA:BB:CC:DD:EE:52"
    today = date(2026, 9, 12)
    last_wet = today - timedelta(days=42)
    rows = [(last_wet.isoformat(), None, 0.31, 100.0)]
    # Dry counter days between, then today with a 0.20 in rise so far.
    for i in range(1, 42):
        rows.append(((last_wet + timedelta(days=i)).isoformat(), None, 0.0, 100.0))
    rows.append((today.isoformat(), None, 0.20, 95.0))
    _rollup_rows(db, mac, rows)
    now_ms = int(datetime(2026, 9, 12, 16, 0, tzinfo=ZoneInfo("America/Phoenix")).timestamp() * 1000)
    out = asyncio.run(hl.assemble(mac, now_ms))
    texts = [h["text"] for h in out["highlights"]]
    assert "First rain in 42 days" in texts, texts
    assert not any(t.startswith("No rain") for t in texts)
    assert out["last_rain_day"] == last_wet.isoformat()
    # Nothing measured today (no row, no counter) is NEITHER streak line.
    unknown = hl.compute(today={"tempf_max": None, "tempf_min": None, "gust_mph": None,
                                "rain_in": None, "humidity_avg": None},
                         window=[], last_rain_day=last_wet.isoformat(), today_day=today)
    assert unknown["highlights"] == []


def test_one_prior_year_is_one_year_not_two(client):
    """R23: the window carries this year's earlier days too, and they
    were counted as a year of record — one prior year printed "in 2
    years"."""
    from app import highlights as hl
    window = _window(years=1)                      # 2025 only
    window += [{"day": f"2026-09-{d:02d}", "tempf_max": 90.0, "tempf_min": 70.0,
                "windgustmph_max": 10.0, "humidity_sum": 4000, "humidity_n": 100,
                "rain_total": 0.0, "yearly_min": None, "yearly_max": None}
               for d in range(5, 12)]
    out = hl.compute(today={"tempf_max": 120.0, "tempf_min": 90.0, "gust_mph": 60.0,
                            "rain_in": 0.0, "humidity_avg": 40.0},
                     window=window, last_rain_day="2026-09-10", today_day=date(2026, 9, 12))
    assert out["years"] == 1 and out["highlights"] == []
    two = hl.compute(today={"tempf_max": 120.0, "tempf_min": 90.0, "gust_mph": 60.0,
                            "rain_in": 0.0, "humidity_avg": 40.0},
                     window=_window(years=2) + window[15:], last_rain_day="2026-09-10",
                     today_day=date(2026, 9, 12))
    assert two["years"] == 2
    assert two["highlights"][0]["text"] == "Hottest mid-September day in 2 years: 120°F"


def test_a_counter_only_station_ranks_its_rain_against_prior_years(client, monkeypatch):
    """R23: the window SELECT left out the counter columns, so every prior
    day of a counter-only station read as unmeasured and the station
    never got a rain line."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app import config, db, highlights as hl
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    monkeypatch.setattr(hl.settings, "timezone", "America/Phoenix")
    mac = "AA:BB:CC:DD:EE:53"
    rows = []
    for y in range(2018, 2026):
        for d in range(5, 20):
            rows.append((f"{y}-09-{d:02d}", None, 0.4 if d == 12 else 0.0, 95.0))
    rows.append(("2026-09-12", None, 0.62, 90.0))
    _rollup_rows(db, mac, rows)
    now_ms = int(datetime(2026, 9, 12, 16, 0, tzinfo=ZoneInfo("America/Phoenix")).timestamp() * 1000)
    out = asyncio.run(hl.assemble(mac, now_ms))
    ids = [h["id"] for h in out["highlights"]]
    assert "rain-record" in ids, out
    assert out["years"] == 8


def test_a_streak_needs_the_days_between_to_have_been_measured(client):
    """F03: the last wet day on record minus today is not a drought when
    the gauge (or the server) was off in between. Without coverage the
    card says when it last RECORDED rain, and never "First rain in"."""
    from app import highlights as hl
    dry = {"tempf_max": None, "tempf_min": None, "gust_mph": None, "rain_in": 0.0, "humidity_avg": None}
    today = date(2026, 9, 15)
    # The review's shape: no rows at all between August 1 and today.
    out = hl.compute(today=dry, window=[], last_rain_day="2026-08-01", today_day=today)
    assert [h["form"] for h in out["highlights"]] == ["last-rain"]
    line = out["highlights"][0]
    assert line["text"] == "Last recorded rain: August 1"
    assert line["args"] == {"date": "2026-08-01", "date_label": "August 1"}
    assert out["last_rain_day"] == "2026-08-01"
    # 39 of the 44 days between is under 90%: still only the record.
    out = hl.compute(today=dry, window=[], last_rain_day="2026-08-01", today_day=today, covered_days=39)
    assert [h["form"] for h in out["highlights"]] == ["last-rain"]
    # 40 of 44 is coverage: the streak is asserted.
    out = hl.compute(today=dry, window=[], last_rain_day="2026-08-01", today_day=today, covered_days=40)
    assert [h["text"] for h in out["highlights"]] == ["No rain in 45 days"]
    # Rain today after an unmeasured gap is not "First rain in 45 days".
    wet = dict(dry, rain_in=0.3)
    out = hl.compute(today=wet, window=[], last_rain_day="2026-08-01", today_day=today)
    assert out["highlights"] == []
    out = hl.compute(today=wet, window=[], last_rain_day="2026-08-01", today_day=today, covered_days=44)
    assert [h["text"] for h in out["highlights"]] == ["First rain in 45 days"]
    # A last wet day in another year says so.
    out = hl.compute(today=dry, window=[], last_rain_day="2025-12-20", today_day=today)
    assert out["highlights"][0]["text"] == "Last recorded rain: December 20, 2025"


def test_coverage_is_read_from_the_ledger(client, monkeypatch):
    """Through assemble: a station with a rollup row for every day since
    the last wet one has a streak; one with a 44-day hole in the ledger
    has only a last recorded rain."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app import config, db, highlights as hl
    monkeypatch.setattr(config.settings, "timezone", "UTC")
    monkeypatch.setattr(hl.settings, "timezone", "UTC")
    today = date(2026, 9, 15)
    last_wet = date(2026, 8, 1)
    now_ms = int(datetime(2026, 9, 15, 16, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)

    full = "AA:BB:CC:DD:EE:61"
    rows = [(last_wet.isoformat(), 0.4, None, 100.0)]
    rows += [((last_wet + timedelta(days=i)).isoformat(), 0.0, None, 100.0) for i in range(1, 45)]
    rows.append((today.isoformat(), 0.0, None, 95.0))
    _rollup_rows(db, full, rows)
    out = asyncio.run(hl.assemble(full, now_ms))
    assert [h["text"] for h in out["highlights"]] == ["No rain in 45 days"]

    hole = "AA:BB:CC:DD:EE:62"
    _rollup_rows(db, hole, [(last_wet.isoformat(), 0.4, None, 100.0),
                            (today.isoformat(), 0.0, None, 95.0)])
    out = asyncio.run(hl.assemble(hole, now_ms))
    assert [h["text"] for h in out["highlights"]] == ["Last recorded rain: August 1"]
    assert out["last_rain_day"] == last_wet.isoformat()


def test_every_line_carries_a_form_and_args_that_reproduce_its_text(client):
    """F04: the app formats a form it knows in the reader's units and
    falls back to `text`. The two must agree, and the args must be native
    numbers (°F, mph, inches, percent, whole years and days), not prose."""
    from app import highlights as hl
    seen: set[str] = set()
    cases = [
        ({"tempf_max": 104.0, "tempf_min": 73.0, "gust_mph": 31.0, "rain_in": 0.0, "humidity_avg": 44.0},
         "2026-07-01", 72),
        ({"tempf_max": 103.5, "tempf_min": 71.0, "gust_mph": 25.0, "rain_in": 0.0, "humidity_avg": 41.0},
         "2026-09-10", 1),
        ({"tempf_max": 94.0, "tempf_min": 69.0, "gust_mph": 10.0, "rain_in": 0.62, "humidity_avg": 80.0},
         "2026-07-20", 53),
        ({"tempf_max": 96.0, "tempf_min": 72.9, "gust_mph": 10.0, "rain_in": 0.3, "humidity_avg": 20.0},
         "2026-07-20", 53),
        ({"tempf_max": 96.0, "tempf_min": 70.5, "gust_mph": 10.0, "rain_in": 0.0, "humidity_avg": 41.0},
         "2026-07-20", None),
    ]
    for today, last_wet, cov in cases:
        out = hl.compute(today=today, window=_window(), last_rain_day=last_wet,
                         today_day=date(2026, 9, 12), covered_days=cov)
        for h in out["highlights"]:
            assert h["form"] in hl.FORMS
            assert hl.FORMS[h["form"]].format(**h["args"]) == h["text"]
            assert h["kind"] in ("warm", "cool", "alert", "info"), "kind is still the tone"
            for k, v in h["args"].items():
                if k in ("period", "date", "date_label"):
                    assert isinstance(v, str)
                else:
                    assert isinstance(v, (int, float)) and not isinstance(v, bool), (k, v)
            seen.add(h["form"])
    assert {"high-record-hot", "high-rank-hot", "low-record-warm", "gust-record",
            "rain-record", "rain-rank", "humid", "dry-air", "dry-streak", "streak-ends",
            "last-rain"} <= seen, seen
    # Every template's placeholders are the args its form is given: a
    # form the loop above did not reach still has a template that formats.
    assert set(hl.FORMS) == {"high-record-hot", "high-record-cool", "high-rank-hot", "high-rank-cool",
                             "low-record-warm", "low-record-cold", "low-rank-warm", "low-rank-cold",
                             "gust-record", "gust-rank", "rain-record", "rain-rank", "humid", "dry-air",
                             "dry-streak", "streak-ends", "last-rain"}
