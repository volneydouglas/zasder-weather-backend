"""Station rename (2.0): `PUT /api/devices/{mac}/name`.

A station's name comes from its payload — AWN sends one, an Ecowitt gateway
sends only a model and arrives as "Ecowitt (GW3000B)" — and upsert_device
rewrites `name` on every post. The rename is an OVERRIDE column that the
ingest path never touches, read through one helper so every surface (the
device list, alerts, the digest, storm summaries, stories, the public page,
/metrics, the token label) shows the same effective name.
"""
from __future__ import annotations

import asyncio
import pytest

H = {"Authorization": "Bearer test-api-token"}
I = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:DD:EE:F0"


def _post(client, name="Ecowitt (GW3000B)", ts="2026-05-14T06:00:00Z",
          tempf=72.5, **outdoor):
    body = {"device": {"id": MAC.replace(":", ""), "name": name},
            "timestamp_utc": ts,
            "outdoor": {"tempf": tempf, "humidity": 50, **outdoor},
            "wind": {"speed_mph": 3}, "rain": {},
            "pressure": {"relative_inhg": 29.9},
            "source": "test"}
    r = client.post("/ingest/custom", headers=I, json=body)
    assert r.status_code == 200, r.text
    return r


def _device(client, mac=MAC, headers=H):
    r = client.get("/api/devices", headers=headers)
    assert r.status_code == 200
    return next(d for d in r.json() if d["mac"] == mac)


def _rename(client, name, headers=H):
    return client.put(f"/api/devices/{MAC}/name", headers=headers,
                      json={"name": name})


# ───────────────────────── set / clear / validate ─────────────────────────

def test_rename_sets_the_effective_name_and_keeps_the_source_name(client):
    _post(client)
    r = _rename(client, "  Back yard  ")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "mac": MAC, "name": "Back yard",
                        "display_name": "Back yard",
                        "source_name": "Ecowitt (GW3000B)"}
    d = _device(client)
    assert d["name"] == "Back yard"
    assert d["display_name"] == "Back yard"
    assert d["source_name"] == "Ecowitt (GW3000B)"


def test_no_override_reports_null_display_name(client):
    _post(client)
    d = _device(client)
    assert d["name"] == "Ecowitt (GW3000B)"
    assert d["display_name"] is None
    assert d["source_name"] == "Ecowitt (GW3000B)"


@pytest.mark.parametrize("clear", ["", "   ", None])
def test_blank_or_null_clears_back_to_the_stations_own_name(client, clear):
    _post(client)
    assert _rename(client, "Back yard").status_code == 200
    r = _rename(client, clear)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Ecowitt (GW3000B)"
    assert r.json()["display_name"] is None
    d = _device(client)
    assert d["name"] == "Ecowitt (GW3000B)" and d["display_name"] is None


def test_omitted_name_key_also_clears(client):
    _post(client)
    assert _rename(client, "Back yard").status_code == 200
    r = client.put(f"/api/devices/{MAC}/name", headers=H, json={})
    assert r.status_code == 200
    assert _device(client)["display_name"] is None


def test_sixty_four_characters_fit_and_sixty_five_do_not(client):
    _post(client)
    assert _rename(client, "x" * 64).status_code == 200
    r = _rename(client, "x" * 65)
    assert r.status_code == 400
    assert "64" in r.json()["detail"]
    # Trimmed BEFORE the length check: padding must not push a fit over.
    assert _rename(client, "  " + "y" * 64 + "  ").status_code == 200
    assert _device(client)["name"] == "y" * 64


@pytest.mark.parametrize("bad", ["Back\nyard", "Back\x00yard", "tab\there",
                                 "del\x7f"])
def test_control_characters_are_rejected(client, bad):
    """The name rides email subjects and push titles; a newline in either
    is a header injection."""
    _post(client)
    r = _rename(client, bad)
    assert r.status_code == 400
    assert "control" in r.json()["detail"]
    assert _device(client)["display_name"] is None


def test_non_string_name_is_a_validation_error(client):
    _post(client)
    assert _rename(client, 12).status_code == 422
    assert _rename(client, ["a"]).status_code == 422


def test_unknown_station_is_404_not_a_phantom_row(client):
    r = client.put("/api/devices/00:11:22:33:44:55/name", headers=H,
                   json={"name": "Ghost"})
    assert r.status_code == 404
    assert client.get("/api/devices", headers=H).json() == []


def test_mac_is_normalized_on_write(client):
    _post(client)
    r = client.put(f"/api/devices/{MAC.replace(':', '').lower()}/name",
                   headers=H, json={"name": "Back yard"})
    assert r.status_code == 200
    assert r.json()["mac"] == MAC
    assert _device(client)["name"] == "Back yard"


def test_rename_requires_a_token(client):
    _post(client)
    assert client.put(f"/api/devices/{MAC}/name",
                      json={"name": "x"}).status_code == 401
    assert _device(client)["display_name"] is None


# ───────────────────────── the override survives ingest ─────────────────────────

def test_override_survives_an_ingest_carrying_a_different_payload_name(client):
    _post(client, name="Ecowitt (GW3000B)")
    assert _rename(client, "Back yard").status_code == 200
    # A later post with a NEW payload name updates the source name only.
    _post(client, name="Ecowitt (GW3000C)", ts="2026-05-14T07:00:00Z")
    d = _device(client)
    assert d["name"] == "Back yard"
    assert d["display_name"] == "Back yard"
    assert d["source_name"] == "Ecowitt (GW3000C)"
    # And once cleared, the CURRENT payload name shows, not the old one.
    assert _rename(client, None).status_code == 200
    assert _device(client)["name"] == "Ecowitt (GW3000C)"


def test_upsert_device_never_touches_display_name():
    """Unit-level pin on the write path itself: neither the INSERT nor the
    UPDATE arm of upsert_device knows the column exists."""
    from app import db
    src = db.upsert_device.__code__.co_consts
    sql = "\n".join(c for c in src if isinstance(c, str))
    # Fail loudly if the statement is no longer a literal in this function:
    # otherwise the negative assertion below silently proves nothing.
    assert "INSERT INTO devices" in sql and "ON CONFLICT(mac)" in sql, \
        "upsert_device's SQL is no longer inspectable here; re-pin this test"
    assert "display_name" not in sql


# ───────────────────────── every reader shows the override ─────────────────────────

def _seed_renamed(client, override="Back yard"):
    _post(client)
    assert _rename(client, override).status_code == 200
    return override


def test_limited_read_token_sees_the_override_too(client):
    """_strip_device_pii spreads the row, so the new fields ride through."""
    _seed_renamed(client)
    d = _device(client, headers={"Authorization": "Bearer test-reviewer-token"})
    assert d["name"] == "Back yard"
    assert d["display_name"] == "Back yard"
    assert d["source_name"] == "Ecowitt (GW3000B)"


def test_status_page_shows_the_override(client):
    _seed_renamed(client)
    page = client.get("/").text
    assert "Back yard" in page
    assert "Ecowitt (GW3000B)" not in page


def test_public_dashboard_and_embed_show_the_override(client, monkeypatch):
    import datetime as _dt
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    now = _dt.datetime.now(_dt.timezone.utc)
    for mins, temp in ((120, 88.0), (60, 91.0)):
        ts = (now - _dt.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _post(client, ts=ts, tempf=temp)
    assert _rename(client, "Back yard").status_code == 200
    for path in ("/", "/embed"):
        page = client.get(path).text
        assert "Back yard" in page, path
        assert "Ecowitt (GW3000B)" not in page, path


def test_metrics_label_shows_the_override(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "prometheus_metrics", True)
    _seed_renamed(client)
    text = client.get("/metrics").text
    assert 'name="Back yard"' in text
    assert "GW3000B" not in text


def test_alert_state_device_list_shows_the_override(client):
    _seed_renamed(client)
    devs = client.get("/api/alerts", headers=H).json()["devices"]
    assert [d["name"] for d in devs if d["mac"] == MAC] == ["Back yard"]


def test_story_context_carries_the_override(client):
    from app import stories
    _seed_renamed(client)
    ctx = asyncio.run(stories.build_context(MAC))
    assert ctx.station_name == "Back yard"
    assert ctx.station({})["name"] == "Back yard"


def test_noaa_report_header_shows_the_override(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "insights", True)
    _seed_renamed(client)
    r = client.get(f"/api/devices/{MAC}/reports/noaa", headers=H,
                   params={"year": 2026})
    assert r.status_code == 200, r.text
    assert "Station: Back yard" in r.text


def test_auto_minted_token_label_uses_the_override(client):
    from app import db
    _seed_renamed(client)
    assert asyncio.run(db.get_device_name(MAC)) == "Back yard"


def _cfg(**over):
    import app.alerts as al
    return al.EffectiveAlertConfig(
        enabled=True, transport_configured=True, recipients=["x@example.com"],
        default_threshold_min=15.0, repeat_hours=0.0, smtp_host="localhost",
        smtp_port=25, smtp_username=None, smtp_password=None,
        smtp_from="a@example.com", smtp_tls=False, smtp_ssl=False,
        email_scope="all", **over)


def _capture_delivery(monkeypatch):
    import app.alerts as al
    sent: list[tuple[str, str]] = []

    async def fake_deliver(cfg, subject, body, title, push_body,
                           email_ok=True, **kw):
        sent.append((subject, title))
        return True

    monkeypatch.setattr(al, "_deliver", fake_deliver)
    return al, sent


def test_threshold_alert_names_the_override(client, monkeypatch):
    """The alert path reads db.list_devices() — the same rows the API
    serves — so the email subject and push title carry the rename."""
    al, sent = _capture_delivery(monkeypatch)
    from app import db
    _seed_renamed(client)
    r = client.post("/api/alerts/rules", headers=H,
                    json={"field": "tempf", "comparator": "above",
                          "threshold": 60.0})
    assert r.status_code in (200, 201), r.text
    _post(client, ts="2026-05-14T07:00:00Z", tempf=95.0)

    async def run():
        devices = await db.list_devices()
        await al.AlertMonitor()._check_threshold_rules(
            _cfg(), devices, 1_800_000_000_000)

    asyncio.run(run())
    assert sent, "the rule must fire for this test to prove anything"
    assert all("Back yard" in title for _, title in sent), sent
    assert not any("GW3000B" in title for _, title in sent)


def test_storm_summary_names_the_override(client, monkeypatch):
    al, sent = _capture_delivery(monkeypatch)
    from app import db
    _seed_renamed(client)
    t0 = 1_800_000_000_000

    async def run():
        rows = [{"dateutc": t0 + i * 60_000, "yearlyrainin": 52.0 + 0.2 * i,
                 "tempf": 70.0 + i, "windgustmph": 10.0 + 3 * i,
                 "hourlyrainin": 0.5 * i} for i in range(7)]
        await db.insert_observations(MAC, rows)
        mon = al.AlertMonitor()
        for i in range(7):
            await db.upsert_device(MAC, {"lastData": rows[i]})
            await mon._check_storm_summaries(
                _cfg(), await db.list_devices(), t0 + i * 60_000)
        dry = {"dateutc": t0 + 6 * 60_000, "yearlyrainin": 52.0 + 1.2,
               "tempf": 76.0}
        await db.upsert_device(MAC, {"lastData": dry})
        await mon._check_storm_summaries(
            _cfg(), await db.list_devices(), t0 + 36 * 60_000 + 1)

    asyncio.run(run())
    assert len(sent) == 1, sent
    assert sent[0][1] == "Back yard Storm Summary"


def test_morning_digest_names_the_override(client, monkeypatch):
    import datetime as _dt
    from zoneinfo import ZoneInfo
    import app.alerts as al
    from app import db, insights
    from app.config import settings as _settings

    sent = []
    monkeypatch.setattr(
        al, "_send_sync",
        lambda subject, body, to, cfg, html=None: sent.append(
            (subject, body, html)))
    try:
        tz = ZoneInfo(_settings.timezone)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).replace(hour=8, minute=0)
    now_ms = int(now.timestamp() * 1000)
    yday = now - _dt.timedelta(days=1)
    _seed_renamed(client)

    async def run():
        await db.upsert_device(MAC, {"lastData": {"dateutc": now_ms,
                                                  "tempf": 90.0}})
        await db.insert_observations(MAC, [
            {"dateutc": int(yday.replace(hour=15).timestamp() * 1000),
             "tempf": 104.0, "humidity": 12.0, "windgustmph": 34.0,
             "dailyrainin": 0.12},
            {"dateutc": int(yday.replace(hour=5).timestamp() * 1000),
             "tempf": 79.0, "humidity": 55.0, "windgustmph": 4.0,
             "dailyrainin": 0.0},
        ])
        await insights.rebuild(MAC)
        await al.AlertMonitor()._maybe_send_digest(
            _cfg(digest_hour=7), await db.list_devices(), now_ms)

    asyncio.run(run())
    assert len(sent) == 1
    _, body, html_body = sent[0]
    assert "Back yard" in body and "Back yard" in html_body
    assert "GW3000B" not in html_body


# ───────────────────────── backup / restore + delete ─────────────────────────

def test_backup_carries_the_rename_and_restore_puts_it_back(client):
    _seed_renamed(client)
    b = client.get("/api/config/backup", headers=H).json()
    assert b["device_names"] == {MAC: "Back yard"}
    # Lose it, then restore.
    assert _rename(client, None).status_code == 200
    assert _device(client)["display_name"] is None
    r = client.post("/api/config/restore", headers=H, json=b)
    assert r.status_code == 200, r.text
    assert r.json()["restored"]["device_names"] == 1
    assert _device(client)["name"] == "Back yard"


def test_restore_applies_the_same_rule_as_the_route(client):
    """A hand-edited file cannot land what the API refuses; an unknown MAC
    is skipped rather than creating a phantom device."""
    _post(client)
    payload = {"version": 1,
               "device_names": {MAC: "Back\nyard",
                                "00:11:22:33:44:55": "Ghost",
                                MAC + "x": 12}}
    r = client.post("/api/config/restore", headers=H, json=payload)
    # Nothing restorable at all is an error, by design.
    assert r.status_code == 400
    assert _device(client)["display_name"] is None
    assert len(client.get("/api/devices", headers=H).json()) == 1
    payload["device_names"][MAC] = "  Back yard  "
    r = client.post("/api/config/restore", headers=H, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["restored"]["device_names"] == 1
    assert _device(client)["name"] == "Back yard"


def test_a_pre_2_0_backup_without_the_key_still_restores(client):
    _post(client)
    r = client.post("/api/config/restore", headers=H, json={
        "version": 1,
        "device_locations": {MAC: {"lat": 33.3, "lon": -111.9,
                                   "label": "Home"}}})
    assert r.status_code == 200, r.text
    assert r.json()["restored"]["device_names"] == 0


def test_delete_clears_the_override_for_a_re_registered_mac(client):
    _seed_renamed(client)
    r = client.delete(f"/api/devices/{MAC}", headers=H)
    assert r.status_code == 200
    _post(client, ts="2026-05-14T08:00:00Z")
    d = _device(client)
    assert d["name"] == "Ecowitt (GW3000B)"
    assert d["display_name"] is None


# ───────────────────────── the one helper ─────────────────────────

@pytest.mark.parametrize("row,expected", [
    ({"name": "Ecowitt (GW3000B)", "display_name": None}, "Ecowitt (GW3000B)"),
    ({"name": "Ecowitt (GW3000B)", "display_name": "Back yard"}, "Back yard"),
    ({"name": "Ecowitt (GW3000B)", "display_name": "   "}, "Ecowitt (GW3000B)"),
    ({"name": None, "display_name": "Back yard"}, "Back yard"),
    ({"name": None, "display_name": None}, None),
    ({"name": ""}, None),                       # pre-2.0 row, no column
    ({"name": "AWN"}, "AWN"),
])
def test_effective_device_name(row, expected):
    from app import db
    assert db.effective_device_name(row) == expected


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("   ", None),
    ("  Back yard ", "Back yard"), ("x" * 64, "x" * 64),
])
def test_clean_display_name_accepts(raw, expected):
    from app import db
    assert db.clean_display_name(raw) == expected


@pytest.mark.parametrize("raw", ["x" * 65, "a\nb", "a\x00", 12, ["a"]])
def test_clean_display_name_rejects(raw):
    from app import db
    with pytest.raises(ValueError):
        db.clean_display_name(raw)
