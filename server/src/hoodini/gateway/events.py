"""Data-only gateway event vocabulary; lifecycle belongs to the listener."""

from dataclasses import dataclass

from hoodini.protocol.frames import Ack, Status


@dataclass(frozen=True)
class SessionOpened:
    cuid: str
    session_id: str
    firmware: dict[int, int]


@dataclass(frozen=True)
class CommandReady:
    cuid: str
    session_id: str


@dataclass(frozen=True)
class StatusReceived:
    cuid: str
    session_id: str
    status: Status


@dataclass(frozen=True)
class AckReceived:
    cuid: str
    session_id: str
    ack: Ack


@dataclass(frozen=True)
class SessionClosed:
    cuid: str
    session_id: str
    reason: str


GatewayEvent = (
    SessionOpened | CommandReady | StatusReceived | AckReceived | SessionClosed
)
