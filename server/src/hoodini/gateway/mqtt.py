"""Pure bounded MQTT 3.1.1 grammar; no transport/session behavior."""

from dataclasses import dataclass, field

MAX_PACKET_BYTES = 65536


class MQTTError(ValueError):
    """Invalid or unsupported packet; messages never contain wire values."""


@dataclass(frozen=True, repr=False)
class ConnectPacket:
    client_id: str
    keepalive: int
    clean_session: bool
    username: str | None
    password: bytes | None
    will_topic: str | None
    will_payload: bytes | None
    will_qos: int
    will_retain: bool


@dataclass(frozen=True)
class SubscribePacket:
    packet_id: int
    topics: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class PublishPacket:
    topic: str
    payload: bytes = field(repr=False)
    qos: int
    packet_id: int | None
    retain: bool
    dup: bool


@dataclass(frozen=True)
class PubackPacket:
    packet_id: int


@dataclass(frozen=True)
class PingreqPacket:
    pass


@dataclass(frozen=True)
class PingrespPacket:
    pass


@dataclass(frozen=True)
class DisconnectPacket:
    pass


Packet = (
    ConnectPacket
    | SubscribePacket
    | PublishPacket
    | PubackPacket
    | PingreqPacket
    | PingrespPacket
    | DisconnectPacket
)


def _text(data: bytes) -> str:
    # UnicodeDecodeError embeds source bytes; never chain it into a public error.
    try:
        value = data.decode("utf-8")
    except UnicodeError:
        value = None
    if value is None or "\x00" in value:
        raise MQTTError("invalid MQTT UTF-8")
    return value


class _Fields:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        if self.pos + size > len(self.data):
            raise MQTTError("truncated MQTT field")
        value = self.data[self.pos : self.pos + size]
        self.pos += size
        return value

    def number(self) -> int:
        return int.from_bytes(self.take(2), "big")

    def binary(self) -> bytes:
        return self.take(self.number())

    def text(self) -> str:
        return _text(self.binary())

    def done(self) -> None:
        if self.pos != len(self.data):
            raise MQTTError("trailing MQTT data")


def _topic(name: str, *, filter_topic: bool = False) -> str:
    if not name or (not filter_topic and ("#" in name or "+" in name)):
        raise MQTTError("invalid MQTT topic")
    if filter_topic:
        parts = name.split("/")
        if any(
            ("#" in part and (part != "#" or i != len(parts) - 1))
            or ("+" in part and part != "+")
            for i, part in enumerate(parts)
        ):
            raise MQTTError("invalid MQTT topic filter")
    return name


def _connect(data: bytes) -> ConnectPacket:
    fields = _Fields(data)
    if fields.binary() != b"MQTT" or fields.take(1) != b"\x04":
        raise MQTTError("unsupported MQTT protocol")
    flags = fields.take(1)[0]
    if flags & 1 or (flags & 0x40 and not flags & 0x80):
        raise MQTTError("invalid CONNECT flags")
    will = bool(flags & 4)
    will_qos = (flags >> 3) & 3
    will_retain = bool(flags & 0x20)
    if (not will and (will_qos or will_retain)) or (will and will_qos == 3):
        raise MQTTError("invalid CONNECT will flags")
    keepalive = fields.number()
    client_id = fields.text()
    if not client_id and not flags & 2:
        raise MQTTError("empty persistent client ID")
    will_topic = _topic(fields.text()) if will else None
    will_payload = fields.binary() if will else None
    username = fields.text() if flags & 0x80 else None
    password = fields.binary() if flags & 0x40 else None
    fields.done()
    return ConnectPacket(
        client_id,
        keepalive,
        bool(flags & 2),
        username,
        password,
        will_topic,
        will_payload,
        will_qos,
        will_retain,
    )


def _parse(packet_type: int, flags: int, data: bytes) -> Packet:
    if packet_type == 1:
        if flags:
            raise MQTTError("invalid CONNECT header")
        return _connect(data)
    fields = _Fields(data)
    if packet_type == 8 and flags == 2:
        packet_id = fields.number()
        if not packet_id:
            raise MQTTError("zero SUBSCRIBE packet ID")
        topics = []
        while fields.pos < len(data):
            topic = _topic(fields.text(), filter_topic=True)
            qos = fields.take(1)[0]
            if qos > 2:
                raise MQTTError("invalid subscription QoS")
            topics.append((topic, qos))
        if not topics:
            raise MQTTError("empty SUBSCRIBE")
        return SubscribePacket(packet_id, tuple(topics))
    if packet_type == 3:
        qos = (flags >> 1) & 3
        dup = bool(flags & 8)
        if qos == 3 or (qos == 0 and dup):
            raise MQTTError("invalid PUBLISH flags")
        topic = _topic(fields.text())
        packet_id = fields.number() if qos else None
        if qos and not packet_id:
            raise MQTTError("zero PUBLISH packet ID")
        return PublishPacket(
            topic,
            fields.take(len(data) - fields.pos),
            qos,
            packet_id,
            bool(flags & 1),
            dup,
        )
    if packet_type == 4 and flags == 0:
        packet_id = fields.number()
        fields.done()
        if not packet_id:
            raise MQTTError("zero PUBACK packet ID")
        return PubackPacket(packet_id)
    if flags == 0 and not data:
        if packet_type == 12:
            return PingreqPacket()
        if packet_type == 13:
            return PingrespPacket()
        if packet_type == 14:
            return DisconnectPacket()
    raise MQTTError("unsupported MQTT packet or flags")


def _header(data: bytes) -> tuple[int, int] | None:
    if not data:
        return None
    length = 0
    multiplier = 1
    for index in range(1, min(len(data), 5)):
        digit = data[index]
        length += (digit & 127) * multiplier
        if not digit & 128:
            if index > 1 and digit == 0:
                raise MQTTError("noncanonical Remaining Length")
            total = index + 1 + length
            if total > MAX_PACKET_BYTES:
                raise MQTTError("MQTT packet exceeds wire limit")
            return index + 1, total
        multiplier *= 128
    if len(data) >= 5:
        raise MQTTError("invalid Remaining Length")
    return None


def frame_sizes(data: bytes) -> tuple[int, int] | None:
    """Return header and total wire sizes; validate framing, not flags or fields."""
    if type(data) is not bytes:
        raise MQTTError("MQTT frame must be bytes")
    return _header(data)


def parse_packet(raw: bytes) -> Packet:
    """Parse exactly one complete packet; reject trailing bytes and oversized declarations."""
    if type(raw) is not bytes:
        raise MQTTError("MQTT packet must be bytes")
    header = _header(raw)
    if header is None or len(raw) != header[1]:
        raise MQTTError("incomplete MQTT packet")
    return _parse(raw[0] >> 4, raw[0] & 15, raw[header[0] :])


class PacketReader:
    """Feed chunks; return complete packets, retaining only a partial packet.

    Discard reader on error: no resynchronization. No socket I/O or logging.
    """

    def __init__(self) -> None:
        self._buffer = b""

    def __repr__(self) -> str:
        return "PacketReader()"

    def feed(self, chunk: bytes) -> list[Packet]:
        if type(chunk) is not bytes:
            raise MQTTError("MQTT chunk must be bytes")
        pending = self._buffer + chunk
        result = []
        while pending:
            header = _header(pending)
            if header is None or len(pending) < header[1]:
                break
            result.append(parse_packet(pending[: header[1]]))
            pending = pending[header[1] :]
        self._buffer = pending
        return result


def _id(value: int) -> bytes:
    if type(value) is not int or not 1 <= value <= 65535:
        raise MQTTError("invalid MQTT packet ID")
    return value.to_bytes(2, "big")


def _length(value: int) -> bytes:
    result = bytearray()
    while True:
        digit = value % 128
        value //= 128
        result.append(digit | (128 if value else 0))
        if not value:
            return bytes(result)


def _wire(first: int, body: bytes) -> bytes:
    result = bytes((first,)) + _length(len(body)) + body
    if len(result) > MAX_PACKET_BYTES:
        raise MQTTError("MQTT packet exceeds wire limit")
    return result


def _encoded_text(value: str) -> bytes:
    if type(value) is not str or "\x00" in value:
        raise MQTTError("invalid MQTT text")
    try:
        data = value.encode("utf-8")
    except UnicodeError:
        data = None
    if data is None or len(data) > 65535:
        raise MQTTError("invalid MQTT text")
    return len(data).to_bytes(2, "big") + data


def encode_connack(return_code: int = 0, *, session_present: bool = False) -> bytes:
    if (
        type(return_code) is not int
        or return_code not in range(6)
        or type(session_present) is not bool
        or (return_code and session_present)
    ):
        raise MQTTError("invalid CONNACK fields")
    return b"\x20\x02" + bytes((int(session_present), return_code))


def encode_suback(packet_id: int, qos: tuple[int, ...]) -> bytes:
    if (
        type(qos) is not tuple
        or not qos
        or any(type(q) is not int or q not in (0, 1, 2, 0x80) for q in qos)
    ):
        raise MQTTError("invalid SUBACK QoS")
    return _wire(0x90, _id(packet_id) + bytes(qos))


def encode_puback(packet_id: int) -> bytes:
    return b"\x40\x02" + _id(packet_id)


def encode_pingresp() -> bytes:
    return b"\xd0\x00"


def encode_publish(
    topic: str,
    payload: bytes,
    *,
    qos: int = 0,
    packet_id: int | None = None,
    retain: bool = False,
) -> bytes:
    if type(qos) is not int or qos not in (0, 1) or type(retain) is not bool:
        raise MQTTError("unsupported PUBLISH QoS or flags")
    if (qos == 0 and packet_id is not None) or (qos == 1 and packet_id is None):
        raise MQTTError("invalid PUBLISH packet ID")
    if type(payload) is not bytes:
        raise MQTTError("invalid PUBLISH payload")
    name = _topic(topic) if type(topic) is str else _topic("")
    body = _encoded_text(name) + (_id(packet_id) if qos else b"") + payload
    return _wire(0x30 | (qos << 1) | int(retain), body)
