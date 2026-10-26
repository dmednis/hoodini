"""Synthetic BLE conversation at the physical write boundary."""

import asyncio
import json

from hoodini.pairing.transport import BleTarget

TARGET = BleTarget("synthetic-device", "ELC_TEST_123")


def reply(command: int, value: dict, *, split: int = 0) -> list[bytes]:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    wire = b"\x55\x53\x48" + bytes((command | 0x40, len(data))) + data + b"\x00\x00"
    return [wire[:split], wire[split:]] if split else [wire]


class FakeTransport:
    def __init__(self, replies: list[list[bytes]], *, size: int = 256) -> None:
        self.replies = list(replies)
        self.notifications: list[bytes] = []
        self.writes: list[bytes] = []
        self.size = size
        self.connections = 0
        self.disconnections = 0
        self.fail_connect = 0
        self.lost = False
        self.block = False
        self.setup_ended = False
        self.rediscovered: list[BleTarget] = []
        self.connect_error: Exception = OSError("synthetic failure")

    async def discover(self, timeout: float) -> list[BleTarget]:
        return [TARGET]

    async def connect(self, target: BleTarget) -> None:
        self.connections += 1
        if self.setup_ended:
            raise OSError("setup mode ended")
        if self.connections <= self.fail_connect:
            raise self.connect_error

    async def rediscover(self, target: BleTarget, timeout: float) -> None:
        self.rediscovered.append(target)

    async def write(self, frame: bytes) -> None:
        self.writes.append(frame)
        if self.replies:
            self.notifications.extend(self.replies.pop(0))

    async def read_notification(self) -> bytes:
        if self.lost:
            raise OSError("synthetic loss")
        if self.block:
            await asyncio.Future()
        if self.notifications:
            return self.notifications.pop(0)
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def disconnect(self) -> None:
        self.disconnections += 1
        if self.writes and any(frame[3] == 4 for frame in self.writes):
            self.setup_ended = True

    def max_write_size(self) -> int:
        return self.size


class FakeClock:
    def __init__(self) -> None:
        self.time = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        for _ in range(4):
            await asyncio.sleep(0)
        self.time += seconds
