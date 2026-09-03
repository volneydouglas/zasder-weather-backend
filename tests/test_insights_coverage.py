"""Per-year coverage in the insights payload (2.0).

Every year-to-date comparison a client draws is only honest when the years
compared covered the same calendar window, and the client cannot see that
for itself: a prior year with no rain point before today's day-of-year looks
identical whether the station measured a dry spring or was still in its box.
The first reading is true, the second is a fabricated 0.00 in — the
absent-is-not-zero family again — so the server publishes the verdict.

The rule is `insights.comparable_to_date`, the SAME function the story
engine's ledger baseline uses. These tests pin both halves of that reuse:
the numbers, and the fact that the two consumers cannot drift apart.

"today" is pinned through `assemble(today=...)`, the seam the story engine
threads, so the anchor and the coverage window can never disagree across a
midnight boundary.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

MAC = "AA:BB:CC:00:00:C0"
H = {"Authorization": "Bearer test-api-token"}

TODAY = date(2026, 8, 30)          # day-of-year 242


@pytest.fixture()
def rollups(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    from app import db
    return db


def _seed(db, spans: list[tuple[date, date, float]], mac: str = MAC) -> None:
    """(first, last, rain-per-day) spans of rollup rows. Rain is a DAILY
    total; the year's cumulative series is built by assemble."""
    async def run():
        async with db.connect() as conn:
            for first, last, rain in spans:
                d = first
                while d <= last:
                    await conn.execute(
                        "INSERT OR REPLACE INTO daily_rollups "
                        "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n, "
                        " rain_total) VALUES (?,?,?,?,?,?,?)",
                        (mac, d.isoformat(), 60.0, 95.0, 95.0, 1, rain))
                    d += timedelta(days=1)
            await conn.commit()
    asyncio.run(run())


def _assemble(mac: str = MAC, today: date = TODAY) -> dict:
    from app import insights
    return asyncio.run(insights.assemble(mac, today=today))


def _years(payload: dict) -> dict[int, dict]:
    return {y["year"]: y for y in payload["years"]}


# ───────────────────── the two cases the client cannot tell apart ─────────

def test_a_full_year_with_no_rain_is_comparable_and_its_zero_is_real(rollups):
    """The station was there all spring and measured nothing. 0.00 in is the
    answer, and the payload says it may be quoted."""
    _seed(rollups, [(date(2025, 1, 1), date(2025, 12, 31), 0.0),
                    (date(2026, 1, 1), TODAY, 0.02)])
    years = _years(_assemble())

    prior = years[2025]
    assert prior["first_day"] == "2025-01-01"
    assert prior["days_to_date"] == 242
    assert prior["window_days_to_date"] == 242
    assert prior["coverage_to_date"] == 1.0
    assert prior["comparable_to_date"] is True
    # And the series really does carry a point at the anchor, holding zero.
    at_anchor = [p for p in prior["rain_series"] if p[0] <= "2025-08-30"][-1]
    assert at_anchor == ["2025-08-30", 0.0]


def test_a_year_the_station_joined_mid_season_is_not_comparable(rollups):
    """The same 0.00 in, and this time it would be a fabrication. The flag
    is what stops a card from claiming a drought nobody measured."""
    _seed(rollups, [(date(2025, 6, 3), date(2025, 12, 31), 0.10),
                    (date(2026, 1, 1), TODAY, 0.02)])
    years = _years(_assemble())

    prior = years[2025]
    assert prior["first_day"] == "2025-06-03"
    assert prior["days_to_date"] == 89          # Jun 3 → Aug 30
    assert prior["window_days_to_date"] == 242
    assert prior["coverage_to_date"] == pytest.approx(89 / 242, abs=1e-4)
    assert prior["comparable_to_date"] is False
    # first_day is what lets a client render "2025 from Jun 3" instead of
    # dropping the year with no explanation.
    assert prior["last_day"] == "2025-12-31"


def test_a_year_with_nothing_before_the_anchor_reports_zero_coverage(rollups):
    """The exact shape behind the bug: a prior year whose series has no
    point at all before today's day-of-year. Coverage is a measured zero
    here — the station recorded that year, just not this part of it — and
    the comparability flag is what the client reads, not the total."""
    _seed(rollups, [(date(2025, 9, 1), date(2025, 12, 31), 0.20),
                    (date(2026, 1, 1), TODAY, 0.02)])
    prior = _years(_assemble())[2025]
    assert prior["days_to_date"] == 0
    assert prior["coverage_to_date"] == 0.0
    assert prior["comparable_to_date"] is False
    assert prior["rain_total"] > 0, "the year DID rain, just not before Aug 30"
    assert not [p for p in prior["rain_series"] if p[0] <= "2025-08-30"]


def test_a_thin_prior_year_is_excluded_even_at_high_coverage(rollups):
    """The floor is absolute as well as relative: 80% of a three-week
    reference is still three weeks."""
    _seed(rollups, [(date(2025, 1, 1), date(2025, 1, 20), 0.0),
                    (date(2026, 1, 1), date(2026, 1, 20), 0.0)])
    years = _years(_assemble(today=date(2026, 1, 20)))
    assert years[2025]["coverage_to_date"] == 1.0      # fully covered...
    assert years[2025]["comparable_to_date"] is False  # ...and still too thin
    assert years[2026]["comparable_to_date"] is False


# ───────────────────────── the anchor seam ─────────────────────────

def test_the_pinned_anchor_fixes_the_window_and_the_verdict(rollups):
    """Coverage is measured against the SAME anchor the to-date counts stop
    at, so the two cannot disagree across a midnight boundary. Move the
    anchor and both move together."""
    _seed(rollups, [(date(2025, 1, 1), date(2025, 3, 31), 0.0),
                    (date(2026, 1, 1), TODAY, 0.0)])

    early = _years(_assemble(today=date(2026, 3, 31)))[2025]
    assert early["window_days_to_date"] == 90        # Jan 1 → Mar 31
    assert early["days_to_date"] == 90
    assert early["coverage_to_date"] == 1.0 and early["comparable_to_date"]

    late = _years(_assemble(today=TODAY))[2025]
    assert late["window_days_to_date"] == 242
    assert late["days_to_date"] == 90                # the station stopped
    assert late["comparable_to_date"] is False

    # Deterministic: the same pinned day gives the same answer every call.
    assert _years(_assemble())[2025] == late


def test_the_payload_names_the_rule_it_applied(rollups):
    """A client should be able to explain its own rendering rather than
    guessing at a threshold."""
    from app import insights
    _seed(rollups, [(date(2025, 1, 1), date(2025, 12, 31), 0.0),
                    (date(2026, 1, 1), TODAY, 0.0)])
    payload = _assemble()
    comp = payload["comparison"]
    assert comp["anchor"] == payload["ledger_anchor"] == "08-30"
    assert comp["reference_year"] == 2026
    assert comp["reference_days_to_date"] == 242
    assert comp["min_days"] == insights.COMPARISON_MIN_DAYS
    assert comp["min_coverage"] == insights.COMPARISON_COVERAGE


def test_a_leap_day_anchor_does_not_mark_a_full_year_partial(temp_env):
    """The one anchor that needs care. In a non-leap year the string window
    "everything ≤ 02-29" holds exactly the 59 days through Feb 28; a
    denominator of 60 would flag a fully-covered year partial every fourth
    year."""
    from app.insights import window_days_to_anchor
    assert window_days_to_anchor(2028, "02-29") == 60      # leap
    assert window_days_to_anchor(2027, "02-29") == 59      # no Feb 29 to reach
    assert window_days_to_anchor(2028, "03-01") == 61
    assert window_days_to_anchor(2027, "03-01") == 60
    assert window_days_to_anchor(2026, "12-31") == 365
    assert window_days_to_anchor(2026, "01-01") == 1


def test_edge_inputs_decline_rather_than_defaulting(temp_env):
    """Absent, empty and malformed all answer "no", never "yes by
    default" — the whole point of a positive-only signal."""
    from app.insights import comparable_to_date, window_days_to_anchor
    assert comparable_to_date(0, 300) is False
    assert comparable_to_date(300, 0) is False
    assert comparable_to_date(-5, 300) is False
    assert comparable_to_date(365, 365) is True
    # A malformed anchor yields no window, and no window yields no fraction.
    assert window_days_to_anchor(2026, "") == 0
    assert window_days_to_anchor(2026, "bogus") == 0


def test_an_empty_station_has_no_years_and_no_reference(rollups):
    payload = _assemble("11:22:33:44:55:66")
    assert payload["years"] == []
    assert payload["comparison"]["reference_year"] is None
    assert payload["comparison"]["reference_days_to_date"] == 0


# ───────────────────── one rule, two consumers ─────────────────────

def test_the_story_engine_and_the_payload_agree_about_the_same_year(
        rollups, monkeypatch):
    """The reuse, asserted rather than assumed. The year the heat ledger
    refuses to put in its baseline is exactly the year the payload flags as
    not comparable — two copies of this rule would eventually disagree
    about the same year on the same screen."""
    from app import climate, stories
    _seed(rollups, [(date(2024, 1, 1), date(2024, 12, 31), 0.0),
                    (date(2025, 6, 3), date(2025, 12, 31), 0.0),
                    (date(2026, 1, 1), TODAY, 0.0)])
    payload = _assemble()
    flags = {y["year"]: y["comparable_to_date"] for y in payload["years"]}
    assert flags == {2024: True, 2025: False, 2026: True}

    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    story = asyncio.run(stories.top_stories(
        MAC, families=[stories.FAMILY_CLIMATE]))["stories"][0]
    c = story["comparison"]
    # Exactly one prior year survived the check, and it is the one the
    # payload marked comparable — 2025 is quoted by neither consumer.
    assert c["of"] == 2, "a year the payload calls uncomparable got a vote"
    assert c["baseline_label"].startswith("2024")
    assert "2025" not in c["baseline_label"]


def test_the_coverage_keys_are_additive(rollups):
    """Older apps must decode this payload unchanged. Nothing was renamed,
    nothing was removed, and every new key is a fresh name."""
    _seed(rollups, [(date(2026, 1, 1), TODAY, 0.05)])
    payload = _assemble()
    yr = payload["years"][0]
    for key in ("year", "days", "days_to_date", "tiers", "tiers_to_date",
                "cold", "rain_total", "rain_series", "cdd", "hdd",
                "longest_dry_streak", "hottest", "coldest"):
        assert key in yr, key
    assert {"first_day", "last_day", "window_days_to_date",
            "coverage_to_date", "comparable_to_date"} <= set(yr)
    # The series shape the rain race decodes is untouched.
    assert yr["rain_series"][0] == ["2026-01-01", 0.05]


def test_the_endpoint_serves_the_coverage_fields(client, rollups):
    """Over the wire, on the station clock — no pinned anchor here, so the
    only claim is that the fields travel and stay self-consistent."""
    _seed(rollups, [(date(2025, 6, 3), date(2025, 12, 31), 0.0),
                    (date(2026, 1, 1), TODAY, 0.0)])
    r = client.get(f"/api/insights?mac={MAC}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["comparison"]["anchor"] == body["ledger_anchor"]
    for yr in body["years"]:
        assert isinstance(yr["comparable_to_date"], bool)
        assert yr["first_day"][:4] == str(yr["year"])
        assert yr["days_to_date"] <= yr["window_days_to_date"]
        if yr["coverage_to_date"] is not None:
            assert 0.0 <= yr["coverage_to_date"] <= 1.0
