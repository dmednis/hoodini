"""Installed CLI, TLS synthetic peer, LAN API and fixture MQTT broker."""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import aiomqtt
import pytest
from aiohttp import ClientSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.hood import Hood
from support.process import ServerProcess

pytest_plugins = ["support.broker"]
pytestmark = pytest.mark.mqtt

FIRST = b"\x00\x00\x00\x01\xd8\x75\x30\x00\x60\x32\x00\x00\x10"
SECOND = b"\x00\x00\x00\x02\xd8\x75\x30\x00\x60\x05\x00\x00\xe4"
COMMAND = (
    b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
    b"\x00\x2a\x8d\x00\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x09"
)


def mqtt(url):
    parsed = urlsplit(url)
    return aiomqtt.Client(parsed.hostname, parsed.port)


async def message(messages, topic, payload=None, timeout=8):
    async def match():
        async for item in messages:
            if str(item.topic) == topic and (
                payload is None or item.payload == payload
            ):
                return item

    return await asyncio.wait_for(match(), timeout)


async def ready_hood(server):
    hood = await Hood.open(server.gateway_port)
    assert await hood.connect(keepalive=0) == b"\x20\x02\x00\x00"
    assert await hood.subscribe() == b"\x90\x03\x00\x01\x00"
    await hood.publish("connect", b"\x01\x01\x02\x00\x03\x07")
    assert b"/connected" in await hood.packet()
    assert b"/time" in await hood.packet()
    return hood


async def device(session, server):
    async with session.get(
        f"http://127.0.0.1:{server.api_port}/api/v1/devices/hood1"
    ) as response:
        assert response.status == 200
        return await response.json()


async def until(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.02)


def test_full_flow_report_not_optimism(broker, tmp_path):
    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        hood = None
        try:
            await server.start()
            async with mqtt(broker.url) as observer, ClientSession() as http:
                await observer.subscribe("#")
                async with asyncio.timeout(60):
                    hood = await ready_hood(server)
                    await hood.publish("status", FIRST)
                    await until(lambda: online(http, server))
                    config = await message(
                        observer.messages, "homeassistant/device/hoodini_hood1/config"
                    )
                    assert (
                        json.loads(config.payload)["cmps"]["light"]["unique_id"]
                        == "hoodini_hood1_light"
                    )
                    await message(
                        observer.messages,
                        "hoodini/hood1/state",
                        b'{"cooktop_light_intensity":50,"rssi":-40}',
                    )
                    await until(lambda: reported(http, server, 50))
                    await observer.publish(
                        "hoodini/hood1/set/light", b'{"brightness":5}', qos=1
                    )
                    assert await hood.packet() == COMMAND
                    assert (await device(http, server))["caps"]["96"] == 50
                    await hood.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
                    assert (await device(http, server))["caps"]["96"] == 50
                    await hood.publish("status", SECOND)
                    await message(
                        observer.messages,
                        "hoodini/hood1/state",
                        b'{"cooktop_light_intensity":5,"rssi":-40}',
                    )
                    await until(lambda: reported(http, server, 5))
        finally:
            if hood is not None:
                await hood.close()
            await server.stop()

    asyncio.run(scenario())


async def online(http, server):
    try:
        return (await device(http, server))["online"]
    except AssertionError:
        return False


async def reported(http, server, value):
    try:
        return (await device(http, server))["caps"].get("96") == value
    except AssertionError:
        return False


def test_crash_lwt_then_disconnected_restart(broker, tmp_path):
    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        hood = None
        try:
            await server.start()
            async with mqtt(broker.url) as observer:
                await observer.subscribe("#")
                hood = await ready_hood(server)
                await hood.publish("status", FIRST)
                await message(
                    observer.messages, "hoodini/hood1/availability", b"online"
                )
                await server.stop(crash=True)
                await message(observer.messages, "hoodini/bridge/state", b"offline")
                await hood.close()
                hood = None
                await server.start()
                await message(
                    observer.messages, "hoodini/hood1/availability", b"offline"
                )
                async with ClientSession() as http:
                    assert not (await device(http, server))["online"]
        finally:
            if hood is not None:
                await hood.close()
            await server.stop()

    asyncio.run(scenario())


def test_graceful_restart_requires_fresh_status(broker, tmp_path):
    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        hood = None
        try:
            await server.start()
            hood = await ready_hood(server)
            await hood.publish("status", FIRST)
            async with ClientSession() as http:
                await until(lambda: reported(http, server, 50))
                await server.stop()
                await hood.close()
                hood = None
                await server.start()
                hood = await ready_hood(server)
                async with mqtt(broker.url) as observer:
                    await observer.subscribe("hoodini/hood1/availability")
                    assert (await device(http, server))["online"] is False
                    await hood.publish("status", FIRST)
                    await message(
                        observer.messages, "hoodini/hood1/availability", b"online"
                    )
                    assert (await device(http, server))["online"] is True
        finally:
            if hood is not None:
                await hood.close()
            await server.stop()

    asyncio.run(scenario())


def test_gateway_cuid_logs_escape_newline_and_log_session_close(broker, tmp_path):
    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        hood = None
        try:
            await server.start()
            hood = await Hood.open(server.gateway_port, "line\nforge")
            assert await hood.connect(keepalive=0) == b"\x20\x02\x00\x00"
            await hood.close()
            hood = None
            async with asyncio.timeout(5):
                while "session_closed" not in server.log.read_text():
                    await asyncio.sleep(0.02)
            lines = server.log.read_text().splitlines()
            assert not any(line == "forge" for line in lines)
            assert any(
                "session_closed cuid='line\\nforge' reason=disconnected" in line
                for line in lines
            )
            assert any("accepted cuid='line\\nforge'" in line for line in lines)
        finally:
            if hood is not None:
                await hood.close()
            await server.stop()

    asyncio.run(scenario())


def test_broker_restart_republishes_without_replacing_hood(broker, tmp_path):
    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        hood = None
        try:
            await server.start()
            hood = await ready_hood(server)
            await hood.publish("status", FIRST)
            async with ClientSession() as http:
                await until(lambda: reported(http, server, 50))
                old = (await device(http, server))["connection_id"]
                await broker.restart()
                async with mqtt(broker.url) as observer:
                    await observer.subscribe("hoodini/hood1/state")
                    await message(
                        observer.messages,
                        "hoodini/hood1/state",
                        b'{"cooktop_light_intensity":50,"rssi":-40}',
                        timeout=20,
                    )
                    assert (await device(http, server))["connection_id"] == old
                    await observer.publish(
                        "hoodini/hood1/set/light", b'{"brightness":5}', qos=1
                    )
                    assert await hood.packet() == COMMAND
                    await hood.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
        finally:
            if hood is not None:
                await hood.close()
            await server.stop()

    asyncio.run(scenario())


def test_replacement_cannot_ack_old_command(broker, tmp_path):
    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        old = new = None
        try:
            await server.start()
            old = await ready_hood(server)
            await old.publish("status", FIRST)
            async with mqtt(broker.url) as observer, ClientSession() as http:
                await observer.subscribe("hoodini/hood1/state")
                await message(
                    observer.messages,
                    "hoodini/hood1/state",
                    b'{"cooktop_light_intensity":50,"rssi":-40}',
                )
                await observer.publish(
                    "hoodini/hood1/set/light", b'{"brightness":5}', qos=1
                )
                assert await old.packet() == COMMAND
                new = await ready_hood(server)
                await new.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
                await new.publish("status", FIRST)
                await until(lambda: reported(http, server, 50))
                assert (await device(http, server))["caps"]["96"] == 50
                await observer.publish(
                    "hoodini/hood1/set/light", b'{"brightness":5}', qos=1
                )
                assert (await new.packet()).startswith(b"\x30\x2a")
        finally:
            if old is not None:
                await old.close()
            if new is not None:
                await new.close()
            await server.stop()

    asyncio.run(scenario())


def test_pending_deletion_precedes_restart_rediscovery(broker, tmp_path):
    from hoodini.storage import AtomicStore

    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        hood = None
        try:
            await server.start()
            hood = await ready_hood(server)
            await hood.publish("status", FIRST)
            async with mqtt(broker.url) as observer:
                await observer.subscribe("homeassistant/device/hoodini_hood1/config")
                await message(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
            await server.stop()
            await hood.close()
            hood = None
            AtomicStore(server.data).update(
                lambda state: state.pending_removals.add("hood1")
            )
            async with mqtt(broker.url) as observer:
                await observer.subscribe("homeassistant/device/hoodini_hood1/config")
                assert (
                    await message(
                        observer.messages, "homeassistant/device/hoodini_hood1/config"
                    )
                ).payload
                await server.start()
                assert (
                    await message(
                        observer.messages, "homeassistant/device/hoodini_hood1/config"
                    )
                ).payload == b""
                assert json.loads(
                    (
                        await message(
                            observer.messages,
                            "homeassistant/device/hoodini_hood1/config",
                        )
                    ).payload
                )["cmps"]["light"]
        finally:
            if hood is not None:
                await hood.close()
            await server.stop()

    asyncio.run(scenario())


def test_short_disconnect_and_reconnect_stays_online(broker, tmp_path):
    async def scenario():
        server = ServerProcess(tmp_path, broker.url)
        hood = replacement = None
        try:
            await server.start()
            hood = await ready_hood(server)
            await hood.publish("status", FIRST)
            async with ClientSession() as http:
                await until(lambda: reported(http, server, 50))
                await until(lambda: online(http, server))
                await hood.close()
                hood = None
                assert (await device(http, server))["online"]
                replacement = await ready_hood(server)
                assert (await device(http, server))["online"]
                await replacement.publish("status", FIRST)
                await until(lambda: online(http, server))
        finally:
            if hood is not None:
                await hood.close()
            if replacement is not None:
                await replacement.close()
            await server.stop()

    asyncio.run(scenario())
