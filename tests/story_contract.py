"""The story wire contract, checked on EVERY response the suite produces.

`GET /api/devices/{mac}/stories` is the one payload this repo cannot fix
after the fact: the app renders it straight onto a picture that leaves in
someone's group chat. The rules below each closed a bug that shipped or
nearly shipped, and until this file every one of them lived only in the
memory of whoever wrote the last producer:

  · interestingness is ONE 0..1 scale across producers (ranking is a sort
    on it; a producer that scored 1.4 would sit on top of every card);
  · a viz row is FLAT — scalar values only — because the app's `StoryJSON`
    decodes no arrays and no objects, so a nested cell is dropped silently
    and the grid it belonged to draws a hole;
  · every stat `value` is a finite number or None, never NaN (the app's
    `firstFinite` family exists because NaN once rendered as 0);
  · `period.kind` is one of `stories.PERIOD_KINDS` (the app names the span
    from it; "station" is not a span);
  · every string the card prints is a string, never a number the card
    would have to format itself.

conftest's `client` fixture wraps `stories.top_stories` with
`check_response`, so the ~400 story tests sweep this contract without
naming it. `test_stories.py::test_the_wire_contract_bites` proves the
checker fails on each violation — a checker that names a rule and passes
regardless is the trap this repo keeps stepping in.
"""
from __future__ import annotations

import math
from typing import Any

_SCALARS = (str, int, float, bool, type(None))


class ContractViolation(AssertionError):
    """Raised with the JSON path of the offending field."""


def _fail(path: str, why: str) -> None:
    raise ContractViolation(f"{path}: {why}")


def _number(path: str, v: Any, *, optional: bool) -> None:
    if v is None:
        if not optional:
            _fail(path, "missing")
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail(path, f"expected a number, got {type(v).__name__}")
    if not math.isfinite(v):
        _fail(path, f"non-finite number {v!r}; absent is None, never NaN")


def _text(path: str, v: Any, *, optional: bool) -> None:
    """A string the card will print or look up."""
    if v is None:
        if not optional:
            _fail(path, "missing")
        return
    if not isinstance(v, str):
        _fail(path, f"expected text, got {type(v).__name__} — the card "
                    "composes nothing, so the words must arrive as words")
    if not optional and not v.strip():
        _fail(path, "empty")


def _scalar(path: str, v: Any) -> None:
    if not isinstance(v, _SCALARS):
        _fail(path, f"nested {type(v).__name__} on the wire; StoryJSON "
                    "decodes scalars only, so the app drops this cell")
    if isinstance(v, float) and not math.isfinite(v):
        _fail(path, f"non-finite number {v!r}")


def check_stat(path: str, stat: Any) -> None:
    if not isinstance(stat, dict):
        _fail(path, f"expected a stat object, got {type(stat).__name__}")
    _text(f"{path}.key", stat.get("key"), optional=False)
    _text(f"{path}.label", stat.get("label"), optional=False)
    _number(f"{path}.value", stat.get("value"), optional=True)
    _text(f"{path}.unit", stat.get("unit"), optional=True)
    precision = stat.get("precision")
    if isinstance(precision, bool) or not isinstance(precision, int) \
            or precision < 0:
        _fail(f"{path}.precision", f"expected a whole number, got {precision!r}")


def check_viz(path: str, viz: Any) -> None:
    if not isinstance(viz, dict):
        _fail(path, f"expected a viz object, got {type(viz).__name__}")
    _text(f"{path}.kind", viz.get("kind"), optional=False)
    _text(f"{path}.unit", viz.get("unit"), optional=True)
    _text(f"{path}.axis_label", viz.get("axis_label"), optional=True)
    _text(f"{path}.footnote", viz.get("footnote"), optional=True)
    _text(f"{path}.highlight_key", viz.get("highlight_key"), optional=True)
    _scalar(f"{path}.highlight", viz.get("highlight"))
    _number(f"{path}.domain_max", viz.get("domain_max"), optional=True)
    series = viz.get("series")
    if not isinstance(series, list):
        _fail(f"{path}.series", "expected a list of rows")
    for i, row in enumerate(series):
        rp = f"{path}.series[{i}]"
        if not isinstance(row, dict):
            _fail(rp, f"expected a row object, got {type(row).__name__}")
        _text(f"{rp}.key", row.get("key"), optional=False)
        for k, v in row.items():
            _scalar(f"{rp}.{k}", v)
            if k in ("note", "label") and v is not None:
                _text(f"{rp}.{k}", v, optional=True)


def check_comparison(path: str, cmp: Any) -> None:
    if not isinstance(cmp, dict):
        _fail(path, f"expected a comparison object, got {type(cmp).__name__}")
    _text(f"{path}.kind", cmp.get("kind"), optional=False)
    _text(f"{path}.label", cmp.get("label"), optional=False)
    _number(f"{path}.value", cmp.get("value"), optional=False)
    _number(f"{path}.baseline", cmp.get("baseline"), optional=True)
    _text(f"{path}.baseline_label", cmp.get("baseline_label"), optional=False)
    if cmp.get("direction") not in ("above", "below", "level"):
        _fail(f"{path}.direction", f"got {cmp.get('direction')!r}")
    _number(f"{path}.delta", cmp.get("delta"), optional=True)
    _number(f"{path}.delta_pct", cmp.get("delta_pct"), optional=True)
    _text(f"{path}.rank_line", cmp.get("rank_line"), optional=True)


def check_story(path: str, s: Any) -> None:
    from app.stories import FAMILIES, PERIOD_KINDS
    if not isinstance(s, dict):
        _fail(path, f"expected a story object, got {type(s).__name__}")
    _text(f"{path}.id", s.get("id"), optional=False)
    if s.get("family") not in FAMILIES:
        _fail(f"{path}.family", f"got {s.get('family')!r}")
    _text(f"{path}.story_type", s.get("story_type"), optional=False)
    _text(f"{path}.title", s.get("title"), optional=False)
    _text(f"{path}.emoji", s.get("emoji"), optional=True)
    _text(f"{path}.hero_line", s.get("hero_line"), optional=False)
    _text(f"{path}.context", s.get("context"), optional=False)
    _text(f"{path}.disclaimer", s.get("disclaimer"), optional=True)
    check_stat(f"{path}.hero", s.get("hero"))
    supporting = s.get("supporting")
    if not isinstance(supporting, list):
        _fail(f"{path}.supporting", "expected a list")
    for i, st in enumerate(supporting):
        check_stat(f"{path}.supporting[{i}]", st)
    if s.get("comparison") is not None:
        check_comparison(f"{path}.comparison", s["comparison"])
    if s.get("viz") is not None:
        check_viz(f"{path}.viz", s["viz"])
    period = s.get("period")
    if not isinstance(period, dict):
        _fail(f"{path}.period", "missing")
    if period.get("kind") not in PERIOD_KINDS:
        _fail(f"{path}.period.kind", f"{period.get('kind')!r} is not in "
                                     f"stories.PERIOD_KINDS")
    _text(f"{path}.period.label", period.get("label"), optional=False)
    _text(f"{path}.period.start", period.get("start"), optional=True)
    _text(f"{path}.period.end", period.get("end"), optional=True)
    if not isinstance(period.get("partial"), bool):
        _fail(f"{path}.period.partial", "expected a bool")
    station = s.get("station")
    if not isinstance(station, dict):
        _fail(f"{path}.station", "missing")
    for k, v in station.items():
        _scalar(f"{path}.station.{k}", v)
    score = s.get("interestingness")
    _number(f"{path}.interestingness", score, optional=False)
    if not 0.0 <= score <= 1.0:
        _fail(f"{path}.interestingness", f"{score!r} is outside 0..1; the "
                                        "scale is shared across producers")
    parts = s.get("score_parts")
    if not isinstance(parts, dict):
        _fail(f"{path}.score_parts", "expected an object")
    for k, v in parts.items():
        _number(f"{path}.score_parts.{k}", v, optional=False)


def check_response(out: dict[str, Any]) -> dict[str, Any]:
    """Validate a `top_stories` payload and return it unchanged."""
    if not isinstance(out, dict):
        _fail("$", f"expected a response object, got {type(out).__name__}")
    for k in ("mac", "anchor_day"):
        _text(f"$.{k}", out.get(k), optional=False)
    if not isinstance(out.get("generated_ms"), int):
        _fail("$.generated_ms", "expected an int")
    for k in ("families", "declined"):
        v = out.get(k)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            _fail(f"$.{k}", "expected a list of names")
    if not isinstance(out.get("candidates"), int):
        _fail("$.candidates", "expected an int")
    stories = out.get("stories")
    if not isinstance(stories, list):
        _fail("$.stories", "expected a list")
    seen: set[str] = set()
    for i, s in enumerate(stories):
        check_story(f"$.stories[{i}]", s)
        if s["id"] in seen:
            _fail(f"$.stories[{i}].id", f"duplicate id {s['id']!r}")
        seen.add(s["id"])
    return out
