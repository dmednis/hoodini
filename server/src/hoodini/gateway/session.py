"""One authenticated TLS peer's bounded transport, deadlines and write fence."""

import asyncio
from dataclasses import dataclass, field

from hoodini.gateway.mqtt import MQTTError, frame_sizes


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one exact bounded MQTT frame; never expose incomplete bytes on errors."""
    header = bytearray(await reader.readexactly(1))
    for _ in range(4):
        if frame_sizes(bytes(header)) is not None:
            break
        header.extend(await reader.readexactly(1))
    sizes = frame_sizes(bytes(header))
    if sizes is None:
        raise MQTTError("invalid MQTT frame")
    return bytes(header) + await reader.readexactly(sizes[1] - sizes[0])


@dataclass(eq=False, repr=False)
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    started: float
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cuid: str | None = None
    session_id: str | None = None
    keepalive: int = 0
    last_received: float = 0
    opened: bool = False
    subscribed: bool = False
    ready: bool = False
    retired: bool = False
    seen: dict[int, bytes] = field(default_factory=dict)
    deadline_handle: asyncio.TimerHandle | None = None
    closed: bool = False

    def close(self) -> None:
        self.retired = True
        self.writer.close()

    def cancel_timer(self) -> None:
        if self.deadline_handle is not None:
            self.deadline_handle.cancel()
            self.deadline_handle = None
