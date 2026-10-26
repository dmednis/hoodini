"""Synthetic TLS peers exercise the real endpoint without real devices or a broker."""

import asyncio
import json
import logging
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hoodini.certs import ensure_cert
from hoodini.diagnostics import DiagnosticSink
from hoodini.gateway.events import (
    AckReceived,
    CommandReady,
    SessionClosed,
    SessionOpened,
    StatusReceived,
)
from hoodini.gateway.server import Gateway, GatewayError
from hoodini.gateway.session import Session
from hoodini.security import TrustStore
from support.clock import Clock
from support.hood import Hood


def setup(tmp_path: Path, clock: Clock | None = None):
    cert, key = ensure_cert(tmp_path / "cert")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    events = []

    async def on_event(event):
        events.append(event)

    sink = DiagnosticSink()
    gateway = Gateway(
        "127.0.0.1",
        0,
        tls,
        TrustStore(tmp_path / "trust"),
        sink,
        on_event,
        clock=clock,
        epoch=lambda: 1,
    )
    return gateway, events, sink


def test_session_repr_never_contains_partial_credential_bytes():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"SECRET_SYNTHETIC_PASSWORD")
        session = Session(reader, object(), 0)
        session.seen[1] = b"SECRET_SYNTHETIC_PASSWORD"
        assert "SECRET_SYNTHETIC_PASSWORD" not in repr(session)

    asyncio.run(scenario())


def test_readiness_waits_for_command_subscription(tmp_path):
    async def scenario():
        gateway, events, sink = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x00"
            assert await hood.subscribe("connected") == b"\x90\x03\x00\x01\x00"
            assert await hood.subscribe("time", 3) == b"\x90\x03\x00\x03\x00"
            assert await hood.subscribe("sync", 4) == b"\x90\x03\x00\x04\x00"
            await hood.publish("connect", b"\x01\x01\x02\x00\x03\x07", packet_id=3)
            assert await hood.packet() == b"\x40\x02\x00\x03"
            first, second = await hood.packet(), await hood.packet()
            assert first == b"\x30\x1b\x00\x19v1/device/hood1/connected"
            assert second == b"\x30\x1b\x00\x14v1/device/hood1/time\x00\x00\x00\x01\x01"
            assert len([e for e in events if isinstance(e, SessionOpened)]) == 1
            assert events[0].firmware == {2: 3}
            assert not any(isinstance(e, CommandReady) for e in events)
            assert await hood.subscribe("op/mode", 2, 2) == b"\x90\x03\x00\x02\x00"
            assert len([e for e in events if isinstance(e, CommandReady)]) == 1
            session_id = events[0].session_id
            assert await gateway.send_command("hood1", session_id, 4, {96: 5})
            assert await hood.packet() == (
                b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                b"\x00\x2a\x8d\x04\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x0d"
            )
            await hood.publish("ping", b"", retain=True)
            assert await hood.packet() == second
            assert isinstance(sink.history[0]["timestamp"], float)
            assert {
                key: value
                for key, value in sink.history[0].items()
                if key != "timestamp"
            } == {
                "kind": "mqtt",
                "direction": "in",
                "packet_type": 1,
                "length": 36,
            }
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_concurrent_commands_are_whole_frames_and_ack_event_is_session_scoped(tmp_path):
    async def scenario():
        gateway, events, _ = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x00"
            await hood.publish("connect", b"\x01\x00\x01")
            await hood.packet()
            await hood.packet()
            await hood.subscribe()
            sid = next(e.session_id for e in events if isinstance(e, SessionOpened))
            assert await asyncio.gather(
                gateway.send_command("hood1", sid, 4, {96: 5}),
                gateway.send_command("hood1", sid, 5, {96: 5}),
            ) == [True, True]
            assert await hood.packet() == (
                b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                b"\x00\x2a\x8d\x04\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x0d"
            )
            assert await hood.packet() == (
                b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                b"\x00\x2a\x8d\x05\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x0e"
            )
            await hood.publish("ack", b"\x00\x2a\x8d\x04\x01\x00\x00\xbc", packet_id=8)
            assert await hood.packet() == b"\x40\x02\x00\x08"
            assert [
                (e.session_id, e.ack.seq, e.ack.result)
                for e in events
                if isinstance(e, AckReceived)
            ] == [(sid, 4, 1)]
            await gateway.disconnect("hood1")
            assert await hood.reader.read() == b""
            assert not await gateway.send_command("hood1", sid, 6, {96: 5})
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_stop_closes_ready_keepalive_zero_peer_before_waiting_for_listener(tmp_path):
    async def scenario():
        gateway, events, _ = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect(keepalive=0) == b"\x20\x02\x00\x00"
            await hood.publish("connect", b"\x01\x00\x01")
            assert await hood.packet() == b"\x30\x1b\x00\x19v1/device/hood1/connected"
            await hood.packet()
            assert await hood.subscribe() == b"\x90\x03\x00\x01\x00"
            ready = [e for e in events if isinstance(e, CommandReady)]
            assert len(ready) == 1
            await asyncio.wait_for(gateway.stop(), 2)
            assert await asyncio.wait_for(hood.reader.read(), 2) == b""
            assert [
                (e.cuid, e.session_id) for e in events if isinstance(e, SessionClosed)
            ] == [("hood1", ready[0].session_id)]
        finally:
            await hood.close()
            await asyncio.wait_for(gateway.stop(), 2)

    asyncio.run(scenario())


def test_replacement_fences_old_commands_and_emits_matching_close(tmp_path):
    async def scenario():
        gateway, events, _ = setup(tmp_path)
        await gateway.start()
        first = await Hood.open(gateway.port)
        second = None
        try:
            assert await first.connect() == b"\x20\x02\x00\x00"
            await first.publish("connect", b"\x01\x00\x01")
            await first.packet()
            await first.packet()
            assert await first.subscribe() == b"\x90\x03\x00\x01\x00"
            old_id = next(e.session_id for e in events if isinstance(e, SessionOpened))
            second = await Hood.open(gateway.port)
            assert await second.connect() == b"\x20\x02\x00\x00"
            await asyncio.sleep(0)
            assert await first.reader.read() == b""
            assert not await gateway.send_command("hood1", old_id, 1, {96: 5})
            for _ in range(20):
                if any(
                    isinstance(e, SessionClosed) and e.session_id == old_id
                    for e in events
                ):
                    break
                await asyncio.sleep(0.01)
            assert (
                len(
                    [
                        e
                        for e in events
                        if isinstance(e, SessionClosed) and e.session_id == old_id
                    ]
                )
                == 1
            )
            await second.publish("connect", b"\x01\x00\x01")
            await second.packet()
            await second.packet()
            assert len([e for e in events if isinstance(e, SessionOpened)]) == 2
            assert events[-1].session_id != old_id
        finally:
            await first.close()
            if second:
                await second.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_keepalive_and_absolute_handshake_deadlines(tmp_path):
    async def scenario():
        clock = Clock()
        gateway, events, _ = setup(tmp_path, clock)
        await gateway.start()
        pending = await Hood.open(gateway.port)
        live = None
        try:
            for _ in range(20):
                if clock.pending:
                    break
                await asyncio.sleep(0)
            assert clock.pending
            await clock.advance(10)
            assert await pending.reader.read() == b""
            live = await Hood.open(gateway.port, "other")
            assert await live.connect(keepalive=30) == b"\x20\x02\x00\x00"
            await clock.advance(10)
            assert await live.reader.read() == b""
            for _ in range(20):
                if any(isinstance(e, SessionClosed) for e in events):
                    break
                await asyncio.sleep(0.01)
            assert len([e for e in events if isinstance(e, SessionClosed)]) == 1
        finally:
            await pending.close()
            if live:
                await live.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_queued_auth_handshake_timeout_never_enrolls_trust(tmp_path):
    async def scenario():
        clock = Clock()
        gateway, events, sink = setup(tmp_path, clock)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        await gateway._auth_lock.acquire()
        try:
            connecting = asyncio.create_task(hood.connect())
            for _ in range(50):
                if any(r["packet_type"] == 1 for r in sink.history):
                    break
                await asyncio.sleep(0.01)
            assert any(r["packet_type"] == 1 for r in sink.history)
            await clock.advance(10)
            gateway._auth_lock.release()
            result = await asyncio.gather(connecting, return_exceptions=True)
            assert len(result) == 1 and isinstance(
                result[0], asyncio.IncompleteReadError
            )
            assert await asyncio.wait_for(hood.reader.read(), 2) == b""
            for _ in range(20):
                if not gateway._sessions:
                    break
                await asyncio.sleep(0.01)
            assert not (tmp_path / "trust" / "trust.json").exists()
            assert not events
        finally:
            if gateway._auth_lock.locked():
                gateway._auth_lock.release()
            await hood.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_queued_short_keepalive_candidate_cannot_replace_healthy_ready_peer(tmp_path):
    async def scenario():
        clock = Clock()
        gateway, events, sink = setup(tmp_path, clock)
        await gateway.start()
        old = await Hood.open(gateway.port)
        candidate = None
        try:
            assert await old.connect(keepalive=0) == b"\x20\x02\x00\x00"
            await old.publish("connect", b"\x01\x00\x01")
            await old.packet()
            await old.packet()
            assert await old.subscribe() == b"\x90\x03\x00\x01\x00"
            sid = next(e.session_id for e in events if isinstance(e, CommandReady))
            await gateway._auth_lock.acquire()
            candidate = await Hood.open(gateway.port)
            connecting = asyncio.create_task(candidate.connect(keepalive=1))
            for _ in range(50):
                if (
                    len(
                        [
                            r
                            for r in sink.history
                            if r["direction"] == "in" and r["packet_type"] == 1
                        ]
                    )
                    == 2
                ):
                    break
                await asyncio.sleep(0.01)
            assert (
                len(
                    [
                        r
                        for r in sink.history
                        if r["direction"] == "in" and r["packet_type"] == 1
                    ]
                )
                == 2
            )
            await clock.advance(2)
            gateway._auth_lock.release()
            result = await asyncio.gather(connecting, return_exceptions=True)
            assert len(result) == 1 and isinstance(
                result[0], asyncio.IncompleteReadError
            )
            assert await gateway.send_command("hood1", sid, 4, {96: 5})
            assert await old.packet() == (
                b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                b"\x00\x2a\x8d\x04\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x0d"
            )
            assert not any(
                isinstance(e, SessionClosed) and e.session_id == sid for e in events
            )
        finally:
            if gateway._auth_lock.locked():
                gateway._auth_lock.release()
            await old.close()
            if candidate:
                await candidate.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_auth_finishing_after_candidate_idle_deadline_preserves_old_peer(tmp_path):
    async def scenario():
        import threading

        clock = Clock()
        gateway, events, _ = setup(tmp_path, clock)
        await gateway.start()
        old = await Hood.open(gateway.port)
        candidate = None
        entered, release = threading.Event(), threading.Event()
        try:
            assert await old.connect(keepalive=0) == b"\x20\x02\x00\x00"
            await old.publish("connect", b"\x01\x00\x01")
            await old.packet()
            await old.packet()
            assert await old.subscribe() == b"\x90\x03\x00\x01\x00"
            sid = next(e.session_id for e in events if isinstance(e, CommandReady))
            original = gateway.trust.check_or_enroll

            def delayed_trust(cuid, password):
                result = original(cuid, password)
                entered.set()
                release.wait(3)
                return result

            gateway.trust.check_or_enroll = delayed_trust
            candidate = await Hood.open(gateway.port)
            connecting = asyncio.create_task(candidate.connect(keepalive=1))
            assert await asyncio.to_thread(entered.wait, 3)
            await clock.advance(2)
            release.set()
            result = await asyncio.gather(connecting, return_exceptions=True)
            assert len(result) == 1 and isinstance(
                result[0], asyncio.IncompleteReadError
            )
            assert await gateway.send_command("hood1", sid, 4, {96: 5})
            assert await old.packet() == (
                b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                b"\x00\x2a\x8d\x04\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x0d"
            )
            assert not any(
                isinstance(e, SessionClosed) and e.session_id == sid for e in events
            )
        finally:
            release.set()
            await old.close()
            if candidate:
                await candidate.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_qos1_dup_ack_without_duplicate_time_or_status(tmp_path):
    async def scenario():
        gateway, events, _ = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x00"
            await hood.publish("connect", b"\x01\x00\x01", packet_id=5, retain=True)
            assert await hood.packet() == b"\x40\x02\x00\x05"
            await hood.packet()
            await hood.packet()
            await hood.publish(
                "connect", b"\x01\x00\x01", packet_id=5, dup=True, retain=True
            )
            assert await hood.packet() == b"\x40\x02\x00\x05"
            assert len([e for e in events if isinstance(e, SessionOpened)]) == 1
            await hood.publish(
                "status", b"\x00\x00\x00\x01\xec\x75\x30\x00\x00\x92", packet_id=6
            )
            assert await hood.packet() == b"\x40\x02\x00\x06"
            await hood.publish(
                "status",
                b"\x00\x00\x00\x01\xec\x75\x30\x00\x00\x92",
                packet_id=6,
                dup=True,
            )
            assert await hood.packet() == b"\x40\x02\x00\x06"
            assert len([e for e in events if isinstance(e, StatusReceived)]) == 1
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_subscription_first_and_unsupported_qos2_and_will(tmp_path):
    async def scenario():
        gateway, events, _ = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x00"
            assert await hood.subscribe("op/mode") == b"\x90\x03\x00\x01\x00"
            assert not any(isinstance(e, CommandReady) for e in events)
            assert await hood.subscribe("+/mode", 2) == b"\x90\x03\x00\x02\x80"
            await hood.publish("connect", b"\x01\x00\x01")
            assert await hood.packet() == b"\x30\x1b\x00\x19v1/device/hood1/connected"
            await hood.packet()
            assert len([e for e in events if isinstance(e, CommandReady)]) == 1
            topic = b"v1/device/hood1/status"
            await hood.send(
                b"\x34"
                + bytes((2 + len(topic) + 2,))
                + len(topic).to_bytes(2, "big")
                + topic
                + b"\x00\x03"
            )
            assert await hood.reader.read() == b""
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_keepalive_zero_and_45_second_idle_expiry(tmp_path):
    async def scenario():
        clock = Clock()
        gateway, _, _ = setup(tmp_path, clock)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect(keepalive=30) == b"\x20\x02\x00\x00"
            await hood.publish("connect", b"\x01\x00\x01")
            await hood.packet()
            await hood.packet()
            await hood.subscribe()
            await clock.advance(44)
            assert not hood.reader.at_eof()
            await clock.advance(1)
            assert await hood.reader.read() == b""
            zero = await Hood.open(gateway.port, "zero")
            try:
                assert await zero.connect(keepalive=0) == b"\x20\x02\x00\x00"
                await zero.publish("connect", b"\x01\x00\x01")
                await zero.packet()
                await zero.packet()
                await zero.subscribe()
                await clock.advance(46)
                await zero.send(b"\xc0\x00")
                assert await zero.packet() == b"\xd0\x00"
            finally:
                await zero.close()
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_split_and_coalesced_frames_record_exact_boundaries(tmp_path):
    async def scenario():
        gateway, _, sink = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x00"
            await hood.send(b"\xc0")
            await hood.send(b"\x00\xc0\x00")
            assert await hood.packet() == b"\xd0\x00"
            assert await hood.packet() == b"\xd0\x00"
            assert [
                r["hex"]
                for r in sink.history
                if r["direction"] == "in" and r["packet_type"] == 12
            ] == ["c000", "c000"]
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_malformed_connect_suppresses_secret_and_partial_eof(tmp_path):
    async def scenario():
        gateway, events, sink = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        healthy = await Hood.open(gateway.port, "healthy")
        try:
            assert await healthy.connect() == b"\x20\x02\x00\x00"
            marker = b"SECRET_SYNTHETIC_MARKER"
            await hood.send(b"\x1f" + bytes((len(marker),)) + marker)
            assert await hood.reader.read() == b""
            assert isinstance(sink.history[-1]["timestamp"], float)
            assert {
                key: value
                for key, value in sink.history[-1].items()
                if key != "timestamp"
            } == {
                "kind": "mqtt",
                "direction": "in",
                "packet_type": 1,
                "length": len(marker) + 2,
            }
            partial = await Hood.open(gateway.port)
            await partial.send(b"\x10\x80")
            await partial.close()
            await asyncio.sleep(0.01)
            assert len([r for r in sink.history if r["packet_type"] == 1]) == 2
            assert not events
            await healthy.send(b"\xc0\x00")
            assert await healthy.packet() == b"\xd0\x00"
        finally:
            await hood.close()
            await healthy.close()
            await gateway.stop()

    asyncio.run(scenario())


def _connect_wire(cuid, password, *, will_flags=0):
    identity = cuid.encode("ascii")
    field = lambda value: len(value).to_bytes(2, "big") + value
    topic = b"synthetic/will/xx"
    payload = b"SYNTHETIC_WILL_XX"
    body = (
        b"\x00\x04MQTT\x04"
        + bytes((0xC2 | will_flags,))
        + b"\x00\x1e"
        + field(identity)
        + (field(topic) + field(payload) if will_flags else b"")
        + field(identity)
        + field(password)
    )
    return b"\x10" + bytes((len(body),)) + body


def test_hood_shaped_114_byte_connect_enrolls_reconnects_and_reaches_command_ready(
    tmp_path, caplog
):
    async def scenario():
        cuid = "synthetic0001"
        password = b"SECRET_BINARY_\xff" + b"z" * (32 - len(b"SECRET_BINARY_\xff"))
        wire = _connect_wire(cuid, password, will_flags=0x2C)
        assert len(wire) == 114
        caplog.set_level(logging.INFO, logger="hoodini.gateway")
        gateway, events, sink = setup(tmp_path)
        await gateway.start()
        try:
            for attempt in range(2):
                hood = await Hood.open(gateway.port, cuid)
                try:
                    await hood.send(wire)
                    assert await hood.packet() == b"\x20\x02\x00\x00"
                    await hood.publish("connect", b"\x01\x00\x01")
                    assert await hood.packet() == (
                        b"\x30\x23\x00\x21v1/device/synthetic0001/connected"
                    )
                    await hood.packet()  # time
                    assert (
                        len([e for e in events if isinstance(e, CommandReady)])
                        == attempt
                    )
                    assert await hood.subscribe() == b"\x90\x03\x00\x01\x00"
                    assert (
                        len([e for e in events if isinstance(e, CommandReady)])
                        == attempt + 1
                    )
                finally:
                    await hood.close()
            wrong = await Hood.open(gateway.port, cuid)
            try:
                await wrong.send(
                    _connect_wire(cuid, password[:-1] + b"q", will_flags=0x2C)
                )
                assert await wrong.packet() == b"\x20\x02\x00\x05"
            finally:
                await wrong.close()
            assert (tmp_path / "trust" / "trust.json").exists()
            assert "will=yes" in caplog.text
            assert "trust_mismatch" in caplog.text
            for forbidden in (
                "SECRET_BINARY",
                "synthetic/will/xx",
                "SYNTHETIC_WILL_XX",
            ):
                assert forbidden not in caplog.text
                assert all(forbidden not in str(event) for event in events)
                assert all(forbidden not in str(record) for record in sink.history)
        finally:
            await gateway.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "will_flags,binary",
    [(0x04, False), (0x0C, False), (0x14, False), (0x24, False), (0, True)],
)
def test_legal_will_or_binary_password_alone_is_accepted(tmp_path, will_flags, binary):
    async def scenario():
        gateway, _, _ = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            await hood.send(
                _connect_wire(
                    "hood1",
                    b"secret\xff" if binary else b"secret",
                    will_flags=will_flags,
                )
            )
            assert await hood.packet() == b"\x20\x02\x00\x00"
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cuid,user,password,reason",
    [
        ("", "", b"secret", "invalid_client_id"),
        ("bad/id", "bad/id", b"secret", "invalid_client_id"),
        ("hood1", "different", b"secret", "username_mismatch"),
        ("hood1", "hood1", b"", "missing_password"),
    ],
)
def test_invalid_connect_identity_or_password_logs_closed_reason_without_enrollment(
    tmp_path, caplog, cuid, user, password, reason
):
    async def scenario():
        gateway, _, _ = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            identity, username = cuid.encode(), user.encode()
            body = (
                b"\x00\x04MQTT\x04\xc2\x00\x1e"
                + len(identity).to_bytes(2, "big")
                + identity
                + len(username).to_bytes(2, "big")
                + username
                + len(password).to_bytes(2, "big")
                + password
            )
            await hood.send(b"\x10" + bytes((len(body),)) + body)
            assert await hood.packet() == b"\x20\x02\x00\x05"
            assert reason in caplog.text
            assert not (tmp_path / "trust" / "trust.json").exists()
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_event_callback_failure_closes_only_peer_without_exception_logging(tmp_path):
    async def scenario():
        gateway, _, _ = setup(tmp_path)
        leaked = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: leaked.append(context)
        )

        async def fail(event):
            if isinstance(event, SessionOpened):
                raise RuntimeError("SECRET_SYNTHETIC_CALLBACK")  # noqa: TRY004

        gateway.on_event = fail
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x00"
            await hood.publish("connect", b"\x01\x00\x01")
            assert await hood.reader.read() == b""
            await asyncio.sleep(0.05)
            assert leaked == []
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_forget_corrupt_trust_has_sanitized_public_error_graph(tmp_path):
    async def scenario():
        gateway, events, _ = setup(tmp_path)
        path = tmp_path / "trust" / "trust.json"
        path.parent.mkdir()
        malformed = b'{"SECRET_SYNTHETIC_TRUST_MARKER":}'
        path.write_bytes(malformed)
        with pytest.raises(GatewayError) as error:
            await gateway.forget("hood1")
        assert path.read_bytes() == malformed
        assert error.value.committed is None
        assert not events
        marker = "SECRET_SYNTHETIC_TRUST_MARKER"
        seen = set()
        stack = [error.value]
        while stack:
            exception = stack.pop()
            if id(exception) in seen:
                continue
            seen.add(id(exception))
            assert marker not in str(exception)
            assert marker not in repr(exception.args)
            assert marker not in repr(vars(exception))
            assert marker not in str(getattr(exception, "doc", ""))
            stack.extend(
                child
                for child in (exception.__cause__, exception.__context__)
                if child is not None
            )
        assert len(seen) == 1

    asyncio.run(scenario())


def test_forget_reports_committed_atomic_write_failure_without_rollback(
    tmp_path, monkeypatch
):
    async def scenario():
        from hoodini import storage

        gateway, events, _ = setup(tmp_path)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x00"
            original_fsync = storage.os.fsync
            calls = 0

            def fail_directory_fsync(fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("SECRET_SYNTHETIC_FSYNC")
                return original_fsync(fd)

            monkeypatch.setattr(storage.os, "fsync", fail_directory_fsync)
            with pytest.raises(GatewayError) as error:
                await gateway.forget("hood1")
            assert error.value.committed is True
            assert error.value.__context__ is None
            assert error.value.__cause__ is None
            assert "SECRET_SYNTHETIC_FSYNC" not in repr(error.value.args)
            assert (
                json.loads((tmp_path / "trust" / "trust.json").read_text())["devices"]
                == {}
            )
            assert await hood.reader.read() == b""
            assert not events or all(isinstance(e, SessionClosed) for e in events)
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_auth_started_during_forget_cannot_reenroll_after_forget(tmp_path, caplog):
    async def scenario():
        import threading

        gateway, _, _ = setup(tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = gateway.trust.forget

        def paused_forget(cuid):
            entered.set()
            release.wait(3)
            original(cuid)

        gateway.trust.forget = paused_forget
        await gateway.start()
        hood = None
        try:
            forgetting = asyncio.create_task(gateway.forget("hood1"))
            assert await asyncio.to_thread(entered.wait, 3)
            hood = await Hood.open(gateway.port)
            connecting = asyncio.create_task(hood.connect())
            await asyncio.sleep(0.02)
            release.set()
            await forgetting
            assert await connecting == b"\x20\x02\x00\x05"
            assert "forget_pending cuid=hood1" in caplog.text
            fresh = await Hood.open(gateway.port)
            try:
                assert await fresh.connect(password=b"new") == b"\x20\x02\x00\x00"
            finally:
                await fresh.close()
        finally:
            release.set()
            if hood:
                await hood.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_partial_packet_and_outbound_traffic_never_extend_idle_deadline(tmp_path):
    async def scenario():
        clock = Clock()
        gateway, events, _ = setup(tmp_path, clock)
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect(keepalive=30) == b"\x20\x02\x00\x00"
            await hood.publish("connect", b"\x01\x00\x01")
            await hood.packet()
            await hood.packet()
            await hood.subscribe()
            sid = next(e.session_id for e in events if isinstance(e, SessionOpened))
            await clock.advance(44)
            assert await gateway.send_command("hood1", sid, 2, {96: 5})
            await hood.packet()
            await hood.send(b"\xc0")  # incomplete PINGREQ has no receipt timestamp
            await clock.advance(1)
            assert await hood.reader.read() == b""
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_identity_mismatch_denied_without_enrollment_and_unicode_password_stable(
    tmp_path,
):
    async def scenario():
        gateway, _, _ = setup(tmp_path)
        await gateway.start()
        wrong = await Hood.open(gateway.port)
        try:
            assert (
                await wrong.connect(password=b"first", username="other")
                == b"\x20\x02\x00\x05"
            )
            assert await wrong.reader.read() == b""
            first = await Hood.open(gateway.port)
            try:
                assert (
                    await first.connect(password=b"caf\xc3\xa9") == b"\x20\x02\x00\x00"
                )
            finally:
                await first.close()
            repeat = await Hood.open(gateway.port)
            try:
                assert (
                    await repeat.connect(password=b"caf\xc3\xa9") == b"\x20\x02\x00\x00"
                )
            finally:
                await repeat.close()
        finally:
            await wrong.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_corrupt_trust_connect_logs_trust_error_and_fails_closed(tmp_path, caplog):
    async def scenario():
        gateway, _, _ = setup(tmp_path)
        trust_path = tmp_path / "trust" / "trust.json"
        trust_path.parent.mkdir()
        trust_path.write_text('{"synthetic-secret-marker":}')
        await gateway.start()
        hood = await Hood.open(gateway.port)
        try:
            assert await hood.connect() == b"\x20\x02\x00\x05"
            assert "trust_error cuid=hood1" in caplog.text
            assert "synthetic-secret-marker" not in caplog.text
            assert trust_path.read_text() == '{"synthetic-secret-marker":}'
        finally:
            await hood.close()
            await gateway.stop()

    asyncio.run(scenario())


def test_wrong_password_and_fresh_enrollment(tmp_path, caplog):
    async def scenario():
        gateway, _, _ = setup(tmp_path)
        await gateway.start()
        a = await Hood.open(gateway.port)
        b = None
        try:
            assert await a.connect(password=b"first") == b"\x20\x02\x00\x00"
            await a.close()
            b = await Hood.open(gateway.port)
            assert await b.connect(password=b"second") == b"\x20\x02\x00\x05"
            assert "trust_mismatch cuid=hood1" in caplog.text
            assert await b.reader.read() == b""
            await gateway.forget("hood1")
            c = await Hood.open(gateway.port)
            try:
                assert await c.connect(password=b"second") == b"\x20\x02\x00\x00"
            finally:
                await c.close()
        finally:
            await a.close()
            if b:
                await b.close()
            await gateway.stop()

    asyncio.run(scenario())
