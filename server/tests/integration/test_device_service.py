"""DeviceService with a synthetic peer on the actual loopback TLS gateway."""

import asyncio
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hoodini import storage
from hoodini.certs import ensure_cert
from hoodini.devices.models import CommandResult
from hoodini.devices.service import DeviceService, ForgetError
from hoodini.diagnostics import DiagnosticSink
from hoodini.gateway.events import AckReceived, SessionClosed, SessionOpened
from hoodini.gateway.server import Gateway
from hoodini.protocol.frames import Ack
from hoodini.security import TrustStore
from hoodini.storage import AtomicStore, AtomicWriteError
from support.clock import Clock
from support.hood import Hood


def setup(tmp_path, *, clock=None, trust=None):
    cert, key = ensure_cert(tmp_path / "cert")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    store = AtomicStore(tmp_path / "state")
    service = DeviceService(store, clock=clock, epoch=lambda: 1234)
    trust = trust or TrustStore(tmp_path / "trust")
    gateway = Gateway(
        "127.0.0.1",
        0,
        tls,
        trust,
        DiagnosticSink(),
        service.handle_gateway,
        clock=clock,
        epoch=lambda: 1,
    )
    service.attach_gateway(gateway)
    return service, gateway, store, trust


async def open_ready(gateway, *, cuid="hood1", caps=b"\x60\x32\x00"):
    hood = await Hood.open(gateway.port, cuid)
    assert await hood.connect(keepalive=0) == b"\x20\x02\x00\x00"
    assert await hood.subscribe("op/mode") == b"\x90\x03\x00\x01\x00"
    await hood.publish("connect", b"\x01\x01\x02\x00\x03\x07")
    await hood.packet()  # connected
    await hood.packet()  # time
    # status: server epoch 1, RSSI -40, partial capability values
    body = b"\x00\x00\x00\x01\xd8\x75\x30\x00" + caps + b"\x00"
    await hood.publish("status", body + bytes((sum(body) % 256,)))
    for _ in range(100):
        await asyncio.sleep(0)
    return hood


def test_service_routes_session_opened_to_state(tmp_path):
    async def scenario():
        service, _, _, _ = setup(tmp_path)
        await service.start()
        try:
            await service.handle_gateway(SessionOpened("hood1", "synthetic", {2: 3}))
            assert service.get("hood1").firmware == {2: 3}
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_service_real_gateway_wiring_wire_ack_and_durable_projection(tmp_path):
    async def scenario():
        service, gateway, store, _ = setup(tmp_path)
        await service.start()
        await gateway.start()
        hood = await open_ready(gateway)
        try:
            view = service.get("hood1")
            assert view.session_state == "ready"
            assert view.caps == {96: 50} and view.known_ids == {96}
            assert view.last_seen == 1234 and view.firmware == {2: 3}
            # Independent literal partial status: fan 2; prior light must survive.
            await hood.publish(
                "status", b"\x00\x00\x00\x01\xd8\x75\x30\x00\x6e\x02\x00\x00\xee"
            )
            for _ in range(30):
                await asyncio.sleep(0)
            assert service.get("hood1").caps == {96: 50, 110: 2}
            assert service.get("hood1").known_ids == {96, 110}
            assert (service.get("hood1").last_light, service.get("hood1").last_fan) == (
                50,
                2,
            )
            command = asyncio.create_task(service.set_caps("hood1", {96: 5}))
            assert await hood.packet() == (
                b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                b"\x00\x2a\x8d\x00\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x09"
            )
            assert service.get("hood1").caps == {96: 50, 110: 2}  # no optimism
            await hood.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
            assert await command == CommandResult("ok", 0, 1)
            assert service.get("hood1").caps == {96: 50, 110: 2}
            assert store.load().devices["hood1"].caps == {96: 50, 110: 2}
        finally:
            await hood.close()
            await gateway.stop()
            await service.stop()

    asyncio.run(scenario())


def test_service_real_forget_removes_trust_and_state_and_permits_reenrollment(tmp_path):
    async def scenario():
        service, gateway, store, trust = setup(tmp_path)
        await service.start()
        await gateway.start()
        hood = await open_ready(gateway)
        try:
            assert "hood1" in trust._path.read_text()  # synthetic trust presence
            await service.forget("hood1")
            assert service.get("hood1") is None
            snapshot = store.load()
            assert "hood1" not in snapshot.devices
            assert "hood1" in snapshot.pending_removals
            assert "hood1" not in trust._path.read_text()
            assert await asyncio.wait_for(hood.reader.read(), 2) == b""
            second = await open_ready(gateway)
            try:
                assert service.get("hood1").session_state == "ready"
            finally:
                await second.close()
        finally:
            await hood.close()
            await gateway.stop()
            await service.stop()

    asyncio.run(scenario())


def test_service_fast_ack_and_stale_close_do_not_retire_replacement(tmp_path):
    async def scenario():
        service, gateway, _, _ = setup(tmp_path)
        await service.start()
        await gateway.start()
        hood = await open_ready(gateway)
        other = None
        try:
            old = service.get("hood1").connection_id
            other = await open_ready(gateway)
            new = service.get("hood1").connection_id
            assert new != old
            await service.handle_gateway(SessionClosed("hood1", old, "old"))
            assert service.get("hood1").connection_id == new
            command = asyncio.create_task(service.set_caps("hood1", {96: 5}))
            assert (await other.packet()).startswith(b"\x30\x2a")
            await service.handle_gateway(AckReceived("hood1", old, Ack(0, 1)))
            assert not command.done()
            await other.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
            assert await command == CommandResult("ok", 0, 1)
        finally:
            await hood.close()
            if other:
                await other.close()
            await gateway.stop()
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "phase,committed",
    [
        ("intent", False),
        ("intent", True),
        ("trust", False),
        ("trust", True),
        ("trust", None),
        ("state", False),
        ("state", True),
    ],
)
def test_real_forget_atomic_phases_preserve_visible_truth(
    tmp_path, monkeypatch, phase, committed
):
    async def scenario():
        service, gateway, store, trust = setup(tmp_path)
        await service.start()
        await gateway.start()
        hood = await open_ready(gateway)
        try:
            if phase == "trust":
                original = trust.forget

                def fail_trust(cuid):
                    if committed is True:
                        original(cuid)
                    raise AtomicWriteError(committed)

                monkeypatch.setattr(trust, "forget", fail_trust)
            else:
                original = storage.atomic_write

                def fail_state(path, payload):
                    if path == store._path and (
                        (
                            phase == "intent"
                            and b'"pending_removals":["hood1"]' in payload
                            and b'"hood1":{' in payload
                        )
                        or (
                            phase == "state"
                            and b'"pending_removals":["hood1"]' in payload
                            and b'"hood1":{' not in payload
                        )
                    ):
                        if committed:
                            original(path, payload)
                        raise AtomicWriteError(committed)
                    original(path, payload)

                monkeypatch.setattr(storage, "atomic_write", fail_state)
            with pytest.raises(ForgetError) as caught:
                await service.forget("hood1")
            assert caught.value.phases == ((phase, committed),)
            assert "synthetic_sensitive_marker" not in repr(caught.value.args)
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            snapshot = store.load()
            row_gone = (
                phase == "trust"
                and committed is True
                or phase == "state"
                and committed is True
            )
            assert ("hood1" not in snapshot.devices) == row_gone
            assert (service.get("hood1") is None) == row_gone
            assert ("hood1" in snapshot.pending_removals) == (
                phase != "intent" or committed is True
            )
            assert ("hood1" not in trust._path.read_text()) == (
                phase == "state" or phase == "trust" and committed is True
            )
            assert await asyncio.wait_for(hood.reader.read(), 2) == b""
            if not row_gone:
                await service.stop()
                await service.start()
                assert service.get("hood1") is not None  # tombstone is MQTT-only
        finally:
            await hood.close()
            await gateway.stop()
            await service.stop()

    asyncio.run(scenario())


def test_caller_cancel_during_real_offloaded_trust_retains_fence_until_terminal(
    tmp_path, monkeypatch
):
    async def scenario():
        service, gateway, store, trust = setup(tmp_path)
        await service.start()
        await gateway.start()
        hood = await open_ready(gateway)
        entered = __import__("threading").Event()
        release = __import__("threading").Event()
        original = trust.forget

        def gated(cuid):
            entered.set()
            assert release.wait(3)
            original(cuid)

        monkeypatch.setattr(trust, "forget", gated)
        task = asyncio.create_task(service.forget("hood1"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(ForgetError) as busy:
                await service.forget("hood1")
            assert busy.value.phases == (("fence", None),)
            assert "hood1" in store.load().devices
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.get("hood1") is None
            assert "hood1" not in trust._path.read_text()
            second = await open_ready(gateway)
            try:
                assert service.get("hood1").session_state == "ready"
            finally:
                await second.close()
        finally:
            release.set()
            await hood.close()
            await gateway.stop()
            await service.stop()

    asyncio.run(scenario())


def test_real_gateway_observers_metadata_grace_and_restart(tmp_path):
    async def scenario():
        clock = Clock()
        service, gateway, store, _ = setup(tmp_path, clock=clock)
        await service.start()
        await gateway.start()
        healthy = []

        async def slow(event):
            await asyncio.Event().wait()

        broken = service.subscribe(slow)
        good = service.subscribe(lambda event: healthy.append(event.kind))
        hood = await open_ready(gateway)
        try:
            view = service.get("hood1")
            assert view.online and view.session_state == "ready"
            await service.update(
                "hood1", {"PRF": "PRF0182867A", "hidden": {"value": 1}}
            )
            assert service.get("hood1").info == {"PRF": "PRF0182867A"}
            view.info["forged"] = 1
            assert "forged" not in service.list()[0].info
            await gateway.disconnect("hood1")
            for _ in range(30):
                await asyncio.sleep(0)
            assert service.get("hood1").online
            assert (await service.set_caps("hood1", {96: 5})).status == "offline"
            await clock.advance(19.9)
            assert service.get("hood1").online
            await clock.advance(0.1)
            assert not service.get("hood1").online
            assert healthy[:2] == ["added", "availability"]
        finally:
            good()
            broken()
            assert await good.wait_closed() == "unsubscribed"
            assert await broken.wait_closed() == "unsubscribed"
            await hood.close()
            await gateway.stop()
            await service.stop()
        assert store.load().devices["hood1"].info["hidden"] == {"value": 1}
        await service.start()
        try:
            assert service.get("hood1").session_state == "none"
            assert not service.get("hood1").online
            assert service.get("hood1").connection_id is None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_real_forget_combined_trust_state_peer_failures_are_sanitized(
    tmp_path, monkeypatch
):
    async def scenario():
        service, gateway, store, trust = setup(tmp_path)
        await service.start()
        await gateway.start()
        hood = await open_ready(gateway)
        original_trust = trust.forget
        original_write = storage.atomic_write
        original_disconnect = gateway.disconnect

        def uncertain_trust(cuid):
            original_trust(cuid)
            raise AtomicWriteError(True)

        def uncertain_state(path, payload):
            original_write(path, payload)
            if (
                path == store._path
                and b'"pending_removals":["hood1"]' in payload
                and b'"hood1":{' not in payload
            ):
                raise AtomicWriteError(True)

        async def failing_disconnect(cuid):
            await original_disconnect(cuid)
            raise ValueError("synthetic_sensitive_marker")

        monkeypatch.setattr(trust, "forget", uncertain_trust)
        monkeypatch.setattr(storage, "atomic_write", uncertain_state)
        monkeypatch.setattr(gateway, "disconnect", failing_disconnect)
        try:
            with pytest.raises(ForgetError) as caught:
                await service.forget("hood1")
            assert caught.value.phases == (
                ("trust", True),
                ("state", True),
                ("peer", None),
            )
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert "synthetic_sensitive_marker" not in repr(caught.value)
            assert service.get("hood1") is None
            assert "hood1" not in trust._path.read_text()
            assert await asyncio.wait_for(hood.reader.read(), 2) == b""
        finally:
            await hood.close()
            await gateway.stop()
            await service.stop()

    asyncio.run(scenario())


def test_real_gateway_blocked_command_write_uses_single_deadline(tmp_path, monkeypatch):
    async def scenario():
        clock = Clock()
        service, gateway, _, _ = setup(tmp_path, clock=clock)
        await service.start()
        await gateway.start()
        hood = await open_ready(gateway)
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        original_write = gateway._write

        async def blocked_write(session, raw, *, preauth=False):
            if raw.startswith(b"\x30\x2a\x00\x17v1/device/hood1/op/mode"):
                original_drain = session.writer.drain

                async def blocked_drain():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()
                        session.writer.drain = original_drain

                session.writer.drain = blocked_drain
            return await original_write(session, raw, preauth=preauth)

        monkeypatch.setattr(gateway, "_write", blocked_write)
        try:
            command = asyncio.create_task(service.set_caps("hood1", {96: 5}))
            await entered.wait()
            await clock.advance(3)
            assert await command == CommandResult("timeout", 0)
            assert cleaned.is_set()
            await hood.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
            assert not gateway._stopping
        finally:
            await hood.close()
            await gateway.stop()
            await service.stop()
            assert not any(not timer.cancelled for _, _, timer in clock.pending)

    asyncio.run(scenario())


def test_real_forget_cancels_active_queued_commands_and_orders_readd(
    tmp_path, monkeypatch
):
    async def scenario():
        service, gateway, store, _ = setup(tmp_path)
        await service.start()
        await gateway.start()
        events = []
        subscription = service.subscribe(lambda event: events.append(event.kind))
        hood = await open_ready(gateway)
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        original_write = gateway._write

        async def blocked(session, raw, *, preauth=False):
            if raw.startswith(b"\x30\x2a\x00\x17v1/device/hood1/op/mode"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return await original_write(session, raw, preauth=preauth)

        monkeypatch.setattr(gateway, "_write", blocked)
        try:
            active = asyncio.create_task(service.set_caps("hood1", {96: 5}))
            await entered.wait()
            queued = asyncio.create_task(service.set_caps("hood1", {96: 6}))
            for _ in range(10):
                await asyncio.sleep(0)
            await service.forget("hood1")
            assert await active == CommandResult("offline", 0)
            assert await queued == CommandResult("offline")
            assert cleaned.is_set()
            assert store.load().pending_removals == {"hood1"}
            replacement = await open_ready(gateway)
            try:
                for _ in range(30):
                    await asyncio.sleep(0)
                assert events.index("forgotten") < events.index(
                    "added", events.index("forgotten")
                )
                assert service.get("hood1").session_state == "ready"
            finally:
                await replacement.close()
        finally:
            subscription()
            assert await subscription.wait_closed() == "unsubscribed"
            await hood.close()
            await gateway.stop()
            await service.stop()

    asyncio.run(scenario())
