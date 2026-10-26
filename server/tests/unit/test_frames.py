"""Literal application payload vectors; none is an observed response to another vector."""

import pytest
from hoodini.protocol.frames import (
    Ack,
    Connect,
    FrameError,
    Status,
    checksum,
    decode_ack,
    decode_connect,
    decode_status,
    encode_time,
)


def test_time_big_endian_checksum() -> None:
    assert encode_time(0x01020304) == bytes.fromhex("010203040a")
    assert checksum(bytes.fromhex("01020304")) == 10


@pytest.mark.parametrize("epoch", [-1, 0x100000000])
def test_time_rejects_out_of_range_epoch(epoch: int) -> None:
    with pytest.raises(ValueError):
        encode_time(epoch)


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("01020305b4753000601e0000e2", Status(0x01020305, -76, {96: 30})),
        ("01020306b47530006e03004001000017", Status(0x01020306, -76, {110: 3, 64: 1})),
        (
            "01020304b47530000500000600000b03002d00003000003500004001004400004500004700004801006000006100006300006e000080640081000083010000e3",
            Status(
                0x01020304,
                -76,
                {
                    5: 0,
                    6: 0,
                    11: 3,
                    45: 0,
                    48: 0,
                    53: 0,
                    64: 1,
                    68: 0,
                    69: 0,
                    71: 0,
                    72: 1,
                    96: 0,
                    97: 0,
                    99: 0,
                    110: 0,
                    128: 100,
                    129: 0,
                    131: 1,
                },
            ),
        ),
        ("01020304b47530000063", Status(0x01020304, -76, {})),
    ],
)
def test_decode_status_fixed_offsets(wire: str, expected: Status) -> None:
    assert decode_status(bytes.fromhex(wire)) == expected


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("002a8d18010000d0", Ack(24, 1)),
        ("002a8d18000000cf", Ack(24, 0)),
    ],
)
def test_decode_ack_result_and_sequence(wire: str, expected: Ack) -> None:
    assert decode_ack(bytes.fromhex(wire)) == expected


def test_decode_connect_big_endian_firmware() -> None:
    assert decode_connect(bytes.fromhex("010303001d01000c02000c3f")) == Connect(
        {3: 29, 1: 12, 2: 12}
    )


@pytest.mark.parametrize(
    ("decoder", "wire"),
    [
        (decode_status, "01020305b4753000601e0000e3"),  # checksum
        (decode_status, "01020305b4753000601e00e2"),  # missing terminator
        (decode_status, "01020305b4753000601e0000"),  # truncated checksum
        (decode_status, "01020305b4753000601e000000e2"),  # ambiguous entry length
        (decode_status, "01020305b4753000601e0001e3"),  # wrong terminator, valid sum
        (decode_status, "000075300001020305b4753000601e0000"),  # no preamble search
        (decode_ack, "002a8d18010000d1"),
        (decode_ack, "002a8d18010000"),
        (decode_ack, "002a8e18010000d1"),  # unexpected header, valid sum
        (decode_ack, "002a8d18010100d1"),  # unexpected reserved byte, valid sum
        (decode_connect, "010303001d01000c02000c40"),
        (decode_connect, "010303001d01000c02000c"),
        (decode_connect, "020303001d01000c02000c40"),  # unexpected version
        (decode_status, "01020305b4753000601e006002000044"),  # duplicate cap
        (decode_connect, "010203001d03000c32"),  # duplicate module
    ],
)
def test_reject_malformed_frames(decoder, wire: str) -> None:
    with pytest.raises(FrameError):
        decoder(bytes.fromhex(wire))


def test_status_rejects_unexpected_fixed_header() -> None:
    wire = bytes.fromhex("01020305b4753100601e0000e3")
    assert len(wire) == 10 + 3  # One entry, plus terminator and checksum.
    assert wire[5:8] == bytes.fromhex("753100")
    assert wire[-1] == sum(wire[:-1]) & 0xFF
    with pytest.raises(FrameError, match="status layout"):
        decode_status(wire)


def test_connect_rejects_count_length_mismatch() -> None:
    wire = bytes.fromhex("010203001d01000c02000c3e")
    assert wire[0] == 1  # Application connect, not MQTT CONNECT.
    assert wire[1] == 2 and len(wire) == 3 + 3 * 3
    assert wire[-1] == sum(wire[:-1]) & 0xFF
    with pytest.raises(FrameError, match="connect layout"):
        decode_connect(wire)
