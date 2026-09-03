"""Story engine (2.0): rendering a whole story in the reader's units.

The defect this suite exists for, found while building the card
templates: EVERY string a card shows is written server-side — that is the
design rule, because ImageRenderer cannot take env objects off-tree — and
those strings bake the unit into the words. "108 DAYS ≥100°F". A reader set
to Celsius received a card that was entirely Fahrenheit, and the client
could not fix it: converting the numbers alone would have put "44°C" beside
"≥100°F" in one picture, which is worse than leaving it. The whole card is
generated in the reader's units or it is not generated.

So the request carries the preference and the producers format through it.
The invariant underneath is the repo's oldest: values are STORED
API-native, every threshold and every comparison happens on the stored
value, and conversion is the LAST thing that happens before a number
becomes words. A constant compared against a converted value is the bug
this project keeps re-shipping, and this is exactly where it would happen.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import stories  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "AA:BB:CC:00:00:2C"
TODAY = date(2026, 8, 30)
FIRST = date(2024, 9, 1)

METRIC = stories.Units(temperature="celsius", wind="kph", rain="mm",
                       pressure="hPa")

# Rain days, so the record carries real dry spells AND a wet season.
_RAIN_DOY = {2024: (250, 264, 271, 278, 300, 320, 350),
             2025: (12, 26, 44, 60, 74, 92, 205, 219, 226, 233, 241, 305, 355),
             2026: (14, 28, 46, 62, 76, 94, 199, 206, 213, 220, 227, 234)}
_DEW_BY_MONTH = {1: 30.0, 2: 32.0, 3: 34.0, 4: 33.0, 5: 32.0, 6: 40.0,
                 7: 62.0, 8: 66.0, 9: 55.0, 10: 45.0, 11: 38.0, 12: 32.0}
_WOBBLE = (9.0, -5.0, 4.0, 7.0, -3.0, 2.0, 6.0)
_SHAPES = (0.02, 0.31, 0.11, 0.64, 0.07, 0.45, 0.22, 0.88, 0.05, 0.37,
           0.16, 0.53, 0.09, 0.71, 0.27, 0.13, 0.42, 0.03, 0.59, 0.19,
           0.34, 0.08, 0.48, 0.24, 0.95, 0.12, 0.40, 0.06, 0.66)


def _rows() -> list[dict]:
    """A station busy enough that all four producers have something to say —
    a heat ledger, a wild day, a dry spell and a wet season."""
    rain_on = {(date(y, 1, 1) + timedelta(days=d - 1)).isoformat()
               for y, ds in _RAIN_DOY.items() for d in ds}
    rows: list[dict] = []
    d, i = FIRST, 0
    while d <= TODAY:
        doy = d.timetuple().tm_yday
        hi = 70.0 + 38.0 * (1 - abs(doy - 200) / 200.0) + 6 * _SHAPES[i % 29]
        rows.append({
            "day": d.isoformat(),
            "hi": round(hi, 1),
            "lo": round(hi - (18.0 + 16.0 * _SHAPES[(i * 7 + 7) % 29]), 1),
            "gust": round(8.0 + 44.0 * _SHAPES[(i * 11 + 11) % 29] ** 2, 1),
            "p_lo": 29.78,
            "p_hi": round(29.85 + 0.30 * _SHAPES[(i * 13 + 13) % 29] ** 2, 3),
            "dew": round(_DEW_BY_MONTH[d.month] + _WOBBLE[i % 7]
                         + 3 * _SHAPES[(i * 17 + 17) % 29], 1),
            "rain": (round(0.55 * _SHAPES[(i * 19 + 19) % 29], 2)
                     if d.isoformat() in rain_on else 0.0),
        })
        d += timedelta(days=1)
        i += 1
    return rows


def _seed(db, rows: list[dict], mac: str = MAC) -> None:
    async def run():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE mac = ?",
                               (mac,))
            for r in rows:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups "
                    "(mac, day, tempf_min, tempf_max, tempf_sum, tempf_n, "
                    " windgustmph_max, baromrelin_min, baromrelin_max, "
                    " dew_point_min, dew_point_max, rain_total) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mac, r["day"], r["lo"], r["hi"], r["hi"], 1, r["gust"],
                     r["p_lo"], r["p_hi"], r["dew"] - 8, r["dew"], r["rain"]))
            await conn.commit()
    asyncio.run(run())


@pytest.fixture()
def engine(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows())
    return db


def _ranked(units: stories.Units | None = None) -> dict:
    kw = {"units": units} if units is not None else {}
    return asyncio.run(stories.top_stories(MAC, limit=12, **kw))


def _strings(story: dict) -> list[str]:
    """EVERY rendered string in one story — the whole surface a card can
    draw. A leak test that misses a field is a leak test that passes."""
    out = [story["title"], story["hero_line"], story["context"],
           story["hero"]["label"], story["period"]["label"],
           story["station"]["name"] or "", story["disclaimer"] or ""]
    out += [x["label"] for x in story["supporting"]]
    if story["comparison"]:
        c = story["comparison"]
        out += [c["label"], c["baseline_label"], c["rank_line"] or ""]
    if story["viz"]:
        out.append(story["viz"]["axis_label"] or "")
        for entry in story["viz"]["series"]:
            out += [str(v) for k, v in entry.items()
                    if k in {"label", "band"} and isinstance(v, str)]
    return out


def _units_used(story: dict) -> set[str]:
    out = {x["unit"] for x in story["supporting"] if x["unit"]}
    if story["hero"]["unit"]:
        out.add(story["hero"]["unit"])
    if story["viz"]:
        if story["viz"]["unit"]:
            out.add(story["viz"]["unit"])
        out |= {e["unit"] for e in story["viz"]["series"]
                if isinstance(e.get("unit"), str)}
    return out


# ───────────────── the defect, closed ─────────────────

def test_a_celsius_render_never_leaks_a_fahrenheit_string(engine):
    """The whole point. Every producer, every rendered field, every viz
    label: not one degree Fahrenheit survives, and no stat still claims to
    be in a unit the card did not draw."""
    assert engine is not None
    ranked = _ranked(METRIC)
    assert len(ranked["stories"]) >= 4, "the fixture must exercise them all"
    assert {s["story_type"] for s in ranked["stories"]} >= {
        "heat_ledger", "wildest_day", "dry_spell", "humid_month"}

    for s in ranked["stories"]:
        for text in _strings(s):
            assert "°F" not in text, (s["story_type"], text)
            assert "inHg" not in text, (s["story_type"], text)
            assert "mph" not in text, (s["story_type"], text)
        assert "F" not in _units_used(s), s["story_type"]
        # "days" and "nights" are COUNTS. They have no imperial/metric axis
        # to convert along, so they are identical in every scale and belong
        # on both allow-lists — the same reasoning UNIT_MIN and UNIT_FT ride
        # on in the producer module.
        # "degree days" joins them: the MAGNITUDE converts by scale (it is
        # a sum of differences) but the unit's NAME is the same in every
        # scale — the base it is measured from is what changes, and that is
        # carried in the prose, which the string sweep above already checks.
        assert _units_used(s) <= {"C", "km/h", "mm", "mm/hr", "hPa",
                                  "days", "nights", "degree days"}, (
            s["story_type"], _units_used(s))


def test_the_native_render_is_unchanged_by_the_new_parameter(engine):
    """Absent preference means exactly what it meant before this existed.
    Byte-for-byte, so nothing that already shipped moves."""
    assert engine is not None
    def stable(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if k != "generated_ms"}
    assert stable(_ranked()) == stable(_ranked(stories.UNITS_NATIVE))
    for s in _ranked()["stories"]:
        assert _units_used(s) <= {"F", "mph", "in", "in/hr", "inHg",
                                  "days", "nights", "degree days"}


def test_the_same_weather_in_two_scales_tells_the_same_story(engine):
    """Only the words and the numbers move. The engine must pick the same
    day, rank it the same way and score it the same — a story that changed
    its mind about what was interesting when the reader switched to Celsius
    would mean a threshold was being compared against a converted value."""
    assert engine is not None
    native = {s["id"]: s for s in _ranked()["stories"]}
    metric = {s["id"]: s for s in _ranked(METRIC)["stories"]}
    assert list(native) == list(metric)
    for sid, a in native.items():
        b = metric[sid]
        assert a["interestingness"] == b["interestingness"], sid
        assert a["score_parts"] == b["score_parts"], sid
        assert a["period"] == b["period"], sid
        assert a["viz"]["highlight_key"] == b["viz"]["highlight_key"], sid


def test_the_payload_says_which_scale_it_was_rendered_in(engine):
    assert engine is not None
    assert _ranked()["units"] == {"temperature": "fahrenheit", "wind": "mph",
                                 "rain": "inches", "pressure": "inHg"}
    assert _ranked(METRIC)["units"] == {"temperature": "celsius",
                                        "wind": "kph", "rain": "mm",
                                        "pressure": "hPa"}


# ───────────────── the conversions themselves ─────────────────

def test_a_reading_takes_the_offset_and_a_departure_takes_the_scale():
    """The trap that would have shipped silently. A +12.4°F departure from
    normal is +6.9°C, not −10.9°C — running a DIFFERENCE through the reading
    conversion applies the 32° offset and turns an anomaly into a
    temperature. The app's own TempUnit.delta exists for this; so does
    Units.temp_delta."""
    c = stories.Units(temperature="celsius")
    assert c.temp(212.0) == pytest.approx(100.0)
    assert c.temp(32.0) == pytest.approx(0.0)
    assert c.temp_delta(12.4) == pytest.approx(6.888, abs=1e-3)
    assert c.temp_delta(12.4) != pytest.approx(c.temp(12.4))
    assert c.temp_deg(100.0) == "37.8°C"
    assert c.temp_delta_deg(45.0) == "25°C"

    f = stories.UNITS_NATIVE
    assert f.temp(100.0) == 100.0 and f.temp_delta(12.4) == 12.4
    assert f.temp_deg(100.0) == "100°F"


def test_the_anomaly_label_shows_a_departure_not_a_temperature(engine):
    """The one label that carries both at once: the reading as the value and
    the departure in the words. In Celsius they must convert differently."""
    assert engine is not None
    def anomaly(units):
        wild = next(s for s in _ranked(units)["stories"]
                    if s["story_type"] == "wildest_day")
        return next(x for x in wild["supporting"] if x["key"] == "anomaly")

    native, metric = anomaly(None), anomaly(METRIC)
    assert native["unit"] == "F" and metric["unit"] == "C"
    # The VALUE is a reading: offset conversion.
    assert metric["value"] == pytest.approx(
        (native["value"] - 32) * 5 / 9, abs=0.06)
    # The DEPARTURE in the label is a difference: scale conversion, so it
    # stays positive and stays small.
    away_f = float(native["label"].split("· ")[1].split("°F")[0])
    away_c = float(metric["label"].split("· ")[1].split("°C")[0])
    assert away_c == pytest.approx(away_f * 5 / 9, abs=0.06)
    assert away_c > 0


def test_a_range_is_a_difference_too(engine):
    """Temperature swing and pressure swing are RANGES — how far the number
    travelled. A 45°F swing is a 25°C swing, never a 7.2°C one."""
    assert engine is not None
    def stat(units, key):
        wild = next(s for s in _ranked(units)["stories"]
                    if s["story_type"] == "wildest_day")
        return next(x for x in wild["supporting"] if x["key"] == key)

    swing_f, swing_c = stat(None, "swing"), stat(METRIC, "swing")
    assert swing_c["value"] == pytest.approx(swing_f["value"] * 5 / 9, abs=0.06)
    assert swing_c["unit"] == "C"

    p_in, p_hpa = stat(None, "pressure"), stat(METRIC, "pressure")
    assert p_hpa["value"] == pytest.approx(p_in["value"] * 33.8639, abs=0.06)
    assert p_hpa["unit"] == "hPa"


def test_wind_covers_every_scale_the_app_offers():
    for name, expected in (("mph", 40.0), ("kph", 64.37), ("ms", 17.88),
                           ("knots", 34.76)):
        u = stories.Units(wind=name)
        assert u.wind_value(40.0) == pytest.approx(expected, abs=0.01)
    # Continuous (unrounded) Beaufort, exactly like the app's WindUnit —
    # charts and round-trips stay honest and display rounds. 40 mph sits
    # inside force 8 (39–46 mph).
    bft = stories.Units(wind="beaufort")
    assert bft.wind_value(40.0) == pytest.approx(7.71, abs=0.02)
    assert bft.wind_token == "Bft"
    # Capped at force 12, the top of the standard scale — a hurricane gust
    # must not print "20 Bft" the way an uncapped renderer once did.
    assert bft.wind_value(400.0) == 12.0
    assert bft.wind_value(0.0) == 0.0


def test_the_rain_day_definition_survives_the_millimetre(engine):
    """0.01 in is 0.25 mm, and the card STATES its own threshold. An integer
    millimetre would print it as "0 mm" — the definition sentence declaring
    that a dry day is a day with less than no rain."""
    assert engine is not None
    mm = stories.Units(rain="mm")
    assert mm.rain_amount(0.01) == "0.3 mm"
    assert stories.UNITS_NATIVE.rain_amount(0.01) == "0.01 in"
    dry = next(s for s in _ranked(METRIC)["stories"]
               if s["story_type"] == "dry_spell")
    assert "0.3 mm" in dry["context"]
    assert " in of rain" not in dry["context"]


def test_dew_point_bands_convert_their_edges_but_not_their_membership(engine):
    """A day is banded on the STORED Fahrenheit reading; only the printed
    edge converts. Banding a Celsius number against a Fahrenheit edge is the
    exact bug this repo keeps re-shipping, and it would move days between
    bands rather than merely mislabel them."""
    assert engine is not None
    def bands(units):
        s = next(x for x in _ranked(units)["stories"]
                 if x["story_type"] == "dew_point_bands")
        return {b["band"]: b for b in s["viz"]["series"]}

    native, metric = bands(None), bands(METRIC)
    assert [b["days"] for b in native.values()] == [
        b["days"] for b in metric.values()]
    assert native["humid"]["min"] == 65.0
    assert metric["humid"]["min"] == pytest.approx(18.3, abs=0.05)
    assert metric["humid"]["label"] == "humid · 18.3–21.1°C"
    assert metric["very dry"]["label"] == "very dry · under 10°C"
    assert metric["very humid"]["label"] == "very humid · 21.1°C and up"


# ───────────────── rank lines ─────────────────

def test_every_computed_rank_arrives_as_a_finished_sentence(engine):
    """"#1 of 973 days" is one of the most compelling things this engine
    knows, and the card had to drop it because composing that line
    client-side is what the plain-inputs rule forbids."""
    assert engine is not None
    ranked = _ranked()
    seen = 0
    for s in ranked["stories"]:
        c = s["comparison"]
        if not c or not c.get("rank"):
            continue
        seen += 1
        assert c["rank_line"], s["story_type"]
        assert str(c["of"]) in c["rank_line"]
    assert seen >= 2, "the fixture must produce ranked comparisons"

    wild = next(s for s in ranked["stories"]
                if s["story_type"] == "wildest_day")
    line = wild["comparison"]["rank_line"]
    assert line.endswith("days this station has recorded")
    assert line.startswith("the wildest of") or line[0].isdigit()


def test_rank_lines_read_like_english():
    r = stories._rank_line
    assert r(1, 3, "comparable years", "longest") == "the longest of 3 comparable years"
    assert r(2, 3, "comparable years", "most") == "2nd of 3 comparable years"
    assert r(3, 9, "days", "wildest") == "3rd of 9 days"
    assert r(4, 9, "days", "wildest") == "4th of 9 days"
    # The teens are where every naive ordinal breaks, and this ships onto a
    # picture.
    assert r(11, 99, "days", "wildest") == "11th of 99 days"
    assert r(12, 99, "days", "wildest") == "12th of 99 days"
    assert r(13, 99, "days", "wildest") == "13th of 99 days"
    assert r(21, 99, "days", "wildest") == "21st of 99 days"
    assert r(112, 999, "days", "wildest") == "112th of 999 days"
    # Nothing to rank against is not a rank. "1 of 1" would be the card
    # congratulating the station for being the only entrant.
    assert r(1, 1, "days", "wildest") is None
    assert r(None, None, "days", "wildest") is None


# ───────────────── viz contract ─────────────────

def test_every_series_entry_carries_a_key_and_the_highlight_names_one(engine):
    """`highlight` is polymorphic across templates for historical reasons —
    a number for the pyramid, a string for the chaos bars. `highlight_key`
    is one spelling every template can use, and the keys are anchored to
    stored values so they do not move when the reader switches scale."""
    assert engine is not None
    for units in (None, METRIC):
        for s in _ranked(units)["stories"]:
            viz = s["viz"]
            if not viz:
                continue
            keys = [e["key"] for e in viz["series"]]
            assert all(isinstance(k, str) for k in keys), s["story_type"]
            assert len(set(keys)) == len(keys), s["story_type"]
            assert viz["highlight_key"] in keys, s["story_type"]


def test_the_pyramid_stopped_shipping_a_share_nothing_renders(engine):
    """The context sentence already says "nearly one in every two days",
    which is the same fact in a form somebody would read out loud."""
    assert engine is not None
    heat = next(s for s in _ranked()["stories"]
                if s["story_type"] == "heat_ledger")
    assert all("share" not in b for b in heat["viz"]["series"])
    assert "one in every" in heat["context"]


# ───────────────── the endpoint ─────────────────

def test_endpoint_takes_the_units_the_app_already_speaks(client, monkeypatch):
    from app import climate, db
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(climate, "local_today", lambda: TODAY)
    _seed(db, _rows())

    r = client.get(f"/api/devices/{MAC}/stories?limit=12&temp_unit=celsius"
                   "&wind_unit=kph&rain_unit=mm&pressure_unit=hPa", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["units"]["temperature"] == "celsius"
    for s in body["stories"]:
        for text in _strings(s):
            assert "°F" not in text

    # Omitted parameters keep the shipped rendering.
    plain = client.get(f"/api/devices/{MAC}/stories?limit=12", headers=H)
    assert plain.json()["units"]["temperature"] == "fahrenheit"
    heat = next(s for s in plain.json()["stories"]
                if s["story_type"] == "heat_ledger")
    assert "°F" in heat["hero_line"]


def test_endpoint_rejects_a_unit_it_cannot_render(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    for bad in ("temp_unit=kelvin", "wind_unit=furlongs", "rain_unit=cubits",
                "pressure_unit=atm"):
        r = client.get(f"/api/devices/{MAC}/stories?{bad}", headers=H)
        assert r.status_code == 400, bad
        assert bad.split("=")[0] in r.json()["detail"]


def test_parse_units_defaults_are_the_native_ones():
    assert stories.parse_units() == stories.UNITS_NATIVE
    assert stories.parse_units(temperature="celsius").wind == "mph"
    with pytest.raises(ValueError):
        stories.parse_units(rain="litres")
