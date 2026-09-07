"""The server names itself (2.1): app value, else SERVER_NAME, else the
public dashboard's location; /api/session carries the name and the
token's role so a multi-server app (2.2) can label each connection."""
H = {"Authorization": "Bearer test-api-token"}


def test_session_carries_name_and_role(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard_location", "Irwin, PA")
    r = client.get("/api/session", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "owner" and body["can_write"] is True
    assert body["server_name"] == "Irwin, PA", "the public location is the last fallback"


def test_env_beats_location_and_the_app_beats_env(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard_location", "Irwin, PA")
    monkeypatch.setattr(settings, "server_name", "Chaucer Drive")
    assert client.get("/api/config/server-name", headers=H).json() == {
        "name": "Chaucer Drive", "source": "env"}
    r = client.put("/api/config/server-name", headers=H,
                   json={"name": "  Chaucer   Drive, the porch  "})
    assert r.status_code == 200
    assert r.json() == {"name": "Chaucer Drive, the porch", "source": "app"}
    assert client.get("/api/session", headers=H).json()["server_name"] == "Chaucer Drive, the porch"
    # Empty forgets the app value.
    r = client.put("/api/config/server-name", headers=H, json={"name": ""})
    assert r.json()["source"] == "env"


def test_the_name_is_one_bounded_printable_line(client):
    r = client.put("/api/config/server-name", headers=H, json={"name": "x" * 200})
    assert r.status_code == 200 and len(r.json()["name"]) == 60
    r = client.put("/api/config/server-name", headers=H, json={"name": "a\x00b"})
    assert r.status_code == 400


def test_a_guest_reads_the_name_but_cannot_set_it(client):
    from app import db
    import asyncio
    asyncio.run(db.set_kv("server_name", "Chaucer Drive"))
    # A REAL guest: an app-minted share token (the 2.1 pre-release review
    # caught this test using the owner token under a guest's name).
    minted = client.post("/api/guest-tokens", headers=H, json={"label": "Dad"})
    assert minted.status_code == 200, minted.text
    guest = {"Authorization": f"Bearer {minted.json()['token']}"}
    assert client.get("/api/config/server-name", headers=guest).json()["name"] == "Chaucer Drive"
    session = client.get("/api/session", headers=guest).json()
    assert session["role"] == "guest" and session["server_name"] == "Chaucer Drive"
    assert client.put("/api/config/server-name", headers=guest,
                      json={"name": "Mine now"}).status_code == 403
    assert client.get("/api/config/server-name").status_code == 401
    assert client.put("/api/config/server-name", json={"name": "x"}).status_code == 401
