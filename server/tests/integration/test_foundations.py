"""Shared-directory integration of the persistent security and diagnostic foundations."""

import pytest
from hoodini.certs import ensure_cert
from hoodini.diagnostics import DiagnosticSink, ble_record, mqtt_record
from hoodini.protocol.ble import NetworkInput, build_frame, encode_network
from hoodini.security import TrustStore, ensure_api_token
from hoodini.storage import AtomicStore, StoredDevice


def test_device_and_cleanup_updates_preserve_separate_private_foundations(tmp_path):
    data_dir = tmp_path / "synthetic-data"
    store = AtomicStore(data_dir)
    trust = TrustStore(data_dir)
    assert trust.check_or_enroll("synthetic-hood", "synthetic-pairing-password")
    token = ensure_api_token(data_dir)
    cert_path, key_path = ensure_cert(data_dir)
    sink = DiagnosticSink(clock=lambda: 123.0)
    sink.record(mqtt_record("out", 12, b"\xc0\x00"))

    private_paths = {
        data_dir / "trust.json",
        data_dir / "api_token",
        cert_path,
        key_path,
    }
    assert len(private_paths) == 4
    assert all(path.is_file() for path in private_paths)
    before = {path: path.read_bytes() for path in private_paths}

    def add_device(snapshot):
        snapshot.devices["synthetic-hood"] = StoredDevice(
            cuid="synthetic-hood",
            info={"model": "synthetic"},
            profile_override="test-profile",
            firmware={1: 2},
            caps={96: 50},
            known_ids={96},
            last_seen=123.0,
        )

    store.update(add_device)

    def add_cleanup(snapshot):
        snapshot.discovery_components["synthetic-hood"] = {"fan", "light"}
        snapshot.pending_removals.add("obsolete-synthetic-hood")
        snapshot.devices["synthetic-hood"].caps = {110: 2}

    store.update(add_cleanup)
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert (data_dir / "devices.json").is_file()
    assert data_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in private_paths)
    assert (data_dir / "devices.json").stat().st_mode & 0o777 == 0o600

    reloaded = AtomicStore(data_dir).load()
    device = reloaded.devices["synthetic-hood"]
    assert device.info == {"model": "synthetic"}
    assert device.profile_override == "test-profile"
    assert device.firmware == {1: 2}
    assert device.caps == {110: 2}
    assert device.known_ids == {96, 110}
    assert reloaded.discovery_components == {"synthetic-hood": {"fan", "light"}}
    assert reloaded.pending_removals == {"obsolete-synthetic-hood"}
    assert TrustStore(data_dir).check_or_enroll(
        "synthetic-hood", "synthetic-pairing-password"
    )
    assert not TrustStore(data_dir).check_or_enroll(
        "synthetic-hood", "different-synthetic-password"
    )
    assert ensure_api_token(data_dir) == token
    assert ensure_cert(data_dir) == (cert_path, key_path)
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert sink.history == [
        {
            "kind": "mqtt",
            "direction": "out",
            "packet_type": 12,
            "length": 2,
            "hex": "c000",
            "timestamp": 123.0,
        }
    ]


@pytest.mark.parametrize(
    ("raw", "packet_type"),
    [
        (
            b"\x10\x24\x00\x04MQTT\x04\xc2\x00\x1e\x00\x03lab\x00\x03usr\x00\x0eSECRET_CONNECT",
            3,
        ),
        (b"\x10\x04\x00\x04SECRET_CONNECT", 3),
        (b"\x1fSECRET_CONNECT", 3),
        (b"", 1),
    ],
    ids=["valid-connect", "truncated-connect", "malformed-connect", "empty-connect"],
)
def test_connect_helpers_to_sink_never_leak_synthetic_secrets(
    tmp_path, caplog, raw, packet_type
):
    if raw.startswith(b"\x10\x24"):
        assert len(raw) == 38  # MQTT 3.1.1 CONNECT, remaining length 36
        assert raw[9] == 0xC2  # username, password, clean session
    sink = DiagnosticSink(clock=lambda: 123.0)
    sink.record(mqtt_record("in", packet_type, raw, client_id="SECRET_CONNECT"))
    assert sink.history == [
        {
            "kind": "mqtt",
            "direction": "in",
            "packet_type": 1,
            "length": len(raw),
            "timestamp": 123.0,
        }
    ]
    with pytest.raises(ValueError) as exc:
        sink.record({**sink.history[0], "raw": "SECRET_CONNECT"})
    for output in (repr(sink.history), caplog.text, repr(exc.value)):
        assert "SECRET_CONNECT" not in output
        assert b"SECRET_CONNECT".hex() not in output


@pytest.mark.parametrize("frame_kind", ["valid", "truncated", "malformed"])
def test_network_par_helpers_to_sink_never_leak_synthetic_secrets(
    tmp_path, caplog, frame_kind
):
    payload = encode_network(
        NetworkInput(b"lab", "WPA2", "SECRET_NETWORK", "192.0.2.4", "SECRET_AUTH")
    )
    raw = {
        "valid": build_frame(6, payload),
        "truncated": build_frame(6, payload)[:-1],
        "malformed": b"USH\x06\xff" + payload,
    }[frame_kind]
    assert b"SECRET_NETWORK" in raw
    assert b"SECRET_AUTH" in raw
    sink = DiagnosticSink(clock=lambda: 123.0)
    sink.record(ble_record("out", 2, raw))
    assert sink.history == [
        {
            "kind": "ble",
            "direction": "out",
            "command": 6,
            "length": len(raw),
            "timestamp": 123.0,
        }
    ]
    with pytest.raises(ValueError) as exc:
        sink.record({**sink.history[0], "raw": "SECRET_NETWORK"})
    for output in (repr(sink.history), caplog.text, repr(exc.value)):
        for marker in ("SECRET_NETWORK", "SECRET_AUTH"):
            assert marker not in output
            assert marker.encode("ascii").hex() not in output
