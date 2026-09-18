"""The 2026-09-16 deep review's map publication findings (S1, S2, S3),
converted from its defect probes into pins of the DESIRED behaviour. The
backend's real handlers and state machine post to the directory's real
FastAPI app over an in-process transport, with the directory's clock
pinned so its one-a-minute throttles can be stepped past deliberately.

Skipped cleanly when `map-directory/` is not alongside (a checkout that
dropped it) or its dependencies are not in this venv, like test_map_parity.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

DIRECTORY_MAIN = Path(__file__).resolve().parents[2] / "map-directory" / "app" / "main.py"

pytestmark = pytest.mark.skipif(
    not DIRECTORY_MAIN.exists(),
    reason="map-directory/ is not alongside backend/ in this checkout")

MAC = "AA:BB:CC:DD:EE:71"
MAC_B = "AA:BB:CC:DD:EE:72"


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("these tests never reach a real directory")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def _station(now_ms: int, mac: str = MAC) -> dict:
    return {"mac": mac, "name": "Probe",
            "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}},
            "lastData": {"dateutc": now_ms - 1000, "tempf": 80}}


class Directory:
    """The directory app on a temp database, its clock ours to move, and
    the backend's `_post` wired to it. `seen` is every (path, status)."""

    def __init__(self, tmp_path: Path, monkeypatch, now_ms: int):
        spec = importlib.util.spec_from_file_location("map_directory_sequences", DIRECTORY_MAIN)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, mod)
        try:
            spec.loader.exec_module(mod)
        except ImportError as e:
            pytest.skip(f"map-directory dependency missing from this venv: {e}")
        monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "directory.db"))
        self.clock = [now_ms]
        monkeypatch.setattr(mod, "time", SimpleNamespace(
            **(vars(time) | {"time": lambda: self.clock[0] / 1000})))
        self.mod = mod
        self.seen: list[tuple[str, int]] = []
        self.down = False
        self.lose_next_ack = False
        self.client: httpx.AsyncClient | None = None

    async def start(self):
        await self.mod.init_db()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.mod.app),
                                        base_url="http://directory.test")
        from app import map_beacon as mb

        async def post(path, envelope):
            if self.down:
                return "ConnectError", {}
            r = await self.client.post(path, json=envelope)
            self.seen.append((path, r.status_code))
            if self.lose_next_ack and r.status_code == 200:
                self.lose_next_ack = False
                return "ReadTimeout", {}       # the directory committed; we never heard
            if r.status_code == 200:
                return None, r.json()
            return mb._explain(r.status_code, r.text), {}
        self._post = post
        return self

    async def pins(self) -> int:
        return (await self.client.get("/v1/beacons")).json()["count"]

    async def pinned_key(self, server_id: str) -> str | None:
        import aiosqlite
        async with aiosqlite.connect(self.mod.DB_PATH) as db:
            row = await (await db.execute(
                "SELECT pubkey FROM servers WHERE server_id = ?", (server_id,))).fetchone()
        return row[0] if row else None

    async def close(self):
        await self.client.aclose()


# ───────────────────────── S1: nothing publishes after opt-out ───────────────

def test_a_verify_in_flight_during_opt_out_does_not_republish(client, tmp_path, monkeypatch):
    """Save and verify checked the switch, then awaited the device list;
    a PUT that turned sharing off in that gap withdrew the pin, and the
    resumed verify signed a fresh beacon and put it straight back, with
    the config saying off and one live pin for three hours. The switch
    is now re-read under the publication lock the off transition holds
    around its config write and tombstone."""
    from app import main, db, map_beacon as mb

    async def run():
        now = int(time.time() * 1000)
        d = await Directory(tmp_path, monkeypatch, now).start()
        monkeypatch.setattr(mb, "_post", d._post)
        monkeypatch.setattr(main, "time", SimpleNamespace(
            **(vars(time) | {"time": lambda: d.clock[0] / 1000})))
        devices = [_station(now)]
        entered, release = asyncio.Event(), asyncio.Event()
        calls = [0]

        async def list_devices():
            calls[0] += 1
            if calls[0] == 1:                    # the verify's read, and only that one
                entered.set()
                await release.wait()
            return devices
        monkeypatch.setattr(db, "list_devices", list_devices)
        await mb.set_config({"enabled": True, "mac": MAC})
        assert (await mb.publish_once(devices, d.clock[0]))["ok"]
        assert await d.pins() == 1
        d.clock[0] += 61_000
        verify = asyncio.create_task(main.test_map_share())
        await entered.wait()                     # the on/off guard passed; device fetch pending
        off = await main.put_map_share(main.MapSharePut(enabled=False))
        assert b'"enabled":false' in off.body
        assert await d.pins() == 0
        d.clock[0] += 1
        release.set()
        result = await verify
        body = result.body.decode()
        assert '"ok":false' in body and mb.SHARING_OFF in body, body
        assert not (await mb.get_config())["enabled"]
        assert await d.pins() == 0, "no beacon after the owner opted out"
        assert d.seen == [("/v1/beacons", 200), ("/v1/withdraw", 200)]
        # The refusal is not an error the owner is shown: they asked for it.
        assert not (await mb.get_status()).get("last_error")
        await d.close()
    asyncio.run(run())


def test_the_off_transition_and_a_publish_serialise_either_way(client, tmp_path, monkeypatch):
    """The other order: a beacon already inside the lock finishes, and the
    off transition that queued behind it withdraws with a LATER stamp, so
    the map ends empty whichever side won the lock."""
    from app import main, map_beacon as mb

    async def run():
        now = int(time.time() * 1000)
        d = await Directory(tmp_path, monkeypatch, now).start()
        real_post = d._post
        sending, proceed = asyncio.Event(), asyncio.Event()

        async def slow_post(path, envelope):
            if path == "/v1/beacons" and not sending.is_set():
                sending.set()
                await proceed.wait()
            return await real_post(path, envelope)
        monkeypatch.setattr(mb, "_post", slow_post)
        monkeypatch.setattr(main, "time", SimpleNamespace(
            **(vars(time) | {"time": lambda: d.clock[0] / 1000})))
        devices = [_station(now)]
        await mb.set_config({"enabled": True, "mac": MAC})
        publish = asyncio.create_task(mb.publish_once(devices, d.clock[0]))
        await sending.wait()                     # signed, mid-send, holding the lock
        off = asyncio.create_task(main.put_map_share(main.MapSharePut(enabled=False)))
        await asyncio.sleep(0.05)
        assert not off.done(), "the off transition waits for the send to finish"
        proceed.set()
        assert (await publish)["ok"]
        await off
        assert d.seen == [("/v1/beacons", 200), ("/v1/withdraw", 200)]
        assert await d.pins() == 0
        await d.close()
    asyncio.run(run())


# ───────────────────────── S2: a lost acknowledgement, then another rotate ───

def test_a_rotation_after_a_lost_acknowledgement_still_ends_with_an_accepted_beacon(client, tmp_path, monkeypatch):
    """Key A pinned. Rotate to B; the directory accepts B but the reply is
    lost. Rotating again used to overwrite B with C while keeping A as the
    blesser, and the directory, holding B, refused A's blessing of C for
    good: every beacon and withdrawal 403'd until the operator forgot the
    server. Now a pending rotation is resolved with a beacon first, and
    only then does the key rotate."""
    from app import map_beacon as mb
    from app.main import ensure_server_id

    async def run():
        now = int(time.time() * 1000)
        d = await Directory(tmp_path, monkeypatch, now).start()
        monkeypatch.setattr(mb, "_post", d._post)
        devices = [_station(now)]
        await mb.set_config({"enabled": True, "mac": MAC})
        assert (await mb.publish_once(devices, d.clock[0]))["ok"]
        server_id = await ensure_server_id()
        key_a = mb.public_key_b64(await mb.ensure_key())
        assert await d.pinned_key(server_id) == key_a

        d.clock[0] += 61_000
        d.lose_next_ack = True
        out = await mb.rotate(devices, d.clock[0])
        key_b = out["pubkey"]
        assert not out["published"]["ok"] and out["rotation_pending"] is True
        assert await d.pinned_key(server_id) == key_b, "the directory DID take B"
        assert mb.public_key_b64(await mb._prev_key()) == key_a

        # Rotating again: the pending hand-over is resolved by a beacon
        # signed with B (accepted, PREV cleared), then C is minted and
        # blessed by B. The directory's one-a-minute throttle holds C's
        # own beacon until the next tick; that is a wait, not a fault.
        d.clock[0] += 61_000
        with pytest.raises(mb.RotationRefused):
            await mb.rotate_key()                # the bare mint refuses while pending
        out = await mb.rotate(devices, d.clock[0])
        key_c = out["pubkey"]
        assert key_c not in (key_a, key_b)
        assert mb.public_key_b64(await mb._prev_key()) == key_b, "B blesses C, never A"
        assert out["published"]["throttled"] is True
        d.clock[0] += 61_000
        res = await mb.publish_once(devices, d.clock[0])
        assert res["ok"], res
        assert await d.pinned_key(server_id) == key_c
        assert not await mb.rotation_pending()
        assert d.seen[-1] == ("/v1/beacons", 200)
        assert all(status != 403 for _, status in d.seen), d.seen
        # Withdrawals work with the keys the server holds.
        d.clock[0] += 61_000
        assert (await mb.withdraw_mac(MAC, d.clock[0]))["ok"]
        assert await d.pins() == 0
        await d.close()
    asyncio.run(run())


def test_a_rotation_is_refused_while_the_directory_cannot_resolve_the_pending_one(client, tmp_path, monkeypatch):
    """With the directory down nothing can be resolved, so the second
    rotate is refused in a sentence and no key changes hands."""
    from app import map_beacon as mb

    async def run():
        now = int(time.time() * 1000)
        d = await Directory(tmp_path, monkeypatch, now).start()
        monkeypatch.setattr(mb, "_post", d._post)
        devices = [_station(now)]
        await mb.set_config({"enabled": True, "mac": MAC})
        assert (await mb.publish_once(devices, d.clock[0]))["ok"]
        d.down = True
        d.clock[0] += 61_000
        out = await mb.rotate(devices, d.clock[0])
        key_b = out["pubkey"]
        assert out["rotation_pending"] is True
        with pytest.raises(mb.RotationRefused, match="still waiting"):
            await mb.rotate(devices, d.clock[0] + 61_000)
        assert mb.public_key_b64(await mb.ensure_key()) == key_b
        assert await mb.rotation_pending()
        await d.close()
    asyncio.run(run())


# ───────────────────────── S3: one stamp per message ─────────────────────────

def test_a_queued_withdrawal_and_the_beacon_in_the_same_tick_both_land(client, tmp_path, monkeypatch):
    """Station A published; the owner switched to B while the directory
    was unreachable (A's withdrawal queued). The next healthy tick
    withdrew A and published B with the SAME `now_ms`; the directory had
    just accepted that stamp for the tombstone and 409'd the beacon, and
    B was off the map until the next interval. Every outgoing message now
    carries a stamp strictly later than the last one sent."""
    from app import main, db, map_beacon as mb

    async def run():
        now = int(time.time() * 1000)
        d = await Directory(tmp_path, monkeypatch, now).start()
        monkeypatch.setattr(mb, "_post", d._post)
        monkeypatch.setattr(main, "time", SimpleNamespace(
            **(vars(time) | {"time": lambda: d.clock[0] / 1000})))
        devices = [_station(now), _station(now, MAC_B)]

        async def list_devices():
            return devices
        monkeypatch.setattr(db, "list_devices", list_devices)
        await main.put_map_share(main.MapSharePut(enabled=True, mac=MAC))
        assert (await mb.publish_once(devices, d.clock[0]))["ok"]
        d.down = True
        d.clock[0] += 10_000
        r = await main.put_map_share(main.MapSharePut(mac=MAC_B))
        assert mb.WITHDRAW_PENDING.encode()[:20] in r.body
        assert [p["mac"] for p in await mb.pending_withdrawals()] == [MAC]
        d.down = False
        d.clock[0] += 61_000
        await mb.publish_if_due(devices, d.clock[0])
        assert d.seen[-2:] == [("/v1/withdraw", 200), ("/v1/beacons", 200)], d.seen
        assert await d.pins() == 1
        assert not await mb.pending_withdrawals()
        listed = (await d.client.get("/v1/beacons")).json()
        from app.main import ensure_server_id
        sid_b = mb.station_id(await ensure_server_id(), MAC_B)
        assert sid_b in str(listed)
        await d.close()
    asyncio.run(run())


def test_switching_back_on_drops_the_obsolete_withdrawal_and_the_pin_stays(client, tmp_path, monkeypatch):
    """The review's exact sequence: publish, opt out while the directory
    is unreachable (withdrawal queued), opt back in, tick. The queued
    tombstone is obsolete the moment the station is back on; sending it
    took the pin down, and the same-stamp beacon that followed was 409'd,
    so the map showed nothing for ten minutes. Now the re-enable drops it
    and the tick's beacon renews the pin."""
    from app import main, db, map_beacon as mb

    async def run():
        now = int(time.time() * 1000)
        d = await Directory(tmp_path, monkeypatch, now).start()
        monkeypatch.setattr(mb, "_post", d._post)
        monkeypatch.setattr(main, "time", SimpleNamespace(
            **(vars(time) | {"time": lambda: d.clock[0] / 1000})))
        devices = [_station(now)]

        async def list_devices():
            return devices
        monkeypatch.setattr(db, "list_devices", list_devices)
        await main.put_map_share(main.MapSharePut(enabled=True, mac=MAC))
        assert (await mb.publish_once(devices, d.clock[0]))["ok"]
        d.down = True
        d.clock[0] += 10_000
        await main.put_map_share(main.MapSharePut(enabled=False))
        assert [p["mac"] for p in await mb.pending_withdrawals()] == [MAC]
        await main.put_map_share(main.MapSharePut(enabled=True))
        assert not await mb.pending_withdrawals(), "back on: the queued tombstone is obsolete"
        d.down = False
        d.clock[0] += 61_000
        await mb.publish_if_due(devices, d.clock[0])
        assert d.seen == [("/v1/beacons", 200), ("/v1/beacons", 200)]
        assert await d.pins() == 1
        await d.close()
    asyncio.run(run())


def test_outgoing_stamps_are_strictly_increasing_and_survive_a_clock_that_stands_still(client):
    """max(now, last + 1), persisted: two messages in one millisecond, or
    a clock that stepped back, never reuse a stamp the directory has."""
    from app import db, map_beacon as mb

    async def run():
        assert await mb._next_sent_ms(1_000) == 1_000
        assert await mb._next_sent_ms(1_000) == 1_001
        assert await mb._next_sent_ms(900) == 1_002
        assert await mb._next_sent_ms(5_000) == 5_000
        assert await db.get_kv(mb.SENT_KEY) == "5000"
        await db.set_kv(mb.SENT_KEY, "not a number")
        assert await mb._next_sent_ms(7_000) == 7_000
    asyncio.run(run())
