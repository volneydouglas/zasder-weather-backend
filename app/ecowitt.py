"""Ecowitt local ingest (1.9): the gateway POSTs straight to us on the
LAN — no vendor cloud in the loop.

Every Ecowitt gateway (GW1000→GW3000, HP consoles) has a "Customized"
upload: an application/x-www-form-urlencoded POST of their documented
"Ecowitt protocol" to a server/path you choose, as often as every 16 s.
Pointed at `/ingest/ecowitt`, that turns the whole Ecowitt/Fine Offset
sensor family into local, cloud-free sources. Units are US-native
throughout (°F, mph, inHg, inches) — the same convention this backend
stores, so values pass through unconverted.

This module is the pure transform: form dict → the normalized ingest
payload `_do_ingest` expects. The route lives in ingest.py.

Device identity: the Ecowitt protocol never includes the gateway's MAC —
only PASSKEY, an MD5 of it (32 hex chars, stable per device). The synth
id is "ECEC" + the first 8 PASSKEY chars: recognizably synthetic (the
LilyGO relay's "5D5D" pattern) and stable across reboots. Renaming this
scheme re-keys every Ecowitt device row — it is pinned by tests.

Rain: a station can carry BOTH rain sensors — a WS90 (haptic/piezo,
`*rain_piezo` keys) and a tipping gauge like the WH40 (`*rainin` keys).
Haptic rain is phantom-prone (wind shake, bumps — the Tempest lesson),
so per field the tipping gauge wins and piezo is only the fallback.

Batteries (the "per-sensor battery" commitment): Ecowitt's own wire
conventions are a zoo — binary flags where 1 = LOW (inverse of the
AWN/stored convention), voltages, and 0–5 levels. Everything is
normalized here to the stored convention (1 = ok, 0 = low) under key
names health_watch already understands; a convention we can't read is
silence, not a claim.
"""
from __future__ import annotations

from typing import Any

# ── battery conventions ─────────────────────────────────────────────────
# Binary flags: Ecowitt sends 1 = LOW (yes, inverted vs. AWN's stored
# 0 = low). WH65/WH24/WH25/WH26 sensor arrays + WH31 channels batt1-8.
_BATT_BINARY = ("wh65batt", "wh24batt", "wh25batt", "wh26batt",
                "batt1", "batt2", "batt3", "batt4",
                "batt5", "batt6", "batt7", "batt8")
# Levels 0–5 (WH57 lightning, WH41/WH43 PM2.5, WH55 leak channels): ≤1 is
# the vendor's "low".
_BATT_LEVEL = ("wh57batt", "pm25batt1", "pm25batt2",
               "pm25batt3", "pm25batt4",
               "leakbatt1", "leakbatt2", "leakbatt3", "leakbatt4")
# Voltages, with the community low-water marks: WS68/WS80/WS90 arrays run
# ~3.3 V (low under 2.4 — the supercap line the Davis relay uses too);
# WH40's newer firmware reports its AA cell (~1.6 V fresh, low under 1.2).
# WH51 soil probes + WN34 temp probes run a single AA (~1.6 V, low under
# 1.2) — unmapped before R11, so their sensors could die silently.
_BATT_VOLTS = {"wh68batt": 2.4, "wh80batt": 2.4, "wh90batt": 2.4,
               "wh40batt": 1.2,
               **{f"soilbatt{i}": 1.2 for i in range(1, 5)},
               **{f"tf_batt{i}": 1.2 for i in range(1, 5)}}


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _synth_mac(passkey: str) -> str | None:
    """"ECEC" + first 8 PASSKEY hex chars — 12 hex total, so ingest's
    _format_mac colonizes it like any real MAC. Pinned by tests: changing
    this orphans every Ecowitt device row's history."""
    pk = (passkey or "").strip()
    if len(pk) < 8:
        return None
    head = pk[:8]
    try:
        int(head, 16)
    except ValueError:
        return None
    return "ECEC" + head.upper()


def _timestamp_utc(dateutc: Any) -> str | None:
    """Ecowitt sends "YYYY-MM-DD HH:MM:SS" (UTC, space-separated) —
    _flatten wants ISO "T". Shape-check only; real parsing (and the
    clock-skew clamp) stays in _flatten."""
    if not isinstance(dateutc, str):
        return None
    s = dateutc.strip()
    if len(s) != 19 or s[10] != " ":
        return None
    return s.replace(" ", "T") + "Z"


# Vendor key → the AWN-native stored name, where AWN defines one (the
# 1.9 field survey: storage speaks one vocabulary; wh57 IS the lightning
# detector, WH41 channel 1 IS AWN's batt_25). Keys without a mapping
# keep their vendor name and ride data_json only.
_BATT_RENAME = {"wh57batt": "batt_lightning", "pm25batt1": "batt_25"}


def _batteries(form: dict[str, Any]) -> dict[str, float]:
    """Per-sensor battery flags, normalized to the stored convention
    (1 = ok, 0 = low), under AWN-native names where one exists."""
    out: dict[str, float] = {}
    for k in _BATT_BINARY:
        v = _f(form.get(k))
        if v in (0.0, 1.0):                 # 1 = LOW on the wire → invert
            out[k] = 0.0 if v == 1.0 else 1.0
    for k in _BATT_LEVEL:
        v = _f(form.get(k))
        if v is not None and 0.0 <= v <= 5.0:
            out[k] = 0.0 if v <= 1.0 else 1.0
    for k, low_v in _BATT_VOLTS.items():
        v = _f(form.get(k))
        # A binary-looking value on a voltage key is an older firmware
        # still sending the flag form — no real cell reads a flat 0 or 1 V.
        if v in (0.0, 1.0):
            out[k] = 0.0 if v == 1.0 else 1.0
        elif v is not None and v > 1.0:
            out[k] = 0.0 if v < low_v else 1.0
    return {_BATT_RENAME.get(k, k): v for k, v in out.items()}


# Channel sensors → AWN-native names (1.9 field survey). Ecowitt shares
# AWN's names for the WH31 T/H channels; soil / leak / leaf spell theirs
# the vendor way and map across.
_CHANNEL_MAP = {
    **{f"temp{i}f": f"temp{i}f" for i in range(1, 5)},
    **{f"humidity{i}": f"humidity{i}" for i in range(1, 5)},
    **{f"soilmoisture{i}": f"soilhum{i}" for i in range(1, 5)},
    # WN34 soil/water temperature probes report as tf_chN — without this
    # mapping the soiltemp1f-4f columns were unreachable from every 1.9
    # source and the readings vanished entirely (R11 V14). Channels 5-8
    # have no columns; they stay unmapped rather than silently aliased.
    **{f"tf_ch{i}": f"soiltemp{i}f" for i in range(1, 5)},
    **{f"leak_ch{i}": f"leak{i}" for i in range(1, 5)},
    **{f"leafwetness_ch{i}": f"leafwetness{i}" for i in range(1, 3)},
}


def _channels(form: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for src, dest in _CHANNEL_MAP.items():
        v = _f(form.get(src))
        if v is not None:
            out[dest] = v
    return out


def _rain(form: dict[str, Any]) -> dict[str, Any]:
    """Tipping gauge (`*rainin`) preferred per field; piezo (`*_piezo`)
    only fills holes. `hourly_in` maps from the RATE key — AWN's
    hourlyrainin semantic is in/hr, and Ecowitt's own hourlyrainin (a
    60-min accumulation) is the nearest fallback.

    WONTFIX (R11→R13 carried, decided 2026-08-28): reviewers keep
    proposing accumulation-first here, but the stored field's semantic IS
    a rate — AWN defines hourlyrainin as in/hr and the WeeWX bridge posts
    rainRate into the same slot. Flipping this one source to
    accumulation-first would make Ecowitt the odd station out across
    charts, peak-rate alerts, and storm summaries. Revisit only if the
    stored semantic itself ever changes."""
    def pick(*keys: str) -> float | None:
        for k in keys:
            v = _f(form.get(k))
            if v is not None:
                return v
        return None

    return {
        "hourly_in":  pick("rainratein", "hourlyrainin",
                           "rrain_piezo", "hrain_piezo"),
        "event_in":   pick("eventrainin", "erain_piezo"),
        "daily_in":   pick("dailyrainin", "drain_piezo"),
        "weekly_in":  pick("weeklyrainin", "wrain_piezo"),
        "monthly_in": pick("monthlyrainin", "mrain_piezo"),
        "yearly_in":  pick("yearlyrainin", "yrain_piezo"),
    }


# ── metric firmware (R11 V4) ────────────────────────────────────────────
# Newer gateways (GW1100/GW2000, HP consoles) post the METRIC key family
# when the console's unit settings are metric: tempc/windspeedkmh/
# baromrelhpa/rainratemm and friends. Reading only the imperial keys made
# a metric gateway 200 and store all-NULL rows forever — the worst failure
# mode for the largest Ecowitt audience. Each metric key converts into its
# imperial twin ONLY when the twin is absent, so an imperial gateway (or a
# mixed payload) is never overridden. The WS90 piezo family's metric
# spelling is unverified until real hardware arrives (GW3001, ~Sep 2026);
# the tipping-gauge family below covers the documented keys.
_METRIC_C_TO_F = {
    "tempc": "tempf", "tempinc": "tempinf",
    **{f"temp{i}c": f"temp{i}f" for i in range(1, 5)},
    **{f"tf_ch{i}c": f"tf_ch{i}" for i in range(1, 5)},
}
_METRIC_KMH_TO_MPH = {"windspeedkmh": "windspeedmph",
                      "windgustkmh": "windgustmph",
                      "maxdailygustkmh": "maxdailygust"}
_METRIC_HPA_TO_INHG = {"baromrelhpa": "baromrelin",
                       "baromabshpa": "baromabsin"}
_METRIC_MM_TO_IN = {
    "rainratemm": "rainratein",
    **{f"{p}rainmm": f"{p}rainin"
       for p in ("hourly", "event", "daily", "weekly", "monthly", "yearly")},
}


def _with_imperial(form: dict[str, Any]) -> dict[str, Any]:
    """The form dict plus imperial twins derived from any metric keys whose
    imperial form is absent. Never overwrites a present imperial key."""
    out = dict(form)

    def fill(mapping: dict[str, str], convert) -> None:
        for src, dest in mapping.items():
            if out.get(dest) is not None:
                continue
            v = _f(form.get(src))
            if v is not None:
                out[dest] = convert(v)

    fill(_METRIC_C_TO_F, lambda c: c * 9.0 / 5.0 + 32.0)
    fill(_METRIC_KMH_TO_MPH, lambda k: k / 1.609344)
    fill(_METRIC_HPA_TO_INHG, lambda h: h * 0.029529983071445)
    fill(_METRIC_MM_TO_IN, lambda m: m / 25.4)
    return out


def normalize(form: dict[str, Any]) -> dict[str, Any] | None:
    """Ecowitt form fields → the normalized ingest payload. None when the
    request can't be an Ecowitt post (no usable PASSKEY / dateutc) — the
    route turns that into a 400."""
    form = _with_imperial(form)
    mac = _synth_mac(str(form.get("PASSKEY") or ""))
    ts = _timestamp_utc(form.get("dateutc"))
    if mac is None or ts is None:
        return None

    device: dict[str, Any] = {"id": mac}
    model = str(form.get("model") or "").strip()
    if model:
        device["model"] = model
    # The outdoor array's own battery drives the battout flag the apps
    # already render. WS90/WS80/WS68 voltage arrays take precedence, then
    # the classic WH65-family flag.
    batteries = _batteries(form)
    for k in ("wh90batt", "wh80batt", "wh68batt", "wh65batt", "wh24batt"):
        if k in batteries:
            device["battery_outdoor"] = "normal" if batteries[k] else "low"
            break

    outdoor = {"tempf": _f(form.get("tempf")),
               "humidity": _f(form.get("humidity")),
               "uv": _f(form.get("uv")),
               "solar_wm2": _f(form.get("solarradiation"))}
    indoor = {"tempf": _f(form.get("tempinf")),
              "humidity": _f(form.get("humidityin"))}
    wind = {"speed_mph": _f(form.get("windspeedmph")),
            "gust_mph": _f(form.get("windgustmph")),
            # The gateway's true running daily peak — without it the stored
            # maxdailygust fell back to the instantaneous gust (R11).
            "max_daily_gust_mph": _f(form.get("maxdailygust")),
            "direction": _f(form.get("winddir"))}
    pressure = {"relative_inhg": _f(form.get("baromrelin")),
                "absolute_inhg": _f(form.get("baromabsin"))}

    return {
        "device": device,
        "source": "ecowitt",
        "timestamp_utc": ts,
        "outdoor": outdoor,
        "indoor": indoor,
        "wind": wind,
        "rain": _rain(form),
        "pressure": pressure,
        "batteries": batteries,
        "extra": _channels(form),
    }
