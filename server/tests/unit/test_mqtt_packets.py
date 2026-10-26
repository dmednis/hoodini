"""Literal MQTT 3.1.1 wire vectors; no broker or real hood traffic."""

import pytest
from hoodini.diagnostics import mqtt_record
from hoodini.gateway.events import (
    AckReceived,
    CommandReady,
    SessionClosed,
    SessionOpened,
    StatusReceived,
)
from hoodini.gateway.mqtt import (
    ConnectPacket,
    DisconnectPacket,
    MQTTError,
    PacketReader,
    PingreqPacket,
    PingrespPacket,
    PubackPacket,
    PublishPacket,
    SubscribePacket,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_publish,
    encode_suback,
    frame_sizes,
    parse_packet,
)
from hoodini.protocol.frames import Ack, Status


def test_frame_sizes_bounded_exact_header_without_parsing_packet():
    assert frame_sizes(b"") is None
    assert frame_sizes(b"\x10") is None
    assert frame_sizes(b"\x10\x03") == (2, 5)
    assert frame_sizes(b"\x10\x80\x01") == (3, 131)
    assert frame_sizes(b"\x1f\x00") == (2, 2)  # Flags belong to parse_packet.
    with pytest.raises(MQTTError, match="bytes"):
        frame_sizes(bytearray(b"\x10\x00"))
    for raw in (b"\x10\x80\x00", b"\x10\xff\xff\xff\x7f", b"\x10\x80\x80\x80\x80"):
        with pytest.raises(MQTTError):
            frame_sizes(raw)


CONNECT = (
    b"\x10\x18\x00\x04MQTT\x04\xc2\x00\x3c\x00\x01c\x00\x04user\x00\x03\xff\x00\x80"
)


def test_events_carry_protocol_types_and_opaque_session_identity():
    status = Status(1, -20, {96: 5})
    ack = Ack(4, 0)
    assert SessionOpened("c", "opaque", {1: 2}).firmware == {1: 2}
    assert CommandReady("c", "opaque").session_id == "opaque"
    assert StatusReceived("c", "opaque", status).status is status
    assert AckReceived("c", "opaque", ack).ack is ack
    assert SessionClosed("c", "opaque", "offline").reason == "offline"


def test_connect_binary_password_and_optional_will():
    packet = parse_packet(CONNECT)
    assert packet == ConnectPacket(
        "c", 60, True, "user", b"\xff\x00\x80", None, None, 0, False
    )
    will = parse_packet(
        b"\x10\x13\x00\x04MQTT\x04\x2e\x00\x00\x00\x01c\x00\x01t\x00\x01x"
    )
    assert will == ConnectPacket("c", 0, True, None, None, "t", b"x", 1, True)


def test_subscribe_publish_qos1_and_control_packets():
    assert parse_packet(b"\x82\x08\x00\x01\x00\x03a/b\x01") == SubscribePacket(
        1, (("a/b", 1),)
    )
    assert parse_packet(b"\x3b\x08\x00\x01t\x00\x02abc") == PublishPacket(
        "t", b"abc", 1, 2, True, True
    )
    assert parse_packet(b"\x40\x02\x00\x02") == PubackPacket(2)
    assert parse_packet(b"\xc0\x00") == PingreqPacket()
    assert parse_packet(b"\xd0\x00") == PingrespPacket()
    assert parse_packet(b"\xe0\x00") == DisconnectPacket()
    assert parse_packet(b"\x31\x04\x00\x01tx") == PublishPacket(
        "t", b"x", 0, None, True, False
    )


def test_split_and_coalesced_packets_are_individually_bounded():
    reader = PacketReader()
    assert reader.feed(b"\xc0") == []
    assert reader.feed(b"\x00\x40\x02\x00") == [PingreqPacket()]
    assert reader.feed(b"\x01\xe0\x00") == [PubackPacket(1), DisconnectPacket()]
    assert reader.feed(b"") == []


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\x00",
        b"\x80\x00",
        b"\x82\x00",
        b"\x82\x02\x00\x00",
        b"\x82\x08\x00\x01\x00\x03a/b\x03",
        b"\x32\x04\x00\x01tx",
        b"\x38\x04\x00\x01tx",
        b"\x30\x05\x00\x02\xff\xffx",
        b"\x40\x02\x00\x00",
        b"\xc1\x00",
        b"\xc0\x01\x00",
        b"\xc0\x80\x00",
        b"\xc0\xff\xff\xff\xff\x01",
        b"\x10\x18\x00\x04MQTT\x04\x42\x00\x3c\x00\x01c\x00\x04user\x00\x03\xff\x00\x80",
        b"\x10\x13\x00\x04MQTT\x04\x1e\x00\x00\x00\x01c\x00\x01t\x00\x01x",
        b"\x10\x0d\x00\x04MQTT\x04\x00\x00\x00\x00\x02\xc0\x80",
    ],
)
def test_malformed_packet_rejected_without_wire_in_error(raw):
    with pytest.raises(MQTTError) as captured:
        parse_packet(raw)
    assert raw.hex() not in str(captured.value) if raw else True


def test_connect_sensitive_bytes_absent_from_repr_and_exception_graph():
    marker = b"SECRET_MARKER_73"
    raw = b"\x10\x1d\x00\x04MQTT\x04\xc2\x00\x00\x00\x01c\x00\x01u\x00\x10" + marker
    assert marker in raw
    # Declared lengths and malformed UTF-8 exercise both header and decoder failures.
    for wire in (
        raw,
        b"\x10\x20\x00\x04MQTT\x04\x82\x00\x00\x00\x01c\x00\x11\xff" + marker,
    ):
        assert marker in wire
        with pytest.raises(MQTTError) as captured:
            parse_packet(wire)
        exc = captured.value
        while exc is not None:
            assert marker.decode() not in repr(exc)
            assert marker not in repr(exc.args).encode()
            exc = exc.__cause__ or exc.__context__
    assert marker.decode() not in repr(
        parse_packet(
            b"\x10\x22\x00\x04MQTT\x04\xc2\x00\x00\x00\x01c\x00\x10SECRET_MARKER_73\x00\x01p"
        )
    )


def test_total_wire_limit_rejects_declared_oversize_before_body():
    reader = PacketReader()
    with pytest.raises(MQTTError):
        reader.feed(b"\x30\xfd\xff\x03")  # 65533 body + 4 header > 65536
    with pytest.raises(MQTTError):
        parse_packet(b"\x30\xfd\xff\x03")
    # 65532 body + four header bytes = 65536; a large packet may share a read.
    body = b"\x00\x01t" + b"x" * 65529
    assert len(body) == 65532
    packets = PacketReader().feed(b"\x30\xfc\xff\x03" + body + b"\xc0\x00")
    assert len(packets) == 2
    assert packets[0].payload == b"x" * 65529
    assert packets[1] == PingreqPacket()


def test_connect_clean_session_required_for_empty_client_id():
    with pytest.raises(MQTTError):
        parse_packet(b"\x10\x0c\x00\x04MQTT\x04\x00\x00\x00\x00\x00")
    assert parse_packet(b"\x10\x0c\x00\x04MQTT\x04\x02\x00\x00\x00\x00").client_id == ""


def test_diagnostics_characterization_classifies_malformed_connect_early():
    raw = b"\x1f\x01XSECRET_MARKER_73"
    assert b"SECRET_MARKER_73" in raw
    assert mqtt_record("in", 3, raw) == {
        "kind": "mqtt",
        "direction": "in",
        "packet_type": 1,
        "length": len(raw),
    }


def test_encoder_literal_packets_and_bad_arguments():
    assert encode_connack() == b"\x20\x02\x00\x00"
    assert encode_connack(0, session_present=True) == b"\x20\x02\x01\x00"
    assert encode_suback(7, (0, 1)) == b"\x90\x04\x00\x07\x00\x01"
    assert encode_puback(7) == b"\x40\x02\x00\x07"
    assert encode_pingresp() == b"\xd0\x00"
    assert (
        encode_publish("t", b"hi", qos=1, packet_id=7, retain=True)
        == b"\x33\x07\x00\x01t\x00\x07hi"
    )
    for call in (
        lambda: encode_puback(0),
        lambda: encode_suback(1, (3,)),
        lambda: encode_publish("t", b"x", qos=1),
        lambda: encode_publish("t", b"x", qos=0, packet_id=1),
        lambda: encode_connack(1, session_present=True),
    ):
        with pytest.raises(MQTTError):
            call()
