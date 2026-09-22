"""The read-only MCP server (2.1): transport, handshake, every tool.

A user points the Claude or ChatGPT they already pay for at POST /mcp
and asks about their own weather. Nothing here writes, nothing here
stores a provider key, and every number comes back in storage units
with a missing sensor as null.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:DD:EE:FF"
MCP_H = {**H, "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": "2025-06-18"}


def _post_obs(client, ts: datetime, tempf=75.0, extra=None):
    body = {"device": {"id": "AABBCCDDEEFF", "name": "Yard"},
            "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outdoor": {"tempf": tempf, "humidity": 40},
            "wind": {"speed_mph": 3, "gust_mph": 10},
            "pressure": {"relative_inhg": 29.9}, "source": "test"}
    if extra:
        body.update(extra)
    r = client.post("/ingest/custom", headers=IH, json=body)
    assert r.status_code == 200, r.text
    return r


def _seed(client, n=4, step_h=6):
    base = datetime.now(timezone.utc) - timedelta(hours=n * step_h)
    for i in range(n):
        _post_obs(client, base + timedelta(hours=i * step_h), 70.0 + i)
    return base


def rpc(client, method, params=None, rid=1, headers=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        msg["id"] = rid
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", headers=headers or MCP_H, json=msg)


def call(client, name, arguments=None, rid=7):
    r = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}},
            rid=rid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == rid
    return body


def result_of(client, name, arguments=None):
    body = call(client, name, arguments)
    assert "result" in body, body
    res = body["result"]
    assert res["isError"] is False, res
    assert res["content"][0]["type"] == "text"
    return res["structuredContent"]


def error_of(client, name, arguments=None) -> str:
    body = call(client, name, arguments)
    res = body["result"]
    assert res["isError"] is True, res
    return res["content"][0]["text"]


# ───────────────────────── transport ─────────────────────────

def test_the_endpoint_is_behind_the_api_token(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    bad = {**MCP_H, "Authorization": "Bearer nope"}
    assert rpc(client, "ping", headers=bad).status_code == 401
    # The token never rides the URL: the spec forbids it and so do we.
    r = client.post("/mcp?token=test-api-token",
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_the_reviewer_read_only_token_may_read(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "reviewer_api_token", "reviewer-token-xyz")
    hdr = {**MCP_H, "Authorization": "Bearer reviewer-token-xyz"}
    assert rpc(client, "ping", headers=hdr).status_code == 200


def test_initialize_negotiates_a_version_and_names_the_tools_capability(client):
    r = rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"]["tools"] == {"listChanged": False}
    assert res["serverInfo"]["name"] == "zasder-weather"
    assert res["serverInfo"]["version"]
    assert "list_stations" in res["instructions"]
    # An unknown version is answered with our latest; the client decides.
    r = rpc(client, "initialize", {"protocolVersion": "1999-01-01",
                                   "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"
    # No session header: the server is stateless and says so by omission.
    assert "mcp-session-id" not in r.headers


def test_notifications_are_accepted_with_202_and_no_body(client):
    r = rpc(client, "notifications/initialized", rid=None)
    assert r.status_code == 202 and r.content == b""
    r = rpc(client, "notifications/cancelled", {"requestId": 3}, rid=None)
    assert r.status_code == 202


def test_ping_and_unknown_methods(client):
    assert rpc(client, "ping").json()["result"] == {}
    body = rpc(client, "resources/list").json()
    assert body["error"]["code"] == -32601


def test_malformed_bodies_are_400_with_a_jsonrpc_error(client):
    r = client.post("/mcp", headers=MCP_H, content=b"{not json")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r = client.post("/mcp", headers=MCP_H, json={"hello": "world"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600
    # Batches: one message per POST.
    r = client.post("/mcp", headers=MCP_H,
                    json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600


def test_protocol_version_header_is_checked(client):
    hdr = {**MCP_H, "MCP-Protocol-Version": "2020-01-01"}
    assert rpc(client, "ping", headers=hdr).status_code == 400
    # Absent = assume 2025-03-26 (spec), which we speak.
    hdr = {k: v for k, v in MCP_H.items() if k != "MCP-Protocol-Version"}
    assert rpc(client, "ping", headers=hdr).status_code == 200


def test_get_and_delete_answer_405(client):
    r = client.get("/mcp", headers=MCP_H)
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert client.delete("/mcp", headers=MCP_H).status_code == 405
    # Still authenticated first: an anonymous GET learns nothing.
    assert client.get("/mcp").status_code == 401


def test_a_foreign_origin_is_refused(client):
    hdr = {**MCP_H, "Origin": "https://evil.example"}
    assert rpc(client, "ping", headers=hdr).status_code == 403
    hdr = {**MCP_H, "Origin": "http://testserver"}
    assert rpc(client, "ping", headers=hdr).status_code == 200


# ───────────────────────── tools/list ─────────────────────────

def test_tools_list_is_well_formed_and_read_only(client):
    res = rpc(client, "tools/list").json()["result"]
    names = [t["name"] for t in res["tools"]]
    assert names == ["list_stations", "current_conditions", "history",
                     "daily_summary", "records", "insights", "stories",
                     "weather_changes", "reports", "report", "storm_history",
                     "noaa_report"]
    for t in res["tools"]:
        assert t["description"]
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert t["annotations"]["readOnlyHint"] is True
        assert t["annotations"]["destructiveHint"] is False
    # Units are in the words the model reads.
    hist = next(t for t in res["tools"] if t["name"] == "history")
    assert "°F" in hist["description"] and "inHg" in hist["description"]


def test_unknown_tool_and_bad_arguments_are_protocol_errors(client):
    body = call(client, "make_it_rain")
    assert body["error"]["code"] == -32602 and "Unknown tool" in body["error"]["message"]
    body = call(client, "current_conditions", {})
    assert body["error"]["code"] == -32602 and "mac is required" in body["error"]["message"]
    body = call(client, "current_conditions", {"mac": MAC, "bogus": 1})
    assert "unknown argument bogus" in body["error"]["message"]
    body = call(client, "history", {"mac": MAC, "start": "x", "limit": 99999})
    assert "limit must be at most" in body["error"]["message"]
    body = call(client, "reports", {"kind": "evening"})
    assert "kind must be one of" in body["error"]["message"]
    r = rpc(client, "tools/call", {"name": 5})
    assert r.json()["error"]["code"] == -32602


# ───────────────────────── the tools ─────────────────────────

def test_list_stations_and_current_conditions(client):
    _seed(client)
    out = result_of(client, "list_stations")
    assert out["count"] == 1
    st = out["stations"][0]
    assert st["mac"] == MAC and st["name"] == "Yard"
    assert st["last_seen_ms"] and st["last_seen_iso"].endswith("+00:00")
    assert "tempf" in st["sensors"] and "solarradiation" not in st["sensors"]
    cur = result_of(client, "current_conditions", {"mac": MAC})
    assert cur["reading"]["tempf"] == 73.0
    assert cur["observed_iso"]
    # A sensor the station lacks is null, never 0.
    assert cur["reading"].get("solarradiation") is None
    assert cur["reading"].get("uv") is None
    # Lowercase / compact MACs normalise like the REST routes do.
    assert result_of(client, "current_conditions",
                     {"mac": "aabbccddeeff"})["mac"] == MAC


def test_unknown_station_is_a_tool_error_not_a_500(client):
    _seed(client)
    text = error_of(client, "current_conditions", {"mac": "11:22:33:44:55:66"})
    assert "unknown station" in text
    text = error_of(client, "current_conditions", {"mac": "not-a-mac"})
    assert "list_stations" in text


def test_history_windows_rows_and_field_selection(client):
    base = _seed(client, n=4, step_h=1)          # four rows an hour apart
    start = (base - timedelta(minutes=5)).isoformat()
    out = result_of(client, "history", {"mac": MAC, "start": start,
                                        "fields": ["tempf"]})
    assert out["count"] == 4 and out["bucketed"] is False
    row = out["rows"][0]
    assert set(row) == {"dateutc", "tempf", "iso"}
    assert [r["tempf"] for r in out["rows"]] == [70.0, 71.0, 72.0, 73.0]
    # Epoch ms works too, and a limit trims.
    out = result_of(client, "history", {"mac": MAC,
                                        "start": int(base.timestamp() * 1000) - 1,
                                        "limit": 2})
    assert out["count"] == 2
    # Too wide → honest refusal.
    text = error_of(client, "history", {
        "mac": MAC, "start": (base - timedelta(days=40)).isoformat()})
    assert "31 days" in text
    text = error_of(client, "history", {"mac": MAC, "start": "2026-13-45"})
    assert "ISO 8601" in text
    text = error_of(client, "history", {"mac": MAC,
                                        "start": base.isoformat(),
                                        "end": (base - timedelta(hours=1)).isoformat()})
    assert "end must be after start" in text


def test_insights_family_declines_when_the_flag_is_off(client):
    _seed(client)
    for name, args in (("daily_summary", {"mac": MAC, "start_day": "2026-01-01"}),
                       ("insights", {"mac": MAC}),
                       ("stories", {"mac": MAC}),
                       ("noaa_report", {"mac": MAC, "year": 2026})):
        assert "INSIGHTS=0" in error_of(client, name, args)


@pytest.fixture
def insights_on(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    return client


def test_daily_summary_records_insights_stories_and_noaa(insights_on):
    client = insights_on
    from app import insights
    ts = datetime(2026, 5, 2, 8, 0, tzinfo=timezone.utc)
    for i in range(6):
        _post_obs(client, ts + timedelta(hours=i * 3), 70.0 + i)
    asyncio.run(insights.rebuild(MAC))
    insights._REBUILD_LOCK = None

    out = result_of(client, "daily_summary", {"mac": MAC, "start_day": "2026-05-01",
                                              "end_day": "2026-05-03"})
    assert out["count"] == 1
    day = out["days"][0]
    assert day["day"] == "2026-05-02"
    assert day["tempf_min"] == 70.0 and day["tempf_max"] == 75.0
    assert day["tempf_mean"] == 72.5
    assert day["uv_max"] is None and day["solarradiation_max"] is None
    assert day["lightning_max"] is None
    assert "at most 366" in error_of(client, "daily_summary", {
        "mac": MAC, "start_day": "2020-01-01", "end_day": "2022-01-01"})
    assert "real calendar day" in error_of(client, "daily_summary", {
        "mac": MAC, "start_day": "2026-02-30"})

    rec = result_of(client, "records", {"mac": MAC})["records"]
    assert isinstance(rec, dict) and rec

    ins = result_of(client, "insights", {"mac": MAC})["insights"]
    assert ins["day_count"] == 1

    st = result_of(client, "stories", {"mac": MAC, "limit": 3})
    assert "stories" in st and isinstance(st["stories"], list)

    noaa = result_of(client, "noaa_report", {"mac": MAC, "year": 2026, "month": 5})
    assert "Yard" in noaa["text"] and noaa["month"] == 5
    noaa = result_of(client, "noaa_report", {"mac": MAC, "year": 2026})
    assert noaa["month"] is None and noaa["text"]


def test_reports_and_report(client):
    from app import db
    _seed(client)

    async def seed():
        a = await db.insert_report("morning", None, 1_700_000_000_000,
                                   "2026-09-01", "Morning report",
                                   "Hi 93°F", {"stations": [], "hi": 93.0},
                                   "morning:2026-09-01")
        b = await db.insert_report("storm", MAC, 1_700_000_100_000, None,
                                   "Storm", "0.5 in", {"total_in": 0.5},
                                   f"storm:{MAC}:1")
        return a, b
    a, b = asyncio.run(seed())
    out = result_of(client, "reports")
    # The kinds list is every kind the server knows (climate reports joined
    # in 2.1), not the kinds present in the two seeded rows.
    assert out["count"] == 2
    assert {"morning", "storm"} <= set(out["kinds"])
    assert out["reports"][0]["id"] == b            # newest first
    assert out["reports"][0]["ts_iso"].startswith("2023-11-14")
    assert "payload" not in out["reports"][0]
    only = result_of(client, "reports", {"kind": "morning"})
    assert [r["id"] for r in only["reports"]] == [a]
    one = result_of(client, "report", {"id": a})["report"]
    assert one["payload"]["hi"] == 93.0
    assert "no report" in error_of(client, "report", {"id": 999})


def test_storm_history(client):
    from app import db
    _seed(client)
    asyncio.run(db.record_storm(MAC, {
        "started_ms": 1_700_000_000_000, "ended_ms": 1_700_003_600_000,
        "total_in": 0.42, "peak_rate_in_hr": 1.2, "max_gust_mph": 31.0}))
    out = result_of(client, "storm_history", {"mac": MAC})
    assert out["count"] == 1
    s = out["storms"][0]
    assert s["total_in"] == 0.42 and s["max_gust_mph"] == 31.0
    assert s["min_tempf"] is None                  # nobody measured it
    assert s["started_iso"] and s["ended_iso"]


def test_a_tool_crash_is_reported_in_band(client, monkeypatch):
    from app import mcp
    _seed(client)

    async def boom(args):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(mcp._BY_NAME, "list_stations",
                        (mcp._BY_NAME["list_stations"][0], boom))
    text = error_of(client, "list_stations")
    assert "failed on the server" in text and "kaboom" not in text


def test_nan_never_reaches_the_client(client, monkeypatch):
    from app import mcp
    _seed(client)

    async def nan(args, role="owner"):
        return {"x": float("nan"), "y": [float("inf"), 1.5]}
    monkeypatch.setitem(mcp._BY_NAME, "list_stations",
                        (mcp._BY_NAME["list_stations"][0], nan))
    out = result_of(client, "list_stations")
    assert out == {"x": None, "y": [None, 1.5]}


def test_validate_arguments_covers_the_schema_subset():
    from app.mcp import validate_arguments
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"},
                             "n": {"type": "integer", "minimum": 1, "maximum": 3},
                             "e": {"type": "string", "enum": ["x", "y"]},
                             "l": {"type": "array", "items": {"type": "string"}},
                             "m": {"type": ["string", "number"]}}}
    assert validate_arguments(schema, {"a": "ok", "n": 2, "e": "x",
                                       "l": ["p"], "m": 3}) == []
    assert validate_arguments(schema, "nope") == ["arguments must be an object"]
    probs = validate_arguments(schema, {"n": True, "e": "z", "l": [1], "m": []})
    assert "a is required" in probs
    assert "n must be integer" in probs
    assert "e must be one of x, y" in probs
    assert "l must be a list of strings" in probs
    assert "m must be string or number" in probs
    assert validate_arguments(schema, {"a": "k", "n": 9}) == ["n must be at most 3"]


def test_a_guest_sees_through_mcp_exactly_what_a_guest_sees_on_the_rest_api(client):
    """2.1 review T2: the role the gate computes reaches every tool, and
    list_stations strips for a guest the way /api/devices does — the
    location label gone, the coordinates rounded to town scale."""
    import asyncio
    import time
    from app import db
    client.post("/ingest/custom", headers=IH, json={
        "device": {"id": "AABBCCDDEEFF", "name": "Yard"},
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdoor": {"tempf": 80.0},
        "coords": {"lat": 33.3004, "lon": -111.9378, "location": "123 Elm St, Chandler"},
        "source": "test"})
    owner = result_of(client, "list_stations")["stations"][0]
    assert owner["location"] == "123 Elm St, Chandler"
    assert owner["coords"] == {"lat": 33.3004, "lon": -111.9378}

    guest_tok = "zwg_" + "cd" * 16
    asyncio.run(db.add_guest_token(guest_tok, "Doren", int(time.time() * 1000)))
    gh = {**MCP_H, "Authorization": f"Bearer {guest_tok}"}
    r = rpc(client, "tools/call", {"name": "list_stations", "arguments": {}}, headers=gh)
    assert r.status_code == 200, r.text
    guest = r.json()["result"]["structuredContent"]["stations"][0]
    assert guest["location"] is None, "the label names a house"
    assert guest["coords"] == {"lat": 33.3, "lon": -111.9}
    # And the REST surface agrees, so neither can quietly drift.
    rest = client.get("/api/devices", headers={"Authorization": f"Bearer {guest_tok}"}).json()[0]
    assert rest["location"] is None
    assert rest["info"]["coords"]["coords"] == {"lat": 33.3, "lon": -111.9}
    # The reading's `_source` blob is the poster's payload verbatim, exact
    # coordinates included: the owner sees it, a guest does not, on both
    # surfaces.
    mac = owner["mac"]
    own = result_of(client, "current_conditions", {"mac": mac})["reading"]
    assert own["_source"]["coords"]["lat"] == 33.3004
    r = rpc(client, "tools/call", {"name": "current_conditions",
                                   "arguments": {"mac": mac}}, headers=gh)
    assert "_source" not in r.json()["result"]["structuredContent"]["reading"]
    rest_own = client.get(f"/api/devices/{mac}/current", headers=H).json()
    rest_guest = client.get(f"/api/devices/{mac}/current",
                            headers={"Authorization": f"Bearer {guest_tok}"}).json()
    assert "_source" in rest_own and "_source" not in rest_guest
    assert rest_guest["tempf"] == 80.0


def test_an_oauth_guest_session_is_stripped_like_a_guest_bearer(client):
    """§5 row 11: the role the gate computes for an OAuth access token
    issued against a guest link token reaches the tools, so list_stations
    is stripped the way the raw guest bearer is."""
    import asyncio
    import base64
    import hashlib
    import secrets
    import time
    from urllib.parse import parse_qs, urlsplit
    from app import db
    client.post("/ingest/custom", headers=IH, json={
        "device": {"id": "AABBCCDDEEFF", "name": "Yard"},
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdoor": {"tempf": 80.0},
        "coords": {"lat": 33.3004, "lon": -111.9378, "location": "123 Elm St, Chandler"},
        "source": "test"})
    guest_tok = "zwg_" + "ef" * 16
    asyncio.run(db.add_guest_token(guest_tok, "Doren", int(time.time() * 1000)))
    redirect = "https://claude.ai/api/mcp/auth_callback"
    reg = client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [redirect],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"]}).json()
    assert client.post(f"/api/oauth/clients/{reg['client_id']}/approve",
                       headers=H).status_code == 200
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": reg["client_id"], "redirect_uri": redirect,
              "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "scope": "weather:read",
              "state": "s", "resource": "http://testserver/mcp"}
    r = client.post("/oauth/authorize", data={**params, "token": guest_tok},
                    follow_redirects=False)
    assert r.status_code == 302, r.text
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect, "client_id": reg["client_id"],
        "code_verifier": verifier, "resource": "http://testserver/mcp"}).json()
    gh = {**MCP_H, "Authorization": f"Bearer {tok['access_token']}"}
    r = rpc(client, "tools/call", {"name": "list_stations", "arguments": {}}, headers=gh)
    assert r.status_code == 200, r.text
    st = r.json()["result"]["structuredContent"]["stations"][0]
    assert st["location"] is None
    assert st["coords"] == {"lat": 33.3, "lon": -111.9}
    r = rpc(client, "tools/call", {"name": "current_conditions",
                                   "arguments": {"mac": st["mac"]}}, headers=gh)
    assert "_source" not in r.json()["result"]["structuredContent"]["reading"]


def test_a_role_that_is_not_the_owner_fails_closed(client):
    """Round-three review BE-F12: one handler stripped on `== "guest"`
    while another stripped on `!= "owner"`; a third role must read as a
    guest everywhere."""
    import asyncio
    from app import mcp
    client.post("/ingest/custom", headers=IH, json={
        "device": {"id": "AABBCCDDEEFF", "name": "Yard"},
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdoor": {"tempf": 80.0},
        "coords": {"lat": 33.3004, "lon": -111.9378, "location": "123 Elm St"},
        "source": "test"})
    reading = asyncio.run(mcp._current_conditions({"mac": MAC}, "auditor"))["reading"]
    assert "_source" not in reading
    stations = asyncio.run(mcp._list_stations({}, "auditor"))["stations"]
    assert stations[0]["source"] is None and stations[0]["location"] is None
    owner = asyncio.run(mcp._list_stations({}, "owner"))["stations"]
    assert owner[0]["source"] is not None
