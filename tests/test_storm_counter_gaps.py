"""A counter that drops out for a reading or several and comes back does
not count rain twice (2.6 follow-up review, R26-04 remainder)."""
import pytest

from app import storm

Y, D = "yearlyrainin", "dailyrainin"


def _rain(rows, cols=(Y, D)):
    return storm.stitched_rain(rows, cols)


def test_the_reviewed_case_one_missing_reading():
    rows = [{Y: 10.0, D: 0.0}, {Y: 10.1, D: 0.0}, {Y: 10.1, D: None},
            {Y: 10.1, D: 0.0}, {Y: 10.1, D: 0.1}]
    assert _rain(rows) == pytest.approx(0.1)
    # Deleting a redundant measurement cannot create rain.
    assert _rain(rows[:2] + rows[3:]) == pytest.approx(0.1)


@pytest.mark.parametrize("gap", [1, 3, 6])
@pytest.mark.parametrize("lead", [Y, D])
def test_a_lagging_counter_back_after_a_gap_adds_nothing(gap, lead):
    lag = D if lead == Y else Y
    base = {Y: 10.0, D: 0.0}
    rows = [dict(base)]
    # The leader rises 0.10; the lagger is missing for `gap` rows, then
    # returns still at its old value, then catches up.
    up = {Y: 10.1, D: 0.1}
    rows.append({lead: up[lead], lag: base[lag]})
    rows += [{lead: up[lead], lag: None} for _ in range(gap)]
    rows.append({lead: up[lead], lag: base[lag]})
    rows.append({lead: up[lead], lag: up[lag]})
    assert _rain(rows) == pytest.approx(0.1)


def test_rain_while_one_counter_is_away_is_counted_once():
    rows = [{Y: 10.0, D: 0.0}, {Y: 10.05, D: None}, {Y: 10.1, D: None},
            {Y: 10.1, D: 0.1}, {Y: 10.2, D: 0.2}]
    assert _rain(rows) == pytest.approx(0.2)


def test_a_counter_back_after_a_reset_still_extends_the_total():
    # The daily counter leaves, the day rolls over while it is gone, and it
    # comes back near zero: it rejoins at the total and its new rain adds.
    rows = [{Y: None, D: 0.30}, {Y: None, D: 0.40}, {Y: 5.0, D: None},
            {Y: None, D: 0.0}, {Y: None, D: 0.15}]
    assert _rain(rows) == pytest.approx(0.25)


def test_a_counter_that_takes_over_still_extends_the_total():
    rows = [{Y: 10.0, D: None}, {Y: 10.2, D: None}, {Y: None, D: 0.5},
            {Y: None, D: 0.6}]
    assert _rain(rows) == pytest.approx(0.3)
