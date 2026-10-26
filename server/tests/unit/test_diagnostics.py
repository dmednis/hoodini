"""Synthetic diagnostic safety and bounded history tests."""

import logging

import pytest
from hoodini import diagnostics
from hoodini.protocol.ble import NetworkInput, build_frame, encode_network

VALID_CONNECT = (
    b"\x10\x34\x00\x04MQTT\x04\xc2\x00\x1e"
    b"\x00\x0dTESTCUID00001\x00\x0dTESTCUID00001\x00\x0aMARKER_PSK"
)


def test_valid_connect_literal_has_consistent_envelope_and_flags():
    assert len(VALID_CONNECT) == 54
    assert VALID_CONNECT[1] == 52
    assert VALID_CONNECT[9] == 0xC2
    assert VALID_CONNECT[12:14] == b"\x00\x0d"
    assert VALID_CONNECT[27:29] == b"\x00\x0d"
    assert VALID_CONNECT[42:44] == b"\x00\x0a"
    assert VALID_CONNECT[44:] == b"MARKER_PSK"


@pytest.mark.parametrize(
    "raw",
    [VALID_CONNECT, b"\x10\x04\x00\x04MARKER_PSK", b"\x10", b"\x1fMARKER_PSK", b""],
    ids=["valid", "incomplete", "short", "malformed", "empty"],
)
def test_mqtt_connect_never_exposes_bytes_with_wrong_caller_type(raw):
    packet_type = 1 if not raw else 3
    result = diagnostics.mqtt_record(
        "in", packet_type, raw, client_id="device", keepalive=30
    )
    assert result == {
        "kind": "mqtt",
        "direction": "in",
        "packet_type": 1,
        "length": len(raw),
    }
    assert "MARKER_PSK" not in repr(result)
    assert b"MARKER_PSK".hex() not in repr(result)


def test_outbound_network_par_never_exposes_credentials_even_with_wrong_command():
    payload = encode_network(
        NetworkInput(b"lab", "WPA2", "MARKER_PSK", "192.0.2.4", "MARKER_AUTH")
    )
    frame = build_frame(6, payload)
    for raw, command in [
        (frame, 2),
        (b"\x55\x53\x48\x06\xff" + payload, 2),
        (payload, 6),
    ]:
        result = diagnostics.ble_record("out", command, raw)
        assert result == {
            "kind": "ble",
            "direction": "out",
            "command": 6,
            "length": len(raw),
        }
        for marker in ("MARKER_PSK", "MARKER_AUTH"):
            assert marker not in repr(result)
            assert marker.encode().hex() not in repr(result)


def test_safe_packets_are_diagnosable_and_directions_canonical():
    assert diagnostics.mqtt_record("out", 12, b"\xc0\x00") == {
        "kind": "mqtt",
        "direction": "out",
        "packet_type": 12,
        "length": 2,
        "hex": "c000",
    }
    assert diagnostics.ble_record("in", 2, b"\x55\x53\x48\x42") == {
        "kind": "ble",
        "direction": "in",
        "command": 2,
        "length": 4,
        "hex": "55534842",
    }
    with pytest.raises(ValueError):
        diagnostics.mqtt_record("incoming", 12, b"\xc0\x00")


def test_sink_only_keeps_sanitized_history_and_detaches_it(caplog):
    sink = diagnostics.DiagnosticSink()
    for raw in (VALID_CONNECT, b"\x10MARKER_PSK", b"\x1fMARKER_PSK", b""):
        record = diagnostics.mqtt_record("in", 1 if not raw else 3, raw)
        sink.record(record)
        record["length"] = 12345
    payload = encode_network(
        NetworkInput(b"lab", "WPA2", "MARKER_PSK", "192.0.2.4", "MARKER_AUTH")
    )
    sink.record(diagnostics.ble_record("out", 2, build_frame(6, payload)))
    sink.record(diagnostics.ble_record("out", 2, b"USH\x06\xff" + payload))
    sink.record(diagnostics.ble_record("out", 6, payload))
    snapshot = sink.history
    snapshot[0]["length"] = 12345
    assert sink.history[0]["length"] == 54
    for output in (repr(sink.history), caplog.text):
        for marker in ("MARKER_PSK", "MARKER_AUTH"):
            assert marker not in output
            assert marker.encode().hex() not in output


def test_sink_rejects_raw_bypass_without_leaking_in_exception_or_logs(caplog):
    sink = diagnostics.DiagnosticSink()
    cases = [
        {
            "kind": "mqtt",
            "direction": "in",
            "packet_type": 3,
            "length": 11,
            "hex": (b"\x10MARKER_PSK").hex(),
        },
        {
            "kind": "ble",
            "direction": "out",
            "command": 2,
            "length": 10,
            "hex": b"MARKER_PSK".hex(),
        },
        {
            "kind": "mqtt",
            "direction": "in",
            "packet_type": 1,
            "length": 10,
            "raw": "MARKER_PSK",
        },
        {
            "kind": "mqtt",
            "direction": "in",
            "packet_type": 3,
            "length": 2,
            "hex": "c000",
            "extra": "MARKER_PSK",
        },
    ]
    for case in cases:
        with pytest.raises(ValueError) as exc:
            sink.record(case)
        for output in (
            repr(exc.value),
            repr(exc.value.__context__),
            repr(exc.value.__cause__),
        ):
            assert "MARKER_PSK" not in output
            assert b"MARKER_PSK".hex() not in output
    assert sink.history == []
    assert "MARKER_PSK" not in caplog.text


def test_sink_rejects_equality_spoofed_kind_without_exposing_marker(caplog):
    class SpoofedKind(str):
        def __eq__(self, other):
            return other == "mqtt"

        __hash__ = str.__hash__

    sink = diagnostics.DiagnosticSink()
    unsafe = {
        "kind": SpoofedKind("SYNTHETIC_MARKER"),
        "direction": "out",
        "packet_type": 12,
        "length": 2,
        "hex": "c000",
    }
    with pytest.raises(ValueError) as exc:
        sink.record(unsafe)
    assert sink.history == []
    for output in (
        repr(exc.value),
        repr(exc.value.__context__),
        repr(exc.value.__cause__),
        caplog.text,
    ):
        assert "SYNTHETIC_MARKER" not in output
        assert b"SYNTHETIC_MARKER".hex() not in output


def test_sink_rejects_unsupported_direction_and_key_before_comparison():
    class SpoofedDirection(str):
        def __eq__(self, other):
            return other == "out"

    class SpoofedKey(str):
        pass

    sink = diagnostics.DiagnosticSink()
    base = diagnostics.mqtt_record("out", 12, b"\xc0\x00")
    for unsafe in (
        {**base, "direction": SpoofedDirection("SYNTHETIC_MARKER")},
        {**base, SpoofedKey("SYNTHETIC_MARKER"): "extra"},
    ):
        with pytest.raises(ValueError) as exc:
            sink.record(unsafe)
        for output in (
            repr(exc.value),
            repr(exc.value.__context__),
            repr(exc.value.__cause__),
        ):
            assert "SYNTHETIC_MARKER" not in output
    assert sink.history == []


def test_history_is_bounded(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_HISTORY", 2)
    sink = diagnostics.DiagnosticSink()
    for index in range(3):
        sink.record(diagnostics.mqtt_record("out", 12, bytes((0xC0, index))))
    assert [item["hex"] for item in sink.history] == ["c001", "c002"]


def test_invalid_builder_inputs_never_echo_secret():
    sink = diagnostics.DiagnosticSink()
    for builder in (
        lambda: diagnostics.mqtt_record("out", 3, "MARKER_PSK"),
        lambda: diagnostics.ble_record("out", 6, "MARKER_PSK"),
    ):
        with pytest.raises((TypeError, ValueError)) as exc:
            sink.record(builder())
        for output in (
            repr(exc.value),
            repr(exc.value.__context__),
            repr(exc.value.__cause__),
        ):
            assert "MARKER_PSK" not in output
            assert b"MARKER_PSK".hex() not in output


def test_sink_emits_no_secret_to_logger(caplog):
    with caplog.at_level(logging.DEBUG):
        sink = diagnostics.DiagnosticSink()
        sink.record(diagnostics.mqtt_record("in", 3, b"\x10MARKER_PSK"))
    assert "MARKER_PSK" not in caplog.text


def test_sink_owns_timestamp_after_validation():
    sink = diagnostics.DiagnosticSink(clock=lambda: 123.5)
    raw = diagnostics.mqtt_record("out", 12, b"\xc0\x00")
    sink.record(raw)
    assert "timestamp" not in raw
    assert sink.history[0]["timestamp"] == 123.5
