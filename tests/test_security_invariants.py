"""Repo-wide security invariants.

These pin *classes* of mistake rather than individual bugs. Each one exists
because the property it protects is easy to lose in a routine change and
produces no visible symptom when it does — a new route quietly defaulting to
public, a whitelist demoted to an assert, a share token gaining write access.

Every test here must be able to FAIL. When adding one, break the property
deliberately and confirm it goes red before committing.
"""
from __future__ import annotations

import pathlib
import re

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
H_PRIMARY = {"Authorization": "Bearer test-api-token"}
H_REVIEWER = {"Authorization": "Bearer test-reviewer-token"}
H_GUEST = {"Authorization": "Bearer test-guest-token"}


# ── route inventory ────────────────────────────────────────────────────

def _routes():
    """(guard, method, path, file) for every declared route.

    Matches @x.api_route(..., methods=[...]) too (R12 W4): the catch-all
    routes in capture.py are declared that way, and the old verb-only
    regex made them invisible to EVERY sweep in this file — a future
    mutating api_route would have passed the whole audit green."""
    out = []
    for f in sorted(APP.glob("*.py")):
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            m = re.match(
                r"\s*@(?:app|router)\.(get|post|put|patch|delete|api_route)\(",
                line)
            if not m:
                continue
            chunk, j = [], i
            while j < len(lines) and not re.match(r"\s*(async\s+)?def ", lines[j]):
                chunk.append(lines[j])
                j += 1
            dec = " ".join(chunk)
            pm = re.search(r'"([^"]+)"', dec)
            path = pm.group(1) if pm else "?"
            if "require_shared_write" in dec:
                guard = "SHARED-WRITE"
            elif "require_write_token" in dec:
                guard = "WRITE"
            elif "require_token" in dec:
                guard = "READ"
            elif "Depends" in dec:
                guard = "DEPENDS"
            else:
                guard = "NO-DEP"
            if m.group(1) == "api_route":
                # One row per declared HTTP method, so the mutating-guard
                # sweeps see api_route POSTs exactly like @app.post ones.
                # Both quote styles (CodeRabbit): methods=['POST'] is valid
                # Python, and a double-quote-only match would have recorded
                # it as GET — sliding it past the mutating sweeps.
                methods = re.findall(
                    r'["\'](GET|POST|PUT|PATCH|DELETE)["\']', dec)
                for meth in (methods or ["GET"]):
                    out.append((guard, meth, path, f.name))
            else:
                out.append((guard, m.group(1).upper(), path, f.name))
    return out


# A route may lack a FastAPI dependency for exactly two reasons:
#   1. it is deliberately public, listed below; or
#   2. it authenticates INSIDE the handler — because the token arrives in a
#      header that must not reach proxy access logs, or because the check is
#      App Attest rather than a bearer token.
#
# Case 2 is VERIFIED, not asserted: the handler body must actually call an
# auth helper. Listing those paths here instead would put literals from
# operator-only modules into a file that ships to the public mirror, where
# the strip scripts are required to remove every trace of them. A derived
# rule is correct in both trees.
PUBLIC_BY_DESIGN = {
    ("GET", "/"), ("GET", "/status"), ("GET", "/healthz"),
    ("GET", "/api/version"), ("GET", "/metrics"),
    # The iframe-able public dashboard. Anonymous BY OPT-IN: the handler
    # 404s unless the operator set PUBLIC_DASHBOARD=1, and it serves the
    # same fragment the open "/" page already shows when that flag is on —
    # no new data crosses the line (1.6.1).
    ("GET", "/embed"),
    # OAuth 2.1 discovery for the MCP server (2.1, app/oauth.py). The two
    # metadata documents say only where to log in (RFC 9728 / RFC 8414),
    # and the consent page names the asking app and takes a token — it
    # grants nothing; the POST that does is checked in-handler.
    ("GET", "/.well-known/oauth-protected-resource/mcp"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/oauth/authorize"),
}

# Routes that are anonymous AND mutating, on purpose: the OAuth 2.1
# endpoints a connector must reach before it has any credential
# (2.1 review, SEC-7). Their control is rate limiting plus what each one
# can and cannot do: registration stores an inert client (nothing until
# the owner approves it); the consent POST verifies a connect code or the
# typed server token; token and revoke authenticate the client_id and the
# code / refresh token by hash. Listed by method+path so a rate limiter
# alone can never count as authentication anywhere else.
ANONYMOUS_BY_DESIGN = {
    ("POST", "/oauth/register"),
    ("POST", "/oauth/authorize"),
    ("POST", "/oauth/token"),
    ("POST", "/oauth/revoke"),
}

_AUTH_CALL = re.compile(
    # in-handler bearer/token checks
    r"_require_\w*token|tokens_match|_require_admin|"
    r"HTTPException\(status_code=401"
    # NB: no App Attest internals are named here. Those symbols are residue
    # strings the relay strip script must erase from the mirror, and this
    # file ships to the mirror — naming them reintroduced the very traces the
    # stripper exists to delete, twice. The hosted-relay module is exempted
    # below instead. ("relay" itself stays public: self-hosters use client
    # mode. It is the server internals that are private.)
    # a credential dependency declared in the SIGNATURE rather than the
    # decorator — `key: dict = Depends(_authed_key)`. Deliberately narrow:
    # matching any Depends() would let a route with only Depends(get_db)
    # pass as authenticated.
    r"|Depends\(\s*_?\w*(auth|token|key|admin)"
    # the OAuth 2.1 endpoints for the MCP server (2.1, app/oauth.py):
    # registration is open by RFC 7591 and controlled by rate limiting like
    # the nonce endpoint below (_rate_ok, and it grants nothing); the
    # consent POST verifies the typed server token through _role_for
    # (tokens_match inside); the token and revoke endpoints authenticate
    # the client_id via _get_client and the code / refresh token by hash.
    r"|_role_for\(|_get_client\("
    # a public nonce endpoint whose control is rate limiting, not identity.
    # It issues a random challenge and grants nothing; the attestation it
    # feeds is verified separately when the challenge is redeemed.
    r"|_challenge_rate_ok")


def _handler_body(path: str, method: str, fname: str | None = None) -> str:
    """Source of the function decorated with this route.

    Restricted to `fname` when given: resolving by method+path alone reads
    the FIRST decorator match across every module, so two modules declaring
    the same route (e.g. mid-move between relay.py and main.py) could hand
    back the guarded twin while the live one ships open (CodeRabbit,
    2026-08-20).

    Parsed with ast rather than sliced by indentation: a multi-line signature
    ends with `) -> dict[str, Any]:` at column zero, so an "indented lines
    belong to the body" heuristic stops at the signature and reports every
    in-handler auth check as missing.
    """
    import ast
    for f in sorted(APP.glob("*.py")):
        if fname is not None and f.name != fname:
            continue
        text = f.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                # api_route declares its verbs in methods=[...] (R12 W4) —
                # resolve it for any of them.
                if not (isinstance(fn, ast.Attribute)
                        and (fn.attr == method.lower()
                             or fn.attr == "api_route")):
                    continue
                if not (dec.args and isinstance(dec.args[0], ast.Constant)
                        and dec.args[0].value == path):
                    continue
                return ast.get_source_segment(text, node) or ""
    return ""


def test_every_undeclared_route_authenticates_in_its_handler():
    """A route with no dependency must either be public by design or prove it
    checks credentials itself. Forgetting `dependencies=[...]` on a new route
    now fails here instead of silently shipping an open endpoint."""
    unguarded = []
    for guard, method, path, fname in _routes():
        if guard != "NO-DEP" or (method, path) in PUBLIC_BY_DESIGN:
            continue
        if (method, path) in ANONYMOUS_BY_DESIGN:
            continue
        # The hosted-relay server is stripped from the public mirror and is
        # gated by App Attest, not a bearer token. Its own test module covers
        # that flow; asserting it here would require naming symbols this file
        # must not contain.
        if fname == "relay.py":
            continue
        body = _handler_body(path, method, fname)
        # The OAuth helpers (_role_for, _get_client, _rate_ok) establish
        # authentication only inside app/oauth.py, where each endpoint's
        # control is documented above; elsewhere they prove nothing.
        if fname != "oauth.py":
            body = re.sub(r"_role_for\(|_get_client\(", "", body)
        if not _AUTH_CALL.search(body):
            unguarded.append(f"{method} {path} ({fname})")
    assert not unguarded, (
        "route(s) with neither an auth dependency nor an in-handler check: "
        f"{unguarded}. Add dependencies=[Depends(require_token)] / "
        "require_write_token, or if it is genuinely public add it to "
        "PUBLIC_BY_DESIGN with a reason."
    )


def test_the_public_route_list_has_not_quietly_grown():
    """PUBLIC_BY_DESIGN is the whole unauthenticated surface. It should change
    only by deliberate edit, never as a side effect."""
    assert len(PUBLIC_BY_DESIGN) == 10  # +/embed (1.6.1); +4 OAuth discovery/consent (2.1)
    assert all(m == "GET" for m, _ in PUBLIC_BY_DESIGN), (
        "a mutating route was added to the public-by-design list")
    assert len(ANONYMOUS_BY_DESIGN) == 4, (
        "an anonymous mutating route was added; document its control here")
    assert all(p.startswith("/oauth/") for _, p in ANONYMOUS_BY_DESIGN)


def test_the_anonymous_mutating_routes_are_exactly_the_oauth_four():
    """The complement of the previous test: every mutating route that
    has no dependency is in ANONYMOUS_BY_DESIGN, and each one lives in
    oauth.py and calls its limiter. Routes are read from source, so a new
    anonymous POST anywhere else fails here before it can be relied on."""
    # Every module (relay.py excepted, as in the sweep above): a new
    # anonymous POST in main.py must land in `found` and fail the equality,
    # not be filtered out before the comparison (CodeRabbit, PR #36).
    # /ingest/* routes check INGEST_TOKEN inside the handler and /mcp
    # runs its own bearer gate (API, guest or OAuth token); the sweep
    # above proves those calls are present, so neither is anonymous.
    found = {(m, p, f) for g, m, p, f in _routes()
             if g == "NO-DEP" and m in {"POST", "PUT", "PATCH", "DELETE"}
             and f != "relay.py" and not p.startswith("/ingest/")
             and p != "/mcp"}
    assert {(m, p) for m, p, _ in found} == ANONYMOUS_BY_DESIGN, found
    assert {f for _, _, f in found} == {"oauth.py"}, found
    for method, path in ANONYMOUS_BY_DESIGN:
        assert "_rate_ok(" in _handler_body(path, method, "oauth.py"), (method, path)


_OWNER = {"Authorization": "Bearer test-api-token"}


def test_no_mcp_handler_defaults_its_role():
    """A privilege parameter fails closed (round-two review, BE-N8): every
    tool handler, call_tool and handle_message take `role` with no
    default, so a new call site cannot quietly run as the owner."""
    import ast
    tree = ast.parse((APP / "mcp.py").read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        args = node.args
        # Positional-only and regular positionals share one defaults list,
        # aligned with the LAST len(defaults) of them; keyword-only
        # parameters carry their own (None = no default).
        positional = [a.arg for a in args.posonlyargs + args.args]
        if "role" in positional:
            idx = positional.index("role") - (len(positional) - len(args.defaults))
            if idx >= 0:
                offenders.append(node.name)
        for a, d in zip(args.kwonlyargs, args.kw_defaults):
            if a.arg == "role" and d is not None:
                offenders.append(node.name)
    assert not offenders, f"role defaults in mcp.py: {offenders}"


def test_mcp_and_oauth_vanish_together_when_disabled(temp_env, monkeypatch):
    """MCP_ENABLED=0 (SEC-2): no /mcp, no OAuth endpoints, no discovery
    documents — the routes are not registered, so they 404 like any
    unknown path rather than answering with a policy error. Probed over
    HTTP: FastAPI stores included routers behind a private wrapper, and a
    behavioural check is what the switch promises anyway."""
    import importlib
    from fastapi.testclient import TestClient

    def probe():
        for mod in ["app.config", "app.main"]:
            if mod in importlib.sys.modules:
                importlib.reload(importlib.sys.modules[mod])
        from app.main import app
        with TestClient(app) as c:
            return {
                "mcp": c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                            "method": "ping"}).status_code,
                "as": c.get("/.well-known/oauth-authorization-server").status_code,
                "pr": c.get("/.well-known/oauth-protected-resource/mcp").status_code,
                "reg": c.post("/oauth/register", json={}).status_code,
                "auth": c.get("/oauth/authorize").status_code,
                # The four owner routes live on the app, not the router
                # (round-two review, SEC-F4): they take the switch as a
                # dependency instead. A made-up client id: with the switch
                # on these answer 404 too, so the "on" assertion below
                # reads them from a real registration.
                "clients": c.get("/api/oauth/clients", headers=_OWNER).status_code,
                "code": c.post("/api/oauth/connect-code", headers=_OWNER).status_code,
                **_approve_and_drop(c),
            }

    def _approve_and_drop(c):
        # A real registration when the switch is on (a made-up id would
        # 404 either way and prove nothing); when off, registration 404s
        # and the two routes are probed with a placeholder.
        reg = c.post("/oauth/register", json={"client_name": "probe",
                                               "redirect_uris": ["https://probe.example/cb"]})
        cid = reg.json().get("client_id", "nope") if reg.status_code in (200, 201) else "nope"
        return {
            "ok": c.post(f"/api/oauth/clients/{cid}/approve", headers=_OWNER).status_code,
            "drop": c.delete(f"/api/oauth/clients/{cid}", headers=_OWNER).status_code,
        }

    import os
    before = os.environ.get("MCP_ENABLED")
    try:
        monkeypatch.setenv("MCP_ENABLED", "0")
        off = probe()
        assert set(off.values()) == {404}, off
        monkeypatch.setenv("MCP_ENABLED", "1")
        on = probe()
        assert on["mcp"] == 401 and on["as"] == 200 and on["pr"] == 200, on
        assert on["reg"] == 400 and on["auth"] == 400, on
        assert on["clients"] == 200 and on["code"] == 200, on
        assert on["ok"] == 200 and on["drop"] == 200, on
    finally:
        # A failed assertion must not leave app.main reloaded with the
        # server switched off for every test that follows.
        if before is None:
            monkeypatch.delenv("MCP_ENABLED", raising=False)
        else:
            monkeypatch.setenv("MCP_ENABLED", before)
        probe()


def test_mutating_routes_are_never_merely_read_guarded():
    """A POST/PUT/PATCH/DELETE behind require_token would let a read-only
    share token or the App Store reviewer token change state."""
    bad = [(m, p, f) for g, m, p, f in _routes()
           if g == "READ" and m in {"POST", "PUT", "PATCH", "DELETE"}]
    assert not bad, f"mutating route(s) guarded by a READ token: {bad}"


# ── the guard that a build flag must not be able to remove ─────────────

def test_no_sql_whitelist_relies_on_assert():
    """`python -O` and PYTHONOPTIMIZE=1 strip asserts. A whitelist guarding an
    f-string column interpolation must be a raise, or the injection guard
    disappears with a build flag. CLAUDE.md states this rule."""
    offenders = []
    for f in APP.glob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if re.match(r"\s*assert\s", line) and re.search(
                    r"in _[A-Z_]*(COL|FIELD|TABLE)", line):
                offenders.append(f"{f.name}:{i}")
    assert not offenders, f"assert-guarded SQL whitelist(s): {offenders}"


def test_token_comparison_is_constant_time():
    """Plain equality short-circuits on the first differing byte, leaking
    prefix-match length. Every auth gate must route through tokens_match."""
    cfg = (APP / "config.py").read_text()
    assert "compare_digest" in cfg, "tokens_match lost its constant-time compare"

    # Parsed, not grepped: the tokens_match docstring NAMES the unsafe pattern
    # it replaces ("presented != expected"), and a text search flags its own
    # documentation. Only real Compare nodes count.
    import ast
    bad = []
    for f in APP.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if not ({"presented", "candidate", "supplied"} & names):
                continue
            for op in node.ops:
                if isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                    bad.append(f"{f.name}:{node.lineno}")
    assert not bad, (
        f"non-constant-time comparison of a presented credential: {bad} — "
        "route it through tokens_match (secrets.compare_digest)")


# ── end to end: what a read-only share token can actually reach ────────

@pytest.fixture
def guest_client(temp_env, monkeypatch):
    """A client with GUEST_API_TOKENS set, mirroring the `client` fixture's
    reload dance so Settings picks the variable up."""
    import importlib, sys
    monkeypatch.setenv("GUEST_API_TOKENS", "test-guest-token")
    for mod in ["app.config", "app.db", "app.insights", "app.wu_upload",
                "app.capture", "app.ingest", "app.discovery",
                "app.alerts", "app.apns", "app.relay", "app.integrations",
                "app.main"]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_a_guest_token_can_read(guest_client):
    """Precondition. If this fails the rest proves nothing — a token that is
    rejected everywhere would pass every negative test below."""
    assert guest_client.get("/api/devices", headers=H_GUEST).status_code == 200


# The parametrize sets are DERIVED by a source scan, and a scan that quietly
# degrades to zero entries would run zero cases while both tests report
# success — the exact vacuous-guard failure this module exists to prevent
# (CodeRabbit, 2026-08-20). Collection fails loudly instead.
_WRITE_CASES = sorted({(m, p) for g, m, p, _ in _routes()
                       if g in ("WRITE", "SHARED-WRITE")
                       and m in {"POST", "PUT", "PATCH", "DELETE"}})
assert _WRITE_CASES, "route scan found no write routes — scanner broke"


@pytest.mark.parametrize("method,path", _WRITE_CASES)
def test_a_guest_token_cannot_reach_any_write_route(guest_client, method, path):
    """Share codes go to family by message. One must never be able to delete a
    device, rewrite alert rules, or restore a config backup."""
    url = path.replace("{mac}", "AA:BB:CC:DD:EE:FF").replace("{rule_id}", "1")
    r = guest_client.request(method, url, headers=H_GUEST, json={})
    assert r.status_code in (401, 403), (
        f"{method} {path} accepted a read-only guest token ({r.status_code})")


def _operator_only_paths():
    """Routes under /api/ that authenticate INSIDE the handler and take no
    path parameter. Derived rather than hardcoded: some operator-only modules
    are stripped from the public mirror, and naming one of their routes here
    put a literal the stripper is required to remove into a test that ships
    with the mirror. Deriving it means this file is correct in both trees.
    """
    return sorted({p for g, m, p, _ in _routes()
                   if g == "NO-DEP" and m == "GET"
                   and p.startswith("/api/") and "{" not in p
                   and p not in ("/api/version",)})


_OPERATOR_ONLY = _operator_only_paths()
assert _OPERATOR_ONLY, "route scan found no operator-only routes — scanner broke"


@pytest.mark.parametrize("path", _OPERATOR_ONLY)
def test_private_routes_reject_guest_and_reviewer_tokens(guest_client, path):
    """These routes are operator diagnostics, not weather. A read-only share
    code handed to family must not widen into household telemetry it was
    never meant to cover, and the App Store reviewer token must not see it
    either."""
    for name, hdr in (("guest", H_GUEST), ("reviewer", H_REVIEWER)):
        r = guest_client.get(path, headers=hdr)
        assert r.status_code == 401, f"{path} accepted the {name} token"
    assert guest_client.get(path, headers=H_PRIMARY).status_code == 200, (
        f"{path} rejected the primary token — the negative assertions above "
        "would pass even if the route were broken")


# ── the write-share tier (1.9) ─────────────────────────────────────────
# Two-sided pin: the EXACT set of routes a zww_ token may mutate, and the
# proof that it can reach those and ONLY those. A new mutating route
# defaults to owner-only; joining this list is a deliberate, reviewed
# edit — that is the whole point of require_shared_write being opt-in.

SHARED_WRITE_BY_DESIGN = {
    ("PUT", "/api/devices/{mac}/alert"),
    ("PUT", "/api/devices/{mac}/location"),
    ("PUT", "/api/devices/{mac}/name"),
    ("PUT", "/api/devices/{mac}/wu-station"),
    ("POST", "/api/alerts/test"),
    ("POST", "/api/alerts/rules"),
    ("PATCH", "/api/alerts/rules/{rule_id}"),
    ("DELETE", "/api/alerts/rules/{rule_id}"),
    ("POST", "/api/push/register"),
    ("POST", "/api/push/unregister"),
    ("POST", "/api/push/live-activity-token"),
    ("POST", "/api/storm/watch/start"),
}


def test_shared_write_surface_is_exactly_the_designed_set():
    actual = {(m, p) for g, m, p, _ in _routes() if g == "SHARED-WRITE"}
    assert actual == SHARED_WRITE_BY_DESIGN, (
        "the write-share route surface changed — additions/removals here "
        f"are a security decision, not a refactor. diff: "
        f"extra={actual - SHARED_WRITE_BY_DESIGN}, "
        f"missing={SHARED_WRITE_BY_DESIGN - actual}")
    # Nothing administrative may ever join: the admin route FAMILIES are
    # categorically outside this tier. Prefix-matched, not substring — a
    # station-ops path may legitimately contain the word "token"
    # (/api/push/live-activity-token is the holder's own phone).
    forbidden_prefixes = ("/api/guest-tokens", "/api/ingest-tokens",
                          "/api/config", "/api/backup", "/api/update",
                          "/api/history-retention", "/api/webhooks",
                          "/api/integrations", "/api/sharing",
                          "/api/import", "/api/write-audit")
    for method, path in actual:
        assert not path.startswith(forbidden_prefixes), (
            f"{path} is an administrative surface and must stay owner-only")
        assert (method, path) != ("DELETE", "/api/devices/{mac}"), (
            "device deletion purges history — owner-only, always")


@pytest.fixture
def write_share_client(temp_env):
    """A client plus a freshly minted zww_ token."""
    import importlib, sys
    for mod in ["app.config", "app.db", "app.insights", "app.wu_upload",
                "app.capture", "app.ingest", "app.discovery",
                "app.alerts", "app.apns", "app.relay", "app.integrations",
                "app.main"]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/guest-tokens", headers=H_PRIMARY,
                   json={"label": "Invariant Suite", "write": True})
        assert r.status_code == 200
        yield c, r.json()["token"]


_OWNER_ONLY_CASES = sorted({(m, p) for g, m, p, _ in _routes()
                            if g == "WRITE"
                            and m in {"POST", "PUT", "PATCH", "DELETE"}})
assert _OWNER_ONLY_CASES, "route scan found no owner-only writes — scanner broke"


@pytest.mark.parametrize("method,path", _OWNER_ONLY_CASES)
def test_a_write_share_token_cannot_reach_owner_routes(write_share_client,
                                                       method, path):
    """The tier boundary itself: a write link must never mint tokens, touch
    credentials/backups/updates, or delete a device. 403 (valid but not
    permitted), never 200."""
    client, token = write_share_client
    url = path.replace("{mac}", "AA:BB:CC:DD:EE:FF") \
              .replace("{rule_id}", "1").replace("{token_id}", "zwg_00000000") \
              .replace("{wid}", "x").replace("{hook_id}", "x") \
              .replace("{provider}", "awn").replace("{target}", "wu")
    r = client.request(method, url,
                       headers={"Authorization": f"Bearer {token}"}, json={})
    assert r.status_code in (401, 403), (
        f"{method} {path} accepted a write-SHARE token ({r.status_code})")


_SHARED_CASES = sorted(SHARED_WRITE_BY_DESIGN)


@pytest.mark.parametrize("method,path", _SHARED_CASES)
def test_a_write_share_token_reaches_every_station_ops_route(
        write_share_client, method, path):
    """The positive half, or the negative half proves nothing: on every
    designed station-ops route the zww_ token must get PAST auth — any
    status except 401/403 (validation 422s and unknown-mac 404s are the
    handler speaking, which means auth admitted us)."""
    client, token = write_share_client
    url = path.replace("{mac}", "AA:BB:CC:DD:EE:FF").replace("{rule_id}", "1")
    r = client.request(method, url,
                       headers={"Authorization": f"Bearer {token}"}, json={})
    # Membership in an expected set, not `not in (401, 403)` (R12): a 500
    # from a crashing handler used to count as "kept its route".
    assert r.status_code in (200, 201, 204, 400, 404, 409, 422), (
        f"{method} {path} answered {r.status_code} to the write-share "
        "token — 401/403 means the tier lost a designed route; 5xx means "
        "the handler is broken, which this sweep must not paper over")


def test_no_read_route_hands_a_guest_the_source_blob(client):
    """§5 row 11, a sweep rather than two named sites: every GET route
    that takes a station is called with a guest bearer, for a station
    whose newest reading carries a `_source` with exact coordinates, and
    none of the answers may contain it. Routes that need more than a mac
    are called with what a client would send by default; a refusal is
    fine, a body with `_source` is not."""
    import asyncio
    import time as _t
    from app import db
    from app.main import app as _app
    client.post("/ingest/custom", headers={"Authorization": "Bearer test-ingest-token"}, json={
        "device": {"id": "AABBCCDDEEFF", "name": "Yard"},
        "timestamp_utc": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
        "outdoor": {"tempf": 80.0, "humidity": 30},
        "coords": {"lat": 33.3004, "lon": -111.9378, "location": "123 Elm St"},
        "source": "test"})
    mac = "AA:BB:CC:DD:EE:FF"
    own = client.get(f"/api/devices/{mac}/current", headers=_OWNER).json()
    assert "_source" in own, "the fixture must carry a source blob to be worth sweeping"
    guest_tok = "zwg_" + "ab" * 16
    asyncio.run(db.add_guest_token(guest_tok, "Sweep", int(_t.time() * 1000)))
    gh = {"Authorization": f"Bearer {guest_tok}"}
    swept = 0
    for route in _app.routes:
        path = getattr(route, "path", "")
        if "GET" not in (getattr(route, "methods", None) or ()) or "{mac}" not in path:
            continue
        url = path.replace("{mac}", mac)
        for k in ("{field}", "{year}", "{kind}", "{id}", "{day}"):
            url = url.replace(k, "tempf" if k == "{field}" else "2026")
        r = client.get(url, headers=gh, params={"hours": 24, "year": 2026, "field": "tempf"})
        assert "_source" not in r.text, (path, r.status_code)
        swept += 1
    assert swept >= 5, swept
    # MCP list_stations: the owner sees the poster's payload, a guest never.
    mcp_h = {"Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": "2025-06-18"}

    def rpc(c, method, params, headers):
        return c.post("/mcp", headers=headers,
                      json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r = rpc(client, "tools/call", {"name": "list_stations", "arguments": {}},
            headers={**mcp_h, **gh})
    assert "_source" not in r.text and "123 Elm" not in r.text
    r = rpc(client, "tools/call", {"name": "list_stations", "arguments": {}},
            headers={**mcp_h, **_OWNER})
    st = r.json()["result"]["structuredContent"]["stations"][0]
    assert st["source"] is not None, "the owner's list names the source (BE-F12)"
