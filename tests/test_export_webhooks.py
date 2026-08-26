"""Pillar B basics: CSV export and outbound webhooks."""
from __future__ import annotations

import asyncio
import datetime as _dt
import time

AUTH = {"Authorization": "Bearer test-api-token"}
INGEST = {"Authorization": "Bearer test-ingest-token"}


def _ingest(client, mac_id: str, tempf: float, minutes_ago: int = 0):
    ts = (_dt.datetime.now(_dt.timezone.utc)
          - _dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.post("/ingest/custom", headers=INGEST,
                    json={"device": {"id": mac_id, "name": "Exporter"},
                          "timestamp_utc": ts,
                          "outdoor": {"tempf": tempf, "humidity": 40.0},
                          "source": "test"})
    assert r.status_code == 200


def test_csv_export_streams_range(client):
    _ingest(client, "AABBCC000008", 100.0, minutes_ago=10)
    _ingest(client, "AABBCC000008", 101.5, minutes_ago=5)
    r = client.get("/api/devices/AA:BB:CC:00:00:08/export.csv", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = [ln for ln in r.text.strip().splitlines() if ln]
    header = lines[0].split(",")
    assert header[0] == "timestamp_utc"
    assert "tempf" in header and "humidity" in header
    assert "data_json" not in header and "mac" not in header
    assert len(lines) == 3                     # header + 2 rows, ascending
    first, second = lines[1].split(","), lines[2].split(",")
    ti = header.index("tempf")
    assert float(first[ti]) == 100.0 and float(second[ti]) == 101.5
    # Absent readings are EMPTY cells, never zero.
    wi = header.index("windspeedmph")
    assert first[wi] == ""


def test_csv_export_rejects_inverted_range(client):
    r = client.get("/api/devices/AA:BB:CC:00:00:08/export.csv"
                   "?start_ms=2000&end_ms=1000", headers=AUTH)
    assert r.status_code == 400


def test_csv_export_requires_token(client):
    r = client.get("/api/devices/AA:BB:CC:00:00:08/export.csv")
    assert r.status_code in (401, 403)


def test_webhook_crud_and_dispatch(client, monkeypatch):
    from app import webhooks as wh

    # SSRF gate: private/loopback and plain http are refused.
    r = client.post("/api/webhooks", headers=AUTH,
                    json={"url": "http://example.com/hook"})
    assert r.status_code == 400
    r = client.post("/api/webhooks", headers=AUTH,
                    json={"url": "https://127.0.0.1/hook"})
    assert r.status_code == 400

    # Public https accepted (resolution stubbed so CI needs no network).
    monkeypatch.setattr(wh, "validate_webhook_url", lambda url: None)
    import app.main as m
    r = client.post("/api/webhooks", headers=AUTH,
                    json={"url": "https://hooks.example.com/zasder"})
    assert r.status_code == 200
    made = r.json()
    assert made["secret"] and made["id"]

    listed = client.get("/api/webhooks", headers=AUTH).json()["webhooks"]
    assert len(listed) == 1
    assert "secret" not in listed[0], "secret is one-shot at creation"

    # Dispatch: capture the outbound POST, verify payload + signature.
    sent = {}

    async def fake_send(hook, payload):
        sent["hook"] = hook
        sent["payload"] = payload

    monkeypatch.setattr(wh, "_send", fake_send)
    asyncio.run(wh.dispatch_alert("storm", "AA:BB:CC:00:00:08",
                                  "Storm Summary", "0.42 in", 1234567))
    import json
    body = json.loads(sent["payload"])
    assert body == {"event": "alert", "kind": "storm",
                    "mac": "AA:BB:CC:00:00:08", "title": "Storm Summary",
                    "body": "0.42 in", "ts_ms": 1234567,
                    "severity": None}
    sig = wh.sign(sent["hook"]["secret"], sent["payload"])
    assert len(sig) == 64

    r = client.delete(f"/api/webhooks/{made['id']}", headers=AUTH)
    assert r.status_code == 200
    assert client.get("/api/webhooks", headers=AUTH).json()["webhooks"] == []
    assert client.delete(f"/api/webhooks/{made['id']}",
                         headers=AUTH).status_code == 404
