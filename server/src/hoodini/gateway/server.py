"""A narrow TLS MQTT endpoint for authenticated hood sessions, not a broker."""

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from ssl import SSLContext

from hoodini.diagnostics import DiagnosticSink, mqtt_record
from hoodini.gateway.events import (
    AckReceived,
    CommandReady,
    GatewayEvent,
    SessionClosed,
    SessionOpened,
    StatusReceived,
)
from hoodini.gateway.mqtt import (
    ConnectPacket,
    DisconnectPacket,
    PingreqPacket,
    PublishPacket,
    SubscribePacket,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_publish,
    encode_suback,
    parse_packet,
)
from hoodini.gateway.session import Session, read_frame
from hoodini.protocol.frames import (
    decode_ack,
    decode_connect,
    decode_status,
    encode_command,
    encode_time,
)
from hoodini.security import TrustStore
from hoodini.storage import AtomicWriteError

logger = logging.getLogger("hoodini.gateway")


def _log_cuid(cuid: str) -> str:
    return cuid if cuid.isprintable() else repr(cuid)


SUBSCRIPTIONS = frozenset(
    (
        "connected",
        "time",
        "op/mode",
        "op/update",
        "op/configuration",
        "op/reset",
        "sync",
    )
)


class _LoopClock:
    def now(self) -> float:
        return asyncio.get_running_loop().time()

    def call_at(self, when: float, callback):
        return asyncio.get_running_loop().call_at(when, callback)


class GatewayError(RuntimeError):
    """Sanitized public gateway failure; committed is known only for atomic writes."""

    def __init__(self, *, committed: bool | None = None) -> None:
        self.committed = committed
        super().__init__("cannot forget device trust")


class Gateway:
    """start/stop a TLS endpoint; pass explicit bind/port (production uses 8883).

    clock exposes monotonic now() and call_at(absolute_time, callback) returning
    a cancellable handle; epoch() independently supplies integer wall-clock seconds.
    Errors and close reasons are generic, never exception strings or packet bodies.
    """

    def __init__(
        self,
        bind: str,
        port: int,
        tls: SSLContext,
        trust: TrustStore,
        diagnostics: DiagnosticSink,
        on_event: Callable[[GatewayEvent], Awaitable[None]],
        *,
        clock=None,
        epoch: Callable[[], int] | None = None,
    ) -> None:
        self.bind, self.port, self.tls = bind, port, tls
        self.trust, self.diagnostics, self.on_event = trust, diagnostics, on_event
        self.clock = clock or _LoopClock()
        self.epoch = epoch or (lambda: int(time.time()))
        self._server: asyncio.AbstractServer | None = None
        self._stopping = False
        self._sessions: set[Session] = set()
        self._current: dict[str, Session] = {}
        self._auth_lock = asyncio.Lock()
        self._forget_pending: dict[str, int] = {}
        self._forget_generation: dict[str, int] = {}

    async def start(self) -> None:
        if self._server is not None:
            return
        self._stopping = False
        self._server = await asyncio.start_server(
            self._serve, self.bind, self.port, ssl=self.tls
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._stopping = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for session in tuple(self._sessions):
            session.close()
        if server is not None:
            await server.wait_closed()
        while self._sessions:
            await asyncio.sleep(0)

    def _current_session(self, session: Session) -> bool:
        return not session.retired and self._current.get(session.cuid) is session

    def _expired(self, session: Session) -> bool:
        now = self.clock.now()
        return (not session.ready and now >= session.started + 10) or (
            session.cuid is not None
            and session.keepalive != 0
            and now >= session.last_received + session.keepalive * 1.5
        )

    def _schedule(self, session: Session) -> None:
        session.cancel_timer()
        if session.retired:
            return
        deadlines = []
        if not session.ready:
            deadlines.append(session.started + 10)
        if session.cuid is not None and session.keepalive:
            deadlines.append(session.last_received + 1.5 * session.keepalive)
        if deadlines:
            session.deadline_handle = self.clock.call_at(
                min(deadlines), lambda: self._timeout(session)
            )

    def _timeout(self, session: Session) -> None:
        if session.cuid is not None and not self._current_session(session):
            return
        if self._expired(session):
            session.close()
        else:
            self._schedule(session)

    async def _write(
        self, session: Session, raw: bytes, *, preauth: bool = False
    ) -> bool:
        async with session.write_lock:
            if (
                session.retired
                or (not preauth and not self._current_session(session))
                or self._expired(session)
            ):
                return False
            self.diagnostics.record(mqtt_record("out", raw[0] >> 4, raw))
            session.writer.write(raw)
            await session.writer.drain()
            return True

    async def _emit(self, session: Session, event: GatewayEvent) -> None:
        if self._current_session(session) and not self._expired(session):
            await self.on_event(event)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = Session(reader, writer, self.clock.now())
        self._sessions.add(session)
        if self._stopping:
            session.close()
        self._schedule(session)
        try:
            while not session.retired:
                raw = await read_frame(reader)
                received = self.clock.now()
                self.diagnostics.record(mqtt_record("in", raw[0] >> 4, raw))
                packet = parse_packet(raw)
                if self._expired(session) or session.retired:
                    break
                if session.cuid is None:
                    if not isinstance(
                        packet, ConnectPacket
                    ) or not await self._authenticate(session, packet, received):
                        break
                elif not self._current_session(session):
                    break
                else:
                    session.last_received = received
                    self._schedule(session)
                    if isinstance(packet, DisconnectPacket):
                        break
                    if isinstance(packet, PingreqPacket):
                        await self._write(session, encode_pingresp())
                    elif isinstance(packet, SubscribePacket):
                        await self._subscribe(session, packet)
                    elif isinstance(packet, PublishPacket):
                        if packet.qos == 2:
                            break
                        await self._publish(session, packet)
                    else:
                        break
        except Exception:  # noqa: BLE001, S110 - exception graphs may contain credentials
            pass
        finally:
            session.close()
            session.cancel_timer()
            if self._current.get(session.cuid) is session:
                self._current.pop(session.cuid, None)
            if session.cuid is not None and not session.closed:
                session.closed = True
                logger.info("session_closed cuid=%r reason=disconnected", session.cuid)
                try:
                    await self.on_event(
                        SessionClosed(session.cuid, session.session_id, "disconnected")
                    )
                except Exception:  # noqa: BLE001, S110 - never log callback graphs
                    pass
            self._sessions.discard(session)
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001, S110 - TLS errors may contain peer data
                pass

    def _candidate_expired(
        self, session: Session, received: float, keepalive: int
    ) -> bool:
        return (
            session.retired
            or self._expired(session)
            or (keepalive > 0 and self.clock.now() >= received + 1.5 * keepalive)
        )

    async def _authenticate(
        self, session: Session, packet: ConnectPacket, received: float
    ) -> bool:
        cuid = packet.client_id
        if not cuid or any(char in cuid for char in "/+#"):
            logger.warning("invalid_client_id")
            await self._write(session, encode_connack(5), preauth=True)
            return False
        if packet.username != cuid:
            logger.warning("username_mismatch cuid=%s", _log_cuid(cuid))
            await self._write(session, encode_connack(5), preauth=True)
            return False
        if not packet.password:
            logger.warning("missing_password cuid=%s", _log_cuid(cuid))
            await self._write(session, encode_connack(5), preauth=True)
            return False
        forget_generation = self._forget_generation.get(cuid, 0)
        async with self._auth_lock:
            if self._candidate_expired(session, received, packet.keepalive):
                return False
            if (
                forget_generation != self._forget_generation.get(cuid, 0)
                or cuid in self._forget_pending
            ):
                logger.warning("forget_pending cuid=%s", _log_cuid(cuid))
                await self._write(session, encode_connack(5), preauth=True)
                return False
            try:
                approved = await asyncio.to_thread(
                    self.trust.check_or_enroll, cuid, packet.password
                )
            except Exception:  # noqa: BLE001 - fail closed without logging trust graphs
                approved = False
                trust_error = True
            else:
                trust_error = False
            if self._candidate_expired(session, received, packet.keepalive):
                return False
            if not approved:
                logger.warning(
                    "%s cuid=%s",
                    "trust_error" if trust_error else "trust_mismatch",
                    _log_cuid(cuid),
                )
                await self._write(session, encode_connack(5), preauth=True)
                return False
            old = self._current.get(cuid)
            session.cuid, session.session_id = cuid, secrets.token_hex(16)
            session.keepalive, session.last_received = packet.keepalive, received
            if old is not None:
                old.close()  # revoke old writes before any awaited I/O
            self._current[cuid] = session
            self._schedule(session)
        accepted = await self._write(session, encode_connack())
        if accepted:
            logger.info(
                "accepted cuid=%s keepalive=%s will=%s",
                _log_cuid(cuid),
                packet.keepalive,
                "yes" if packet.will_topic is not None else "no",
            )
        return accepted

    async def _subscribe(self, session: Session, packet: SubscribePacket) -> None:
        prefix = f"v1/device/{session.cuid}/"
        granted = tuple(
            0
            if topic.startswith(prefix) and topic[len(prefix) :] in SUBSCRIPTIONS
            else 128
            for topic, _ in packet.topics
        )
        if not await self._write(session, encode_suback(packet.packet_id, granted)):
            return
        if any(topic == prefix + "op/mode" for topic, _ in packet.topics):
            session.subscribed = True
            await self._ready(session)

    async def _ready(self, session: Session) -> None:
        if (
            self._current_session(session)
            and not self._expired(session)
            and session.opened
            and session.subscribed
            and not session.ready
        ):
            session.ready = True
            self._schedule(session)
            await self._emit(session, CommandReady(session.cuid, session.session_id))

    async def _publish(self, session: Session, packet: PublishPacket) -> None:
        if packet.qos == 1:
            if not await self._write(session, encode_puback(packet.packet_id)):
                return
            signature = hashlib.sha256(
                packet.topic.encode("utf-8") + b"\x00" + packet.payload
            ).digest()
            if packet.dup and session.seen.get(packet.packet_id) == signature:
                return
            session.seen[packet.packet_id] = signature
        prefix = f"v1/device/{session.cuid}/"
        if (
            not packet.topic.startswith(prefix)
            or not self._current_session(session)
            or self._expired(session)
        ):
            return
        relative = packet.topic[len(prefix) :]
        if relative == "connect" and not session.opened:
            connect = decode_connect(packet.payload)
            session.opened = True
            await self._emit(
                session,
                SessionOpened(session.cuid, session.session_id, connect.firmware),
            )
            if await self._write(session, encode_publish(prefix + "connected", b"")):
                await self._time(session)
                await self._ready(session)
        elif relative == "ping":
            await self._time(session)
        elif relative == "status":
            status = decode_status(packet.payload)
            await self._emit(
                session, StatusReceived(session.cuid, session.session_id, status)
            )
        elif relative == "ack":
            ack = decode_ack(packet.payload)
            await self._emit(
                session, AckReceived(session.cuid, session.session_id, ack)
            )

    async def _time(self, session: Session) -> None:
        await self._write(
            session,
            encode_publish(f"v1/device/{session.cuid}/time", encode_time(self.epoch())),
        )

    async def send_command(
        self, cuid: str, session_id: str, seq: int, caps: dict[int, int]
    ) -> bool:
        session = self._current.get(cuid)
        if session is None or session.session_id != session_id or not session.ready:
            return False
        return await self._write(
            session,
            encode_publish(f"v1/device/{cuid}/op/mode", encode_command(seq, caps)),
        )

    async def disconnect(self, cuid: str) -> None:
        session = self._current.get(cuid)
        if session is not None:
            session.close()

    async def forget(self, cuid: str) -> None:
        self._forget_pending[cuid] = self._forget_pending.get(cuid, 0) + 1
        self._forget_generation[cuid] = self._forget_generation.get(cuid, 0) + 1
        failed = False
        committed = None
        try:
            async with self._auth_lock:
                session = self._current.get(cuid)
                if session is not None:
                    session.close()
                    self._current.pop(cuid, None)
                await asyncio.to_thread(self.trust.forget, cuid)
        except AtomicWriteError as error:
            committed = error.committed
            failed = True
        except Exception:  # noqa: BLE001 - discard trust error graphs at public boundary
            failed = True
        finally:
            self._forget_generation[cuid] += 1
            self._forget_pending[cuid] -= 1
            if not self._forget_pending[cuid]:
                self._forget_pending.pop(cuid)
        if failed:
            # Raising after handlers finish prevents retaining trust decoder context.
            raise GatewayError(committed=committed)
