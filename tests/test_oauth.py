"""OAuth 2.1 for the MCP server (2.1, app/oauth.py).

The backend is its own authorization server so claude.ai and ChatGPT
connectors can reach POST /mcp. This suite walks the whole flow the MCP
authorization spec (2025-06-18) prescribes — discovery, registration,
consent, PKCE exchange, use, refresh rotation, revocation — and pins the
refusals that keep it safe: wrong token issues no code, wrong verifier
issues no token, unregistered redirect never redirects, plain PKCE is
refused, a spent code is spent, an expired token is 401 with the
challenge, and the consent POST is rate-limited.
"""
import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import pytest

H = {"Authorization": "Bearer test-api-token"}
MCP_H = {"Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def _fresh_limiter():
    from app import oauth
    oauth.reset_state()
    yield
    oauth.reset_state()


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(client, uris=(REDIRECT,), name="Claude", approve=True):
    r = client.post("/oauth/register", json={
        "client_name": name, "redirect_uris": list(uris),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"]})
    assert r.status_code == 201, r.text
    reg = r.json()
    if approve:
        # The owner's approval (SEC-1): until it, the consent page takes
        # only a connect code. Most tests exercise the approved path.
        a = client.post(f"/api/oauth/clients/{reg['client_id']}/approve", headers=H)
        assert a.status_code == 200, a.text
    return reg


def _authorize_params(client_id, challenge, state="xyz", **extra):
    p = {"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
         "code_challenge": challenge, "code_challenge_method": "S256",
         "scope": "weather:read", "state": state,
         "resource": "http://testserver/mcp"}
    p.update(extra)
    return p


def _consent(client, params, token):
    return client.post("/oauth/authorize", data={**params, "token": token},
                       follow_redirects=False)


def _code_from(resp):
    assert resp.status_code == 302, resp.text
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    return q


def _exchange(client, client_id, code, verifier, redirect=REDIRECT):
    return client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect, "client_id": client_id,
        "code_verifier": verifier, "resource": "http://testserver/mcp"})


def _login(client, token="test-api-token"):
    reg = _register(client)
    verifier, challenge = _pkce()
    q = _code_from(_consent(client, _authorize_params(reg["client_id"], challenge), token))
    r = _exchange(client, reg["client_id"], q["code"][0], verifier)
    assert r.status_code == 200, r.text
    return reg["client_id"], r.json()


def _initialize(client, access):
    return client.post("/mcp", headers={**MCP_H, "Authorization": f"Bearer {access}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18",
                                        "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}}})


# ── discovery ─────────────────────────────────────────────────────────

def test_protected_resource_metadata_names_this_server(client):
    for path in ("/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-protected-resource"):
        r = client.get(path)
        assert r.status_code == 200
        body = r.json()
        assert body["resource"] == "http://testserver/mcp"
        assert body["authorization_servers"] == ["http://testserver"]
        assert body["scopes_supported"] == ["weather:read"]
        assert body["bearer_methods_supported"] == ["header"]


def test_authorization_server_metadata_is_rfc8414_shaped(client):
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["issuer"] == "http://testserver"
    assert body["authorization_endpoint"] == "http://testserver/oauth/authorize"
    assert body["token_endpoint"] == "http://testserver/oauth/token"
    assert body["registration_endpoint"] == "http://testserver/oauth/register"
    assert body["response_types_supported"] == ["code"]
    assert set(body["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]


def test_issuer_follows_the_edge_proto(client):
    body = client.get("/.well-known/oauth-authorization-server",
                      headers={"X-Forwarded-Proto": "https"}).json()
    assert body["issuer"] == "https://testserver"


def test_an_unauthenticated_mcp_request_advertises_the_metadata(client):
    r = client.post("/mcp", headers=MCP_H, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    www = r.headers["WWW-Authenticate"]
    assert www.startswith("Bearer ")
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in www


# ── registration ──────────────────────────────────────────────────────

def test_registration_returns_a_public_client(client):
    reg = _register(client)
    assert reg["client_id"].startswith("zwo_")
    assert reg["token_endpoint_auth_method"] == "none"
    assert reg["redirect_uris"] == [REDIRECT]
    assert reg["client_name"] == "Claude"


@pytest.mark.parametrize("uri", ["http://evil.example/cb", "ftp://x/y",
                                 "https://ok.example/cb#frag", "not a url"])
def test_registration_refuses_bad_redirects(client, uri):
    r = client.post("/oauth/register", json={"redirect_uris": [uri]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_registration_allows_loopback_http(client):
    reg = _register(client, uris=("http://localhost:3334/callback", "http://127.0.0.1/cb"))
    assert len(reg["redirect_uris"]) == 2


def test_registration_refuses_confidential_clients(client):
    r = client.post("/oauth/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_basic"})
    assert r.status_code == 400


# ── consent ───────────────────────────────────────────────────────────

def test_the_consent_page_names_the_client_and_asks_for_a_token(client):
    reg = _register(client, name="Claude <script>")
    _, challenge = _pkce()
    r = client.get("/oauth/authorize", params=_authorize_params(reg["client_id"], challenge))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Claude &lt;script&gt;" in r.text and "<script>" not in r.text
    assert 'name="token" type="password"' in r.text
    assert "claude.ai" in r.text, "the page names where the grant will be sent"
    assert r.headers["cache-control"] == "no-store"


def test_an_unknown_client_or_unregistered_redirect_never_redirects(client):
    reg = _register(client)
    _, challenge = _pkce()
    r = client.get("/oauth/authorize", params=_authorize_params("zwo_nope", challenge),
                   follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers
    r = client.get("/oauth/authorize",
                   params=_authorize_params(reg["client_id"], challenge,
                                            redirect_uri="https://claude.ai/other"),
                   follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers


def test_plain_pkce_and_wrong_resource_are_refused_via_redirect(client):
    reg = _register(client)
    _, challenge = _pkce()
    r = client.get("/oauth/authorize",
                   params=_authorize_params(reg["client_id"], challenge,
                                            code_challenge_method="plain"),
                   follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["error"] == ["invalid_request"] and q["state"] == ["xyz"]
    r = client.get("/oauth/authorize",
                   params=_authorize_params(reg["client_id"], challenge,
                                            resource="https://other.example/mcp"),
                   follow_redirects=False)
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_target"]


def test_a_wrong_token_issues_no_code(client):
    reg = _register(client)
    _, challenge = _pkce()
    r = _consent(client, _authorize_params(reg["client_id"], challenge), "not-the-token")
    assert r.status_code == 401
    assert "location" not in r.headers
    assert "not one this server knows" in r.text
    assert "not-the-token" not in r.text


def test_the_right_token_redirects_with_code_and_state(client):
    reg = _register(client)
    _, challenge = _pkce()
    r = _consent(client, _authorize_params(reg["client_id"], challenge), "test-api-token")
    q = _code_from(r)
    assert q["state"] == ["xyz"]
    assert q["code"][0].startswith("zwx_")
    assert r.headers["location"].startswith(REDIRECT + "?")


def test_the_consent_post_is_rate_limited(client, monkeypatch):
    from app import oauth
    monkeypatch.setattr(oauth, "AUTHORIZE_PER_IP", 3)
    reg = _register(client)
    _, challenge = _pkce()
    for _ in range(3):
        assert _consent(client, _authorize_params(reg["client_id"], challenge), "x").status_code == 401
    r = _consent(client, _authorize_params(reg["client_id"], challenge), "test-api-token")
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"


# ── token exchange ────────────────────────────────────────────────────

def test_wrong_verifier_fails_and_spends_the_code(client):
    reg = _register(client)
    verifier, challenge = _pkce()
    q = _code_from(_consent(client, _authorize_params(reg["client_id"], challenge), "test-api-token"))
    r = _exchange(client, reg["client_id"], q["code"][0], secrets.token_urlsafe(48))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # Spent: the right verifier no longer helps.
    r = _exchange(client, reg["client_id"], q["code"][0], verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_redirect_uri_mismatch_at_exchange_is_refused(client):
    reg = _register(client, uris=(REDIRECT, "https://claude.ai/two"))
    verifier, challenge = _pkce()
    q = _code_from(_consent(client, _authorize_params(reg["client_id"], challenge), "test-api-token"))
    r = _exchange(client, reg["client_id"], q["code"][0], verifier, redirect="https://claude.ai/two")
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_an_unknown_client_at_the_token_endpoint_is_401(client):
    r = client.post("/oauth/token", data={"grant_type": "authorization_code",
                                          "client_id": "zwo_nope", "code": "x",
                                          "code_verifier": "y" * 43})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    assert "WWW-Authenticate" in r.headers


def test_the_full_flow_ends_with_a_working_mcp_session(client):
    client_id, tok = _login(client)
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600
    assert tok["access_token"].startswith("zwa_") and tok["refresh_token"].startswith("zwr_")
    assert tok["scope"] == "weather:read"
    r = _initialize(client, tok["access_token"])
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "zasder-weather"
    # The owner sees the connection, never its tokens.
    listed = client.get("/api/oauth/clients", headers=H).json()["clients"]
    assert [c["client_id"] for c in listed] == [client_id]
    assert listed[0]["role"] == "owner" and listed[0]["sessions"] == 1
    assert "zwa_" not in str(listed) and "zwr_" not in str(listed)


def test_a_guest_link_token_yields_a_guest_session(client):
    import asyncio
    from app import db
    asyncio.run(db.add_guest_token("zwg_" + "ab" * 16, "Doren", int(time.time() * 1000)))
    _, tok = _login(client, token="zwg_" + "ab" * 16)
    assert _initialize(client, tok["access_token"]).status_code == 200
    listed = client.get("/api/oauth/clients", headers=H).json()["clients"]
    assert listed[0]["role"] == "guest"


def test_an_oauth_token_never_opens_a_write_route(client):
    _, tok = _login(client)
    r = client.put("/api/config/server-name",
                   headers={"Authorization": f"Bearer {tok['access_token']}"},
                   json={"name": "pwned"})
    assert r.status_code == 401
    r = client.get("/api/devices", headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code == 401, "OAuth tokens are for /mcp, not the REST surface"


def test_an_expired_access_token_is_401_with_the_challenge(client):
    import asyncio
    from app import db
    _, tok = _login(client)

    async def expire():
        async with db.connect() as conn:
            await conn.execute("UPDATE oauth_tokens SET expires_ms = 1 WHERE kind = 'access'")
            await conn.commit()
    asyncio.run(expire())
    r = _initialize(client, tok["access_token"])
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["WWW-Authenticate"]


def test_refresh_rotates_and_the_old_refresh_token_dies(client):
    client_id, tok = _login(client)
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": tok["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"] != tok["access_token"]
    assert new["refresh_token"] != tok["refresh_token"]
    assert _initialize(client, new["access_token"]).status_code == 200
    assert _initialize(client, tok["access_token"]).status_code == 401, \
        "the old family died with the rotation"
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": tok["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_revoke_kills_the_session(client):
    client_id, tok = _login(client)
    r = client.post("/oauth/revoke", data={"token": tok["refresh_token"], "client_id": client_id})
    assert r.status_code == 200
    assert _initialize(client, tok["access_token"]).status_code == 401


def test_the_owner_can_cut_a_client_off(client):
    client_id, tok = _login(client)
    assert client.delete(f"/api/oauth/clients/{client_id}", headers=H).status_code == 200
    assert _initialize(client, tok["access_token"]).status_code == 401
    assert client.get("/api/oauth/clients", headers=H).json()["clients"] == []
    assert client.delete(f"/api/oauth/clients/{client_id}", headers=H).status_code == 404
    # A reviewer / read-only token cannot manage clients.
    assert client.get("/api/oauth/clients").status_code == 401


def test_the_sweep_drops_expired_rows_and_idle_clients(client):
    import asyncio
    from app import db, oauth
    client_id, _ = _login(client)
    far = int(time.time() * 1000) + oauth.REFRESH_TTL_MS + oauth.CLIENT_IDLE_TTL_MS + 1
    out = asyncio.run(oauth.sweep_expired(far))
    assert out["tokens"] == 2 and out["clients"] == 1

    async def count():
        async with db.connect() as conn:
            return (await (await conn.execute("SELECT COUNT(*) FROM oauth_clients")).fetchone())[0]
    assert asyncio.run(count()) == 0
    assert client_id  # (the id is gone with the client)


def test_the_bearer_path_is_unchanged(client):
    """The API token keeps working on /mcp exactly as before OAuth."""
    r = client.post("/mcp", headers={**MCP_H, **H},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 200


# ── approval, connect codes, the open-redirect closure (SEC-1) ────────

def test_an_unapproved_client_gets_the_pending_page_and_no_token_field(client):
    reg = _register(client, approve=False)
    _, challenge = _pkce()
    r = client.get("/oauth/authorize", params=_authorize_params(reg["client_id"], challenge))
    assert r.status_code == 200
    assert "waiting for the server owner" in r.text
    assert "Do not type your API token" in r.text
    assert "Allow read access" not in r.text
    # The API token is refused outright on the pending page: no code.
    r = _consent(client, _authorize_params(reg["client_id"], challenge), "test-api-token")
    assert r.status_code == 401 and "location" not in r.headers
    assert "approval first" in r.text          # (the apostrophe is HTML-escaped)
    listed = client.get("/api/oauth/clients", headers=H).json()["clients"]
    assert listed[0]["approved"] is False


def test_a_malformed_request_for_an_unapproved_client_never_redirects(client):
    """The open redirect (SEC-1): before approval, a bad response_type,
    method, scope or resource gets an error PAGE on this origin, not a
    302 to the client's own address."""
    reg = _register(client, approve=False)
    _, challenge = _pkce()
    for bad in ({"response_type": "token"}, {"code_challenge_method": "plain"},
                {"code_challenge": "short"}, {"code_challenge": "*" * 43},
                {"scope": "weather:write"}, {"resource": "https://other.example/mcp"}):
        r = client.get("/oauth/authorize",
                       params=_authorize_params(reg["client_id"], challenge, **bad),
                       follow_redirects=False)
        assert r.status_code == 403 and "location" not in r.headers, bad


def test_a_connect_code_approves_and_connects_in_one_step(client):
    reg = _register(client, approve=False)
    verifier, challenge = _pkce()
    code = client.post("/api/oauth/connect-code", headers=H).json()
    assert code["code"].startswith("zwc_") and code["expires_in"] == 600
    r = _consent(client, _authorize_params(reg["client_id"], challenge), code["code"])
    q = _code_from(r)
    listed = client.get("/api/oauth/clients", headers=H).json()["clients"]
    assert listed[0]["approved"] is True
    tok = _exchange(client, reg["client_id"], q["code"][0], verifier)
    assert tok.status_code == 200
    assert _initialize(client, tok.json()["access_token"]).status_code == 200
    assert listed[0]["role"] in (None, "owner")
    # One use only.
    reg2 = _register(client, approve=False)
    r = _consent(client, _authorize_params(reg2["client_id"], challenge), code["code"])
    assert r.status_code == 401 and "location" not in r.headers


def test_a_connect_code_works_on_an_approved_client_too(client):
    reg = _register(client)
    _, challenge = _pkce()
    code = client.post("/api/oauth/connect-code", headers=H).json()["code"]
    assert _code_from(_consent(client, _authorize_params(reg["client_id"], challenge), code))
    # Minting is owner-only.
    assert client.post("/api/oauth/connect-code").status_code == 401


def test_a_connect_code_minted_for_one_client_approves_only_that_client(client):
    """SEC-G5 (2.2): the owner mints the code for the pending app they are
    looking at; a look-alike registration cannot spend it, and the code is
    still there for the right one. An unbound code keeps working anywhere."""
    good = _register(client, name="Codex", approve=False)
    imposter = _register(client, name="Codex ", approve=False)
    _, challenge = _pkce()
    minted = client.post("/api/oauth/connect-code", headers=H,
                         json={"client_id": good["client_id"]}).json()
    assert minted["client_id"] == good["client_id"]
    code = minted["code"]
    # The imposter's page refuses it and stays unapproved.
    page = _consent(client, _authorize_params(imposter["client_id"], challenge), code)
    assert page.status_code == 401
    assert "not a connect code" in page.text
    # The intended client spends it: approved and connected in one step.
    assert _code_from(_consent(client, _authorize_params(good["client_id"], challenge), code))
    # Once spent, gone.
    assert _consent(client, _authorize_params(good["client_id"], challenge), code).status_code == 401
    # An unbound code still works for anyone, as before.
    loose = client.post("/api/oauth/connect-code", headers=H).json()
    assert loose["client_id"] is None
    assert _code_from(_consent(client, _authorize_params(imposter["client_id"], challenge), loose["code"]))


def test_the_owner_approve_route_is_write_gated_and_404s_unknowns(client):
    assert client.post("/api/oauth/clients/zwo_nope/approve", headers=H).status_code == 404
    reg = _register(client, approve=False)
    assert client.post(f"/api/oauth/clients/{reg['client_id']}/approve").status_code == 401


# ── T1: a code is bound to the client it was issued to ────────────────

def test_a_code_issued_to_client_a_cannot_be_redeemed_by_client_b(client):
    a = _register(client, name="A")
    b = _register(client, name="B")
    verifier, challenge = _pkce()
    q = _code_from(_consent(client, _authorize_params(a["client_id"], challenge), "test-api-token"))
    r = _exchange(client, b["client_id"], q["code"][0], verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # And it is spent for A too: a code presented is a code gone.
    r = _exchange(client, a["client_id"], q["code"][0], verifier)
    assert r.status_code == 400


# ── T5: the limits bite ───────────────────────────────────────────────

def test_registration_and_token_limits_and_the_client_cap_bite(client, monkeypatch):
    from app import oauth
    monkeypatch.setattr(oauth, "REGISTER_PER_IP", 2)
    _register(client, approve=False)
    _register(client, approve=False)
    r = client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    assert r.status_code == 429
    oauth.reset_state()

    monkeypatch.setattr(oauth, "AUTHORIZE_GET_PER_IP", 2)
    reg = _register(client)
    _, challenge = _pkce()
    for _ in range(2):
        assert client.get("/oauth/authorize",
                          params=_authorize_params(reg["client_id"], challenge)).status_code == 200
    assert client.get("/oauth/authorize",
                      params=_authorize_params(reg["client_id"], challenge)).status_code == 429
    oauth.reset_state()

    monkeypatch.setattr(oauth, "TOKEN_PER_IP", 2)
    for _ in range(2):
        client.post("/oauth/token", data={"grant_type": "x", "client_id": reg["client_id"]})
    assert client.post("/oauth/token", data={"grant_type": "x",
                                             "client_id": reg["client_id"]}).status_code == 429
    assert client.post("/oauth/revoke", data={"token": "zwa_x",
                                              "client_id": reg["client_id"]}).status_code == 429
    oauth.reset_state()
    monkeypatch.setattr(oauth, "TOKEN_PER_IP", 60)

    # CLIENTS_MAX: with every client holding a live session, a new
    # registration is refused rather than evicting a working one.
    monkeypatch.setattr(oauth, "CLIENTS_MAX", 1)
    for cid in [c["client_id"] for c in client.get("/api/oauth/clients", headers=H).json()["clients"]]:
        client.delete(f"/api/oauth/clients/{cid}", headers=H)
    _login(client)                              # one client, one live session
    r = client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    assert r.status_code == 503, r.text


# ── T6: single use under concurrency ──────────────────────────────────

def _two_at_once(fn):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(lambda _: fn(), range(2)))


def test_two_concurrent_exchanges_of_one_code_yield_one_token(client):
    reg = _register(client)
    verifier, challenge = _pkce()
    q = _code_from(_consent(client, _authorize_params(reg["client_id"], challenge), "test-api-token"))
    codes = [r.status_code for r in
             _two_at_once(lambda: _exchange(client, reg["client_id"], q["code"][0], verifier))]
    assert sorted(codes) == [200, 400], codes


def test_two_concurrent_rotations_of_one_refresh_token_yield_one_success(client):
    client_id, tok = _login(client)
    codes = [r.status_code for r in _two_at_once(lambda: client.post(
        "/oauth/token", data={"grant_type": "refresh_token",
                              "refresh_token": tok["refresh_token"],
                              "client_id": client_id}))]
    assert sorted(codes) == [200, 400], codes


# ── T10 / SEC-3: rotation keeps the family; reuse revokes it ──────────

def test_reuse_of_a_rotated_refresh_token_revokes_the_whole_family(client):
    import asyncio
    from app import db
    client_id, tok = _login(client)
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": tok["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 200
    new = r.json()

    async def families():
        async with db.connect() as conn:
            rows = await (await conn.execute(
                "SELECT DISTINCT family FROM oauth_tokens WHERE client_id = ?",
                (client_id,))).fetchall()
            return {r[0] for r in rows}
    assert len(asyncio.run(families())) == 1, "rotation must keep the family"
    assert _initialize(client, new["access_token"]).status_code == 200
    # The stolen (or retried) old refresh token comes back: breach.
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": tok["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 400 and "revoked" in r.json()["error_description"]
    assert _initialize(client, new["access_token"]).status_code == 401, \
        "the live session of the same family must die with the reuse"
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": new["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 400


# ── SEC-5: identity comes from configuration, not the Host header ─────

def test_a_configured_public_base_url_fixes_the_issuer(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_base_url", "https://weather.example.com/")
    doc = client.get("/.well-known/oauth-authorization-server",
                     headers={"Host": "evil.example"}).json()
    assert doc["issuer"] == "https://weather.example.com"
    pr = client.get("/.well-known/oauth-protected-resource/mcp",
                    headers={"Host": "evil.example"}).json()
    assert pr["resource"] == "https://weather.example.com/mcp"
    r = client.post("/mcp", headers={"Host": "evil.example", **MCP_H},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert "weather.example.com" in r.headers["WWW-Authenticate"]
    assert "evil" not in r.headers["WWW-Authenticate"]


def test_a_host_header_that_is_not_a_host_is_refused_not_echoed(client, monkeypatch):
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    r = client.get("/.well-known/oauth-authorization-server",
                   headers={"Host": 'evil"x'})
    assert r.status_code == 400
    # The 401 is still a 401 (round-two review, SEC-F5): the challenge
    # simply carries no metadata URL, since none can be built from that
    # Host and no origin is configured.
    r = client.post("/mcp", headers={"Host": 'evil"x', **MCP_H},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    challenge = r.headers.get("WWW-Authenticate", "")
    assert challenge.startswith("Bearer") and '"x' not in challenge
    assert "resource_metadata" not in challenge


def test_an_access_token_is_bound_to_the_audience_it_was_issued_for(client):
    _, tok = _login(client)
    ok = client.post("/mcp", headers={**MCP_H, "Authorization": f"Bearer {tok['access_token']}"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert ok.status_code == 200
    other = client.post("/mcp", headers={**MCP_H, "Host": "other.example",
                                         "Authorization": f"Bearer {tok['access_token']}"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert other.status_code == 401, "issued for testserver/mcp, presented elsewhere"


# ── SEC-6: the advice route is owner-only ─────────────────────────────

def test_server_advice_is_write_gated(client):
    import asyncio
    import time
    from app import db
    guest_tok = "zwg_" + "ef" * 16
    asyncio.run(db.add_guest_token(guest_tok, "Doren", int(time.time() * 1000)))
    # require_write_token answers a read-only token with 403, not 401.
    assert client.get("/api/server/advice",
                      headers={"Authorization": f"Bearer {guest_tok}"}).status_code == 403
    assert client.get("/api/server/advice", headers=H).status_code == 200


# ── round-two review: SEC-F3, SEC-F5, SEC-F6, the nits, §5 rows 9–10 ──

def _rotate(client, client_id, refresh):
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": refresh,
                                          "client_id": client_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_reuse_detection_survives_rotation_churn(client):
    """SEC-F3: forty-six rotations after a theft used to evict the consumed
    tombstone through the per-client cap, so the victim's replay read as
    "unknown" and the attacker's session lived on. Tombstones have their
    own bound now."""
    from app import oauth
    client_id, first = _login(client)
    cur = _rotate(client, client_id, first["refresh_token"])   # the "thief"
    for _ in range(oauth.TOKENS_PER_CLIENT_MAX + 6):
        cur = _rotate(client, client_id, cur["refresh_token"])
    assert _initialize(client, cur["access_token"]).status_code == 200
    # The victim's copy of the ORIGINAL refresh token comes back.
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": first["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 400 and "revoked" in r.json()["error_description"], r.text
    assert _initialize(client, cur["access_token"]).status_code == 401, \
        "the attacker's live session must die with the reuse"


def test_reuse_detection_survives_a_day_offline(client, monkeypatch):
    """SEC-F3: the consumed row used to be clipped to 24 h; a client that
    was offline for a day then replayed read "unknown", not "reuse"."""
    from app import oauth
    client_id, first = _login(client)
    new = _rotate(client, client_id, first["refresh_token"])
    later = oauth._now_ms() + 25 * 3_600_000
    monkeypatch.setattr(oauth, "_now_ms", lambda: later)
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": first["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 400 and "revoked" in r.json()["error_description"], r.text
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": new["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 400, "the family died with the reuse"


def test_a_tombstone_is_not_a_session(client):
    """A consumed refresh row must not count as a login on the owner's
    screen nor keep an idle client alive."""
    import asyncio
    from app import oauth
    client_id, first = _login(client)
    _rotate(client, client_id, first["refresh_token"])
    listed = client.get("/api/oauth/clients", headers=H).json()["clients"]
    assert listed[0]["sessions"] == 1
    # Revoke the live family by hand; the tombstone stays, the client is
    # idle, the sweep must still remove it once it is old enough.
    r = client.post("/oauth/revoke",
                    data={"token": first["access_token"], "client_id": client_id})
    assert r.status_code == 200
    far = oauth._now_ms() + oauth.CLIENT_IDLE_TTL_MS + 60_000
    out = asyncio.run(oauth.sweep_expired(now_ms=far))
    assert out["clients"] == 1, out


def test_the_shipped_rate_limits_are_what_the_docs_say(client):
    """§5 row 10: the tests lower these to trip them; the constants
    themselves were asserted nowhere."""
    from app import oauth
    assert oauth.REGISTER_PER_IP == 10
    assert oauth.AUTHORIZE_PER_IP == 10
    assert oauth.AUTHORIZE_GET_PER_IP == 30
    assert oauth.TOKEN_PER_IP == 60
    assert oauth.RATE_WINDOW_MS == 60_000
    assert oauth.CONNECT_TTL_MS == 10 * 60_000
    assert oauth.TOKENS_PER_CLIENT_MAX == 40
    assert oauth.TOMBSTONES_PER_CLIENT_MAX == 5000


def test_a_connect_code_expires_after_its_ttl(client, monkeypatch):
    from app import oauth
    reg = _register(client, approve=False)
    _, challenge = _pkce()
    code = client.post("/api/oauth/connect-code", headers=H).json()["code"]
    later = oauth._now_ms() + oauth.CONNECT_TTL_MS + 1_000
    monkeypatch.setattr(oauth, "_now_ms", lambda: later)
    r = client.post("/oauth/authorize",
                    data={**_authorize_params(reg["client_id"], challenge),
                          "connect_code": code}, follow_redirects=False)
    assert r.status_code == 401 and "location" not in r.headers
    listed = client.get("/api/oauth/clients", headers=H).json()["clients"]
    assert listed[0]["approved"] is False


def test_the_pending_page_takes_the_code_in_its_own_field(client):
    """The pending consent page's input is not the token field: a
    password manager that saved the API token on the approved page must
    not offer it on the page that says not to type it (SEC nit)."""
    reg = _register(client, approve=False)
    _, challenge = _pkce()
    page = client.get("/oauth/authorize",
                      params=_authorize_params(reg["client_id"], challenge)).text
    assert 'name="connect_code"' in page and 'autocomplete="one-time-code"' in page
    assert 'name="token"' not in page
    code = client.post("/api/oauth/connect-code", headers=H).json()["code"]
    r = client.post("/oauth/authorize",
                    data={**_authorize_params(reg["client_id"], challenge),
                          "connect_code": code}, follow_redirects=False)
    assert r.status_code == 302, r.text
    approved = _register(client)
    page = client.get("/oauth/authorize",
                      params=_authorize_params(approved["client_id"], challenge)).text
    assert 'name="token"' in page and 'name="connect_code"' not in page


def test_fly_app_name_is_the_default_origin(client, monkeypatch):
    """SEC-F6: unset PUBLIC_BASE_URL on Fly used to mean Host reflection;
    the app's own hostname is known and is the default now — when the
    process is provably on Fly (FLY_APP_NAME corroborated by
    FLY_MACHINE_ID, round-three review SEC-G2); the app name alone is a
    string anyone can put in an env file and proves nothing."""
    from app.config import settings
    monkeypatch.setattr(settings, "public_base_url", None)
    monkeypatch.setenv("FLY_APP_NAME", "zasder-weather-test")
    monkeypatch.setenv("ALLOWED_HOSTS", "*")
    doc = client.get("/.well-known/oauth-authorization-server",
                     headers={"Host": "evil.example"}).json()
    assert doc["issuer"] == "http://evil.example", "an app name alone is not Fly"
    monkeypatch.setenv("FLY_MACHINE_ID", "e28465f1a2b3c4")
    doc = client.get("/.well-known/oauth-authorization-server",
                     headers={"Host": "evil.example"}).json()
    assert doc["issuer"] == "https://zasder-weather-test.fly.dev"
    r = client.post("/mcp", headers={"Host": 'evil"x', **MCP_H},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert "zasder-weather-test.fly.dev" in r.headers["WWW-Authenticate"]


def test_a_malformed_public_base_url_is_ignored_and_logged(client, monkeypatch, caplog):
    """§5 row 9."""
    import logging
    from app import oauth
    from app.config import settings
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.setattr(settings, "public_base_url", 'ftp://weather"x')
    with caplog.at_level(logging.WARNING, logger="app.oauth"):
        assert oauth.configured_origin() is None
    assert any("PUBLIC_BASE_URL" in rec.getMessage() for rec in caplog.records)
    doc = client.get("/.well-known/oauth-authorization-server",
                     headers={"Host": "weather.example.org"}).json()
    assert doc["issuer"] == "http://weather.example.org"


def test_the_last_used_stamp_updates_once_per_interval_and_is_bounded(client, monkeypatch):
    """§5 row 9: a chatty assistant must not take the writer on every
    call, and the stamp cache cannot grow for the process lifetime."""
    import asyncio
    from app import db, oauth
    client_id, tok = _login(client)
    h = oauth._hash(tok["access_token"])

    async def stamp():
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT last_used_ms FROM oauth_tokens WHERE token_hash = ?", (h,))).fetchone()
            return row["last_used_ms"]
    assert _initialize(client, tok["access_token"]).status_code == 200
    first = asyncio.run(stamp())
    assert first is not None
    base = oauth._now_ms()
    monkeypatch.setattr(oauth, "_now_ms", lambda: base + 30_000)
    assert _initialize(client, tok["access_token"]).status_code == 200
    assert asyncio.run(stamp()) == first, "an UPDATE ran inside the interval"
    monkeypatch.setattr(oauth, "_now_ms", lambda: base + oauth.LAST_USED_STAMP_EVERY_MS + 1_000)
    assert _initialize(client, tok["access_token"]).status_code == 200
    assert asyncio.run(stamp()) > first
    # Bounded: a burst of distinct tokens is pruned back below the cap.
    now = oauth._now_ms()
    oauth._LAST_USED_STAMP.clear()
    for i in range(oauth.LAST_USED_STAMP_MAX):
        oauth._LAST_USED_STAMP[f"t{i}"] = now - oauth.LAST_USED_STAMP_EVERY_MS - 1
    oauth._prune_last_used_stamps(now)
    assert len(oauth._LAST_USED_STAMP) < oauth.LAST_USED_STAMP_MAX


def test_the_late_columns_are_added_to_a_table_from_an_earlier_build(temp_env):
    """§5 row 9: the 2.1-beta upgrade path. A database whose oauth tables
    predate approval, consumed-refresh marking and the audience must be
    current after init_db, with no separate migration step."""
    import asyncio
    import sqlite3
    con = sqlite3.connect(temp_env)
    try:
        con.executescript("""
        CREATE TABLE oauth_clients (
            client_id TEXT PRIMARY KEY, client_name TEXT, redirect_uris TEXT NOT NULL,
            created_ms INTEGER NOT NULL, last_used_ms INTEGER);
        CREATE TABLE oauth_tokens (
            token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
            scope TEXT NOT NULL, role TEXT NOT NULL, family TEXT NOT NULL,
            created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL, last_used_ms INTEGER);
        INSERT INTO oauth_clients VALUES ('c1', 'Old', '[]', 1, NULL);
        """)
        con.commit()
    finally:
        con.close()
    # config.settings is a singleton: re-import so it reads THIS test's
    # DATABASE_PATH (the db_module fixture in test_db.py does the same;
    # the `client` fixture reloads the app modules again for the next
    # test, so nothing here outlives this test).
    import importlib
    for mod in ["app.config", "app.db"]:
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import db
    asyncio.run(db.init_db())
    con = sqlite3.connect(temp_env)
    try:
        clients = {r[1] for r in con.execute("PRAGMA table_info(oauth_clients)")}
        tokens = {r[1] for r in con.execute("PRAGMA table_info(oauth_tokens)")}
        assert "approved_ms" in clients
        assert {"consumed_ms", "resource"} <= tokens
        assert con.execute("SELECT approved_ms FROM oauth_clients").fetchone() == (None,)
    finally:
        con.close()


# ── round-three review: SEC-G1, SEC-G2, SEC-G3, §5 rows 7–10 ──

def test_a_rotation_storm_revokes_the_client_instead_of_evicting_the_evidence(client, monkeypatch):
    """SEC-G1: an attacker rotating past the tombstone cap used to evict
    the victim's tombstone. Reaching the cap is the anomaly now: the
    client loses every token, and the replay is refused either way."""
    from app import oauth
    monkeypatch.setattr(oauth, "TOMBSTONES_PER_CLIENT_MAX", 30)
    client_id, first = _login(client)
    cur = _rotate(client, client_id, first["refresh_token"])   # the "thief"
    storm = None
    for _ in range(oauth.TOMBSTONES_PER_CLIENT_MAX + 6):
        r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                              "refresh_token": cur["refresh_token"],
                                              "client_id": client_id})
        if r.status_code != 200:
            storm = r
            break
        cur = r.json()
    assert storm is not None and storm.status_code == 400, "the storm was never refused"
    assert "too many" in storm.json()["error_description"]
    assert _initialize(client, cur["access_token"]).status_code == 401, \
        "the storming client's live session must die"
    r = client.post("/oauth/token", data={"grant_type": "refresh_token",
                                          "refresh_token": first["refresh_token"],
                                          "client_id": client_id})
    assert r.status_code == 400
    assert client.get("/api/oauth/clients", headers=H).json()["clients"][0]["sessions"] == 0


def test_a_custom_domain_in_allowed_hosts_is_its_own_origin(client, monkeypatch):
    """SEC-G2: a Fly app served at wx.example.com with PUBLIC_BASE_URL unset
    advertised fly.dev and refused the resource its clients dialled. A
    Host this deployment answers to by name is the origin; anything else
    on Fly falls back to the fly.dev hostname."""
    from app.config import settings
    monkeypatch.setattr(settings, "public_base_url", None)
    monkeypatch.setenv("FLY_APP_NAME", "zasder-weather-test")
    monkeypatch.setenv("FLY_MACHINE_ID", "e28465f1a2b3c4")
    monkeypatch.setenv("ALLOWED_HOSTS", "wx.example.com,zasder-weather-test.fly.dev")
    # Fly's edge terminates TLS and forwards the scheme; the origin takes
    # it from the request rather than assuming https (CodeRabbit, PR #36).
    doc = client.get("/.well-known/oauth-protected-resource/mcp",
                     headers={"Host": "wx.example.com",
                              "X-Forwarded-Proto": "https"}).json()
    assert doc["resource"] == "https://wx.example.com/mcp"
    doc = client.get("/.well-known/oauth-protected-resource/mcp",
                     headers={"Host": "evil.example"}).json()
    assert doc["resource"] == "https://zasder-weather-test.fly.dev/mcp"
    from app import oauth
    origin, why = oauth.describe_origin()
    assert origin == "https://zasder-weather-test.fly.dev" and "ALLOWED_HOSTS" in why


def test_a_token_issued_under_one_origin_is_refused_under_another(client, monkeypatch):
    """§5 row 10: the audience is the origin; a changed PUBLIC_BASE_URL
    means every assistant reconnects (the changelog says so)."""
    from app.config import settings
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    _, tok = _login(client)
    assert _initialize(client, tok["access_token"]).status_code == 200
    monkeypatch.setattr(settings, "public_base_url", "https://moved.example")
    assert _initialize(client, tok["access_token"]).status_code == 401


def test_a_valid_token_with_a_hostile_host_is_a_401_with_a_challenge(client, monkeypatch):
    """SEC-G3: authenticate_access_token reached issuer() and raised a 400
    with no challenge; the token endpoint answered FastAPI's 400 rather
    than an OAuth error object."""
    from app.config import settings
    client_id, tok = _login(client)
    monkeypatch.setattr(settings, "public_base_url", None)
    monkeypatch.setenv("FLY_APP_NAME", "")
    monkeypatch.setenv("FLY_MACHINE_ID", "")
    r = client.post("/mcp", headers={"Host": 'evil"x', **MCP_H,
                                     "Authorization": f"Bearer {tok['access_token']}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401 and r.headers["WWW-Authenticate"].startswith("Bearer")
    # The token endpoint compares a named resource against the issuer;
    # with a Host that is not a host the answer is an OAuth error object.
    r = client.post("/oauth/token", headers={"Host": 'evil"x'},
                    data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                          "client_id": client_id, "resource": "http://testserver/mcp"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request", r.text


def test_every_redirect_reason_is_covered_by_the_unapproved_gate(client):
    """§5 row 7, derived from the source: every `_redirect_error` reason in
    `_validate_authorize` has a request shape here that an UNAPPROVED
    client gets an error page for, never a 302. A new reason without a
    shape fails this test before it can become an open redirect."""
    import ast
    import inspect
    from app import oauth
    tree = ast.parse(inspect.getsource(oauth._validate_authorize))
    reasons = sorted({
        node.args[1].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_redirect_error"
        and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)})
    shapes = {
        "unsupported_response_type": {"response_type": "token"},
        "invalid_request": {"code_challenge_method": "plain"},
        "invalid_scope": {"scope": "weather:write"},
        "invalid_target": {"resource": "https://other.example/mcp"},
    }
    assert set(reasons) == set(shapes), reasons
    reg = _register(client, approve=False)
    _, challenge = _pkce()
    for reason, bad in shapes.items():
        r = client.get("/oauth/authorize",
                       params=_authorize_params(reg["client_id"], challenge, **bad),
                       follow_redirects=False)
        assert r.status_code == 403 and "location" not in r.headers, (reason, bad)
    # The second invalid_request shape (a malformed challenge) too.
    for bad_challenge in ("short", "*" * 43, ""):
        r = client.get("/oauth/authorize",
                       params=_authorize_params(reg["client_id"], bad_challenge),
                       follow_redirects=False)
        assert r.status_code == 403 and "location" not in r.headers, bad_challenge


def test_an_allow_listed_plain_http_host_keeps_its_scheme(monkeypatch):
    """CodeRabbit, PR #36: a local-Docker server reached over http:// with
    its host in ALLOWED_HOSTS advertised https://host, so the token
    endpoint answered invalid_target. The scheme is the request's."""
    from starlette.requests import Request
    from app import oauth
    from app.config import settings
    monkeypatch.setattr(settings, "public_base_url", None)
    monkeypatch.setenv("ALLOWED_HOSTS", "weather.lan")
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)

    def req(scheme, headers):
        scope = {"type": "http", "method": "GET", "path": "/mcp", "scheme": scheme,
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                 "server": ("weather.lan", 80), "query_string": b""}
        return Request(scope)
    assert oauth.configured_origin(req("http", {"host": "weather.lan"})) == "http://weather.lan"
    assert oauth.configured_origin(req("http", {"host": "weather.lan",
                                              "x-forwarded-proto": "https"})) == "https://weather.lan"
    assert oauth.configured_origin(req("https", {"host": "weather.lan"})) == "https://weather.lan"
