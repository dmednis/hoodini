"""Public detached device projection and command result types."""

from dataclasses import dataclass
from typing import Literal

from hoodini.protocol.profiles import Profile


@dataclass(frozen=True)
class DeviceView:
    cuid: str
    info: dict
    profile: Profile
    profile_override: str | None
    firmware: dict[int, int]
    caps: dict[int, int]
    known_ids: set[int]
    rssi: int | None
    last_seen: float | None
    last_light: int
    last_fan: int
    session_state: Literal["none", "handshaking", "ready"]
    online: bool
    connection_id: str | None


@dataclass(frozen=True)
class CommandResult:
    status: Literal["ok", "rejected", "timeout", "offline"]
    seq: int | None = None
    ack_result: int | None = None


@dataclass(frozen=True)
class DeviceEvent:
    kind: Literal["added", "state", "availability", "metadata", "forgotten"]
    cuid: str
    view: DeviceView | None
