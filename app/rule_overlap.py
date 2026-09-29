"""Rules that duplicate a built-in watch (2.5, C5).

Doren keeps 28 threshold rules, most written before the smart watches
existed. A "Temperature below 33" rule and the frost watch now both fire on
the same cold night, and the owner gets two pushes about one event. This
finds the rules that the alert history shows firing ALONGSIDE a matching
built-in watch, so the apps can offer "retire this rule". Nothing is
turned off here; the owner decides.

Evidence, not guesswork: a rule is suggested only when the alert log holds
at least MIN_TOGETHER occasions in the last WINDOW_DAYS where it fired
within PAIR_MS of a watch of a matching kind on the same station.

A rule row in `alert_log` carries no rule id, but its text is built by one
pure function (`alerts.build_threshold_message`) from the station name, the
field and the threshold, so a row is attributed to a rule by the exact
pieces that function writes: the title's field label and the body's
"(< 33°F)". A rule whose threshold was edited since is matched on its
current threshold only, which undercounts rather than guessing.
"""
from __future__ import annotations

from typing import Any

from . import db

WINDOW_DAYS = 90
PAIR_MS = 2 * 3_600_000
MIN_TOGETHER = 2

# Which built-in watch a rule duplicates, by field and direction. The
# thresholds only gate the obvious: a "temperature below 33" rule is a frost
# rule; "below 70" is somebody's comfort rule and is left alone.
_LABEL_OF = {
    "frost": "Frost watch", "pipe_freeze": "Pipe freeze watch",
    "first_frost": "First frost", "heat": "Heat watch",
    "pressure_drop": "Pressure drop watch", "wind_ramp": "Wind ramp watch",
    "temp_drop": "Temperature drop watch", "rain_start": "Rain starting",
    "storm": "Storm summary",
}


def overlapping_kinds(field: str, comparator: str, threshold: float) -> set[str]:
    """Pure: the built-in watch kinds a rule would duplicate."""
    if field in ("tempf", "feelsLike"):
        if comparator == "below" and threshold <= 40:
            return {"frost", "pipe_freeze", "first_frost"}
        if comparator == "above" and threshold >= 95:
            return {"heat"}
    if field == "baromrelin" and comparator == "below":
        return {"pressure_drop"}
    if field in ("windgustmph", "windspeedmph") and comparator == "above":
        return {"wind_ramp"}
    if field in ("hourlyrainin", "dailyrainin") and comparator == "above":
        return {"rain_start", "storm"}
    return set()


def _marker(rule: dict[str, Any]) -> tuple[str, str]:
    """The (title suffix, body fragment) build_threshold_message writes for
    this rule, whatever the reading was."""
    from .alerts import _COMPARATOR_SYM, _FIELD_LABELS, _FIELD_UNITS
    label = _FIELD_LABELS.get(rule["field"], rule["field"])
    unit = _FIELD_UNITS.get(rule["field"], "")
    sym = _COMPARATOR_SYM.get(rule["comparator"], rule["comparator"])
    return f": {label} alert", f"({sym} {float(rule['threshold']):g}{unit})"


# Which switch runs each watch (alerts.py's tick): the smart family and the
# seasonal first frost ride smart_alerts, the rest have their own.
_SMART = {"frost", "pipe_freeze", "first_frost", "heat", "pressure_drop",
          "wind_ramp", "temp_drop"}


def active_kinds(cfg: Any) -> set[str]:
    """The watch kinds that are switched on now. A rule is only ever
    suggested for retirement in favour of a watch that is still running
    (R25-A04, the 2.5 additional review: with smart alerts off the frost
    rule was offered for retirement and one tap left no frost alert)."""
    kinds: set[str] = set()
    if getattr(cfg, "smart_alerts", False):
        kinds |= _SMART
    if getattr(cfg, "rain_start", False):
        kinds.add("rain_start")
    if getattr(cfg, "storm_summary", False):
        kinds.add("storm")
    return kinds


async def find(now_ms: int, active: set[str] | None = None) -> list[dict[str, Any]]:
    """`active` is the set of watch kinds switched on; None reads it from
    the current alert config."""
    if active is None:
        from .alerts import effective_config
        active = active_kinds(await effective_config())
    since = now_ms - WINDOW_DAYS * 86_400_000
    rules = [r for r in await db.list_alert_rules(enabled_only=True)
             if overlapping_kinds(r["field"], r["comparator"], float(r["threshold"])) & active]
    if not rules:
        return []
    kinds = set().union(*(overlapping_kinds(r["field"], r["comparator"],
                                            float(r["threshold"])) & active for r in rules))
    async with db.connect() as conn:
        rule_rows = await (await conn.execute(
            "SELECT ts_ms, mac, title, body FROM alert_log "
            "WHERE kind = 'rule' AND ts_ms >= ?", (since,))).fetchall()
        marks = ",".join("?" for _ in kinds)
        watch_rows = await (await conn.execute(
            f"SELECT ts_ms, mac, kind FROM alert_log WHERE ts_ms >= ? "
            f"AND kind IN ({marks})", (since, *sorted(kinds)))).fetchall()
    # A station whose storm summary is muted has no storm watch running
    # there (the storm tick skips it), so its storm rows cannot justify
    # retiring that station's rain rule (Greptile, PR #52).
    prefs = await db.get_device_alert_prefs()
    watch_rows = [w for w in watch_rows
                  if not (w["kind"] == "storm"
                          and (prefs.get(w["mac"]) or {}).get("storm_summary") is False)]
    out = []
    for rule in rules:
        suffix, fragment = _marker(rule)
        want = overlapping_kinds(rule["field"], rule["comparator"],
                                 float(rule["threshold"])) & active
        fires = [r for r in rule_rows
                 if (r["title"] or "").endswith(suffix)
                 and fragment in (r["body"] or "")
                 and (rule.get("target_mac") in (None, "") or r["mac"] == rule["target_mac"])]
        together: dict[str, int] = {}
        last_ms = 0
        for f in fires:
            for w in watch_rows:
                if w["kind"] in want and w["mac"] == f["mac"] \
                        and abs(w["ts_ms"] - f["ts_ms"]) <= PAIR_MS:
                    together[w["kind"]] = together.get(w["kind"], 0) + 1
                    last_ms = max(last_ms, f["ts_ms"])
                    break
        if not together:
            continue
        kind, n = max(together.items(), key=lambda kv: kv[1])
        total = sum(together.values())
        if total < MIN_TOGETHER:
            continue
        out.append({"rule_id": rule["id"], "watch_kind": kind,
                    "watch_label": _LABEL_OF.get(kind, kind),
                    "together": total, "rule_fires": len(fires),
                    "last_ms": last_ms, "window_days": WINDOW_DAYS})
    out.sort(key=lambda o: -o["together"])
    return out
