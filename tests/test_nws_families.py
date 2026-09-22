"""NWS alert families and levels (2.4, item 1).

Doren, 2026-09-19: a Flood Watch lit the widget's warning triangle and
pushed at him like a tornado would, and the only control was one switch
that turned every National Weather Service alert off. These pin the
classification the per-family toggles rest on — above all the ORDER of
the rules, because the product vocabulary reuses words across families
("Storm Warning" is marine, "Winter Storm Warning" is not) and a
reordering that looks harmless silently re-files half a family.

The list below is the real product vocabulary from api.weather.gov's
`/alerts/types`, with the family each product belongs to.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import nws_families as nf  # noqa: E402

# (event, family). Every NWS product that is plausibly active for a
# backyard station, plus the ones whose NAMES collide across families.
PRODUCTS = [
    ("Tornado Warning", "tornado"),
    ("Tornado Watch", "tornado"),
    ("Severe Thunderstorm Warning", "thunderstorm"),
    ("Severe Thunderstorm Watch", "thunderstorm"),
    ("Severe Weather Statement", "thunderstorm"),
    ("Special Marine Warning", "thunderstorm"),
    ("Flash Flood Warning", "flood"),
    ("Flash Flood Watch", "flood"),
    ("Flash Flood Statement", "flood"),
    ("Flood Warning", "flood"),
    ("Flood Watch", "flood"),
    ("Flood Advisory", "flood"),
    ("Coastal Flood Warning", "flood"),
    ("Coastal Flood Advisory", "flood"),
    ("Lakeshore Flood Warning", "flood"),
    ("Hydrologic Outlook", "flood"),
    ("Dam Break Warning", "flood"),
    ("Debris Flow Warning", "flood"),
    ("Hurricane Warning", "tropical"),
    ("Hurricane Watch", "tropical"),
    ("Hurricane Local Statement", "tropical"),
    ("Tropical Storm Warning", "tropical"),
    ("Tropical Depression Local Statement", "tropical"),
    ("Typhoon Warning", "tropical"),
    ("Storm Surge Warning", "tropical"),
    ("Winter Storm Warning", "winter"),
    ("Winter Storm Watch", "winter"),
    ("Winter Weather Advisory", "winter"),
    ("Ice Storm Warning", "winter"),
    ("Blizzard Warning", "winter"),
    ("Snow Squall Warning", "winter"),
    ("Lake Effect Snow Warning", "winter"),
    ("Freezing Rain Advisory", "winter"),
    ("Avalanche Warning", "winter"),
    ("Extreme Heat Warning", "heat_cold"),
    ("Excessive Heat Warning", "heat_cold"),
    ("Heat Advisory", "heat_cold"),
    ("Extreme Cold Warning", "heat_cold"),
    ("Cold Weather Advisory", "heat_cold"),
    ("Wind Chill Advisory", "heat_cold"),
    ("Freeze Warning", "heat_cold"),
    ("Hard Freeze Warning", "heat_cold"),
    ("Frost Advisory", "heat_cold"),
    ("High Wind Warning", "wind"),
    ("High Wind Watch", "wind"),
    ("Wind Advisory", "wind"),
    ("Extreme Wind Warning", "wind"),
    ("Lake Wind Advisory", "wind"),
    ("Dust Storm Warning", "wind"),
    ("Blowing Dust Advisory", "wind"),
    ("Red Flag Warning", "fire"),
    # Its own product — a fire threatening a populated area — and it used
    # to land in `other`, so muting Fire left it audible (CodeRabbit).
    ("Fire Warning", "fire"),
    ("Fire Weather Watch", "fire"),
    ("Extreme Fire Danger", "fire"),
    ("Air Quality Alert", "air"),
    ("Air Stagnation Advisory", "air"),
    ("Dense Smoke Advisory", "air"),
    ("Ashfall Advisory", "air"),
    ("Small Craft Advisory", "marine"),
    ("Gale Warning", "marine"),
    ("Storm Warning", "marine"),
    ("Hurricane Force Wind Warning", "marine"),
    ("Marine Weather Statement", "marine"),
    ("High Surf Advisory", "marine"),
    ("Rip Current Statement", "marine"),
    ("Beach Hazards Statement", "marine"),
    ("Tsunami Warning", "marine"),
    ("Low Water Advisory", "marine"),
    ("Freezing Spray Advisory", "marine"),
    ("Special Weather Statement", "other"),
    ("Dense Fog Advisory", "other"),
    ("Child Abduction Emergency", "other"),
    ("Civil Danger Warning", "other"),
    ("Earthquake Warning", "other"),
    ("Volcano Warning", "other"),
    ("Hazardous Materials Warning", "other"),
    ("Law Enforcement Warning", "other"),
    ("Local Area Emergency", "other"),
    ("Evacuation Immediate", "other"),
    ("Shelter In Place Warning", "other"),
    ("911 Telephone Outage Emergency", "other"),
]


def test_every_product_lands_in_the_family_it_belongs_to():
    wrong = [(e, want, nf.family(e)) for e, want in PRODUCTS
             if nf.family(e) != want]
    assert not wrong, "misfiled: " + "; ".join(
        f"{e} wanted {w} got {g}" for e, w, g in wrong)


def test_the_name_collisions_the_order_exists_for():
    """The pairs that a re-ordering would quietly break."""
    assert nf.family("Storm Warning") == "marine"
    assert nf.family("Winter Storm Warning") == "winter"
    assert nf.family("Tropical Storm Warning") == "tropical"
    assert nf.family("Dust Storm Warning") == "wind"
    assert nf.family("Hurricane Warning") == "tropical"
    assert nf.family("Hurricane Force Wind Warning") == "marine"
    assert nf.family("Coastal Flood Advisory") == "flood"


def test_an_unknown_product_is_a_family_not_a_hole():
    # A product nobody thought of must be mutable like any other, never
    # silently dropped and never silently promoted.
    assert nf.family("Moon Landing Advisory") == "other"
    assert nf.family("") == "other"
    assert nf.family(None) == "other"


def test_levels_read_the_product_class():
    assert nf.level("Tornado Warning") == "warning"
    assert nf.level("Flood Watch") == "watch"
    assert nf.level("Frost Advisory") == "advisory"
    assert nf.level("Special Weather Statement") == "statement"
    # An Alert is the advisory of a line that never minted one.
    assert nf.level("Air Quality Alert") == "advisory"
    # An Emergency is the loudest thing the service sends.
    assert nf.level("Local Area Emergency") == "warning"
    # Unknown shapes read LOUD: the quiet side is the dangerous one.
    assert nf.level("Moon Landing") == "warning"


def test_tiers_make_a_watch_quieter_than_a_warning():
    """Today every relayed alert rides the `warning` tier — time
    sensitive, through quiet hours — for a Frost Advisory as readily as
    for a tornado."""
    assert nf.tier("Tornado Warning") == "warning"     # punches Focus
    assert nf.tier("Flood Watch") == "major"           # wakes, does not punch
    assert nf.tier("Frost Advisory") == "watch"        # an ordinary push
    assert nf.tier("Rip Current Statement") == "info"  # held overnight


def test_muted_families_are_cleaned_not_trusted():
    assert nf.normalise_muted(["marine", "flood"]) == ["flood", "marine"]
    # FAMILIES order, deduped, and a key nobody knows is dropped rather
    # than stored: a typo must not silence a family the UI cannot show.
    assert nf.normalise_muted(["marine", "marine", "nonsense"]) == ["marine"]
    assert nf.normalise_muted("marine") == []
    assert nf.normalise_muted(None) == []


def test_allows_is_the_one_rule_every_surface_asks():
    muted = ["flood", "marine"]
    assert nf.allows("Tornado Warning", muted) is True
    assert nf.allows("Flood Watch", muted) is False
    assert nf.allows("Small Craft Advisory", muted) is False
    # The level floor sits above the families, not inside them.
    assert nf.allows("Tornado Watch", muted, warnings_only=True) is False
    assert nf.allows("Tornado Warning", muted, warnings_only=True) is True
    assert nf.allows("Anything At All", None) is True


def test_every_family_has_a_label_and_nothing_else_does():
    assert set(nf.LABELS) == set(nf.FAMILIES)
    assert nf.FAMILIES[-1] == "other"
    assert all(nf.LABELS[f].strip() for f in nf.FAMILIES)


def test_an_event_that_is_not_a_string_never_kills_the_pass():
    """NWS sends a string. A feed that sends anything else used to reach
    `.strip()` and abort the WHOLE polling pass for every station — the
    R7 R10 shape, one bad record killing the tick (CodeRabbit, PR #40)."""
    from app.nws_watch import is_warning
    for junk in (5, 12.5, True, {"x": 1}, ["Tornado Warning"]):
        assert nf.family(junk) == "other"
        assert nf.level(junk) == "warning"
        assert nf.allows(junk, ["flood"]) is True
        assert is_warning(junk) is False
    # …and the one shape that IS a string still reads normally.
    assert nf.event_name("  Tornado Warning  ") == "Tornado Warning"


# The SAME literal table as `levelVectors` in
# ios/ZasderWeatherTests/NWSAlertFamilyTests.swift, in the same order;
# bin/tests/test_nws_family_parity.py refuses a drift between the two. The
# app once judged warnings-only by the suffix alone and hid an Outlook the
# server had pushed at the loud tier (2.4 review). None means no name.
LEVEL_VECTORS = [
    ("Tornado Warning", "warning"),
    ("Tornado Watch", "watch"),
    ("Frost Advisory", "advisory"),
    ("Special Weather Statement", "statement"),
    ("Air Quality Alert", "advisory"),
    ("Local Area Emergency", "warning"),
    ("Hazardous Weather Outlook", "warning"),
    ("Evacuation Immediate", "warning"),
    ("", "warning"),
    (None, "warning"),
]


def test_the_level_vectors_the_app_pins_too():
    for event, expected in LEVEL_VECTORS:
        assert nf.level(event) == expected, event
        assert nf.allows(event, [], warnings_only=True) is (expected == "warning")
