"""Story engine (2.0): the server decides what is interesting about a
station's weather and hands the app a finished story.

Everything here reads the INSIGHTS rollups (through `insights.assemble`),
never the raw observations table — the reference station carries 1.15M+
rows and a share card is not worth a full scan. One assemble() per request
feeds every producer, so adding producers costs Python time, not queries.

Every story has the same shape:

    family · story type · hero statistic · comparison · supporting stats ·
    visualization spec · context sentence · station attribution

The four families are the taxonomy the design review settled on, and they
are also the app's grouping: `records` ("look what happened"), `climate`
("how unusual is this?"), `science` ("what does it mean?"), `sky` ("what's
happening around us?").

Cards take PLAIN INPUTS only (env objects off-tree crash ImageRenderer), so
every string a card shows — hero line, context sentence, stat labels — is
built HERE. A card template lays out what this module decided; it never
derives a number or writes a sentence.

Producers register with @producer(family, name), take a StoryContext, and
return zero or more Story objects carrying an `interestingness` score.
RETURNING NOTHING IS A NORMAL OUTCOME, not an error: a station with no
solar sensor is not a station with zero sun, and a three-week-old station
has no heat ledger. A producer that cannot tell an HONEST story declines,
and the engine ranks whatever is left.

Units are the stored API-native ones (°F, mph, inHg, inches). Every Stat
carries its unit token, and every threshold in this module is Fahrenheit
because that is what daily_rollups holds — display conversion belongs to
the client, and a constant compared against a converted value is the bug
this repo keeps re-shipping.

Copy honesty, each rule paid for once already:
- the ledger counts `hi >= tier`, so the words are "≥100°F" / "100°F or
  hotter" — never "above 100°F";
- a still-running year is labelled "so far", never "all year";
- a comparison whose baseline is missing is absent, not zero.

Imports of `settings`, `db`, `insights` and `climate` are function-local on
purpose: the test suite reloads those modules per test, and a module-level
binding here would pin the FIRST test's objects (the same trap conftest
documents for app.apns / app.relay).
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger("zasder.stories")

FAMILY_RECORDS = "records"     # "look what happened"
FAMILY_CLIMATE = "climate"     # "how unusual is this?"
FAMILY_SCIENCE = "science"     # "what does it mean?"
FAMILY_SKY     = "sky"         # "what's happening around us?"
FAMILIES = (FAMILY_RECORDS, FAMILY_CLIMATE, FAMILY_SCIENCE, FAMILY_SKY)

# Unit tokens name the unit the value IS IN — what the story actually
# rendered, not what the database stores. Storage stays API-native (°F, mph,
# inches, inHg) everywhere else in this repo; a story is the one payload
# that has already been converted, because a story is finished prose.
#
# ⚠️ WHY THE CONVERSION HAPPENS HERE AND NOT IN THE APP (field defect,
# 2026-08-30): every string a card shows is written server-side — that is
# the design rule, because ImageRenderer cannot take env objects off-tree.
# Those strings BAKE THE UNIT INTO THE WORDS: "108 DAYS ≥100°F". A client
# that converted the numbers would put "44°C" beside "≥100°F" in the same
# picture, which is worse than not converting at all. The card is generated
# in the reader's units or it is not generated. So the request carries the
# preference, `Units` converts at the moment of FORMATTING, and every
# threshold in this module stays Fahrenheit right up to that moment —
# comparing a constant against a converted value is the bug this repo keeps
# re-shipping, and this is exactly the place it would happen.
UNIT_F = "F"
UNIT_DAYS = "days"
# Nights, for the cold ledger. The same count as UNIT_DAYS — one rollup day
# carries one low — but a card that says "18 days ≤32°F" is describing
# something that happened while people were asleep. The noun is the honest
# one, and it costs a client nothing: units convert temperatures, never the
# name of the thing being counted.
UNIT_NIGHTS = "nights"
UNIT_MPH = "mph"
UNIT_IN = "in"
UNIT_IN_HR = "in/hr"
UNIT_INHG = "inHg"
UNIT_PCT = "%"
# Feet, and feet only. Density altitude is DEFINED in feet by the NWS chain
# `derived.density_altitude_ft` implements and the Science screen already
# renders it that way, and the app's unit preferences carry no altitude axis
# — there is nothing for a client to send. Inventing one here would put a
# fifth spelling into a contract the app cannot speak yet; a reader on
# Celsius therefore sees a Celsius temperature beside a foot altitude, which
# is what the Science screen has always shown them.
UNIT_FT = "ft"
# Minutes and seconds, and for the same reason as feet: a duration has no
# imperial/metric axis to convert along, so there is nothing for a client to
# send and nothing for `Units` to decide. Day length, the moon-free dark
# window and "146 seconds of daylight lost today" are all rendered in these
# two, in every scale the app offers.
UNIT_MIN = "min"
UNIT_SEC = "s"
# A dimensionless INDEX — Fosberg, the Chandler Burning Index. It has no
# imperial/metric axis at all: the formulae are defined on °F and °C
# respectively and both emit a bare number on their own published scale, so
# there is nothing for `Units` to convert and nothing for a client to send.
# The inputs it was computed FROM are converted, and are carried separately.
UNIT_INDEX = "index"

# A story about a period the station barely covered is not a story. Below
# this many rollup days the heat producer declines rather than presenting a
# ledger of near-zeros that reads like a cool year (absent is not zero).
MIN_LEDGER_DAYS = 30

# A prior year only earns a place in a to-date comparison when it covered a
# comparable share of the same calendar window. That rule lives in
# `insights.comparable_to_date` — ONE definition, because the /api/insights
# payload publishes the same verdict per year for clients to render, and two
# copies of "is this year comparable" would eventually disagree about the
# same year on the same screen.

# ── Wildest Day tuning ───────────────────────────────────────────────────
# Every dimension is scored against the STATION'S OWN distribution, so these
# are counts and shares, never weather values. A wild day in Chandler is not
# a wild day in Irwin PA and no absolute threshold could be right in both.

# Below this many rollup days there is no station distribution to rank
# against, only a handful of numbers that would each look like a record.
MIN_STORY_DAYS = 30

# A dimension whose above-floor pool is thinner than this cannot be
# normalized at all and is DROPPED station-wide (not scored as zero). Three
# is deliberately low: a desert station may have four rainy days on record
# and rain is exactly the dimension its wildest day turns on.
MIN_DIM_POOL = 3

# A monthly normal wants a month's worth of that month. Under this the
# anomaly dimension is absent for those days rather than measured against a
# "normal" that is really one week of weather.
MIN_NORMAL_DAYS = 10

# The line a dimension crosses to count as one the day OWNS — its share of
# the station's own above-floor pool. Breadth counts these; a day clearing
# none of them is not a wild day and the scope declines.
WILD_NOTABLE = 0.70

# storm_history keeps the newest 50 episodes per station (db.record_storm
# prunes to that), so this is the whole table for one station, not a scan.
STORM_LOOKBACK = 50

# The month scope's claim when it names the same date the year scope already
# named is TRUE but not NEW — the larger period already said it. The month
# story still ships (the monthly card fetches that scope by name) and simply
# ranks below its own superset in Worth Sharing.
REDUNDANT_SCOPE = 0.95


# ───────────────────────── display units ─────────────────────────
#
# The VALUE SPELLINGS are the app's own enum raw values (TempUnit.celsius,
# WindUnit.kph, RainUnit.mm, PressureUnit.hPa …) so the client sends what it
# already has in hand rather than translating into a second vocabulary the
# two sides could drift apart on. The TOKENS this emits are display labels
# ("°C" reads as "C", "km/h", "mm", "hPa"), which is what `Stat.unit` now
# means.

# name → (Stat token, the suffix copy uses)
TEMP_UNITS = {"fahrenheit": ("F", "°F"), "celsius": ("C", "°C")}
WIND_UNITS = {"mph": ("mph", "mph"), "kph": ("km/h", "km/h"),
              "ms": ("m/s", "m/s"), "knots": ("kt", "kt"),
              "beaufort": ("Bft", "Bft")}
RAIN_UNITS = {"inches": ("in", "in"), "mm": ("mm", "mm")}
PRESSURE_UNITS = {"inHg": ("inHg", "inHg"), "hPa": ("hPa", "hPa")}


@dataclass(frozen=True)
class Units:
    """What the reader's app is set to. Defaults reproduce the API-native
    rendering exactly, so a request that names no preference gets byte-for-
    byte what it got before this existed."""
    temperature: str = "fahrenheit"
    wind: str = "mph"
    rain: str = "inches"
    pressure: str = "inHg"

    # ── temperature ──────────────────────────────────────────────────────
    @property
    def temp_token(self) -> str:
        return TEMP_UNITS[self.temperature][0]

    @property
    def temp_suffix(self) -> str:
        return TEMP_UNITS[self.temperature][1]

    def temp(self, f: float) -> float:
        """A READING, offset and all."""
        return f if self.temperature == "fahrenheit" else (f - 32.0) * 5.0 / 9.0

    def temp_delta(self, f: float) -> float:
        """A DIFFERENCE — anomaly, departure, swing, range. Scale only: run
        a delta through the reading conversion and a +2°F anomaly becomes
        −16.7°C, which is the same trap the app's own TempUnit.delta exists
        to close."""
        return f if self.temperature == "fahrenheit" else f * 5.0 / 9.0

    def temp_text(self, f: float) -> str:
        return _sig(self.temp(f))

    def temp_deg(self, f: float) -> str:
        return f"{_sig(self.temp(f))}{self.temp_suffix}"

    def temp_delta_deg(self, f: float) -> str:
        return f"{_sig(self.temp_delta(f))}{self.temp_suffix}"

    # ── rain ─────────────────────────────────────────────────────────────
    @property
    def rain_token(self) -> str:
        return RAIN_UNITS[self.rain][0]

    @property
    def rate_token(self) -> str:
        return f"{RAIN_UNITS[self.rain][0]}/hr"

    @property
    def rain_precision(self) -> int:
        # Two decimals of an inch, ONE of a millimetre. Not zero: the
        # rain-day definition this engine states out loud is 0.01 in, which
        # is 0.3 mm, and an integer millimetre would print the card's own
        # threshold as "0 mm".
        return 2 if self.rain == "inches" else 1

    def rain_value(self, inches: float) -> float:
        return inches if self.rain == "inches" else inches * 25.4

    def rain_text(self, inches: float) -> str:
        return f"{self.rain_value(inches):.{self.rain_precision}f}"

    def rain_amount(self, inches: float) -> str:
        return f"{self.rain_text(inches)} {self.rain_token}"

    # ── pressure ─────────────────────────────────────────────────────────
    @property
    def pressure_token(self) -> str:
        return PRESSURE_UNITS[self.pressure][0]

    @property
    def pressure_precision(self) -> int:
        return 2 if self.pressure == "inHg" else 1

    def pressure_value(self, inhg: float) -> float:
        return inhg if self.pressure == "inHg" else inhg * 33.8639

    def pressure_text(self, inhg: float) -> str:
        return f"{self.pressure_value(inhg):.{self.pressure_precision}f}"

    def pressure_amount(self, inhg: float) -> str:
        return f"{self.pressure_text(inhg)} {self.pressure_token}"

    # ── wind ─────────────────────────────────────────────────────────────
    @property
    def wind_token(self) -> str:
        return WIND_UNITS[self.wind][0]

    def wind_value(self, mph: float) -> float:
        if self.wind == "mph":
            return mph
        if self.wind == "kph":
            return mph * 1.609344
        if self.wind == "ms":
            return mph * 0.44704
        if self.wind == "knots":
            return mph * 0.868976
        # Beaufort from speed: B = (v[m/s] / 0.836)^(2/3), capped at force 12
        # — the top of the standard scale. The app caps at display time; a
        # story IS display time, so the cap belongs here.
        ms = mph * 0.44704
        return 0.0 if ms <= 0 else min(12.0, (ms / 0.836) ** (2.0 / 3.0))

    # ── distance ─────────────────────────────────────────────────────────
    # The app carries NO distance preference: temperature, wind, rain and
    # pressure are the four axes it has, and the lightning tile and chart
    # have shipped "N mi away" unconverted since 1.8 (ChartsView notes it as
    # a known gap). A story is rendered whole in the reader's units or not
    # at all, so a strike distance BORROWS THE RAIN AXIS: inches means the
    # reader lives in miles, millimetres means kilometres. It is the same
    # imperial/metric split and the only one the request can express.
    @property
    def distance_token(self) -> str:
        return "mi" if self.rain == "inches" else "km"

    def distance_value(self, miles: float) -> float:
        return miles if self.rain == "inches" else miles * 1.609344

    def distance_amount(self, miles: float) -> str:
        return f"{self.distance_value(miles):.1f} {self.distance_token}"


UNITS_NATIVE = Units()


def parse_units(temperature: str | None = None, wind: str | None = None,
                rain: str | None = None, pressure: str | None = None) -> Units:
    """Query strings → Units. Raises ValueError naming the offending
    parameter, so the endpoint can 400 the same way the family filter does
    rather than silently rendering the wrong scale."""
    for name, value, table in (("temp_unit", temperature, TEMP_UNITS),
                               ("wind_unit", wind, WIND_UNITS),
                               ("rain_unit", rain, RAIN_UNITS),
                               ("pressure_unit", pressure, PRESSURE_UNITS)):
        if value is not None and value not in table:
            raise ValueError(
                f"unknown {name} {value!r}: one of {list(table)}")
    return Units(temperature=temperature or UNITS_NATIVE.temperature,
                 wind=wind or UNITS_NATIVE.wind,
                 rain=rain or UNITS_NATIVE.rain,
                 pressure=pressure or UNITS_NATIVE.pressure)


# ───────────────────────── schema ─────────────────────────

@dataclass(frozen=True)
class Stat:
    """One number with everything needed to render it and nothing else.

    `value` may be None — that is the honest representation of a reading the
    station never took. Renderers must show a dash, never a zero.
    """
    key: str
    label: str
    value: float | int | None
    unit: str | None = None
    precision: int = 0


@dataclass(frozen=True)
class Comparison:
    """The "compared to what?" half of a story.

    A story with no trustworthy baseline carries `comparison: null` — the
    engine never fabricates a zero baseline to keep the field populated.
    """
    kind: str                       # e.g. "prior_years_to_date"
    label: str                      # "vs the same date in 2 earlier years"
    value: float
    baseline: float | None
    baseline_label: str
    direction: str                  # "above" | "below" | "level"
    delta: float | None
    delta_pct: float | None         # None when the baseline is 0 — not ∞
    rank: int | None = None         # 1 = highest of `of` comparable periods
    of: int | None = None
    # The rank as a FINISHED SENTENCE. "#1 of 973 days" is one of the most
    # compelling things this engine knows and the card could not render it:
    # composing that line client-side is precisely what the plain-inputs
    # rule forbids, so the card was dropping the field. Written here, once,
    # beside the numbers it describes.
    rank_line: str | None = None


@dataclass(frozen=True)
class Viz:
    """What to draw. `kind` names an app-side template; `series` is already
    ordered the way the template should draw it, so the card iterates and
    lays out without deciding anything.

    Two string fields carry the words a chart needs, both written here for
    the same reason every other string is: the card may lay out prose, never
    compose it.

    · `footnote` — one sentence about the CHART, printed under it.
    · a series entry's optional `note` — a short phrase about THAT ROW,
      printed on or beside it. It exists because a template that wanted to
      mark a bar as still running could only reach for a glyph, and a glyph
      is a word the client invented. `"still running"` beside the bar is the
      producer saying it in its own voice.

    Every entry also carries a stable string `key`; clients match on
    `highlight_key` rather than `highlight`, which is polymorphic (a year as
    a NUMBER in one template, a band NAME in another) and therefore does not
    compare equal to the key it names.
    """
    kind: str
    series: list[dict[str, Any]]
    unit: str | None = None
    axis_label: str | None = None
    # `highlight` is polymorphic across templates for historical reasons (a
    # number for ledger_pyramid, a string for chaos_dimensions). Every entry
    # now carries a stable string `key` and `highlight_key` names that entry,
    # so a template can find the hero bar with one spelling. New templates
    # should read `highlight_key`; `highlight` stays for the ones that
    # already shipped against it.
    highlight: Any = None           # the series entry the hero came from
    highlight_key: str | None = None
    # THE FULL EXTENT OF THE AXIS, in `unit`, when the rows do not imply it.
    #
    # A template that sizes its ruler from the rows is really assuming the
    # last row sits at the end of the span, and that assumption is invisible
    # until the day it breaks. `night_timeline` is the case that found it:
    # the ruler ran 0..last point, which matched the night's length only
    # because first light happened to be the final event — on a night whose
    # last drawn point is a moonset the ruler would quietly short itself and
    # every position on it would shift. The producer knows the true span, so
    # the producer states it. None where the rows genuinely are the domain.
    domain_max: float | None = None
    # ONE SENTENCE EXPLAINING A VISUAL DISTINCTION THE CHART MAKES.
    #
    # The plain-inputs rule forbids a card from writing prose, and that rule
    # had a hole in it: a producer could mark a row `comparable: false`, the
    # template could correctly grey it, and the finished IMAGE would carry a
    # faded bar with nothing anywhere saying why (field report from the card
    # templates, 2026-08-30). A shareable picture that draws a distinction
    # the viewer cannot interpret is exactly the quiet ambiguity this engine
    # exists to remove.
    #
    # So: any producer whose series can contain rows the reader must read
    # differently writes the explanation HERE, in its own voice, and the
    # template prints it verbatim under the chart. None when every row means
    # the same thing — an unconditional legend is noise.
    footnote: str | None = None


# Every span a story may claim. Listed HERE, on the dataclass, because the
# app's `StoryPeriod` decodes `kind` as a plain string and a producer that
# invented "station" would ship a card whose period nobody could name. The
# suite checks every response against this set (tests/story_contract.py).
PERIOD_KINDS = frozenset({"year", "month", "spell", "water_year", "moment",
                          "all"})


@dataclass(frozen=True)
class Period:
    """What span the story covers. `partial` is load-bearing copy: a year
    still running is labelled "so far" everywhere it is named."""
    kind: str                       # one of PERIOD_KINDS
    label: str
    start: str | None
    end: str | None
    partial: bool = False


@dataclass(frozen=True)
class Story:
    id: str                         # stable across runs; also the sort tiebreak
    family: str
    story_type: str                 # names the app-side card template
    title: str
    emoji: str | None
    hero: Stat
    hero_line: str                  # the rendered headline, card-ready
    context: str                    # the sentence under the hero
    comparison: Comparison | None
    supporting: list[Stat]
    viz: Viz | None
    period: Period
    station: dict[str, Any]
    interestingness: float          # 0..1, comparable ACROSS producers
    disclaimer: str | None = None   # science-family cards render this on the image
    score_parts: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ───────────────────────── registry ─────────────────────────

StoryProducer = Callable[["StoryContext"], Awaitable[list[Story]]]
_REGISTRY: list[StoryProducer] = []


def producer(family: str, name: str):
    """Register a producer. Family is checked at import time so a typo is a
    startup failure, not a story that quietly never groups anywhere."""
    if family not in FAMILIES:
        raise ValueError(f"unknown story family {family!r}")

    def deco(fn: StoryProducer) -> StoryProducer:
        fn.story_family = family        # type: ignore[attr-defined]
        fn.story_name = name            # type: ignore[attr-defined]
        _REGISTRY.append(fn)
        return fn
    return deco


def registered() -> list[tuple[str, str]]:
    """(family, name) for every producer — the endpoint's self-description."""
    return [(fn.story_family, fn.story_name)      # type: ignore[attr-defined]
            for fn in _REGISTRY]


@dataclass
class StoryContext:
    """Everything a producer may read, fetched once and shared.

    `insights()` is memoized: eight producers asking for the ledger cost one
    rollup read, not eight.
    """
    mac: str
    station_name: str
    today: date
    # What the reader's app is set to. Producers format THROUGH this and
    # never touch a raw °F/inch/inHg number after the last comparison.
    units: Units = UNITS_NATIVE
    # The station's own device row, or None when this MAC has never been
    # registered. Carried rather than re-fetched because `build_context`
    # already reads `db.list_devices()` for the name, and the sky producers
    # need the `info.coords` that comes with it — a second read for a field
    # already in hand is the kind of query this module exists to avoid.
    device: dict[str, Any] | None = None
    _insights: dict[str, Any] | None = None
    _daily: list[dict[str, Any]] | None = None
    _comfort: list[tuple[int, int, int, int, int, int, int]] | None = None
    _storms: list[dict[str, Any]] | None = None
    _current: dict[str, Any] | None = None
    _current_read: bool = False
    _day_ahead: dict[str, dict[str, Any]] | None = None
    _normals: tuple[str, dict[str, dict[str, float]]] | None = None
    _normals_read: bool = False

    async def insights(self) -> dict[str, Any]:
        if self._insights is None:
            from . import insights as _ins
            self._insights = await _ins.assemble(self.mac, today=self.today)
        return self._insights

    async def daily(self) -> list[dict[str, Any]]:
        """Every rollup day for this station, oldest first — the per-day
        extremes `insights.assemble` folds away.

        assemble() returns YEAR aggregates; a producer that ranks individual
        days needs the days themselves, so this is its own read rather than a
        second pass over a summary that no longer has them. Still rollups
        only: one indexed range per request over a table with one row per
        day, never the 1.15M-row observations table.

        The column list is the UNION of what every day-ranking producer
        needs, because the alternative is one query per producer over the
        same rows. Widening it costs bytes on a row already being read; a
        second SELECT would cost another pass. A column a producer does not
        use is simply ignored, and a column the station never filled arrives
        as None — which is how "no dew point sensor" is spelled.

        `dew_point_min` and `humidity_max` have no consumer yet (the
        humidity family reads `dew_point_max`); they ride along for the
        producer that wants them, at the cost of two columns on a row
        already being read. Leave them.
        """
        if self._daily is None:
            from . import db as dbmod
            async with dbmod.connect() as conn:
                rows = await (await conn.execute(
                    "SELECT day, tempf_min, tempf_max, windgustmph_max, "
                    "baromrelin_min, baromrelin_max, rain_total, "
                    "yearly_min, yearly_max, dew_point_min, dew_point_max, "
                    "humidity_max, lightning_max FROM daily_rollups "
                    "WHERE mac = ? ORDER BY day", (self.mac,))).fetchall()
            self._daily = [dict(r) for r in rows]
        return self._daily

    async def comfort(self) -> list[tuple[int, int, int, int, int, int, int]]:
        """The comfort ledger for this station: (year, month, hour, n,
        comfortable_n, hot_n, cold_n), every row. Counts of feels-like
        readings inside, above and below `insights.COMFORT_LOW_F..HIGH_F`,
        keyed by year so a producer can set this year's months beside the
        record's. One indexed read of a table with at most 24 x 12 rows
        per station-year; only the SHARES are meaningful (the counts do not
        survive history thinning, the ratios do)."""
        if self._comfort is None:
            from . import db as dbmod
            async with dbmod.connect() as conn:
                rows = await (await conn.execute(
                    "SELECT year, month, hour, n, comfortable_n, hot_n, "
                    "cold_n FROM comfort_rollups WHERE mac = ? "
                    "ORDER BY year, month, hour", (self.mac,))).fetchall()
            self._comfort = [tuple(int(x) for x in r) for r in rows]
        return self._comfort

    async def current(self) -> dict[str, Any] | None:
        """The station's latest observation, or None when it has never
        reported.

        The ONE place this module leaves the rollups, and it does so without
        scanning anything: `db.latest_observation` reads the newest row and
        the ~5 minutes behind it, which is an index seek on (mac, dateutc_ms)
        — the same read `/api/devices/{mac}/current` does on every app
        launch. A story about RIGHT NOW cannot be told from daily extremes,
        and the alternative (a rollup column for instantaneous conditions)
        would be a second, staler copy of a row that already exists.

        Memoized including the MISS: a station with no observations must
        cost one query no matter how many current-conditions producers ask,
        and `None` is a real answer that has to stick.
        """
        if not self._current_read:
            from . import db as dbmod
            self._current = await dbmod.latest_observation(self.mac)
            self._current_read = True
        return self._current

    async def day_ahead_forecasts(self) -> dict[str, dict[str, Any]]:
        """The model's day-ahead call for every local day it made one,
        keyed by valid date, from the first of LAST month onward.

        `forecast_snapshots` keeps every issue run; this is the newest
        lead-one run per valid date, and only for the provider that
        carries temperatures (the Zambretti ledger is a provider with no
        highs to score). One indexed read, shared by every producer that
        scores the forecast against what the station measured. Memoized
        including an empty result — a server that has never snapshotted
        must cost one query, not one per producer.
        """
        if self._day_ahead is None:
            from . import forecast_snapshots as _fs
            first_of_month = self.today.replace(day=1)
            since = (first_of_month - timedelta(days=1)).replace(day=1)
            self._day_ahead = await _fs.day_ahead_calls(
                FORECAST_PROVIDER, since, lead_days=FORECAST_LEAD_DAYS)
        return self._day_ahead

    async def normals(self) -> tuple[str, dict[str, dict[str, float]]] | None:
        """(NCEI station name, the 30-year normals keyed "MM-DD") for this
        station's location, or None.

        CACHE ONLY. `normals.today()` is the request-path reader and it
        goes to NCEI on a cold cache (a station search plus a full-year
        download); a producer runs inside the loop over every producer on
        every /stories call and must never block on the network. So this
        reads what `normals.cached_year` finds in server_kv and nothing
        else. The cache fills the first time the app opens its
        Today-vs-normal row, which every install does; until then, and on
        every station outside U.S. coverage, the "vs normal" line is
        simply absent. Absent, not computed from the station's own
        history: that fallback is the one Volney explicitly refused.

        Memoized including the miss, so three producers asking cost two
        kv reads, not six, and None sticks.
        """
        if not self._normals_read:
            self._normals_read = True
            coords = _station_coords(self)
            if coords is not None:
                from . import normals as _normals
                try:
                    self._normals = await _normals.cached_year(*coords)
                except Exception:
                    log.exception("normals cache read failed")
                    self._normals = None
        return self._normals

    async def storms(self) -> list[dict[str, Any]]:
        """Closed storm episodes, newest first. Bounded by construction —
        db.record_storm prunes each station to its newest 50 — so this is a
        whole-table read of a tiny table, not a history scan."""
        if self._storms is None:
            from . import db as dbmod
            self._storms = await dbmod.list_storms(self.mac,
                                                   limit=STORM_LOOKBACK)
        return self._storms

    def station(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Station attribution — who measured this, and over what span. The
        footer voice ("measured in this backyard") needs the span to be true."""
        return {"mac": self.mac, "name": self.station_name,
                "first_day": payload.get("first_day"),
                "last_day": payload.get("last_day"),
                "day_count": payload.get("day_count") or 0}


# A station with no device row still gets a card, and the card gets a
# header. It used to get the MAC address — honest, and a device identifier
# printed across a picture people post publicly. The identity lives in
# `station.mac` where a client can use it; the NAME falls back to something
# a reader can look at.
UNNAMED_STATION = "This Station"


async def build_context(mac: str,
                        units: Units = UNITS_NATIVE) -> StoryContext:
    from . import db as dbmod
    from .climate import local_today
    dev = next((d for d in await dbmod.list_devices() if d["mac"] == mac), None)
    return StoryContext(mac=mac,
                        station_name=((dev or {}).get("name")
                                      or UNNAMED_STATION),
                        today=local_today(), units=units, device=dev)


# ───────────────────────── the engine ─────────────────────────

async def top_stories(mac: str, *, limit: int = 4,
                      families: Sequence[str] | None = None,
                      min_score: float = 0.0,
                      units: Units = UNITS_NATIVE) -> dict[str, Any]:
    """Run the registry, rank the candidates, return the top `limit`.

    A producer that raises is logged and skipped: one broken producer must
    not empty the Worth Sharing section. A producer that DECLINES is not an
    error at all and is simply named in `declined` for diagnostics.
    """
    ctx = await build_context(mac, units)
    wanted = set(families) if families else None
    candidates: list[Story] = []
    declined: list[str] = []
    for fn in _REGISTRY:
        if wanted is not None and fn.story_family not in wanted:   # type: ignore[attr-defined]
            continue
        try:
            produced = await fn(ctx)
        except Exception:
            log.exception("story producer %s failed", fn.story_name)  # type: ignore[attr-defined]
            continue
        if not produced:
            declined.append(fn.story_name)                          # type: ignore[attr-defined]
            continue
        candidates.extend(produced)

    ranked = sorted((s for s in candidates if s.interestingness >= min_score),
                    key=lambda s: (-s.interestingness, s.id))
    return {
        "mac": ctx.mac,
        "generated_ms": int(time.time() * 1000),
        "anchor_day": ctx.today.isoformat(),
        # Echoed so a cached or forwarded payload can never be mistaken for
        # one rendered in a different scale.
        "units": asdict(ctx.units),
        "families": list(FAMILIES),
        "candidates": len(candidates),
        "declined": declined,
        "stories": [s.to_dict() for s in ranked[:limit]],
    }


# ───────────────────────── copy helpers ─────────────────────────

_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
          7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def _sig(v: float) -> str:
    """A number for COPY, at the precision it has earned: whole when it is
    whole, one decimal when it is not.

    A percentile threshold of 112.6 must not print as "113" while the count
    behind it is `hi >= 112.6` — that mismatch is the bug the streak card
    shipped with. The same rule carries into Celsius, where a tier that is a
    round 100°F becomes a decidedly unround 37.8°C and must say so.
    """
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def _sig_precision(v: float) -> int:
    """How many decimals `_sig` decided this number had earned.

    A Stat that is the SAME NUMBER as the hero line must be rendered with
    the same precision, or one picture carries "DEW POINT PEAKED AT 74°F"
    across the top and "74.0°F" in the stat beneath it (field report from
    the card templates, 2026-08-30). A chart series is a different surface —
    it keeps a uniform precision, because per-point precision makes an axis
    unreadable — but a headline and its own stat are one claim and must
    agree to the digit.
    """
    return 0 if abs(v - round(v)) < 0.05 else 1


def _share_phrase(count: int, total: int, noun: str = "days") -> str:
    """"nearly one in every two days" — the line that makes a ledger tier
    mean something. Qualified honestly: `nearly` under the fraction,
    `more than` over it, `about` within 5%.

    `noun` because the cold ledger counts NIGHTS: "one in every five days"
    under a headline reading "48 NIGHTS ≤32°F" is the card contradicting
    itself in the space of two lines.
    """
    if total <= 0 or count <= 0:
        return ""
    if count >= total:
        return f"every {noun[:-1] if noun.endswith('s') else noun} recorded"
    frac = count / total
    n = round(1 / frac)
    if n < 2:
        # Over half: "one in every one" is nonsense, so quote the percentage.
        return f"{round(frac * 100)}% of them"
    exact = 1 / n
    if frac >= exact * 1.05:
        qualifier = "more than"
    elif frac <= exact * 0.95:
        qualifier = "nearly"
    else:
        qualifier = "about"
    return f"{qualifier} one in every {_WORDS.get(n, str(n))} {noun}"


_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    # Two traps, both of which ship onto a picture: 11th/12th/13th take
    # "th" despite ending in 1/2/3, and the suffix decorates the WHOLE
    # number — 21 is "21st", not "1st".
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{_ORDINAL_SUFFIX.get(n % 10, 'th')}"


def _rank_line(rank: int | None, of: int | None, noun: str,
               superlative: str) -> str | None:
    """"the longest of 3 comparable years" / "2nd of 973 days on record".

    None when there is nothing to rank against — a rank of one out of one
    is not an achievement, and printing it would be the card congratulating
    the station for being the only entrant.
    """
    if not rank or not of or of < 2:
        return None
    if rank == 1:
        return f"the {superlative} of {of} {noun}"
    return f"{_ordinal(rank)} of {of} {noun}"


def _period_label(year: int, partial: bool) -> str:
    return f"{year} so far" if partial else str(year)


# Month names are spelled out here rather than taken from strftime("%B"):
# %b/%B are LOCALE-dependent and this string ships onto a share card, the
# same trap the storm clock pays for with %p. (%-d for an unpadded day is
# also non-portable — the day number is interpolated directly.)
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _short_date(d: date) -> str:
    return f"{_MONTHS[d.month - 1][:3]} {d.day}"


def _long_date(d: date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _long_date_iso(iso: str) -> str:
    """"January 1 2025" from a rollup's ISO day, for a stat LABEL. The ISO
    form is data; a label is copy, and every other date in copy goes
    through `_long_date`. Falls back to the raw string rather than
    declining a whole card over one unparseable label."""
    try:
        d = date.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso)
    return f"{_long_date(d)} {d.year}"


def _range_label(start: date, end: date) -> str:
    """"Apr 5 – Aug 30 2026", or "Dec 12 2025 – Mar 3 2026" when the range
    crosses New Year.

    A series row that ships raw ISO `start`/`end` and nothing else hands the
    template a choice it is not allowed to make: formatting a date range is
    composing, and composing is the client's forbidden move (field report
    from the card templates, 2026-08-30). The ISO pair stays — it is DATA, a
    client may sort or anchor on it — and this is the copy beside it.

    The year appears once when both ends share it, because "Apr 5 2026 –
    Aug 30 2026" wastes half a bar label saying the same thing twice.
    """
    if start.year == end.year:
        return f"{_short_date(start)} – {_short_date(end)} {end.year}"
    return (f"{_short_date(start)} {start.year} – "
            f"{_short_date(end)} {end.year}")


# The sentence a faded bar needs. Written once because two producers now
# draw non-comparable rows and a reader who meets both must not have to
# learn two vocabularies for the same grey.
INCOMPARABLE_FOOTNOTE = ("Faded years didn't cover enough of the window to "
                         "be compared. They are drawn, not counted.")


NORMALS_SOURCE = "NOAA/NCEI 1991-2020 U.S. Climate Normals"


def _vs_normal(normals: tuple[str, dict[str, dict[str, float]]] | None,
               on: date, reading: float | None, side: str,
               u: Units) -> str | None:
    """"ran 4°F above the 30-year normal high for the date", or None.

    A predicate, so the caller supplies the subject: "The hottest day,
    July 3, ran 4°F above...". None when there are no cached normals, no
    entry for the date, or no reading, and the caller prints nothing: a
    card that says "0°F above normal" for want of a normal is the zero
    bug wearing a lab coat.

    The departure is a DIFFERENCE and converts by scale; the reading it
    came from stays where it was. Nothing here names the NCEI station:
    the geography rule keeps place names out of producer copy, and the
    station name is the chart footnote's to carry (`_normals_footnote`).
    """
    if normals is None or reading is None or side not in ("high", "low"):
        return None
    entry = normals[1].get(f"{on.month:02d}-{on.day:02d}")
    base = _num((entry or {}).get(side))
    if base is None:
        return None
    delta = reading - base
    if abs(u.temp_delta(delta)) < 0.05:
        return f"landed right on the 30-year normal {side} for the date"
    return (f"ran {u.temp_delta_deg(abs(delta))} "
            f"{'above' if delta > 0 else 'below'} the 30-year normal "
            f"{side} for the date")


def _normals_footnote(normals: tuple[str, dict[str, dict[str, float]]]
                      ) -> str:
    """The one place the NOAA station's name may appear: under the chart,
    as attribution, the way the Today-vs-normal row already shows it."""
    return f"Normals for the date: {NORMALS_SOURCE}, {normals[0]}."


def _join(items: Sequence[str]) -> str:
    """"a, b and c" — no Oxford comma, matching the card copy already
    shipping."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _num(v: Any) -> float | None:
    """A rollup column as a finite float, or None.

    `insert_observations` stores poller values uncoerced and SQLite keeps
    text in a REAL column, so a garbled reading can surface here as a
    string; NaN/inf pass isinstance and would poison every comparison this
    module makes. Both are absent, and absent is not zero."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ───────────────────────── producers ─────────────────────────

def _ledger_view(yr: dict[str, Any], partial: bool) -> tuple[dict[str, int], int]:
    """(tier counts, days recorded) for one year, in the frame the story
    uses. A running year is measured THROUGH TODAY'S MONTH-DAY in every
    year it is compared with — comparing 8 months of 2026 against 12 months
    of 2025 is the kind of unfairness that reads as a headline."""
    if partial:
        return (dict(yr.get("tiers_to_date") or {}),
                int(yr.get("days_to_date") or 0))
    return dict(yr.get("tiers") or {}), int(yr.get("days") or 0)


def _cold_view(yr: dict[str, Any], partial: bool) -> tuple[dict[str, int], int]:
    """`_ledger_view`'s mirror for the cold ladder.

    Kept separate rather than parameterised because the frames are not the
    same shape: the heat ladder's to-date window trims a summer that has not
    finished, while the cold one trims a year that has had ONE winter to a
    comparison year's two half-winters. Same mechanism, different reason,
    and a reader of either deserves to see the reason next to it.
    """
    if partial:
        return (dict(yr.get("cold_to_date") or {}),
                int(yr.get("days_to_date") or 0))
    return dict(yr.get("cold") or {}), int(yr.get("days") or 0)


@producer(FAMILY_CLIMATE, "how_hot_is_hot")
async def how_hot_is_hot(ctx: StoryContext) -> list[Story]:
    """The heat ledger as a pyramid: how many days cleared each threshold.

    "108 DAYS ≥100°F · nearly one in every two days this year" is the whole
    idea — a count nobody disputes, translated into a proportion anybody
    feels. The counts come straight from insights.assemble's per-year
    ledger; this producer picks WHICH tier to lead with, compares it against
    the same window in earlier years, and writes the copy.

    Declines when the station has no meaningful year (fewer than
    MIN_LEDGER_DAYS rollup days) or never reached the lowest tier. A station
    that has not seen 80°F has no heat ledger — it does not have a ledger
    full of zeros.
    """
    from . import insights as _ins
    payload = await ctx.insights()
    years = payload.get("years") or []
    if not years:
        return []

    # Newest year WITH DATA, not necessarily this calendar year: a station
    # that stopped reporting last autumn still has a real 2025 story, and
    # inventing an empty 2026 one would be the zero bug in another costume.
    yr = years[-1]
    year = int(yr["year"])
    partial = year == ctx.today.year
    counts, days = _ledger_view(yr, partial)
    if days < MIN_LEDGER_DAYS:
        return []

    tiers = [float(t) for t in (payload.get("ledger_tiers")
                                or _ins.LEDGER_TIERS)]
    ladder = [(t, int(counts.get(str(int(t)), 0))) for t in tiers]
    if not any(c > 0 for _, c in ladder):
        return []

    # The hero tier is the most QUOTABLE one, not the highest. A fraction
    # near a half is what carries the "one in every two days" line; a tier
    # cleared almost every day says nothing, and a tier cleared twice is a
    # footnote. Ties break upward — the higher threshold is the bigger claim.
    def quotability(c: int) -> float:
        f = c / days
        return 4 * f * (1 - f)

    hero_tier, hero_count = max(
        ((t, c) for t, c in ladder if c > 0),
        key=lambda tc: (round(quotability(tc[1]), 6), tc[0]))
    key = str(int(hero_tier))

    # Comparison: the same tier, the same calendar window, earlier years —
    # but only years whose coverage is comparable. A year the station spent
    # mostly offline has FEWER hot days because it has fewer days, and
    # letting it into the baseline manufactures records.
    prior: list[tuple[int, int]] = []
    for other in years:
        if int(other["year"]) == year:
            continue
        o_counts, o_days = _ledger_view(other, partial)
        if _ins.comparable_to_date(o_days, days):
            prior.append((int(other["year"]), int(o_counts.get(key, 0))))

    comparison: Comparison | None = None
    standout: float | None = None
    if prior:
        baseline = sum(c for _, c in prior) / len(prior)
        rank = 1 + sum(1 for _, c in prior if c > hero_count)
        of = len(prior) + 1
        delta = hero_count - baseline
        # One prior year is a year, not an average — saying "2024 average"
        # of a single number invites the reader to imagine a normal that
        # isn't there.
        span = (f"{min(y for y, _ in prior)}–{max(y for y, _ in prior)} average"
                if len(prior) > 1 else str(prior[0][0]))
        # `_short_date`, never strftime: %b is locale-dependent and %-d is
        # not portable, and this string ships onto a share card.
        window = (f" through {_short_date(ctx.today)}" if partial else "")
        comparison = Comparison(
            kind="prior_years_to_date" if partial else "prior_years_full",
            label=(f"vs the same window in {_WORDS.get(len(prior), len(prior))} "
                   f"earlier year{'s' if len(prior) > 1 else ''}"),
            value=hero_count,
            baseline=round(baseline, 1),
            baseline_label=f"{span}{window}",
            direction=("level" if abs(delta) < 0.5
                       else "above" if delta > 0 else "below"),
            delta=round(delta, 1),
            # Absent is not zero, and it is not infinity either: a baseline
            # of 0 has no percentage, so the field says so.
            delta_pct=(round(100 * delta / baseline, 1) if baseline else None),
            rank=rank, of=of,
            rank_line=_rank_line(rank, of, "comparable years", "most"))
        standout = (of - rank) / (of - 1) if of > 1 else None

    reach = max((i for i, (_, c) in enumerate(ladder) if c > 0), default=0)
    parts = {
        "quotability": round(quotability(hero_count), 4),
        "reach": round((reach + 1) / len(ladder), 4),
    }
    weights = [(0.40, parts["quotability"]), (0.35, parts["reach"])]
    if standout is not None:
        parts["standout"] = round(standout, 4)
        weights.append((0.25, parts["standout"]))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    # Every tier stayed Fahrenheit through the comparisons above and turns
    # into the reader's scale only HERE, at the moment it becomes words.
    u = ctx.units
    label = f"≥{u.temp_deg(hero_tier)}"
    supporting = [
        Stat("days_recorded", f"days recorded in {_period_label(year, partial)}",
             days, UNIT_DAYS),
        Stat("top_tier_reached", "highest threshold cleared",
             round(u.temp(ladder[reach][0]), 1), u.temp_token, 1),
    ]
    hottest = yr.get("hottest")
    if hottest:
        supporting.append(Stat("hottest",
                               f"hottest day · {_long_date_iso(hottest[0])}",
                               round(u.temp(float(hottest[1])), 1),
                               u.temp_token, 1))
    p90 = payload.get("p90_high")
    streak = int(yr.get("longest_p90_streak") or 0)
    if p90 is not None and streak > 0:
        # Names the threshold AND what it is. The count is `hi >= p90`, so
        # the label says ≥ — the wording fixed on this branch after a card
        # shipped saying "above 113°" while counting 113.
        supporting.append(Stat(
            "longest_p90_streak",
            f"longest run of days ≥{u.temp_deg(float(p90))} "
            f"(the station's 90th-percentile high)",
            streak, UNIT_DAYS))

    share = _share_phrase(hero_count, days)
    context = (f"{hero_count} of the {days} days recorded in "
               f"{_period_label(year, partial)} reached "
               f"{u.temp_deg(hero_tier)} or hotter"
               + (f", {share}." if share else "."))
    # One sentence against the 30-year normal (2.0): the hottest day, set
    # beside what the date usually manages. Only when normals are cached
    # for this location; otherwise the sentence and the footnote are both
    # absent, and the card reads exactly as it did before.
    footnote: str | None = None
    if hottest and _num(hottest[1]) is not None:
        try:
            hot_day: date | None = date.fromisoformat(str(hottest[0]))
        except (TypeError, ValueError):
            hot_day = None
        line = (_vs_normal(await ctx.normals(), hot_day, float(hottest[1]),
                           "high", u) if hot_day else None)
        if line:
            context += f" The hottest day, {_long_date(hot_day)}, {line}."
            footnote = _normals_footnote(await ctx.normals())

    return [Story(
        id=f"climate.heat_ledger.{year}",
        family=FAMILY_CLIMATE,
        story_type="heat_ledger",
        title="How Hot Is Hot?",
        emoji="🔥",
        hero=Stat(f"tier_{int(hero_tier)}", f"days {label}", hero_count,
                  UNIT_DAYS),
        hero_line=f"{hero_count} DAYS {label}",
        context=context,
        comparison=comparison,
        supporting=supporting,
        # `key` is anchored to the STORED tier, so it is the same string in
        # every unit system and a template can match on it without knowing
        # what scale the numbers came out in. `share` is gone: the context
        # sentence already says "nearly one in every two days", which is the
        # same fact in a form somebody would read out loud.
        viz=Viz(kind="ledger_pyramid",
                series=[{"key": f"tier_{int(t)}",
                         "threshold": round(u.temp(t), 1),
                         "label": f"≥{u.temp_deg(t)}", "days": c}
                        for t, c in ladder],
                unit=u.temp_token,
                axis_label="days",
                footnote=footnote,
                highlight=int(hero_tier),
                highlight_key=f"tier_{int(hero_tier)}"),
        period=Period(kind="year", label=_period_label(year, partial),
                      start=f"{year}-01-01",
                      end=(ctx.today.isoformat() if partial
                           else f"{year}-12-31"),
                      partial=partial),
        station=ctx.station(payload),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


@producer(FAMILY_CLIMATE, "how_cold_is_cold")
async def how_cold_is_cold(ctx: StoryContext) -> list[Story]:
    """The heat ledger's mirror: how many nights fell to each threshold.

    Same pyramid, read downward. The ladder is climate-adaptive — the
    thresholds come from `insights.cold_tiers_for`, keyed on the station's
    own 10th-percentile low — so a Chandler card counts nights at 45/36/32/
    28/25°F and an Anchorage one counts 0/-10/-20/-30/-40°F, and neither is
    a ladder of zeros pretending to be a climate.

    DECLINES, in order:
      · no insights years at all;
      · fewer than MIN_LEDGER_DAYS rollup days in the newest year;
      · that year recorded NO LOW AT ALL (`coldest` is None) — a station
        with no cold sensor has no cold story, and this is the one check
        that separates it from the station below;
      · no night reached even the mildest tier. A station that never gets
        cold is not a station with a ledger full of zeros.

    The distinction those last two draw is the whole point of the card's
    warmest line: "not one night reached freezing" is a real and delightful
    measurement, but ONLY from a thermometer that was there for the winter
    and watching. It is never inferred from silence.
    """
    from . import insights as _ins
    payload = await ctx.insights()
    years = payload.get("years") or []
    if not years:
        return []

    yr = years[-1]
    year = int(yr["year"])
    partial = year == ctx.today.year
    counts, days = _cold_view(yr, partial)
    if days < MIN_LEDGER_DAYS:
        return []

    # Absent is not zero, and this is the exact spot the two look alike: a
    # year with no `coldest` never measured a low, so every count below
    # would be 0 for want of a sensor rather than for want of cold.
    coldest = yr.get("coldest")
    if not coldest or _num(coldest[1]) is None:
        return []

    tiers = [float(t) for t in (payload.get("cold_tiers")
                                or _ins.cold_tiers_for(payload.get("p10_low")))]
    ladder = [(t, int(counts.get(str(int(t)), 0))) for t in tiers]
    if not any(c > 0 for _, c in ladder):
        return []

    # Identical to the heat side on purpose: the most QUOTABLE tier, not the
    # coldest. Ties break toward the COLDER threshold — the bigger claim —
    # which on a ladder that descends means negating the key.
    def quotability(c: int) -> float:
        f = c / days
        return 4 * f * (1 - f)

    hero_tier, hero_count = max(
        ((t, c) for t, c in ladder if c > 0),
        key=lambda tc: (round(quotability(tc[1]), 6), -tc[0]))
    key = str(int(hero_tier))

    prior: list[tuple[int, int]] = []
    for other in years:
        if int(other["year"]) == year:
            continue
        o_counts, o_days = _cold_view(other, partial)
        if _ins.comparable_to_date(o_days, days):
            prior.append((int(other["year"]), int(o_counts.get(key, 0))))

    comparison: Comparison | None = None
    standout: float | None = None
    if prior:
        baseline = sum(c for _, c in prior) / len(prior)
        # Rank 1 is the COLDEST year — more nights at the tier is more
        # extreme — so the ranking reads the same direction as the heat
        # card's even though the thermometer runs the other way.
        rank = 1 + sum(1 for _, c in prior if c > hero_count)
        of = len(prior) + 1
        delta = hero_count - baseline
        span = (f"{min(y for y, _ in prior)}–{max(y for y, _ in prior)} average"
                if len(prior) > 1 else str(prior[0][0]))
        window = (f" through {_short_date(ctx.today)}" if partial else "")
        comparison = Comparison(
            kind="prior_years_to_date" if partial else "prior_years_full",
            label=(f"vs the same window in {_WORDS.get(len(prior), len(prior))} "
                   f"earlier year{'s' if len(prior) > 1 else ''}"),
            value=hero_count,
            baseline=round(baseline, 1),
            baseline_label=f"{span}{window}",
            direction=("level" if abs(delta) < 0.5
                       else "above" if delta > 0 else "below"),
            delta=round(delta, 1),
            delta_pct=(round(100 * delta / baseline, 1) if baseline else None),
            rank=rank, of=of,
            rank_line=_rank_line(rank, of, "comparable years", "coldest"))
        standout = (of - rank) / (of - 1) if of > 1 else None

    reach = max((i for i, (_, c) in enumerate(ladder) if c > 0), default=0)
    # The same three parts and the same weights as the heat ledger, because
    # the two must be rankable against each other on ONE 0..1 scale: a
    # remarkable winter should be able to outrank a merely warm summer in
    # the same feed, and it can only do that if neither producer is quietly
    # scoring on a scale of its own.
    parts = {
        "quotability": round(quotability(hero_count), 4),
        "reach": round((reach + 1) / len(ladder), 4),
    }
    weights = [(0.40, parts["quotability"]), (0.35, parts["reach"])]
    if standout is not None:
        parts["standout"] = round(standout, 4)
        weights.append((0.25, parts["standout"]))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    # Fahrenheit through every comparison above; the reader's scale only now.
    u = ctx.units
    label = f"≤{u.temp_deg(hero_tier)}"
    period = _period_label(year, partial)
    freezes = int((yr.get("freezes_to_date") if partial
                   else yr.get("freezes")) or 0)

    supporting = [
        Stat("nights_recorded", f"nights recorded in {period}",
             days, UNIT_NIGHTS),
        Stat("coldest_night",
             f"coldest night · {_long_date_iso(coldest[0])}",
             round(u.temp(float(coldest[1])), 1), u.temp_token, 1),
        Stat("lowest_tier_reached", "coldest threshold reached",
             round(u.temp(ladder[reach][0]), 1), u.temp_token, 1),
        # A measured zero is a real number here — the `coldest` check above
        # proved the thermometer was reading — so this ships at 0 and says
        # something true. It is also the count that earns the warmest line.
        Stat("freezing_nights", f"nights at or below {u.temp_deg(FREEZE_F)}",
             freezes, UNIT_NIGHTS),
    ]
    p10 = payload.get("p10_low")
    streak = int(yr.get("longest_p10_streak") or 0)
    if p10 is not None and streak > 0:
        supporting.append(Stat(
            "longest_p10_streak",
            f"longest run of nights ≤{u.temp_deg(float(p10))} "
            f"(the station's 10th-percentile low)",
            streak, UNIT_NIGHTS))

    share = _share_phrase(hero_count, days, "nights")
    context = (f"{hero_count} of the {days} nights recorded in {period} fell "
               f"to {u.temp_deg(hero_tier)} or colder"
               + (f", {share}." if share else "."))
    # The warmest thing this card can say, and it is only sayable when the
    # record actually spans the cold shoulders of the year. A summer-only
    # record has no freezes either, and saying so would be absent-is-not-
    # zero wearing a gardening apron.
    first_day = str(yr.get("first_day") or "")
    last_day = str(yr.get("last_day") or "")
    watched_winter = bool(first_day and first_day <= f"{year}-01-15"
                          and (partial or last_day >= f"{year}-12-15"))
    if freezes == 0 and watched_winter:
        context += (f" Not one night in {period} reached "
                    f"{u.temp_deg(FREEZE_F)}. The tomatoes survived.")
    # The coldest night against the 30-year normal LOW for its date, on the
    # same terms as the heat card: present only when normals are cached.
    footnote: str | None = None
    try:
        cold_day: date | None = date.fromisoformat(str(coldest[0]))
    except (TypeError, ValueError):
        cold_day = None
    line = (_vs_normal(await ctx.normals(), cold_day, float(coldest[1]),
                       "low", u) if cold_day else None)
    if line:
        context += f" The coldest night, {_long_date(cold_day)}, {line}."
        footnote = _normals_footnote(await ctx.normals())

    return [Story(
        id=f"climate.cold_ledger.{year}",
        family=FAMILY_CLIMATE,
        story_type="cold_ledger",
        title="How Cold Is Cold?",
        emoji="🥶",
        hero=Stat(f"tier_{int(hero_tier)}", f"nights {label}", hero_count,
                  UNIT_NIGHTS),
        hero_line=f"{hero_count} NIGHTS {label}",
        context=context,
        comparison=comparison,
        supporting=supporting,
        # Deliberately the heat card's template and the heat card's series
        # shape — the spec's "two cards for one template". The palette flip
        # keys off `story_type`, a stable identifier already on every story;
        # it is NOT inferred from the "≤" in a label, because a colour
        # chosen by parsing prose is a colour that breaks the first time the
        # prose changes. The ladder descends, so the pyramid stands on its
        # point where the heat one stands on its base.
        viz=Viz(kind="ledger_pyramid",
                series=[{"key": f"tier_{int(t)}",
                         "threshold": round(u.temp(t), 1),
                         "label": f"≤{u.temp_deg(t)}",
                         # `days` is the shared template's name for the bar
                         # length; renaming it per card would make the "one
                         # template" claim false. `nights` rides alongside
                         # for a card that wants the honest noun.
                         "days": c, "nights": c}
                        for t, c in ladder],
                unit=u.temp_token,
                axis_label="nights",
                footnote=footnote,
                highlight=int(hero_tier),
                highlight_key=f"tier_{int(hero_tier)}"),
        period=Period(kind="year", label=period,
                      start=f"{year}-01-01",
                      end=(ctx.today.isoformat() if partial
                           else f"{year}-12-31"),
                      partial=partial),
        station=ctx.station(payload),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# ───────────────────────── Wildest Day ─────────────────────────
#
# The producer the whole engine was worth building for: nobody decides which
# day deserves a graphic, the station's own record does. Two scopes, ONE
# implementation — "the wildest day of the month" (round 1's story-day
# callout, which the monthly card renders) and "the wildest day of the year"
# (the Worth Sharing hero) differ only in which days are CANDIDATES. Both
# rank against the same station-wide distributions, because normalizing a
# month against its own 30 days would crown a "wildest day" every month by
# construction, at a percentile that means nothing.


@dataclass(frozen=True)
class _Dim:
    """One axis of chaos.

    `floor` is where the dimension starts counting. "median" means the
    station's OWN median for that axis, so half of an ordinary station's
    days score zero on it by definition — the floor moves with the station,
    which is the whole reason a wild day in Chandler and a wild day in
    Irwin PA can be told apart. "rain_day" is the fixed line insights
    already uses for a rain day (0.01 in, one bucket tip): rain's median is
    zero almost everywhere, so a median floor would rank a trace against a
    field of zeros and call it weather.

    Weights are mild and relative: a gust and a soaking count for more than
    a barometer wobble, and the WEIGHTED MEAN IS RENORMALIZED over the
    dimensions actually present — a station with no rain gauge is not a
    station where it never rains.
    """
    key: str
    noun: str                       # "the strongest gust"
    # The NATIVE unit and precision — what the dimension is measured in.
    # What a card SHOWS comes from `_dim_display`, which converts into the
    # reader's scale; these stay Fahrenheit/mph/inches because every score
    # above is computed on the stored values.
    unit: str | None
    precision: int
    weight: float
    floor: str                      # "median" | "rain_day"


# Order is the card's reading order, not a priority.
_DIMS: tuple[_Dim, ...] = (
    _Dim("anomaly",  "the biggest departure from normal", UNIT_F,     1, 1.00, "median"),
    _Dim("swing",    "the widest temperature swing",      UNIT_F,     1, 0.85, "median"),
    _Dim("gust",     "the strongest gust",                UNIT_MPH,   0, 1.00, "median"),
    _Dim("rain",     "the most rain",                     UNIT_IN,    2, 1.00, "rain_day"),
    _Dim("rate",     "the hardest rain rate",             UNIT_IN_HR, 2, 0.80, "rain_day"),
    _Dim("pressure", "the widest pressure swing",         UNIT_INHG,  2, 0.70, "median"),
)
_DIM_BY_KEY = {d.key: d for d in _DIMS}


def _day_rain_in(row: dict[str, Any]) -> float | None:
    """The day's rainfall, or None when the station never measured it.

    PROVENANCE, and this is the rule a bumped mast is not allowed to beat:
    `rain_total` is the day's high-water mark of `dailyrainin`, and the
    tipping-gauge-over-haptic preference is applied where it belongs — at
    ingest, once, for every consumer (ecowitt._rain picks `dailyrainin`
    ahead of `drain_piezo`, `yearlyrainin` ahead of `yrain_piezo`). A piezo
    reading only ever reaches these columns on a station whose tipping gauge
    is silent, so reading them IS honouring the rule; re-deciding it here
    would be a second, divergent copy of it.

    Nothing here touches `hourlyrainin`. That field is a RATE, not an
    accumulation, and reading it as one is a documented way to invent rain.

    None, never 0.0, when neither counter reported: a station with no rain
    gauge must drop the dimension, and `insights.day_rain`'s 0.0 default is
    right for a running total and wrong for a ranking.
    """
    total = _num(row.get("rain_total"))
    if total is not None:
        return max(0.0, total)
    lo, hi = _num(row.get("yearly_min")), _num(row.get("yearly_max"))
    # The yearly-counter fallback for sources that carry no daily total.
    # Not on Jan 1: the counter resets there, so the day's delta is the
    # whole previous year running backwards.
    new_year = str(row.get("day", "")).endswith("-01-01")
    if lo is not None and hi is not None and not new_year:
        return max(0.0, hi - lo)
    return None


def _monthly_normals(rows: Sequence[dict[str, Any]]) -> tuple[dict[int, float],
                                                              dict[int, float]]:
    """(normal high, normal low) per calendar month, from the station's own
    record. Months thinner than MIN_NORMAL_DAYS are absent — an anomaly
    measured against one week of April is not an anomaly."""
    his: dict[int, list[float]] = {}
    los: dict[int, list[float]] = {}
    for r in rows:
        try:
            month = int(str(r["day"])[5:7])
        except (ValueError, KeyError):
            continue
        hi, lo = _num(r.get("tempf_max")), _num(r.get("tempf_min"))
        if hi is not None:
            his.setdefault(month, []).append(hi)
        if lo is not None:
            los.setdefault(month, []).append(lo)

    def mean_where_thick(src: dict[int, list[float]]) -> dict[int, float]:
        return {m: sum(v) / len(v) for m, v in src.items()
                if len(v) >= MIN_NORMAL_DAYS}
    return mean_where_thick(his), mean_where_thick(los)


async def _storm_peak_rates(ctx: StoryContext) -> dict[str, float]:
    """local day → hardest rain rate recorded by a storm that STARTED that
    day, in in/hr.

    Peak rate is the one dimension the daily rollups do not carry, and the
    only other place it exists is the raw observations table — which is
    exactly the scan this module refuses to do. storm_history has it
    already, pruned to 50 episodes per station, so the dimension rides that
    bounded table or it is absent. Absent for a day with no recorded storm
    (summaries are opt-in, and episodes predating the table were never
    written), which is why the dimension DROPS on those days instead of
    scoring a zero nobody measured.

    A storm running past midnight is attributed to the day it began — the
    event's own day, the same day its summary is stamped with.
    """
    from datetime import datetime, timezone as _tzu
    from . import insights as _ins
    tz = _ins._tz()
    out: dict[str, float] = {}
    for s in await ctx.storms():
        rate = _num(s.get("peak_rate_in_hr"))
        started = _num(s.get("started_ms"))
        if rate is None or started is None or rate <= 0:
            continue
        day = (datetime.fromtimestamp(started / 1000, tz=_tzu.utc)
               .astimezone(tz).strftime("%Y-%m-%d"))
        out[day] = max(out.get(day, 0.0), rate)
    return out


@dataclass
class _WildDay:
    day: str
    on: date
    values: dict[str, float]        # dimension key → measured value
    scores: dict[str, float]        # dimension key → 0..1 vs station history
    anomaly: float | None           # SIGNED, for copy
    anomaly_side: str | None        # "high" | "low"
    hi: float | None
    lo: float | None
    chaos: float = 0.0
    intensity: float = 0.0
    breadth: float = 0.0


def _measure_days(rows: Sequence[dict[str, Any]],
                  rates: dict[str, float]) -> list[_WildDay]:
    """Per-day dimension VALUES. No scoring yet, and no zero-filling: a
    dimension the station did not measure that day is simply not a key."""
    norm_hi, norm_lo = _monthly_normals(rows)
    out: list[_WildDay] = []
    for r in rows:
        day = str(r.get("day") or "")
        try:
            on = date.fromisoformat(day)
        except ValueError:
            continue
        hi, lo = _num(r.get("tempf_max")), _num(r.get("tempf_min"))
        values: dict[str, float] = {}

        # Anomaly takes the LARGER of the two departures, so "the coldest
        # morning of the month" is as findable as "the hottest afternoon" —
        # a day with a freezing dawn and an ordinary afternoon has an
        # unremarkable midpoint and is exactly the day this card is for.
        anomaly: float | None = None
        side: str | None = None
        for value, normals, which in ((hi, norm_hi, "high"), (lo, norm_lo, "low")):
            base = normals.get(on.month)
            if value is None or base is None:
                continue
            delta = value - base
            if anomaly is None or abs(delta) > abs(anomaly):
                anomaly, side = delta, which
        if anomaly is not None:
            values["anomaly"] = abs(anomaly)

        if hi is not None and lo is not None:
            values["swing"] = hi - lo
        gust = _num(r.get("windgustmph_max"))
        if gust is not None:
            values["gust"] = gust
        rain = _day_rain_in(r)
        if rain is not None:
            values["rain"] = rain
        if day in rates:
            values["rate"] = rates[day]
        p_lo, p_hi = _num(r.get("baromrelin_min")), _num(r.get("baromrelin_max"))
        if p_lo is not None and p_hi is not None:
            # The rollups keep the day's extremes, not the order they
            # arrived in, so this is the day's pressure RANGE — how far the
            # barometer travelled, not which way it went.
            values["pressure"] = max(0.0, p_hi - p_lo)

        out.append(_WildDay(day=day, on=on, values=values, scores={},
                            anomaly=anomaly, anomaly_side=side, hi=hi, lo=lo))
    return out


def _median(pool: Sequence[float]) -> float:
    """The middle value — the UPPER of the two middles for an even pool
    (`sorted(pool)[n // 2]`) — so the floor is always a value the station
    actually recorded rather than an average of two it didn't."""
    return sorted(pool)[len(pool) // 2]


def _pools(days: Sequence[_WildDay]) -> dict[str, tuple[float, list[float], int]]:
    """dimension key → (floor, the above-floor pool, days measured at all),
    for the dimensions this station can be ranked on. The measured count
    rides along so a tile can say "2nd of 973 days" — the rank is against
    every day that MEASURED the dimension, not just the notable ones."""
    from . import insights as _ins
    out: dict[str, tuple[float, list[float], int]] = {}
    for dim in _DIMS:
        pool = [d.values[dim.key] for d in days if dim.key in d.values]
        if not pool:
            continue
        floor = (_ins.RAIN_DAY_MIN_IN if dim.floor == "rain_day"
                 else _median(pool))
        above = [v for v in pool if v >= floor]
        if len(above) < MIN_DIM_POOL:
            # Too few comparable values to say anything about where a day
            # sits. Dropped, not zeroed — the renormalized mean handles it.
            continue
        out[dim.key] = (floor, above, len(pool))
    return out


def _rank_share(pool: Sequence[float], value: float) -> float:
    """Where `value` sits inside the station's own above-floor pool, 0..1.

    Mid-rank so ties share their position, and divided by the pool size
    rather than (n - 1) on purpose: the largest of four recorded values
    scores 0.875, not 1.0. A single wet day on record cannot be called a
    once-in-a-station event, and this is where that honesty lives — the
    score grows toward 1 only as the station accumulates enough history to
    justify the claim.
    """
    below = sum(1 for v in pool if v < value)
    equal = sum(1 for v in pool if v == value)
    return (below + 0.5 * equal) / len(pool)


def _score_days(days: Sequence[_WildDay],
                pools: dict[str, tuple[float, list[float]]]) -> None:
    """Fill in each day's per-dimension scores and its composite, in place.

    Composite = 0.60 intensity + 0.40 breadth, both weighted means over the
    dimensions PRESENT on that day:

      intensity — how far up the station's own distribution the day sits;
      breadth   — the weighted share of its dimensions that cleared
                  WILD_NOTABLE, which is the entire point of the card. A
                  day that maxes exactly one axis has one notable share of
                  six and loses to a day that owns three at once, which is
                  the behaviour the motivating example demands.

    Renormalizing both terms over present dimensions is the same move
    `how_hot_is_hot` makes when its standout term is missing, and it is what
    lets a rain-gauge-less station still tell this story.
    """
    for d in days:
        total_w = 0.0
        weighted = 0.0
        notable = 0.0
        for dim in _DIMS:
            if dim.key not in pools or dim.key not in d.values:
                continue
            floor, pool, _ = pools[dim.key]
            value = d.values[dim.key]
            # Below the floor is MEASURED-ORDINARY, which is a real zero —
            # unlike a missing dimension, which never enters the sum.
            score = 0.0 if value < floor else _rank_share(pool, value)
            d.scores[dim.key] = round(score, 4)
            total_w += dim.weight
            weighted += dim.weight * score
            if score >= WILD_NOTABLE:
                notable += dim.weight
        if total_w <= 0:
            continue
        d.intensity = weighted / total_w
        d.breadth = notable / total_w
        d.chaos = 0.60 * d.intensity + 0.40 * d.breadth


# (superlative when the day HOLDS the period's maximum, plain when it does
# not). A stat labelled "strongest gust of August" while carrying the
# winner's second-place gust would be the card lying about its own number —
# the winning day owns most axes, not necessarily all of them.
_STAT_LABELS = {
    "swing":    ("widest temperature swing", "temperature swing"),
    "gust":     ("strongest gust",           "peak gust"),
    "rain":     ("most rain",                "rain that day"),
    "rate":     ("hardest rain rate",        "peak rain rate"),
    "pressure": ("widest pressure swing",    "pressure swing"),
}


def _stat_for(dim: _Dim, day: _WildDay, scope_of: str, owned: bool,
              u: Units,
              pools: dict[str, tuple[float, list[float], int]] | None = None,
              ) -> Stat:
    """One dimension as a card-ready supporting stat, in the reader's units.
    The VALUE is always the measurement, never the score — a card shows
    "46 mph", not "0.97"."""
    value = day.values[dim.key]
    if dim.key == "anomaly":
        # The anomaly's own stat quotes the READING and puts the departure
        # in the label, because "68°F" is what happened and "12.4°F below
        # the August normal low" is why it mattered.
        #
        # ⚠️ The reading takes the OFFSET conversion and the departure takes
        # the SCALE one. Sending a 12.4°F departure through the reading
        # conversion yields −11°C, which is not a departure at all — it is
        # the temperature 12.4°F is. Same trap the app's TempUnit.delta
        # exists to close, on the one label that shows both at once.
        side = day.anomaly_side or "high"
        reading = day.hi if side == "high" else day.lo
        away = abs(day.anomaly or 0.0)
        direction = "below" if (day.anomaly or 0.0) < 0 else "above"
        noun = {("high", "above"): "hottest afternoon",
                ("high", "below"): "coolest afternoon",
                ("low", "below"): "coldest morning",
                ("low", "above"): "warmest night"}[(side, direction)]
        return Stat(dim.key,
                    f"{noun} · {u.temp_delta_deg(away)} {direction} the "
                    f"{_MONTHS[day.on.month - 1]} normal {side}",
                    round(u.temp(reading), 1) if reading is not None else None,
                    u.temp_token, 1)
    superlative, plain = _STAT_LABELS[dim.key]
    label = f"{superlative} {scope_of}" if owned else plain
    # The tile's complementary half (2.0 round-5 review): the tiles used to
    # repeat the four bar values verbatim, so the card said everything
    # twice. The RANK — this value against every day that measured the
    # dimension, whole record — is the fact the bar cannot carry: an
    # August-owned gust can still be 37th all-time, and saying so is the
    # difference between a tile and an echo.
    if owned and pools and dim.key in pools:
        _, above, measured = pools[dim.key]
        rank = 1 + sum(1 for v in above if v > value)
        label += f" · {_ordinal(rank)} of {measured} days"
    shown, token, precision = _dim_display(dim, value, u)
    return Stat(dim.key, label, round(shown, precision), token, precision)


def _dim_display(dim: _Dim, value: float, u: Units) -> tuple[float, str, int]:
    """(value, unit token, precision) for one chaos dimension in the
    reader's units. `swing` and `pressure` are RANGES — how far the number
    travelled — so both take the scale-only conversion, never the offset."""
    if dim.key == "swing":
        return u.temp_delta(value), u.temp_token, 1
    if dim.key == "gust":
        return u.wind_value(value), u.wind_token, 0
    if dim.key == "rain":
        return u.rain_value(value), u.rain_token, u.rain_precision
    if dim.key == "rate":
        return u.rain_value(value), u.rate_token, u.rain_precision
    if dim.key == "pressure":
        return u.pressure_value(value), u.pressure_token, u.pressure_precision
    return value, dim.unit or "", dim.precision


def _best_day(days: Sequence[_WildDay]) -> _WildDay | None:
    """The period's winner. Ties break on the LATER date — a tie means the
    two days are indistinguishable against the station's record, and the
    more recent one is the one people remember."""
    if not days:
        return None
    return max(days, key=lambda d: (round(d.chaos, 6), d.day))


def _scope_story(ctx: StoryContext, kind: str, days: list[_WildDay],
                 scored: list[_WildDay],
                 pools: dict[str, tuple[float, list[float]]],
                 *, label: str, possessive: str, scope_of: str,
                 start: str, end: str, partial: bool,
                 story_id: str,
                 normals: tuple[str, dict[str, dict[str, float]]] | None = None,
                 ) -> Story | None:
    """One scope's finished story, or None when the scope has nothing
    honest to say. `normals` is the cached 30-year table (or None), for
    the one sentence that sets the day beside what its date usually does."""
    best = _best_day(days)
    if best is None:
        return None
    if not any(s >= WILD_NOTABLE for s in best.scores.values()):
        # Nothing in this period stood out against the station's own record.
        # A "wildest day" that owns nothing is a calendar entry, not a story.
        return None

    # Which dimensions this day HOLDS THE PERIOD'S MAXIMUM for — round 1's
    # story-day test ("one date owns ≥3 monthly extremes") computed by the
    # same machinery, so the monthly callout and the yearly card can never
    # disagree about which day it was.
    owned: list[str] = []
    for dim in _DIMS:
        if dim.key not in pools or dim.key not in best.values:
            continue
        peak = max(d.values[dim.key] for d in days if dim.key in d.values)
        if best.values[dim.key] >= peak and best.scores.get(dim.key, 0.0) > 0:
            owned.append(dim.key)

    present = [dim for dim in _DIMS
               if dim.key in pools and dim.key in best.values]
    ordered = sorted(present, key=lambda dim: -best.scores.get(dim.key, 0.0))

    # Only the first noun carries the possessive: "August's strongest gust,
    # most rain and coldest morning" reads; repeating "August's" does not.
    owned_nouns = [_DIM_BY_KEY[k].noun.replace("the ", "", 1) for k in owned]
    if owned_nouns:
        owned_nouns[0] = f"{possessive} {owned_nouns[0]}"
        tally = (f"every one of the "
                 f"{_WORDS.get(len(present), str(len(present)))} measurements"
                 if len(owned) == len(present) else
                 f"{_WORDS.get(len(owned), str(len(owned)))} of the "
                 f"{_WORDS.get(len(present), str(len(present)))} measurements")
        context = (f"{_long_date(best.on)} held {_join(owned_nouns)}, "
                   f"{tally} this station can rank.")
    else:
        context = (f"{_long_date(best.on)} set no single record. It "
                   f"finished high in {_WORDS.get(len(present), str(len(present)))} "
                   f"measurements at once, which no other day in "
                   f"{label} managed.")
    # The side the day's own anomaly pointed at, against the 30-year normal
    # for that side of that date. The station-history anomaly above is a
    # ranking device; this is the number a reader can check against the
    # almanac. Absent when no normals are cached, never zero-filled.
    side = best.anomaly_side or ("high" if best.hi is not None else "low")
    reading = best.hi if side == "high" else best.lo
    normal_line = _vs_normal(normals, best.on, reading, side, ctx.units)
    if normal_line:
        context += f" Its {side} {normal_line}."

    # Comparison: not against the runner-up (an arbitrary neighbour) but
    # against every day this station has ever recorded, which is the claim
    # the score is actually making.
    all_chaos = sorted(d.chaos for d in scored)
    baseline = _median(all_chaos)
    rank = 1 + sum(1 for c in all_chaos if c > best.chaos)
    delta = best.chaos - baseline
    comparison = Comparison(
        kind="station_days",
        label="vs every day this station has recorded",
        value=round(best.chaos, 4),
        baseline=round(baseline, 4),
        baseline_label=f"the median of {len(all_chaos)} recorded days",
        direction=("level" if abs(delta) < 0.01
                   else "above" if delta > 0 else "below"),
        delta=round(delta, 4),
        delta_pct=(round(100 * delta / baseline, 1) if baseline else None),
        rank=rank, of=len(all_chaos),
        rank_line=_rank_line(rank, len(all_chaos),
                             "days this station has recorded", "wildest"))

    parts = {
        "intensity": round(best.intensity, 4),
        "breadth": round(best.breadth, 4),
        "dimensions": float(len(present)),
        "extremes_owned": float(len(owned)),
    }

    # ONE Stat per dimension, shared by the supporting list and the viz: the
    # anomaly's stat quotes the READING while its dimension value is the
    # departure, and a card that drew one and labelled it with the other
    # would be quietly wrong in the only place anyone would notice.
    stats = {dim.key: _stat_for(dim, best, scope_of, dim.key in owned,
                                ctx.units, pools)
             for dim in ordered}
    supporting = [stats[dim.key] for dim in ordered]
    supporting.append(Stat("days_considered", f"days recorded in {label}",
                           len(days), UNIT_DAYS))

    return Story(
        id=story_id,
        family=FAMILY_RECORDS,
        story_type="wildest_day",
        title="Wildest Day",
        emoji="⚡",
        hero=Stat("extremes_owned",
                  f"of the {len(present)} extremes this station ranks, held "
                  f"by this one day", len(owned)),
        # "SO FAR" on any period still running, month as much as year: on
        # the 19th of a month that has eleven days left, "August's wildest
        # day" is a claim about days nobody has measured yet. Same rule the
        # ledger copy pays for with "2026 so far".
        hero_line=f"{_short_date(best.on).upper()} WAS {possessive.upper()} "
                  f"WILDEST DAY" + (" SO FAR" if partial else ""),
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="chaos_dimensions",
                series=[{"key": dim.key,
                         "label": dim.noun.replace("the ", "", 1),
                         "score": best.scores.get(dim.key, 0.0),
                         "value": stats[dim.key].value,
                         "unit": stats[dim.key].unit,
                         "precision": stats[dim.key].precision,
                         "owned": dim.key in owned}
                        for dim in ordered],
                axis_label="share of this station's own record",
                footnote=(_normals_footnote(normals)
                          if normal_line and normals else None),
                highlight=(ordered[0].key if ordered else None),
                highlight_key=(ordered[0].key if ordered else None)),
        period=Period(kind=kind, label=label, start=start, end=end,
                      partial=partial),
        station=ctx.station({"first_day": scored[0].day,
                             "last_day": scored[-1].day,
                             "day_count": len(scored)}),
        interestingness=round(min(1.0, max(0.0, best.chaos)), 4),
        score_parts=parts,
    )


# A month with fewer measured swings than this is not a month worth ranking
# days inside. Lower than MIN_LEDGER_DAYS on purpose: a swing is a fact about
# ONE day, so a partial month can still hold a real answer, where a ledger
# counting proportions cannot.
MIN_SWING_DAYS = 10
# The station's own typical swing needs enough days to be a climate rather
# than a fortnight. Shares MIN_NORMAL_DAYS' reasoning, one scale up.
MIN_SWING_HISTORY = 60
# A day's swing, in °F, that saturates the magnitude score. 50°F between a
# day's high and its low is a continental-spring kind of day; the biggest in
# this repo's fixtures and in Chandler's record sit well under it, so the
# cap shapes the top of the scale without flattening the range people
# actually see.
SWING_SATURATION_F = 50.0


@producer(FAMILY_RECORDS, "biggest_swing")
async def biggest_swing(ctx: StoryContext) -> list[Story]:
    """The month's widest gap between a day's high and its low, against the
    swing this station usually manages — and the month's NARROWEST day
    beside it, which is the half that tells you something.

    A big swing is a clear, dry, still night radiating heat away. A tiny one
    is cloud, humidity or a storm holding the temperature in place, and a
    reader who sees "42°F on the 3rd, 9°F on the 19th" learns what the sky
    was doing on both days without a forecast ever being mentioned.

    Rollups only: one memoized pass over daily_rollups, which already stores
    tempf_min and tempf_max per day. Nothing here reads an observation.

    DECLINES when no day in the record carries BOTH extremes (a station with
    only a max is not a station with a zero swing), when the newest month
    holds fewer than MIN_SWING_DAYS measured days, or when the whole record
    holds fewer than MIN_SWING_HISTORY of them — "bigger than typical" needs
    a typical, and two weeks of readings is not one. Also declines when the
    month's widest day beat no day on record: a flat station has a maximum
    without having a story.

    UNITS: a swing is a DIFFERENCE and converts by scale alone. The high and
    low it was measured between are READINGS and carry the offset. Running a
    30°F swing through the reading conversion yields −1°C, which is the trap
    this module keeps a separate `temp_delta` for.
    """
    days = await ctx.daily()
    if not days:
        return []

    # (date, high, low, swing) for every day that measured both ends.
    measured: list[tuple[date, float, float, float]] = []
    for row in days:
        hi, lo = _num(row.get("tempf_max")), _num(row.get("tempf_min"))
        if hi is None or lo is None or hi < lo:
            continue
        try:
            when = date.fromisoformat(str(row["day"]))
        except (TypeError, ValueError):
            continue
        measured.append((when, hi, lo, hi - lo))
    if len(measured) < MIN_SWING_HISTORY:
        return []

    # The newest month WITH DATA, not necessarily this calendar month: a
    # station that stopped reporting in June still has a real June story,
    # and inventing an empty August one is the zero bug in another costume.
    last = measured[-1][0]
    year, month = last.year, last.month
    in_month = [m for m in measured if m[0].year == year and m[0].month == month]
    if len(in_month) < MIN_SWING_DAYS:
        return []
    partial = (year, month) == (ctx.today.year, ctx.today.month)

    widest = max(in_month, key=lambda m: (m[3], -m[0].toordinal()))
    narrowest = min(in_month, key=lambda m: (m[3], -m[0].toordinal()))
    hero_day, hero_hi, hero_lo, hero_swing = widest

    # The station's typical day, as a MEDIAN over the whole record. A mean
    # is dragged upward by exactly the outliers this card is about, so the
    # comparison would flatter every hero it ever picked.
    ordered = sorted(m[3] for m in measured)
    mid = len(ordered) // 2
    typical = (ordered[mid] if len(ordered) % 2
               else (ordered[mid - 1] + ordered[mid]) / 2)

    # Rank among every day the station ever measured, not just this month's
    # — "the 3rd widest day on record" is the sentence worth printing, and
    # a rank inside a 30-day window is not a rank.
    bigger = sum(1 for m in measured if m[3] > hero_swing)
    rank, of = bigger + 1, len(measured)
    # Standout counts the days this one STRICTLY beat, not its rank. On a
    # record where every day swung the same amount the competition rank is
    # still 1 — nothing is above it — and scoring that as a perfect 1.0
    # would let a month in which nothing whatsoever happened outrank a
    # genuinely violent one. Strictly-smaller makes a tie score zero, which
    # is what a tie means.
    smaller = sum(1 for m in measured if m[3] < hero_swing)
    standout = smaller / (of - 1) if of > 1 else None
    # …and if it beat NOTHING, there is no story. A perfectly flat station
    # still has a widest day — some day has to be the maximum — but "20°F
    # in one day" printed over a record where every single day swung 20°F
    # is a calendar entry, the same judgment the wildest-day producer makes
    # when nothing clears its notability floor.
    if not smaller:
        return []

    u = ctx.units
    month_label = f"{_MONTHS[month - 1]} {year}"
    period_label = f"{month_label} so far" if partial else month_label

    delta = hero_swing - typical
    comparison = Comparison(
        kind="vs_station_typical",
        label="vs this station's typical day",
        value=round(u.temp_delta(hero_swing), 1),
        baseline=round(u.temp_delta(typical), 1),
        baseline_label=(f"{u.temp_delta_deg(typical)} median swing across "
                        f"{of} measured days"),
        direction=("level" if abs(delta) < 1.0
                   else "above" if delta > 0 else "below"),
        delta=round(u.temp_delta(delta), 1),
        # A median swing of zero would mean a station whose thermometer
        # never moved; absent is not zero and neither is a percentage of it.
        delta_pct=(round(100 * delta / typical, 1) if typical else None),
        rank=rank, of=of,
        rank_line=_rank_line(rank, of, "days on record", "widest"))

    parts = {
        "magnitude": round(min(1.0, hero_swing / SWING_SATURATION_F), 4),
        # How much this month varied between its widest and narrowest day.
        # The spec's point about the small swing: a month holding both a
        # 42° day and a 9° one is a more interesting month than one holding
        # two 30° days, whatever its maximum.
        "contrast": round((hero_swing - narrowest[3]) / hero_swing, 4)
        if hero_swing > 0 else 0.0,
    }
    weights = [(0.35, parts["magnitude"]), (0.25, parts["contrast"])]
    if standout is not None:
        parts["standout"] = round(standout, 4)
        weights.append((0.40, parts["standout"]))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    supporting = [
        # The two READINGS the swing was measured between — offset and all.
        Stat("high", f"high on {_long_date(hero_day)}",
             round(u.temp(hero_hi), 1), u.temp_token, 1),
        Stat("low", f"low on {_long_date(hero_day)}",
             round(u.temp(hero_lo), 1), u.temp_token, 1),
        # …and the month's other end, which is the half that reveals the sky.
        Stat("narrowest_swing",
             f"narrowest swing · {_long_date(narrowest[0])}",
             round(u.temp_delta(narrowest[3]), 1), u.temp_token, 1),
        Stat("typical_swing", "the station's median day",
             round(u.temp_delta(typical), 1), u.temp_token, 1),
        Stat("days_measured", f"days measuring both ends in {period_label}",
             len(in_month), UNIT_DAYS),
    ]

    context = (
        f"{_long_date(hero_day)} ran from {u.temp_deg(hero_lo)} to "
        f"{u.temp_deg(hero_hi)}, a {u.temp_delta_deg(hero_swing)} swing, "
        f"against a median of {u.temp_delta_deg(typical)} here. "
        f"The narrowest day of {period_label} moved just "
        f"{u.temp_delta_deg(narrowest[3])}: wide days are clear and dry, "
        f"narrow ones are cloud, damp or a storm holding the air still.")

    return [Story(
        id=f"records.biggest_swing.{year}-{month:02d}",
        family=FAMILY_RECORDS,
        story_type="biggest_swing",
        title="Biggest Swing",
        emoji="🌡️",
        hero=Stat("swing", f"between high and low on {_long_date(hero_day)}",
                  round(u.temp_delta(hero_swing), 1), u.temp_token, 1),
        hero_line=f"{u.temp_delta_deg(hero_swing)} IN ONE DAY",
        context=context,
        comparison=comparison,
        supporting=supporting,
        # Every day of the month as a bar from its low to its high, in the
        # order they happened. `swing` is the bar LENGTH and converts by
        # scale; `low`/`high` are where the bar sits on the thermometer and
        # carry the offset. A template that derived the length by
        # subtracting the two would get the same number — and would be one
        # refactor away from subtracting two Celsius readings and calling
        # the result a Celsius swing, which is why the producer states it.
        viz=Viz(kind="swing_month",
                series=[{"key": d.isoformat(), "date": d.isoformat(),
                         "label": _short_date(d),
                         "swing": round(u.temp_delta(sw), 1),
                         "low": round(u.temp(lo), 1),
                         "high": round(u.temp(hi), 1),
                         "note": ("widest" if d == hero_day
                                  else "narrowest" if d == narrowest[0]
                                  else None),
                         "hero": d == hero_day}
                        for d, hi, lo, sw in in_month],
                unit=u.temp_token,
                axis_label=f"daily high-to-low range · {period_label}",
                # No median line: the median swing is a LENGTH, and on a
                # chart whose bars are POSITIONED low-to-high a length has
                # no honest place to sit. It lives in the stats and the
                # comparison instead; the footnote describes only what the
                # picture actually draws.
                footnote=("Each bar spans one day's low to its high, in "
                          "the order the month ran. The widest and the "
                          "narrowest days are named on the chart."),
                highlight=hero_day.isoformat(),
                highlight_key=hero_day.isoformat()),
        period=Period(kind="month", label=period_label,
                      start=f"{year}-{month:02d}-01",
                      end=in_month[-1][0].isoformat(),
                      partial=partial),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


@producer(FAMILY_RECORDS, "wildest_day")
async def wildest_day(ctx: StoryContext) -> list[Story]:
    """The day the database nominates, at two scopes.

    Every day the station recorded gets a chaos score built from the
    dimensions it actually measured — departure from that month's own
    normal, temperature swing, peak gust, rainfall, peak rain rate,
    pressure range — each ranked against THAT STATION'S own distribution.
    The winner of the newest month becomes the monthly card's callout; the
    winner of the newest year becomes the Worth Sharing hero.

    Declines when the station has fewer than MIN_STORY_DAYS rollup days (no
    distribution to rank against), when no dimension survives the pool
    check (nothing to rank), or — per scope — when the period's best day
    cleared nothing notable.
    """
    rows = await ctx.daily()
    if len(rows) < MIN_STORY_DAYS:
        return []

    days = _measure_days(rows, await _storm_peak_rates(ctx))
    pools = _pools(days)
    if not pools:
        return []
    _score_days(days, pools)
    scored = [d for d in days if d.scores]
    if len(scored) < MIN_STORY_DAYS:
        return []

    # Newest period WITH DATA, not the calendar's newest: a station that
    # stopped reporting last autumn still has a real story about last
    # autumn, and an empty "this month" would be the zero bug in costume.
    newest = scored[-1].on
    year, month = newest.year, newest.month

    year_days = [d for d in scored if d.on.year == year]
    month_days = [d for d in year_days if d.on.month == month]
    year_partial = year == ctx.today.year
    month_partial = (year, month) == (ctx.today.year, ctx.today.month)

    normals = await ctx.normals()
    year_label = _period_label(year, year_partial)
    year_story = _scope_story(
        ctx, "year", year_days, scored, pools,
        label=year_label, possessive=f"{year}'s", scope_of=f"of {year_label}",
        start=f"{year}-01-01",
        end=(ctx.today.isoformat() if year_partial else f"{year}-12-31"),
        partial=year_partial,
        story_id=f"records.wildest_day.year.{year}", normals=normals)

    month_label = (f"{_MONTHS[month - 1]} {year} so far" if month_partial
                   else f"{_MONTHS[month - 1]} {year}")
    last_of_month = (date(year + (month == 12), (month % 12) + 1, 1)
                     - timedelta(days=1))
    month_story = _scope_story(
        ctx, "month", month_days, scored, pools,
        label=month_label,
        possessive=f"{_MONTHS[month - 1]}'s", scope_of=f"of {month_label}",
        start=f"{year}-{month:02d}-01",
        end=(ctx.today.isoformat() if month_partial
             else last_of_month.isoformat()),
        partial=month_partial,
        story_id=f"records.wildest_day.month.{year}-{month:02d}",
        normals=normals)

    year_best, month_best = _best_day(year_days), _best_day(month_days)
    if (year_story is not None and month_story is not None
            and year_best is not None and month_best is not None
            and year_best.day == month_best.day):
        # One date won both scopes. Both stories still ship — the monthly
        # card fetches the month scope by name — but the month's claim is a
        # subset of the year's, so it ranks below its own superset instead
        # of showing the same day twice at the top of Worth Sharing.
        month_story = _demote_redundant(month_story)
    return [s for s in (year_story, month_story) if s is not None]


def _demote_redundant(story: Story, key: str = "redundant_scope") -> Story:
    """The same demotion, named for WHY: `redundant_scope` when a month
    repeats its year's claim, `redundant_rendering` when a second card
    re-tells the same month's numbers."""
    from dataclasses import replace
    return replace(story,
                   interestingness=round(story.interestingness
                                         * REDUNDANT_SCOPE, 4),
                   score_parts={**story.score_parts, key: REDUNDANT_SCOPE})


# ───────────────────────── Dry Spell ─────────────────────────
#
# "124 days. Not a drop." — and the whole card rests on one rule that is
# easy to get wrong and impossible to notice afterwards:
#
#   A DRY SPELL IS BUILT FROM MEASURED-ZERO DAYS, NOT FROM MISSING DAYS.
#
# A station that was unplugged for three months has not had a 90-day dry
# spell; it has had three months of not knowing. `insights.assemble`'s
# per-year `longest_dry_streak` counts consecutive ROLLUP ROWS, which is the
# right shape for the rain-gap card (a coverage gap cannot INFLATE it) but
# the wrong shape for this one: rows either side of a gap are still
# consecutive rows, so a run would be presented as unbroken across days
# nobody measured. This producer therefore walks the days itself and
# requires CALENDAR contiguity — a missing day, or a present day whose rain
# was never measured, ends the run.
#
# Everything else about comparability is borrowed rather than rewritten:
# `insights.comparable_to_date` decides which prior years may appear beside
# the current one, and `insights.RAIN_DAY_MIN_IN` is the one definition of a
# rain day (0.01 in, one bucket tip) that the copy then states out loud.

# Below this many recorded spells there is no distribution to rank a spell
# against — the same reasoning as MIN_DIM_POOL. A record whose only dry run
# is "everything since the box was opened" cannot say whether that is
# remarkable, so it declines instead of guessing.
MIN_SPELL_POOL = 3

# A copy floor, not a climate threshold: "four days. Not a drop." is not a
# sentence anybody shares, in any climate. Nothing else in this producer
# compares a station against an absolute number of days.
MIN_SPELL_DAYS = 5

# "And counting" is a claim about RIGHT NOW, so it needs the station to
# still be reporting. Two days of slack covers a poller that has not folded
# today's rollup yet without letting a station that went dark in March claim
# a spell that is still running.
LIVE_WITHIN_DAYS = 2

# A live spell this close to the station's record takes the hero slot: an
# ongoing drought is the story, a finished one is a fact. Below it the
# record leads and the live run rides along as a supporting stat.
LIVE_HERO_SHARE = 0.60


@dataclass(frozen=True)
class _Spell:
    """One unbroken run of MEASURED dry days.

    `broke_on` is the day the rain finally came — set only when the very
    next calendar day was measured and rained. A run that ends because the
    record ends, or because the station stopped measuring rain, has no
    breaking day and must not borrow the next reading it happens to find.
    """
    start: date
    end: date
    days: int
    broke_on: str | None
    broke_amount: float | None


def _dry_spells(rows: Sequence[dict[str, Any]]) -> list[_Spell]:
    """Every measured-dry run in the record, oldest first.

    Three things end a run, and only the first of them is rain:
      · a measured day at or above RAIN_DAY_MIN_IN (the run BROKE);
      · a day whose rain was never measured (absent is not zero);
      · a calendar gap — the next row is not the next day.
    """
    from . import insights as _ins
    out: list[_Spell] = []
    start: date | None = None
    prev: date | None = None

    def close(broke_on: str | None = None,
              broke_amount: float | None = None) -> None:
        nonlocal start, prev
        if start is not None and prev is not None:
            out.append(_Spell(start=start, end=prev,
                              days=(prev - start).days + 1,
                              broke_on=broke_on, broke_amount=broke_amount))
        start = prev = None

    for r in rows:
        try:
            on = date.fromisoformat(str(r.get("day") or ""))
        except ValueError:
            continue
        rain = _day_rain_in(r)
        contiguous = prev is not None and on == prev + timedelta(days=1)
        if rain is None:
            # The station was up and the rain gauge was not. A run cannot be
            # carried across a day nobody measured.
            close()
            continue
        if rain >= _ins.RAIN_DAY_MIN_IN:
            # Only a run this day directly follows was BROKEN by it. After a
            # gap the run already ended, on the last day anybody measured,
            # and the next rain it happens to find is not its ending.
            if contiguous:
                close(str(r.get("day")), round(rain, 3))
            else:
                close()
            continue
        if not contiguous:
            close()
            start = on
        elif start is None:
            start = on
        prev = on
    close()
    return out


def _spell_in_window(spells: Sequence[_Spell], year: int,
                     cutoff: date) -> _Spell | None:
    """The longest dry run this station had LIVED THROUGH by `cutoff` in
    `year`, truncated at the cutoff.

    A spell belongs to a year when it reached into it, and its length is
    counted from its own start — even a start in the previous December —
    through the cutoff. That is what makes the year bars comparable: on
    August 30 the question is "how long a run had this station been through
    by this date", asked identically of every year, rather than "how much
    dry weather fits inside a calendar year", which would split every
    winter drought at New Year and flatter no year consistently.
    """
    first = date(year, 1, 1)
    best: _Spell | None = None
    for s in spells:
        if s.end < first or s.start > cutoff:
            continue
        cut = min(s.end, cutoff)
        days = (cut - s.start).days + 1
        truncated = cut != s.end
        cand = _Spell(start=s.start, end=cut, days=days,
                      broke_on=None if truncated else s.broke_on,
                      broke_amount=None if truncated else s.broke_amount)
        if best is None or cand.days > best.days:
            best = cand
    return best


def _spell_period(s: _Spell, ongoing: bool) -> Period:
    return Period(kind="spell",
                  label=(f"{_long_date(s.start)} {s.start.year} to "
                         f"{_long_date(s.end)} {s.end.year}"),
                  start=s.start.isoformat(), end=s.end.isoformat(),
                  partial=ongoing)


@producer(FAMILY_CLIMATE, "dry_spell")
async def dry_spell(ctx: StoryContext) -> list[Story]:
    """The station's longest run of days that measured no rain.

    Hero is the LIVE spell when one is running and has reached
    LIVE_HERO_SHARE of the station's record — an ongoing drought is the
    story people share, and "and counting" is the line that makes it one.
    Otherwise the record spell leads and the live run appears beneath it.

    Declines when the station has too little history (MIN_STORY_DAYS), when
    fewer than MIN_SPELL_POOL dry runs exist to rank against, or when the
    best run is shorter than MIN_SPELL_DAYS. A station whose rain gauge
    never reported has NO dry spells rather than one long one: every day is
    unmeasured, every run is empty, and the pool check declines.
    """
    from . import insights as _ins
    rows = await ctx.daily()
    if len(rows) < MIN_STORY_DAYS:
        return []
    spells = _dry_spells(rows)
    if len(spells) < MIN_SPELL_POOL:
        return []

    payload = await ctx.insights()
    years = payload.get("years") or []
    if not years:
        return []
    anchor_md = str(payload.get("ledger_anchor") or ctx.today.strftime("%m-%d"))
    newest = int(years[-1]["year"])
    partial = newest == ctx.today.year

    # One frame for every year, exactly as the heat ledger does it: a
    # running year is measured through today's month-day in EVERY year it is
    # compared with, and a finished record compares whole years.
    def cutoff_for(year: int) -> date:
        if not partial:
            return date(year, 12, 31)
        window = _ins.window_days_to_anchor(year, anchor_md)
        return date(year, 1, 1) + timedelta(days=max(0, window - 1))

    per_year: dict[int, _Spell] = {}
    for yr in years:
        y = int(yr["year"])
        found = _spell_in_window(spells, y, cutoff_for(y))
        if found is not None:
            per_year[y] = found
    if not per_year:
        return []

    # The record is the biggest bar, so the hero and the chart can never
    # disagree about the same number.
    record_year = max(per_year, key=lambda y: (per_year[y].days, y))
    record = per_year[record_year]

    # Live only when the station is still reporting: the newest recorded day
    # is the end of the newest spell AND that day is recent.
    last_day = str(rows[-1].get("day") or "")
    live: _Spell | None = None
    try:
        last_on = date.fromisoformat(last_day)
    except ValueError:
        last_on = None
    if (last_on is not None
            and (ctx.today - last_on).days <= LIVE_WITHIN_DAYS
            and spells and spells[-1].end == last_on):
        live = spells[-1]

    # The live run leads only when it has reached LIVE_HERO_SHARE of the
    # record AND is the newest year's own longest. A year that already
    # produced a longer run has said its piece; "and counting" beside a
    # smaller number than the same year's bar would make the hero line and
    # the chart disagree, which is the one thing a card may never do.
    hero_is_live = (live is not None
                    and live.days >= LIVE_HERO_SHARE * record.days
                    and live.days == per_year.get(newest, live).days)
    hero = live if (hero_is_live and live is not None) else record
    if hero.days < MIN_SPELL_DAYS:
        return []

    lengths = [s.days for s in spells]
    typical = _median(lengths)
    # Two station-relative axes, no absolute weather thresholds anywhere:
    #   dominance — how far this run towers over an ordinary dry run HERE,
    #               which is what separates a desert drought from a wet
    #               climate's slightly-longer-than-usual gap;
    #   rarity    — where it sits in the station's own distribution of runs,
    #               mid-ranked so the longest of four cannot claim 1.0.
    dominance = max(0.0, min(1.0, 1.0 - typical / hero.days))
    rarity = _rank_share(lengths, hero.days)

    # Prior years, comparability decided by insights' one definition — but
    # asked IN THE FRAME THE BARS USE. `comparable_to_date` is published
    # against the to-date window (Jan 1 → today's month-day); when the
    # newest year is finished the bars are whole years, and a year covered
    # Jan–Aug and dark Sep–Dec passes the to-date test while its longest
    # run never had the chance to span the autumn nobody measured. So the
    # published flag is read only when the two frames coincide (a running
    # year), and the whole-year frame counts each year's measured days
    # through its own cutoff and calls the same rule directly, exactly as
    # the ledgers and degree-day producers do. Either way the signal is
    # POSITIVE-ONLY: a year that did not cover the window is left out,
    # never folded in as a low number.
    if partial:
        comparable = {int(y["year"]) for y in years
                      if y.get("comparable_to_date") is True}
    else:
        measured: dict[int, int] = {}
        for r in rows:
            try:
                on = date.fromisoformat(str(r.get("day") or ""))
            except ValueError:
                continue
            if on <= cutoff_for(on.year):
                measured[on.year] = measured.get(on.year, 0) + 1
        reference = measured.get(newest, 0)
        comparable = {y for y, n in measured.items()
                      if _ins.comparable_to_date(n, reference)}
    bars = [(y, per_year[y], y in comparable) for y in sorted(per_year)]
    prior = [(y, s.days) for y, s, ok in bars if ok and y != newest]

    comparison: Comparison | None = None
    standout: float | None = None
    if prior:
        baseline = sum(d for _, d in prior) / len(prior)
        rank = 1 + sum(1 for _, d in prior if d > hero.days)
        of = len(prior) + 1
        delta = hero.days - baseline
        span = (f"{min(y for y, _ in prior)}–{max(y for y, _ in prior)} average"
                if len(prior) > 1 else str(prior[0][0]))
        window = (f" through {_short_date(ctx.today)}" if partial else "")
        comparison = Comparison(
            kind="prior_years_to_date" if partial else "prior_years_full",
            label=(f"vs the longest run in "
                   f"{_WORDS.get(len(prior), len(prior))} earlier "
                   f"year{'s' if len(prior) > 1 else ''}"),
            value=hero.days,
            baseline=round(baseline, 1),
            baseline_label=f"{span}{window}",
            direction=("level" if abs(delta) < 0.5
                       else "above" if delta > 0 else "below"),
            delta=round(delta, 1),
            delta_pct=(round(100 * delta / baseline, 1) if baseline else None),
            rank=rank, of=of,
            rank_line=_rank_line(rank, of, "comparable years", "longest"))
        standout = (of - rank) / (of - 1) if of > 1 else None

    parts = {"dominance": round(dominance, 4), "rarity": round(rarity, 4)}
    weights = [(0.40, dominance), (0.35, rarity)]
    if standout is not None:
        parts["standout"] = round(standout, 4)
        weights.append((0.25, standout))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    ongoing = hero_is_live
    u = ctx.units
    hero_line = (f"{hero.days} DAYS AND COUNTING. NOT A DROP." if ongoing
                 else f"{hero.days} DAYS. NOT A DROP.")
    # The definition ships ON the card, and so does the coverage rule — the
    # number means nothing without both, and a reader who cannot check the
    # claim has to take it on trust. The threshold is one bucket tip either
    # way; the reader sees it in the units their app is set to.
    context = (
        f"{_long_date(hero.start)} {hero.start.year} through "
        f"{_long_date(hero.end)} {hero.end.year}: {hero.days} straight days "
        f"on which this station measured less than "
        f"{u.rain_amount(_ins.RAIN_DAY_MIN_IN)} of rain, "
        f"the smallest amount a tipping bucket records. Every one of them "
        f"was measured. A day the station missed breaks the run.")

    supporting: list[Stat] = []
    if ongoing:
        supporting.append(Stat("record_spell",
                               ("the station's longest run"
                                if hero.days < record.days
                                else "and it is the station's longest run"),
                               record.days, UNIT_DAYS))
    elif live is not None:
        supporting.append(Stat("current_spell", "dry right now", live.days,
                               UNIT_DAYS))
    supporting.append(Stat("typical_spell", "a typical dry run here", typical,
                           UNIT_DAYS))
    if hero.broke_on and hero.broke_amount is not None:
        supporting.append(Stat("broke_amount",
                               f"the rain that ended it · {hero.broke_on}",
                               round(u.rain_value(hero.broke_amount),
                                     u.rain_precision),
                               u.rain_token, u.rain_precision))
    supporting.append(Stat("spells_ranked", "dry runs on record", len(spells)))
    supporting.append(Stat("days_recorded", "days recorded", len(rows),
                           UNIT_DAYS))

    hero_year = newest if ongoing else record_year
    return [Story(
        id=(f"climate.dry_spell.{hero.start.isoformat()}"),
        family=FAMILY_CLIMATE,
        story_type="dry_spell",
        title="Dry Spell",
        emoji="🏜️",
        hero=Stat("spell_days", "consecutive days with no measurable rain",
                  hero.days, UNIT_DAYS),
        hero_line=hero_line,
        context=context,
        comparison=comparison,
        supporting=supporting,
        # Every year gets a bar, and each bar says whether it may be READ
        # beside the others: a year the station spent mostly offline has a
        # short longest-run because it has few days, and drawing it as an
        # equal is how a card manufactures a record. The client greys it;
        # the baseline above never sees it.
        # `range_label` is the dates as COPY; `start`/`end` stay as the ISO
        # data beside it. `note` is how a still-running bar says so in the
        # producer's own words instead of the template reaching for a glyph.
        viz=Viz(kind="dry_spell_years",
                series=[{"key": str(y), "year": y, "days": s.days,
                         "start": s.start.isoformat(),
                         "end": s.end.isoformat(),
                         "range_label": _range_label(s.start, s.end),
                         "comparable": ok,
                         "ongoing": ongoing and y == hero_year,
                         "note": ("still running" if ongoing and y == hero_year
                                  else None),
                         "hero": y == hero_year}
                        for y, s, ok in bars],
                unit=UNIT_DAYS,
                axis_label=("longest run without measurable rain"
                            + (f", through {_short_date(ctx.today)}"
                               if partial else "")),
                # Only when there is actually a faded bar to explain.
                footnote=(INCOMPARABLE_FOOTNOTE
                          if any(not ok for _, _, ok in bars) else None),
                highlight=hero_year, highlight_key=str(hero_year)),
        period=_spell_period(hero, ongoing),
        station=ctx.station(payload),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# ─────────────────── the humidity / storm month family ───────────────────
#
# One producer, two renderings, per the design review's merge of "Monsoon
# Meter" and "Humidity Invasion": the month's humidity-and-storm CHARACTER
# (peak dew point, the day it peaked, rain total, rainy days, the wettest
# day) and the DEW-POINT BAND timeline (how many days the air spent in each
# band). Same month, same numbers, two cards.
#
# ⚠️ "Monsoon" is a Chandler word. It is earned from the STATION'S OWN
# distribution and nothing else — no latitude, no region, no hardcoded
# station. What "monsoon" actually names is a CONTRAST: a place that is dry
# almost all year, into which humid air arrives with storms. So the title
# turns on three measured things at once — the month is humid, the rest of
# the station's record is not, and rain actually fell. A station that is
# muggy every month of its life has no invasion to report and gets the
# sticky-season title; a station whose air never gets there gets a plain
# one. The neutral copy contains no monsoon vocabulary at all.
#
# Dew-point band EDGES are absolute °F, unlike every threshold in the
# Wildest Day scorer, and deliberately: a 70°F dew point is oppressive in
# Chandler and in Charleston alike. Human comfort does not renormalize per
# station. The station-relative work is done by `contrast` in the score and
# by the flavour rule above.

# The day's PEAK dew point is what places it in a band: the muggy hour is
# the one people remember, and a band claimed on the day's peak makes "very
# dry" a strong statement (even at its worst the air stayed there).
DEW_BANDS: tuple[tuple[float | None, float | None, str], ...] = (
    (None,  50.0, "very dry"),
    (50.0,  60.0, "dry"),
    (60.0,  65.0, "noticeable"),
    (65.0,  70.0, "humid"),
    (70.0,  None, "very humid"),
)

# The line where most people stop calling it warm and start calling it
# sticky — the bottom of the "humid" band, and the one number the flavour
# rule counts days against.
HUMID_DEW_F = 65.0

# Flavour rule, all three measured:
#   the month is humid …
MONSOON_MONTH_SHARE = 0.40
#   … the station, the rest of the time, is not …
MONSOON_STATION_SHARE = 0.25
#   … and the humidity arrived with weather, not just discomfort.
MONSOON_MIN_RAIN_DAYS = 3
# A humid month on a station that is humid anyway: real, worth a card, and
# not an invasion.
STICKY_MONTH_SHARE = MONSOON_MONTH_SHARE

# Under this many days with a dew-point reading there is no month to
# describe — a band histogram over four days is a rumour.
MIN_BAND_DAYS = 10

# The hero band is the HIGHEST band holding at least this many days: the
# ceiling the month actually reached and held, rather than a single sticky
# afternoon. Falls back to the fullest band when nothing clears it.
BAND_HERO_MIN = 3

_FLAVOUR_TITLES = {
    # flavour → (character-card title, band-timeline title, emoji)
    "monsoon": ("Monsoon Meter", "Humidity Invasion", "🌩️"),
    "sticky":  ("The Sticky Season", "Days You Felt It", "🥵"),
    "neutral": ("Humidity & Rain", "How Humid Was It?", "💧"),
}


def _dew_band(dew: float) -> str:
    for lo, hi, name in DEW_BANDS:
        if (lo is None or dew >= lo) and (hi is None or dew < hi):
            return name
    return DEW_BANDS[-1][2]


def _band_label(lo: float | None, hi: float | None, name: str,
                u: Units) -> str:
    """The band's range, spelled out in the reader's scale. A band drawn
    without its numbers is a mood; the card carries the degrees so the
    reader can place their own day. Band EDGES are readings, so they take
    the offset conversion — 65°F is 18.3°C, not 36.1."""
    if lo is None:
        return f"{name} · under {u.temp_deg(hi or 0)}"
    if hi is None:
        return f"{name} · {u.temp_deg(lo)} and up"
    return f"{name} · {u.temp_text(lo)}–{u.temp_deg(hi)}"


@dataclass
class _MonthAir:
    """One month's humidity and rain, measured. Every field may be absent,
    and absent means the station did not measure it — never zero."""
    year: int
    month: int
    days: list[dict[str, Any]] = field(default_factory=list)
    dew: list[tuple[str, float]] = field(default_factory=list)  # (day, peak)
    rain: list[tuple[str, float]] = field(default_factory=list)

    @property
    def peak(self) -> tuple[str, float] | None:
        return max(self.dew, key=lambda t: (t[1], t[0])) if self.dew else None

    def bands(self) -> dict[str, int]:
        out = {name: 0 for _, _, name in DEW_BANDS}
        for _, v in self.dew:
            out[_dew_band(v)] += 1
        return out

    @property
    def humid_days(self) -> int:
        return sum(1 for _, v in self.dew if v >= HUMID_DEW_F)


def _month_air(rows: Sequence[dict[str, Any]]) -> dict[tuple[int, int], _MonthAir]:
    """Rollup rows folded per calendar month. Dew point rides
    `dew_point_max` — the day's peak — and a day whose sensor never reported
    contributes no entry at all rather than a zero that would drag a band
    histogram into "very dry"."""
    out: dict[tuple[int, int], _MonthAir] = {}
    for r in rows:
        day = str(r.get("day") or "")
        try:
            on = date.fromisoformat(day)
        except ValueError:
            continue
        key = (on.year, on.month)
        air = out.setdefault(key, _MonthAir(year=on.year, month=on.month))
        air.days.append(r)
        dew = _num(r.get("dew_point_max"))
        if dew is not None:
            air.dew.append((day, dew))
        # Rain provenance is inherited from ingest, exactly as Wildest Day
        # explains: `_day_rain_in` reads the normalized columns the tipping
        # gauge already won, and returns None — not 0.0 — when nothing
        # measured rain at all.
        rain = _day_rain_in(r)
        if rain is not None:
            air.rain.append((day, rain))
    return out


def _month_point(row: dict[str, Any], u: Units) -> dict[str, Any]:
    """One day on the Monsoon Meter. `None` means the station measured
    nothing that day — the card draws a break in the line, never a zero
    that would read as bone-dry air.

    The BAND is decided on the stored Fahrenheit value and only the
    displayed number converts: banding a Celsius reading against a
    Fahrenheit edge is the exact bug this repo keeps re-shipping.

    WHAT `band` IS FOR (asked by the card templates, 2026-08-30, and the
    answer is "keep it"): it is a BACKGROUND-SHADING SPEC, not a second
    label. The dew-point line is continuous, so the comfort bands cannot be
    read off it; a template shades each day's column by its band name and
    the reader can see the month climb out of "dry" into "very humid"
    without a single number being printed. A template that does not shade
    ignores the key — it costs one short string a day — and the names are
    the SAME stable strings the `dew_point_bands` viz uses, so one palette
    serves both cards and neither template decides where a band begins.
    """
    dew = _num(row.get("dew_point_max"))
    rain = _day_rain_in(row)
    day = str(row.get("day"))
    return {"key": day, "day": day,
            "dew_point": None if dew is None else round(u.temp(dew), 1),
            "band": None if dew is None else _dew_band(dew),
            "rain": (None if rain is None
                     else round(u.rain_value(rain), u.rain_precision))}


def _flavour(month_share: float, station_share: float | None,
             rain_days: int | None) -> str:
    """Which vocabulary this month has earned.

    `station_share` is None when the station has too little history outside
    this month to know what its normal is — and without that, no contrast
    can be claimed, so the month cannot be called an invasion of anything.
    """
    if month_share < STICKY_MONTH_SHARE:
        return "neutral"
    if (station_share is not None
            and station_share <= MONSOON_STATION_SHARE
            and (rain_days or 0) >= MONSOON_MIN_RAIN_DAYS):
        return "monsoon"
    return "sticky"


@producer(FAMILY_CLIMATE, "humid_month")
async def humid_month(ctx: StoryContext) -> list[Story]:
    """The newest month's humidity and storms, at two renderings.

    Declines outright when the station has no dew-point sensor: a station
    that never measured dew point has not had a dry month, it has had no
    reading. That is the whole reason this producer exists as a decline path
    as much as a card.
    """
    from . import insights as _ins
    rows = await ctx.daily()
    if len(rows) < MIN_STORY_DAYS:
        return []
    months = _month_air(rows)
    if not months:
        return []

    # Newest month WITH DATA, not the calendar's — the same rule the other
    # producers follow so a station that stopped reporting still gets its
    # last real month instead of an invented empty one.
    key = max(months)
    air = months[key]
    year, month = key
    if len(air.dew) < MIN_BAND_DAYS:
        return []
    peak = air.peak
    if peak is None:
        return []

    # The station's own baseline, THIS MONTH EXCLUDED: the question the
    # flavour rule asks is "is the rest of the year like this?", and folding
    # the month into its own comparison answers a different one.
    other_dew = [v for k, m in months.items() if k != key for _, v in m.dew]
    station_share: float | None = None
    if len(other_dew) >= MIN_STORY_DAYS:
        station_share = sum(1 for v in other_dew
                            if v >= HUMID_DEW_F) / len(other_dew)

    dew_days = len(air.dew)
    month_share = air.humid_days / dew_days

    # Rain is a separate sensor and a separate absence. A month with no rain
    # measurement keeps its humidity story and drops every rain line.
    rain_measured = bool(air.rain)
    rain_total = round(sum(v for _, v in air.rain), 2) if rain_measured else None
    rain_days = (sum(1 for _, v in air.rain if v >= _ins.RAIN_DAY_MIN_IN)
                 if rain_measured else None)
    wettest = (max(air.rain, key=lambda t: (t[1], t[0]))
               if rain_measured else None)
    if wettest is not None and wettest[1] < _ins.RAIN_DAY_MIN_IN:
        wettest = None                      # a trace is not a wettest day

    flavour = _flavour(month_share, station_share, rain_days)
    partial = (year, month) == (ctx.today.year, ctx.today.month)
    label = (f"{_MONTHS[month - 1]} {year} so far" if partial
             else f"{_MONTHS[month - 1]} {year}")
    last_of_month = (date(year + (month == 12), (month % 12) + 1, 1)
                     - timedelta(days=1))
    period = Period(kind="month", label=label,
                    start=f"{year}-{month:02d}-01",
                    end=(ctx.today.isoformat() if partial
                         else last_of_month.isoformat()),
                    partial=partial)
    payload = await ctx.insights()
    station = ctx.station(payload)

    # ── the band ladder ─────────────────────────────────────────────────
    # The hero band is the HIGHEST band holding at least BAND_HERO_MIN days:
    # the ceiling the month actually reached AND HELD, rather than one sticky
    # afternoon. The same index is the score's `reach`, so the headline band
    # and the number that ranked the story can never come apart.
    bands = air.bands()
    ladder = [(lo, hi, name, bands[name]) for lo, hi, name in DEW_BANDS]
    held = [i for i, (_, _, _, n) in enumerate(ladder) if n >= BAND_HERO_MIN]
    hero_idx = (held[-1] if held
                else max(range(len(ladder)), key=lambda i: (ladder[i][3], i)))
    b_lo, b_hi, b_name, b_days = ladder[hero_idx]

    # ── scoring ─────────────────────────────────────────────────────────
    # `reach` and `saturation` ride the ABSOLUTE comfort scale, which is the
    # honest axis for a card whose subject is how the air felt — the same
    # shape as the heat ledger's tier ladder, and for the same reason. Only
    # `contrast` is station-relative, mid-ranked against the station's own
    # days so the muggiest of four readings cannot claim 1.0. Together they
    # are what keeps a bone-dry station's least-dry month from scoring like
    # a wet season: it wins the contrast and loses the ladder.
    # Anything the station did not measure is dropped and the weighted mean
    # renormalizes, the move `how_hot_is_hot` makes when standout is missing.
    reach = (hero_idx + 1) / len(ladder)
    saturation = month_share
    parts: dict[str, float] = {"reach": round(reach, 4),
                               "saturation": round(saturation, 4)}
    weights: list[tuple[float, float]] = [(0.35, reach), (0.30, saturation)]

    if station_share is not None:
        # Where the month's TYPICAL day sits among the station's other days.
        # The peak answers "how bad did it get"; this answers "was the whole
        # month unlike here", which is the half a single storm cannot fake.
        typical = _median([v for _, v in air.dew])
        contrast = _rank_share(other_dew, typical)
        parts["contrast"] = round(contrast, 4)
        weights.append((0.20, contrast))

    # Rain days as a SHARE, never a total: a partial month has fewer days,
    # and ranking its running total against finished months would call every
    # month-in-progress the driest on record.
    if rain_measured and rain_days is not None:
        shares = [sum(1 for _, v in m.rain if v >= _ins.RAIN_DAY_MIN_IN)
                  / len(m.rain)
                  for m in months.values() if len(m.rain) >= MIN_BAND_DAYS]
        if len(shares) >= MIN_DIM_POOL:
            wetness = _rank_share(shares, rain_days / len(air.rain))
            parts["wetness"] = round(wetness, 4)
            weights.append((0.15, wetness))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)
    score = round(min(1.0, max(0.0, score)), 4)

    char_title, band_title, emoji = _FLAVOUR_TITLES[flavour]
    out: list[Story] = []

    # ── rendering 1: the month's character ───────────────────────────────
    u = ctx.units
    peak_day = date.fromisoformat(peak[0])
    felt = _share_phrase(air.humid_days, dew_days)
    sentences = [
        f"{label}: the dew point peaked at {u.temp_deg(peak[1])} on "
        f"{_long_date(peak_day)}"]
    if air.humid_days:
        sentences[0] += (
            f", and the air reached {u.temp_deg(HUMID_DEW_F)} dew point or "
            f"higher on {air.humid_days} of the {dew_days} days measured"
            + (f", {felt}" if felt else ""))
    else:
        sentences[0] += (
            f", and never reached the {u.temp_deg(HUMID_DEW_F)} dew point "
            f"most people call humid")
    if rain_total is not None and rain_days:
        sentences.append(
            f"{u.rain_amount(rain_total)} of rain fell on {rain_days} of them"
            + (f", {u.rain_amount(wettest[1])} of it on "
               f"{_long_date(date.fromisoformat(wettest[0]))}"
               if wettest else ""))
    elif rain_total is not None:
        sentences.append("no day recorded measurable rain")
    context = ". ".join(sentences) + "."

    supporting = [
        Stat("humid_days",
             f"days at {u.temp_deg(HUMID_DEW_F)} dew point or higher",
             air.humid_days, UNIT_DAYS),
        Stat("dew_days_measured", "days with a dew-point reading", dew_days,
             UNIT_DAYS),
    ]
    if rain_total is not None:
        supporting.append(Stat("rain_total", "rain this month",
                               round(u.rain_value(rain_total),
                                     u.rain_precision),
                               u.rain_token, u.rain_precision))
    if rain_days is not None:
        supporting.append(Stat(
            "rain_days",
            f"days with at least {u.rain_amount(_ins.RAIN_DAY_MIN_IN)}",
            rain_days, UNIT_DAYS))
    if wettest is not None:
        supporting.append(Stat(
            "wettest_day",
            f"wettest day · {_long_date(date.fromisoformat(wettest[0]))}",
            round(u.rain_value(wettest[1]), u.rain_precision),
            u.rain_token, u.rain_precision))
    # Peak rain RATE, when a storm episode recorded one. Same bounded table
    # Wildest Day reads; absent for a month with no recorded storm, which is
    # a missing reading and not a calm month.
    rates = await _storm_peak_rates(ctx)
    in_month = [v for d, v in rates.items()
                if d[:7] == f"{year}-{month:02d}"]
    if in_month:
        supporting.append(Stat("peak_rate", "hardest rain rate",
                               round(u.rain_value(max(in_month)),
                                     u.rain_precision),
                               u.rate_token, u.rain_precision))

    comparison: Comparison | None = None
    if station_share is not None:
        delta = month_share - station_share
        comparison = Comparison(
            kind="station_days",
            label=(f"vs the rest of this station's record"),
            value=round(month_share, 4),
            baseline=round(station_share, 4),
            baseline_label=(f"{len(other_dew)} other days measured here"),
            direction=("level" if abs(delta) < 0.01
                       else "above" if delta > 0 else "below"),
            delta=round(delta, 4),
            delta_pct=(round(100 * delta / station_share, 1)
                       if station_share else None),
            # A share against a share: there is no ranking here, so there is
            # no rank line either. The field stays absent rather than
            # inventing a leaderboard of one.
            rank_line=None)

    month_points = [_month_point(r, u) for r in air.days]
    # The hero STAT and the hero LINE are the same reading, so they carry the
    # same precision: the card was printing "DEW POINT PEAKED AT 74°F" across
    # the top and "74.0°F" in the stat beneath it. `_sig` decides once and
    # both follow. The chart series keeps its uniform one decimal — an axis
    # with per-point precision is unreadable, and that IS a different surface.
    peak_shown = round(u.temp(peak[1]), 1)
    out.append(Story(
        id=f"climate.humid_month.{year}-{month:02d}",
        family=FAMILY_CLIMATE,
        story_type="humid_month",
        title=char_title,
        emoji=emoji,
        hero=Stat("peak_dew_point", f"peak dew point · {_long_date(peak_day)}",
                  peak_shown, u.temp_token, _sig_precision(peak_shown)),
        hero_line=f"DEW POINT PEAKED AT {u.temp_deg(peak[1])}",
        context=context,
        comparison=comparison,
        supporting=supporting,
        # The Monsoon Meter itself: the month's dew-point line with the
        # day's rain under it. One entry per day the station measured
        # either, and `dew_point: null` on a day it measured neither.
        viz=Viz(kind="humidity_month",
                series=month_points,
                unit=u.temp_token,
                axis_label="daily peak dew point",
                # A break in the line is a visual distinction with no words
                # on it, and the wrong reading of one is the worst reading
                # available: a gap looks like a plunge to bone-dry air.
                footnote=("Gaps in the line are days this station measured "
                          "no dew point, not dry days."
                          if any(p["dew_point"] is None for p in month_points)
                          else None),
                highlight=peak[0], highlight_key=peak[0]),
        period=period,
        station=station,
        interestingness=score,
        score_parts={**parts, "flavour_monsoon": 1.0 if flavour == "monsoon"
                     else 0.0},
    ))

    # ── rendering 2: the band timeline ───────────────────────────────────
    band_parts = {"reach": parts["reach"],
                  "concentration": round(b_days / dew_days, 4)}
    band_weights = [(0.40, band_parts["reach"]),
                    (0.35, band_parts["concentration"])]
    if "contrast" in parts:
        band_parts["contrast"] = parts["contrast"]
        band_weights.append((0.25, parts["contrast"]))
    band_score = sum(w * v for w, v in band_weights) / sum(
        w for w, _ in band_weights)

    # A second rendering of the SAME month, not a month inside its year:
    # the demotion is identical, the reason is not, and the key says which.
    out.append(_demote_redundant(Story(
        id=f"climate.dew_bands.{year}-{month:02d}",
        family=FAMILY_CLIMATE,
        story_type="dew_point_bands",
        title=band_title,
        emoji=emoji if flavour != "neutral" else "💦",
        hero=Stat("band_days", f"days of {b_name} air", b_days, UNIT_DAYS),
        hero_line=f"{b_days} {b_name.upper()} DAYS",
        context=(f"{label}: each day placed by its PEAK dew point, so a day "
                 f"counts as {b_name} only if its muggiest hour got there. "
                 f"{b_days} of the {dew_days} days measured landed in "
                 f"{_band_label(b_lo, b_hi, b_name, u)}."),
        comparison=comparison,
        supporting=[Stat(f"band_{name.replace(' ', '_')}",
                         _band_label(lo, hi, name, u), n, UNIT_DAYS)
                    for lo, hi, name, n in ladder],
        # `min`/`max` are the band edges in the reader's scale; `key` is the
        # band's name, which is the same string in every unit system.
        viz=Viz(kind="dew_point_bands",
                series=[{"key": name, "band": name,
                         "label": _band_label(lo, hi, name, u),
                         "days": n, "share": round(n / dew_days, 4),
                         "min": None if lo is None else round(u.temp(lo), 1),
                         "max": None if hi is None else round(u.temp(hi), 1)}
                        for lo, hi, name, n in ladder],
                unit=u.temp_token,
                axis_label="days",
                highlight=b_name, highlight_key=b_name),
        period=period,
        station=station,
        interestingness=round(min(1.0, max(0.0, band_score)), 4),
        score_parts={**band_parts,
                     "flavour_monsoon": 1.0 if flavour == "monsoon" else 0.0},
    ), key="redundant_rendering"))
    return out


# ───────────────────────── Air & Flight ─────────────────────────
#
# ⚠️ THE FRAMING IS THE FEATURE, and it is the one thing in this module that
# is not a judgement call. This card is LOCAL WEATHER SCIENCE. It is not a
# pilot report, it is not a PIREP, and it must never read as one:
#
#   · the disclaimer ships ON THE IMAGE (Story.disclaimer exists for exactly
#     this, and the science-family template renders it);
#   · there are NO visibility, ceiling or official-altimeter rows, because
#     this server consumes no METAR source and every one of those numbers
#     would be invented. A row we cannot measure is a row we do not draw —
#     the same rule as "absent is not zero", applied to a whole product;
#   · the vocabulary stays plain. "The air is acting like 4,284 ft" is
#     something anybody understands and nobody could mistake for an
#     operational product.
#
# The physics is NOT reimplemented here. `derived.density_altitude_ft` is
# the ONE definition in this codebase — the NWS El Paso chain (vapor
# pressure → virtual temperature → feet) that the app's Science screen
# already renders — and it is called, not copied. It takes STATION pressure
# (`baromabsin`), never a sea-level-corrected one, and this producer
# declines rather than substituting: feeding SLP to that chain is the
# classic wrong-answer path and it fails silently, with a plausible number.
#
# ELEVATION IS THE HARD PART. "Absent is not zero" has unusual force at
# altitude: reading an unknown elevation as sea level would hand a Denver
# operator a 5,000 ft "penalty" that is entirely fiction. The only source
# this server has that is unambiguous about its own units is
# `settings.station_elevation_ft`, which the operator sets and which is
# already load-bearing for the ingest-time pressure correction. When it is
# unset the CONTRAST LINE declines — the density altitude itself is still a
# true, complete, standalone fact, so the story ships without it and the
# score renormalizes over the dimensions that remain, exactly the way
# `how_hot_is_hot` drops its standout term.
#
# (The AWN device payload also carries an elevation under `info.coords`, but
# its unit is undocumented here and `db.list_devices` REPLACES that whole
# `coords` object when an operator sets a location override — so it is both
# unverified and destructible. A guessed unit at altitude is the same class
# of bug as a guessed zero.)

AIR_DISCLAIMER = "Local weather science · not for flight planning."

# The two scoring ladders are ABSOLUTE, unlike the Wildest Day scorer's
# station-relative floors, and for the same reason the dew-point bands are:
# density altitude is a physical fact about the air, not a local custom. Two
# thousand feet of extra density altitude costs the same lift in Chandler as
# in Irwin PA. What IS station-relative — where the station sits — is the
# contrast term, and that is exactly the term that declines when unknown.
PENALTY_FULL_FT = 5000.0        # air acting 5,000 ft higher than the ground
THINNESS_FULL_FT = 10000.0      # …and 10,000 ft is thin in anybody's book
MOISTURE_FULL_FT = 1200.0       # what humidity alone is worth on a wet day

# Below this the elevation reading is not believable as a station site
# (Dead Sea shore ≈ −1,400 ft; nothing on land is above ~19,000 ft), so a
# fat-fingered env value declines the contrast rather than headlining it.
ELEVATION_MIN_FT = -2000.0
ELEVATION_MAX_FT = 20000.0

# The dew point the "how much is the humidity worth" stat measures against:
# air this dry contributes essentially nothing to the virtual temperature,
# so the difference IS the moisture's share. Not a threshold anything is
# compared to — a reference point for one subtraction.
BONE_DRY_DEW_F = -40.0


def _feet(v: float) -> str:
    """A number of feet for COPY: thousands separated, no decimal. Density
    altitude is quoted to the foot by the calculators people know, and a
    fractional foot on a share card is noise."""
    return f"{round(v):,}"


def _station_elevation_ft() -> float | None:
    """The station's elevation in feet, or None when nobody has said.

    `station_elevation_ft` defaults to 0.0 and its documented meaning there
    is "off", so 0.0 reads as UNKNOWN rather than as sea level. That costs a
    genuinely-at-sea-level operator their contrast line, which is the right
    side to be wrong on: the setting cannot tell the two apart, and inventing
    a 4,000 ft penalty for a mile-high station that never configured one is
    the failure that actually misleads. A NEGATIVE elevation is knowledge,
    not absence, and passes through.
    """
    from .config import settings
    v = _num(settings.station_elevation_ft)
    if v is None or v == 0.0:
        return None
    return v if ELEVATION_MIN_FT <= v <= ELEVATION_MAX_FT else None


def _obs_local_day(obs: dict[str, Any]) -> date | None:
    """The local date an observation was taken on, or None when the row
    carries no usable timestamp.

    The STATION's clock — the same one that assigns `daily_rollups.day` —
    because a host on UTC is a day ahead of Phoenix for part of every
    evening, and this card's whole claim is about which day it is.

    Every producer whose subject is RIGHT NOW (air_flight, fire_weather,
    barometer_says) declines unless this equals `ctx.today`: a card built
    from last Tuesday's barometer is a lie about weather nobody is
    measuring any more. Freshness is measured in LOCAL DAYS against the
    pinned anchor, not in minutes against the wall clock, and that is
    deliberate: the whole engine is reproducible under
    `climate.local_today`, and a minutes-since-now check would make these
    the producers whose payload moved between two calls in the same
    second. "Taken on the anchor day" is coarse in the way a date is
    coarse and exact in the way a test needs.
    """
    from datetime import datetime, timezone as _tzu
    from . import insights as _ins
    ms = _num(obs.get("dateutc"))
    if ms is None:
        return None
    try:
        return (datetime.fromtimestamp(ms / 1000, tz=_tzu.utc)
                .astimezone(_ins._tz()).date())
    except (OverflowError, OSError, ValueError):
        # A garbled epoch (seconds where milliseconds were expected, a
        # negative sentinel) is a timestamp we do not have, not one we do.
        return None


@producer(FAMILY_SCIENCE, "air_flight")
async def air_flight(ctx: StoryContext) -> list[Story]:
    """Density altitude: the altitude this air is behaving like.

    Warm air is thin, moist air is thinner still, and low pressure is
    thinner again — roll all three together and you get the altitude at
    which a standard atmosphere would be this thin. On a Chandler afternoon
    a station 1,200 ft up breathes like an airport four thousand feet
    higher, and that single sentence is the whole card.

    Declines when the station has never reported, when its newest
    observation was not taken on the anchor day (a "right now" card built
    from last Tuesday is a lie), when it reports no ABSOLUTE pressure (the
    chain needs station pressure and a sea-level reading would produce a
    plausible wrong answer), or when temperature and moisture are missing.

    Declines the CONTRAST — not the story — when the station's elevation is
    unknown. See the header above: an unknown altitude read as zero is the
    "absent is not zero" bug with a mile of leverage on it.
    """
    from . import derived
    obs = await ctx.current()
    if not obs:
        return []
    seen_on = _obs_local_day(obs)
    if seen_on is None or seen_on != ctx.today:
        return []

    temp_f = _num(obs.get("tempf"))
    humidity = _num(obs.get("humidity"))
    # The stored dew point when the source computed one, the Magnus form
    # otherwise — the same order `/api/devices/{mac}/derived` uses, so the
    # Science screen and this card can never quote two different dew points
    # for one observation.
    dew_f = _num(obs.get("dewPoint"))
    if dew_f is None:
        dew_f = derived.dew_point_f(temp_f, humidity)
    # STATION pressure. `baromrelin` is sea-level-corrected and is NOT a
    # substitute: the chain would return a confident number that is wrong by
    # roughly the station's own elevation, which is precisely the error this
    # card is about.
    press_inhg = _num(obs.get("baromabsin"))
    if temp_f is None or dew_f is None or press_inhg is None:
        return []

    da_ft = derived.density_altitude_ft(temp_f, dew_f, press_inhg)
    if da_ft is None:
        return []
    # What the humidity alone is worth: the same chain, run again with the
    # moisture taken out. One formula, two inputs — not a second definition.
    dry_ft = derived.density_altitude_ft(temp_f, BONE_DRY_DEW_F, press_inhg)
    moisture_ft = (None if dry_ft is None else max(0.0, da_ft - dry_ft))

    elevation_ft = _station_elevation_ft()
    penalty_ft = None if elevation_ft is None else da_ft - elevation_ft

    # ── scoring ─────────────────────────────────────────────────────────
    parts: dict[str, float] = {}
    weights: list[tuple[float, float]] = []
    thinness = min(1.0, max(0.0, da_ft / THINNESS_FULL_FT))
    parts["thinness"] = round(thinness, 4)
    weights.append((0.85, thinness))
    if penalty_ft is not None:
        # Clamped at zero from below on purpose: air DENSER than the ground
        # it sits on is a real and rather charming winter fact, but it is not
        # a bigger story for being further below, and a negative term would
        # drag the mean around.
        lift = min(1.0, max(0.0, penalty_ft / PENALTY_FULL_FT))
        parts["penalty"] = round(lift, 4)
        weights.append((1.00, lift))
    if moisture_ft is not None:
        damp = min(1.0, max(0.0, moisture_ft / MOISTURE_FULL_FT))
        parts["moisture"] = round(damp, 4)
        weights.append((0.45, damp))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    # ── copy ────────────────────────────────────────────────────────────
    u = ctx.units
    # Every reading stayed API-native through the physics above and converts
    # only here. The pressure the chain ATE was inHg; what the sentence
    # SHOWS is the reader's scale.
    reading_line = (f"{u.temp_deg(temp_f)} air, a {u.temp_deg(dew_f)} dew "
                    f"point and {u.pressure_amount(press_inhg)} at the "
                    f"sensor")
    # A density altitude BELOW sea level is not an error and not a rounding
    # artefact — it is what a cold, dry, high-pressure morning genuinely
    # measures, and "acting like −647 ft" is not a sentence. Both halves of
    # the copy flip with it, because the air is DENSE on those days and
    # describing it as "thin" would be the card contradicting its own number.
    if da_ft >= 0:
        hero_line = f"THE AIR IS ACTING LIKE {_feet(da_ft)} FT"
        thinness_line = f"as thin as a standard day {_feet(da_ft)} ft up"
    else:
        hero_line = (f"THE AIR IS ACTING LIKE {_feet(abs(da_ft))} FT "
                     f"BELOW SEA LEVEL")
        thinness_line = (f"as dense as a standard day {_feet(abs(da_ft))} ft "
                         f"BELOW sea level")
    if penalty_ft is not None and elevation_ft is not None:
        if penalty_ft >= 0:
            contrast = (f", {_feet(penalty_ft)} ft above the "
                        f"{_feet(elevation_ft)} ft this station actually "
                        f"sits at")
        else:
            contrast = (f", {_feet(abs(penalty_ft))} ft BELOW the "
                        f"{_feet(elevation_ft)} ft this station actually "
                        f"sits at, because cold dense air is heavier than "
                        f"standard")
    else:
        # No elevation, no contrast, and no sentence pretending otherwise.
        contrast = ""
    context = (
        f"Air thins as it warms and thins again as it takes on moisture. "
        f"With {reading_line}, this air is {thinness_line}{contrast}. "
        f"Computed from this station's own barometer, never a sea-level "
        f"reading.")

    comparison: Comparison | None = None
    if penalty_ft is not None and elevation_ft is not None:
        comparison = Comparison(
            kind="station_elevation",
            label="vs where this station actually sits",
            value=round(da_ft),
            baseline=round(elevation_ft),
            baseline_label=f"{_feet(elevation_ft)} ft, the station's elevation",
            direction=("level" if abs(penalty_ft) < 50
                       else "above" if penalty_ft > 0 else "below"),
            delta=round(penalty_ft),
            # A station at exactly 0 ft never reaches here (that reads as
            # unknown), so there is no zero baseline to guard — but a
            # station BELOW sea level has a negative one, and a percentage
            # against a negative baseline is nonsense rather than absent.
            delta_pct=(round(100 * penalty_ft / elevation_ft, 1)
                       if elevation_ft > 0 else None),
            # No leaderboard here: this is one moment measured against one
            # fixed number, not a rank among periods.
            rank_line=None)

    supporting: list[Stat] = []
    if elevation_ft is not None:
        supporting.append(Stat("station_elevation", "this station sits at",
                               round(elevation_ft), UNIT_FT))
    if penalty_ft is not None:
        supporting.append(Stat(
            "altitude_penalty",
            ("feet of altitude the air is giving away" if penalty_ft >= 0
             else "feet of altitude the air is handing back"),
            round(abs(penalty_ft)), UNIT_FT))
    supporting.append(Stat("temperature", "air temperature",
                           round(u.temp(temp_f), 1), u.temp_token, 1))
    supporting.append(Stat("dew_point", "dew point",
                           round(u.temp(dew_f), 1), u.temp_token, 1))
    if humidity is not None:
        supporting.append(Stat("humidity", "relative humidity",
                               round(humidity), UNIT_PCT))
    supporting.append(Stat("station_pressure", "pressure at the sensor",
                           round(u.pressure_value(press_inhg),
                                 u.pressure_precision),
                           u.pressure_token, u.pressure_precision))
    if moisture_ft is not None:
        supporting.append(Stat("moisture_share",
                               "feet of it that the humidity alone is worth",
                               round(moisture_ft), UNIT_FT))
    wind = _num(obs.get("windspeedmph"))
    gust = _num(obs.get("windgustmph"))
    if wind is not None:
        # Wind is NOT an input to the density chain and the label says so —
        # it is here because it is the other thing the air is doing at that
        # moment, and a card that quietly listed it among the inputs would
        # be teaching the physics wrong.
        supporting.append(Stat("wind", "wind at the same moment (not part "
                               "of the density calculation)",
                               round(u.wind_value(wind)), u.wind_token))
    if gust is not None:
        supporting.append(Stat("gust", "gust at the same moment",
                               round(u.wind_value(gust)), u.wind_token))

    # Two bars and the gap between them — the whole picture. `feet` is the
    # stored, unconverted number in both entries so the template never has
    # to reconcile two scales inside one chart.
    series: list[dict[str, Any]] = []
    if elevation_ft is not None:
        series.append({"key": "station", "label": "this station sits at",
                       "feet": round(elevation_ft)})
    series.append({"key": "density", "label": "the air is acting like",
                   "feet": round(da_ft),
                   "above_station": (None if penalty_ft is None
                                     else round(penalty_ft)),
                   # The card draws `above_station` as a signed bracket
                   # across the gap, and a sign is not a sentence: on a
                   # shared image "+4,113 ft" has to explain itself
                   # (field report from the card templates, 2026-08-30).
                   # The describing phrase lived only in a supporting stat,
                   # which the bracket cannot reach. Written here, beside
                   # the number it describes, and absent whenever the
                   # station's elevation is — there is no bracket to label
                   # without a second bar to measure from.
                   "above_station_label": (
                       None if penalty_ft is None
                       else f"{_feet(penalty_ft)} ft above the station"
                       if penalty_ft >= 0
                       else f"{_feet(abs(penalty_ft))} ft below the station")})

    seen_day = ctx.today.isoformat()
    return [Story(
        id=f"science.air_flight.{seen_day}",
        family=FAMILY_SCIENCE,
        story_type="air_flight",
        title="Air & Flight",
        emoji="✈️",
        hero=Stat("density_altitude", "the altitude this air is acting like",
                  round(da_ft), UNIT_FT),
        hero_line=hero_line,
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="altitude_ladder", series=series, unit=UNIT_FT,
                axis_label="feet above sea level",
                highlight="density", highlight_key="density"),
        period=Period(kind="moment", label="right now",
                      start=seen_day, end=seen_day, partial=False),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        # The card renders this ON the image. It is not a footnote and it is
        # not optional — see the header.
        disclaimer=AIR_DISCLAIMER,
        score_parts=parts,
    )]


# ───────────────────────── Water Year ─────────────────────────
#
# Rain measured October 1 → September 30 instead of January → December, and
# the reason is the whole card: a wet season that starts in November and
# ends in March is ONE season, and the calendar cuts it in half at New Year.
# Every hydrologist counts it this way; almost no consumer weather app ever
# explains why, which makes the explanation the most valuable line on the
# graphic.
#
# The start month is `settings.water_year_start_month` (10 by default, the
# western-US convention; the setting exists so a southern-hemisphere or a
# don't-care operator can move it), and the boundary itself is
# `climate.water_year_start` — ONE definition, shared with /api/climate, so
# the card and the Climate screen can never disagree about which year the
# rain in front of you belongs to.
#
# NAMING: a water year is named for the calendar year it ENDS in, the USGS
# convention — so November 2025 is inside water year 2026. That falls out of
# the bounds arithmetic below rather than being asserted, which also gets the
# degenerate case right: an operator who sets the start month to 1 has a
# water year that starts AND ends in the same calendar year, and the label
# follows without a special case (the producer then declines outright — a
# water year identical to the calendar year has no concept left to teach).
#
# COVERAGE is borrowed whole from insights, not re-derived:
# `comparable_to_date` decides which earlier water years may stand beside
# this one, and the same COMPARISON_MIN_DAYS / COMPARISON_COVERAGE floors
# decide whether the running one may be presented as a TOTAL at all. A
# station that joined in February has not had a dry water year; it has had
# four months of not knowing, and 3.10 in printed as the season's total
# would be the "absent is not zero" bug wearing a rain gauge.

# Percentage-first hero, per the review's rain-race note: "38% BELOW NORMAL"
# is a headline and "4.21 in" is a measurement. Under this the amount leads,
# because a 4% departure dressed as a percentage reads bigger than it is.
WATER_YEAR_PCT_HERO = 15.0

# Where the departure term saturates. Three quarters away from normal is as
# far from normal as a 0..1 score needs to care about; past it the story is
# already "the wettest/driest on record" and `extremity` is carrying it.
DEPARTURE_FULL = 0.75

# One earlier year is a year. Two are the beginning of a normal, and the
# copy says "normal" only from there — quoting "the 2025 average" of a
# single number invites the reader to imagine a climatology that isn't there.
MIN_NORMAL_YEARS = 2


def _shift_year(d: date, delta: int) -> date:
    """`d` moved by whole years, February 29 landing on the 28th.

    The anchor arithmetic's one trap: a water year measured "through today"
    must be measured through the SAME month-day in every earlier year, and
    every fourth today has no counterpart. Clamping down by a day is the
    same choice `insights.window_days_to_anchor` makes when it walks a Feb 29
    anchor backwards into a non-leap year.
    """
    try:
        return d.replace(year=d.year + delta)
    except ValueError:
        return d.replace(year=d.year + delta, day=28)


def _water_year_bounds(label: int, start_month: int) -> tuple[date, date]:
    """(first day, last day) of the water year NAMED `label`.

    A water year is named for the year it ends in, so with the default
    October start, water year 2026 runs 2025-10-01 → 2026-09-30. With a
    January start it runs 2026-01-01 → 2026-12-31 and the name is the
    calendar year's, which is the arithmetic agreeing with itself rather
    than a special case.
    """
    start = date(label - 1 if start_month > 1 else label, start_month, 1)
    return start, _shift_year(start, 1) - timedelta(days=1)


def _water_year_label(d: date, start_month: int) -> int:
    """Which water year `d` falls in — the year that water year ENDS in.

    Built on `climate.water_year_start` so there is exactly one place in
    this server that decides where a water year begins.
    """
    from .climate import water_year_start
    start = water_year_start(d, start_month)
    return _shift_year(start, 1).year if start_month > 1 else start.year


@dataclass(frozen=True)
class _WaterYearWindow:
    """One water year measured through an anchor, and how much of that
    window the station was actually there for."""
    label: int
    start: date
    anchor: date                    # the last day this measurement covers
    complete: bool                  # did the anchor reach the year's own end
    rain: float
    rain_days: int
    measured: int                   # days inside the window with a reading
    window: int                     # calendar days in the window
    wettest: tuple[str, float] | None

    @property
    def coverage(self) -> float:
        return self.measured / self.window if self.window > 0 else 0.0


def _measure_water_year(by_day: dict[str, float | None], label: int,
                        start_month: int, anchor: date) -> _WaterYearWindow:
    """Fold the rollup days that fall inside one water year's window.

    `by_day` maps day → rain in inches, or None for a day the station
    recorded WITHOUT measuring rain. A None day counts against coverage and
    contributes nothing to the total, which is the difference between a dry
    day and an unknown one.
    """
    from . import insights as _ins
    start, end = _water_year_bounds(label, start_month)
    cut = min(anchor, end)
    total = 0.0
    rain_days = 0
    measured = 0
    wettest: tuple[str, float] | None = None
    d = start
    while d <= cut:
        key = d.isoformat()
        if key in by_day:
            rain = by_day[key]
            if rain is not None:
                measured += 1
                total += rain
                if rain >= _ins.RAIN_DAY_MIN_IN:
                    rain_days += 1
                    if wettest is None or rain > wettest[1]:
                        wettest = (key, rain)
        d += timedelta(days=1)
    return _WaterYearWindow(
        label=label, start=start, anchor=cut, complete=cut >= end,
        rain=round(total, 3), rain_days=rain_days, measured=measured,
        window=(cut - start).days + 1, wettest=wettest)


def _water_year_span(start: date, end: date) -> str:
    """"October 2025 – September 2026" — the span spelled out, because
    "water year 2026" means nothing to a reader who has never met one."""
    return (f"{_MONTHS[start.month - 1]} {start.year} – "
            f"{_MONTHS[end.month - 1]} {end.year}")


# A degree day is a SUM OF DIFFERENCES from a base temperature, so it
# converts by scale exactly like any other delta, and the base converts like
# the reading it is. Both halves are stated in the reader's scale together or
# the card teaches a definition it then contradicts: "one degree for every
# degree above 65°F" printed over a Celsius number is two different units in
# one sentence.
UNIT_DEGREE_DAYS = "degree days"
# Below this many measured days a season's demand total is an artefact of
# how long the station was switched on.
MIN_DEGREE_DAY_DAYS = 60


@producer(FAMILY_SCIENCE, "degree_days")
async def how_hard_did_the_ac_work(ctx: StoryContext) -> list[Story]:
    """Degree days, translated out of jargon.

    Almost nobody knows what a degree day is, so the card teaches it in the
    same breath it quotes one: a degree day counts one degree of demand for
    every degree the day's AVERAGE sat away from the base, so a 95° day with
    a 75° night averages 85° and books 20 degree days of cooling. That
    sentence is the card's whole reason to exist; the number is the hook.

    Leads with whichever side actually worked. In Chandler that is cooling
    by a factor of fifty, and "the heating season, all eleven degree days of
    it" is the joke the data tells; in Duluth the same producer leads with
    heating and the joke is the other one. A card that always led with
    cooling would be an Arizona card pretending to be a feature.

    ONE definition, shared: `climate.degree_days` is what the Science
    surface and the insights rollups both compute, and this producer calls
    it rather than adding a fourth copy of `max(0, mean - base)`.

    Rollups only — daily highs and lows, one memoized pass.

    Declines when fewer than MIN_DEGREE_DAY_DAYS days in the newest year
    measured both ends, and when the year booked no demand of either kind
    (a station that sat at the base every single day has no story, and a
    station with no thermometer has no reading to be at the base).
    """
    from . import insights as _ins
    from .climate import HDD_CDD_BASE_F, degree_days
    days = await ctx.daily()
    if not days:
        return []

    # (date, hdd, cdd) per measured day, using the SHARED definition.
    scored: list[tuple[date, float, float]] = []
    for row in days:
        hdd_cdd = degree_days(_num(row.get("tempf_min")),
                              _num(row.get("tempf_max")))
        if hdd_cdd is None:            # one end missing: absent, not zero
            continue
        try:
            when = date.fromisoformat(str(row["day"]))
        except (TypeError, ValueError):
            continue
        scored.append((when, hdd_cdd[0], hdd_cdd[1]))
    if not scored:
        return []

    year = scored[-1][0].year
    partial = year == ctx.today.year
    # A running year is measured through today's month-day in every year it
    # is compared against — the frame the heat ledger established.
    anchor_md = ctx.today.strftime("%m-%d")

    def in_window(d: date) -> bool:
        return not partial or d.strftime("%m-%d") <= anchor_md

    this_year = [s for s in scored if s[0].year == year and in_window(s[0])]
    if len(this_year) < MIN_DEGREE_DAY_DAYS:
        return []
    cool = sum(s[2] for s in this_year)
    heat = sum(s[1] for s in this_year)
    if cool <= 0 and heat <= 0:
        return []

    leads_cooling = cool >= heat
    lead_total, other_total = ((cool, heat) if leads_cooling else (heat, cool))
    lead_word = "cooling" if leads_cooling else "heating"
    other_word = "heating" if leads_cooling else "cooling"

    # Per month, so the card can show WHERE the demand came from — the
    # spec's "August created a third of the year's cooling".
    months: dict[int, list[float]] = {}
    for when, hdd, cdd in this_year:
        acc = months.setdefault(when.month, [0.0, 0.0])
        acc[0] += hdd
        acc[1] += cdd
    busiest = max(months.items(), key=lambda kv: kv[1][1 if leads_cooling else 0])
    busiest_month, busiest_acc = busiest
    busiest_total = busiest_acc[1 if leads_cooling else 0]
    busiest_share = busiest_total / lead_total if lead_total else 0.0

    # Prior years over the SAME window, and only those that covered it.
    prior: list[tuple[int, float]] = []
    for other_year in sorted({s[0].year for s in scored} - {year}):
        rows = [s for s in scored if s[0].year == other_year and in_window(s[0])]
        if _ins.comparable_to_date(len(rows), len(this_year)):
            prior.append((other_year,
                          sum(r[2] if leads_cooling else r[1] for r in rows)))

    u = ctx.units
    period = _period_label(year, partial)
    comparison: Comparison | None = None
    standout: float | None = None
    if prior:
        baseline = sum(v for _, v in prior) / len(prior)
        rank = 1 + sum(1 for _, v in prior if v > lead_total)
        of = len(prior) + 1
        delta = lead_total - baseline
        span = (f"{min(y for y, _ in prior)}–{max(y for y, _ in prior)} average"
                if len(prior) > 1 else str(prior[0][0]))
        window = (f" through {_short_date(ctx.today)}" if partial else "")
        comparison = Comparison(
            kind="prior_years_to_date" if partial else "prior_years_full",
            label=f"vs the same window's {lead_word} demand",
            value=round(u.temp_delta(lead_total)),
            baseline=round(u.temp_delta(baseline)),
            baseline_label=f"{span}{window}",
            direction=("level" if abs(delta) < 1.0
                       else "above" if delta > 0 else "below"),
            delta=round(u.temp_delta(delta)),
            delta_pct=(round(100 * delta / baseline, 1) if baseline else None),
            rank=rank, of=of,
            rank_line=_rank_line(rank, of, "comparable years",
                                 "hardest-working"))
        smaller = sum(1 for _, v in prior if v < lead_total)
        standout = smaller / (of - 1) if of > 1 else None

    parts = {
        # How lopsided the year was. A climate where one side does all the
        # work is the interesting case in both directions — Chandler and
        # Duluth are each remarkable, a mild maritime year is not.
        "lopsidedness": round(lead_total / (lead_total + other_total), 4)
        if (lead_total + other_total) else 0.0,
        # How much of it one month owned.
        "concentration": round(min(1.0, busiest_share), 4),
    }
    weights = [(0.35, parts["lopsidedness"]), (0.25, parts["concentration"])]
    if standout is not None:
        parts["standout"] = round(standout, 4)
        weights.append((0.40, parts["standout"]))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    ratio_line = ""
    if other_total > 0 and lead_total / other_total >= 2:
        ratio_line = (f" {lead_word.capitalize()} outworked {other_word} "
                      f"{lead_total / other_total:.0f} to 1.")
    elif other_total <= 0:
        ratio_line = f" There was no {other_word} demand at all."

    supporting = [
        Stat(f"{lead_word}_degree_days", f"{lead_word} degree days in {period}",
             round(u.temp_delta(lead_total)), UNIT_DEGREE_DAYS),
        Stat(f"{other_word}_degree_days",
             f"{other_word} degree days in {period}",
             round(u.temp_delta(other_total)), UNIT_DEGREE_DAYS),
        Stat("busiest_month",
             f"from {_MONTHS[busiest_month - 1]} alone, "
             f"{round(busiest_share * 100)}% of the {lead_word}",
             round(u.temp_delta(busiest_total)), UNIT_DEGREE_DAYS),
        Stat("days_measured", f"days measuring both ends in {period}",
             len(this_year), UNIT_DAYS),
    ]

    context = (
        f"A degree day counts one degree of demand for every degree a day's "
        f"AVERAGE temperature sat away from {u.temp_deg(HDD_CDD_BASE_F)}. An "
        f"afternoon of {u.temp_deg(95.0)} over a night of {u.temp_deg(75.0)} "
        f"averages {u.temp_deg(85.0)} and books "
        f"{u.temp_delta_deg(20.0)}\u00b7days of cooling. "
        f"{period} has booked {round(u.temp_delta(lead_total)):,} of them on "
        f"the {lead_word} side.{ratio_line}")

    return [Story(
        id=f"science.degree_days.{year}",
        family=FAMILY_SCIENCE,
        story_type="degree_days",
        title="How Hard Did the AC Work?",
        emoji="❄️",
        hero=Stat(f"{lead_word}_degree_days",
                  f"{lead_word} degree days in {period}",
                  round(u.temp_delta(lead_total)), UNIT_DEGREE_DAYS),
        hero_line=f"{round(u.temp_delta(lead_total)):,} DEGREE DAYS OF "
                  f"{lead_word.upper()}",
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="degree_day_months",
                # `lead` names which of the two series the story is ABOUT —
                # the side the hero counted. The renderer draws that side
                # bright and the other grey, and the footnote says so in
                # words: without the field the card would have to parse the
                # axis label's prose to learn which bar carries the
                # headline, and prose is not a contract.
                series=[{"key": f"{year}-{m:02d}",
                         "month": m, "label": _MONTHS[m - 1][:3],
                         "cooling": round(u.temp_delta(acc[1])),
                         "heating": round(u.temp_delta(acc[0])),
                         "lead": lead_word,
                         "note": (f"{round(busiest_share * 100)}% of the year's "
                                  f"{lead_word}" if m == busiest_month else None),
                         "hero": m == busiest_month}
                        for m, acc in sorted(months.items())],
                unit=UNIT_DEGREE_DAYS,
                axis_label=f"monthly {lead_word} and {other_word} demand · {period}",
                footnote=(f"Bright bars are {lead_word}, grey bars are "
                          f"{other_word}. Both are degree days against a "
                          f"{u.temp_deg(HDD_CDD_BASE_F)} base, the same "
                          f"definition a utility uses to explain a bill."),
                highlight=busiest_month,
                highlight_key=f"{year}-{busiest_month:02d}"),
        period=Period(kind="year", label=period,
                      start=f"{year}-01-01",
                      end=(ctx.today.isoformat() if partial
                           else f"{year}-12-31"),
                      partial=partial),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# The wording is the Science surface's, verbatim, and it is not decoration:
# Fosberg and the Chandler Burning Index are WEATHER indices. They know the
# air and know nothing about fuel, terrain, drought or what any agency has
# declared. A card that let a reader mistake one for a fire-danger rating
# would be worse than a card that never shipped, so this rides ON the image
# via Story.disclaimer, exactly as the aviation card's does, and a test
# holds the string.
FIRE_DISCLAIMER = ("Weather-driven index only · "
                   "not an official fire-danger rating.")

# Chandler Burning Index bands, from the source formula (derived.py):
# <50 low, 50-75 moderate, 75-90 high, 90-97.5 very high, >97.5 extreme.
CBI_BANDS = ((97.5, "extreme"), (90.0, "very high"), (75.0, "high"),
             (50.0, "moderate"), (0.0, "low"))
# Fosberg saturates near 100 (about 30 mph over bone-dry fuels), so it is
# its own 0..1 scale once clamped. No station-relative floor here for the
# same reason the density-altitude ladders are absolute: dry air at 30 mph
# is dry air at 30 mph in Chandler and in Irwin PA alike.
FFWI_FULL = 100.0
# Below this the index is describing ordinary air and there is no story. A
# card that fires every mild afternoon teaches a reader to ignore it, which
# is the last thing a fire-weather card should do.
CBI_STORY_FLOOR = 50.0


def _cbi_band(cbi: float) -> str:
    for floor, name in CBI_BANDS:
        if cbi >= floor:
            return name
    return "low"


@producer(FAMILY_SCIENCE, "fire_weather")
async def fire_weather(ctx: StoryContext) -> list[Story]:
    """What the air alone is doing to fire risk, and what it is NOT saying.

    Two indices the Science surface already computes, reused rather than
    reimplemented: the Chandler Burning Index (temperature and humidity)
    and the Fosberg Fire Weather Index (those plus wind). Both describe the
    AIR. Neither knows what is on the ground, and the card says so on the
    image.

    Declines when the station has never reported, when its newest reading
    was not taken on the anchor day (a "right now" card built from last
    Tuesday is a lie about weather nobody is measuring), when temperature
    or humidity is missing — absent is not zero, and a humidity read as 0
    would manufacture the most alarming number this card can print — and
    when the air is simply ordinary, below CBI_STORY_FLOOR.

    Wind is OPTIONAL and its absence is honest: without an anemometer there
    is no Fosberg, so the card carries the Chandler index alone and says
    which one is missing rather than substituting a zero wind speed, which
    would read as "calm" and understate the very thing Fosberg measures.
    """
    from . import derived
    obs = await ctx.current()
    if not obs:
        return []
    seen_on = _obs_local_day(obs)
    if seen_on is None or seen_on != ctx.today:
        return []

    temp_f = _num(obs.get("tempf"))
    humidity = _num(obs.get("humidity"))
    cbi = derived.chandler_burning_index(temp_f, humidity)
    if cbi is None or temp_f is None or humidity is None:
        return []
    if cbi < CBI_STORY_FLOOR:
        return []

    # Absent wind is absent, never calm. `windspeedmph` missing means no
    # anemometer reported, and feeding 0 into Fosberg would return a
    # reassuring number the station never measured.
    wind_mph = _num(obs.get("windspeedmph"))
    gust_mph = _num(obs.get("windgustmph"))
    ffwi = derived.fosberg_fwi(temp_f, humidity, wind_mph)

    band = _cbi_band(cbi)
    u = ctx.units

    parts = {
        # The Chandler index on its own 0..1 scale, clamped at the top of
        # its published band table rather than at an invented maximum.
        "burning_index": round(min(1.0, cbi / 97.5), 4),
    }
    weights = [(0.55, parts["burning_index"])]
    if ffwi is not None:
        parts["fosberg"] = round(min(1.0, ffwi / FFWI_FULL), 4)
        weights.append((0.45, parts["fosberg"]))
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    supporting = [
        Stat("temperature", "air temperature", round(u.temp(temp_f), 1),
             u.temp_token, 1),
        Stat("humidity", "relative humidity", round(humidity), UNIT_PCT),
    ]
    if ffwi is not None and wind_mph is not None:
        supporting.append(Stat("wind", "wind driving the Fosberg index",
                               round(u.wind_value(wind_mph)), u.wind_token))
    if gust_mph is not None:
        supporting.append(Stat("gust", "strongest gust reported",
                               round(u.wind_value(gust_mph)), u.wind_token))
    supporting.append(Stat("chandler_index",
                           f"Chandler Burning Index, {band}",
                           round(cbi, 1), UNIT_INDEX, 1))
    if ffwi is not None:
        supporting.append(Stat(
            "fosberg_index",
            "Fosberg index, the same air with the wind in it",
            round(ffwi, 1), UNIT_INDEX, 1))

    # The card's job is to explain what it is measuring, because the number
    # means nothing on its own and the WRONG meaning is dangerous.
    lesson = (f"The Chandler Burning Index reads {round(cbi)}, {band}, "
              f"from air at {u.temp_deg(temp_f)} and {round(humidity)}% "
              f"humidity. ")
    if ffwi is not None:
        lesson += (f"Fosberg adds the wind and reads {round(ffwi)}. ")
    else:
        lesson += ("This station reports no wind, so the Fosberg index, "
                   "the one that accounts for it, is not computed here. ")
    lesson += ("Both describe the AIR. Neither knows what is on the ground, "
               "how dry the fuels are, or what any agency has declared.")

    return [Story(
        id=f"science.fire_weather.{ctx.today.isoformat()}",
        family=FAMILY_SCIENCE,
        story_type="fire_weather",
        title="Fire Weather",
        emoji="🔥",
        hero=Stat("chandler_index", f"Chandler Burning Index · {band}",
                  round(cbi), UNIT_INDEX),
        hero_line=f"{band.upper()} FIRE WEATHER",
        context=lesson,
        comparison=None,
        supporting=supporting,
        disclaimer=FIRE_DISCLAIMER,
        # The band ladder, so the card can show WHERE this afternoon sits
        # rather than printing a bare number against no scale. A number
        # with no ladder is exactly how an index gets mistaken for a rating.
        viz=Viz(kind="index_bands",
                series=[{"key": name.replace(" ", "_"), "label": name,
                         "floor": floor,
                         "hero": name == band}
                        for floor, name in reversed(CBI_BANDS)],
                unit=UNIT_INDEX,
                axis_label="Chandler Burning Index bands",
                footnote=("The bands are the index's own published ones. "
                          "They describe weather, not the ground it blows "
                          "over."),
                domain_max=97.5,
                highlight=band,
                highlight_key=band.replace(" ", "_")),
        period=Period(kind="moment", label="right now",
                      start=ctx.today.isoformat(),
                      end=ctx.today.isoformat(), partial=False),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# The three-hour trend window lives with the ledger now
# (`zambretti_ledger.TREND_MS`): the card and the daily snapshot read the
# same barometer through the same helper, so the constant has one home.
# A three-hour pressure change this big is a genuinely rapid one — roughly
# twice the WMO "rapid" threshold — and tops this card's scale. Picked so
# the ordinary daily drift of a few hundredths lands low rather than
# flattering itself into the middle of the feed.
BAROMETER_RAPID_INHG = 0.12

# The card says "change in three hours" and "falling for three hours", so
# the span it measured has to BE three hours. `zambretti_ledger.compute_call`
# finds its anchor at or before obs−3h with a three-hour freshness floor
# (the right rule for a ledger that must file a call most mornings), which
# after an outage puts the anchor anywhere in [obs−6h, obs−3h]. This is
# how much older than obs−3h the anchor may be before the label is a lie
# and the card declines: one missed poll of a five-minute station, or one
# of a fifteen-minute one, is slack; a second missing hour is not.
BAROMETER_ANCHOR_SLACK_MS = 30 * 60_000


async def _todays_forecast(today: date) -> dict[str, Any] | None:
    """The freshest stored forecast for today, or None.

    Read from `forecast_snapshots` — the local archive the monitor already
    writes every ~6h — never a live call. A story producer that reached out
    to a third-party API would make the whole feed as slow and as reliable
    as somebody else's server, and this card is worth exactly zero outages.

    One indexed lookup on (provider, valid_date), newest issue wins — for
    the ONE provider the card names. The table holds other providers too
    (the Zambretti ledger files its own daily call there, with no
    temperatures), and "the newest row for today" would hand the modern
    column a verdict the slide rule wrote.
    """
    from . import db as dbmod
    async with dbmod.connect() as conn:
        row = await (await conn.execute(
            "SELECT tmax_f, tmin_f, pop FROM forecast_snapshots "
            "WHERE provider = ? AND valid_date = ? "
            "ORDER BY issued_ms DESC LIMIT 1",
            (FORECAST_PROVIDER, today.isoformat()))).fetchone()
    return dict(row) if row else None


@producer(FAMILY_SCIENCE, "barometer_says")
async def barometer_says(ctx: StoryContext) -> list[Story]:
    """A forecast from 1920, beside the one from this morning.

    The Negretti & Zambra slide rule turns one pressure reading and its
    three-hour trend into a sentence, and it has been doing it since before
    anybody had a satellite. `derived.zambretti` is the implementation the
    Science surface already renders; this card puts its verdict next to
    what a modern numerical model said about the same day, and lets the
    reader enjoy the comparison.

    It does NOT score them. "Who won" is the forecast-verification backlog
    item wearing a costume: honest scoring needs forecasts captured at
    issue time, matched to outcomes, over a season — infrastructure this
    card does not have and must not pretend to. Two forecasts side by side
    is the whole product.

    Declines when the station has never reported, when its newest reading
    was not taken on the anchor day, when it carries no sea-level pressure,
    and — the one that matters — when there is no reading three hours back
    (within BAROMETER_ANCHOR_SLACK_MS of it) to take a trend from.
    Zambretti is a function OF the trend; defaulting an unknown trend to
    "steady" would invent the input and print a confident sentence built
    on it.

    The modern forecast is OPTIONAL. No stored snapshot means the card
    carries the barometer alone and says the comparison is missing, rather
    than implying the slide rule went unchallenged.
    """
    from . import zambretti_ledger
    obs = await ctx.current()
    if not obs:
        return []
    seen_on = _obs_local_day(obs)
    if seen_on is None or seen_on != ctx.today:
        return []

    # SEA-LEVEL pressure, the three-hour trend and the slide rule, all read
    # by `zambretti_ledger.compute_call` — the SAME helper the daily ledger
    # snapshots from at 09:00, so the card and the season's record of calls
    # cannot disagree about what the barometer said.
    call = await zambretti_ledger.compute_call(ctx.mac, obs)
    if call is None:
        return []
    # Same lookup, tighter floor: the anchor `compute_call` used is the
    # newest reading at or before obs−3h, so asking again within
    # BAROMETER_ANCHOR_SLACK_MS of that edge either finds the same row or
    # finds nothing — and nothing means the span the label names is not the
    # span that was measured.
    from . import db as dbmod
    near = await dbmod.value_at_or_before(
        ctx.mac, "baromrelin", call.obs_ms - zambretti_ledger.TREND_MS,
        max_age_ms=BAROMETER_ANCHOR_SLACK_MS)
    if near is None:
        return []
    slp, delta = call.slp_inhg, call.delta_inhg
    code, word, says = call.code, call.trend, call.says

    u = ctx.units
    modern = await _todays_forecast(ctx.today)

    supporting = [
        Stat("pressure", "sea-level pressure now",
             round(u.pressure_value(slp), u.pressure_precision),
             u.pressure_token, u.pressure_precision),
        # A three-hour CHANGE. Pressure converts by scale here for the same
        # reason a temperature swing does — it is a difference, and the
        # pressure units are all linear with no offset, so `pressure_value`
        # is right for both. The SIGN is the whole meaning and survives.
        Stat("trend_3h", f"change in three hours · {word}",
             round(u.pressure_value(delta), u.pressure_precision),
             u.pressure_token, u.pressure_precision),
        Stat("tendency_code", "WMO pressure tendency code", code, UNIT_INDEX),
    ]
    if modern:
        hi, lo = _num(modern.get("tmax_f")), _num(modern.get("tmin_f"))
        pop = _num(modern.get("pop"))
        if hi is not None:
            supporting.append(Stat("forecast_high", "forecast high today",
                                   round(u.temp(hi), 1), u.temp_token, 1))
        if lo is not None:
            supporting.append(Stat("forecast_low", "forecast low today",
                                   round(u.temp(lo), 1), u.temp_token, 1))
        if pop is not None:
            supporting.append(Stat("forecast_pop",
                                   "forecast chance of rain today",
                                   round(pop), UNIT_PCT))

    context = (f"Your barometer reads {u.pressure_amount(slp)} and has been "
               f"{word} for three hours. Feed those two facts into the "
               f"Negretti & Zambra slide rule, a brass instrument from the "
               f"1920s with no satellites and no model, and it answers: "
               f"\u201c{says}\u201d")
    if modern:
        bits = []
        m_hi, m_lo = _num(modern.get("tmax_f")), _num(modern.get("tmin_f"))
        m_pop = _num(modern.get("pop"))
        if m_hi is not None and m_lo is not None:
            bits.append(f"{u.temp_deg(m_hi)} over {u.temp_deg(m_lo)}")
        if m_pop is not None:
            bits.append(f"a {round(m_pop)}% chance of rain")
        if bits:
            context += (f" This morning\u2019s numerical forecast for the "
                        f"same day says {_join(bits)}.")
    else:
        context += (" No stored forecast for today to set beside it. The "
                    "slide rule is unopposed here, not unbeaten.")

    # ONE part, on purpose. How decisively the barometer is MOVING is the
    # whole of what makes this card worth reading: a still barometer says
    # "settled" every day and is the least interesting instrument in the
    # house, while a fast fall is the one worth putting on a picture.
    #
    # Whether a stored forecast happens to sit beside it is NOT a second
    # part. It is data availability, and scoring it would hand every
    # station that has forecasts a permanent head start over one that does
    # not — the feed would rank a dead-calm barometer with a snapshot above
    # a plunging one without, which is precisely backwards. (It cost this
    # card a flat 1.000 on the first smoke run, and a score that saturates
    # is a score with no headroom to rank anything against.)
    parts = {"motion": round(min(1.0, abs(delta) / BAROMETER_RAPID_INHG), 4)}
    score = parts["motion"]

    return [Story(
        id=f"science.barometer_says.{ctx.today.isoformat()}",
        family=FAMILY_SCIENCE,
        story_type="barometer_says",
        title="The Barometer Says\u2026",
        emoji="\U0001f9ed",
        hero=Stat("pressure", f"sea-level pressure · {word}",
                  round(u.pressure_value(slp), u.pressure_precision),
                  u.pressure_token, u.pressure_precision),
        hero_line=says.upper(),
        context=context,
        comparison=None,
        supporting=supporting,
        # Two verdicts, side by side, each labelled with where it came from
        # and when. No score, no winner — see the docstring.
        viz=Viz(kind="forecast_pair",
                series=[{"key": "zambretti", "label": "The slide rule, 1920",
                         "verdict": says,
                         "detail": (f"{u.pressure_amount(slp)}, {word}"),
                         "note": "pressure alone"},
                        *([{"key": "modern",
                            "label": "This morning\u2019s model",
                            "verdict": _forecast_verdict(u, modern),
                            "detail": FORECAST_PROVIDER,
                            "note": "satellites, radar, a supercomputer"}]
                          if modern else [])],
                unit=None,
                axis_label="two forecasts for the same day",
                footnote=("Set beside each other, not scored against each "
                          "other. Telling which was RIGHT needs a "
                          "season of forecasts matched to what happened."),
                highlight="zambretti",
                highlight_key="zambretti"),
        period=Period(kind="moment", label="right now",
                      start=ctx.today.isoformat(),
                      end=ctx.today.isoformat(), partial=False),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


def _forecast_verdict(u: Units, modern: dict[str, Any]) -> str:
    """The modern forecast as one phrase, written HERE so the card places
    it and never composes it."""
    hi, lo = _num(modern.get("tmax_f")), _num(modern.get("tmin_f"))
    pop = _num(modern.get("pop"))
    bits = []
    if hi is not None and lo is not None:
        bits.append(f"{u.temp_deg(hi)} over {u.temp_deg(lo)}")
    elif hi is not None:
        bits.append(f"a high of {u.temp_deg(hi)}")
    if pop is not None:
        bits.append(f"{round(pop)}% chance of rain")
    return _join(bits) if bits else "no numbers stored"


# ── The Forecast vs. the Backyard ────────────────────────────────────────
# Which stored provider is scored. Only one carries temperatures today; the
# Zambretti ledger is a second "provider" with a sentence and no numbers,
# and `day_ahead_calls` additionally requires a high, so a future provider
# that stores verdicts without temperatures cannot leak in as 0°F.
FORECAST_PROVIDER = "open-meteo"
# The lead time scored: the call the model made the day before. Longer
# leads are a different question ("how far out is it any good?") for a
# card that does not exist yet.
FORECAST_LEAD_DAYS = 1
# Below this many matched days a mean error is an anecdote. A partial
# month qualifies at the same floor, marked "so far".
MIN_FORECAST_DAYS = 10
# A probability at or above this is the model CALLING for rain. Scored
# against the engine's own rain-day definition (insights.RAIN_DAY_MIN_IN).
FORECAST_RAIN_POP = 50.0
# A systematic bias this large tops the card's scale: 0.1 for a perfect
# month, ~0.8 at five degrees, rain calls fill the last fifth.
FORECAST_BIAS_FULL_F = 5.0
# The chart's yardstick: a bar this long is a full bar. Twice the scoring
# yardstick so a month's largest single miss usually fits on the picture.
FORECAST_BAR_FULL_F = 10.0


def _plural(n: int, noun: str) -> str:
    """"1 day" / "2 days" for the rain-call sentence, written here so the
    card never pluralizes."""
    return f"{n} {noun[:-1] if n == 1 and noun.endswith('s') else noun}"


def _forecast_month(today: date,
                    matched: dict[date, tuple[float, float, float, float]]
                    ) -> tuple[int, int, bool] | None:
    """(year, month, partial) for the month this card is about, or None.

    The current month, so far, once it holds MIN_FORECAST_DAYS matched
    days; otherwise the last complete calendar month; otherwise nothing.
    Freshest wins on purpose: a card about last month on the 25th is a card
    about weather the reader has stopped thinking about.
    """
    this = (today.year, today.month)
    prev_last = today.replace(day=1) - timedelta(days=1)
    prev = (prev_last.year, prev_last.month)
    for (y, m), partial in ((this, True), (prev, False)):
        n = sum(1 for d in matched if (d.year, d.month) == (y, m))
        if n >= MIN_FORECAST_DAYS:
            return y, m, partial
    return None


@producer(FAMILY_SCIENCE, "forecast_vs_backyard")
async def forecast_vs_backyard(ctx: StoryContext) -> list[Story]:
    """A month of the model's day-ahead calls, scored against what the
    station actually measured.

    `forecast_snapshots` has been filling since 1.8 with every forecast AS
    ISSUED, and until now nothing joined it to the rollups. This does, for
    one lead time (the day-ahead call) and one month: mean signed error on
    the high, mean signed error on the low, the largest single miss, and
    how the rain calls fared. Signed, forecast minus measured, so "ran
    warm" means the model promised more heat than the backyard delivered.

    Matching is strict in both directions. A day counts only when the
    model filed a high AND a low for it and the station measured both;
    today is never matched, because today's high has not happened yet.
    Rain is scored on the subset of matched days where the model filed a
    probability AND the station measured rain at all: a gauge-less station
    has no rain calls to grade, not a perfect record (absent is not zero).

    Declines below MIN_FORECAST_DAYS matched days in both the current
    month and the last complete one.
    """
    from . import insights as _ins
    calls = await ctx.day_ahead_forecasts()
    if not calls:
        return []
    rows = await ctx.daily()
    if not rows:
        return []
    by_day = {r["day"]: r for r in rows}

    # (forecast hi, forecast lo, measured hi, measured lo) per matched day.
    matched: dict[date, tuple[float, float, float, float]] = {}
    for iso, fc in calls.items():
        row = by_day.get(iso)
        if row is None:
            continue
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        if d >= ctx.today:
            continue
        f_hi, f_lo = _num(fc.get("tmax_f")), _num(fc.get("tmin_f"))
        a_hi, a_lo = _num(row.get("tempf_max")), _num(row.get("tempf_min"))
        if None in (f_hi, f_lo, a_hi, a_lo):
            continue
        matched[d] = (f_hi, f_lo, a_hi, a_lo)

    pick = _forecast_month(ctx.today, matched)
    if pick is None:
        return []
    year, month, partial = pick
    days = sorted(d for d in matched if (d.year, d.month) == (year, month))
    n = len(days)

    hi_err = {d: matched[d][0] - matched[d][2] for d in days}
    lo_err = {d: matched[d][1] - matched[d][3] for d in days}
    mean_hi = sum(hi_err.values()) / n
    mean_lo = sum(lo_err.values()) / n
    mean_f_hi = sum(matched[d][0] for d in days) / n
    mean_a_hi = sum(matched[d][2] for d in days) / n
    mean_f_lo = sum(matched[d][1] for d in days) / n
    mean_a_lo = sum(matched[d][3] for d in days) / n

    # The largest miss on either end. Ties go to the earlier day, which is
    # the day the reader met first.
    worst_side, worst_day, worst_err = max(
        [("high", d, hi_err[d]) for d in days]
        + [("low", d, lo_err[d]) for d in days],
        key=lambda t: (abs(t[2]), -t[1].toordinal()))

    # Rain calls, on the days both sides said something about rain.
    hits = false_alarms = misses = quiet = 0
    for d in days:
        pop = _num(calls[d.isoformat()].get("pop"))
        rain = _day_rain_in(by_day[d.isoformat()])
        if pop is None or rain is None:
            continue
        called = pop >= FORECAST_RAIN_POP
        rained = rain >= _ins.RAIN_DAY_MIN_IN
        if called and rained:
            hits += 1
        elif called:
            false_alarms += 1
        elif rained:
            misses += 1
        else:
            quiet += 1
    rain_calls = hits + false_alarms
    rain_days = hits + misses
    # No calls and no rain is not a perfect record, it is nothing to grade:
    # the rain sentence and the rain stats are OMITTED, never "0 of 0".
    rain_graded = rain_calls + rain_days > 0

    u = ctx.units
    month_label = f"{_MONTHS[month - 1]} {year}"
    period_label = f"{month_label} so far" if partial else month_label

    def side_word(err: float) -> str:
        return "warm" if err > 0 else "cool"

    # The side with the larger mean error leads the card; the high wins a
    # tie because it is the number people plan their day around.
    lead = "high" if abs(mean_hi) >= abs(mean_lo) else "low"
    lead_mean = mean_hi if lead == "high" else mean_lo
    lead_f = mean_f_hi if lead == "high" else mean_f_lo
    lead_a = mean_a_hi if lead == "high" else mean_a_lo
    spot_on = abs(u.temp_delta(lead_mean)) < 0.05

    if spot_on:
        title = "The Forecast Was Spot On"
        hero_line = f"THE DAY-AHEAD {lead.upper()} WAS SPOT ON"
        rank_line = (f"the day-ahead {lead} landed on the measured {lead} "
                     f"on average over {_plural(n, 'days')}")
        direction = "level"
    else:
        title = f"The Forecast Ran {side_word(lead_mean).capitalize()}"
        hero_line = (f"THE DAY-AHEAD {lead.upper()} RAN "
                     f"{u.temp_delta_deg(abs(lead_mean))} "
                     f"{side_word(lead_mean).upper()}")
        rank_line = (f"the day-ahead {lead} ran "
                     f"{u.temp_delta_deg(abs(lead_mean))} "
                     f"{side_word(lead_mean)} over {_plural(n, 'days')}")
        direction = "above" if lead_mean > 0 else "below"

    def mean_phrase(err: float) -> str:
        if abs(u.temp_delta(err)) < 0.05:
            return "landed on the measured number"
        return f"ran {u.temp_delta_deg(abs(err))} {side_word(err)}"

    context = (
        f"Every morning the model files a call for the next day, and every "
        f"night the station files what actually happened. Over "
        f"{_plural(n, 'days')} of {period_label} the day-ahead high "
        f"{mean_phrase(mean_hi)} on average and the low "
        f"{mean_phrase(mean_lo)}. The largest miss was the {worst_side} on "
        f"{_long_date(worst_day)}, {u.temp_delta_deg(abs(worst_err))} too "
        f"{side_word(worst_err)}.")
    if rain_graded:
        if rain_calls:
            context += (
                f" The model called for rain on {_plural(rain_calls, 'days')}: "
                f"{hits} came true and {false_alarms} stayed dry")
            context += (f", and rain fell on {_plural(misses, 'days')} it "
                        f"never called for." if misses
                        else ", and no rain fell uncalled.")
        else:
            context += (f" The model never called for rain, and rain fell on "
                        f"{_plural(misses, 'days')}.")

    supporting = [
        Stat("high_bias", "day-ahead high · mean miss",
             round(u.temp_delta(mean_hi), 1), u.temp_token, 1),
        Stat("low_bias", "day-ahead low · mean miss",
             round(u.temp_delta(mean_lo), 1), u.temp_token, 1),
        Stat("largest_miss",
             f"largest miss · {worst_side} on {_long_date(worst_day)}",
             round(u.temp_delta(worst_err), 1), u.temp_token, 1),
        Stat("days_matched", f"days matched in {period_label}", n, UNIT_DAYS),
    ]
    if rain_graded:
        supporting += [
            Stat("rain_hits", "rain calls that came true", hits, UNIT_DAYS),
            Stat("rain_false_alarms", "rain calls that stayed dry",
                 false_alarms, UNIT_DAYS),
            Stat("rain_misses", "rain days the model never called",
                 misses, UNIT_DAYS),
        ]

    # Scoring. Bias is the larger mean error against a five-degree
    # yardstick; rain is the share of graded rain events (calls plus rain
    # days) the model got wrong. A perfect month lands at 0.1, a five-degree
    # systematic bias with clean rain calls at 0.8, and only a month that
    # is wrong about everything reaches 1.0. Availability moves nothing:
    # a station with no rain to grade scores its bias alone.
    bias = max(abs(mean_hi), abs(mean_lo))
    parts = {"bias": round(min(1.0, bias / FORECAST_BIAS_FULL_F), 4),
             "rain_misses": round((false_alarms + misses)
                                  / (rain_calls + misses), 4)
             if rain_graded else 0.0}
    score = 0.1 + 0.7 * parts["bias"] + 0.2 * parts["rain_misses"]

    def bar(err: float) -> float:
        return round(min(1.0, abs(err) / FORECAST_BAR_FULL_F), 4)

    return [Story(
        id=f"science.forecast_vs_backyard.{year}-{month:02d}",
        family=FAMILY_SCIENCE,
        story_type="forecast_vs_backyard",
        title=title,
        emoji="\U0001f3af",
        hero=Stat("mean_miss", f"day-ahead {lead} · mean miss over "
                               f"{_plural(n, 'days')}",
                  round(u.temp_delta(lead_mean), 1), u.temp_token, 1),
        hero_line=hero_line,
        context=context,
        comparison=Comparison(
            kind="day_ahead_vs_measured",
            label=f"the model's day-ahead {lead} vs what the station measured",
            value=round(u.temp(lead_f), 1),
            baseline=round(u.temp(lead_a), 1),
            baseline_label=f"measured {lead}, averaged over "
                           f"{_plural(n, 'days')}",
            direction=direction,
            delta=round(u.temp_delta(lead_mean), 1),
            # A percentage of a temperature depends on where the reader's
            # scale puts zero. Not a number.
            delta_pct=None,
            rank_line=rank_line),
        supporting=supporting,
        # Three misses as bars on one yardstick. The bar is the SIZE of the
        # miss (a departure, so it converts by scale); the number beside it
        # keeps the sign, and the label says which way it went.
        viz=Viz(kind="chaos_dimensions",
                series=[
                    {"key": "high",
                     "label": f"high · ran {side_word(mean_hi)}"
                              if abs(u.temp_delta(mean_hi)) >= 0.05
                              else "high · spot on",
                     "score": bar(mean_hi),
                     "value": round(u.temp_delta(mean_hi), 1),
                     "unit": u.temp_token, "precision": 1,
                     "owned": lead == "high"},
                    {"key": "low",
                     "label": f"low · ran {side_word(mean_lo)}"
                              if abs(u.temp_delta(mean_lo)) >= 0.05
                              else "low · spot on",
                     "score": bar(mean_lo),
                     "value": round(u.temp_delta(mean_lo), 1),
                     "unit": u.temp_token, "precision": 1,
                     "owned": lead == "low"},
                    {"key": "largest",
                     "label": f"largest miss · {_short_date(worst_day)}",
                     "score": bar(worst_err),
                     "value": round(u.temp_delta(worst_err), 1),
                     "unit": u.temp_token, "precision": 1,
                     "owned": False},
                ],
                unit=u.temp_token,
                axis_label=(f"size of the miss · a full bar is "
                            f"{u.temp_delta_deg(FORECAST_BAR_FULL_F)}"),
                footnote=(f"Day-ahead calls from {FORECAST_PROVIDER}, stored "
                          f"as issued and scored against this station's own "
                          f"readings. A miss is forecast minus measured, so "
                          f"warm means the model promised more heat than "
                          f"arrived."),
                highlight=lead,
                highlight_key=lead),
        period=Period(kind="month", label=period_label,
                      start=f"{year}-{month:02d}-01",
                      end=days[-1].isoformat(), partial=partial),
        station=ctx.station({"first_day": rows[0]["day"],
                             "last_day": rows[-1]["day"],
                             "day_count": len(rows)}),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


@producer(FAMILY_CLIMATE, "water_year")
async def water_year(ctx: StoryContext) -> list[Story]:
    """Rain by the water year, and one line explaining why that is the
    honest way to count it.

    The hero is the newest water year the station covered well enough to
    quote a TOTAL for — the running one when it qualifies, otherwise the
    most recent completed one, which is the same "newest period with data"
    rule every other producer follows. Earlier water years appear beside it
    only when `insights.comparable_to_date` says their coverage earns it.

    Declines when the start month is January (the water year would BE the
    calendar year and there is no concept left to teach), when no water year
    in the record clears the coverage floors, and — the one that matters
    most — when the station never measured rain at all. A station with no
    gauge has not had a dry water year.
    """
    from . import insights as _ins
    from .config import settings
    start_month = int(settings.water_year_start_month)
    if start_month == 1:
        return []

    rows = await ctx.daily()
    if not rows:
        return []
    by_day: dict[str, float | None] = {}
    for r in rows:
        day = str(r.get("day") or "")
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        # `_day_rain_in` is the shared reader, and the rain-rate rule it
        # documents is inherited whole: it takes the normalized daily and
        # yearly counters the tipping gauge already won at ingest, never an
        # hourly RATE column, and returns None rather than 0.0 when nothing
        # measured. A water year is an ACCUMULATION, which is exactly the
        # thing a rate must never be mistaken for.
        by_day[day] = _day_rain_in(r)
    if not any(v is not None for v in by_day.values()):
        return []

    # Candidate water years, newest first: the running one, then completed
    # ones walking backwards. The first that clears both floors is the hero.
    newest = _water_year_label(ctx.today, start_month)
    oldest = _water_year_label(date.fromisoformat(min(by_day)), start_month)
    hero: _WaterYearWindow | None = None
    for label in range(newest, oldest - 1, -1):
        cand = _measure_water_year(by_day, label, start_month, ctx.today)
        if (cand.measured >= _ins.COMPARISON_MIN_DAYS
                and cand.coverage >= _ins.COMPARISON_COVERAGE):
            hero = cand
            break
    if hero is None:
        return []
    partial = not hero.complete

    # Every OTHER water year in the record, each measured through the SAME
    # point in its own year — the straddle is why this is not `insights`'
    # Jan-1-anchored `window_days_to_anchor`: the window here starts in
    # October and the anchor sits in the FOLLOWING calendar year, so the
    # offset is taken as whole years off the hero's anchor (Feb 29 clamped
    # by `_shift_year`) rather than as a month-day inside one year.
    #
    # Years NEWER than the hero exist whenever the hero is a fallback — the
    # running year was too thin to quote a total for. They cannot be shifted
    # forward past today, so their anchor is today, and they are drawn but
    # never compared: that is the whole reason they are not the hero.
    others: list[_WaterYearWindow] = []
    for label in range(oldest, newest + 1):
        if label == hero.label:
            continue
        shifted = _shift_year(hero.anchor, label - hero.label)
        other = _measure_water_year(by_day, label, start_month,
                                    min(shifted, ctx.today))
        if other.measured > 0:
            others.append(other)
    priors = [o for o in others if o.label < hero.label]
    comparable = [p for p in priors
                  if _ins.comparable_to_date(p.measured, hero.measured)]

    u = ctx.units
    baseline: float | None = None
    baseline_label = ""
    is_normal = False
    if len(comparable) >= MIN_NORMAL_YEARS:
        baseline = sum(p.rain for p in comparable) / len(comparable)
        years = sorted(p.label for p in comparable)
        baseline_label = (f"the {years[0]}–{years[-1]} average, "
                          f"{u.rain_amount(baseline)}")
        is_normal = True
    elif len(comparable) == 1:
        baseline = comparable[0].rain
        baseline_label = (f"water year {comparable[0].label}, "
                          f"{u.rain_amount(baseline)}")

    delta = None if baseline is None else hero.rain - baseline
    delta_pct = (round(100 * delta / baseline, 1)
                 if (delta is not None and baseline) else None)

    comparison: Comparison | None = None
    if baseline is not None and delta is not None:
        rank = 1 + sum(1 for p in comparable if p.rain > hero.rain)
        of = len(comparable) + 1
        window = (f" through {_short_date(hero.anchor)}" if partial else "")
        comparison = Comparison(
            kind="prior_water_years_to_date" if partial
                 else "prior_water_years_full",
            label=(f"vs {_WORDS.get(len(comparable), len(comparable))} "
                   f"earlier water year"
                   f"{'s' if len(comparable) > 1 else ''}"),
            value=round(u.rain_value(hero.rain), u.rain_precision),
            baseline=round(u.rain_value(baseline), u.rain_precision),
            baseline_label=f"{baseline_label}{window}",
            direction=("level" if abs(delta) < _ins.RAIN_DAY_MIN_IN
                       else "above" if delta > 0 else "below"),
            delta=round(u.rain_value(delta), u.rain_precision),
            # A water year with no rain at all is a real measurement and a
            # real story; a percentage against it is not a number.
            delta_pct=delta_pct,
            rank=rank, of=of,
            rank_line=_rank_line(rank, of, "comparable water years",
                                 "wettest"))

    # ── scoring ─────────────────────────────────────────────────────────
    parts: dict[str, float] = {}
    weights: list[tuple[float, float]] = []
    if baseline:
        departure = min(1.0, abs(delta or 0.0) / baseline / DEPARTURE_FULL)
        parts["departure"] = round(departure, 4)
        weights.append((1.00, departure))
    pool = [p.rain for p in comparable] + [hero.rain]
    if len(pool) >= MIN_DIM_POOL:
        # Mid-ranked against the station's own water years, then folded
        # around the middle: the DRIEST season on record is exactly as
        # remarkable as the wettest, and a plain rank-share would score a
        # record drought at zero.
        extremity = abs(2.0 * _rank_share(pool, hero.rain) - 1.0)
        parts["extremity"] = round(extremity, 4)
        weights.append((0.85, extremity))
    if hero.rain > 0 and hero.wettest is not None:
        # Round 1's concentration framing: when one storm holds most of the
        # season, that IS the stat. Always available, so it is what a
        # first-year station's story is scored on alone.
        concentration = min(1.0, hero.wettest[1] / hero.rain)
        parts["concentration"] = round(concentration, 4)
        weights.append((0.55, concentration))
    score = (sum(w * v for w, v in weights) / sum(w for w, _ in weights)
             if weights else 0.0)

    # ── copy ────────────────────────────────────────────────────────────
    _, hero_end = _water_year_bounds(hero.label, start_month)
    amount = u.rain_amount(hero.rain)
    if (delta_pct is not None and abs(delta_pct) >= WATER_YEAR_PCT_HERO):
        direction = "ABOVE" if delta_pct > 0 else "BELOW"
        if is_normal:
            tail = "NORMAL FOR THE DATE" if partial else "NORMAL"
            hero_line = f"{abs(delta_pct):.0f}% {direction} {tail}"
        else:
            more = "MORE" if delta_pct > 0 else "LESS"
            hero_line = (f"{abs(delta_pct):.0f}% {more} RAIN THAN WATER "
                         f"YEAR {comparable[0].label}")
    elif partial:
        hero_line = (f"{amount.upper()} SINCE "
                     f"{_MONTHS[start_month - 1].upper()} 1")
    else:
        # Not "8.42 IN IN WATER YEAR 2026": the unit token and the
        # preposition are the same two letters in inches, and the year has
        # to lead for the sentence to survive it.
        hero_line = f"WATER YEAR {hero.label}: {amount.upper()}"

    teach = (f"A water year runs {_MONTHS[start_month - 1]} 1 to "
             f"{_MONTHS[hero_end.month - 1]} {hero_end.day}, so one wet "
             f"season stays in one column instead of being cut in half at "
             f"New Year.")
    covered = (f"This station measured {hero.measured} of the {hero.window} "
               f"days in water year {hero.label}"
               + (f" so far and recorded {amount}" if partial
                  else f" and recorded {amount}")
               + (f" of rain, {u.rain_amount(hero.wettest[1])} of it on "
                  f"{_long_date(date.fromisoformat(hero.wettest[0]))}"
                  if hero.wettest else " of rain"))
    context = f"{teach} {covered}."

    supporting: list[Stat] = [
        Stat("rain_days",
             f"days with at least {u.rain_amount(_ins.RAIN_DAY_MIN_IN)}",
             hero.rain_days, UNIT_DAYS),
    ]
    if hero.wettest is not None:
        supporting.append(Stat(
            "wettest_day",
            f"wettest day · {_long_date(date.fromisoformat(hero.wettest[0]))}",
            round(u.rain_value(hero.wettest[1]), u.rain_precision),
            u.rain_token, u.rain_precision))
    if baseline is not None:
        supporting.append(Stat(
            "baseline",
            ("the station's own normal to this date" if is_normal
             else f"water year {comparable[0].label} to this date"),
            round(u.rain_value(baseline), u.rain_precision),
            u.rain_token, u.rain_precision))
    if partial:
        # The teaching moment as a number: the SAME rain counted the way
        # every other app counts it. On a desert station in August the two
        # totals are wildly different, and the difference is the card.
        jan = date(hero.anchor.year, 1, 1)
        calendar = sum(v for k, v in by_day.items()
                       if v is not None
                       and jan.isoformat() <= k <= hero.anchor.isoformat())
        supporting.append(Stat(
            "calendar_ytd", "the same rain counted from January 1",
            round(u.rain_value(calendar), u.rain_precision),
            u.rain_token, u.rain_precision))
    supporting.append(Stat("days_measured",
                           f"days measured of a {hero.window}-day window",
                           hero.measured, UNIT_DAYS))

    # One bar per water year in the record, each measured through its own
    # copy of the hero's anchor. Years that failed the comparability check
    # are still DRAWN — they are real measurements — and flagged so the
    # client can grey them rather than reading a half-covered season as a
    # dry one. The baseline above never sees them.
    comparable_labels = {p.label for p in comparable}
    bars = sorted(others + [hero], key=lambda w: w.label)
    return [Story(
        id=f"climate.water_year.{hero.label}",
        family=FAMILY_CLIMATE,
        story_type="water_year",
        title="Water Year",
        emoji="💧",
        hero=Stat("water_year_rain",
                  f"rain since {_long_date(hero.start)} {hero.start.year}",
                  round(u.rain_value(hero.rain), u.rain_precision),
                  u.rain_token, u.rain_precision),
        hero_line=hero_line,
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="water_year_bars",
                series=[{"key": str(w.label), "water_year": w.label,
                         "label": _water_year_span(
                             *_water_year_bounds(w.label, start_month)),
                         "rain": round(u.rain_value(w.rain),
                                       u.rain_precision),
                         # ⚠️ THE ROW SAYS WHAT PRECISION ITS OWN NUMBER
                         # WAS ROUNDED TO (field report from the card
                         # templates, 2026-08-30). `rain` arrives already
                         # converted and rounded to the reader's scale, and
                         # without this the card had to INFER the precision
                         # from the digits — a guess that is thinnest
                         # exactly where it matters, on a millimetre value
                         # ending in .0. Same field, same purpose, as the
                         # chaos_dimensions rows.
                         "precision": u.rain_precision,
                         "days_measured": w.measured,
                         "window_days": w.window,
                         # …and the coverage as a SENTENCE. The two counts
                         # above are data a client can sort on; the only
                         # honest way to put them ON a bar is prose, and
                         # composing prose is the client's forbidden move,
                         # so the card was drawing neither. Written here,
                         # only when the numbers differ — "334 of 334 days
                         # measured" is a label that says nothing.
                         "note": (None if w.measured >= w.window
                                  else f"{w.measured} of {w.window} days "
                                       f"measured"),
                         "comparable": (w.label == hero.label
                                        or w.label in comparable_labels),
                         "hero": w.label == hero.label}
                        for w in bars],
                unit=u.rain_token,
                axis_label=(f"rain from {_MONTHS[start_month - 1]} 1"
                            + (f", through {_short_date(hero.anchor)}"
                               if partial else "")),
                # Same grey, same sentence as the dry spell's year bars —
                # a reader who meets both charts must not have to learn two
                # vocabularies for the same visual distinction.
                footnote=(INCOMPARABLE_FOOTNOTE
                          if any(w.label != hero.label
                                 and w.label not in comparable_labels
                                 for w in bars) else None),
                highlight=hero.label, highlight_key=str(hero.label)),
        period=Period(kind="water_year",
                      label=(f"water year {hero.label} so far" if partial
                             else f"water year {hero.label}"),
                      start=hero.start.isoformat(),
                      end=hero.anchor.isoformat(),
                      partial=partial),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# ═══════════════════════ Sky & Seasons ═══════════════════════
#
# The family that can speak when nothing else can. A station switched on
# this morning has no heat ledger, no dry spell and no water year — and it
# still has a sky. On day one this family IS the Worth Sharing section,
# which makes its decline rules matter more than their small size suggests.
#
# Two things this server may simply not know:
#
#   · ASTRONOMY NEEDS A LOCATION. Every number here moves with latitude: the
#     length of the day, the moment the moon clears the horizon, the date of
#     the longest day. A guessed position produces a card that is wrong
#     invisibly — a sunset half an hour out looks perfectly plausible
#     anywhere — so no coordinates means no story. That is `air_flight`'s
#     unknown-elevation judgement applied to a whole producer rather than
#     one line, because there is nothing left of "Tonight's Sky" once the
#     sky is removed.
#   · A GROWING SEASON NEEDS COLD DATA. A station that has never recorded a
#     freeze is not a station in a frost-free paradise. It is almost always
#     a station that has not been through a winter yet, and printing "no
#     freeze" for it would be the absent-is-not-zero bug with a gardening
#     hat on.
#
# THE ARITHMETIC IS NOT HERE. `app.almanac` is a constant-for-constant port
# of the SolarMath / MoonMath / SeasonMath this project already ships in the
# app, so a share card and the almanac card inside the same app can never
# print two different sunsets for one backyard.
#
# DETERMINISM. Nothing below reads a clock. Every instant is derived from
# `ctx.today` — the pinned `climate.local_today` anchor — plus the station's
# timezone, so two calls in the same second produce the same sky, and a test
# that pins the anchor pins the moon.

# Coordinates are operator data and are not validated at write time (the
# lesson `nws_watch` pays for), so they are range-checked at read.
#
# (0, 0) READS AS UNKNOWN. Null Island is where an unset coordinate pair
# goes to look like a real place, exactly as `station_elevation_ft`'s 0.0
# means "off" rather than "sea level". The cost is a station on the equator
# in the Gulf of Guinea, where there is nothing; the alternative is drawing
# a Chandler operator a sunset from four thousand miles away.
LAT_MAX, LON_MAX = 90.0, 180.0


async def _station_is_known(ctx: StoryContext) -> bool:
    """Has this server ever heard of this station?

    A device row OR any rollup at all. `build_context` answers every MAC
    with a context (UNNAMED_STATION, device=None) so that a station with
    rollups and no device row still gets its cards, and `_station_coords`
    falls back to the server's own forecast location. Put together, those
    two kindnesses let `/api/devices/{anything}/stories` render a sunset
    over the operator's backyard attributed to "This Station". The sky
    producers ask this first: a station that has neither is not a station,
    and declines everything rather than borrowing a place.
    """
    return ctx.device is not None or bool(await ctx.daily())


def _station_coords(ctx: StoryContext) -> tuple[float, float] | None:
    """Where this station is, or None when nobody has said.

    Two sources, in the order the rest of the server already uses them: the
    device's own `info.coords` (which an operator location override
    REPLACES wholesale — `db.list_devices` does that merge, so this reads
    the winner), then `settings.forecast_lat/lon`. The second is not a
    guess: it is the location the operator configured for this server's own
    forecasts, which `alerts.py` already falls back to for the same reason.
    """
    info = (ctx.device or {}).get("info") or {}
    coords = (info.get("coords") or {}).get("coords") or {}
    lat, lon = _num(coords.get("lat")), _num(coords.get("lon"))
    if lat is None or lon is None:
        from .config import settings
        lat, lon = _num(settings.forecast_lat), _num(settings.forecast_lon)
    if lat is None or lon is None:
        return None
    if abs(lat) > LAT_MAX or abs(lon) > LON_MAX:
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return lat, lon


def _station_tz() -> Any:
    """The station's clock — `insights._tz()`, the SAME zone that stamped
    every `daily_rollups.day`. One definition, because a sunset drawn in the
    host's zone beside a rollup day drawn in the station's would disagree
    about which evening it was."""
    from . import insights as _ins
    return _ins._tz()


def _clock(when: datetime, tz: Any) -> str:
    """An instant as a wall clock: "6:00 am".

    Written out rather than taken from strftime("%p"), which is LOCALE-
    dependent and would put an empty string (or a localized one) onto a
    share card — the same trap the storm clock already pays for. The app's
    own almanac row uses the reader's locale here; the server cannot know
    it, so it uses the plain twelve-hour spelling the rest of the card copy
    is written in. There is no clock axis in the `Units` contract yet and
    nothing for a client to send, exactly as with UNIT_FT.
    """
    local = when.astimezone(tz)
    return (f"{local.hour % 12 or 12}:{local.minute:02d} "
            f"{'am' if local.hour < 12 else 'pm'}")


def _minute_of_day(when: datetime, tz: Any) -> int:
    """Minutes past local midnight — the numeric behind a clock string, so a
    template can place an event on a dial without parsing the words."""
    local = when.astimezone(tz)
    return local.hour * 60 + local.minute


def _hm(seconds: float) -> str:
    """A duration as "12h 56m", or "47m" when it is under an hour."""
    total = int(round(abs(seconds) / 60.0))
    h, m = divmod(total, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _mins_secs(seconds: float) -> str:
    """"2m 03s", or "41s" under a minute.

    Day length changes by seconds near a solstice and by two and a half
    minutes near an equinox, and the whole point of the story is that the
    number is small — rounding it to minutes would print "0m" on exactly
    the days the card is most worth showing.
    """
    s = int(round(abs(seconds)))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


# ───────────────────────── The Shrinking Day ─────────────────────────
#
# Today's daylight, what it did overnight, and — the number the design
# review called the real story — how much of it has gone or come back since
# the sun last turned around. "1h 26m of daylight gone since the Summer
# Solstice" is a fact everybody has half-noticed and nobody has measured.
#
# SEASONAL EDITIONS are the reason this producer is worth ranking rather
# than pinning: for most of the year the derivative story (a couple of
# minutes a day) is small, and then four times a year it is the only thing
# happening in the sky. So the engine detects them and lets the score say
# so — solstice, equinox, and the station's OWN longest and shortest day,
# which is a slightly different question from the solstice date and can land
# a day either side of it.
#
# ⚠️ THE SCORE IS SCALED BY LATITUDE, not just normalized by it. Every other
# producer here scores against the station's own distribution, and doing
# only that would hand a station on the equator a perfect equinox score for
# a seven-minute annual swing. Amplitude — how much daylight actually moves
# over a year at this latitude — is therefore a MULTIPLIER on the whole
# composite rather than one term inside the mean: a place with no seasons of
# light has no seasons-of-light story, whatever the calendar says. It scales
# the edition floors too, for the same reason.

# How near a solstice or equinox its edition triggers. Three days each side:
# close enough that "the solstice is on Sunday" is still the news, short
# enough that the card is not claiming a season for a fortnight.
SEASON_EDITION_DAYS = 3

# The station's own longest/shortest day is searched this far either side of
# the astronomical solstice. The two can differ by a day because a local day
# is a clock day and the solstice is an instant; six days is generous.
EXTREME_DAY_SEARCH = 6

# Two day lengths this close together are the same day length. See
# `_extreme_local_day`: the days flanking a solstice differ by hundredths of
# a second, and a tiebreak on that noise would contradict every printed
# calendar.
EXTREME_TIE_S = 5.0

# Where the annual daylight swing saturates. Six hours is a thoroughly
# seasonal place — Chandler swings about four and a half, Seattle about
# eight — and past it the reader is already living inside the story.
AMPLITUDE_FULL_S = 6 * 3600.0

# Below this the "since the solstice" line is not the headline, because a
# quarter of an hour of daylight is not something anybody has noticed. The
# card then leads with the day length itself.
DAYLIGHT_HERO_MIN_S = 15 * 60.0

# What an edition is worth before latitude scaling. These are FLOORS, not
# terms: the whole point of a solstice is that the daily change is zero
# there, so a mean that included the derivative would rank the year's most
# interesting sky day at the bottom. The edition is not a dimension of how
# interesting the day is — it is the reason the story is being told at all.
EDITION_FLOOR = {
    "polar_day": 0.95,          # the sun does not set today
    "polar_night": 0.95,        # …or does not rise
    "longest_day": 0.86,
    "shortest_day": 0.86,
    "solstice": 0.78,
    "equinox": 0.78,
}


def _extreme_local_day(lat: float, lon: float, around: date, tz: Any,
                       longest: bool) -> tuple[date, float] | None:
    """The local day near `around` with the most (or least) daylight, and
    how much.

    ⚠️ TIES ARE THE NORMAL CASE, and getting them wrong moves the card. The
    two days either side of a solstice differ by a FRACTION OF A SECOND —
    Chandler's 2026 December pair are 0.03 s apart — which is four orders of
    magnitude finer than the sunrise model's own accuracy. Picking the
    arithmetic minimum there is a coin flip that would print "the shortest
    day of the year" on December 22 while every almanac in the country said
    the 21st. So anything inside EXTREME_TIE_S counts as tied, and the tie
    goes to the day nearest the solstice instant, which is what a reader
    means by the shortest day. The same rule keeps a fortnight of identical
    24-hour polar days from being decided by rounding noise.
    """
    from . import almanac
    found: list[tuple[int, date, float]] = []
    for offset in range(-EXTREME_DAY_SEARCH, EXTREME_DAY_SEARCH + 1):
        day = around + timedelta(days=offset)
        secs = almanac.daylight_seconds(lat, lon, day, tz)
        if secs is not None:
            found.append((abs(offset), day, secs))
    if not found:
        return None
    peak = (max if longest else min)(s for _, _, s in found)
    tied = [f for f in found if abs(f[2] - peak) <= EXTREME_TIE_S]
    _, day, secs = min(tied, key=lambda f: (f[0], f[1]))
    return day, secs


@producer(FAMILY_SKY, "shrinking_day")
async def shrinking_day(ctx: StoryContext) -> list[Story]:
    """How long today is, which way it is going, and how far it has come
    since the sun turned around.

    Needs no sensor at all — only a location and a date — which is exactly
    why it exists: it is the story a station can tell on the morning it is
    unboxed. Declines when the station's coordinates are unknown (see the
    family header: a guessed latitude produces a plausible wrong sunset) and
    when the geometry yields no usable day length.
    """
    from . import almanac
    if not await _station_is_known(ctx):
        return []
    coords = _station_coords(ctx)
    if coords is None:
        return []
    lat, lon = coords
    tz = _station_tz()
    today, yesterday = ctx.today, ctx.today - timedelta(days=1)

    today_s = almanac.daylight_seconds(lat, lon, today, tz)
    if today_s is None:
        return []
    yesterday_s = almanac.daylight_seconds(lat, lon, yesterday, tz)
    change_s = None if yesterday_s is None else today_s - yesterday_s
    lengthening = (change_s or 0.0) >= 0

    # Local NOON is the anchor instant for every season comparison: it is
    # unambiguously inside the local day (a midnight anchor is one DST
    # transition away from belonging to the day before) and it is derived
    # from the pinned date, never from a clock.
    noon = almanac.local_midnight(today, tz) + timedelta(hours=12)
    next_event, next_when = almanac.next_season(noon)
    prev_event, prev_when = almanac.previous_season(noon)
    last_solstice, last_solstice_when = almanac.previous_season(
        noon, almanac.SOLSTICES)
    solstice_day = last_solstice_when.astimezone(tz).date()
    solstice_s = almanac.daylight_seconds(lat, lon, solstice_day, tz)
    since_s = None if solstice_s is None else today_s - solstice_s

    # The station's own longest and shortest days this calendar year, which
    # is what the chart marks and what the "longest day" edition tests
    # against. Searched around the solstice instants rather than assumed to
    # BE them.
    june = almanac.season_instant(almanac.JUNE_SOLSTICE,
                                  today.year).astimezone(tz).date()
    december = almanac.season_instant(almanac.DECEMBER_SOLSTICE,
                                      today.year).astimezone(tz).date()
    longest = _extreme_local_day(lat, lon, june, tz, longest=True)
    shortest = _extreme_local_day(lat, lon, december, tz, longest=False)
    span_s = (None if longest is None or shortest is None
              else longest[1] - shortest[1])

    # ── edition ─────────────────────────────────────────────────────────
    state = almanac.sun_state(lat, lon, today)
    to_next = (next_when.astimezone(tz).date() - today).days
    from_prev = (today - prev_when.astimezone(tz).date()).days
    if state == almanac.SUN_ALWAYS_UP:
        edition = "polar_day"
    elif state == almanac.SUN_ALWAYS_DOWN:
        edition = "polar_night"
    elif longest is not None and today == longest[0]:
        edition = "longest_day"
    elif shortest is not None and today == shortest[0]:
        edition = "shortest_day"
    elif min(abs(to_next), abs(from_prev)) <= SEASON_EDITION_DAYS:
        near = next_event if abs(to_next) <= abs(from_prev) else prev_event
        edition = "solstice" if near in almanac.SOLSTICES else "equinox"
    else:
        edition = ""

    # ── scoring ─────────────────────────────────────────────────────────
    #
    # Two station-relative dimensions inside the mean, then the latitude
    # scale over the top. `swing` peaks at the equinoxes and `turned` peaks
    # there too — they are the same seasonal fact measured as a rate and as
    # a total — so the editions carry the solstices, where both are zero by
    # definition.
    parts: dict[str, float] = {}
    weights: list[tuple[float, float]] = []
    # The fastest the day length moves anywhere in this year HERE, so the
    # daily change is ranked against the station's own maximum rather than
    # against a number that only means something at one latitude. Day length
    # is very nearly a cosine of amplitude span/2 over a 365-day period, so
    # its steepest slope — at the equinoxes — is span·π/365 per day. Cheaper
    # and steadier than differencing 365 sunsets, and accurate enough for a
    # 0..1 term.
    fastest_s = (None if span_s is None or span_s <= 0
                 else span_s * math.pi / 365.0)
    if change_s is not None and fastest_s:
        swing = min(1.0, abs(change_s) / fastest_s)
        parts["swing"] = round(swing, 4)
        weights.append((0.55, swing))
    if since_s is not None and span_s:
        turned = min(1.0, abs(since_s) / span_s)
        parts["turned"] = round(turned, 4)
        weights.append((0.45, turned))
    base = (sum(w * v for w, v in weights) / sum(w for w, _ in weights)
            if weights else 0.0)
    amplitude = (0.0 if span_s is None
                 else min(1.0, max(0.0, span_s / AMPLITUDE_FULL_S)))
    parts["amplitude"] = round(amplitude, 4)
    score = amplitude * base
    if edition:
        floor = amplitude * EDITION_FLOOR[edition]
        parts["edition"] = round(floor, 4)
        score = max(score, floor)

    # ── copy ────────────────────────────────────────────────────────────
    rise = almanac.sunrise(lat, lon, today, tz)
    fall = almanac.sunset(lat, lon, today, tz)
    dawn = almanac.first_light(lat, lon, today, tz)
    dusk = almanac.last_light(lat, lon, today, tz)
    solstice_name = almanac.SEASON_NAMES[last_solstice]
    next_name = almanac.SEASON_NAMES[next_event]
    next_day = next_when.astimezone(tz).date()

    if edition == "polar_day":
        title, emoji = "The Midnight Sun", "🌞"
        hero_line = "THE SUN DOES NOT SET TODAY"
    elif edition == "polar_night":
        title, emoji = "Polar Night", "🌑"
        hero_line = "THE SUN DOES NOT RISE TODAY"
    elif edition == "longest_day":
        title, emoji = "The Longest Day", "🌅"
        hero_line = f"THE LONGEST DAY OF THE YEAR · {_hm(today_s).upper()}"
    elif edition == "shortest_day":
        title, emoji = "The Shortest Day", "🌅"
        hero_line = f"THE SHORTEST DAY OF THE YEAR · {_hm(today_s).upper()}"
    else:
        title = ("The Lengthening Day" if lengthening else "The Shrinking Day")
        emoji = "🌅"
        if edition:
            when = ("TODAY" if to_next == 0 or from_prev == 0
                    else f"IN {abs(to_next)} DAY{'S' if abs(to_next) != 1 else ''}"
                    if abs(to_next) <= abs(from_prev)
                    else f"{abs(from_prev)} DAY"
                         f"{'S' if abs(from_prev) != 1 else ''} AGO")
            near_name = (next_name if abs(to_next) <= abs(from_prev)
                         else almanac.SEASON_NAMES[prev_event])
            hero_line = (f"{near_name.upper()} {when} · "
                         f"{_hm(today_s).upper()} OF DAYLIGHT")
        elif since_s is not None and abs(since_s) >= DAYLIGHT_HERO_MIN_S:
            gone = "BACK" if since_s > 0 else "GONE"
            hero_line = (f"{_hm(since_s).upper()} OF DAYLIGHT {gone} SINCE "
                         f"THE {solstice_name.upper()}")
        else:
            hero_line = f"{_hm(today_s).upper()} OF DAYLIGHT TODAY"

    # Every sentence states what it measured, because a day length is a
    # definition as much as a number: sunrise to sunset, centre of the disc,
    # refraction included — which is also why an equinox is not exactly
    # twelve hours, and saying so is the most interesting line on the card.
    if rise is not None and fall is not None:
        head = (f"The sun is up from {_clock(rise, tz)} to "
                f"{_clock(fall, tz)}, {_hm(today_s)} between sunrise and "
                f"sunset.")
    else:
        head = f"Today measures {_hm(today_s)} of daylight."
    if dawn is not None and dusk is not None:
        head += (f" First light comes at {_clock(dawn, tz)} and the last of "
                 f"it at {_clock(dusk, tz)}.")
    if change_s is not None and abs(change_s) >= 1:
        drift = (f"That is {_mins_secs(change_s)} "
                 f"{'more' if change_s > 0 else 'less'} than yesterday.")
    else:
        drift = "The day length is standing still."
    if since_s is not None and abs(since_s) >= 60:
        turn = (f"Since the {solstice_name} on "
                f"{_long_date(solstice_day)}, this station has "
                f"{'gained' if since_s > 0 else 'lost'} {_hm(since_s)} of "
                f"daylight.")
    else:
        turn = (f"The {solstice_name} was {_long_date(solstice_day)}, and the "
                f"day length has barely moved since.")
    if to_next <= 0:
        # The instant is still ahead of local noon but lands on this same
        # local date. "0 days from now" is arithmetic; "later today" is what
        # a person would say, and this card is read by people.
        ahead = f"The {next_name} arrives later today."
    elif to_next == 1:
        ahead = f"The {next_name} is tomorrow."
    else:
        ahead = (f"The {next_name} lands {_long_date(next_day)}, "
                 f"{to_next} days from now.")
    context = f"{head} {drift} {turn} {ahead}"

    # ── stats ───────────────────────────────────────────────────────────
    #
    # A clock is not a quantity, so the CLOCK LIVES IN THE LABEL and the
    # value is minutes past local midnight — the number a dial needs. The
    # precedent is the water-year card's "wettest day · August 19" label;
    # the alternative is a client formatting a time, which is composing, and
    # composing is the client's forbidden move.
    supporting: list[Stat] = [
        Stat("daylight_today", f"daylight today · {_hm(today_s)}",
             round(today_s / 60.0), UNIT_MIN),
    ]
    if change_s is not None:
        supporting.append(Stat(
            "change_today",
            (f"{'more' if change_s > 0 else 'less'} than yesterday · "
             f"{_mins_secs(change_s)}"),
            round(change_s), UNIT_SEC))
    if since_s is not None:
        supporting.append(Stat(
            "since_solstice",
            (f"daylight {'gained' if since_s > 0 else 'lost'} since the "
             f"{solstice_name} · {_hm(since_s)}"),
            round(abs(since_s) / 60.0), UNIT_MIN))
    for key, label, when in (("sunrise", "sunrise", rise),
                             ("sunset", "sunset", fall),
                             ("first_light", "first light", dawn),
                             ("last_light", "last light", dusk)):
        if when is not None:
            supporting.append(Stat(key, f"{label} · {_clock(when, tz)}",
                                   _minute_of_day(when, tz), UNIT_MIN))
    supporting.append(Stat(
        "days_to_next",
        (f"the {next_name} is today · {_long_date(next_day)}" if to_next <= 0
         else f"days to the {next_name} · {_long_date(next_day)}"),
        to_next, UNIT_DAYS))
    if longest is not None:
        supporting.append(Stat(
            "longest_day",
            f"the longest day here · {_long_date(longest[0])} · "
            f"{_hm(longest[1])}", round(longest[1] / 60.0), UNIT_MIN))
    if shortest is not None:
        supporting.append(Stat(
            "shortest_day",
            f"the shortest day here · {_long_date(shortest[0])} · "
            f"{_hm(shortest[1])}", round(shortest[1] / 60.0), UNIT_MIN))

    comparison: Comparison | None = None
    if since_s is not None and solstice_s is not None:
        comparison = Comparison(
            kind="since_last_solstice",
            label=f"vs the {solstice_name}",
            value=round(today_s / 60.0),
            baseline=round(solstice_s / 60.0),
            baseline_label=(f"{_hm(solstice_s)} on "
                            f"{_long_date(solstice_day)}"),
            direction=("level" if abs(since_s) < 60
                       else "above" if since_s > 0 else "below"),
            delta=round(since_s / 60.0),
            delta_pct=(round(100 * since_s / solstice_s, 1)
                       if solstice_s else None),
            # There is no leaderboard in an orbit. Every year does this.
            rank_line=None)

    # ── the chart ───────────────────────────────────────────────────────
    #
    # Daylight across the calendar year, one point a week, plus the three
    # days worth naming: today, and the station's own longest and shortest.
    # Weekly rather than daily because 365 points is a solid band at card
    # size, and the curve's shape is the whole message.
    marks: dict[date, str] = {}
    if longest is not None:
        marks[longest[0]] = "longest day"
    if shortest is not None:
        marks[shortest[0]] = "shortest day"
    marks[today] = "today"
    sample_days: set[date] = set(marks)
    day = date(today.year, 1, 1)
    end_of_year = date(today.year, 12, 31)
    while day <= end_of_year:
        sample_days.add(day)
        day += timedelta(days=7)
    series: list[dict[str, Any]] = []
    for day in sorted(sample_days):
        secs = almanac.daylight_seconds(lat, lon, day, tz)
        if secs is None:
            continue
        # LENGTH only, deliberately — no per-row sunrise_min/sunset_min.
        # They were asked for so a client could draw a sun-dial (the
        # sunrise/sunset envelope through the year) instead of a length
        # curve. Declined: no card draws that today, and shipping fields on
        # the wire that no honest client can render is the exact habit the
        # growing-season row labels were just removed for — speculative
        # payload ages into payload nobody dares delete. The subject of this
        # story is how long the day IS; `minutes` plus `length_label` serve
        # it completely, and today's clock times are already supporting
        # stats on the sky stories that are about instants. Revisit when a
        # template actually exists: the cost is two more almanac calls per
        # sampled day, and the series is weekly samples (~55 rows), not 365,
        # so the weight would be acceptable — it is the absent renderer,
        # not the bytes, that decides this.
        series.append({"key": day.isoformat(), "date": day.isoformat(),
                       "label": _short_date(day),
                       "minutes": round(secs / 60.0),
                       "length_label": _hm(secs),
                       "note": marks.get(day),
                       "hero": day == today})

    return [Story(
        id=f"sky.daylight.{today.isoformat()}",
        family=FAMILY_SKY,
        story_type="daylight",
        title=title,
        emoji=emoji,
        hero=Stat("daylight_today", f"daylight today · {_hm(today_s)}",
                  round(today_s / 60.0), UNIT_MIN),
        hero_line=hero_line,
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="daylight_year", series=series, unit=UNIT_MIN,
                axis_label=f"daylight through {today.year}",
                footnote=("Sunrise to sunset, centre of the sun, refraction "
                          "included. It is the same definition an almanac uses, "
                          "which is why an equinox is a few minutes over "
                          "twelve hours rather than exactly twelve."),
                highlight=today.isoformat(),
                highlight_key=today.isoformat()),
        period=Period(kind="moment", label=_long_date(today),
                      start=today.isoformat(), end=today.isoformat(),
                      partial=False),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# ───────────────────────── Tonight's Sky ─────────────────────────
#
# Moon phase, moonrise and moonset, sunset and last light — and one derived
# number that is worth more than all of them: THE DARKEST MOON-FREE VIEWING
# WINDOW. Anybody can print a moon phase. What a person standing in a
# backyard actually wants is "between 9:41 pm and 4:12 am there is no moon
# in the sky", and nothing else in this app tells them that.
#
# ⚠️ THAT DERIVATION IS WHERE THE BUGS LIVE, and the design review flagged
# it before a line was written. Reason about it as rise-and-set bookkeeping
# and there are at least five branches to get wrong:
#
#   · the moon never rises during the night (it is a moonless night, and
#     naively there is no interval to build from);
#   · the moon never sets during the night (it may be up the whole time, or
#     it may have set before the night even began);
#   · the moon is up from dusk to dawn (there is NO window, and reporting
#     zero minutes as "0" beside "moonrise —" is meaningless);
#   · the window straddles midnight, which every date-based approach gets
#     wrong on exactly the nights people care about;
#   · the moon sets and rises again inside one long winter night.
#
# So the window is NOT derived from rise/set bookkeeping. `almanac.
# darkest_window` scans the moon's ALTITUDE across the actual interval and
# returns the longest stretch it spends below the horizon. Every case above
# collapses into "the scan found one interval / no intervals / two
# intervals", the clock never enters into it, and midnight is not a special
# number. The five branches become a max().
#
# The night itself is measured between LAST LIGHT and FIRST LIGHT — civil
# twilight, the sun 6° down — because that is when the sky is genuinely dark
# rather than merely sunless. Two polar cases are handled explicitly and
# neither is a missing value: a sun that never reaches 6° below the horizon
# means there is no dark window tonight (true, and a good card), and a sun
# that never reaches 6° ABOVE it means the darkness runs around the clock.

# The instant the moon phase is read at. Not "now" — the engine is pinned to
# a local DATE and must be reproducible under it — but the middle of the
# night being described, which is also the most defensible single instant
# for a card headed "tonight".

# A gap this short is not a viewing window. Fifteen minutes of moonless sky
# between a moonset and civil dawn is a fact, not a plan, and quoting it
# would invite somebody to carry a telescope outside for it.
MIN_DARK_WINDOW_S = 15 * 60.0

# Full and new are read from ILLUMINATION rather than from the phase name,
# because the eight-phase buckets are ~3.7 days wide by design (they match
# what calendars print) and "Full Moon" for four nights running is not an
# edition. Ninety-eight percent lit is the night people photograph.
FULL_EDITION_ILLUM = 0.98
NEW_EDITION_ILLUM = 0.02

# A window covering this much of the night is "the whole night" for copy
# purposes — the last few minutes of a moonset at dusk are not worth a
# caveat on a share card.
WHOLE_NIGHT_SHARE = 0.98

# ⚠️ A CEILING ON A STORY THAT COMES ROUND EVERY NIGHT.
#
# `interestingness` is documented as comparable ACROSS producers, and this
# producer can always speak: any station with coordinates has a sky tonight.
# The best possible version of it — a new moon over a long night — is a good
# night out, not the most interesting thing that ever happened at this
# station, and it recurs every twenty-nine and a half days. The heat ledger
# and the record dry spell make ANNUAL claims. So the whole composite is
# scaled to sit below them, deliberately and in one visible place, rather
# than any single dimension being quietly bent to produce the same effect.
NIGHTLY_CEILING = 0.80

# Floors, already on the post-ceiling scale. Same reasoning as the daylight
# editions: on a full-moon night the darkness dimension is zero BY
# DEFINITION, so a plain mean would rank the most photographed night of the
# month at the bottom of the section.
NIGHT_EDITION_FLOOR = {
    "new_moon": 0.76,
    "full_moon": 0.72,
    "moonless_night": 0.68,
    "moon_all_night": 0.62,
}


@dataclass(frozen=True)
class _Night:
    """Tonight, as instants. `dark` is None when the sky does not get dark
    at all — which is a measurement about a place, not a gap in the data."""
    start: datetime                 # when the scan window opens
    end: datetime                   # …and closes
    dark: tuple[datetime, datetime] | None
    all_dark: bool                  # dark around the clock (polar night)


def _tonight(lat: float, lon: float, day: date, tz: Any) -> _Night:
    """The night that begins on `day`.

    The DARK span is last light tonight → first light tomorrow. The SCAN
    span is the same, widened to a plain 6 pm → 6 am when the sun refuses to
    cooperate, so the moon's own rise and set still have somewhere to be
    reported from. The two are kept separate on purpose: the fallback exists
    to place the MOON, and no claim about darkness is ever made from it.
    """
    from . import almanac
    civil = almanac.sun_state(lat, lon, day, angle_deg=almanac.CIVIL_ANGLE)
    midnight = almanac.local_midnight(day, tz)
    evening = midnight + timedelta(hours=18)
    morning = almanac.local_midnight(day + timedelta(days=1),
                                     tz) + timedelta(hours=6)
    if civil == almanac.SUN_ALWAYS_DOWN:
        # Deep polar night: the sun never climbs to within 6° of the
        # horizon, so "night" is the whole rotation. Noon to noon, because a
        # window centred on midnight is the one a stargazer means.
        start = midnight + timedelta(hours=12)
        end = almanac.local_midnight(day + timedelta(days=1),
                                     tz) + timedelta(hours=12)
        return _Night(start=start, end=end, dark=(start, end), all_dark=True)
    if civil == almanac.SUN_ALWAYS_UP:
        return _Night(start=evening, end=morning, dark=None, all_dark=False)
    dusk = almanac.last_light(lat, lon, day, tz)
    dawn = almanac.first_light(lat, lon, day + timedelta(days=1), tz)
    if dusk is None or dawn is None or dawn <= dusk:
        return _Night(start=evening, end=morning, dark=None, all_dark=False)
    return _Night(start=dusk, end=dawn, dark=(dusk, dawn), all_dark=False)


def _moon_events(lat: float, lon: float, day: date, tz: Any, night: _Night
                 ) -> tuple[datetime | None, datetime | None, datetime | None]:
    """(moonrise tonight, moonset tonight, the rise that put it up there).

    ⚠️ ONLY EVENTS INSIDE THE NIGHT COUNT, and the first version of this got
    it wrong in a way that read perfectly plausibly: it took the next
    moonrise and moonset AFTER dusk, which on a full-moon night is
    tomorrow's rise and on a new-moon night is tomorrow's set. The card then
    said "the moon is already up — it rises again at 7:41 pm", seventeen
    minutes after a dusk it was supposedly already above. A time that is
    real, on a night it does not belong to, is worse than a dash.

    So: the rise and the set are the ones that happen BETWEEN last light and
    first light, either of which is legitimately None. The third value is
    the most recent rise at or before dusk, which is the honest answer to
    "when did that moon get up there" on a night it never sets.

    Three local days are scanned (yesterday, today, tomorrow) because a
    night spans midnight and the rise that matters can sit on either side of
    it — and, for the third value, on the evening before.
    """
    from . import almanac
    rises: list[datetime] = []
    sets: list[datetime] = []
    for offset in (-1, 0, 1):
        rise, fall = almanac.moon_rise_set(lat, lon,
                                           day + timedelta(days=offset), tz)
        if rise is not None:
            rises.append(rise)
        if fall is not None:
            sets.append(fall)
    rises.sort()
    sets.sort()
    inside = (night.start, night.end)
    return (next((t for t in rises if inside[0] <= t <= inside[1]), None),
            next((t for t in sets if inside[0] <= t <= inside[1]), None),
            next((t for t in reversed(rises) if t <= inside[0]), None))


def _moon_phrase(phase_name: str) -> str:
    """The phase as something that can follow "the moon is": "full", "new",
    "at last quarter", "waning gibbous".

    The eight almanac names do not share a grammar — two are nouns ("New
    Moon"), two are places in a cycle ("First Quarter") and four are plain
    adjectives — so each group needs its own handling. "There is last
    quarter out, 52% lit" is the sentence this exists to prevent.
    """
    lowered = phase_name.lower()
    if lowered.endswith(" moon"):
        return lowered[:-5]
    if lowered.endswith("quarter"):
        return f"at {lowered}"
    return lowered


@producer(FAMILY_SKY, "tonights_sky")
async def tonights_sky(ctx: StoryContext) -> list[Story]:
    """What the sky is doing tonight, and when it is actually dark.

    Declines only when the station's coordinates are unknown — see the
    family header. Everything else here is a property of the sky over a
    place, so there is no sensor to be absent and no zero to mistake for
    one; the parts that cannot be known (a dark window under a midnight
    sun) are dropped from the score and the mean renormalizes over what is
    left, exactly as `air_flight` does with an unknown elevation.
    """
    from . import almanac
    if not await _station_is_known(ctx):
        return []
    coords = _station_coords(ctx)
    if coords is None:
        return []
    lat, lon = coords
    tz = _station_tz()
    today = ctx.today
    night = _tonight(lat, lon, today, tz)

    # The phase is read at the MIDDLE of the night being described. Derived
    # from the pinned date, never from a clock, so two calls in the same
    # second — or the same test run twice — see the same moon.
    mid = night.start + (night.end - night.start) / 2
    illum = almanac.moon_illumination(mid)
    phase_name = almanac.moon_phase_name(mid)
    age = almanac.moon_age_days(mid)
    to_full = almanac.days_to_full(mid)

    night_s = ((night.dark[1] - night.dark[0]).total_seconds()
               if night.dark else None)
    window = (almanac.darkest_window(lat, lon, *night.dark)
              if night.dark else None)
    window_s = (None if window is None
                else (window[1] - window[0]).total_seconds())
    if window_s is not None and window_s < MIN_DARK_WINDOW_S:
        # Below the copy floor the honest answer is "there isn't one", not a
        # nine-minute window somebody might plan an evening around.
        window, window_s = None, None
    dark_share = (None if night_s is None or not night_s
                  else min(1.0, (window_s or 0.0) / night_s))

    moonrise, moonset, rose_before = _moon_events(lat, lon, today, tz, night)
    up_at_dusk = almanac.moon_altitude_deg(
        lat, lon, night.start) > almanac.MOON_HORIZON_DEG

    # ── edition ─────────────────────────────────────────────────────────
    if illum >= FULL_EDITION_ILLUM:
        edition = "full_moon"
    elif illum <= NEW_EDITION_ILLUM:
        edition = "new_moon"
    elif dark_share is not None and dark_share >= WHOLE_NIGHT_SHARE:
        edition = "moonless_night"
    elif night.dark is not None and window is None:
        edition = "moon_all_night"
    else:
        edition = ""

    # ── scoring ─────────────────────────────────────────────────────────
    parts: dict[str, float] = {}
    weights: list[tuple[float, float]] = []
    if dark_share is not None:
        parts["darkness"] = round(dark_share, 4)
        weights.append((0.85, dark_share))
    # New and full are the two nights anybody plans around; the quarters are
    # the ones nobody photographs. |cos| peaks at both ends of the cycle,
    # which is the same curve the illumination is drawn from and therefore
    # cannot disagree with it.
    phase_pull = abs(math.cos(2 * math.pi * almanac.moon_phase_fraction(mid)))
    parts["phase"] = round(phase_pull, 4)
    weights.append((0.65, phase_pull))
    composite = sum(w * v for w, v in weights) / sum(w for w, _ in weights)
    score = NIGHTLY_CEILING * composite
    if edition:
        parts["edition"] = NIGHT_EDITION_FLOOR[edition]
        score = max(score, NIGHT_EDITION_FLOOR[edition])

    # ── copy ────────────────────────────────────────────────────────────
    sunset_t = almanac.sunset(lat, lon, today, tz)
    dusk_t = night.dark[0] if night.dark else None
    dawn_t = night.dark[1] if night.dark else None
    lit = f"{illum * 100:.0f}%"

    if window is not None and window_s is not None:
        window_label = f"{_clock(window[0], tz)} – {_clock(window[1], tz)}"
    else:
        window_label = None

    if night.dark is None:
        title, emoji = "Tonight's Sky", "🌙"
        hero_line = "THE SKY NEVER GETS FULLY DARK TONIGHT"
    elif window is None:
        title, emoji = "Tonight's Sky", "🌕" if edition == "full_moon" else "🌙"
        hero_line = (f"A FULL MOON, UP ALL NIGHT" if edition == "full_moon"
                     else f"THE MOON IS UP ALL NIGHT · {lit} LIT")
    elif edition == "new_moon":
        title, emoji = "Tonight's Sky", "🌑"
        hero_line = f"NEW MOON · {_hm(window_s).upper()} OF DARK SKY"
    elif dark_share is not None and dark_share >= WHOLE_NIGHT_SHARE:
        title, emoji = "Tonight's Sky", "🌙"
        hero_line = f"NO MOON ALL NIGHT · {_hm(window_s).upper()} OF DARK SKY"
    else:
        title, emoji = "Tonight's Sky", "🌙"
        hero_line = f"{_hm(window_s).upper()} OF MOON-FREE DARK"

    sentences: list[str] = []
    if night.all_dark:
        sentences.append("The sun does not come close to rising, so it is "
                         "dark around the clock.")
    else:
        if sunset_t is not None:
            sentences.append(f"The sun sets at {_clock(sunset_t, tz)}.")
        if dusk_t is not None and dawn_t is not None:
            sentences.append(f"The sky is properly dark from "
                             f"{_clock(dusk_t, tz)} until "
                             f"{_clock(dawn_t, tz)}.")
        elif night.dark is None:
            sentences.append("The sun does not set far enough tonight for "
                             "the sky to darken.")
    head = " ".join(sentences)

    # Every clause below names an event that happens INSIDE the night. A
    # moonrise from tomorrow evening is not part of tonight's sky, however
    # true it is.
    moon_bits: list[str] = []
    if up_at_dusk:
        moon_bits.append(
            f"it was already up, having risen at {_clock(rose_before, tz)}"
            if rose_before is not None else "it is already up")
    elif moonrise is not None:
        moon_bits.append(f"it rises at {_clock(moonrise, tz)}")
    if moonset is not None:
        moon_bits.append(f"it sets at {_clock(moonset, tz)}")
    elif up_at_dusk:
        moon_bits.append("it does not set before first light")
    elif moonrise is None:
        moon_bits.append("it never clears the horizon at all")
    if up_at_dusk and moonrise is not None:
        # A long winter night can genuinely fit a set and a second rise.
        moon_bits.append(f"then rises again at {_clock(moonrise, tz)}")
    moon_line = (f"Tonight the moon is {_moon_phrase(phase_name)}, {lit} lit"
                 + (f", and {_join(moon_bits)}." if moon_bits else "."))

    if window_label is not None and window_s is not None:
        share = ("the whole night" if dark_share is not None
                 and dark_share >= WHOLE_NIGHT_SHARE
                 else f"{(dark_share or 0) * 100:.0f}% of the night")
        dark_line = (f"That leaves {_hm(window_s)} with no moon in the sky, "
                     f"{window_label}, {share}.")
    elif night.dark is None:
        dark_line = ("There is no moon-free dark window to name: the sky "
                     "stays lit all night at this latitude in this season.")
    else:
        dark_line = ("There is no moon-free dark window tonight. The moon "
                     "is above the horizon from dusk to dawn.")
    context = f"{head} {moon_line} {dark_line}"

    # ── stats ───────────────────────────────────────────────────────────
    supporting: list[Stat] = [
        Stat("illumination", f"of the disc lit · {phase_name}",
             round(illum * 100), UNIT_PCT),
        Stat("moon_age", "days since the new moon", round(age, 1), UNIT_DAYS, 1),
    ]
    if window_s is not None and window_label is not None:
        supporting.append(Stat("dark_window",
                               f"moon-free dark · {window_label}",
                               round(window_s / 60.0), UNIT_MIN))
    if night_s is not None:
        supporting.append(Stat("night_length",
                               f"dark sky tonight · {_hm(night_s)}",
                               round(night_s / 60.0), UNIT_MIN))
    # The moonrise a card should print is the one that put the moon in
    # tonight's sky — which on a night the moon is already up is the rise
    # from before dusk, and the label says which of the two it is.
    rise_stat = moonrise if moonrise is not None else (
        rose_before if up_at_dusk else None)
    rise_label = ("moonrise" if moonrise is not None
                  else "already up since")
    for key, label, when in (("moonrise", rise_label, rise_stat),
                             ("moonset", "moonset", moonset),
                             ("sunset", "sunset", sunset_t),
                             ("last_light", "last light", dusk_t),
                             ("first_light", "first light", dawn_t)):
        if when is not None:
            supporting.append(Stat(key, f"{label} · {_clock(when, tz)}",
                                   _minute_of_day(when, tz), UNIT_MIN))
    supporting.append(Stat("days_to_full", "days to the next full moon",
                           round(to_full, 1), UNIT_DAYS, 1))

    # ── comparison: last night ──────────────────────────────────────────
    #
    # The moon rises about fifty minutes later each day, so tonight's window
    # is always meaningfully different from last night's — which makes this
    # the one comparison that is both cheap and genuinely informative. Two
    # more altitude scans, no queries, no history.
    comparison: Comparison | None = None
    prior = _tonight(lat, lon, today - timedelta(days=1), tz)
    prior_window = (almanac.darkest_window(lat, lon, *prior.dark)
                    if prior.dark else None)
    prior_s = (0.0 if prior.dark is not None and prior_window is None
               else None if prior_window is None
               else (prior_window[1] - prior_window[0]).total_seconds())
    if window_s is not None and prior_s is not None:
        delta = (window_s - prior_s) / 60.0
        comparison = Comparison(
            kind="vs_last_night",
            label="vs last night",
            value=round(window_s / 60.0),
            baseline=round(prior_s / 60.0),
            baseline_label=(f"{_hm(prior_s)} last night" if prior_s
                            else "no moon-free dark at all last night"),
            direction=("level" if abs(delta) < 5
                       else "above" if delta > 0 else "below"),
            delta=round(delta),
            # Last night having NO window is a real measurement; a
            # percentage against zero is not a number.
            delta_pct=(round(100 * (window_s - prior_s) / prior_s, 1)
                       if prior_s else None),
            rank_line=None)

    # ── the chart ───────────────────────────────────────────────────────
    #
    # One night on one axis. Points are instants, bands are spans, and every
    # one of them carries the clock string already written — a template
    # places them and never formats a time.
    origin = sunset_t if sunset_t is not None else night.start
    total_min = max(1, round((night.end - origin).total_seconds() / 60.0))

    def _at(when: datetime) -> int:
        return round((when - origin).total_seconds() / 60.0)

    series: list[dict[str, Any]] = []
    if night.dark is not None:
        series.append({"key": "night", "label": "Dark sky", "band": True,
                       "start": night.dark[0].isoformat(),
                       "end": night.dark[1].isoformat(),
                       "start_min": _at(night.dark[0]),
                       "end_min": _at(night.dark[1]),
                       "clock": (f"{_clock(night.dark[0], tz)} – "
                                 f"{_clock(night.dark[1], tz)}"),
                       "note": None})
    if window is not None and window_label is not None:
        series.append({"key": "dark_window", "label": "Moon-free dark",
                       "band": True,
                       "start": window[0].isoformat(),
                       "end": window[1].isoformat(),
                       "start_min": _at(window[0]),
                       "end_min": _at(window[1]),
                       "clock": window_label,
                       "note": _hm(window_s or 0.0)})
    for key, label, when in (("sunset", "Sunset", sunset_t),
                             ("last_light", "Last light", dusk_t),
                             ("moonset", "Moonset", moonset),
                             ("moonrise", "Moonrise", rise_stat),
                             ("first_light", "First light", dawn_t)):
        if when is not None and origin <= when <= night.end:
            series.append({"key": key, "label": label, "band": False,
                           "at": when.isoformat(), "at_min": _at(when),
                           "clock": _clock(when, tz), "note": None})
    series.sort(key=lambda e: e.get("start_min", e.get("at_min", 0)))

    # A moon that was already up when the sky went dark leaves NO moonrise
    # on this axis: `rise_stat` is then a rise from before sunset, and the
    # in-range filter above correctly refuses to place it on a ruler that
    # starts at sunset. What the reader is left with is a moonset arriving
    # from nowhere, or a dark-sky band that is mysteriously moonlit — the
    # chart drawing a distinction it never explains, which is the same hole
    # `footnote` was added to close. The note slot says it on the row a card
    # already draws. Keyed off the series itself, not off `moonrise`: a rise
    # between sunset and dusk IS on the axis and needs no qualifier.
    if up_at_dusk and not any(e["key"] == "moonrise" for e in series):
        qualifier = ("already up at sunset" if rose_before is None
                     else f"already up, risen {_clock(rose_before, tz)}")
        target = (next((e for e in series if e["key"] == "moonset"), None)
                  or next((e for e in series if e["key"] == "night"), None))
        if target is not None and target.get("note") is None:
            target["note"] = qualifier

    return [Story(
        id=f"sky.tonights_sky.{today.isoformat()}",
        family=FAMILY_SKY,
        story_type="tonights_sky",
        title=title,
        emoji=emoji,
        hero=(Stat("dark_window", f"moon-free dark · {window_label}",
                   round(window_s / 60.0), UNIT_MIN)
              if window_s is not None and window_label is not None
              else Stat("illumination", f"of the disc lit · {phase_name}",
                        round(illum * 100), UNIT_PCT)),
        hero_line=hero_line,
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="night_timeline", series=series, unit=UNIT_MIN,
                # The night's true length, stated rather than left to be
                # inferred from the last row: `first_light` is normally the
                # final point and happens to sit at the end, but a night
                # whose last drawn point is a moonset would silently shorten
                # a rows-derived ruler. It was only in the axis_label prose
                # before, which a template cannot measure with.
                domain_max=total_min,
                axis_label=f"tonight · {total_min} minutes from sunset",
                footnote=("Dark sky runs from last light to first light, "
                          "the sun 6° below the horizon, and the moon-free "
                          "band is the longest stretch inside it with the "
                          "moon under the horizon."),
                highlight="dark_window" if window is not None else "night",
                highlight_key=("dark_window" if window is not None
                               else "night")),
        period=Period(kind="moment",
                      label=f"the night of {_long_date(today)}",
                      start=night.start.isoformat(),
                      end=night.end.isoformat(), partial=False),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# ───────────────────────── Growing Season ─────────────────────────
#
# Last spring freeze, first autumn freeze, and the run of days between them
# — the number every gardener in the country plants by, and one this server
# can measure in its own backyard instead of quoting a county-wide map.
#
# TWO RULES, both of which cost somebody a card before they were written
# down:
#
#   (a) NEVER CLAIM A YEAR WE HAVE NOT LIVED. A season still running is
#       "freeze-free, 239 days and counting" — never "all year", never "365
#       days". This is the same honesty the Wrapped card's footer already
#       carries and the same rule `_period_label` applies to a partial year;
#       the difference here is that the phrase people expect to read IS
#       "frost-free all year", so the wrong words are the fluent ones.
#   (b) A STATION WITH NO COLD DATA DECLINES. A record with no freeze in it
#       is almost never a frost-free paradise — it is a station that has not
#       been through a winter yet, or one whose thermometer arrived in
#       April. Printing "no freeze recorded" for either would be the
#       absent-is-not-zero bug wearing gardening gloves, so a station that
#       has never measured a freeze says nothing at all.
#
# COVERAGE IS THE HARD PART, harder than the freezes. The last spring freeze
# can only be known by a station that was switched on THROUGH the cold
# season: one that arrived in March would report "no spring freeze" for a
# year that had one in February, and the freeze-free span it printed would
# be too long by weeks. So a year qualifies only when it was measured from
# the very start of the year and covered densely enough afterwards.
#
# HEMISPHERE. A calendar year cuts a southern-hemisphere growing season in
# half at New Year, which no rewording can fix. The split between "spring
# freeze" and "autumn freeze" is therefore placed at the station's OWN
# warmest month rather than at a hardcoded July, and a station whose warmest
# month falls in the northern winter declines outright — inventing a
# "growing year" the way the water year is invented is a bigger change than
# one producer should make on its own.

# A freeze is a daily low at or below this. Fahrenheit, like every other
# threshold in this module, because that is what daily_rollups holds; the
# COPY converts at the moment of rendering and says "0°C" to a reader on
# Celsius. The value is the freezing point of water, which is what a plant
# cares about and what every published frost date uses.
FREEZE_F = 32.0
# A hard freeze — the one that ends a tomato rather than nipping it.
HARD_FREEZE_F = 28.0

# A year needs this many measured days before it may state a season. Below
# it there is no season, only a fragment of one.
MIN_GROWING_DAYS = 200

# …and it must have been measuring from the start of the year. A station
# that arrived on January 20 cannot rule out a January freeze, and "the last
# spring freeze was March 3" would then be a claim about a month nobody
# watched. Two weeks of slack for a rollup that has not folded yet.
GROWING_START_BY = 15

# Where the departure term saturates: a season a quarter longer or shorter
# than this station's own average is a big year, and past that `extremity`
# is carrying the score anyway.
GROWING_DEPARTURE_FULL = 0.25

# Three weeks of movement in the last spring freeze is a season that arrived
# noticeably early or late. Days, compared against the station's own mean.
GROWING_SHIFT_FULL_DAYS = 21.0

# What a first season is worth. With one year on record there is nothing to
# rank against and nothing to depart from, and the card is left teaching the
# concept with an honest number in it — worth telling, never worth leading.
GROWING_BASE = 0.22

# Months in which a warmest-month peak means the calendar-year frame does
# not fit this station. See the hemisphere note above.
_WINTER_MONTHS = (11, 12, 1, 2)


@dataclass(frozen=True)
class _Season:
    """One calendar year's freeze picture.

    `last_spring` / `first_autumn` are None when that freeze did not happen
    in the measured window — which is a different thing from a freeze this
    station cannot see, and the coverage gate above is what keeps the two
    apart.
    """
    year: int
    last_spring: date | None
    spring_low: float | None        # how cold that last spring freeze got
    first_autumn: date | None
    autumn_low: float | None
    start: date                     # first freeze-free day
    end: date                       # last freeze-free day counted
    days: int
    # ⚠️ THE SAME SEASON MEASURED THROUGH THE ANCHOR'S MONTH-DAY. A running
    # season compared against finished ones is 158 days against an average
    # of 231, and "31% below average" is a headline the calendar wrote, not
    # the weather. Every other producer here solves this the same way — the
    # heat ledger's `tiers_to_date`, the dry spell's `cutoff_for` — by
    # asking every year the same question about the same window.
    days_to_date: int
    to_date_end: date               # where that measurement stopped
    running: bool                   # the autumn freeze has not happened yet
    measured: int
    window: int
    coldest: float | None
    coldest_on: date | None
    freezes: int
    hard_freezes: int

    @property
    def coverage(self) -> float:
        return self.measured / self.window if self.window > 0 else 0.0


def _warmest_month(rows: Sequence[dict[str, Any]]) -> int | None:
    """The month this station is hottest in, from its own record.

    Used to place the line between a SPRING freeze and an AUTUMN one. A
    hardcoded July would be wrong by six months for half the planet and
    wrong by weeks for a monsoon climate whose peak is in June; the station
    already knows the answer and it is one pass over rows already in hand.
    """
    totals: dict[int, tuple[float, int]] = {}
    for r in rows:
        try:
            day = date.fromisoformat(str(r.get("day") or ""))
        except ValueError:
            continue
        hi = _num(r.get("tempf_max"))
        if hi is None:
            continue
        total, count = totals.get(day.month, (0.0, 0))
        totals[day.month] = (total + hi, count + 1)
    if not totals:
        return None
    return max(totals, key=lambda m: totals[m][0] / totals[m][1])


def _seasons(rows: Sequence[dict[str, Any]], split_month: int,
             today: date) -> dict[int, _Season]:
    """One `_Season` per calendar year that measured enough to have one.

    A day counts as measured only when it carries a LOW — a rollup row with
    no `tempf_min` is a day the station was up and the thermometer was not,
    and a freeze could have hidden in it.
    """
    from . import insights as _ins
    lows: dict[int, dict[date, float]] = {}
    for r in rows:
        try:
            day = date.fromisoformat(str(r.get("day") or ""))
        except ValueError:
            continue
        lo = _num(r.get("tempf_min"))
        if lo is None:
            continue
        lows.setdefault(day.year, {})[day] = lo

    out: dict[int, _Season] = {}
    for year, by_day in lows.items():
        cut = min(today, date(year, 12, 31))
        first = date(year, 1, 1)
        if cut < first:
            continue
        window = (cut - first).days + 1
        measured = sum(1 for d in by_day if first <= d <= cut)
        if measured < MIN_GROWING_DAYS:
            continue
        if not _ins.comparable_to_date(measured, window):
            continue
        earliest = min(by_day)
        if (earliest - first).days > GROWING_START_BY:
            # Arrived after the cold season had already started running.
            # Anything this year said about a "last spring freeze" would be
            # a claim about days nobody watched.
            continue

        split = date(year, split_month, 15)
        freezes = sorted(d for d, lo in by_day.items()
                         if d <= cut and lo <= FREEZE_F)
        spring = [d for d in freezes if d <= split]
        autumn = [d for d in freezes if d > split]
        last_spring = spring[-1] if spring else None
        first_autumn = autumn[0] if autumn else None

        start = (last_spring + timedelta(days=1)) if last_spring else first
        if first_autumn is not None:
            end = first_autumn - timedelta(days=1)
            running = False
        else:
            # No autumn freeze YET. For a finished year that is the whole
            # remainder of it; for the running year it is today, and the
            # copy says "and counting" rather than naming a total.
            end = cut
            running = cut < date(year, 12, 31)
        if end < start:
            # A spring freeze on the very day the autumn one is looked for —
            # possible in a cold year with an early split. No season.
            continue

        # The same season stopped at this year's copy of the anchor date.
        # `_shift_year` clamps a February 29 anchor into a non-leap year the
        # same way the water-year arithmetic does.
        anchor = min(_shift_year(today, year - today.year), end)
        to_date = max(0, (anchor - start).days + 1) if anchor >= start else 0

        cold_days = [(lo, d) for d, lo in by_day.items() if d <= cut]
        coldest, coldest_on = (min(cold_days) if cold_days else (None, None))
        out[year] = _Season(
            year=year,
            last_spring=last_spring,
            spring_low=(by_day.get(last_spring) if last_spring else None),
            first_autumn=first_autumn,
            autumn_low=(by_day.get(first_autumn) if first_autumn else None),
            start=start, end=end, days=(end - start).days + 1,
            days_to_date=to_date, to_date_end=max(anchor, start),
            running=running, measured=measured, window=window,
            coldest=coldest, coldest_on=coldest_on,
            freezes=len(freezes),
            hard_freezes=sum(1 for d, lo in by_day.items()
                             if d <= cut and lo <= HARD_FREEZE_F))
    return out


def _season_note(s) -> str | None:
    """The one thing worth saying ON this bar, in the producer's voice.

    One note slot and several candidates, so the precedence is explicit and
    ordered by how much each changes the way the bar's LENGTH is read.

    (2) is where the per-row `last_spring_label` / `first_autumn_label`
    ended up. Those sent a date phrase on EVERY row — three of them across
    ten bars — which no card drew and which would have made a table out of
    a picture if one had; the hero's own two dates are supporting stats,
    where a card can actually place them. What the labels were reaching for
    is only interesting when a freeze is MISSING: that bar does not run
    freeze-to-freeze at all, it stops at the edge of the record, so its
    length is not the same measurement as the bars beside it. That is worth
    a word on the bar. A date that merely confirms what the bar already
    draws is not.
    """
    # (1) Still growing: the number is a floor, which changes the reading of
    # every comparison. It also already implies the missing autumn freeze,
    # so the "yet"/"recorded" distinction below never has to be made here.
    if s.running:
        return "still running"
    # (2) Bounded by the record rather than by a freeze.
    missing_spring = s.last_spring is None
    missing_autumn = s.first_autumn is None
    if missing_spring and missing_autumn:
        return "no freeze at either end"
    if missing_spring:
        return "no spring freeze"
    if missing_autumn:
        return "no autumn freeze recorded"
    # (3) Coverage, and only when incomplete — "365 of 365 days measured"
    # is a label that says nothing.
    if s.measured < s.window:
        return f"{s.measured} of {s.window} days measured"
    return None


@producer(FAMILY_SKY, "growing_season")
async def growing_season(ctx: StoryContext) -> list[Story]:
    """The run of days between the last spring freeze and the first autumn
    one, with the station's earlier years beside it.

    Declines when the station never measured a low at all, when no year was
    measured from the start of the year and densely enough to know its own
    season, when the station has NEVER recorded a freeze (see rule (b) in
    the header — that is a missing winter far more often than a missing
    frost), and when its warmest month lands in the northern winter, where a
    calendar year cuts the growing season in half.
    """
    from . import insights as _ins
    rows = await ctx.daily()
    if not rows:
        return []
    split_month = _warmest_month(rows)
    if split_month is None:
        # No daily highs anywhere: no thermometer, no season.
        return []
    if split_month in _WINTER_MONTHS:
        return []

    seasons = _seasons(rows, split_month, ctx.today)
    if not seasons:
        return []
    if not any(s.freezes for s in seasons.values()):
        # Rule (b). Every qualifying year, and not one freeze between them.
        # A genuinely frost-free station exists; a station that has simply
        # not met winter is far more common, and this producer cannot tell
        # them apart, so it says nothing rather than the flattering thing.
        return []

    # The newest year with a season, which is the running one when it
    # qualifies — the same "newest period with data" rule the rest of the
    # module follows.
    hero_year = max(seasons)
    hero = seasons[hero_year]
    priors = [s for y, s in sorted(seasons.items()) if y != hero_year]
    # A prior year may stand beside this one only when it covered a
    # comparable share of the same window — `insights.comparable_to_date` is
    # the ONE definition of that, shared with /api/insights.
    comparable = [s for s in priors
                  if _ins.comparable_to_date(s.measured, hero.measured)]

    u = ctx.units
    # ONE FRAME FOR EVERY YEAR. While this season is still running, every
    # year in the comparison is measured through today's month-day; once it
    # has met its autumn freeze, whole seasons stand against whole seasons.
    partial = hero.running

    def span_of(s: _Season) -> int:
        return s.days_to_date if partial else s.days

    hero_span = span_of(hero)
    window_note = (f" through {_short_date(hero.to_date_end)}" if partial
                   else "")
    baseline: float | None = None
    if comparable:
        baseline = sum(span_of(s) for s in comparable) / len(comparable)
    delta = None if baseline is None else hero_span - baseline

    comparison: Comparison | None = None
    if baseline is not None and delta is not None:
        rank = 1 + sum(1 for s in comparable if span_of(s) > hero_span)
        of = len(comparable) + 1
        years = sorted(s.year for s in comparable)
        span = (f"{years[0]}–{years[-1]} average" if len(years) > 1
                else f"{years[0]}")
        comparison = Comparison(
            kind="prior_years_to_date" if partial else "prior_years_full",
            label=(f"vs {_WORDS.get(len(comparable), len(comparable))} "
                   f"earlier season"
                   f"{'s' if len(comparable) > 1 else ''}"),
            value=hero_span,
            baseline=round(baseline, 1),
            baseline_label=f"{span}, {round(baseline)} days{window_note}",
            direction=("level" if abs(delta) < 1
                       else "above" if delta > 0 else "below"),
            delta=round(delta, 1),
            delta_pct=(round(100 * delta / baseline, 1) if baseline else None),
            rank=rank, of=of,
            # A running season CAN be ranked, but only inside the to-date
            # frame, and the words have to carry the frame with them: "the
            # longest so far of 4 comparable seasons to this date" is a
            # claim the next frost cannot withdraw, where "the longest
            # season on record" beside a bar that is still growing would be.
            rank_line=_rank_line(
                rank, of,
                "comparable seasons to this date" if partial
                else "comparable seasons",
                "longest so far" if partial else "longest"))

    # ── scoring ─────────────────────────────────────────────────────────
    parts: dict[str, float] = {}
    weights: list[tuple[float, float]] = []
    pool = [span_of(s) for s in comparable] + [hero_span]
    if len(pool) >= MIN_DIM_POOL:
        # Folded around the middle: the SHORTEST growing season this station
        # has recorded is exactly as remarkable as the longest, and a plain
        # rank-share would score a killing late frost at zero. Safe for a
        # running season because the pool is the to-date frame.
        extremity = abs(2.0 * _rank_share(pool, hero_span) - 1.0)
        parts["extremity"] = round(extremity, 4)
        weights.append((1.00, extremity))
    if baseline and delta is not None:
        departure = min(1.0, abs(delta) / baseline / GROWING_DEPARTURE_FULL)
        parts["departure"] = round(departure, 4)
        weights.append((0.85, departure))
    spring_priors = [s.last_spring for s in comparable if s.last_spring]
    if hero.last_spring is not None and spring_priors:
        # How far the last frost moved, in days, against this station's own
        # average date. Works for a running season — the spring freeze has
        # already happened — which is why it is the one term a mid-year card
        # can be scored on.
        mean_doy = sum(d.timetuple().tm_yday for d in spring_priors) / len(
            spring_priors)
        shift = min(1.0, abs(hero.last_spring.timetuple().tm_yday - mean_doy)
                    / GROWING_SHIFT_FULL_DAYS)
        parts["shift"] = round(shift, 4)
        weights.append((0.60, shift))
    score = (sum(w * v for w, v in weights) / sum(w for w, _ in weights)
             if weights else GROWING_BASE)
    if not weights:
        parts["first_season"] = GROWING_BASE

    # ── copy ────────────────────────────────────────────────────────────
    #
    # ⚠️ Rule (a) lives in these four lines. A running season NEVER gets a
    # total; it gets a count and the words "and counting".
    if hero.running:
        hero_line = f"FREEZE-FREE, {hero.days} DAYS AND COUNTING"
    else:
        hero_line = f"{hero.days} FREEZE-FREE DAYS"
    freeze_def = (f"A freeze is a day this station's low reached "
                  f"{u.temp_deg(FREEZE_F)} or colder.")
    if hero.last_spring is not None:
        # The reading converts here and nowhere earlier: FREEZE_F stayed
        # Fahrenheit through every comparison above, which is the invariant
        # this module keeps re-learning.
        opened = (f"The last spring freeze was {_long_date(hero.last_spring)}"
                  + (f", a low of {u.temp_deg(hero.spring_low)}"
                     if hero.spring_low is not None else ""))
    else:
        opened = (f"No spring freeze was recorded in {hero_year}. The "
                  f"season was already running on January 1")
    if hero.first_autumn is not None:
        closed = (f", and the first autumn freeze came "
                  f"{_long_date(hero.first_autumn)}"
                  + (f" at {u.temp_deg(hero.autumn_low)}"
                     if hero.autumn_low is not None else "")
                  + f", {hero.days} freeze-free days between them.")
    elif hero.running:
        closed = (f", and no autumn freeze has come yet, {hero.days} days "
                  f"and still counting as of {_long_date(hero.end)}.")
    else:
        closed = (f", and no autumn freeze followed it before the year ran "
                  f"out: {hero.days} freeze-free days.")
    covered = (f"Measured on {hero.measured} of the {hero.window} days "
               f"in {hero_year}"
               + (" so far" if hero.running else "") + ".")
    context = f"{opened}{closed} {freeze_def} {covered}"

    supporting: list[Stat] = []
    if hero.last_spring is not None:
        # The VALUE is the day of the year — the number a timeline places a
        # marker at. The date itself is in the label, the way the water-year
        # card carries "wettest day · August 19".
        supporting.append(Stat(
            "last_spring_freeze",
            f"last spring freeze · {_long_date(hero.last_spring)}",
            hero.last_spring.timetuple().tm_yday, UNIT_DAYS))
    if hero.first_autumn is not None:
        supporting.append(Stat(
            "first_autumn_freeze",
            f"first autumn freeze · {_long_date(hero.first_autumn)}",
            hero.first_autumn.timetuple().tm_yday, UNIT_DAYS))
    if baseline is not None:
        supporting.append(Stat("typical_season",
                               "the station's own average season",
                               round(baseline), UNIT_DAYS))
    supporting.append(Stat("freeze_days",
                           f"days at or below {u.temp_deg(FREEZE_F)}"
                           + (f" in {hero_year} so far" if hero.running
                              else f" in {hero_year}"),
                           hero.freezes, UNIT_DAYS))
    if hero.hard_freezes:
        supporting.append(Stat(
            "hard_freeze_days",
            f"of them at or below {u.temp_deg(HARD_FREEZE_F)}",
            hero.hard_freezes, UNIT_DAYS))
    if hero.coldest is not None and hero.coldest_on is not None:
        supporting.append(Stat(
            "coldest_low", f"coldest low · {_long_date(hero.coldest_on)}",
            round(u.temp(hero.coldest), _sig_precision(u.temp(hero.coldest))),
            u.temp_token, _sig_precision(u.temp(hero.coldest))))
    supporting.append(Stat("days_measured",
                           f"days measured of a {hero.window}-day window",
                           hero.measured, UNIT_DAYS))

    comparable_years = {s.year for s in comparable}
    bars = sorted(seasons.values(), key=lambda s: s.year)
    running_any = any(s.running for s in bars)
    faded_any = any(s.year != hero_year and s.year not in comparable_years
                    for s in bars)
    footnote_bits: list[str] = []
    if faded_any:
        footnote_bits.append(INCOMPARABLE_FOOTNOTE)
    if partial:
        footnote_bits.append(
            f"Every season is counted through {_short_date(hero.to_date_end)},"
            f" the date this one has reached. A running season measured "
            f"against finished ones would look short for no reason but the "
            f"calendar.")
    if running_any:
        footnote_bits.append("A bar marked still running has not met its "
                             "autumn freeze yet. It can only get longer.")

    return [Story(
        id=f"sky.growing_season.{hero_year}",
        family=FAMILY_SKY,
        story_type="growing_season",
        title="Growing Season",
        emoji="🌱",
        hero=Stat("freeze_free_days", "days between the freezes", hero.days,
                  UNIT_DAYS),
        hero_line=hero_line,
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="growing_season_years",
                series=[{"key": str(s.year), "year": s.year,
                         # The frame-appropriate number — to-date while this
                         # season runs, whole once it has closed — with the
                         # complete span kept beside it so a client that
                         # wants the finished figure never has to re-derive
                         # one from dates.
                         "days": span_of(s),
                         "days_full": s.days,
                         "start": s.start.isoformat(),
                         "end": (s.to_date_end if partial
                                 else s.end).isoformat(),
                         "start_doy": s.start.timetuple().tm_yday,
                         "end_doy": (s.to_date_end if partial
                                     else s.end).timetuple().tm_yday,
                         "range_label": _range_label(
                             s.start, s.to_date_end if partial else s.end),
                         # The freeze dates stay as DATA — a client can sort
                         # or place them — but the prose that used to ride
                         # beside them on every row is gone; see
                         # `_season_note`, which says the only part of it a
                         # card can honestly draw.
                         "last_spring": (s.last_spring.isoformat()
                                         if s.last_spring else None),
                         "first_autumn": (s.first_autumn.isoformat()
                                          if s.first_autumn else None),
                         "running": s.running,
                         "days_measured": s.measured,
                         "window_days": s.window,
                         "note": _season_note(s),
                         "comparable": (s.year == hero_year
                                        or s.year in comparable_years),
                         "hero": s.year == hero_year}
                        for s in bars],
                unit=UNIT_DAYS,
                axis_label="days between the last spring and first autumn "
                           "freeze",
                footnote=(" ".join(footnote_bits) if footnote_bits else None),
                highlight=hero_year, highlight_key=str(hero_year)),
        period=Period(kind="year",
                      label=(f"{hero_year} so far" if hero.running
                             else str(hero_year)),
                      start=hero.start.isoformat(),
                      end=hero.end.isoformat(),
                      partial=hero.running),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# ─────────────── The Storm That Broke the Heat ───────────────
#
# "108°F before, 84°F after." The design review called it the best
# composite idea in three rounds, and it is the one story in this module
# that could not be built when the module was: every other producer reads
# rollups that keep forever, and this one needs the minute-by-minute
# readings either side of a storm — which history thinning ages away within
# days. So the numbers are CAPTURED AT CLOSE onto the storm row
# (db._storm_close_capture holds the windows and the reasoning), and this
# producer reads what was captured or says nothing at all.
#
# That makes the decline path the load-bearing part. Every storm this
# server closed before 2.0 carries NULL in those columns, permanently, and
# so does every storm on a station with no barometer. NULL means "we did
# not capture this" — a producer that filled it with a zero would draw a
# card claiming the temperature did not move, about a storm nobody
# measured. It declines instead.

# A storm cannot be ranked without a station distribution to rank it
# against, and the distributions come from the same rollup days Wildest Day
# uses — deliberately the SAME pools, so the two producers can never
# disagree about how unusual a given gust was on this station.

# The drop is ranked against the station's own DAILY TEMPERATURE SWINGS —
# how far its temperature travels in a whole day. That is the comparison
# that makes the number mean something ("this storm did in an hour what an
# ordinary day here does between dawn and mid-afternoon"), and it is the
# only well-populated distribution available: ranking a storm's drop
# against other captured storms would mean ranking it against a pool of
# one for a long time yet.
STORM_DROP_NOTABLE = 0.70

# ...and an absolute floor underneath the ranking, which the rest of this
# module deliberately avoids. A station whose days barely move would
# otherwise rank a four-degree dip in its top quartile and publish a share
# card headlined by it. This is not a claim about weather anywhere; it is
# the smallest fall that can carry a sentence with the word "broke" in it.
STORM_MIN_DROP_F = 8.0

# Whether the storm broke HEAT, or just broke the day. The card is named
# for a claim, so the claim gets checked: the pre-storm reading has to sit
# in the top quarter of everything this station has ever recorded as a
# daily high. Below that the same story ships under an honest title rather
# than being declined — a thirty-degree fall is worth sharing whatever the
# thermometer started at.
STORM_HEAT_SHARE = 0.75

# The dimensions, their weights and the pool each is ranked in. `pool` names
# a key of `_pools()` — the station's own above-floor distribution for that
# axis. Mild and relative, like the Wildest Day weights: the drop is the
# headline and the rest is evidence.
_STORM_DIMS: tuple[tuple[str, str, float], ...] = (
    ("drop",     "swing",    1.00),
    ("rain",     "rain",     0.85),
    ("gust",     "gust",     0.80),
    ("pressure", "pressure", 0.60),
)


def _storm_local(ms: float, tz: Any) -> datetime:
    from datetime import timezone as _tzu
    return datetime.fromtimestamp(ms / 1000, tz=_tzu.utc).astimezone(tz)


@dataclass
class _BrokenHeat:
    """One captured storm, measured but not yet judged."""
    row: dict[str, Any]
    started_ms: int
    ended_ms: int
    pre: float
    post: float
    drop: float
    values: dict[str, float]        # dimension key → the measured value
    scores: dict[str, float]        # dimension key → 0..1 vs this station
    score: float = 0.0


def _storm_candidates(storms: Sequence[dict[str, Any]]) -> list[_BrokenHeat]:
    """The storms that carry a capture, and only those.

    Three separate absences, all spelled NULL and all meaning decline: a
    storm closed before the columns existed, a storm whose raw hours were
    already thinned when the backfill ran, and a station whose thermometer
    was down over one of the two windows. None of them is a storm that
    cooled nothing.
    """
    out: list[_BrokenHeat] = []
    for s in storms:
        pre, post = _num(s.get("pre_tempf")), _num(s.get("post_tempf"))
        drop = _num(s.get("temp_drop_f"))
        started, ended = _num(s.get("started_ms")), _num(s.get("ended_ms"))
        if (pre is None or post is None or drop is None
                or started is None or ended is None):
            continue
        # A storm that left the air WARMER is a real thing (a winter
        # overnight front, a nocturnal thunderstorm) and it is not this
        # story. Declined here rather than scored at zero, because the
        # hero line would read "84°F BEFORE, 108°F AFTER" under a title
        # about breaking heat.
        if drop <= 0:
            continue
        values: dict[str, float] = {"drop": drop}
        total = _num(s.get("total_in"))
        if total is not None and total > 0:
            values["rain"] = total
        gust = _num(s.get("max_gust_mph"))
        if gust is not None:
            values["gust"] = gust
        pressure = _num(s.get("pressure_change_inhg"))
        if pressure is not None:
            # How far the barometer MOVED. The captured value is signed
            # (a storm usually pushes it up) and the sign is copy, not
            # score — the station's daily pressure pool is a range too.
            values["pressure"] = abs(pressure)
        out.append(_BrokenHeat(row=s, started_ms=int(started),
                               ended_ms=int(ended), pre=pre, post=post,
                               drop=drop, values=values, scores={}))
    return out


def _score_storms(candidates: Sequence[_BrokenHeat],
                  pools: dict[str, tuple[float, list[float]]]) -> None:
    """Each candidate's per-dimension scores and its composite, in place.

    A weighted mean RENORMALIZED over the dimensions actually present, the
    same move every other producer here makes: a station with no barometer
    is not a station where the pressure held still, and a storm that
    dropped no measurable rain is not scored as if it had.
    """
    for c in candidates:
        total_w = 0.0
        weighted = 0.0
        for key, pool_key, weight in _STORM_DIMS:
            if pool_key not in pools or key not in c.values:
                continue
            floor, pool, _ = pools[pool_key]
            value = c.values[key]
            # Below the station's own floor is MEASURED-ORDINARY, a real
            # zero — unlike a missing dimension, which never enters the sum.
            score = 0.0 if value < floor else _rank_share(pool, value)
            c.scores[key] = round(score, 4)
            total_w += weight
            weighted += weight * score
        c.score = (weighted / total_w) if total_w > 0 else 0.0


@producer(FAMILY_RECORDS, "storm_broke_the_heat")
async def storm_broke_the_heat(ctx: StoryContext) -> list[Story]:
    """The storm that took the top off the day.

    Reads only storms that carry the 2.0 close capture, ranks each one's
    drop, rain, gust and pressure move against this station's own record,
    and returns the best — or nothing.

    Declines when: no stored storm carries a capture (every pre-2.0 storm,
    and every storm whose raw hours were gone before the backfill ran);
    the station has fewer than MIN_STORY_DAYS rollup days to build a
    distribution from; no swing pool survives, so the drop cannot be
    ranked at all; every captured storm left the air warmer; the best
    drop is under STORM_MIN_DROP_F; or the best drop does not clear
    STORM_DROP_NOTABLE against this station's own daily swings.

    The candidate pool is recent by construction — db.record_storm prunes
    each station to its newest 50 episodes — so this never reaches back
    past what the station has lately lived through.
    """
    candidates = _storm_candidates(await ctx.storms())
    if not candidates:
        return []
    rows = await ctx.daily()
    if len(rows) < MIN_STORY_DAYS:
        return []
    # The same measurement and pooling Wildest Day runs, on the same rows.
    # Peak rain rate is not a dimension here (the storm carries its own),
    # so the rates map is empty.
    days = _measure_days(rows, {})
    pools = _pools(days)
    if "swing" not in pools:
        return []
    _score_storms(candidates, pools)

    # Ties break on the LATER storm: two episodes indistinguishable against
    # this station's record are the same story, and the recent one is the
    # one anybody remembers. Same rule as `_best_day`.
    best = max(candidates, key=lambda c: (round(c.score, 6), c.ended_ms))
    if best.drop < STORM_MIN_DROP_F:
        return []
    if best.scores.get("drop", 0.0) < STORM_DROP_NOTABLE:
        return []

    u = ctx.units
    tz = _station_tz()
    began, closed = _storm_local(best.started_ms, tz), _storm_local(
        best.ended_ms, tz)
    on = began.date()
    minutes = max(0, round((best.ended_ms - best.started_ms) / 60_000))

    # ── the heat claim, checked ─────────────────────────────────────────
    highs = [v for v in (_num(r.get("tempf_max")) for r in rows)
             if v is not None]
    heat_share = (_rank_share(highs, best.pre)
                  if len(highs) >= MIN_DIM_POOL else None)
    broke_heat = heat_share is not None and heat_share >= STORM_HEAT_SHARE

    # ── the comparison: the drop against whole DAYS ─────────────────────
    swings = [d.values["swing"] for d in days if "swing" in d.values]
    baseline = _median(swings)
    rank = 1 + sum(1 for v in swings if v > best.drop)
    delta = best.drop - baseline
    comparison = Comparison(
        kind="station_daily_swings",
        label="vs how far this station's temperature travels in a whole day",
        # Converted, like every number that reaches a reader — and by the
        # DEPARTURE conversion, because a drop and a swing are both
        # differences. Run either through the reading conversion and a
        # 24°F fall becomes −4.4°C, which is a temperature, not a fall.
        value=round(u.temp_delta(best.drop), 1),
        baseline=round(u.temp_delta(baseline), 1),
        # No unit in the words: the numbers beside it carry the token, and
        # a Fahrenheit suffix here is exactly how a Celsius card leaks.
        baseline_label=f"the median daily swing of {len(swings)} "
                       f"recorded days",
        direction=("level" if abs(delta) < 0.5
                   else "above" if delta > 0 else "below"),
        delta=round(u.temp_delta(delta), 1),
        delta_pct=(round(100 * delta / baseline, 1) if baseline else None),
        rank=rank, of=len(swings),
        rank_line=_rank_line(rank, len(swings), "recorded daily swings",
                             "biggest"))

    # ── copy ────────────────────────────────────────────────────────────
    drop_text = u.temp_delta_deg(best.drop)
    opened = (f"Rain began at {_clock(began, tz)} on {_long_date(on)} and "
              f"the last of it fell {_hm(minutes * 60)} later.")
    measured = (f"The hour before, this station read {u.temp_deg(best.pre)}; "
                f"the hour after, {u.temp_deg(best.post)}, a fall of "
                f"{drop_text}.")
    tail: list[str] = []
    if "rain" in best.values:
        tail.append(f"{u.rain_amount(best.values['rain'])} of rain")
    if "gust" in best.values:
        tail.append(f"a {u.wind_value(best.values['gust']):.0f} "
                    f"{u.wind_token} gust")
    signed_pressure = _num(best.row.get("pressure_change_inhg"))
    if signed_pressure is not None and abs(signed_pressure) > 0:
        way = "rise" if signed_pressure > 0 else "fall"
        tail.append(f"a {u.pressure_amount(abs(signed_pressure))} "
                    f"barometer {way}")
    fell = f" It brought {_join(tail)}." if tail else ""
    heat_note = ""
    if broke_heat and heat_share is not None:
        # "hotter than 100% of the days" is arithmetic, not English. At the
        # top of the record the sentence says what it means.
        pct = round(heat_share * 100)
        heat_note = (" That afternoon was hotter than every day this "
                     "station has recorded." if pct >= 100 else
                     f" That afternoon was hotter than {pct}% of the days "
                     f"this station has recorded.")
    context = f"{opened} {measured}{fell}{heat_note}"

    # ── stats ───────────────────────────────────────────────────────────
    pre_p, post_p = (_sig_precision(u.temp(best.pre)),
                     _sig_precision(u.temp(best.post)))
    # The temperature pair goes LAST (2.0 card-agent finding): the card
    # already states it three times — hero line, both bars, the context
    # sentence — while gust, pressure and rain never reached the four
    # tiles the chassis draws. The tiles lead with what the picture
    # cannot say; a sparse station with nothing else still surfaces the
    # pair at the end.
    supporting: list[Stat] = []
    if "rain" in best.values:
        supporting.append(Stat(
            "rain", "rain the storm dropped",
            round(u.rain_value(best.values["rain"]), u.rain_precision),
            u.rain_token, u.rain_precision))
    rate = _num(best.row.get("peak_rate_in_hr"))
    if rate is not None and rate > 0:
        supporting.append(Stat(
            "peak_rate", "hardest it rained",
            round(u.rain_value(rate), u.rain_precision),
            u.rate_token, u.rain_precision))
    if "gust" in best.values:
        supporting.append(Stat(
            "gust", "peak gust", round(u.wind_value(best.values["gust"])),
            u.wind_token, 0))
    if signed_pressure is not None:
        way = "rose" if signed_pressure >= 0 else "fell"
        supporting.append(Stat(
            "pressure_change", f"the barometer {way}",
            round(u.pressure_value(abs(signed_pressure)),
                  u.pressure_precision),
            u.pressure_token, u.pressure_precision))
    dew = _num(best.row.get("dew_change_f"))
    if dew is not None:
        # A CHANGE in dew point, so the scale conversion — the same trap
        # the drop itself carries, on a stat nobody would check twice.
        way = "rose" if dew >= 0 else "fell"
        supporting.append(Stat(
            "dew_change", f"the dew point {way}",
            round(u.temp_delta(abs(dew)), 1), u.temp_token, 1))
    supporting.append(Stat("duration", "minutes of rain", minutes, UNIT_MIN))
    supporting.append(Stat("temp_before", "hottest reading in the hour before",
                           round(u.temp(best.pre), pre_p), u.temp_token,
                           pre_p))
    supporting.append(Stat("temp_after", "coolest reading in the hour after",
                           round(u.temp(best.post), post_p), u.temp_token,
                           post_p))

    drop_p = _sig_precision(u.temp_delta(best.drop))
    parts = {f"{key}_score": best.scores[key]
             for key, _, _ in _STORM_DIMS if key in best.scores}
    parts["dimensions"] = float(len(best.scores))
    if heat_share is not None:
        parts["heat_share"] = round(heat_share, 4)

    return [Story(
        id=f"records.storm_broke_the_heat.{best.ended_ms}",
        family=FAMILY_RECORDS,
        story_type="storm_broke_the_heat",
        # The title is a claim and it is checked above. A storm that fell
        # thirty degrees off a mild afternoon still gets its card; it just
        # does not get to say it broke a heat that was never there.
        title=("The Storm That Broke the Heat" if broke_heat
               else "The Storm That Cooled the Day"),
        emoji="⛈️",
        hero=Stat("temp_drop", "degrees the temperature fell",
                  round(u.temp_delta(best.drop), drop_p), u.temp_token,
                  drop_p),
        # The headline is the PAIR, because the pair is what people repeat
        # out loud. The hero stat stays the drop — the single number the
        # card sets in type — and the two agree because one is the
        # difference of the other two.
        hero_line=f"{u.temp_deg(best.pre)} BEFORE, {u.temp_deg(best.post)} "
                  f"AFTER",
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="storm_before_after",
                series=[{"key": "before",
                         "label": "the hour before",
                         "value": round(u.temp(best.pre), pre_p),
                         "unit": u.temp_token, "precision": pre_p,
                         "note": "hottest reading"},
                        {"key": "after",
                         "label": "the hour after",
                         "value": round(u.temp(best.post), post_p),
                         "unit": u.temp_token, "precision": post_p,
                         "note": "coolest reading"}],
                unit=u.temp_token,
                axis_label="temperature",
                # The two bars are not the same kind of number — one is a
                # peak and one is a trough — and a picture of two bars says
                # nothing about which is which. The footnote is where this
                # card explains its own definition, because a reader who
                # cannot tell what "before" means cannot tell whether the
                # fall is impressive.
                footnote="Before is the hottest reading in the hour ending "
                         "when the rain started; after is the coolest in "
                         "the hour that followed the last drop.",
                highlight="after", highlight_key="after"),
        period=Period(kind="moment",
                      label=f"{_long_date(on)}, {on.year}",
                      start=on.isoformat(),
                      end=closed.date().isoformat(),
                      partial=False),
        station=ctx.station(await ctx.insights()),
        interestingness=round(min(1.0, max(0.0, best.score)), 4),
        score_parts=parts,
    )]


# ── the diurnal grids: The Shape of a Year + The Humidity Tax ───────────────
#
# Both read the 12×24 month-by-hour grids `insights.assemble` already
# publishes (`diurnal_tempf` / `diurnal_feels`, hour_rollups underneath) —
# no raw scan, same cost as every other Insights-family producer.
#
# The viz is FLAT: one series row per MEASURED cell, {month, hour, value},
# because a series row is a dict of scalars on the wire (the client's
# StoryJSON decodes no arrays). A cell the station never measured sends NO
# row — the renderer leaves a hole, and a hole is the truth. Zero-filling a
# blank 3 am in February would draw the coldest cell of a year the station
# slept through.
#
# Axis words are the producer's, on the rows that carry them (the daylight
# chart's tick rule): `month_label` rides each month's first sent cell, and
# `hour_label` rides the labelled hours of the first sent month. The client
# formats no clock and no month name.

# A YEAR's shape needs the year: every month must have most of its hours
# measured, or the fingerprint is a rumour with a shape-shaped hole in it.
DIURNAL_MIN_HOURS_PER_MONTH = 18

# Below this, a feels-like grid is flat because the station has no way to
# feel (no humidity sensor reaches the rollup): the tax card would be a
# rounding error dressed as a story. °F, a difference.
TAX_MIN_SPREAD_F = 1.5
# The refund is only CLAIMED when the air measurably hands something back.
TAX_REFUND_CLAIM_F = -1.0

_HOUR_LABELS = {0: "midnight", 6: "6 am", 12: "noon", 18: "6 pm"}


def _hour_word(h: int) -> str:
    """An hour as a word a sentence can hold: 'the average 5 pm'."""
    if h == 0:
        return "midnight"
    if h == 12:
        return "noon"
    return f"{h} am" if h < 12 else f"{h - 12} pm"


def _grid_cells(grid: list[list[float | None]]) -> list[tuple[int, int, float]]:
    """(month 1-12, hour 0-23, value °F) for every measured cell."""
    return [(m + 1, h, v)
            for m, row in enumerate(grid)
            for h, v in enumerate(row)
            if v is not None]


def _grid_covers_year(cells: list[tuple[int, int, float]]) -> bool:
    per_month: dict[int, int] = {}
    for m, _, _ in cells:
        per_month[m] = per_month.get(m, 0) + 1
    return (len(per_month) == 12
            and min(per_month.values()) >= DIURNAL_MIN_HOURS_PER_MONTH)


def _grid_series(cells: list[tuple[int, int, float]],
                 convert) -> list[dict[str, Any]]:
    """Flat rows in calendar order, with the producer's own axis words on
    the rows that carry them."""
    first_month = min(m for m, _, _ in cells)
    seen_months: set[int] = set()
    out: list[dict[str, Any]] = []
    for m, h, v in sorted(cells):
        row: dict[str, Any] = {"key": f"m{m:02d}h{h:02d}", "month": m,
                               "hour": h, "value": round(convert(v), 1)}
        if m not in seen_months:
            seen_months.add(m)
            row["month_label"] = _MONTHS[m - 1][:3]
        if m == first_month and h in _HOUR_LABELS:
            row["hour_label"] = _HOUR_LABELS[h]
        out.append(row)
    return out


@producer(FAMILY_CLIMATE, "shape_of_year")
async def shape_of_year(ctx: StoryContext) -> list[Story]:
    """The year's thermal fingerprint: every hour of every month, averaged
    over everything this station has measured, as one picture.

    The hero is the LAG — the year's hottest average hour, which is never
    noon and never June 21: the air banks heat long after the sun tops out,
    and where that peak lands is a fact about THIS backyard. Declines until
    all twelve months have most of their hours measured, because the shape
    of a year cannot be told about part of one.
    """
    ins = await ctx.insights()
    grid = ins.get("diurnal_tempf")
    if not grid:
        return []
    cells = _grid_cells(grid)
    if not cells or not _grid_covers_year(cells):
        return []
    u = ctx.units

    hot = max(cells, key=lambda c: c[2])
    cold = min(cells, key=lambda c: c[2])
    span = hot[2] - cold[2]

    hot_when = f"the average {_hour_word(hot[1])} in {_MONTHS[hot[0] - 1]}"
    cold_when = f"the average {_hour_word(cold[1])} in {_MONTHS[cold[0] - 1]}"
    lag_hours = hot[1] - 12

    context = (f"Averaged over every day this station has measured, the "
               f"year's hottest hour is {hot_when} at {u.temp_deg(hot[2])}, "
               f"and its coldest is {cold_when} at {u.temp_deg(cold[2])}.")
    if lag_hours >= 2:
        context += (f" The sun tops out at noon; the air keeps banking heat "
                    f"for another {lag_hours} hours. That lag is the shape "
                    f"of this place.")
    else:
        context += (" Every cell is this station's own long-run average for "
                    "that hour of that month.")

    parts = {
        # How much of a year this station's year actually contains.
        "span": round(min(1.0, span / 45.0), 4),
        # Heat arriving late is the phenomenon the card teaches.
        "lag": round(min(1.0, max(0.0, lag_hours / 6.0)), 4),
    }
    score = 0.6 * parts["span"] + 0.4 * parts["lag"]

    return [Story(
        id="climate.shape_of_year",
        family=FAMILY_CLIMATE,
        story_type="shape_of_year",
        title="The Shape of a Year",
        emoji="🌡️",
        hero=Stat("peak_hour", f"hottest hour of the year · {hot_when}",
                  round(u.temp(hot[2]), 1), u.temp_token, 1),
        hero_line=(f"THE YEAR PEAKS AT "
                   f"{_hour_word(hot[1]).upper()} IN "
                   f"{_MONTHS[hot[0] - 1].upper()}"),
        context=context,
        comparison=None,
        supporting=[
            # Tile labels are TILE-sized: a third-width tile holds about
            # two short lines, and "the average 5 pm in July" twice over
            # rendered as "THE AVER…" in the probe. The long phrasing
            # lives in the context, where there is room to say it.
            Stat("peak_hour",
                 f"hottest hour · {_hour_word(hot[1])} in "
                 f"{_MONTHS[hot[0] - 1]}",
                 round(u.temp(hot[2]), 1), u.temp_token, 1),
            Stat("coldest_hour",
                 f"coldest hour · {_hour_word(cold[1])} in "
                 f"{_MONTHS[cold[0] - 1]}",
                 round(u.temp(cold[2]), 1), u.temp_token, 1),
            # The year's full sweep — a DIFFERENCE, scale conversion.
            Stat("year_span", "from the coldest hour to the hottest",
                 round(u.temp_delta(span), 1), u.temp_token, 1),
            Stat("days_averaged", "days averaged into the picture",
                 ins.get("day_count"), UNIT_DAYS),
        ],
        viz=Viz(kind="month_hour_grid",
                series=_grid_series(cells, u.temp),
                unit=u.temp_token,
                axis_label="average temperature, by month and hour of day",
                footnote=("Each cell is this station's own average for that "
                          "hour of that month, over everything it has "
                          "measured. A blank cell was never measured."),
                highlight=hot[1], highlight_key=f"m{hot[0]:02d}h{hot[1]:02d}"),
        period=Period(kind="all", label="the whole record",
                      start=ins.get("first_day"), end=ins.get("last_day"),
                      partial=False),
        station=ctx.station(ins),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


@producer(FAMILY_SCIENCE, "humidity_tax")
async def humidity_tax(ctx: StoryContext) -> list[Story]:
    """What the air charges on top of the thermometer, hour by hour across
    the year — and what it hands back.

    The grid is feels-like minus temperature, cell by cell, only where BOTH
    were measured. The tax (humidity slowing sweat) is the headline; the
    refund — hours that feel COOLER than the reading, dry air evaporating
    the difference — is the half nobody expects, and it is only claimed
    when the station measured it. Declines while the spread is under
    TAX_MIN_SPREAD_F: a station with no way to feel humidity produces a
    flat grid, and a flat grid is a rounding error, not a story.
    """
    ins = await ctx.insights()
    temp_grid = ins.get("diurnal_tempf")
    feels_grid = ins.get("diurnal_feels")
    if not temp_grid or not feels_grid:
        return []
    cells = [(m + 1, h, feels_grid[m][h] - temp_grid[m][h])
             for m in range(12) for h in range(24)
             if temp_grid[m][h] is not None and feels_grid[m][h] is not None]
    if not cells or not _grid_covers_year(cells):
        return []
    u = ctx.units

    tax = max(cells, key=lambda c: c[2])
    refund = min(cells, key=lambda c: c[2])
    if tax[2] - refund[2] < TAX_MIN_SPREAD_F or tax[2] <= 0:
        return []
    charged = sum(1 for _, _, v in cells if v >= 1.0)
    charged_pct = round(100 * charged / len(cells))

    tax_when = f"the average {_hour_word(tax[1])} in {_MONTHS[tax[0] - 1]}"
    refund_when = (f"the average {_hour_word(refund[1])} in "
                   f"{_MONTHS[refund[0] - 1]}")

    context = (f"Feels-like is what the air charges on top of the "
               f"thermometer: humidity slows sweat, and the body reads the "
               f"difference as heat. The bill peaks at {tax_when}, "
               f"{u.temp_delta_deg(tax[2])} above the reading.")
    has_refund = refund[2] <= TAX_REFUND_CLAIM_F
    if has_refund:
        context += (f" The air hands it back too. {refund_when[0].upper()}{refund_when[1:]} feels "
                    f"{u.temp_delta_deg(abs(refund[2]))} BELOW the "
                    f"thermometer, dry air evaporating the difference.")

    # Tile labels are TILE-sized (the shape card's rule): the long
    # "the average 7 am in August" phrasing lives in the context.
    supporting = [
        Stat("biggest_tax",
             f"steepest charge · {_hour_word(tax[1])} in "
             f"{_MONTHS[tax[0] - 1]}",
             round(u.temp_delta(tax[2]), 1), u.temp_token, 1),
    ]
    if has_refund:
        supporting.append(Stat(
            "biggest_refund",
            f"biggest refund · {_hour_word(refund[1])} in "
            f"{_MONTHS[refund[0] - 1]}",
            round(u.temp_delta(refund[2]), 1), u.temp_token, 1))
    supporting.append(Stat(
        "charged_share", "hours the air charges extra",
        charged_pct, UNIT_PCT))

    parts = {
        "tax": round(min(1.0, tax[2] / 12.0), 4),
        "refund": round(min(1.0, abs(min(0.0, refund[2])) / 8.0), 4),
    }
    score = 0.6 * parts["tax"] + 0.4 * parts["refund"]

    return [Story(
        id="science.humidity_tax",
        family=FAMILY_SCIENCE,
        story_type="humidity_tax",
        title="The Humidity Tax",
        emoji="🥵",
        hero=Stat("biggest_tax", f"over the thermometer at {tax_when}",
                  round(u.temp_delta(tax[2]), 1), u.temp_token, 1),
        hero_line=f"A {u.temp_delta_deg(tax[2])} HUMIDITY TAX",
        context=context,
        comparison=None,
        supporting=supporting,
        viz=Viz(kind="month_hour_delta_grid",
                series=_grid_series(cells, u.temp_delta),
                unit=u.temp_token,
                axis_label=("feels-like minus temperature, by month and "
                            "hour of day"),
                footnote=("Warm-tinted cells feel hotter than the "
                          "thermometer reads; cool-tinted cells feel "
                          "cooler, dry air handing the difference back. "
                          "A blank cell was never measured."),
                highlight=tax[1],
                highlight_key=f"m{tax[0]:02d}h{tax[1]:02d}"),
        period=Period(kind="all", label="the whole record",
                      start=ins.get("first_day"), end=ins.get("last_day"),
                      partial=False),
        station=ctx.station(ins),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]


# ───────────────────────── Comfortable Months ─────────────────────────
#
# "When I am awake and outside, which month feels best?" The diurnal grid
# answers where the year is hottest; this card answers when it is PLEASANT,
# and it answers for the hours a person is actually out in it. A month whose
# small hours are lovely and whose afternoons are not is not a comfortable
# month, so the count runs only over waking hours.
#
# Waking hours: 7 am up to 10 pm, local. The American Time Use Survey has
# most adults up between 6 and 7:30 am and in bed near 11 pm, about fifteen
# waking hours; 7 to 10 is the middle of that, and it is the same window
# for every station because the question is about people, not weather.
#
# Comfortable: a FEELS-LIKE reading inside `insights.COMFORT_LOW_F..HIGH_F`
# (60..80°F). Feels-like, not the thermometer, because a humid 82°F and a
# dry 82°F are different afternoons and the reader knows it. The band is
# fixed at fold time (insights._comfort_params); the card states it.
#
# The record's ranking is the picture; this year's months ride along as
# row notes and one sentence, because "so far this year" is the question
# the reader asked and "on average" is the answer they can trust.
WAKING_START_HOUR = 7
WAKING_END_HOUR = 22                     # up to, not including
WAKING_HOURS = range(WAKING_START_HOUR, WAKING_END_HOUR)
# A (year, month) counts once it has this many measured days and this many
# of the fifteen waking hours with readings; a waking hour needs this many
# readings to have a share at all. Clock time, not readings, is the unit:
# every covered hour weighs the same and every covered year-month weighs
# the same, so a year polled once a minute cannot outvote a year polled
# every five, and thinning old history cannot move a month's rank
# (R18 finding 1: pooled counts gave a 60-per-hour year five times the
# say of a 12-per-hour year).
COMFORT_MIN_DAYS_PER_MONTH = 10
COMFORT_MIN_HOURS_PRESENT = 12
COMFORT_MIN_READINGS_PER_HOUR = 3
# Fewer ranked months than this is not a ranking of the year.
COMFORT_MIN_MONTHS = 6
# Full marks when the best and worst months are 60 points of share apart.
COMFORT_FULL_SPREAD = 0.6


def _hours_a_day(share: float) -> float:
    # A mean of floats lands a hair under its exact value (0.85 arrives as
    # 0.8499999…); settle the share first so 0.85 of 15 hours is 12.8, not 12.7.
    return round(round(share, 6) * len(WAKING_HOURS), 1)


def _comfort_tail(hot_share: float, cold_share: float) -> str:
    """Why the least comfortable month is uncomfortable, in its own voice."""
    if hot_share >= 2 * cold_share and hot_share > 0:
        return "the rest too hot"
    if cold_share >= 2 * hot_share and cold_share > 0:
        return "the rest too cold"
    return "the rest split between too hot and too cold"


@producer(FAMILY_CLIMATE, "comfortable_months")
async def comfortable_months(ctx: StoryContext) -> list[Story]:
    from . import insights as _ins
    rows = await ctx.comfort()
    if not rows:
        return []
    daily = await ctx.daily()
    if not daily:
        return []
    u = ctx.units
    this_year = ctx.today.year

    days_by_ym: dict[tuple[int, int], int] = {}
    for d in daily:
        ym = (int(d["day"][:4]), int(d["day"][5:7]))
        days_by_ym[ym] = days_by_ym.get(ym, 0) + 1

    # One share per (year, month, hour) with enough readings; a (year, month)
    # is the mean of its covered waking hours; a calendar month is the mean
    # of its covered year-months. Equal weight at every step (see the
    # constants above).
    hours: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for year, month, hour, n, comfy, hot, cold in rows:
        if hour not in WAKING_HOURS or n < COMFORT_MIN_READINGS_PER_HOUR:
            continue
        hours.setdefault((year, month), []).append(
            (comfy / n, hot / n, cold / n))

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs)

    year_month: dict[tuple[int, int], tuple[float, float, float]] = {}
    for (y, m), shares in hours.items():
        if (len(shares) < COMFORT_MIN_HOURS_PRESENT
                or days_by_ym.get((y, m), 0) < COMFORT_MIN_DAYS_PER_MONTH):
            continue
        year_month[(y, m)] = (_mean([c for c, _, _ in shares]),
                              _mean([h for _, h, _ in shares]),
                              _mean([k for _, _, k in shares]))

    by_month: dict[int, list[tuple[float, float, float]]] = {}
    for (y, m), sh in year_month.items():
        by_month.setdefault(m, []).append(sh)
    ranked = sorted(
        ((m, _mean([c for c, _, _ in v]), _mean([h for _, h, _ in v]),
          _mean([k for _, _, k in v]))
         for m, v in by_month.items()),
        key=lambda x: (-x[1], x[0]))
    if len(ranked) < COMFORT_MIN_MONTHS:
        return []
    best, worst = ranked[0], ranked[-1]
    spread = best[1] - worst[1]
    years_of = {m: len(v) for m, v in by_month.items()}

    this_ranked = sorted(
        ((m, sh[0]) for (y, m), sh in year_month.items() if y == this_year),
        key=lambda x: (-x[1], x[0]))
    this_best = this_ranked[0] if this_ranked else None
    this_share = {m: sh for m, sh in this_ranked}

    # The band edges as whole degrees in the reader's scale: 60..80°F is
    # exact, 16..27°C is the same band to the half-degree, and a definition
    # that reads "15.6°C" is a conversion, not a definition.
    lo = f"{round(u.temp(_ins.COMFORT_LOW_F))}{u.temp_suffix}"
    hi = f"{round(u.temp(_ins.COMFORT_HIGH_F))}{u.temp_suffix}"
    window = f"{_hour_word(WAKING_START_HOUR)} and {_hour_word(WAKING_END_HOUR)}"
    # The years THIS month was measured in, not the record's span: a month
    # seen in one year says so.
    n_years = years_of[best[0]]
    year_word = "year" if n_years == 1 else "years"
    best_name = _MONTHS[best[0] - 1]
    worst_name = _MONTHS[worst[0] - 1]

    context = (f"Between {window}, when most people are up, {best_name}'s "
               f"feels-like temperature sat between {lo} and {hi} for "
               f"{_hours_a_day(best[1])} of every {len(WAKING_HOURS)} hours, "
               f"averaged over {n_years} {year_word} of record. "
               f"{worst_name} is the other end at "
               f"{_hours_a_day(worst[1])}, {_comfort_tail(worst[2], worst[3])}.")
    if this_best is not None:
        tb_name = _MONTHS[this_best[0] - 1]
        if this_best[0] == best[0]:
            context += (f" So far in {this_year} it is {tb_name} again, at "
                        f"{_hours_a_day(this_best[1])} hours a day.")
        else:
            context += (f" So far in {this_year}, {tb_name} has been the most "
                        f"comfortable month, at {_hours_a_day(this_best[1])} "
                        f"hours a day.")

    # Two tiles at most, so each gets half the card: "least comfortable"
    # truncated at three columns on the probe render, and the years of
    # record are already in the sentence above.
    supporting = [
        Stat("worst_month", f"worst month · {worst_name}",
             _hours_a_day(worst[1]), "h", 1),
    ]
    if this_best is not None:
        supporting.insert(0, Stat(
            "best_this_year",
            f"best so far in {this_year} · {_MONTHS[this_best[0] - 1]}",
            _hours_a_day(this_best[1]), "h", 1))

    series = []
    for m, share, hot, cold in ranked:
        row: dict[str, Any] = {
            "key": f"m{m:02d}", "month": m, "label": _MONTHS[m - 1][:3],
            "share": round(share, 4), "hours": _hours_a_day(share),
            "hero": m == best[0],
        }
        if m in this_share:
            # Short on purpose: the note sits beside the value in a
            # column a few words wide, and the sentence above already says
            # "so far".
            row["note"] = f"{_hours_a_day(this_share[m])} h in {this_year}"
        series.append(row)

    parts = {"spread": min(1.0, spread / COMFORT_FULL_SPREAD)}
    return [Story(
        id=f"climate.comfortable_months.{daily[-1]['day']}",
        family=FAMILY_CLIMATE,
        story_type="comfort_months",
        title="The Comfortable Months",
        emoji="🌤️",
        hero=Stat("best_month", f"comfortable hours a day · {best_name}",
                  _hours_a_day(best[1]), "h", 1),
        hero_line=f"{best_name.upper()} IS THE MONTH TO BE OUTSIDE",
        context=context,
        comparison=None,
        supporting=supporting,
        viz=Viz(kind="comfort_months", series=series, unit="h",
                axis_label="comfortable hours in a waking day, by month",
                highlight=best[0], highlight_key=f"m{best[0]:02d}",
                domain_max=float(len(WAKING_HOURS)),
                footnote=(f"Comfortable means a feels-like temperature between "
                          f"{lo} and {hi}, counted between {window}. A month "
                          f"the record has not covered is left out.")),
        period=Period(kind="all", label="the whole record",
                      start=daily[0]["day"], end=daily[-1]["day"],
                      partial=False),
        station=ctx.station({"first_day": daily[0]["day"],
                             "last_day": daily[-1]["day"],
                             "day_count": len(daily)}),
        interestingness=parts["spread"],
        score_parts=parts,
    )]


__all__ = ["FAMILIES", "PERIOD_KINDS", "Comparison", "Period", "Stat", "Story",
           "StoryContext",
           "UNITS_NATIVE", "Units", "Viz", "build_context", "parse_units",
           "producer", "registered", "top_stories"]


# ───────────────────────── Record Broken ─────────────────────────
#
# The Strava-PR card: TODAY, still running, has already passed a mark this
# station had never reached. Nothing else in the engine is about today;
# every other card ranks finished periods. This one exists because the
# moment a record falls is the moment somebody wants to say so, and by
# tomorrow it is a line in a table.
#
# Today's rollup row updates at ingest, so the day's extremes are a live
# high-water mark and only move one way before midnight: a high, a gust
# and a rain total can only climb, a low can only fall. That is what lets
# the copy say "so far" and still be a record.

# A station this young has not seen a year, and a record on a two-week
# archive is a description of the fortnight. Counted per metric, INCLUDING
# today: a rain gauge added last month cannot set a rain record on a
# thermometer's two years of history.
MIN_RECORD_DAYS = 365
# A month-of-year record needs a month's worth of prior same-month days,
# or "the hottest August day on record" is measured against one August
# afternoon.
MIN_MONTH_RECORD_DAYS = 30
# The margin a record must clear, in NATIVE units, so a sensor's own
# jitter cannot break a record every morning. Compared before anything is
# converted; the reader's scale only ever sees the result.
RECORD_MARGIN_F = 0.5
RECORD_MARGIN_MPH = 1.0
RECORD_MARGIN_IN = 0.01
# Rows on the ranked chart: today and the four days it beat.
RECORD_ROWS = 5
# The two scores, exactly as decided: an all-time record is the most
# interesting thing that can happen to a station on a given day, and a
# month-of-year record is a real one with a smaller claim. Availability
# is not interestingness: the card either exists at one of these two
# values or it does not exist.
RECORD_SCORE_ALL_TIME = 1.0
RECORD_SCORE_MONTH = 0.7
UNIT_YEARS = "years"


@dataclass(frozen=True)
class _RecordMetric:
    key: str
    higher: bool            # True: the record is the LARGEST value
    margin: float           # native units
    adjective: str          # "Hottest"
    noun: str               # "day" / "night" / "gust": the thing counted
    quantity: str           # "high" / "low" / "peak gust" / "rain"
    verb: str               # what today's number can still do: climb / fall


_RECORD_METRICS: tuple[_RecordMetric, ...] = (
    _RecordMetric("high", True, RECORD_MARGIN_F, "Hottest", "day", "high", "climb"),
    _RecordMetric("low", False, RECORD_MARGIN_F, "Coldest", "night", "low", "fall"),
    _RecordMetric("gust", True, RECORD_MARGIN_MPH, "Strongest", "gust", "peak gust", "climb"),
    _RecordMetric("rain", True, RECORD_MARGIN_IN, "Wettest", "day", "rain", "climb"),
)


def _record_reading(metric: _RecordMetric, row: dict[str, Any]) -> float | None:
    if metric.key == "high":
        return _num(row.get("tempf_max"))
    if metric.key == "low":
        return _num(row.get("tempf_min"))
    if metric.key == "gust":
        return _num(row.get("windgustmph_max"))
    return _day_rain_in(row)


def _record_display(metric: _RecordMetric, value: float, u: Units
                    ) -> tuple[float, str, int, str]:
    """(value in the reader's units, unit token, precision, the value as
    copy). READINGS, offset and all, for the two temperatures."""
    if metric.key in ("high", "low"):
        return round(u.temp(value), 1), u.temp_token, 1, u.temp_deg(value)
    if metric.key == "gust":
        shown = u.wind_value(value)
        return round(shown), u.wind_token, 0, f"{shown:.0f} {u.wind_token}"
    return (round(u.rain_value(value), u.rain_precision), u.rain_token,
            u.rain_precision, u.rain_amount(value))


def _record_delta(metric: _RecordMetric, delta: float, u: Units
                  ) -> tuple[float, str]:
    """A DIFFERENCE between two readings: temperatures by scale only."""
    if metric.key in ("high", "low"):
        return round(u.temp_delta(delta), 1), u.temp_delta_deg(delta)
    if metric.key == "gust":
        return round(u.wind_value(delta), 1), f"{u.wind_value(delta):.1f} {u.wind_token}"
    return round(u.rain_value(delta), u.rain_precision), u.rain_amount(delta)


@dataclass(frozen=True)
class _Record:
    metric: _RecordMetric
    scope: str                          # "all_time" | "month"
    value: float                        # today's, native
    prior: float                        # the old record, native
    prior_day: date
    pool: list[tuple[date, float]]      # every prior measured day in scope
    all_time: tuple[float, date] | None # for a month record: the mark it did NOT beat

    @property
    def margin(self) -> float:
        return (self.value - self.prior if self.metric.higher
                else self.prior - self.value)


def _prior_record(metric: _RecordMetric, pool: Sequence[tuple[date, float]]
                  ) -> tuple[float, date]:
    """The standing record and the day it was SET: the earliest day to
    reach it. Later days that tied it equalled a record, they did not set
    one."""
    best = max(v for _, v in pool) if metric.higher else min(v for _, v in pool)
    when = min(d for d, v in pool if v == best)
    return best, when


@producer(FAMILY_RECORDS, "record_broken")
async def record_broken(ctx: StoryContext) -> list[Story]:
    """Today has already beaten a station record, and the day is not over.

    Today's rollup row is compared, per metric, against every PRIOR day:
    the high and the low against the whole record and against the same
    calendar month in earlier years, the peak gust and the rain total
    against the whole record. A metric counts only when today's number
    clears the old mark by a real margin, compared in native units.

    DECLINES when there is no row for today (a station that stopped
    reporting has no "so far today"), when a metric has fewer than
    MIN_RECORD_DAYS measured days including today, and when nothing was
    beaten. A tie is not a record. One card at most: the biggest claim
    wins, an all-time record over a month-of-year one, then the larger
    margin relative to its own threshold.
    """
    rows = await ctx.daily()
    if not rows:
        return []
    today_iso = ctx.today.isoformat()
    today_row = rows[-1]
    if str(today_row.get("day")) != today_iso:
        return []

    candidates: list[_Record] = []
    for metric in _RECORD_METRICS:
        value = _record_reading(metric, today_row)
        if value is None:
            continue
        pool: list[tuple[date, float]] = []
        for r in rows[:-1]:
            v = _record_reading(metric, r)
            if v is None:
                continue
            try:
                pool.append((date.fromisoformat(str(r["day"])), v))
            except (TypeError, ValueError):
                continue
        if len(pool) + 1 < MIN_RECORD_DAYS:
            continue

        prior, prior_day = _prior_record(metric, pool)
        beat = ((value - prior) if metric.higher else (prior - value))
        if beat >= metric.margin:
            candidates.append(_Record(metric, "all_time", value, prior,
                                      prior_day, pool, None))
            continue
        # Month-of-year, temperatures only: "the hottest August day" is a
        # claim people make; "the windiest August gust" is not.
        if metric.key not in ("high", "low"):
            continue
        month_pool = [(d, v) for d, v in pool if d.month == ctx.today.month]
        if len(month_pool) < MIN_MONTH_RECORD_DAYS:
            continue
        m_prior, m_day = _prior_record(metric, month_pool)
        m_beat = ((value - m_prior) if metric.higher else (m_prior - value))
        if m_beat >= metric.margin:
            candidates.append(_Record(metric, "month", value, m_prior, m_day,
                                      month_pool, (prior, prior_day)))
    if not candidates:
        return []

    order = {m.key: i for i, m in enumerate(_RECORD_METRICS)}
    best = max(candidates,
               key=lambda c: ((1 if c.scope == "all_time" else 0),
                              c.margin / c.metric.margin,
                              -order[c.metric.key]))
    metric, u = best.metric, ctx.units
    month_name = _MONTHS[ctx.today.month - 1]
    shown, token, precision, text = _record_display(metric, best.value, u)
    old_shown, _, _, old_text = _record_display(metric, best.prior, u)
    of = len(best.pool) + 1
    nouns = f"{metric.noun}s"
    scoped_nouns = f"{month_name} {nouns}" if best.scope == "month" else nouns
    superlative = metric.adjective.lower()
    old_when = f"{_long_date(best.prior_day)} {best.prior_day.year}"

    if best.scope == "all_time":
        title = f"{metric.adjective} {metric.noun} on record"
        standing = f"the {superlative} of {of} {nouns} on record"
    else:
        title = f"{metric.adjective} {month_name} {metric.noun} on record"
        standing = f"the {superlative} of {of} {month_name} {nouns} on record"
    rank_line = f"{text} so far today, past {old_text} on {old_when}, {standing}"

    context = (f"Today's {metric.quantity} stands at {text}, past the "
               f"{old_text} measured here on {old_when}. That makes it "
               f"{standing}")
    if best.all_time is not None:
        at_value, at_day = best.all_time
        _, _, _, at_text = _record_display(metric, at_value, u)
        context += (f", though {at_text} on {_long_date(at_day)} "
                    f"{at_day.year} still holds the all-time mark")
    context += f", and it can only {metric.verb} before midnight."

    delta_shown, delta_text = _record_delta(metric, best.margin, u)
    comparison = Comparison(
        kind="vs_prior_record",
        label="vs the previous record",
        value=shown,
        baseline=old_shown,
        baseline_label=f"previous record · {old_when}",
        direction="above" if metric.higher else "below",
        delta=delta_shown if metric.higher else -delta_shown,
        # A percentage of a temperature depends on where the scale puts
        # zero. Gust and rain are ratios from a true zero and may carry one.
        delta_pct=(round(100 * best.margin / best.prior, 1)
                   if metric.key in ("gust", "rain") and best.prior > 0
                   else None),
        rank=1, of=of, rank_line=rank_line)

    stood = (ctx.today - best.prior_day).days
    years = round(len(best.pool) / 365.25, 1)
    supporting = [
        Stat(f"today_{metric.key}", f"{metric.quantity} so far today",
             shown, token, precision),
        Stat("previous_record", f"previous record · {old_when}",
             old_shown, token, precision),
        Stat("record_stood", "days the old record stood", stood, UNIT_DAYS),
        Stat("years_of_record", f"years of {scoped_nouns} on record"
             if best.scope == "month" else "years of record",
             years, UNIT_YEARS, 1),
    ]

    # The ranked list: today, then the days it beat, in order. The bar is
    # how far each sits past the station's MEDIAN for the metric, with
    # today as the full bar, so a record beaten by a hair draws as two
    # near-equal bars, which is what a hair means. Rain's floor is zero:
    # its median is a dry day almost everywhere.
    ranked = sorted(best.pool, key=lambda dv: (-dv[1] if metric.higher else dv[1],
                                               dv[0]))[:RECORD_ROWS - 1]
    values = sorted(v for _, v in best.pool)
    floor = (0.0 if metric.key == "rain"
             else values[len(values) // 2])
    span = (best.value - floor) if metric.higher else (floor - best.value)

    def bar(v: float) -> float:
        if span <= 0:
            return 1.0 if v == best.value else 0.0
        along = (v - floor) if metric.higher else (floor - v)
        return round(min(1.0, max(0.0, along / span)), 4)

    series = [{"key": "today", "label": "so far today", "score": 1.0,
               "value": shown, "unit": token, "precision": precision,
               "owned": True}]
    for d, v in ranked:
        v_shown, _, _, _ = _record_display(metric, v, u)
        series.append({"key": d.isoformat(),
                       "label": f"{_short_date(d)} {d.year}",
                       "score": bar(v), "value": v_shown, "unit": token,
                       "precision": precision, "owned": False})
    _, margin_text = _record_delta(metric, metric.margin, u)
    axis = ("share of today's total" if metric.key == "rain" else
            f"how far {'above' if metric.higher else 'below'} this "
            f"station's median {metric.quantity} · a full bar is today")

    score = (RECORD_SCORE_ALL_TIME if best.scope == "all_time"
             else RECORD_SCORE_MONTH)
    return [Story(
        id=f"records.record_broken.{today_iso}.{metric.key}.{best.scope}",
        family=FAMILY_RECORDS,
        story_type="record_broken",
        title=title,
        emoji="\U0001f3c6",
        hero=Stat(f"today_{metric.key}", f"{metric.quantity} so far today",
                  shown, token, precision),
        hero_line=f"{text.upper()} SO FAR TODAY",
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="chaos_dimensions",
                series=series,
                unit=token,
                axis_label=axis,
                footnote=(f"Today against the {len(ranked)} {scoped_nouns} "
                          f"it passed, out of {of} on record. A record has "
                          f"to clear the old mark by {margin_text} to count "
                          f"here; today's number is still moving."),
                highlight="today",
                highlight_key="today"),
        period=Period(kind="moment", label=f"{_long_date(ctx.today)} so far",
                      start=today_iso, end=today_iso, partial=True),
        station=ctx.station({"first_day": rows[0]["day"],
                             "last_day": rows[-1]["day"],
                             "day_count": len(rows)}),
        interestingness=score,
        score_parts={"scope": score, "margin_over_threshold":
                     round(best.margin / metric.margin, 4),
                     "years_of_record": years},
    )]


# ───────────────────────── Lightning Season ─────────────────────────
#
# THE CAPABILITY FILTER IS THE WHOLE PRODUCER. `lightning_max` is folded
# into daily_rollups from `lightning_last_1hr`, and that field leaks onto
# stations with no detector: a WS-2902 console posts it as 0, a relay
# forwards it, and the rollup dutifully records a day of "0 strikes/hr"
# that nobody measured. A station whose entire record is zeros has never
# detected a strike and is not a station where it never thunders, so this
# producer will not speak until the archive holds ONE day with a count
# above zero. From then on a measured zero is a real calm day.

# A trailing window rather than the calendar year: "lightning days this
# year" on January 9 is a card about nine days, and a season is what the
# reader is asking about. Ninety days is a season.
LIGHTNING_WINDOW_DAYS = 90
# Under this many lightning days there is a storm or two, not a season.
MIN_LIGHTNING_DAYS = 3
# Lightning days in the window that saturate the frequency score. Twenty
# in ninety is a monsoon; a station that sees more is not more interesting
# than one that sees exactly that.
LIGHTNING_BUSY_DAYS = 20
# The closest strike scores against this yardstick: a strike at the
# station is 1.0, one this far out (or beyond) is 0. Miles, native.
LIGHTNING_NEAR_MI = 25.0
# Busiest days on the chart.
LIGHTNING_ROWS = 6
UNIT_STRIKES_HR = "strikes/hr"


async def _closest_strike(ctx: StoryContext, since: date
                          ) -> tuple[float, date] | None:
    """(distance in miles, local day) of the closest strike since `since`,
    or None.

    The second place this module leaves the rollups, and like `current()`
    it does so without scanning the archive: one range on (mac, dateutc_ms)
    bounded to the window, over columns idx_obs_chart covers. Distance is
    not in the rollups at all, and folding it in would be a second copy of
    the one fact for which the interesting number is the SMALLEST (the
    history SELECT says the same over MIN(lightning_distance_mi)).

    Gated on `lightning_last_1hr > 0`, the same gate the app's lightning
    tile and chart apply: `lightning_distance_mi` is the LAST strike's
    distance and some sources keep reporting it long after the storm has
    gone, so a reading only counts as a strike position while the trailing
    hour actually held strikes. Runs only after the rollup-level capability
    filter has passed, so a detector-less station never pays for it.
    """
    from datetime import datetime as _dt, timezone as _tzu
    from . import db as dbmod
    tz = _station_tz()
    start_ms = int(_dt(since.year, since.month, since.day, tzinfo=tz)
                   .timestamp() * 1000)
    async with dbmod.connect() as conn:
        row = await (await conn.execute(
            "SELECT dateutc_ms, lightning_distance_mi FROM observations "
            "WHERE mac = ? AND dateutc_ms >= ? AND lightning_last_1hr > 0 "
            "AND lightning_distance_mi IS NOT NULL "
            "ORDER BY lightning_distance_mi ASC, dateutc_ms ASC LIMIT 1",
            (ctx.mac, start_ms))).fetchone()
    if not row:
        return None
    dist, ms = _num(row["lightning_distance_mi"]), _num(row["dateutc_ms"])
    if dist is None or ms is None or dist < 0:
        return None
    when = _dt.fromtimestamp(ms / 1000, tz=_tzu.utc).astimezone(tz).date()
    return dist, when


@producer(FAMILY_RECORDS, "lightning_season")
async def lightning_season(ctx: StoryContext) -> list[Story]:
    """The last ninety days of lightning, from a station that has actually
    detected some.

    Lightning days and the busiest of them from `daily_rollups.lightning_max`
    (each day's peak trailing-hour strike count; the rollups keep no daily
    total, so none is claimed), and the closest strike from one bounded
    observation read. Compared against the same ninety days a year earlier
    when the detector was reporting then too.

    DECLINES when no day in the whole archive ever counted a strike (the
    capability filter above), and under MIN_LIGHTNING_DAYS lightning days
    in the window.
    """
    from . import insights as _ins
    rows = await ctx.daily()
    if not rows:
        return []
    days: list[tuple[date, float | None]] = []
    for r in rows:
        try:
            days.append((date.fromisoformat(str(r["day"])),
                         _num(r.get("lightning_max"))))
        except (TypeError, ValueError):
            continue
    if not any(peak is not None and peak > 0 for _, peak in days):
        return []

    since = ctx.today - timedelta(days=LIGHTNING_WINDOW_DAYS - 1)
    measured = [(d, peak) for d, peak in days
                if since <= d <= ctx.today and peak is not None]
    strikes = [(d, peak) for d, peak in measured if peak > 0]
    if len(strikes) < MIN_LIGHTNING_DAYS:
        return []
    n = len(strikes)
    # Ties go to the LATER day: indistinguishable on the record, and the
    # more recent one is the storm people remember.
    busiest_day, busiest_peak = max(strikes, key=lambda t: (t[1], t[0]))
    closest = await _closest_strike(ctx, since)
    u = ctx.units

    # The same window one year back, only when the detector covered it
    # comparably. A year the detector was not yet installed is not a year
    # with no lightning.
    prior_since, prior_until = _shift_year(since, -1), _shift_year(ctx.today, -1)
    prior_measured = [(d, peak) for d, peak in days
                      if prior_since <= d <= prior_until and peak is not None]
    comparison: Comparison | None = None
    if _ins.comparable_to_date(len(prior_measured), len(measured)):
        prior_n = sum(1 for _, peak in prior_measured if peak > 0)
        delta = n - prior_n
        rank = 1 if n >= prior_n else 2
        comparison = Comparison(
            kind="prior_year_same_window",
            label=f"vs the same {LIGHTNING_WINDOW_DAYS} days a year earlier",
            value=n, baseline=prior_n,
            baseline_label=f"lightning days, {_range_label(prior_since, prior_until)}",
            direction=("level" if delta == 0
                       else "above" if delta > 0 else "below"),
            delta=delta,
            delta_pct=(round(100 * delta / prior_n, 1) if prior_n else None),
            rank=rank, of=2,
            rank_line=_rank_line(rank, 2, "comparable seasons", "busier"))

    window_label = f"the last {LIGHTNING_WINDOW_DAYS} days"
    context = (f"Lightning was detected here on {n} of {window_label}, "
               f"with the detector reporting on {len(measured)} of them. "
               f"The busiest was {_long_date(busiest_day)}, peaking at "
               f"{busiest_peak:.0f} strikes an hour")
    if closest is not None:
        dist, when = closest
        context += (f", and the closest strike came within "
                    f"{u.distance_amount(dist)} on {_long_date(when)}.")
    else:
        context += "."

    supporting = [
        Stat("busiest_day", f"busiest day · {_long_date(busiest_day)}",
             round(busiest_peak), UNIT_STRIKES_HR),
        Stat("days_reporting",
             f"days the detector reported in {window_label}",
             len(measured), UNIT_DAYS),
    ]
    parts = {"frequency": round(min(1.0, n / LIGHTNING_BUSY_DAYS), 4)}
    weights = [(0.60, parts["frequency"])]
    if closest is not None:
        dist, when = closest
        supporting.insert(1, Stat(
            "closest_strike", f"closest strike · {_long_date(when)}",
            round(u.distance_value(dist), 1), u.distance_token, 1))
        parts["proximity"] = round(1.0 - min(dist, LIGHTNING_NEAR_MI)
                                   / LIGHTNING_NEAR_MI, 4)
        weights.append((0.40, parts["proximity"]))
    # Renormalized over the parts present: a station whose source carries
    # no distance is scored on frequency alone, not on a proximity of zero.
    score = sum(w * v for w, v in weights) / sum(w for w, _ in weights)

    # The busiest days as bars, the busiest first. The window can cross
    # New Year, and then every label carries its year.
    with_year = since.year != ctx.today.year
    top = sorted(strikes, key=lambda t: (-t[1], -t[0].toordinal()))[:LIGHTNING_ROWS]
    series = [{"key": d.isoformat(),
               "label": (f"{_short_date(d)} {d.year}" if with_year
                         else _short_date(d)),
               "score": round(peak / busiest_peak, 4),
               "value": round(peak), "unit": UNIT_STRIKES_HR, "precision": 0,
               "owned": d == busiest_day}
              for d, peak in top]

    return [Story(
        id=f"records.lightning_season.{ctx.today.isoformat()}",
        family=FAMILY_RECORDS,
        story_type="lightning_season",
        title="Lightning Season",
        emoji="\U0001f329️",
        hero=Stat("lightning_days", f"days with lightning in {window_label}",
                  n, UNIT_DAYS),
        hero_line=f"{n} LIGHTNING DAYS IN {LIGHTNING_WINDOW_DAYS}",
        context=context,
        comparison=comparison,
        supporting=supporting,
        viz=Viz(kind="chaos_dimensions",
                series=series,
                unit=UNIT_STRIKES_HR,
                axis_label="peak strikes per hour · a full bar is the busiest day",
                footnote=(f"The {len(top)} busiest of {n} lightning days. "
                          f"Each bar is that day's peak trailing-hour strike "
                          f"count; the record keeps no daily total, so none "
                          f"is claimed."),
                highlight=busiest_day.isoformat(),
                highlight_key=busiest_day.isoformat()),
        period=Period(kind="spell", label=window_label,
                      start=since.isoformat(), end=ctx.today.isoformat(),
                      partial=True),
        station=ctx.station({"first_day": rows[0]["day"],
                             "last_day": rows[-1]["day"],
                             "day_count": len(rows)}),
        interestingness=round(min(1.0, max(0.0, score)), 4),
        score_parts=parts,
    )]
