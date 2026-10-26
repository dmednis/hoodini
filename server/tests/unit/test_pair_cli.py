import argparse
import asyncio

import pytest
from hoodini.pairing import cli
from hoodini.pairing.client import Network, PairResult
from hoodini.pairing.transport import BleTarget


def test_resolve_setup_key_uses_default_then_saved_override(tmp_path, monkeypatch):
    from hoodini.pairing import keys

    monkeypatch.setattr(keys, "DEFAULT_SETUP_KEY", "fictional-key")
    assert keys.resolve_setup_key(tmp_path) == "fictional-key"
    keys.save_setup_key("saved-fictional-key", tmp_path)
    assert keys.resolve_setup_key(tmp_path) == "saved-fictional-key"


def test_resolve_setup_key_both_empty_errors_without_secret(tmp_path, monkeypatch):
    from hoodini.pairing import keys

    monkeypatch.setattr(keys, "DEFAULT_SETUP_KEY", "")
    with pytest.raises(
        ValueError,
        match="No setup key is bundled or saved; run hoodini extract-key APK",
    ):
        keys.resolve_setup_key(tmp_path)
    keys.save_setup_key("", tmp_path)
    with pytest.raises(
        ValueError,
        match="No setup key is bundled or saved; run hoodini extract-key APK",
    ):
        keys.resolve_setup_key(tmp_path)


def test_bundled_setup_key_has_no_surrounding_whitespace():
    from hoodini.pairing.keys import DEFAULT_SETUP_KEY

    if DEFAULT_SETUP_KEY and DEFAULT_SETUP_KEY != DEFAULT_SETUP_KEY.strip():
        pytest.fail("bundled setup key has surrounding whitespace", pytrace=False)


class FakeClient:
    def session(self, target):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def active():
            yield self

        return active()

    def __init__(self, transport):
        self.transport = transport

    async def networks(self, target):
        return [Network(b"synthetic-ssid", "WPA2", -40)]

    async def pair(self, target, network, psk, server, key):
        assert network.ssid == b"synthetic-ssid"
        return PairResult("SYNTHETIC-CUID", {"CUID": "SYNTHETIC-CUID"})


class FakeTransport:
    async def discover(self, timeout):
        return [BleTarget("synthetic", "ELC_SYNTHETIC")]


def test_tokenless_pairing_reports_generic(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "PairingClient", FakeClient, raising=False)
    monkeypatch.setattr(cli, "BleakTransport", FakeTransport, raising=False)
    monkeypatch.setattr(
        cli,
        "probe",
        lambda server: [{"cuid": "SYNTHETIC-CUID", "connection_id": "old"}],
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "device",
        lambda server, cuid: {
            "session_state": "ready",
            "connection_id": "new",
            "known_ids": [1],
        },
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "getpass",
        lambda prompt: "SYNTHETIC-PSK" if "Wi-Fi" in prompt else "",
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "input",
        lambda prompt: "yes" if "Use server" in prompt else "",
        raising=False,
    )
    from hoodini.pairing.keys import save_setup_key

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    asyncio.run(
        cli.pair(
            argparse.Namespace(
                server="192.0.2.1",
                ssid="synthetic-ssid",
                token=None,
                config_dir=tmp_path,
            )
        )
    )
    output = capsys.readouterr()
    assert "paired ✓" in output.out and "generic" in output.out
    assert "SYNTHETIC-PSK" not in output.out + output.err


def test_metadata_upload_failure_is_not_full_success(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "PairingClient", FakeClient, raising=False)
    monkeypatch.setattr(cli, "BleakTransport", FakeTransport, raising=False)
    monkeypatch.setattr(cli, "probe", lambda server: [], raising=False)
    monkeypatch.setattr(
        cli,
        "device",
        lambda server, cuid: {
            "session_state": "ready",
            "connection_id": "new",
            "known_ids": [1],
        },
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "upload",
        lambda *args: (_ for _ in ()).throw(ValueError("failure")),
        raising=False,
    )
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK", raising=False)
    monkeypatch.setattr(
        cli,
        "input",
        lambda prompt: "yes" if "Use server" in prompt else "",
        raising=False,
    )
    from hoodini.pairing.keys import save_setup_key

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    with pytest.raises(cli.PairCommandError, match="metadata upload failed"):
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token="SYNTHETIC-TOKEN",
                    config_dir=tmp_path,
                )
            )
        )
    assert "paired ✓" not in capsys.readouterr().out


def test_readiness_needs_new_nonnull_connection_id(monkeypatch):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

        async def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(cli.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cli.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(
        cli,
        "device",
        lambda *args: {
            "online": True,
            "session_state": "ready",
            "connection_id": "old",
        },
    )
    assert not asyncio.run(cli._wait("192.0.2.1", "SYNTHETIC-CUID", "old"))
    monkeypatch.setattr(
        cli,
        "device",
        lambda *args: {"online": True, "session_state": "ready", "connection_id": None},
    )
    assert not asyncio.run(cli._wait("192.0.2.1", "SYNTHETIC-CUID", None))
    monkeypatch.setattr(
        cli, "device", lambda *args: {"session_state": "ready", "connection_id": "new"}
    )
    assert asyncio.run(cli._wait("192.0.2.1", "SYNTHETIC-CUID", "old"))


def test_first_status_wait_and_timeout_warning(monkeypatch, capsys):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

        async def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(cli.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cli.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(cli, "device", lambda *args: {"known_ids": []})
    assert not asyncio.run(cli._wait("192.0.2.1", "SYNTHETIC-CUID", None, status=True))
    assert "first full status" in capsys.readouterr().out
    monkeypatch.setattr(cli, "device", lambda *args: {"known_ids": [96]})
    assert asyncio.run(cli._wait("192.0.2.1", "SYNTHETIC-CUID", None, status=True))


def test_invalid_server_rejected_before_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli,
        "input",
        lambda prompt: pytest.fail("prompt before validation"),
        raising=False,
    )
    with pytest.raises(cli.PairCommandError, match="IPv4"):
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="invalid", ssid=None, token=None, config_dir=tmp_path
                )
            )
        )


def test_probe_failure_stops_before_ble(monkeypatch, tmp_path):
    from hoodini.pairing.keys import save_setup_key

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK")
    monkeypatch.setattr(
        cli, "probe", lambda server: (_ for _ in ()).throw(OSError("private"))
    )
    monkeypatch.setattr(cli, "BleakTransport", lambda: pytest.fail("BLE before probe"))
    with pytest.raises(cli.PairCommandError, match="Server probe failed"):
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token=None,
                    config_dir=tmp_path,
                )
            )
        )


def test_unresolved_network_tie_requires_selection(monkeypatch):
    monkeypatch.setattr(cli, "input", lambda prompt: "2", raising=False)
    one = Network(b"synthetic-ssid", "AUTH1", -40)
    two = Network(b"synthetic-ssid", "AUTH2", -40)
    assert cli._choose([one, two], lambda network: network.auth) is two


def test_explicit_ipv4_skips_bonjour_and_still_probes(monkeypatch, tmp_path):
    from hoodini.pairing.keys import save_setup_key

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK")
    monkeypatch.setattr(
        cli, "browse_servers", lambda: pytest.fail("Bonjour should be skipped")
    )
    monkeypatch.setattr(
        cli,
        "probe",
        lambda server: pytest.fail("probe reached") if server != "192.0.2.1" else [],
    )

    class EmptyTransport:
        async def discover(self, timeout):
            return []

    monkeypatch.setattr(cli, "BleakTransport", EmptyTransport)
    with pytest.raises(cli.PairCommandError, match="No hood found"):
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token=None,
                    config_dir=tmp_path,
                )
            )
        )


def test_no_hood_prints_t6_instructions(monkeypatch, tmp_path):
    from hoodini.pairing.keys import save_setup_key

    class EmptyTransport:
        async def discover(self, timeout):
            return []

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK")
    monkeypatch.setattr(cli, "probe", lambda server: [])
    monkeypatch.setattr(cli, "BleakTransport", EmptyTransport)
    with pytest.raises(cli.PairCommandError, match="T6.*6 s.*6 s.*5 minutes"):
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token=None,
                    config_dir=tmp_path,
                )
            )
        )


def _local_probe_listener(monkeypatch, tmp_path, *, tls):
    import datetime
    import socket
    import ssl
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(4)
    port = listener.getsockname()[1]
    raw_connect = socket.create_connection

    def redirect(address, timeout):
        assert address == ("127.0.0.1", 8883)
        return raw_connect(("127.0.0.1", port), timeout=timeout)

    monkeypatch.setattr(cli.socket, "create_connection", redirect)
    if tls:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(
                datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
            )
            .not_valid_after(
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
            )
            .sign(key, hashes.SHA256())
        )
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)

    def serve():
        try:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(4)
                    if tls:
                        with context.wrap_socket(
                            connection, server_side=True
                        ) as secured:
                            assert (
                                secured.recv(1) == b""
                            )  # handshake-only: immediate close
                    else:
                        connection.recv(1024)  # accept a TLS ClientHello, never reply
        except (OSError, ssl.SSLError):
            pass  # expected from failed handshake on plain listener

    thread = threading.Thread(target=serve)
    thread.start()
    return thread


def test_probe_tls_handshake_self_signed_local_only(monkeypatch, tmp_path):
    thread = _local_probe_listener(monkeypatch, tmp_path, tls=True)
    monkeypatch.setattr(cli, "_request", lambda server, path: [])
    try:
        assert cli.probe("127.0.0.1") == []
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_probe_plain_tcp_rejected_before_ble(monkeypatch, tmp_path):
    thread = _local_probe_listener(monkeypatch, tmp_path, tls=False)
    monkeypatch.setattr(
        cli, "_request", lambda server, path: pytest.fail("API after failed TLS")
    )
    try:
        with pytest.raises((OSError, TimeoutError)):
            cli.probe("127.0.0.1")
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_probe_refused_before_ble(monkeypatch):
    def refused(address, timeout):
        assert address == ("127.0.0.1", 8883)
        raise ConnectionRefusedError("synthetic refused")

    monkeypatch.setattr(cli.socket, "create_connection", refused)
    monkeypatch.setattr(
        cli, "_request", lambda server, path: pytest.fail("API after refusal")
    )
    with pytest.raises(ConnectionRefusedError):
        cli.probe("127.0.0.1")


def test_oversize_network_reports_cause_and_never_writes(monkeypatch, tmp_path):
    from hoodini.pairing.client import PairingClient
    from hoodini.pairing.keys import save_setup_key

    class WritingTransport(FakeTransport):
        def __init__(self):
            self.writes = []

        async def write(self, frame):
            self.writes.append(frame)

    transport = WritingTransport()

    class OversizeClient(FakeClient):
        async def pair(self, target, network, psk, server, key):
            return await PairingClient(self.transport).pair(
                target, network, psk, server, key
            )

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    monkeypatch.setattr(cli, "PairingClient", OversizeClient)
    monkeypatch.setattr(cli, "BleakTransport", lambda: transport)
    monkeypatch.setattr(cli, "probe", lambda server: [])
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK-" + '"' * 200)
    with pytest.raises(
        cli.PairCommandError,
        match="encoded network payload length .* exceeds 245 bytes",
    ) as failure:
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token=None,
                    config_dir=tmp_path,
                )
            )
        )
    assert "hoodini[pairing]" not in str(failure.value)
    assert transport.writes == []


@pytest.mark.parametrize(
    "stage,message",
    [
        ("wifi", "Wi-Fi wrong password"),
        ("network_par", "Provisioning authentication password rejected"),
        ("ssid_list", "SSID list stayed empty before setup deadline"),
    ],
)
def test_pairing_error_uses_fixed_message(monkeypatch, tmp_path, stage, message):
    from hoodini.pairing.client import PairingError
    from hoodini.pairing.keys import save_setup_key

    class FailureClient(FakeClient):
        async def pair(self, *args):
            raise PairingError(stage, message)

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    monkeypatch.setattr(cli, "PairingClient", FailureClient)
    monkeypatch.setattr(cli, "BleakTransport", FakeTransport)
    monkeypatch.setattr(cli, "probe", lambda server: [])
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK")
    with pytest.raises(cli.PairCommandError) as error:
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token=None,
                    config_dir=tmp_path,
                )
            )
        )
    assert str(error.value) == message


def test_wait_yields_to_event_loop_between_polls(monkeypatch):
    ticks = []
    responses = iter([{}, {"session_state": "ready", "connection_id": "new"}])
    monkeypatch.setattr(cli, "device", lambda *args: next(responses))

    async def run():
        asyncio.get_running_loop().call_soon(ticks.append, "event loop tick")
        assert await cli._wait("192.0.2.1", "SYNTHETIC-CUID", "old")
        assert ticks == ["event loop tick"]

    asyncio.run(run())


def test_probe_runs_off_event_loop(monkeypatch, tmp_path):
    import threading

    from hoodini.pairing.keys import save_setup_key

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    owner = threading.get_ident()

    def probe_off_loop(server):
        assert threading.get_ident() != owner
        return []

    class StopAfterProbe(Exception):
        pass

    monkeypatch.setattr(cli, "probe", probe_off_loop)
    monkeypatch.setattr(
        cli, "BleakTransport", lambda: (_ for _ in ()).throw(StopAfterProbe())
    )
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK")
    with pytest.raises(StopAfterProbe):
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token=None,
                    config_dir=tmp_path,
                )
            )
        )


def test_multiple_servers_and_hoods_selected_and_confirmed(monkeypatch, tmp_path):
    from hoodini.pairing.keys import save_setup_key

    chosen = []

    async def browse():
        return ["192.0.2.1", "192.0.2.2"]

    class MultipleTransport:
        async def discover(self, timeout):
            return [BleTarget("first", "ELC_FIRST"), BleTarget("second", "ELC_SECOND")]

    class SelectedClient(FakeClient):
        async def networks(self, target):
            chosen.append(target.id)
            return await super().networks(target)

        async def pair(self, target, network, psk, server, key):
            chosen.append((server, target.id, psk, key))
            return await super().pair(target, network, psk, server, key)

    async def ready(*args, **kwargs):
        return True

    def choose(prompt):
        if prompt == "Select number: ":
            return "2"
        if prompt == "Use server 192.0.2.2? [y/N] ":
            return "yes"
        return ""

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    monkeypatch.setattr(cli, "browse_servers", browse)
    monkeypatch.setattr(cli, "PairingClient", SelectedClient)
    monkeypatch.setattr(cli, "BleakTransport", MultipleTransport)
    monkeypatch.setattr(cli, "probe", lambda server: [])
    monkeypatch.setattr(cli, "_wait", ready)
    monkeypatch.setattr(cli, "input", choose, raising=False)
    monkeypatch.setattr(
        cli, "getpass", lambda prompt: "SYNTHETIC-PSK" if "Wi-Fi" in prompt else ""
    )
    asyncio.run(
        cli.pair(
            argparse.Namespace(
                server=None, ssid="synthetic-ssid", token=None, config_dir=tmp_path
            )
        )
    )
    assert chosen == [
        "second",
        ("192.0.2.2", "second", "SYNTHETIC-PSK", "SYNTHETIC-KEY"),
    ]


@pytest.mark.parametrize("reuse,uploaded", [("yes", True), ("no", False)])
def test_cross_endpoint_token_reuse_requires_prompt(
    monkeypatch, tmp_path, capsys, reuse, uploaded
):
    from hoodini.pairing.keys import save_setup_key, save_token

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    save_token("192.0.2.2:8765", "SYNTHETIC-OTHER-TOKEN", tmp_path)
    uploads = []

    async def ready(*args, **kwargs):
        return True

    monkeypatch.setattr(cli, "PairingClient", FakeClient)
    monkeypatch.setattr(cli, "BleakTransport", FakeTransport)
    monkeypatch.setattr(cli, "probe", lambda server: [])
    monkeypatch.setattr(cli, "_wait", ready)
    monkeypatch.setattr(cli, "upload", lambda *args: uploads.append(args))
    monkeypatch.setattr(
        cli,
        "input",
        lambda prompt: (
            reuse
            if prompt.startswith("Reuse it")
            else "yes"
            if prompt.startswith("Use server")
            else "no"
        ),
        raising=False,
    )
    monkeypatch.setattr(
        cli, "getpass", lambda prompt: "SYNTHETIC-PSK" if "Wi-Fi" in prompt else ""
    )
    asyncio.run(
        cli.pair(
            argparse.Namespace(
                server="192.0.2.1",
                ssid="synthetic-ssid",
                token=None,
                config_dir=tmp_path,
            )
        )
    )
    assert bool(uploads) is uploaded
    if uploaded:
        assert uploads[0][3] == "SYNTHETIC-OTHER-TOKEN"
    else:
        assert "generic" in capsys.readouterr().out


def test_missing_pairing_extra_install_hint(monkeypatch, tmp_path):
    from hoodini.pairing.keys import save_setup_key
    from hoodini.protocol.ble import ProvisioningError

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    monkeypatch.setattr(cli, "probe", lambda server: [])
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK")
    monkeypatch.setattr(
        cli,
        "BleakTransport",
        lambda: (_ for _ in ()).throw(
            ProvisioningError("Install hoodini[pairing] for BLE pairing")
        ),
    )
    with pytest.raises(cli.PairCommandError, match=r"hoodini\[pairing\]"):
        asyncio.run(
            cli.pair(
                argparse.Namespace(
                    server="192.0.2.1",
                    ssid="synthetic-ssid",
                    token=None,
                    config_dir=tmp_path,
                )
            )
        )


def test_missing_extract_extra_install_hint(monkeypatch, tmp_path):
    from hoodini.pairing.keys import ExtractionError

    monkeypatch.setattr(
        cli,
        "extract_setup_key",
        lambda path: (_ for _ in ()).throw(
            ExtractionError("Install hoodini[extract] to extract a setup key")
        ),
    )
    with pytest.raises(cli.PairCommandError, match=r"hoodini\[extract\]"):
        cli.extract(
            argparse.Namespace(apk=tmp_path / "synthetic.apk", config_dir=tmp_path)
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["plain", "refused"])
def test_tls_failure_stops_pair_before_ble(monkeypatch, tmp_path, kind):
    from hoodini.pairing.keys import save_setup_key

    save_setup_key("SYNTHETIC-KEY", tmp_path)
    thread = None
    if kind == "plain":
        thread = _local_probe_listener(monkeypatch, tmp_path, tls=False)
    else:

        def refused(address, timeout):
            assert address == ("127.0.0.1", 8883)
            raise ConnectionRefusedError("synthetic refused")

        monkeypatch.setattr(cli.socket, "create_connection", refused)
    monkeypatch.setattr(cli, "_request", lambda *args: pytest.fail("HTTP before TLS"))
    monkeypatch.setattr(cli, "BleakTransport", lambda: pytest.fail("BLE before TLS"))
    monkeypatch.setattr(cli, "input", lambda prompt: "yes", raising=False)
    monkeypatch.setattr(cli, "getpass", lambda prompt: "SYNTHETIC-PSK")
    try:
        with pytest.raises(cli.PairCommandError, match="Server probe failed"):
            asyncio.run(
                cli.pair(
                    argparse.Namespace(
                        server="127.0.0.1",
                        ssid="synthetic-ssid",
                        token=None,
                        config_dir=tmp_path,
                    )
                )
            )
    finally:
        if thread:
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_pair_verbose_logs_redacted_network_par(caplog):
    import logging
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from hoodini.pairing.client import PairingClient
    from support.ble import TARGET, FakeTransport, reply

    async def scenario():
        fake = FakeTransport(
            [reply(6, {"RESULT": "OK", "ERROR": "WIFI WRONG PASSWORD"})]
        )
        client = PairingClient(fake)
        from hoodini.pairing.client import Network, PairingError

        with pytest.raises(PairingError):
            await client.pair(
                TARGET,
                Network(b"Net", "WPA2", -40),
                "SYNTH-PSK",
                "192.0.2.4",
                "SYNTH-KEY",
            )

    with caplog.at_level(logging.DEBUG, logger="hoodini.pairing"):
        asyncio.run(scenario())
    assert "Inbound BLE notification" in caplog.text
    assert "NETWORK_PAR" in caplog.text
    assert "SYNTH-PSK" not in caplog.text and "SYNTH-KEY" not in caplog.text


def test_first_status_wait_covers_observed_hardware_delay():
    # The hood's first full status has been observed 215 s and 302 s after
    # readiness; a 300 s wait expired seconds before it arrived.
    assert cli.FIRST_STATUS_WAIT >= 302 + 60
