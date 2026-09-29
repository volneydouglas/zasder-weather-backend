"""The watering call (2.5, C6)."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import db, watering  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}
MAC = "5D:5D:05:00:00:01"


def test_extraterrestrial_radiation_matches_fao56_example_8():
    # FAO-56 example 8: 20°S on 3 September, Ra = 32.2 MJ m-2 day-1.
    ra_mm = watering.extraterrestrial_mm(-20.0, 246)
    assert abs(ra_mm / 0.408 - 32.2) < 0.3


def test_a_phoenix_summer_day_loses_about_a_third_of_an_inch():
    et = watering.hargreaves_et0_in(80.0, 108.0, 33.3, 190)
    assert 0.25 < et < 0.40
    assert watering.hargreaves_et0_in(90, 80, 33.3, 190) is None


def _days(rain, et):
    return [{"day": f"d{i}", "rain_in": r, "et_in": e}
            for i, (r, e) in enumerate(zip(rain, et))]


def test_verdicts():
    dry = watering.verdict(_days([0] * 7, [0.3] * 7))
    assert dry["verdict"] == "water" and dry["balance_in"] == -2.1
    wet = watering.verdict(_days([0, 0, 0, 0, 0, 0, 0.5], [0.2] * 7))
    assert wet["verdict"] == "skip", "a real rain in the last two days"
    light = watering.verdict(_days([0.1] * 7, [0.13] * 7))
    assert light["verdict"] == "light"
    covered = watering.verdict(_days([0.2] * 7, [0.1] * 7))
    assert covered["verdict"] == "skip"


def test_unmeasured_days_are_not_dry_days():
    call = watering.verdict(_days([None] * 4 + [0, 0, 0], [0.3] * 7))
    assert call["verdict"] is None and call["days_used"] == 3


def _seed(tz_today: date):
    async def go():
        await db.upsert_device(MAC, {"name": "Yard", "info": {
            "coords": {"coords": {"lat": 33.3, "lon": -111.8}}}})
        async with db.connect() as conn:
            for k in range(1, 8):
                d = (tz_today - timedelta(days=k)).isoformat()
                await conn.execute(
                    "INSERT INTO daily_rollups (mac, day, tempf_min, tempf_max, rain_total) "
                    "VALUES (?, ?, 80, 106, 0)", (MAC, d))
            await conn.commit()
    asyncio.run(go())


def test_the_route_answers_for_the_last_seven_days(client):
    from zoneinfo import ZoneInfo
    from app.config import settings
    try:
        today = datetime.now(ZoneInfo(settings.timezone)).date()
    except Exception:
        today = datetime.now(timezone.utc).date()
    _seed(today)
    body = client.get(f"/api/devices/{MAC}/watering", headers=H).json()
    assert body["verdict"] == "water" and body["days_used"] == 7
    assert body["method"] == "hargreaves" and body["rain_in"] == 0


def test_the_morning_call_goes_to_webhooks_once(client, monkeypatch):
    from app import webhooks
    sent = []

    async def fake(event, data):
        sent.append((event, data["verdict"]))
    monkeypatch.setattr(webhooks, "dispatch_event", fake)

    async def hooks(enabled_only=False):
        return [{"id": 1}]
    monkeypatch.setattr(db, "list_webhooks", hooks)
    from zoneinfo import ZoneInfo
    from app.config import settings
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    local = datetime.now(tz).replace(hour=6, minute=0, second=0, microsecond=0)
    _seed(local.date())
    devices = asyncio.run(db.list_devices())
    now_ms = int(local.timestamp() * 1000)
    assert asyncio.run(watering.send_if_due(devices, now_ms)) is True
    assert asyncio.run(watering.send_if_due(devices, now_ms + 60_000)) is False
    assert sent == [("watering", "water")]
    early = int(local.replace(hour=4).timestamp() * 1000)
    asyncio.run(db.set_kv("watering.sent_day", None))
    assert asyncio.run(watering.send_if_due(devices, early)) is False


def test_a_morning_with_no_call_yet_tries_again_later(client, monkeypatch):
    """Greptile, PR #48: the day was marked sent before the verdict was
    known, so a first tick with too few measured days (or no location)
    silenced the day even after the data arrived."""
    from app import webhooks
    sent = []

    async def fake(event, data):
        sent.append(data["verdict"])
    monkeypatch.setattr(webhooks, "dispatch_event", fake)

    async def hooks(enabled_only=False):
        return [{"id": 1}]
    monkeypatch.setattr(db, "list_webhooks", hooks)
    from zoneinfo import ZoneInfo
    from app.config import settings
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    local = datetime.now(tz).replace(hour=5, minute=1, second=0, microsecond=0)
    now_ms = int(local.timestamp() * 1000)
    asyncio.run(db.upsert_device(MAC, {"name": "Yard", "info": {
        "coords": {"coords": {"lat": 33.3, "lon": -111.8}}}}))
    devices = asyncio.run(db.list_devices())
    assert asyncio.run(watering.send_if_due(devices, now_ms)) is False
    assert sent == []
    _seed(local.date())
    devices = asyncio.run(db.list_devices())
    assert asyncio.run(watering.send_if_due(devices, now_ms + 3_600_000)) is True
    assert sent == ["water"]


def test_a_day_the_station_barely_covered_is_not_a_measured_day(client, monkeypatch):
    """R25-A03 (the 2.5 additional review): four days of one reading each
    made tmin == tmax, so ET read 0 and the call was a confident "skip"."""
    monkeypatch.setattr(db.settings, "insights", True)
    base = int(datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp() * 1000)

    async def go():
        await db.upsert_device(MAC, {"name": "Partial"})
        await db.insert_observations(MAC, [
            {"dateutc": base - k * 86_400_000 + 12 * 3_600_000,
             "tempf": 80.0, "dailyrainin": 0.0} for k in range(4, 0, -1)])
        return await watering.for_station(MAC, date(2026, 9, 27), 33.3)
    result = asyncio.run(go())
    assert result["days_used"] == 0 and result["verdict"] is None, result
