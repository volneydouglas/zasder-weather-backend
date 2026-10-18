"""The stable public API contract (2.7).

Third-party clients build on a small set of endpoints documented in
API.md. The promise is additive only: within a major version a stable
field is never removed, renamed or given a different JSON type. The
fields and their types live in tests/fixtures/stable_api.json; this file
seeds a database, calls every stable endpoint with the owner token AND a
read-only guest token, and fails when a listed field is missing, null
where the contract says it cannot be, or of another type.

Every listed field must also be SEEN with a real value at least once
across the probes. Without that rule an optional field (a sensor a
station may not have) could be renamed and the check would shrug,
because "absent" is allowed for it.

A failure here means a change broke clients the maintainers cannot see.
Do not edit the contract to make the test pass: put the old field back,
and add the new one beside it.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

H = {"Authorization": "Bearer test-api-token"}
INGEST = {"Authorization": "Bearer test-ingest-token"}
CONTRACT_PATH = Path(__file__).parent / "fixtures" / "stable_api.json"
# backend/API.md here; API.md at the root of the public mirror. Either
# way it is the directory above tests/.
API_DOC_PATH = Path(__file__).parent.parent / "API.md"

STATION = "AA:BB:CC:DD:EE:01"
LIFETIME = "12:34:56:78:9A:BC"     # posts a lifetime rain counter only

BANNER = """
========================================================================
PUBLIC API CONTRACT BROKEN

{route} no longer matches tests/fixtures/stable_api.json. That file is
the promise API.md makes to third-party developers: within a major
version a stable field is never removed, renamed or given another type.
Their apps break when it is, and nobody here will see it happen.

Do not change the contract file to make this pass. Keep the old field
as it was and ADD the new one beside it. Adding fields is always fine.
(A deliberate break belongs to a new major version, decided by the
maintainer, with API.md updated in the same change.)

{problems}
========================================================================
"""


def _contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text())


# ── the checker ───────────────────────────────────────────────────────

_PY_TYPES = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    for name in ("boolean", "integer", "number", "string", "object", "array"):
        if _PY_TYPES[name](v):
            return name
    return type(v).__name__


def _resolve(spec: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """A spec with its `ref` and `extends` folded in. The referring
    spec's own nullable/optional win over the definition's."""
    if "ref" in spec:
        base = _resolve(defs[spec["ref"]], defs)
        spec = {**base, **{k: v for k, v in spec.items() if k != "ref"}}
    if "extends" in spec:
        base = _resolve(defs[spec["extends"]], defs)
        fields = {**base.get("fields", {}), **spec.get("fields", {})}
        spec = {**base, **{k: v for k, v in spec.items() if k != "extends"},
                "fields": fields}
    return spec


def _declared_paths(spec: dict[str, Any], defs: dict[str, Any],
                    path: str = "") -> list[str]:
    spec = _resolve(spec, defs)
    out = [path or "(body)"]
    if spec["type"] == "object":
        for name, fs in spec.get("fields", {}).items():
            out += _declared_paths(fs, defs, f"{path}.{name}" if path else name)
    elif spec["type"] == "array" and "items" in spec:
        out += _declared_paths(spec["items"], defs, f"{path}[]")
    return out


def _check(value: Any, spec: dict[str, Any], defs: dict[str, Any],
           path: str, errors: list[str], seen: set[str]) -> None:
    spec = _resolve(spec, defs)
    where = path or "(body)"
    if value is None:
        if not spec.get("nullable"):
            errors.append(f"  {where}: is null; the contract says "
                          f"{spec['type']} and never null")
        return
    if not _PY_TYPES[spec["type"]](value):
        errors.append(f"  {where}: is {_json_type(value)}; the contract "
                      f"says {spec['type']}")
        return
    seen.add(where)
    if spec["type"] == "object":
        for name, fs in spec.get("fields", {}).items():
            sub = f"{path}.{name}" if path else name
            if name not in value:
                if not _resolve(fs, defs).get("optional"):
                    errors.append(f"  {sub}: is missing (a required stable "
                                  f"field; removed or renamed?)")
                continue
            _check(value[name], fs, defs, sub, errors, seen)
    elif spec["type"] == "array" and "items" in spec:
        for item in value:
            _check(item, spec["items"], defs, f"{path}[]", errors, seen)


def _assert_contract(route: str, bodies: list[tuple[str, Any]]) -> None:
    contract = _contract()
    defs = contract["definitions"]
    spec = contract["endpoints"][route]["response"]
    problems: list[str] = []
    seen: set[str] = set()
    for label, body in bodies:
        errors: list[str] = []
        _check(body, spec, defs, "", errors, seen)
        if errors:
            problems.append(f"{label}:\n" + "\n".join(sorted(set(errors))))
    never = [p for p in _declared_paths(spec, defs) if p not in seen]
    if never and not problems:
        problems.append(
            "never seen with a value in any seeded response (renamed? or "
            "the seed in tests/test_stable_api.py needs to cover it):\n"
            + "\n".join(f"  {p}" for p in never))
    if problems:
        pytest.fail(BANNER.format(route=route, problems="\n\n".join(problems)),
                    pytrace=False)


# ── seeding ───────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _post(client, body: dict[str, Any]) -> None:
    r = client.post("/ingest/custom", headers=INGEST, json=body)
    assert r.status_code == 200, r.text
    assert not r.json().get("quarantined"), r.text


def _seed(client) -> str:
    """Two stations, a rename, two alerts and a release check. Returns a
    read-only guest token."""
    now = datetime.now(timezone.utc)
    for i in range(4):
        _post(client, {
            "device": {"id": STATION.replace(":", ""), "name": "Back yard"},
            "coords": {"location": "Home", "lat": 33.3004, "lon": -111.9378},
            "timestamp_utc": _iso(now - timedelta(minutes=10 * (3 - i))),
            "outdoor": {"tempf": 70.0 + i, "humidity": 40, "dew_point_f": 45.0,
                        "uv": 3, "solar_wm2": 400.5},
            "indoor": {"tempf": 72.0, "humidity": 35},
            "pressure": {"relative_inhg": 29.92, "absolute_inhg": 28.71},
            "wind": {"speed_mph": 5.0, "gust_mph": 9.0, "direction": 180},
            "rain": {"hourly_in": 0.0, "event_in": 0.1, "daily_in": 0.1,
                     "weekly_in": 0.2, "monthly_in": 0.5, "yearly_in": 3.2},
            "lightning": {"strike_count": 1, "strike_count_last_1hr": 2,
                          "last_distance_mi": 6.0},
            "air": {"pm25": 8.0, "pm10": 12.0, "co2": 420},
        })
    for i in range(2):
        _post(client, {
            "device": {"id": LIFETIME.replace(":", ""), "name": "Gauge"},
            "timestamp_utc": _iso(now - timedelta(minutes=20 * (1 - i))),
            "outdoor": {"tempf": 69.0},
            "rain": {"yearly_in": 41.5 + i * 0.01},
        })
    r = client.put(f"/api/devices/{STATION}/name", headers=H,
                   json={"name": "Garden"})
    assert r.status_code == 200, r.text

    from app import db
    from app.main import app
    ts = int(time.time() * 1000)
    asyncio.run(db.log_alert(ts - 60_000, "device_down", STATION,
                             "Back yard is quiet", "No data for 30 min",
                             True, severity="watch"))
    asyncio.run(db.log_alert(ts, "rule", STATION, "Hot", "tempf above 70",
                             False, severity="info", rule_id=7))
    asyncio.run(db.log_alert(ts + 1, "digest", None, "Morning report",
                             "Yesterday 104/79", True, severity="info",
                             report_id=31))
    # What the daily release check leaves behind once it has run.
    app.state.update_info = {**app.state.update_info, "latest": "9.9.9",
                             "update_available": True, "checked_ms": ts}

    r = client.post("/api/guest-tokens", headers=H, json={"label": "contract"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# Open-Meteo and The Weather Company, answered locally. The shapes are
# trimmed copies of the real responses; only the keys the server reads
# or passes through matter.
_OPEN_METEO = {
    "latitude": 33.3, "longitude": -111.94, "elevation": 371.0,
    "timezone": "America/Phoenix",
    "current": {"time": "2026-10-15T12:00", "temperature_2m": 88.1,
                "relative_humidity_2m": 20, "weather_code": 0,
                "wind_speed_10m": 4.2, "wind_direction_10m": 200},
    "daily_units": {"temperature_2m_max": "°F"},
    "daily": {
        "time": ["2026-10-15", "2026-10-16"],
        "weather_code": [0, 3],
        "temperature_2m_max": [91.2, 89.0],
        "temperature_2m_min": [66.1, 64.8],
        "precipitation_probability_max": [0, 10],
        "wind_speed_10m_max": [9.8, 12.1],
        "wind_direction_10m_dominant": [210, 190],
    },
}
_TWC = {
    "validTimeLocal": ["2026-10-15T07:00:00-0700", "2026-10-16T07:00:00-0700"],
    "calendarDayTemperatureMax": [91, 89],
    "calendarDayTemperatureMin": [66, 65],
    "daypart": [{
        "daypartName": ["Today", "Tonight", "Tomorrow", "Tomorrow night"],
        "narrative": ["Sunny. High 91F.", "Clear. Low 66F.",
                      "Partly cloudy. High 89F.", "Clear. Low 65F."],
        "precipChance": [0, 0, 10, 0],
        "windSpeed": [9, 5, 12, 6],
        "windDirection": [210, 180, 190, 170],
        "iconCode": [32, 31, 30, 29],
        "qpf": [0.0, 0.0, 0.0, 0.0],
    }],
}


def _mock_forecast_upstreams(monkeypatch, twc_fails: dict[str, bool]) -> None:
    import httpx
    from app.config import settings
    monkeypatch.setattr(settings, "wu_api_key", "contract-test-wu-key")
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=_OPEN_METEO)
        if request.url.host == "api.weather.com":
            if twc_fails["on"]:
                return httpx.Response(500, text="upstream down")
            return httpx.Response(200, json=_TWC)
        return httpx.Response(404)
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler)))


# ── probes: how each stable endpoint is exercised ─────────────────────

def _get(client, url: str, headers: dict[str, str]) -> Any:
    r = client.get(url, headers=headers)
    assert r.status_code == 200, f"GET {url} -> {r.status_code}: {r.text}"
    return r.json()


def _probe_version(client, hdrs, monkeypatch):
    return [("GET /api/version (no token)", _get(client, "/api/version", {})),
            ("GET /api/version (with token)", _get(client, "/api/version", hdrs))]


def _probe_devices(client, hdrs, monkeypatch):
    return [("GET /api/devices", _get(client, "/api/devices", hdrs))]


def _probe_current(client, hdrs, monkeypatch):
    return [(f"GET /api/devices/{mac}/current",
             _get(client, f"/api/devices/{mac}/current", hdrs))
            for mac in (STATION, LIFETIME, STATION.replace(":", "").lower())]


def _probe_history(client, hdrs, monkeypatch):
    # 2 h is served raw; 48 h is bucketed (the *_min/*_max fields).
    return [(f"GET /api/devices/{STATION}/history?hours={h}",
             _get(client, f"/api/devices/{STATION}/history?hours={h}", hdrs))
            for h in (2, 48)]


def _probe_forecast(client, hdrs, monkeypatch):
    twc_fails = {"on": False}
    _mock_forecast_upstreams(monkeypatch, twc_fails)
    out = [("GET /api/forecast", _get(client, "/api/forecast", hdrs)),
           ("GET /api/forecast?source=twc",
            _get(client, "/api/forecast?source=twc&lat=33.3&lon=-111.9", hdrs))]
    twc_fails["on"] = True
    out.append(("GET /api/forecast?source=twc (TWC down, falls back)",
                _get(client, "/api/forecast?source=twc&lat=33.3&lon=-111.9", hdrs)))
    # The mocked upstream 500 must really take the fallback path, or the
    # probe would check an ordinary TWC answer twice (CodeRabbit, PR #94).
    fallback = out[-1][1]
    assert fallback["source"] == "open-meteo"
    assert fallback["fallback_from"] == "twc"
    return out


def _probe_alerts_recent(client, hdrs, monkeypatch):
    return [("GET /api/alerts/recent",
             _get(client, "/api/alerts/recent", hdrs))]


PROBES = {
    "GET /api/version": _probe_version,
    "GET /api/devices": _probe_devices,
    "GET /api/devices/{mac}/current": _probe_current,
    "GET /api/devices/{mac}/history": _probe_history,
    "GET /api/forecast": _probe_forecast,
    "GET /api/alerts/recent": _probe_alerts_recent,
}


def test_every_stable_endpoint_has_a_probe():
    """A route added to the contract without a probe would never be
    checked; a probe left behind for a dropped route hides the drop."""
    assert set(PROBES) == set(_contract()["endpoints"])


@pytest.mark.parametrize("route", sorted(PROBES))
def test_stable_endpoint_keeps_its_contract(client, monkeypatch, route):
    guest = _seed(client)
    bodies: list[tuple[str, Any]] = []
    for who, hdrs in (("owner token", H),
                      ("guest token", {"Authorization": f"Bearer {guest}"})):
        for label, body in PROBES[route](client, hdrs, monkeypatch):
            bodies.append((f"{label} [{who}]", body))
    _assert_contract(route, bodies)


def test_stable_routes_exist_in_the_app(client):
    """The contract's routes are real routes, spelled the same way."""
    from app.main import app
    have = {(m, r.path) for r in app.routes
            for m in getattr(r, "methods", None) or ()}
    for route in _contract()["endpoints"]:
        method, path = route.split(" ", 1)
        assert (method, path) in have, f"{route} is in the contract but not served"


def test_api_doc_names_every_stable_field():
    """API.md is the human copy of the contract. A stable field the doc
    never mentions is a promise nobody can read."""
    doc = API_DOC_PATH.read_text()
    contract = _contract()
    defs = contract["definitions"]
    missing = []
    for route, entry in contract["endpoints"].items():
        assert f"`{route.split(' ', 1)[1]}`" in doc, f"API.md does not document {route}"
        for p in _declared_paths(entry["response"], defs):
            leaf = re.split(r"[.\[\]]+", p.rstrip("[]"))[-1]
            # `name` or a dotted path ending in it (`daily.time`).
            if leaf and leaf != "(body)" and not re.search(
                    rf"`(?:[\w\[\]]+\.)*{re.escape(leaf)}`", doc):
                missing.append(f"{route}: {p}")
    assert not missing, "API.md does not mention:\n" + "\n".join(sorted(set(missing)))


def test_capabilities_are_documented(client):
    """Each capability string /api/version advertises is explained in
    API.md, so a client knows what it may rely on."""
    doc = API_DOC_PATH.read_text()
    caps = client.get("/api/version").json()["capabilities"]
    assert caps, "capabilities must never be empty: stable-api is always there"
    assert "stable-api" in caps
    for c in caps:
        assert f"`{c}`" in doc, f"API.md does not explain capability {c!r}"


# ── the checker bites ─────────────────────────────────────────────────

def test_the_contract_check_bites():
    """Removing, renaming, retyping or nulling a field each fail, with the
    message that tells the developer what they broke."""
    route = "GET /api/alerts/recent"
    good = {"alerts": [{"id": 1, "ts_ms": 1, "kind": "rule", "mac": "M",
                        "title": "t", "body": "b", "delivered": 1,
                        "severity": "info", "rule_id": 3, "report_id": 5}]}
    _assert_contract(route, [("good", good)])          # passes as is

    def broken(mutate) -> str:
        body = json.loads(json.dumps(good))
        mutate(body["alerts"][0])
        with pytest.raises(pytest.fail.Exception) as e:
            _assert_contract(route, [("broken", body)])
        return str(e.value)

    msg = broken(lambda a: a.pop("ts_ms"))
    assert "PUBLIC API CONTRACT BROKEN" in msg and "alerts[].ts_ms: is missing" in msg
    msg = broken(lambda a: a.__setitem__("time_ms", a.pop("ts_ms")))
    assert "alerts[].ts_ms: is missing" in msg
    msg = broken(lambda a: a.__setitem__("id", "1"))
    assert "alerts[].id: is string; the contract says integer" in msg
    msg = broken(lambda a: a.__setitem__("title", None))
    assert "alerts[].title: is null" in msg
    msg = broken(lambda a: a.__setitem__("delivered", True))
    assert "alerts[].delivered: is boolean" in msg
    # A nullable field that is never anything but null cannot have its
    # type checked, so it fails as unseen.
    msg = broken(lambda a: a.__setitem__("rule_id", None))
    assert "never seen with a value" in msg and "alerts[].rule_id" in msg
