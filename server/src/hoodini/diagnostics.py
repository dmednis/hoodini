"""Sanitized diagnostic records and bounded in-memory history."""

import time
from collections import deque
from collections.abc import Callable

MAX_HISTORY = 1000


def _direction(direction: str) -> None:
    if type(direction) is not str or direction not in ("in", "out"):
        raise ValueError("invalid diagnostic direction")


def mqtt_record(
    direction: str,
    packet_type: int,
    raw: bytes,
    *,
    client_id: str | None = None,
    keepalive: int | None = None,
) -> dict:
    """Classify the first wire byte before honoring a caller's packet type.

    CONNECT metadata is deliberately limited to type/length/direction: caller-provided
    client IDs are not independently verified as safe and are not stored.
    """
    _direction(direction)
    if type(packet_type) is not int or not 0 <= packet_type <= 15:
        raise ValueError("invalid MQTT packet type")
    if not isinstance(raw, bytes):
        raise TypeError("MQTT frame must be bytes")
    wire_type = raw[0] >> 4 if raw else packet_type
    if packet_type == 1 or wire_type == 1:
        return {
            "kind": "mqtt",
            "direction": direction,
            "packet_type": 1,
            "length": len(raw),
        }
    return {
        "kind": "mqtt",
        "direction": direction,
        "packet_type": wire_type,
        "length": len(raw),
        "hex": raw.hex(),
    }


def ble_record(direction: str, command: int, raw: bytes) -> dict:
    """Outbound BLE bodies are never exported, including malformed provisioning frames."""
    _direction(direction)
    if type(command) is not int or not 0 <= command <= 255:
        raise ValueError("invalid BLE command")
    if not isinstance(raw, bytes):
        raise TypeError("BLE frame must be bytes")
    wire_command = (
        (raw[3] & 0x3F if direction == "in" else raw[3])
        if raw.startswith(b"USH") and len(raw) >= 4
        else command
    )
    if direction == "out":
        return {
            "kind": "ble",
            "direction": direction,
            "command": 6 if 6 in (command, wire_command) else wire_command,
            "length": len(raw),
        }
    return {
        "kind": "ble",
        "direction": direction,
        "command": wire_command,
        "length": len(raw),
        "hex": raw.hex(),
    }


def _validate(record: dict) -> dict:
    """Rebuild a whitelist-only copy; never format untrusted values in errors."""
    if type(record) is not dict or any(type(key) is not str for key in record):
        raise ValueError("invalid diagnostic record")
    kind = record.get("kind")
    if type(kind) is not str:
        raise ValueError("invalid diagnostic kind")
    direction = record.get("direction")
    _direction(direction)
    length = record.get("length")
    if type(length) is not int or length < 0:
        raise ValueError("invalid diagnostic record length")
    if kind == "mqtt":
        packet_type = record.get("packet_type")
        if type(packet_type) is not int or not 0 <= packet_type <= 15:
            raise ValueError("invalid diagnostic packet type")
        fields = {"kind", "direction", "packet_type", "length"}
        sensitive = packet_type == 1
        code = packet_type
    elif kind == "ble":
        code = record.get("command")
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError("invalid diagnostic BLE command")
        fields = {"kind", "direction", "command", "length"}
        sensitive = direction == "out"
    else:
        raise ValueError("invalid diagnostic kind")
    expected = fields if sensitive else fields | {"hex"}
    if record.keys() != expected:
        raise ValueError("invalid diagnostic record fields")
    if not sensitive:
        encoded = record["hex"]
        if type(encoded) is not str or len(encoded) != length * 2:
            raise ValueError("invalid diagnostic hex")
        try:
            raw = bytes.fromhex(encoded)
        except ValueError:
            raise ValueError("invalid diagnostic hex") from None
        if raw.hex() != encoded:
            raise ValueError("invalid diagnostic hex")
        if kind == "mqtt" and raw and (raw[0] >> 4 != code or code == 1):
            raise ValueError("inconsistent MQTT type")
        if (
            kind == "ble"
            and raw.startswith(b"USH")
            and len(raw) >= 4
            and raw[3] & 0x3F != code
        ):
            raise ValueError("inconsistent BLE command")
    safe = {"kind": "mqtt" if kind == "mqtt" else "ble", "direction": direction}
    safe["packet_type" if kind == "mqtt" else "command"] = code
    safe["length"] = length
    if not sensitive:
        safe["hex"] = encoded
    return safe


class DiagnosticSink:
    """History of the last MAX_HISTORY sanitized records."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._history: deque[dict] = deque(maxlen=MAX_HISTORY)
        self._clock = clock

    @property
    def history(self) -> list[dict]:
        """Detached oldest-to-newest records, safe for an admin reader to mutate."""
        return [item.copy() for item in self._history]

    def record(self, record: dict) -> None:
        safe = _validate(record)
        safe["timestamp"] = self._clock()
        self._history.append(safe)
