"""R17 follow-ups (2026-09-01 review) that live outside the story engine.

#5: `send_widget_refresh`'s dead-token prune was the one send path left
out of the R16 f3 prune-isolation fix. A transient DB error there used to
propagate into `widget_push.check`, which then skipped its `_last_push_ms`
stamp and re-sent reload pushes to Apple every alert tick for the length
of the outage. Every existing widget test monkeypatches the function
itself, so nothing pinned the real prune loop.
"""
import asyncio

import pytest


@pytest.mark.usefixtures("client")
def test_widget_refresh_prune_failure_does_not_clobber_the_send(monkeypatch):
    from app import apns, db

    async def fake_relay():
        return "https://relay.example", "zwr_" + "x" * 40

    async def fake_push(tokens, title, body, url, relay_token, **kw):
        assert kw.get("push_type") == "widgets"
        return {"sent": len(tokens), "dead": [tokens[0]], "failed": 0}

    async def exploding_prune(tok):
        raise RuntimeError("database is locked")

    assert not apns.settings.apns_configured, "conftest blanks APNs env"
    monkeypatch.setattr(apns, "effective_relay", fake_relay)
    monkeypatch.setattr(apns, "_push_via_relay", fake_push)
    monkeypatch.setattr(db, "remove_live_activity_token", exploding_prune)

    async def run():
        await db.register_live_activity_token("tok-a", "widgets", "prod")
        await db.register_live_activity_token("tok-b", "widgets", "prod")
        return await apns.send_widget_refresh()

    res = asyncio.run(run())
    # The send stands: APNs already accepted the deliveries, and the prune
    # failure is logged rather than raised (same contract as the three
    # Live Activity sends fixed in R16).
    assert res["sent"] == 2
    assert res["dead"] == ["tok-a"]


@pytest.mark.usefixtures("client")
def test_widget_push_check_stamps_even_when_prune_fails(monkeypatch):
    """The reason #5 mattered: widget_push.check must record the push so
    the next tick is throttled, even if the post-send prune blew up."""
    import time
    from app import apns, db, widget_push

    calls = []

    async def fake_relay():
        return "https://relay.example", "zwr_" + "x" * 40

    async def fake_push(tokens, title, body, url, relay_token, **kw):
        calls.append(len(tokens))
        return {"sent": len(tokens), "dead": list(tokens), "failed": 0}

    async def exploding_prune(tok):
        raise RuntimeError("database is locked")

    assert not apns.settings.apns_configured, "conftest blanks APNs env"
    monkeypatch.setattr(apns, "effective_relay", fake_relay)
    monkeypatch.setattr(apns, "_push_via_relay", fake_push)
    monkeypatch.setattr(db, "remove_live_activity_token", exploding_prune)

    now = int(time.time() * 1000)

    def dev(ts):
        return [{"mac": "AA", "lastData": {"dateutc": ts}}]

    async def run():
        await db.register_live_activity_token("tok-a", "widgets", "prod")
        await widget_push.check(dev(now), now)
        # Inside the throttle gap with fresher data: must NOT push again.
        await widget_push.check(dev(now + 1000), now + widget_push._MIN_GAP_MS // 2)

    asyncio.run(run())
    assert calls == [1], "one push, then throttled -- the stamp survived the prune failure"
