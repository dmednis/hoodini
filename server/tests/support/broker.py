"""Fixture-owned, isolated MQTT test broker."""

import asyncio
import os
import socket
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiomqtt
import pytest

IMAGE = "eclipse-mosquitto@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408"
PREFIX = "hoodini-test-mqtt-"


class Broker:
    def __init__(self, url, name=None, port=None):
        self.url, self.name, self.port = url, name, port

    def stop(self):
        if self.name:
            subprocess.run(
                ["docker", "rm", "-f", self.name], capture_output=True, check=True
            )

    async def restart(self):
        if self.name is None:
            pytest.skip("broker restart requires fixture-owned Docker broker")
        self.stop()
        _start(self.name, self.port)
        await _ready(self.url)


def _start(name, port):
    conf = Path(__file__).with_name("mosquitto.conf").resolve()
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:1883",
            "-v",
            f"{conf}:/mosquitto/config/mosquitto.conf:ro",
            IMAGE,
        ],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def broker():
    external = os.environ.get("HOODINI_TEST_MQTT_URL")
    if external:
        parsed = urlsplit(external)
        if parsed.scheme != "mqtt" or not parsed.hostname or not parsed.port:
            pytest.fail("invalid HOODINI_TEST_MQTT_URL")
        instance = Broker(external)
    else:
        name = PREFIX + uuid.uuid4().hex
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", 18883))
            except OSError:
                sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        try:
            _start(name, port)
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.skip(f"fixture Docker broker unavailable: {type(exc).__name__}")
        instance = Broker(f"mqtt://127.0.0.1:{port}", name, port)
    try:
        asyncio.run(_ready(instance.url))
        yield instance
    finally:
        instance.stop()


async def _ready(url):
    parsed = urlsplit(url)
    for _ in range(100):
        try:
            async with aiomqtt.Client(
                parsed.hostname,
                parsed.port,
                username=parsed.username,
                password=parsed.password,
            ):
                return
        except (OSError, aiomqtt.MqttError):
            await asyncio.sleep(0.05)
    pytest.fail("fixture MQTT broker did not become ready")
