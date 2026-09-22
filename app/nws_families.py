"""Which kind of weather an NWS alert is about, and how loud it should be
(2.4, item 1).

Doren, 2026-09-19: a Flood Watch lit the widget's warning triangle and
pushed at him like a tornado would. He lives where flooding is somebody
else's problem and freezes are his, and the only control the app had was
one switch that turned every National Weather Service alert off.

So: eleven families the owner can mute one at a time, and a level, both
honoured EVERYWHERE the alerts surface — the push, the dashboard banner,
the widget's triangle, the watch — from one set of toggles.

CLASSIFICATION. NWS event names are a closed vocabulary of about eighty
products (api.weather.gov `/alerts/types`), each ending in the product
class: Warning, Watch, Advisory, Statement, Emergency, Outlook. The
family comes from the rest of the name, matched against ordered
substring rules — ORDER IS THE WHOLE TRICK, because the vocabulary
reuses words across families:

    "Storm Warning"          marine   (a gale-force marine product)
    "Winter Storm Warning"   winter
    "Ice Storm Warning"      winter
    "Tropical Storm Warning" tropical
    "Severe Thunderstorm …"  thunderstorm
    "Dust Storm Warning"     wind

Every rule is therefore checked most-specific first, and the table is
pinned by a test that walks the real product list. An event nobody
thought of lands in `other`, which is a family the owner can mute like
any other — never silently dropped and never silently promoted.

LEVEL. The product class is the level: a Warning means it is happening
or about to, a Watch means conditions are coming together, an Advisory
means inconvenience rather than danger. Today every relayed alert is
pushed at the `warning` tier — time-sensitive, through quiet hours, for
a Frost Advisory as readily as for a tornado.
"""
from __future__ import annotations

# The families, in the order the app lists them. `other` is last and is
# deliberately a real family rather than a bin: an owner who wants only
# tornado warnings needs to be able to mute it.
FAMILIES: tuple[str, ...] = (
    "tornado", "thunderstorm", "flood", "tropical", "winter",
    "heat_cold", "wind", "fire", "air", "marine", "other",
)

# Human labels, so the API can hand the apps the list rather than every
# client keeping its own copy of the spelling.
LABELS: dict[str, str] = {
    "tornado": "Tornado",
    "thunderstorm": "Thunderstorm",
    "flood": "Flood",
    "tropical": "Tropical",
    "winter": "Winter",
    "heat_cold": "Heat & cold",
    "wind": "Wind & dust",
    "fire": "Fire weather",
    "air": "Air quality",
    "marine": "Marine & coastal",
    "other": "Everything else",
}

# (family, substrings) in decreasing specificity. A rule matches when any
# of its substrings appears in the lowercased event name.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Named first: these words appear inside other families' products.
    ("tornado", ("tornado",)),
    # A marine product that wears the word "hurricane" without being a
    # tropical cyclone: winds of that force over water, from any cause.
    ("marine", ("hurricane force wind",)),
    ("tropical", ("hurricane", "tropical storm", "tropical depression",
                  "typhoon", "storm surge")),
    ("winter", ("winter storm", "ice storm", "winter weather", "blizzard",
                "snow", "sleet", "freezing rain", "freezing drizzle",
                "ice ", "avalanche", "lake effect")),
    ("thunderstorm", ("thunderstorm", "severe weather statement",
                      "special marine warning")),
    ("flood", ("flood", "debris flow", "dam ", "levee", "hydrologic",
               "seiche")),
    ("heat_cold", ("heat", "wind chill", "extreme cold", "cold weather",
                   "freeze", "frost")),
    # "Fire Warning" is its own product (a fire threatening a populated
    # area), and it was landing in `other` — so muting Fire left it
    # audible (CodeRabbit, PR #40). Bare "fire" is safe here: nothing
    # else in the vocabulary carries the word.
    ("fire", ("fire", "red flag")),
    # Dust belongs to Wind & dust, not to Air quality: the hazard is the
    # wall you cannot drive through, not the week of bad air.
    ("air", ("air quality", "air stagnation", "smoke", "ashfall")),
    # The wind products whose names collide with the marine rule below:
    # "Dust Storm Warning" is not a marine storm, and an owner who mutes
    # Marine because they live in Arizona still wants the dust wall.
    ("wind", ("dust storm", "blowing dust", "extreme wind", "lake wind")),
    # Marine before the general wind rule: a "Gale Warning" and a bare
    # "Storm Warning" are wind
    # products, but they are the MARINE ones and a landlocked owner who
    # keeps Wind on does not want them.
    ("marine", ("marine", "small craft", "gale", "storm warning",
                "storm watch", "hurricane force wind", "surf", "rip current",
                "beach hazard", "tsunami", "coastal", "lakeshore",
                "low water", "freezing spray", "sea ", "waterspout")),
    ("wind", ("wind", "dust")),
)

# The product classes, most urgent first. The suffix of the event name.
LEVELS: tuple[str, ...] = ("warning", "watch", "advisory", "statement")


def event_name(event) -> str:
    """One place that turns whatever `properties.event` held into a
    string. NWS sends a string; a feed that sends anything else used to
    reach `.strip()` and abort the WHOLE polling pass for every station
    (the R7 R10 shape, one bad record killing the tick). CodeRabbit,
    PR #40."""
    if isinstance(event, str):
        return event.strip()
    # Anything else reads as NO name rather than as its repr: a list
    # holding "Tornado Warning" would otherwise classify as a tornado
    # through `str()`, which is an accident, not a reading. No name means
    # `other` and the loud level, which is the safe pair for a product
    # nobody can identify.
    return ""


def family(event: str | None) -> str:
    """The family an NWS event name belongs to. Unknown names are
    `other` — a family, not a hole."""
    name = event_name(event).lower()
    if not name:
        return "other"
    for fam, needles in _RULES:
        for needle in needles:
            if needle in name:
                return fam
    return "other"


def level(event: str | None) -> str:
    """The product class: warning | watch | advisory | statement.
    Anything that names none of them (an Emergency, an Outlook, a
    Statement by another name) is read as a warning — the loud side is
    the safe side for a product we do not recognise."""
    name = event_name(event).lower()
    for lv in ("warning", "watch", "advisory", "statement"):
        if name.endswith(lv):
            return lv
    # "Air Quality Alert" and its few siblings: an alert is the advisory
    # of a product line that never minted an Advisory.
    if name.endswith("alert"):
        return "advisory"
    if "emergency" in name:
        return "warning"
    return "warning"


# How loud each level is, in the delivery tiers alerts.py already has:
#   warning  time-sensitive, through quiet hours (a tornado)
#   major    through quiet hours, not time-sensitive (a watch: worth
#            waking for, not worth punching Focus for)
#   watch    an ordinary push (an advisory)
#   info     pushed by day, held overnight (a statement)
TIER_FOR_LEVEL: dict[str, str] = {
    "warning": "warning",
    "watch": "major",
    "advisory": "watch",
    "statement": "info",
}


def tier(event: str | None) -> str:
    """The delivery tier for one alert, from its product class."""
    return TIER_FOR_LEVEL.get(level(event), "warning")


def normalise_muted(raw) -> list[str]:
    """The stored mute list, cleaned: known family keys, in FAMILIES
    order, no duplicates. Anything else is dropped rather than stored —
    a typo must not silence a family nobody can see in the UI, and it
    must not travel to the app as a key it will not recognise."""
    if not isinstance(raw, (list, tuple, set)):
        return []
    have = {str(x) for x in raw}
    return [f for f in FAMILIES if f in have]


def allows(event: str | None, muted: list[str] | None,
           warnings_only: bool = False) -> bool:
    """Should this alert reach the owner at all?

    ONE rule. The push relay asks it here; the app's banner, the widget's
    triangle and the watch ask its Swift twin (`NWSAlertFamily.swift`,
    pinned rule for rule by `bin/tests/test_nws_family_parity.py`). A
    family that is muted is muted everywhere; a level below the floor is
    quiet everywhere."""
    if family(event) in (muted or ()):
        return False
    if warnings_only and level(event) != "warning":
        return False
    return True
