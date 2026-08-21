"""App-minted read-only share tokens (/api/guest-tokens).

The env-secret GUEST_API_TOKENS path exists for setup scripts; these are the
same read-only contract minted from the app's "Share read-only access"
button. The invariants that matter: minting/listing/revoking are owner-only
(write token), a minted token reads but never writes, revocation takes
effect immediately, and tokens survive a restart via the init_db cache load.
"""
from __future__ import annotations

H = {"Authorization": "Bearer test-api-token"}


def _mint(client, label=None):
    body = {"label": label} if label else None
    r = client.post("/api/guest-tokens", headers=H, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_minted_token_reads_but_never_writes(client):
    out = _mint(client, label="Dad")
    token = out["token"]
    assert token.startswith("zwg_") and len(token) == 4 + 32
    assert out["id"] == token[:12]
    assert out["label"] == "Dad"

    gh = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/devices", headers=gh).status_code == 200
    # A write with the share token must 403 with the read-only explanation,
    # not 401 — the token IS valid, just not permitted (same contract as the
    # reviewer token).
    r = client.post("/api/insights/rebuild", headers=gh)
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]


def test_guest_cannot_mint_list_or_revoke(client):
    token = _mint(client)["token"]
    gh = {"Authorization": f"Bearer {token}"}
    # A guest minting further guests would make "revoke one person" a lie.
    assert client.post("/api/guest-tokens", headers=gh).status_code == 403
    assert client.get("/api/guest-tokens", headers=gh).status_code == 403
    assert client.delete(f"/api/guest-tokens/{token[:12]}",
                         headers=gh).status_code == 403


def test_listing_never_ships_the_full_token(client):
    token = _mint(client, label="Neighbour")["token"]
    r = client.get("/api/guest-tokens", headers=H)
    assert r.status_code == 200
    body = r.text
    assert token not in body, "full credential leaked in the listing"
    rows = r.json()["tokens"]
    assert rows[0]["id"] == token[:12]
    assert rows[0]["label"] == "Neighbour"


def test_revocation_takes_effect_immediately(client):
    token = _mint(client)["token"]
    gh = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/devices", headers=gh).status_code == 200
    r = client.delete(f"/api/guest-tokens/{token[:12]}", headers=H)
    assert r.status_code == 200
    assert client.get("/api/devices", headers=gh).status_code == 401
    # Revoking it again is a 404, not a silent success.
    assert client.delete(f"/api/guest-tokens/{token[:12]}",
                         headers=H).status_code == 404


def test_tokens_survive_restart_via_cache_reload(client):
    """The auth gate reads an in-process cache (the dep is sync). A restart
    must reload it from the table, or every share link dies on redeploy."""
    import asyncio
    from app import db
    token = _mint(client)["token"]
    db._GUEST_TOKEN_CACHE = set()          # simulate a fresh process
    gh = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/devices", headers=gh).status_code == 401
    asyncio.run(db.init_db())              # what boot does
    assert client.get("/api/devices", headers=gh).status_code == 200


def test_minted_token_gets_the_limited_view(client):
    """A share-link recipient must see the weather, not the operator: no
    SMTP identity from /api/alerts, no home coordinates or location label
    from /api/devices — the same stripping the reviewer and env-guest
    tokens get. Found by the 2026-08-20 review: minted tokens were admitted
    to the read surface without joining _is_limited_read, so every share
    recipient got the full operator view."""
    # A device with a location label + coords, and an SMTP host on prefs.
    r = client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    json={"device": {"id": "AABBCCDD0910",
                                     "name": "Backyard",
                                     "info": {"coords": {
                                         "location": "Home",
                                         "coords": {"lat": 33.3, "lon": -111.9}}}},
                          "timestamp_utc": "2026-01-01T00:00:00Z",
                          "outdoor": {"tempf": 70.0}})
    assert r.status_code == 200
    assert client.put("/api/alerts", headers=H,
                      json={"smtp_host": "smtp.example.com"}).status_code == 200

    token = _mint(client)["token"]
    gh = {"Authorization": f"Bearer {token}"}

    alerts = client.get("/api/alerts", headers=gh)
    assert alerts.status_code == 200
    assert "smtp.example.com" not in alerts.text, \
        "share token sees the operator's SMTP identity"
    # The operator still sees their own transport config.
    assert "smtp.example.com" in client.get("/api/alerts", headers=H).text

    devices = client.get("/api/devices", headers=gh)
    assert devices.status_code == 200
    body = devices.text
    assert "33.3" not in body and "Home" not in body, \
        "share token sees the operator's home coordinates/location"
    assert "Backyard" in body   # the weather-station identity itself survives


def test_minted_token_forecast_hides_home_coordinates(client, monkeypatch):
    """/api/forecast called with no args resolves the operator's home coords
    and Open-Meteo echoes them back (grid-snapped) in the response body — a
    guest must get the forecast WITHOUT that echo, while the operator keeps
    it (CODE_REVIEW_R5 R5-03, widening R3-06)."""
    import httpx
    from app.config import settings
    monkeypatch.setattr(settings, "forecast_lat", None)
    monkeypatch.setattr(settings, "forecast_lon", None)
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token"},
                json={"device": {"id": "AABBCCDD0911"},
                      "timestamp_utc": "2026-01-01T00:00:00Z",
                      "outdoor": {"tempf": 70},
                      "coords": {"lat": 33.3004, "lon": -111.9378},
                      "source": "test"})
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "latitude": 33.3, "longitude": -111.94, "elevation": 371.0,
            "daily": {}})
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler)))

    token = _mint(client)["token"]
    guest = client.get("/api/forecast",
                       headers={"Authorization": f"Bearer {token}"})
    assert guest.status_code == 200, guest.text
    body = guest.json()
    assert ("latitude" not in body and "longitude" not in body
            and "elevation" not in body), \
        "share token sees the operator's home coordinates via the echo"
    assert "daily" in body            # the forecast itself survives
    # The operator's own call keeps the upstream echo untouched.
    assert client.get("/api/forecast", headers=H).json()["latitude"] == 33.3


def test_minted_token_alerts_hide_recipients(client):
    """Recipient addresses are the operator's personal emails — the same
    data class as smtp_from, frequently the same mailbox. Guests get an
    empty list (CODE_REVIEW_R5 R5-04, widening R3-07)."""
    assert client.put("/api/alerts", headers=H,
                      json={"recipients": ["operator@example.com"]}
                      ).status_code == 200
    token = _mint(client)["token"]
    guest = client.get("/api/alerts",
                       headers={"Authorization": f"Bearer {token}"}).json()
    assert guest["recipients"] == []
    assert "operator@example.com" not in str(guest), \
        "share token sees the operator's alert recipients"
    assert client.get("/api/alerts",
                      headers=H).json()["recipients"] == ["operator@example.com"]
