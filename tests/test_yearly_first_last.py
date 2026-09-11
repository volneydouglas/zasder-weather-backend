"""The yearly counter's first/last reading per day (2.2, ref_rain_counters):
the day's rain from a lifetime counter is last - first, and a reset is a
fact (last < first), not the min/max signature guess.
"""
import asyncio

import pytest

from app.day_rain import day_rain_in


def test_first_last_beats_the_signature_guess():
    # A reset inside the day: min touches zero, max is the old total. The
    # old rule filed the day as unknown; first/last knows 0.30 in fell
    # after the swap.
    row = {"rain_total": None, "yearly_min": 0.05, "yearly_max": 42.10,
           "yearly_first": 42.10, "yearly_last": 0.05}
    assert day_rain_in(row) == pytest.approx(0.05)
    # Same day without the 2.2 columns: the signature rule still applies.
    old = {"rain_total": None, "yearly_min": 0.05, "yearly_max": 42.10}
    assert day_rain_in(old) is None
    # An ordinary day: last - first, and a dip below first-of-day from a
    # correction is not negative rain.
    assert day_rain_in({"yearly_first": 10.00, "yearly_last": 10.25}) == pytest.approx(0.25)
    assert day_rain_in({"yearly_first": 10.00, "yearly_last": 10.00}) == 0.0
    # A replaced gauge that jumps a season in one day is still implausible.
    assert day_rain_in({"yearly_first": 1.0, "yearly_last": 99.0}) is None
    # rain_total still wins when a daily total exists.
    assert day_rain_in({"rain_total": 0.5, "yearly_first": 1.0, "yearly_last": 3.0}) == 0.5


def test_rollup_keeps_first_and_last_by_reading_time(client, monkeypatch):
    """Rows fold out of order (a history import, a resumed relay): first
    and last follow the reading's own time, not arrival."""
    from app import db, insights, config
    monkeypatch.setattr(config.settings, "insights", True)
    monkeypatch.setattr(insights.settings, "insights", True)
    mac = "AA:BB:CC:00:00:77"
    noon = 1_756_728_000_000                       # 2026-09-01 12:00 UTC
    rows = [
        {"dateutc": noon, "yearlyrainin": 12.40},
        {"dateutc": noon - 3_600_000 * 4, "yearlyrainin": 12.10},   # earlier, arrives second
        {"dateutc": noon + 3_600_000 * 3, "yearlyrainin": 12.65},
        {"dateutc": noon + 3_600_000, "yearlyrainin": 12.50},       # between, arrives last
        {"dateutc": noon + 3_600_000 * 2, "tempf": 80.0},            # no counter: ignored
    ]

    async def run():
        async with db.connect() as conn:
            await insights.update_rollups(conn, mac, rows)
            await conn.commit()
            return await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?", (mac,))).fetchone()
    r = asyncio.run(run())
    assert (r["yearly_first"], r["yearly_first_ms"]) == (12.10, noon - 3_600_000 * 4)
    assert (r["yearly_last"], r["yearly_last_ms"]) == (12.65, noon + 3_600_000 * 3)
    assert (r["yearly_min"], r["yearly_max"]) == (12.10, 12.65)
    assert day_rain_in(r) == pytest.approx(0.55)
