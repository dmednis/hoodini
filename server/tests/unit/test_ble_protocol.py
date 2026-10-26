"""Independent synthetic BLE vectors; reply CRC and SET_LEN encoding are unknown."""

import json
from pathlib import Path

import pytest
from hoodini.protocol.ble import (
    BleReassembler,
    BleReply,
    NetworkInput,
    ProvisioningError,
    build_frame,
    crc16,
    encode_network,
)


def test_crc_known_vector() -> None:
    assert crc16(b"123456789") == 0x29B1


def test_get_info_literal_frame() -> None:
    assert build_frame(2) == bytes.fromhex("5553480200b849")


def test_network_par_literal_frame() -> None:
    assert build_frame(6, b"{}") == bytes.fromhex("55534806027b7d9141")


def test_ble_payload_limit() -> None:
    assert len(build_frame(6, b"x" * 245)) == 252
    with pytest.raises(ProvisioningError):
        build_frame(6, b"x" * 246)


@pytest.mark.parametrize("command", [-1, 256, 0x40, 0x80, 0x20])
def test_outbound_command_rejects_invalid_types(command: int) -> None:
    with pytest.raises(ProvisioningError):
        build_frame(command)


def test_network_par_exact_utf8_bytes_and_compact_fields() -> None:
    value = NetworkInput(
        "Café".encode(), "WPA/WPA2 MIXED", "safe-pass", "192.0.2.4", "key"
    )
    payload = encode_network(value)
    assert payload == (
        b'{"SSID":"Caf\xc3\xa9","AUTH":"WPA/WPA2 MIXED","PWD":"safe-pass",'
        b'"ServerIP":"192.0.2.4","ServerURL":"192.0.2.4",'
        b'"ServerPort":"1883","AUTH_PWD":"key"}'
    )
    assert json.loads(payload)["SSID"].encode() == value.ssid


def test_synthetic_fixture_vectors_are_independent_literal_bytes() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parents[3] / "protocol-fixtures/ble.json").read_text()
    )
    get_info, network = fixture["examples"]
    assert (
        build_frame(get_info["command"], get_info["payload_utf8"].encode()).hex()
        == get_info["frame_hex"]
    )
    value = NetworkInput(
        network["ssid_utf8"].encode(),
        network["auth"],
        network["psk"],
        network["server_ip"],
        network["auth_pwd"],
    )
    assert encode_network(value) == network["payload_utf8"].encode()
    assert (
        build_frame(network["command"], network["payload_utf8"].encode()).hex()
        == network["frame_hex"]
    )


def test_network_par_ssid_escaped_characters_preserve_bytes() -> None:
    raw = 'é"\\'.encode()
    payload = encode_network(NetworkInput(raw, "WPA2", "safe", "192.0.2.4", "key"))
    assert json.loads(payload)["SSID"].encode() == raw
    assert b'\\"' in payload and b"\\\\" in payload


def test_network_par_32_byte_ssid_63_character_password() -> None:
    value = NetworkInput(b"s" * 32, "WPA2", "p" * 63, "192.0.2.4", "key")
    assert len(encode_network(value)) <= 245


def test_network_par_final_escaped_size_rejected_without_secret_leak() -> None:
    secret = "SECRET_PSK_MARKER"
    value = NetworkInput(b"s" * 32, "WPA2", secret + '"' * 63, "192.0.2.4", "key")
    with pytest.raises(ProvisioningError) as exc:
        encode_network(value)
    assert secret not in repr(exc.value)
    assert exc.value.__cause__ is None


def test_network_par_invalid_ssid_is_safe() -> None:
    value = NetworkInput(b"SECRET_SSID_MARKER\xff", "WPA2", "pass", "192.0.2.4", "key")
    with pytest.raises(ProvisioningError) as exc:
        encode_network(value)
    assert "SECRET_SSID_MARKER" not in repr(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_network_par_invalid_unicode_secret_is_safe() -> None:
    value = NetworkInput(b"safe", "WPA2", "SECRET_PSK_MARKER\ud800", "192.0.2.4", "key")
    with pytest.raises(ProvisioningError) as exc:
        encode_network(value)
    assert "SECRET_PSK_MARKER" not in repr(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_network_par_repr_redacts_secrets() -> None:
    value = NetworkInput(
        b"synthetic", "WPA2", "SECRET_PSK_MARKER", "192.0.2.4", "SECRET_AUTH_MARKER"
    )
    assert "SECRET_PSK_MARKER" not in repr(value)
    assert "SECRET_AUTH_MARKER" not in repr(value)


def test_network_par_rejects_auth_pwd_oversize_without_secret_leak() -> None:
    value = NetworkInput(
        b"synthetic", "WPA2", "pass", "192.0.2.4", "SECRET_AUTH_MARKER" * 20
    )
    with pytest.raises(ProvisioningError) as exc:
        encode_network(value)
    assert "SECRET_AUTH_MARKER" not in repr(exc.value)


def test_reassembly_split_and_coalesced_notifications() -> None:
    # Synthetic reply CRCs deliberately do not follow the outbound rule.
    first = bytes.fromhex("555348c2027b7d0000")
    last = bytes.fromhex("55534842024f4b0000")
    reassembler = BleReassembler()
    assert reassembler.feed(first[:3]) == []
    assert reassembler.feed(first[3:] + last[:5]) == []
    assert reassembler.feed(last[5:]) == [BleReply(2, b"{}OK", "unverified")]
    assert reassembler.feed(last) == [BleReply(2, b"OK", "unverified")]


def test_set_len_is_informational_without_assuming_integer_encoding() -> None:
    reassembler = BleReassembler()
    # Arbitrary opaque bytes: no established length width or endianness.
    assert reassembler.feed(bytes.fromhex("5553482004deadbeef0000")) == []
    assert reassembler.feed(bytes.fromhex("55534842024f4b0000")) == [
        BleReply(2, b"OK", "unverified")
    ]


def test_reassembly_mismatched_fragment_resets() -> None:
    reassembler = BleReassembler()
    assert reassembler.feed(bytes.fromhex("555348c201410000")) == []
    with pytest.raises(ProvisioningError):
        reassembler.feed(bytes.fromhex("5553484401420000"))
    assert reassembler.feed(bytes.fromhex("5553484201430000")) == [
        BleReply(2, b"C", "unverified")
    ]


def test_reassembly_truncated_notification_waits_and_reset_discards() -> None:
    reassembler = BleReassembler()
    assert reassembler.feed(bytes.fromhex("555348420241")) == []
    reassembler.reset()  # new connection must not inherit partial notifications
    assert reassembler.feed(bytes.fromhex("5553484201420000")) == [
        BleReply(2, b"B", "unverified")
    ]


def test_reassembly_resyncs_on_the_start_byte() -> None:
    # The hood's replies do not always start with a strict "55 53 48" header;
    # only the 0x55 start byte is required and anything before it is discarded.
    reassembler = BleReassembler()
    assert reassembler.feed(bytes.fromhex("00ff55534842024f4b0000")) == [
        BleReply(2, b"OK", "unverified")
    ]
    assert reassembler.feed(bytes.fromhex("55485342024f4b0000")) == [
        BleReply(2, b"OK", "unverified")
    ]
    assert reassembler.feed(bytes.fromhex("0102")) == []
    assert reassembler.feed(bytes.fromhex("55534842000000")) == [
        BleReply(2, b"", "unverified")
    ]


def test_reassembly_skips_unrecognised_frame_types() -> None:
    reassembler = BleReassembler()
    assert reassembler.feed(bytes.fromhex("55534806000000")) == []
    assert reassembler.feed(bytes.fromhex("55534849000000")) == []
    assert reassembler.feed(bytes.fromhex("55534842000000")) == [
        BleReply(2, b"", "unverified")
    ]


def test_reassembly_caps_notification_buffer_and_resets() -> None:
    reassembler = BleReassembler()
    with pytest.raises(ProvisioningError):
        reassembler.feed(b"x" * 65537)
    assert reassembler.feed(bytes.fromhex("55534842000000")) == [
        BleReply(2, b"", "unverified")
    ]


def test_reassembly_caps_partial_reply_and_resets() -> None:
    reassembler = BleReassembler()
    fragment = bytes.fromhex("555348c2f5") + b"x" * 245 + b"\x00\x00"
    with pytest.raises(ProvisioningError):
        for _ in range(270):
            reassembler.feed(fragment)
    assert reassembler.feed(bytes.fromhex("55534842000000")) == [
        BleReply(2, b"", "unverified")
    ]
