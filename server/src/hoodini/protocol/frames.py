"""Pure application payload codecs."""

from dataclasses import dataclass


class FrameError(ValueError):
    """Invalid application frame."""


@dataclass(frozen=True)
class Status:
    epoch: int
    rssi: int
    caps: dict[int, int]


@dataclass(frozen=True)
class Ack:
    seq: int
    result: int


@dataclass(frozen=True)
class Connect:
    firmware: dict[int, int]


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _verify_checksum(data: bytes) -> None:
    if not data or checksum(data[:-1]) != data[-1]:
        raise FrameError("invalid frame checksum")


def decode_status(data: bytes) -> Status:
    if len(data) < 10 or (len(data) - 10) % 3 or data[5:8] != b"\x75\x30\x00":
        raise FrameError("invalid status layout")
    if data[-2] != 0:
        raise FrameError("invalid status terminator")
    _verify_checksum(data)
    caps: dict[int, int] = {}
    for offset in range(8, len(data) - 2, 3):
        cap_id = data[offset]
        if cap_id in caps:
            raise FrameError("duplicate status capability")
        caps[cap_id] = int.from_bytes(data[offset + 1 : offset + 3], "little")
    return Status(
        int.from_bytes(data[:4], "big"),
        int.from_bytes(data[4:5], "big", signed=True),
        caps,
    )


def decode_ack(data: bytes) -> Ack:
    if len(data) != 8 or data[:3] != b"\x00\x2a\x8d" or data[5:7] != b"\x00\x00":
        raise FrameError("invalid ack layout")
    _verify_checksum(data)
    return Ack(seq=data[3], result=data[4])


def decode_connect(data: bytes) -> Connect:
    if len(data) < 3 or data[0] != 1 or len(data) != 3 + data[1] * 3:
        raise FrameError("invalid connect layout")
    _verify_checksum(data)
    firmware: dict[int, int] = {}
    for offset in range(2, len(data) - 1, 3):
        module_id = data[offset]
        if module_id in firmware:
            raise FrameError("duplicate connect module")
        firmware[module_id] = int.from_bytes(data[offset + 1 : offset + 3], "big")
    return Connect(firmware)


def encode_command(seq: int, caps: dict[int, int]) -> bytes:
    if type(seq) is not int or not 0 <= seq <= 255:
        raise FrameError("sequence must fit an unsigned byte")
    if not caps:
        raise FrameError("empty command")
    payload = bytearray(b"\x00\x2a\x8d")
    payload.extend((seq, 0, 0))
    payload.extend(b"\x75\x30\x00")
    for cap_id, value in caps.items():
        if type(cap_id) is not int or not 0 <= cap_id <= 255 or cap_id == 71:
            raise FrameError("invalid command capability ID")
        if type(value) is not int or not 0 <= value <= 65535:
            raise FrameError("invalid command capability value")
        payload.append(cap_id)
        payload.extend(value.to_bytes(2, "little"))
    payload.extend(b"\x47\x01\x00\x00")
    return bytes(payload) + bytes((checksum(payload),))


def encode_time(epoch: int) -> bytes:
    if not 0 <= epoch <= 0xFFFFFFFF:
        raise ValueError("epoch must fit an unsigned 32-bit integer")
    payload = epoch.to_bytes(4, "big")
    return payload + bytes((checksum(payload),))
