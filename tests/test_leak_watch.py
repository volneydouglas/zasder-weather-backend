"""Water leak detectors (2.5, C2): stored since 1.9, never alerted on."""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

import pytest  # noqa: E402

from app import leak_watch  # noqa: E402


class _Cfg:
    email_scope = "device_down"


def test_only_dry_and_wet_are_claims():
    assert leak_watch.leak_channels({"leak1": 0, "leak2": 1, "leak3": 2,
                                     "leak4": None}) == {1: False, 2: True}
    assert leak_watch.leak_channels({"leak1": True}) == {}
    assert leak_watch.leak_channels({}) == {}


@pytest.fixture
def sent():
    return []


@pytest.fixture
def deliver(sent):
    async def _d(cfg, subject, body, title, push_body, **kw):
        sent.append((kw.get("kind"), title, push_body))
        return True
    return _d


def _dev(now, **leaks):
    return [{"mac": "EC:EC:00:00:00:01", "name": "Basement",
             "lastData": {"dateutc": now, **leaks}}]


async def test_one_alert_per_leak_then_one_all_clear(client, deliver, sent):
    now = 1_790_000_000_000
    await leak_watch.check(_Cfg(), _dev(now, leak1=1), now, deliver)
    await leak_watch.check(_Cfg(), _dev(now + 60_000, leak1=1), now + 60_000, deliver)
    assert [k for k, _, _ in sent] == ["leak"]
    assert sent[0][1] == "Basement: Water leak detected"
    assert sent[0][2] == "Water detected at the leak sensor."
    await leak_watch.check(_Cfg(), _dev(now + 120_000, leak1=0), now + 120_000, deliver)
    assert [k for k, _, _ in sent] == ["leak", "leak_cleared"]


async def test_channels_are_named_when_there_are_several(client, deliver, sent):
    now = 1_790_000_000_000
    await leak_watch.check(_Cfg(), _dev(now, leak1=0, leak3=1), now, deliver)
    assert sent[0][2] == "Water detected at leak sensor 3."


async def test_a_stale_reading_belongs_to_device_down(client, deliver, sent):
    now = 1_790_000_000_000
    await leak_watch.check(_Cfg(), _dev(now - 3_600_000, leak1=1), now, deliver)
    assert sent == []


def test_a_leak_is_a_warning():
    from app import alerts
    assert alerts.severity_of("leak") == "warning"
    assert alerts.severity_of("leak_cleared") == "info"


async def test_the_smart_switch_never_forgets_a_leak(client):
    """The smart-off tick clears the smart family's edges; a leak's must
    survive it or the alert repeats every minute while the floor is wet."""
    from app import db
    await db.upsert_smart_alert_state("EC:EC:00:00:00:01", "leak:1", 1, 1)
    await db.upsert_smart_alert_state("EC:EC:00:00:00:01", "frost", 1, 1)
    await db.clear_smart_alert_states()
    states = await db.get_smart_alert_states()
    assert states.get(("EC:EC:00:00:00:01", "leak:1")) == 1
    assert ("EC:EC:00:00:00:01", "frost") not in states
