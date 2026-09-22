"""2.3 NWS relay controls (Doren, 09-11: the Severe weather toggle was
phone-local and a Flood Watch pushed twice): the owner's switch, the
warnings-only filter, and reissue dedupe."""
import asyncio
import time
from types import SimpleNamespace

H = {"Authorization": "Bearer test-api-token"}
DEV = [{"mac": "AA:BB:CC:00:00:66", "name": "Chaucer",
        "info": {"coords": {"coords": {"lat": 40.32, "lon": -79.70}}}}]


def _cfg(**kw):
    base = dict(enabled=False, email_scope="device_down", recipients=[],
                storm_summary=False, nws_push=True, nws_warnings_only=False,
                nws_muted_families=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _run(batches, cfg, monkeypatch, gap_min=11):
    from app import nws_watch as nw
    nw._reset_for_tests()
    delivered = []

    tiers = []

    async def fake_deliver(cfg, subject, body, pt, pb, **kw):
        delivered.append(pt)
        tiers.append(kw.get("severity"))
        return True

    calls = []

    async def fake_fetch(lat, lon):
        calls.append(1)
        return batches.pop(0) if batches else []
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    now = int(time.time() * 1000)

    async def run():
        for i in range(len(batches) or 1):
            await nw.check(cfg, DEV, now + i * gap_min * 60_000, fake_deliver)
    asyncio.run(run())
    _run.tiers = tiers            # the last run's tiers, for item 1
    return delivered, len(calls)


def test_prefs_round_trip_and_defaults(client):
    g = client.get("/api/alerts", headers=H).json()
    assert g["nws_push"] is True and g["nws_warnings_only"] is False
    r = client.put("/api/alerts", headers=H, json={"nws_push": False, "nws_warnings_only": True})
    assert r.status_code == 200, r.text
    g = client.get("/api/alerts", headers=H).json()
    assert g["nws_push"] is False and g["nws_warnings_only"] is True
    client.put("/api/alerts", headers=H, json={"nws_push": True})
    assert client.get("/api/alerts", headers=H).json()["nws_push"] is True


def test_the_switch_stops_the_poll_and_the_push(client, monkeypatch):
    alert = {"id": "urn:f1", "severity": "Severe", "event": "Flood Watch",
             "headline": "Flood Watch until 1 AM"}
    delivered, polls = _run([[alert]], _cfg(nws_push=False), monkeypatch)
    assert delivered == [] and polls == 0
    delivered, polls = _run([[alert]], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch"] and polls == 1


def test_warnings_only_holds_watches_and_advisories(client, monkeypatch):
    batch = [{"id": "urn:w1", "severity": "Severe", "event": "Flood Watch", "headline": "w"},
             {"id": "urn:w2", "severity": "Extreme", "event": "Tornado Warning", "headline": "t"},
             {"id": "urn:w3", "severity": "Severe", "event": "Wind Advisory", "headline": "a"}]
    delivered, _ = _run([batch], _cfg(nws_warnings_only=True), monkeypatch)
    assert delivered == ["Chaucer: Tornado Warning"]
    # And the held ones are seen: a later poll does not raise them again.
    from app import nws_watch as nw
    import json
    from app import db
    seen = json.loads(asyncio.run(db.get_kv(nw._SEEN_KEY)))
    assert set(seen) >= {"urn:w1", "urn:w2", "urn:w3"}


def test_a_reissue_of_a_pushed_alert_is_silent(client, monkeypatch):
    first = {"id": "urn:f1", "severity": "Severe", "event": "Flood Watch",
             "headline": "Flood Watch issued 1:26 PM", "messageType": "Alert"}
    update = {"id": "urn:f2", "severity": "Severe", "event": "Flood Watch",
              "headline": "Flood Watch issued 9:05 PM", "messageType": "Update",
              "references": [{"@id": "https://api.weather.gov/alerts/urn:f1",
                              "identifier": "urn:f1", "sender": "w-nws.webmaster@noaa.gov",
                              "sent": "2026-09-11T13:26:00-04:00"}]}
    cancel = {"id": "urn:f3", "severity": "Severe", "event": "Flood Watch",
              "headline": "Flood Watch cancelled", "messageType": "Cancel",
              "references": [{"identifier": "urn:f2"}]}
    # A fresh update whose original we never saw is new to us and pushes.
    stranger = {"id": "urn:h9", "severity": "Extreme", "event": "Extreme Heat Warning",
                "headline": "hot", "messageType": "Update",
                "references": [{"identifier": "urn:h8"}]}
    delivered, polls = _run([[first], [first, update], [update, cancel, stranger]],
                            _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch", "Chaucer: Extreme Heat Warning"]
    assert polls == 3


def test_is_warning_and_is_reissue_words(client):
    from app import nws_watch as nw
    assert nw.is_warning("Tornado Warning") and nw.is_warning("extreme heat warning ")
    assert not nw.is_warning("Flood Watch") and not nw.is_warning("Special Weather Statement")
    assert not nw.is_reissue({"messageType": "Alert", "references": [{"identifier": "x"}]}, {"x"})
    assert not nw.is_reissue({"messageType": "Update", "references": [{"identifier": "x"}]}, set())
    assert nw.is_reissue({"messageType": "Update", "references": [{"identifier": "x"}]}, {"x"})
    assert not nw.is_reissue({"messageType": "Update", "references": [{"identifier": "y"}]}, {"x"})
    assert nw.is_reissue({"messageType": "Cancel", "references": []}, set())
    assert not nw.is_reissue({"messageType": "Update"}, {"x"})


def test_a_reference_by_url_alone_still_counts_as_a_reissue(client, monkeypatch):
    first = {"id": "NWS-IDP-PROD-1-1", "severity": "Severe", "event": "Flood Watch",
             "headline": "first", "messageType": "Alert"}
    by_url = {"id": "NWS-IDP-PROD-1-2", "severity": "Severe", "event": "Flood Watch",
              "headline": "extended", "messageType": "Update",
              "references": [{"@id": "https://api.weather.gov/alerts/NWS-IDP-PROD-1-1"}]}
    delivered, polls = _run([[first], [by_url]], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch"] and polls == 2
    from app import nws_watch as nw
    assert nw._reference_ids({"@id": "https://x/alerts/A-1/"}) == ["https://x/alerts/A-1/", "A-1"]
    assert nw._reference_ids("A-2") == ["A-2", "A-2"] and nw._reference_ids(7) == []


def test_an_upgrade_of_a_filtered_alert_still_pushes(client, monkeypatch):
    """The seen-set holds filtered ids too; reissue dedupe must consult
    only the PUSHED ids, or a Moderate alert upgraded to Severe by an
    Update that references it would be swallowed."""
    moderate = {"id": "urn:m1", "severity": "Moderate", "event": "Flood Advisory",
                "headline": "advisory", "messageType": "Alert"}
    upgrade = {"id": "urn:m2", "severity": "Severe", "event": "Flood Warning",
               "headline": "warning", "messageType": "Update",
               "references": [{"identifier": "urn:m1"}]}
    again = {"id": "urn:m3", "severity": "Severe", "event": "Flood Warning",
             "headline": "extended", "messageType": "Update",
             "references": [{"identifier": "urn:m2"}]}
    delivered, polls = _run([[moderate], [upgrade], [again]], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Warning"] and polls == 3
    import json
    from app import db, nws_watch as nw
    pushed = json.loads(asyncio.run(db.get_kv(nw._PUSHED_KEY)))
    seen = json.loads(asyncio.run(db.get_kv(nw._SEEN_KEY)))
    # m3 was silent, and it now stands in for m2 in the ledger (R23).
    assert pushed == ["urn:m2", "urn:m3"] and set(seen) >= {"urn:m1", "urn:m2", "urn:m3"}
    assert "urn:m1" not in pushed


def test_a_watch_held_by_warnings_only_pushes_when_it_becomes_a_warning(client, monkeypatch):
    watch = {"id": "urn:w1", "severity": "Severe", "event": "Tornado Watch",
             "headline": "watch", "messageType": "Alert"}
    warning = {"id": "urn:w2", "severity": "Extreme", "event": "Tornado Warning",
               "headline": "warning", "messageType": "Update",
               "references": [{"identifier": "urn:w1"}]}
    delivered, _ = _run([[watch], [warning]], _cfg(nws_warnings_only=True), monkeypatch)
    assert delivered == ["Chaucer: Tornado Warning"]


def test_a_pushed_id_missing_from_seen_is_still_not_pushed_again(client, monkeypatch):
    """Crash between the two ledger writes: pushed holds the id, seen does
    not. The load folds pushed into seen, so the next poll stays quiet."""
    import json
    from app import db, nws_watch as nw
    asyncio.run(db.set_kv(nw._PUSHED_KEY, json.dumps(["urn:c1"])))
    asyncio.run(db.set_kv(nw._SEEN_KEY, json.dumps([])))
    alert = {"id": "urn:c1", "severity": "Severe", "event": "Flood Watch", "headline": "w"}
    delivered, polls = _run([[alert]], _cfg(), monkeypatch)
    assert delivered == [] and polls == 1
    assert "urn:c1" in json.loads(asyncio.run(db.get_kv(nw._SEEN_KEY)))


def _upd(i, prev, severity="Severe", event="Flood Watch", tag="u"):
    return {"id": f"urn:{tag}{i}", "severity": severity, "event": event,
            "headline": f"update {i}", "messageType": "Update",
            "references": [{"identifier": f"urn:{tag}{prev}"}]}


def test_an_update_chain_pushes_once(client, monkeypatch):
    """R23: NWS mints a new id for every update and the common shape
    references ONLY the previous message. A suppressed reissue that did
    not join the pushed ledger let u3 (naming u2 alone) push again, so a
    long-lived Flood Watch pushed every OTHER update."""
    u1 = {"id": "urn:u1", "severity": "Severe", "event": "Flood Watch",
          "headline": "issued", "messageType": "Alert"}
    delivered, polls = _run([[u1], [_upd(2, 1)], [_upd(3, 2)], [_upd(4, 3)]],
                            _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch"] and polls == 4
    import json
    from app import db, nws_watch as nw
    pushed = json.loads(asyncio.run(db.get_kv(nw._PUSHED_KEY)))
    assert pushed == ["urn:u1", "urn:u2", "urn:u3", "urn:u4"]
    meta = json.loads(asyncio.run(db.get_kv(nw._PUSHED_META_KEY)))
    assert meta["urn:u4"] == [nw.severity_rank("Severe"), False]


def test_an_update_that_raises_severity_pushes_again(client, monkeypatch):
    """Product decision (R23): the sky changed. Severe -> Extreme through
    a chain pushes a second time; the extension that follows at the new
    level is silent again."""
    u1 = {"id": "urn:u1", "severity": "Severe", "event": "Flood Watch",
          "headline": "issued", "messageType": "Alert"}
    delivered, _ = _run([[u1], [_upd(2, 1)], [_upd(3, 2, severity="Extreme")],
                         [_upd(4, 3, severity="Extreme")]], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch", "Chaucer: Flood Watch"]
    # A downgrade and a return to the old level is NOT a raise: the
    # ledger remembers the highest the chain was pushed at.
    v1 = dict(u1, id="urn:v1")
    delivered, _ = _run([[v1], [_upd(2, 1, severity="Extreme", tag="v")],
                         [_upd(3, 2, severity="Severe", tag="v")],
                         [_upd(4, 3, severity="Extreme", tag="v")]], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch", "Chaucer: Flood Watch"]


def test_an_update_that_becomes_a_warning_pushes_again(client, monkeypatch):
    u1 = {"id": "urn:u1", "severity": "Severe", "event": "Flood Watch",
          "headline": "issued", "messageType": "Alert"}
    delivered, _ = _run([[u1], [_upd(2, 1)], [_upd(3, 2, event="Flood Warning")],
                         [_upd(4, 3, event="Flood Warning")]], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch", "Chaucer: Flood Warning"]


def test_an_unchanged_extension_stays_silent_and_a_cancel_never_escalates(client, monkeypatch):
    u1 = {"id": "urn:u1", "severity": "Extreme", "event": "Tornado Warning",
          "headline": "issued", "messageType": "Alert"}
    cancel = dict(_upd(3, 2, severity="Extreme", event="Tornado Warning"),
                  messageType="Cancel")
    delivered, _ = _run([[u1], [_upd(2, 1, severity="Extreme", event="Tornado Warning")],
                         [cancel]], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Tornado Warning"]
    from app import nws_watch as nw
    # A ledger entry from before the meta key existed never re-pushes.
    assert not nw.escalates(_upd(2, 1, severity="Extreme", event="Tornado Warning"),
                            {"urn:u1"}, {})
    assert nw.escalates(_upd(2, 1, severity="Extreme"), {"urn:u1"}, {"urn:u1": [3, False]})
    assert not nw.escalates(_upd(2, 1, severity="Severe"), {"urn:u1"}, {"urn:u1": [3, False]})


# ───────────── 2.4 item 1: families and levels ─────────────


def test_family_prefs_round_trip_with_their_catalogue(client):
    """The apps draw the family list from the server rather than each
    keeping its own copy of the keys — that is how a family ends up
    mutable on the phone and not on the Mac."""
    g = client.get("/api/alerts", headers=H).json()
    assert g["nws_families"] == []
    cat = g["nws_family_catalogue"]
    assert [c["key"] for c in cat][0] == "tornado"
    assert [c["key"] for c in cat][-1] == "other"
    assert all(c["label"].strip() for c in cat)

    r = client.put("/api/alerts", headers=H,
                   json={"nws_families": ["marine", "flood", "nonsense"]})
    assert r.status_code == 200, r.text
    # Cleaned on the way in: the key nobody knows is dropped rather than
    # stored, and the order is the catalogue's.
    assert client.get("/api/alerts", headers=H).json()["nws_families"] \
        == ["flood", "marine"]
    # An empty list is a real value — un-muting everything.
    client.put("/api/alerts", headers=H, json={"nws_families": []})
    assert client.get("/api/alerts", headers=H).json()["nws_families"] == []


def test_a_muted_family_never_pushes(client, monkeypatch):
    """Doren's Flood Watch. Muting Flood must stop it without touching
    the tornado sitting beside it in the same batch."""
    batch = [{"id": "urn:m1", "severity": "Severe", "event": "Flood Watch",
              "headline": "w"},
             {"id": "urn:m2", "severity": "Extreme", "event": "Tornado Warning",
              "headline": "t"},
             {"id": "urn:m3", "severity": "Severe", "event": "Small Craft Advisory",
              "headline": "s"}]
    delivered, _ = _run([batch], _cfg(nws_muted_families=["flood", "marine"]),
                        monkeypatch)
    assert delivered == ["Chaucer: Tornado Warning"]
    # The muted ones are SEEN, so un-muting later does not replay a week
    # of held alerts — and a later poll does not raise them again.
    import json

    from app import db
    from app import nws_watch as nw
    seen = json.loads(asyncio.run(db.get_kv(nw._SEEN_KEY)))
    assert set(seen) >= {"urn:m1", "urn:m2", "urn:m3"}


def test_the_product_class_sets_how_loud_it_is(client, monkeypatch):
    """Until 2.4 every relayed alert rode the `warning` tier — time
    sensitive, through quiet hours — so a Frost Advisory punched through
    Focus exactly as a Tornado Warning did."""
    batch = [{"id": "urn:t1", "severity": "Extreme", "event": "Tornado Warning",
              "headline": "t"},
             {"id": "urn:t2", "severity": "Severe", "event": "Flood Watch",
              "headline": "w"},
             {"id": "urn:t3", "severity": "Severe", "event": "Wind Advisory",
              "headline": "a"}]
    delivered, _ = _run([batch], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Tornado Warning", "Chaucer: Flood Watch",
                         "Chaucer: Wind Advisory"]
    assert _run.tiers == ["warning", "major", "watch"]


def test_no_muted_families_is_the_pre_24_behaviour(client, monkeypatch):
    batch = [{"id": "urn:d1", "severity": "Severe", "event": "Flood Watch",
              "headline": "w"}]
    delivered, _ = _run([batch], _cfg(), monkeypatch)
    assert delivered == ["Chaucer: Flood Watch"]


def test_a_malformed_event_stays_loud_and_still_says_something(client,
                                                               monkeypatch):
    """CodeRabbit, PR #40: the display fallback "Weather alert" ends in
    "alert", so for one commit a malformed event classified as an
    ADVISORY — quiet tier, and dropped entirely under warnings-only.
    An unidentifiable product gets the loud side, and the title still
    reads as words."""
    batch = [{"id": "urn:x1", "severity": "Extreme", "event": None,
              "headline": "something is very wrong"}]
    delivered, _ = _run([batch], _cfg(nws_warnings_only=True), monkeypatch)
    assert delivered == ["Chaucer: Weather alert"]
    assert _run.tiers == ["warning"]


def test_an_advisory_is_held_through_quiet_hours_and_sent_after(client, monkeypatch):
    """2.4 review: the tiers made an Advisory an ordinary push and a
    Statement an after-the-fact line, and both sit below the quiet-hours
    floor. `deliver` then attempts nothing at night, reports the alert
    HANDLED, and nws_watch recorded it pushed for good — a Frost Advisory
    issued at 22:30 never reached the phone at all. Held is not handled:
    the id stays unseen until quiet hours end, then goes once. Through the
    REAL deliver, because the stub in _run is exactly what hid this."""
    from app import alerts, nws_watch as nw
    nw._reset_for_tests()
    sent = []

    async def fake_push_configured():
        return True

    async def fake_send_to_all(title, body, **kw):
        sent.append((title, kw.get("tier")))
        return {"sent": 1, "total": 1}
    monkeypatch.setattr(alerts.apns, "push_configured", fake_push_configured)
    monkeypatch.setattr(alerts.apns, "send_to_all", fake_send_to_all)

    quiet = {"on": True}
    monkeypatch.setattr(alerts, "in_quiet_hours",
                        lambda now_ms, tz, s, e: quiet["on"] and s is not None)
    batch = [{"id": "urn:q1", "severity": "Severe", "event": "Frost Advisory",
              "headline": "Frost tonight"},
             {"id": "urn:q2", "severity": "Extreme", "event": "Tornado Warning",
              "headline": "Take cover"}]
    batches = [list(batch), list(batch), list(batch)]

    async def fake_fetch(lat, lon):
        return batches.pop(0) if batches else []
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    cfg = _cfg(quiet_start_min=22 * 60, quiet_end_min=7 * 60,
               recipients=[], smtp_host=None)
    now = int(time.time() * 1000)

    async def run():
        # 1. Night: the warning goes, the advisory is held.
        await nw.check(cfg, DEV, now, alerts._deliver)
        assert sent == [("Chaucer: Tornado Warning", "warning")]
        # 2. Still night, next poll: held again, not re-sent, not marked.
        await nw.check(cfg, DEV, now + 11 * 60_000, alerts._deliver)
        assert len(sent) == 1
        # 3. Morning: the advisory goes exactly once.
        quiet["on"] = False
        await nw.check(cfg, DEV, now + 22 * 60_000, alerts._deliver)
        assert sent[1:] == [("Chaucer: Frost Advisory", "watch")]
        await nw.check(cfg, DEV, now + 33 * 60_000, alerts._deliver)
        assert len(sent) == 2
    asyncio.run(run())
