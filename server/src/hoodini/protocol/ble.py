"""Pure bounded BLE provisioning codecs; reply CRC and SET_LEN encoding are unverified."""

import json
from dataclasses import dataclass, field


class ProvisioningError(ValueError):
    """Invalid BLE provisioning input or reply (messages contain no input bodies)."""


@dataclass(frozen=True)
class BleReply:
    command: int
    payload: bytes
    crc_status: str


@dataclass(frozen=True)
class NetworkInput:
    ssid: bytes
    auth: str
    psk: str = field(repr=False)
    server_ip: str
    auth_pwd: str = field(repr=False)


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def build_frame(command: int, payload: bytes = b"") -> bytes:
    if type(command) is not int or command not in (2, 4, 6):
        raise ProvisioningError("unsupported outbound BLE command")
    if not isinstance(payload, bytes):
        raise ProvisioningError("BLE payload must be bytes")
    if len(payload) > 245:
        raise ProvisioningError(f"BLE payload length {len(payload)} exceeds 245 bytes")
    frame = b"\x55\x53\x48" + bytes((command, len(payload))) + payload
    return frame + crc16(frame[1:]).to_bytes(2, "big")


def encode_network(value: NetworkInput) -> bytes:
    if not isinstance(value.ssid, bytes):
        raise ProvisioningError("SSID must be bytes")
    if not all(
        isinstance(item, str)
        for item in (value.auth, value.psk, value.server_ip, value.auth_pwd)
    ):
        raise ProvisioningError("network fields must be strings")
    try:
        ssid = value.ssid.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        ssid = None
    if ssid is None:
        raise ProvisioningError(f"SSID is not UTF-8 (length {len(value.ssid)})")
    data = {
        "SSID": ssid,
        "AUTH": value.auth,
        "PWD": value.psk,
        "ServerIP": value.server_ip,
        "ServerURL": value.server_ip,
        "ServerPort": "1883",
        "AUTH_PWD": value.auth_pwd,
    }
    try:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except UnicodeError:
        payload = None
    if payload is None:
        raise ProvisioningError("network fields contain invalid Unicode")
    if len(payload) > 245:
        raise ProvisioningError(
            f"encoded network payload length {len(payload)} exceeds 245 bytes"
        )
    return payload


class BleReassembler:
    """One instance per connection; call reset when starting a new connection."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._wire = bytearray()
        self._payload = bytearray()
        self._command: int | None = None

    def feed(self, chunk: bytes) -> list[BleReply]:
        if not isinstance(chunk, bytes):
            self.reset()
            raise ProvisioningError("BLE notification must be bytes")
        try:
            if len(self._wire) + len(chunk) > 65536:
                raise ProvisioningError("BLE notification buffer exceeds 65536 bytes")
            self._wire.extend(chunk)
            replies: list[BleReply] = []
            while self._wire:
                # Framing observed on the hood: only the 0x55 start byte is
                # required; bytes before it are skipped and the two ID bytes of
                # hood->app frames are not checked.
                start = self._wire.find(b"\x55")
                if start < 0:
                    self._wire.clear()
                    break
                del self._wire[:start]
                if len(self._wire) < 5:
                    break
                size = self._wire[4] + 7
                if len(self._wire) < size:
                    break
                kind = self._wire[3]
                payload = bytes(self._wire[5 : size - 2])
                del self._wire[:size]
                if kind == 0x20:
                    # The SET_LEN body format is not established: never allocate from it.
                    continue
                command = kind & 0x3F
                if kind & 0x40 == 0 or command not in (2, 4, 6):
                    continue  # not a reply we wait for; the client times out safely
                if self._command is not None and command != self._command:
                    raise ProvisioningError("BLE fragment command mismatch")
                if len(self._payload) + len(payload) > 65536:
                    raise ProvisioningError("BLE reply exceeds 65536 bytes")
                self._payload.extend(payload)
                if kind & 0x80:
                    self._command = command
                else:
                    replies.append(
                        BleReply(command, bytes(self._payload), "unverified")
                    )
                    self._payload.clear()
                    self._command = None
            return replies
        except ProvisioningError:
            self.reset()
            raise
