"""Synthetic bounded provisioning conversations, without a Bluetooth adapter."""

import asyncio
import builtins
import functools
import logging
import sys
import types
from pathlib import Path

import pytest
from hoodini.pairing.client import Network, PairingClient
from hoodini.pairing.transport import BleakTransport, BleTarget
from hoodini.protocol.ble import ProvisioningError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.ble import TARGET, FakeClock, FakeTransport, reply

INFO = {"CUID": "synthetic-cuid", "WIFI STATUS": "SOFTAP", "ERROR": ""}
CLOUD = {**INFO, "WIFI STATUS": "CLOUD CONNECTED"}
SSID = {"SSID_LIST": [{"SSID_NAME": "Café", "SSID_AUTH": "WPA2", "SSID_RSSI": -42}]}
NETWORK = Network("Café".encode(), "WPA2", -42)


def run_async(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def client(replies, *, size=256):
    transport = FakeTransport(replies, size=size)
    clock = FakeClock()
    return (
        PairingClient(transport, clock=clock.now, sleep=clock.sleep),
        transport,
        clock,
    )


@run_async
async def test_empty_then_populated_ssid_list_preserves_bytes_and_splits():
    pairing, fake, clock = client(
        [reply(2, INFO, split=3), reply(4, {"SSID_LIST": []}), reply(4, SSID, split=7)]
    )
    assert await pairing.networks(TARGET) == [NETWORK]
    assert fake.writes[:2] == [
        bytes.fromhex("5553480200b849"),
        bytes.fromhex("555348040012ef"),
    ]
    assert fake.disconnections == 1
    assert clock.sleeps.count(3) >= 1


@run_async
async def test_oversized_network_never_written():
    pairing, fake, _ = client([reply(2, INFO)])
    with pytest.raises(ProvisioningError):
        await pairing.pair(TARGET, NETWORK, '"' * 200, "192.0.2.4", "synthetic-key")
    assert not any(frame[3] == 6 for frame in fake.writes)


@run_async
async def test_fragmented_json_mismatched_command_and_unknown_crc():
    payload = b'{"SSID_LIST":[{"SSID_NAME":"Cafe","SSID_AUTH":"WPA2","SSID_RSSI":-25}]}'
    first = payload[:25]
    second = payload[25:]
    part = b"\x55\x53\x48" + bytes((0xC4, len(first))) + first + b"\xff\xff"
    last = b"\x55\x53\x48" + bytes((0x44, len(second))) + second + b"\xff\xff"
    pairing, fake, _ = client(
        [reply(2, INFO), [*reply(2, INFO), part[:4], part[4:], last]]
    )
    assert await pairing.networks(TARGET) == [Network(b"Cafe", "WPA2", -25)]
    assert fake.disconnections == 1


@run_async
async def test_pair_progression_network_par_once_and_redacted(caplog):
    pairing, fake, clock = client(
        [
            reply(6, {"RESULT": "OK"}),
            reply(2, INFO),
            reply(2, {**INFO, "WIFI STATUS": "CONNECTING"}),
            reply(2, CLOUD),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="hoodini.pairing"):
        result = await pairing.pair(
            TARGET,
            NETWORK,
            "synthetic-PSK-secret",
            "192.0.2.4",
            "synthetic-AUTH-secret",
        )
    assert result.cuid == "synthetic-cuid" and result.info == CLOUD
    assert [frame[3] for frame in fake.writes] == [6, 2, 2, 2]
    assert clock.sleeps.count(3) == 2
    assert fake.disconnections == 1
    text = caplog.text
    assert "synthetic-PSK-secret" not in text and "synthetic-AUTH-secret" not in text
    assert "***" in text


@run_async
@pytest.mark.parametrize(
    ("error", "stage"),
    [("WIFI WRONG PASSWORD", "wifi"), ("AUTH_PWD_WRONG", "network_par")],
)
async def test_provisioning_stage_errors(error, stage):
    pairing, fake, _ = client(
        [reply(6, {"RESULT": "OK"}), reply(2, {**INFO, "ERROR": error})]
    )
    with pytest.raises(ProvisioningError) as exc:
        await pairing.pair(
            TARGET, NETWORK, "synthetic-pass", "192.0.2.4", "synthetic-key"
        )
    assert exc.value.stage == stage
    assert fake.disconnections == 1


@run_async
async def test_rejected_network_parameters_stop_polling():
    pairing, fake, _ = client([reply(6, {"RESULT": "FAIL"})])
    with pytest.raises(ProvisioningError) as exc:
        await pairing.pair(
            TARGET, NETWORK, "synthetic-pass", "192.0.2.4", "synthetic-key"
        )
    assert exc.value.stage == "network_par"
    assert [frame[3] for frame in fake.writes] == [6]
    assert fake.disconnections == 1


@run_async
async def test_connection_loss_mid_provisioning():
    pairing, fake, _ = client([reply(6, {"RESULT": "OK"})])
    original = fake.write

    async def write(frame):
        await original(frame)
        fake.lost = True

    fake.write = write
    with pytest.raises(ProvisioningError, match="Connection lost"):
        await pairing.pair(
            TARGET, NETWORK, "synthetic-pass", "192.0.2.4", "synthetic-key"
        )
    assert fake.disconnections == 1
    assert [frame[3] for frame in fake.writes] == [6]


@run_async
async def test_cancellation_disconnects_and_reraises():
    pairing, fake, _ = client([reply(6, {"RESULT": "OK"})])
    fake.block = True
    task = asyncio.create_task(
        pairing.pair(TARGET, NETWORK, "synthetic-pass", "192.0.2.4", "synthetic-key")
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if fake.writes:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.disconnections == 1


@run_async
async def test_connect_retry_limit():
    pairing, fake, _ = client([])
    fake.fail_connect = 4
    with pytest.raises(ProvisioningError) as exc:
        await pairing.networks(TARGET)
    assert exc.value.stage == "connect"
    assert fake.connections == 3 and fake.disconnections == 4


@run_async
async def test_unsupported_transport_never_writes_network_par():
    pairing, fake, _ = client([], size=30)
    with pytest.raises(ProvisioningError) as exc:
        await pairing.pair(
            TARGET, NETWORK, "synthetic-pass", "192.0.2.4", "synthetic-key"
        )
    assert exc.value.stage == "unsupported_transport"
    assert not any(frame[3] == 6 for frame in fake.writes)


@run_async
async def test_empty_ssid_deadline_and_disconnect():
    pairing, fake, clock = client([reply(2, INFO)] + [reply(4, {"SSID_LIST": []})] * 60)
    with pytest.raises(ProvisioningError) as exc:
        await pairing.networks(TARGET)
    assert exc.value.stage == "ssid_list"
    assert clock.now() == 180
    assert fake.disconnections == 1


@run_async
async def test_status_deadline_never_retries_network_par():
    pairing, fake, clock = client([reply(6, {"RESULT": "OK"})] + [reply(2, INFO)] * 60)
    with pytest.raises(ProvisioningError) as exc:
        await pairing.pair(
            TARGET, NETWORK, "synthetic-pass", "192.0.2.4", "synthetic-key"
        )
    assert exc.value.stage == "cloud"
    assert clock.now() == 180
    assert sum(frame[3] == 6 for frame in fake.writes) == 1
    assert fake.disconnections == 1


@run_async
async def test_network_reply_timeout_does_not_retry_write():
    pairing, fake, _ = client([])
    with pytest.raises(ProvisioningError) as exc:
        await pairing.pair(
            TARGET, NETWORK, "synthetic-pass", "192.0.2.4", "synthetic-key"
        )
    assert exc.value.stage == "network_par"
    assert [frame[3] for frame in fake.writes] == [6]
    assert fake.disconnections == 1


@run_async
async def test_bleak_adapter_uses_names_and_write_with_response(monkeypatch):
    from hoodini.pairing import transport

    class Scanner:
        @staticmethod
        async def discover(*, timeout):
            assert timeout == 15
            return [
                types.SimpleNamespace(address="a", name="ELC_1"),
                types.SimpleNamespace(address="b", name="OTHER"),
            ]

    class Adapter:
        def __init__(self, device, *, services, timeout, disconnected_callback):
            assert device.address == "a"
            assert services == [transport.SERVICE_UUID] and timeout == 15
            self.disconnected_callback = disconnected_callback
            self.mtu_size = 256
            self.callback = None
            self.written = []

        async def connect(self):
            pass

        async def start_notify(self, uuid, callback):
            assert uuid == transport.NOTIFY_UUID
            self.callback = callback

        async def write_gatt_char(self, uuid, frame, *, response):
            self.written.append((uuid, frame, response))

        async def disconnect(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "bleak",
        types.SimpleNamespace(BleakClient=Adapter, BleakScanner=Scanner),
    )
    adapter = BleakTransport()
    assert await adapter.discover(90) == [BleTarget("a", "ELC_1")]
    await adapter.connect(BleTarget("a", "ELC_1"))
    assert adapter.max_write_size() == 253
    await adapter.write(b"literal")
    assert adapter._client.written == [(transport.WRITE_UUID, b"literal", True)]
    adapter._client.callback(None, bytearray(b"notify"))
    assert await adapter.read_notification() == b"notify"
    adapter._client.disconnected_callback(adapter._client)
    with pytest.raises(ConnectionError):
        await adapter.read_notification()
    await adapter.disconnect()


def test_base_import_without_bleak_and_missing_extra(monkeypatch):
    original = builtins.__import__

    def no_bleak(name, *args, **kwargs):
        if name.startswith("bleak"):
            raise ImportError("no bleak")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_bleak)
    from hoodini.pairing import transport

    assert transport.BleTarget("x", "ELC_x") == BleTarget("x", "ELC_x")
    with pytest.raises(ProvisioningError, match=r"hoodini\[pairing\]"):
        BleakTransport()


@run_async
async def test_setup_conversation_reuses_one_connection():
    pairing, fake, _ = client(
        [reply(2, INFO), reply(4, SSID), reply(6, {"RESULT": "OK"}), reply(2, CLOUD)]
    )
    async with pairing.session(TARGET):
        network = (await pairing.networks(TARGET))[0]
        result = await pairing.pair(
            TARGET, network, "synthetic-pass", "192.0.2.4", "synthetic-key"
        )
    assert result.cuid == "synthetic-cuid"
    assert fake.connections == fake.disconnections == 1


@run_async
async def test_scan_allows_scanner_cleanup_margin():
    class Scanner:
        @staticmethod
        async def discover(timeout):
            await asyncio.sleep(timeout + 0.01)
            return [type("Device", (), {"address": "AA", "name": "ELC_TEST"})()]

    transport = BleakTransport.__new__(BleakTransport)
    transport._scanner = Scanner
    transport._devices = {}
    assert await transport.discover(0.05) == [BleTarget("AA", "ELC_TEST")]


@run_async
async def test_connect_retries_rescan_and_name_the_failure():
    pairing, fake, clock = client([])
    fake.fail_connect = 4
    fake.connect_error = OSError("hood dropped: disconnected")
    with pytest.raises(ProvisioningError) as exc:
        await pairing.networks(TARGET)
    assert exc.value.stage == "connect"
    assert fake.rediscovered == [TARGET, TARGET]
    assert 2 in clock.sleeps
    assert str(exc.value) == (
        "Connection failed after 3 attempts (hood dropped the connection)"
    )


@run_async
async def test_bleak_connect_reuses_scanned_device_and_limits_services(monkeypatch):
    from hoodini.pairing import transport

    scanned = types.SimpleNamespace(address="a", name="ELC_1")
    refreshed = types.SimpleNamespace(address="a", name="ELC_1")
    clients = []

    class Scanner:
        @staticmethod
        async def discover(*, timeout):
            return [scanned]

        @staticmethod
        async def find_device_by_address(address, *, timeout):
            assert address == "a"
            return refreshed

    class Adapter:
        def __init__(self, device, *, services, timeout, disconnected_callback):
            clients.append((device, services, timeout))
            self.mtu_size = 256

        async def connect(self):
            pass

        async def start_notify(self, uuid, callback):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "bleak",
        types.SimpleNamespace(BleakClient=Adapter, BleakScanner=Scanner),
    )
    adapter = BleakTransport()
    await adapter.discover(15)
    await adapter.connect(BleTarget("a", "ELC_1"))
    await adapter.disconnect()
    await adapter.rediscover(BleTarget("a", "ELC_1"), 10)
    await adapter.connect(BleTarget("a", "ELC_1"))
    assert clients == [
        (scanned, [transport.SERVICE_UUID], 15),
        (refreshed, [transport.SERVICE_UUID], 15),
    ]
