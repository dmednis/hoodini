"""Broker failure reasons are specific enough to act on and never echo credentials."""

import asyncio
import logging
import socket
import ssl

import aiomqtt
import pytest
from aiomqtt.exceptions import MqttConnectError
from hoodini.config import MqttSettings
from hoodini.ha_bridge import client as bridge_client
from hoodini.ha_bridge.client import HaBridge, broker_failure_reason
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

SECRET = "SYNTHETIC-BROKER-SECRET"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            MqttConnectError(ReasonCode(PacketTypes.CONNACK, "Not authorized")),
            "broker rejected the connection",
        ),
        (MqttConnectError(5), "broker rejected the connection (code 5)"),
        (aiomqtt.MqttError("Operation timed out"), "connection timed out"),
        (
            aiomqtt.MqttError(f"[Errno 61] Connect call failed ('{SECRET}', 1883)"),
            "connection refused",
        ),
        (
            aiomqtt.MqttError(f"[Errno 8] nodename nor servname provided: {SECRET}"),
            "broker host name could not be resolved",
        ),
        (
            aiomqtt.MqttError("[Errno -2] Name or service not known"),
            "broker host name could not be resolved",
        ),
        (
            aiomqtt.MqttError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
            ),
            "TLS certificate rejected",
        ),
        (ssl.SSLError(1, SECRET), "TLS handshake failed"),
        (ValueError(SECRET), "invalid broker URL"),
        (aiomqtt.MqttError(f"unexpected {SECRET}"), "connection failed"),
    ],
)
def test_reason_is_specific_and_never_echoes_input(error, expected):
    reason = broker_failure_reason(error)
    assert reason == expected
    assert SECRET not in reason


def test_refused_broker_logs_reason_without_credentials(tmp_path, caplog):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    class Devices:
        def list(self):
            return []

        def get(self, cuid):
            return None

        def subscribe(self, listener):
            class Handle:
                def __call__(self):
                    pass

                async def wait_closed(self):
                    await asyncio.Event().wait()

            return Handle()

    async def scenario():
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 1:
                raise asyncio.CancelledError

        settings = MqttSettings(
            f"mqtt://127.0.0.1:{port}", username="user", password=SECRET
        )
        from hoodini.storage import AtomicStore

        bridge = HaBridge(settings, Devices(), AtomicStore(tmp_path), sleep=sleep)
        with caplog.at_level(logging.WARNING, logger=bridge_client.__name__):
            await bridge.start()
            try:
                await asyncio.wait_for(bridge._task, 10)
            except asyncio.CancelledError:
                pass
            bridge._task = None

    asyncio.run(scenario())
    assert "HA bridge broker unavailable: connection refused" in caplog.text
    assert SECRET not in caplog.text
    assert "user" not in caplog.text.replace("username", "")
