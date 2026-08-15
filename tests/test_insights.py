"""Insights rollups — incremental correctness, duplicate safety, rebuild
equivalence, endpoint gating."""
import datetime as dt

import pytest

_H = {"Authorization": "Bearer test-api-token"}
_ING = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture()
def insights_on(client, monkeypatch):
    """Enable the flag on the live settings object (env is fixed by the
    client fixture before app import; the flag is read per-call)."""
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    return client


def _post(client, ts: dt.datetime, tempf, rain_daily=None, feels=None):
    body = {"device": {"id": "AABBCCDDEEFF", "name": "Davis"},
            "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outdoor": {"tempf": tempf, "humidity": 40, "feels_like": feels},
            "wind": {"speed_mph": 3, "gust_mph": 10},
            "rain": ({"daily_in": rain_daily} if rain_daily is not None else {}),
            "pressure": {"relative_inhg": 29.9},
            "source": "test"}
    r = client.post("/ingest/custom", headers=_ING, json=body)
    assert r.status_code == 200, r.text


def test_rollups_fold_incrementally_and_skip_duplicates(insights_on):
    client = insights_on
    base = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.timezone.utc)
    _post(client, base, 100.0, rain_daily=0.1)
    _post(client, base + dt.timedelta(minutes=90), 110.0, rain_daily=0.3)
    # Same timestamp re-delivered: INSERT OR IGNORE skips the row, and the
    # rollup must not double-count its sum.
    _post(client, base, 100.0, rain_daily=0.1)

    r = client.get("/api/insights?mac=" + MAC, headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["day_count"] == 1
    year = body["years"][0]
    assert year["hottest"][1] == 110.0
    assert year["rain_total"] == pytest.approx(0.3)
    cal = body["calendar"]
    assert cal[0][1] == 110.0                       # daily high
    assert body["calendar_lo"][0][1] == 100.0       # daily low (Low heatmap)
    # TIMEZONE is pinned to UTC in conftest, so cells are exact: 12:00 →
    # hour 12, 13:30 → hour 13. A double-counted sum would break these.
    grid = body["diurnal_tempf"]
    assert grid[6][12] == 100.0
    assert grid[6][13] == 110.0


def test_ledger_tiers_and_percentiles(insights_on):
    client = insights_on
    base = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)
    for i, hi in enumerate([85, 95, 101, 106, 111, 90, 88, 92, 97, 99]):
        _post(client, base + dt.timedelta(days=i), float(hi))
    body = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    yr = body["years"][0]
    assert yr["tiers"]["100"] == 3                  # 101, 106, 111
    assert yr["tiers"]["110"] == 1
    assert yr["tiers"]["80"] == 10
    assert body["p90_high"] is not None
    assert body["monthly_normals"]["06"] == pytest.approx(96.4, abs=0.1)


def _past_mid_july() -> dt.datetime:
    """The most recent mid-July that is safely in the PAST (UTC).

    Mid-July RELATIVE to now, not hardcoded — a literal 2026-07-15 crosses
    ingest's ~400-day past horizon around 2027-08-19 and starts failing with
    a misleading 400 (R4-30). And not simply jan1+195d: from January to
    mid-July that lands in the FUTURE, where ingest clamps the timestamp to
    now and the asserted July grid cells silently move (CodeRabbit). Day 195
    keeps month cell 6; at most ~13 months old stays inside the horizon."""
    base = _recent_jan1().replace(hour=12) + dt.timedelta(days=195)
    if base > dt.datetime.now(dt.timezone.utc):
        base = base.replace(year=base.year - 1)
    return base


def test_diurnal_feels_grid(insights_on):
    client = insights_on
    base = _past_mid_july()
    _post(client, base, 100.0, feels=104.0)
    _post(client, base + dt.timedelta(minutes=30), 102.0, feels=108.0)
    # A row without an explicit feels_like still lands in the temp grid;
    # ingest derives a feels value for it, so feels_n advances too — the
    # assertion below only pins the explicit-values cell.
    body = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    assert body["diurnal_tempf"][6][12] == 101.0     # (100 + 102) / 2
    assert body["diurnal_feels"][6][12] == 106.0     # (104 + 108) / 2


def _recent_jan1() -> dt.datetime:
    """The most recent Jan 1 that is at least 10 days in the past (UTC).

    Dates for the season-spanning tests are computed RELATIVE TO NOW:
    hardcoded winter dates crossed ingest's ~400-day past horizon on a
    calendar boundary (2025-11-22 + 400 d = 2026-12-27) and the suite
    started failing with a misleading 400. Anchoring on the latest Jan 1
    keeps every date inside the horizon (a Jan 1 is at most ~366 days old,
    and the fall-side dates reach at most ~22 days before it) and out of
    the future (future timestamps clamp to now, which would silently move
    the asserted freeze dates)."""
    now = dt.datetime.now(dt.timezone.utc)
    jan1 = dt.datetime(now.year, 1, 1, 6, 0, tzinfo=dt.timezone.utc)
    if (now - jan1).days < 10:      # early January: spring-side dates would
        jan1 = jan1.replace(year=now.year - 1)  # land in the future
    return jan1


def test_cold_ledger_tiers_and_freeze_dates(insights_on):
    client = insights_on
    # One observation per day; the daily LOW is what the cold ledger reads.
    # A real winter season spanning the year boundary: fall freezes in
    # December of one calendar year, spring freezes in January of the next.
    jan1 = _recent_jan1()
    lows = [
        (jan1 - dt.timedelta(days=22), 32.0),  # freeze — first fall freeze, Dec
        (jan1 - dt.timedelta(days=12), 24.0),  # severe, Dec
        (jan1 + dt.timedelta(days=2), 28.0),   # hard freeze (spring side, Jan)
        (jan1 + dt.timedelta(days=5), 31.0),   # freeze — last spring freeze
        (jan1 + dt.timedelta(days=8), 41.0),   # chill only
    ]
    for ts, low in lows:
        _post(client, ts, low)
    body = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    assert body["cold_tiers"] == [45, 36, 32, 28, 25]
    assert body["p10_low"] is not None
    y_fall, y_spring = body["years"]
    assert y_fall["year"] == jan1.year - 1
    assert y_fall["cold"] == {"45": 2, "36": 2, "32": 2, "28": 1, "25": 1}
    assert y_fall["first_fall_freeze"] == lows[0][0].strftime("%Y-%m-%d")
    assert y_fall["last_spring_freeze"] is None
    assert y_spring["year"] == jan1.year
    assert y_spring["cold"] == {"45": 3, "36": 2, "32": 2, "28": 1, "25": 0}
    assert y_spring["last_spring_freeze"] == lows[3][0].strftime("%Y-%m-%d")
    assert y_spring["first_fall_freeze"] is None


def test_rain_gap_and_dry_streaks(insights_on):
    client = insights_on
    # Anchored just before the most recent Jan 1 (see _recent_jan1) so every
    # date is inside the ingest horizon, safely in the past, and the whole
    # window stays inside ONE calendar year (a now-relative base splits the
    # per-year streak assertion across years in early January).
    base = _recent_jan1() - dt.timedelta(days=15)
    rains = {0: 0.5, 3: 0.2}                 # rain days; the rest are dry
    for i in range(10):
        _post(client, base + dt.timedelta(days=i), 70.0,
              rain_daily=rains.get(i))
    body = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    assert body["last_rain_day"] == \
        (base + dt.timedelta(days=3)).strftime("%Y-%m-%d")
    assert body["last_rain_amount"] == pytest.approx(0.2)
    # Days 4..9 are dry: 6 calendar days since the last rain day.
    assert body["dry_streak_days"] == 6
    assert len(body["years"]) == 1
    # Two dry runs — days 1-2 (len 2) and days 4-9 (len 6): longest is 6.
    assert body["years"][0]["longest_dry_streak"] == 6
    assert "_dry_streak" not in body["years"][0]


def test_rain_gap_rain_on_last_day_and_no_rain(insights_on):
    client = insights_on
    base = _recent_jan1() - dt.timedelta(days=10)
    # Dry, dry, then rain on the newest day → streak is over (0 days).
    _post(client, base, 70.0)
    _post(client, base + dt.timedelta(days=1), 70.0)
    _post(client, base + dt.timedelta(days=2), 70.0, rain_daily=0.3)
    body = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    assert body["dry_streak_days"] == 0
    assert body["last_rain_day"] == \
        (base + dt.timedelta(days=2)).strftime("%Y-%m-%d")
    assert body["years"][0]["longest_dry_streak"] == 2

    # A record with NO rain at all: last_rain_* stay None and the streak
    # spans the whole record (3 calendar days here).
    other = "11:22:33:44:55:66"
    for i in range(3):
        b = {"device": {"id": "112233445566"},
             "timestamp_utc": (base + dt.timedelta(days=i))
                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
             "outdoor": {"tempf": 70.0, "humidity": 40},
             "wind": {}, "rain": {},
             "pressure": {"relative_inhg": 29.9}, "source": "test"}
        assert client.post("/ingest/custom", headers=_ING,
                           json=b).status_code == 200
    body = client.get("/api/insights?mac=" + other, headers=_H).json()
    assert body["last_rain_day"] is None
    assert body["last_rain_amount"] is None
    assert body["dry_streak_days"] == 3


def test_rebuild_matches_incremental(insights_on):
    client = insights_on
    base = dt.datetime(2026, 5, 1, 8, 0, tzinfo=dt.timezone.utc)
    for i in range(5):
        _post(client, base + dt.timedelta(hours=i * 6), 70.0 + i)
    before = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    r = client.post("/api/insights/rebuild", headers=_H)
    assert r.status_code == 200 and r.json()["rows"] == 5
    after = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    assert before["calendar"] == after["calendar"]
    assert before["years"] == after["years"]
    assert before["diurnal_tempf"] == after["diurnal_tempf"]


def test_insights_gated_when_disabled(client):
    _ = client  # flag defaults off in tests
    assert client.get("/api/insights?mac=" + MAC,
                      headers=_H).status_code == 404
    assert client.post("/api/insights/rebuild", headers=_H).status_code == 404
    # And rollups are NOT maintained while disabled.
    _post(client, dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc), 99.0)
    from app.config import settings
    import pytest as _p
    with _p.MonkeyPatch.context() as mp:
        mp.setattr(settings, "insights", True)
        body = client.get("/api/insights?mac=" + MAC, headers=_H).json()
        assert body["day_count"] == 0
        assert "rebuild" in body.get("hint", "")
        # rebuild folds the pre-flag history in.
        client.post("/api/insights/rebuild", headers=_H)
        assert client.get("/api/insights?mac=" + MAC,
                          headers=_H).json()["day_count"] == 1


def test_same_batch_duplicate_timestamps_fold_once(insights_on, monkeypatch):
    """A bulk insert containing the same dateutc twice stores ONE row
    (INSERT OR IGNORE, first wins) — the rollups must fold it exactly once
    (CodeRabbit: sums/averages double-counted)."""
    import asyncio
    from app import db as dbmod
    ts = int(dt.datetime(2026, 7, 20, 12, 0,
                         tzinfo=dt.timezone.utc).timestamp() * 1000)
    rows = [{"dateutc": ts, "tempf": 100.0, "humidity": 40.0},
            {"dateutc": ts, "tempf": 200.0, "humidity": 90.0}]   # dup, ignored
    added = asyncio.run(dbmod.insert_observations(MAC, rows))
    assert added == 1
    body = insights_on.get("/api/insights?mac=" + MAC, headers=_H).json()
    assert body["day_count"] == 1
    assert body["calendar"][0][1] == 100.0          # first row won
    assert body["diurnal_tempf"][6][12] == 100.0    # summed once, n=1


def test_jan1_yearly_fallback_not_counted(insights_on):
    client = insights_on
    # No dailyrainin; yearly counter resets on Jan 1 — the fallback delta
    # must not book the reset day as rain. Anchored on the most recent Jan 1
    # (see _recent_jan1: hardcoded dates go stale at the ingest horizon), and
    # every post + the final shape is ASSERTED — the old version posted
    # unchecked and guarded its only assertion with `if body["years"]`, so
    # once the posts started failing it passed vacuously forever.
    ts = _recent_jan1().replace(hour=10, minute=0)
    for minute, yearly in ((0, 20.0), (30, 0.0), (60, 0.05)):
        body = {"device": {"id": "AABBCCDDEEFF", "name": "Davis"},
                "timestamp_utc": (ts + dt.timedelta(minutes=minute))
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outdoor": {"tempf": 50.0, "humidity": 40},
                "wind": {}, "rain": {"yearly_in": yearly},
                "pressure": {"relative_inhg": 29.9}, "source": "test"}
        r = client.post("/ingest/custom", headers=_ING, json=body)
        assert r.status_code == 200, r.text
    body = client.get("/api/insights?mac=" + MAC, headers=_H).json()
    assert body["day_count"] == 1
    assert len(body["years"]) == 1, "yearly rollups vanished"
    assert body["years"][0]["rain_total"] == 0.0


def test_daily_series_endpoint(insights_on):
    """/api/insights/daily: per-day lo/hi/mean, oldest-first, days-capped —
    the sensor-drift card's data source."""
    client = insights_on
    # Anchor-relative for the same horizon reason as test_diurnal_feels_grid.
    base = _past_mid_july()
    _post(client, base, 100.0)
    _post(client, base + dt.timedelta(minutes=90), 110.0)
    _post(client, base + dt.timedelta(days=1), 90.0)

    r = client.get("/api/insights/daily?mac=" + MAC + "&days=60", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["mac"] == MAC
    series = body["series"]
    d0 = base.strftime("%Y-%m-%d")
    d1 = (base + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    assert [d[0] for d in series] == [d0, d1]
    day1 = series[0]
    assert day1[1] == 100.0 and day1[2] == 110.0
    assert day1[3] == pytest.approx(105.0)
    assert series[1][3] == pytest.approx(90.0)

    # days clamp is enforced by FastAPI validation.
    assert client.get("/api/insights/daily?mac=" + MAC + "&days=1",
                      headers=_H).status_code == 422


def test_daily_series_gated_by_flag(client):
    r = client.get("/api/insights/daily?mac=" + MAC, headers=_H)
    assert r.status_code == 404
