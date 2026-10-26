"""Isolated broker wire tests for the HA bridge."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import aiomqtt
import pytest
from hoodini.config import MqttSettings
from hoodini.devices.models import CommandResult, DeviceEvent, DeviceView
from hoodini.ha_bridge.client import HaBridge
from hoodini.protocol.profiles import resolve_profile
from hoodini.storage import AtomicStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest_plugins = ["support.broker"]

pytestmark = pytest.mark.mqtt


class Devices:
    def __init__(self, views):
        self.views = {view.cuid: view for view in views}
        self.listeners = []
        self.commands = []
        self.result = CommandResult("ok")

    def list(self):
        return list(self.views.values())

    def get(self, cuid):
        return self.views.get(cuid)

    def subscribe(self, callback):
        self.listeners.append(callback)
        return Subscription(self, callback)

    def emit(self, kind, cuid):
        for callback in self.listeners[:]:
            callback(DeviceEvent(kind, cuid, self.get(cuid)))

    async def set_caps(self, cuid, caps):
        self.commands.append((cuid, caps))
        return self.result


class Subscription:
    def __init__(self, owner, callback):
        self.owner, self.callback = owner, callback
        self.closed = asyncio.get_running_loop().create_future()

    def __call__(self):
        self.owner.listeners.remove(self.callback)
        self.closed.set_result("unsubscribed")

    async def wait_closed(self):
        return await asyncio.shield(self.closed)


def view(cuid="hood1", caps=None):
    caps = caps if caps is not None else {96: 50}
    return DeviceView(
        cuid,
        {},
        resolve_profile({}, None),
        None,
        {},
        caps,
        set(caps),
        -40,
        1,
        50,
        1,
        "ready",
        True,
        "session",
    )


def client(url):
    parsed = urlsplit(url)
    return aiomqtt.Client(
        parsed.hostname, parsed.port, username=parsed.username, password=parsed.password
    )


async def receive(messages, topic, payload=None, timeout=5):
    async def matching():
        async for message in messages:
            if str(message.topic) == topic and (
                payload is None or message.payload == payload
            ):
                return message

    return await asyncio.wait_for(matching(), timeout)


def test_birth_order_and_literal_retained_state(broker, tmp_path):
    async def scenario():
        devices = Devices([view()])
        bridge = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
        async with client(broker.url) as observer:
            await observer.subscribe("#")
            await bridge.start()
            try:
                observed = []

                async def collect():
                    async for message in observer.messages:
                        observed.append((str(message.topic), message.payload))
                        if (str(message.topic), message.payload) == (
                            "hoodini/bridge/state",
                            b"online",
                        ):
                            return

                await asyncio.wait_for(collect(), 5)
                assert ("hoodini/hood1/availability", b"offline") in observed
                assert observed.index(
                    ("hoodini/hood1/availability", b"offline")
                ) < observed.index(("hoodini/bridge/state", b"online"))
                config = await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                assert (
                    json.loads(config.payload)["cmps"]["light"]["unique_id"]
                    == "hoodini_hood1_light"
                )
                state = await receive(observer.messages, "hoodini/hood1/state")
                assert state.payload == b'{"cooktop_light_intensity":50,"rssi":-40}'
                assert (
                    await receive(observer.messages, "hoodini/hood1/availability")
                ).payload == b"online"
            finally:
                await bridge.stop()

    asyncio.run(scenario())


def test_forget_clears_retained_config(broker, tmp_path):
    async def scenario():
        devices = Devices([view()])
        bridge = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
        async with client(broker.url) as observer:
            await observer.subscribe("homeassistant/device/hoodini_hood1/config")
            await bridge.start()
            try:
                await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                devices.views.pop("hood1")
                devices.emit("forgotten", "hood1")
                cleared = await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config", b""
                )
                assert cleared.payload == b""
                async with client(broker.url) as fresh:
                    await fresh.subscribe("homeassistant/device/hoodini_hood1/config")
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(
                            receive(
                                fresh.messages,
                                "homeassistant/device/hoodini_hood1/config",
                            ),
                            0.2,
                        )
            finally:
                await bridge.stop()

    asyncio.run(scenario())


def test_commands_ignore_retained_and_invalid_topics(broker, tmp_path):
    async def scenario():
        devices = Devices([view()])
        async with client(broker.url) as sender:
            await sender.publish(
                "hoodini/hood1/set/light", b'{"brightness":1}', qos=1, retain=True
            )
            bridge = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
            await bridge.start()
            try:
                await asyncio.sleep(0.2)
                assert devices.commands == []
                await sender.publish(
                    "hoodini/hood1/set/light", b'{"brightness":5}', qos=1
                )
                for _ in range(100):
                    if devices.commands:
                        break
                    await asyncio.sleep(0.01)
                assert devices.commands == [("hood1", {96: 5})]
                await sender.publish(
                    "hoodini/other/set/light", b'{"brightness":6}', qos=1
                )
                await sender.publish(
                    "hoodini/hood1/set/other", b'{"brightness":6}', qos=1
                )
                await asyncio.sleep(0.1)
                assert devices.commands == [("hood1", {96: 5})]
            finally:
                await bridge.stop()
                await sender.publish("hoodini/hood1/set/light", b"", qos=1, retain=True)

    asyncio.run(scenario())


def test_ha_online_republishes_and_profile_change_removes_component(broker, tmp_path):
    async def scenario():
        devices = Devices([view(caps={96: 50, 129: 20, 131: 2})])
        bridge = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
        async with client(broker.url) as observer:
            await observer.subscribe("homeassistant/device/hoodini_hood1/config")
            await bridge.start()
            try:
                first = await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                assert "charcoal_filter" in json.loads(first.payload)["cmps"]
                devices.views["hood1"] = view(caps={96: 50, 129: 20, 131: 0})
                devices.emit("state", "hood1")
                removed = await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                assert json.loads(removed.payload)["cmps"]["charcoal_filter"] == {
                    "p": "sensor"
                }
                clean = await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                assert "charcoal_filter" not in json.loads(clean.payload)["cmps"]
                await observer.publish("homeassistant/status", b"online", qos=1)
                await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                assert bridge.status.ha_state == "online"
                assert bridge.status.ha_seen_at is not None
            finally:
                await bridge.stop()

    asyncio.run(scenario())


def test_forget_while_down_persists_across_bridge_restart(broker, tmp_path):
    async def scenario():
        devices = Devices([view()])
        store = AtomicStore(tmp_path)
        bridge = HaBridge(
            MqttSettings(broker.url),
            devices,
            store,
            sleep=lambda _: asyncio.sleep(0.01),
            jitter=lambda: 0,
        )
        async with client(broker.url) as observer:
            await observer.subscribe("homeassistant/device/hoodini_hood1/config")
            await bridge.start()
            await receive(
                observer.messages, "homeassistant/device/hoodini_hood1/config"
            )
            broker.stop()
            devices.views.pop("hood1")
            devices.emit("forgotten", "hood1")
            assert "hood1" in store.load().pending_removals
            await bridge.stop()
        await broker.restart()
        restarted = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
        async with client(broker.url) as observer:
            await observer.subscribe("homeassistant/device/hoodini_hood1/config")
            await restarted.start()
            try:
                await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config", b""
                )
                assert not AtomicStore(tmp_path).load().pending_removals
            finally:
                await restarted.stop()

    asyncio.run(scenario())


def test_broker_restart_republishes_current_report(broker, tmp_path):
    async def scenario():
        devices = Devices([view()])
        bridge = HaBridge(
            MqttSettings(broker.url),
            devices,
            AtomicStore(tmp_path),
            sleep=lambda _: asyncio.sleep(0.05),
            jitter=lambda: 0,
        )
        async with client(broker.url) as observer:
            await observer.subscribe("hoodini/bridge/state")
            await bridge.start()
            await receive(observer.messages, "hoodini/bridge/state", b"online")
        # Change the view silently (no device event) before the restart: the recreated
        # broker has no persistence, so the only way it can receive 25 is the bridge
        # republishing the current view on reconnect. Changing it after the restart
        # raced the reconnect, which could republish the old 50 first.
        devices.views["hood1"] = view(caps={96: 25})
        await broker.restart()
        async with client(broker.url) as observer:
            await observer.subscribe("hoodini/hood1/state")
            try:
                # A reconnect attempt made while the broker is still restarting can wait
                # out the client's 10 s connect timeout under load, so allow longer.
                state = await receive(
                    observer.messages, "hoodini/hood1/state", timeout=30
                )
                assert state.payload == b'{"cooktop_light_intensity":25,"rssi":-40}'
            finally:
                await bridge.stop()

    asyncio.run(scenario())


def test_mqtt_light_command_roundtrip_with_real_gateway(broker, tmp_path, monkeypatch):
    from dataclasses import replace

    from hoodini import app
    from hoodini.config import load_settings
    from integration.test_runtime import loopback
    from support.hood import Hood

    loopback(monkeypatch)

    async def scenario():
        runtime = app.Runtime(
            replace(
                load_settings(None), data_dir=tmp_path, mqtt=MqttSettings(broker.url)
            )
        )
        await runtime.start()
        hood = None
        try:
            hood = await Hood.open(runtime.gateway.port)
            assert await hood.connect(keepalive=0) == b"\x20\x02\x00\x00"
            assert await hood.subscribe("op/mode") == b"\x90\x03\x00\x01\x00"
            await hood.publish("connect", b"\x01\x01\x02\x00\x03\x07")
            await hood.packet()
            await hood.packet()
            await hood.publish(
                "status", b"\x00\x00\x00\x01\xd8\x75\x30\x00\x60\x32\x00\x00\x10"
            )
            for _ in range(100):
                if (
                    runtime.devices.get("hood1")
                    and runtime.devices.get("hood1").session_state == "ready"
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("synthetic hood never became ready")
            async with client(broker.url) as observer:
                await observer.subscribe("hoodini/hood1/state")
                await observer.publish(
                    "hoodini/hood1/set/light", b'{"brightness":5}', qos=1
                )
                assert await asyncio.wait_for(hood.packet(), 3) == (
                    b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                    b"\x00\x2a\x8d\x00\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x09"
                )
                assert runtime.devices.get("hood1").caps[96] == 50
                await hood.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
                await hood.publish(
                    "status", b"\x00\x00\x00\x02\xd8\x75\x30\x00\x60\x05\x00\x00\xe4"
                )
                message = await receive(
                    observer.messages,
                    "hoodini/hood1/state",
                    b'{"cooktop_light_intensity":5,"rssi":-40}',
                )
                assert message.payload == b'{"cooktop_light_intensity":5,"rssi":-40}'
        finally:
            if hood is not None:
                await hood.close()
            await runtime.stop()

    asyncio.run(scenario())


def test_graceful_stop_retains_offline_bridge_without_changing_device(broker, tmp_path):
    async def scenario():
        bridge = HaBridge(
            MqttSettings(broker.url), Devices([view()]), AtomicStore(tmp_path)
        )
        async with client(broker.url) as observer:
            await observer.subscribe("hoodini/bridge/state")
            await bridge.start()
            await receive(observer.messages, "hoodini/bridge/state", b"online")
            await bridge.stop()
            assert bridge.status.state == "stopped"
            assert bridge.status.reason is bridge.status.retry_at is None
        async with client(broker.url) as fresh:
            await fresh.subscribe("hoodini/bridge/state")
            assert (
                await receive(fresh.messages, "hoodini/bridge/state")
            ).payload == b"offline"
            await fresh.subscribe("hoodini/hood1/availability")
            assert (
                await receive(fresh.messages, "hoodini/hood1/availability")
            ).payload == b"online"

    asyncio.run(scenario())


def test_state_event_only_publishes_dirty_device_and_growth_reconciles(
    broker, tmp_path
):
    async def scenario():
        devices = Devices([view(), view("hood2")])
        bridge = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
        async with (
            client(broker.url) as observer,
            client(broker.url) as state_watcher,
            client(broker.url) as other_watcher,
        ):
            await observer.subscribe("homeassistant/device/hoodini_hood1/config")
            await state_watcher.subscribe("hoodini/hood1/state")
            await other_watcher.subscribe("hoodini/hood2/state")
            await bridge.start()
            try:
                await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                await receive(
                    state_watcher.messages,
                    "hoodini/hood1/state",
                    b'{"cooktop_light_intensity":50,"rssi":-40}',
                )
                await receive(
                    other_watcher.messages,
                    "hoodini/hood2/state",
                    b'{"cooktop_light_intensity":50,"rssi":-40}',
                )
                devices.views["hood1"] = view(caps={96: 25})
                devices.emit("state", "hood1")
                await receive(
                    state_watcher.messages,
                    "hoodini/hood1/state",
                    b'{"cooktop_light_intensity":25,"rssi":-40}',
                )
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(
                        receive(
                            observer.messages,
                            "homeassistant/device/hoodini_hood1/config",
                        ),
                        0.3,
                    )
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(
                        receive(other_watcher.messages, "hoodini/hood2/state"), 0.3
                    )
                devices.views["hood1"] = view(caps={96: 25, 128: 70})
                devices.emit("state", "hood1")
                config = await receive(
                    observer.messages, "homeassistant/device/hoodini_hood1/config"
                )
                assert "grease_filter" in json.loads(config.payload)["cmps"]
                async with asyncio.timeout(5):
                    while bridge.store.load().discovery_components.get(
                        "hood1", set()
                    ) < {
                        "light",
                        "grease_filter",
                    }:
                        await asyncio.sleep(0.01)
            finally:
                await bridge.stop()

    asyncio.run(scenario())


def test_reconnect_delay_resets_after_online(tmp_path):
    async def scenario():
        delays = []
        enough = asyncio.Event()
        devices = Devices([])
        bridge = HaBridge(
            MqttSettings("mqtt://127.0.0.1:1"),
            devices,
            AtomicStore(tmp_path),
            jitter=lambda: 0.25,
        )
        calls = 0

        async def connection():
            nonlocal calls
            calls += 1
            if calls == 3:
                bridge._online = True
                bridge._transition("connected")
                return
            raise OSError("synthetic unavailable")

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 4:
                enough.set()
                await asyncio.Event().wait()
            await asyncio.sleep(0)

        bridge._connection = connection
        bridge._sleep = sleep
        await bridge.start()
        try:
            await asyncio.wait_for(enough.wait(), 2)
            assert delays == [1.25, 2.25, 1.25, 2.25]
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_mqtts_url_implies_tls_and_safe_default_port(tmp_path, monkeypatch):
    import ssl

    captured = {}

    class ProbeClient:
        def __init__(self, hostname, port, **kwargs):
            captured.update(
                hostname=hostname, port=port, tls_context=kwargs["tls_context"]
            )

        async def __aenter__(self):
            raise OSError("synthetic connect failure")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("hoodini.ha_bridge.client.aiomqtt.Client", ProbeClient)

    async def scenario():
        bridge = HaBridge(
            MqttSettings("mqtts://localhost"), Devices([]), AtomicStore(tmp_path)
        )
        with pytest.raises(OSError, match="synthetic connect failure"):
            await bridge._connection()

    asyncio.run(scenario())
    assert captured["hostname"] == "localhost"
    assert captured["port"] == 8883
    assert isinstance(captured["tls_context"], ssl.SSLContext)


def test_abrupt_transport_loss_publishes_will_before_rebirth(broker, tmp_path):
    import socket

    async def scenario():
        paused = asyncio.Event()
        reconnect = asyncio.Event()

        async def sleep(_delay):
            paused.set()
            await reconnect.wait()

        devices = Devices([view()])
        bridge = HaBridge(
            MqttSettings(broker.url),
            devices,
            AtomicStore(tmp_path),
            sleep=sleep,
            jitter=lambda: 0,
        )
        async with client(broker.url) as observer:
            await observer.subscribe("hoodini/bridge/state")
            await bridge.start()
            try:
                await receive(observer.messages, "hoodini/bridge/state", b"online")
                transport = bridge._client._client.socket()
                assert transport is not None
                transport.shutdown(
                    socket.SHUT_RDWR
                )  # No MQTT DISCONNECT: broker sends will.
                await receive(observer.messages, "hoodini/bridge/state", b"offline")
                await asyncio.wait_for(paused.wait(), 5)
                async with client(broker.url) as retained:
                    await retained.subscribe("hoodini/bridge/state")
                    assert (
                        await receive(retained.messages, "hoodini/bridge/state")
                    ).payload == b"offline"
                    await retained.subscribe("hoodini/hood1/availability")
                    assert (
                        await receive(retained.messages, "hoodini/hood1/availability")
                    ).payload == b"online"
                async with client(broker.url) as rebirth:
                    await rebirth.subscribe("#")
                    reconnect.set()
                    seen = []
                    async with asyncio.timeout(5):
                        async for message in rebirth.messages:
                            seen.append((str(message.topic), message.payload))
                            if (str(message.topic), message.payload) == (
                                "hoodini/bridge/state",
                                b"online",
                            ):
                                break
                    assert ("hoodini/hood1/availability", b"offline") in seen
                    assert seen.index(
                        ("hoodini/hood1/availability", b"offline")
                    ) < seen.index(("hoodini/bridge/state", b"online"))
                    assert devices.get("hood1").online
                    await receive(
                        rebirth.messages, "hoodini/hood1/availability", b"online"
                    )
            finally:
                reconnect.set()
                await bridge.stop()

    asyncio.run(scenario())


def test_mqtt_fan_command_roundtrip_with_real_gateway(broker, tmp_path, monkeypatch):
    from dataclasses import replace

    from hoodini import app
    from hoodini.config import load_settings
    from integration.test_runtime import loopback
    from support.hood import Hood

    loopback(monkeypatch)

    async def scenario():
        runtime = app.Runtime(
            replace(
                load_settings(None), data_dir=tmp_path, mqtt=MqttSettings(broker.url)
            )
        )
        await runtime.start()
        hood = None
        try:
            hood = await Hood.open(runtime.gateway.port)
            assert await hood.connect(keepalive=0) == b"\x20\x02\x00\x00"
            assert await hood.subscribe("op/mode") == b"\x90\x03\x00\x01\x00"
            await hood.publish("connect", b"\x01\x01\x02\x00\x03\x07")
            await hood.packet()  # connected
            await hood.packet()  # time
            await hood.publish(
                "status",
                b"\x00\x00\x00\x01\xd8\x75\x30\x00\x6e\x02\x00\x40\x01\x00\x00\x2f",
            )
            for _ in range(100):
                current = runtime.devices.get("hood1")
                if (
                    current is not None
                    and current.session_state == "ready"
                    and current.caps.get(110) == 2
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("synthetic fan never became ready")
            async with client(broker.url) as observer:
                await observer.subscribe("hoodini/hood1/state")
                await observer.publish("hoodini/hood1/set/fan", b'{"speed":3}', qos=1)
                assert await asyncio.wait_for(hood.packet(), 3) == (
                    b"\x30\x2d\x00\x17v1/device/hood1/op/mode"
                    b"\x00\x2a\x8d\x00\x00\x00\x75\x30\x00\x6e\x03\x00\x40\x01\x00\x47\x01\x00\x00\x56"
                )
                assert runtime.devices.get("hood1").caps[110] == 2  # no optimism
                await hood.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
                await hood.publish(
                    "status",
                    b"\x00\x00\x00\x02\xd8\x75\x30\x00\x6e\x03\x00\x40\x01\x00\x00\x31",
                )
                state = await receive(
                    observer.messages,
                    "hoodini/hood1/state",
                    b'{"fan_mode1":1,"fan_speed":3,"rssi":-40}',
                )
                assert state.payload == b'{"fan_mode1":1,"fan_speed":3,"rssi":-40}'
        finally:
            if hood is not None:
                await hood.close()
            await runtime.stop()

    asyncio.run(scenario())


def test_pending_removal_precedes_bridge_online(broker, tmp_path):
    async def scenario():
        store = AtomicStore(tmp_path)
        store.update(lambda state: state.pending_removals.add("forgotten"))
        bridge = HaBridge(MqttSettings(broker.url), Devices([]), store)
        async with client(broker.url) as observer:
            await observer.subscribe("#")
            await bridge.start()
            try:
                seen = []
                async with asyncio.timeout(5):
                    async for message in observer.messages:
                        seen.append((str(message.topic), message.payload))
                        if seen[-1] == ("hoodini/bridge/state", b"online"):
                            break
                for topic in (
                    "homeassistant/device/hoodini_forgotten/config",
                    "hoodini/forgotten/state",
                    "hoodini/forgotten/availability",
                ):
                    assert (topic, b"") in seen
                    assert seen.index((topic, b"")) < len(seen) - 1
            finally:
                await bridge.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result",
    [
        CommandResult("rejected", 1, 2),
        CommandResult("offline"),
        CommandResult("timeout", 1),
    ],
)
def test_command_non_ok_result_logs_sanitized_warning(broker, tmp_path, caplog, result):
    async def scenario():
        devices = Devices([view()])
        devices.result = result
        bridge = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
        await bridge.start()
        try:
            async with client(broker.url) as sender:
                async with asyncio.timeout(5):
                    while not bridge._online:
                        await asyncio.sleep(0.01)
                await sender.publish("hoodini/hood1/set/light", b'{"state":"ON"}')
                async with asyncio.timeout(5):
                    while not devices.commands:
                        await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
            assert f"HA bridge command {result.status}" in caplog.text
            assert "synthetic-secret" not in caplog.text
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_rapid_commands_coalesce_to_latest_per_entity(broker, tmp_path):
    """A slow hood must not replay every intermediate HA value."""

    async def scenario():
        devices = Devices([view()])
        release = asyncio.Event()
        original = devices.set_caps

        async def slow_set_caps(cuid, caps):
            result = await original(cuid, caps)
            await release.wait()
            return result

        devices.set_caps = slow_set_caps
        bridge = HaBridge(MqttSettings(broker.url), devices, AtomicStore(tmp_path))
        await bridge.start()
        try:
            async with client(broker.url) as sender:
                async with asyncio.timeout(5):
                    while not bridge._online:
                        await asyncio.sleep(0.01)
                for brightness in range(10, 60, 5):
                    await sender.publish(
                        "hoodini/hood1/set/light",
                        json.dumps({"brightness": brightness}).encode(),
                    )
                await sender.publish("hoodini/hood1/set/fan", b'{"speed":2}')
                async with asyncio.timeout(5):
                    while not devices.commands:
                        await asyncio.sleep(0.01)
                await asyncio.sleep(0.3)  # let every message reach the bridge
                release.set()
                async with asyncio.timeout(5):
                    while len(devices.commands) < 3:
                        await asyncio.sleep(0.01)
                await asyncio.sleep(0.3)
            assert devices.commands == [
                ("hood1", {96: 10}),
                ("hood1", {96: 55}),
                ("hood1", {110: 2, 64: 1}),
            ]
        finally:
            await bridge.stop()

    asyncio.run(scenario())


async def wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_status_tracks_each_state_start_and_retry_deadline(tmp_path):
    async def scenario():
        now = 1000
        attempts, retries = [], []
        blocked = asyncio.Event()
        bridge = HaBridge(
            MqttSettings("mqtt://broker"),
            Devices([]),
            AtomicStore(tmp_path),
            clock=lambda: now,
            jitter=lambda: 0,
        )

        async def connection():
            nonlocal now
            attempts.append(bridge.status)
            if len(attempts) == 3:
                blocked.set()
                await asyncio.Event().wait()
            now = 1010 if len(attempts) == 1 else 1021
            raise OSError("connection refused SYNTHETICSECRET")

        async def sleep(delay):
            nonlocal now
            retries.append(bridge.status)
            now += delay
            await asyncio.sleep(0)

        bridge._connection, bridge._sleep = connection, sleep
        await bridge.start()
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            assert [(s.state, s.since, s.retry_at) for s in attempts] == [
                ("connecting", 1000, None),
                ("connecting", 1011, None),
                ("connecting", 1023, None),
            ]
            assert [s.reason for s in attempts] == [
                None,
                "connection refused",
                "connection refused",
            ]
            assert [(s.state, s.since, s.retry_at) for s in retries] == [
                ("retrying", 1010, 1011),
                ("retrying", 1021, 1023),
            ]
            assert all(s.reason == "connection refused" for s in retries)
            assert attempts[0].since == 1000  # Previously read snapshots never mutate.
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_status_recovery_clears_reason_and_resets_backoff(tmp_path):
    async def scenario():
        now = 1000
        delays, recovered = [], []
        enough = asyncio.Event()
        bridge = HaBridge(
            MqttSettings("mqtt://broker"),
            Devices([]),
            AtomicStore(tmp_path),
            clock=lambda: now,
            jitter=lambda: 0,
        )
        bridge._status = replace(bridge.status, ha_state="online", ha_seen_at=900)
        calls = 0

        async def connection():
            nonlocal calls, now
            calls += 1
            now += 10
            if calls == 3:
                bridge._online = True
                bridge._transition("connected")
                recovered.append(bridge.status)
                now += 5
            raise OSError("refused")

        async def sleep(delay):
            nonlocal now
            delays.append(delay)
            now += delay
            if len(delays) == 4:
                enough.set()
                await asyncio.Event().wait()

        bridge._connection, bridge._sleep = connection, sleep
        await bridge.start()
        try:
            await asyncio.wait_for(enough.wait(), 2)
            assert delays == [1, 2, 1, 2]
            assert recovered[0].since == 1033
            assert recovered[0].reason is recovered[0].retry_at is None
            assert (bridge.status.ha_state, bridge.status.ha_seen_at) == ("online", 900)
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_status_retry_deadline_matches_jittered_sleep_and_cap(tmp_path):
    async def scenario():
        now = 0
        delays = []
        enough = asyncio.Event()
        bridge = HaBridge(
            MqttSettings("mqtt://broker"),
            Devices([]),
            AtomicStore(tmp_path),
            clock=lambda: now,
            jitter=lambda: 0.25,
        )

        async def connection():
            raise OSError("refused")

        async def sleep(delay):
            nonlocal now
            assert bridge.status.state == "retrying"
            assert bridge.status.retry_at == now + delay
            delays.append(delay)
            now += delay
            if len(delays) == 9:
                enough.set()
                await asyncio.Event().wait()

        bridge._connection, bridge._sleep = connection, sleep
        await bridge.start()
        try:
            await asyncio.wait_for(enough.wait(), 2)
            assert delays == [1.25, 2.25, 4.25, 8.25, 16.25, 32.25, 60.25, 60.25, 60.25]
        finally:
            await bridge.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["changes", "both", "subscription", "normal", "cancelled"]
)
def test_status_handles_change_failure_and_subscription_replacement(tmp_path, failure):
    async def scenario():
        paused = asyncio.Event()
        ready = asyncio.Event()
        devices = Devices([])
        bridge = HaBridge(
            MqttSettings("mqtt://broker"),
            devices,
            AtomicStore(tmp_path),
            jitter=lambda: 0,
        )
        bridge._status = replace(bridge.status, ha_state="offline", ha_seen_at=1)

        async def connection():
            bridge._online = True
            bridge._transition("connected")
            ready.set()
            if failure == "both":
                raise OSError("connection refused")
            if failure == "normal":
                return
            if failure == "cancelled":
                raise asyncio.CancelledError
            if failure == "subscription":
                bridge._subscription()
            await asyncio.Event().wait()

        async def changes():
            await ready.wait()
            if failure in ("changes", "both"):
                raise OSError("network unreachable")
            await asyncio.Event().wait()

        async def sleep(_):
            paused.set()
            await asyncio.Event().wait()

        bridge._connection, bridge._changes, bridge._sleep = connection, changes, sleep
        await bridge.start()
        original_subscription = bridge._subscription
        try:
            await asyncio.wait_for(paused.wait(), 2)
            expected = {
                "changes": "network unreachable",
                "both": "connection refused",
            }.get(failure)
            assert (
                bridge.status.state == "retrying" and bridge.status.reason == expected
            )
            assert bridge.status.retry_at is not None
            assert bridge.status.ha_state == "offline"
            assert len(devices.listeners) == 1
            if failure == "subscription":
                assert bridge._subscription is not original_subscription
        finally:
            await bridge.stop()
        assert not devices.listeners

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["connecting", "retrying"])
def test_status_stop_during_connect_or_retry_does_not_retry(tmp_path, phase):
    async def scenario():
        now = 1000
        reached = asyncio.Event()
        attempts = []
        devices = Devices([])
        bridge = HaBridge(
            MqttSettings("mqtt://broker"),
            devices,
            AtomicStore(tmp_path),
            clock=lambda: now,
        )
        bridge._status = replace(bridge.status, ha_state="online", ha_seen_at=900)
        await bridge.stop()
        assert bridge.status.state == "stopped" and bridge._task is None

        async def connection():
            attempts.append(bridge.status)
            if phase == "retrying":
                raise OSError("refused")
            reached.set()
            await asyncio.Event().wait()

        async def sleep(_):
            reached.set()
            await asyncio.Event().wait()

        bridge._connection, bridge._sleep = connection, sleep
        await bridge.start()
        try:
            await asyncio.wait_for(reached.wait(), 2)
        finally:
            await bridge.stop()
        stopped = bridge.status
        await bridge.stop()
        assert bridge.status == stopped and stopped.state == "stopped"
        assert stopped.reason is stopped.retry_at is None
        assert bridge._task is bridge._subscription is None and not devices.listeners
        assert len(attempts) == 1
        now = 2000
        reached.clear()
        await bridge.start()
        await bridge.start()
        try:
            assert bridge.status.state == "connecting" and bridge.status.since == 2000
            await asyncio.wait_for(reached.wait(), 2)
            assert len(devices.listeners) == 1 and len(attempts) == 2
            assert (bridge.status.ha_state, bridge.status.ha_seen_at) == ("online", 900)
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_connection_failure_status_is_safe_before_cleanup_finishes(
    tmp_path, monkeypatch
):
    async def scenario():
        cleanup, release, retry = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class ProbeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def subscribe(self, *args, **kwargs):
                pass

            async def publish(self, *args, **kwargs):
                pass

        monkeypatch.setattr("hoodini.ha_bridge.client.aiomqtt.Client", ProbeClient)
        bridge = HaBridge(
            MqttSettings("mqtt://broker"), Devices([]), AtomicStore(tmp_path)
        )

        async def messages(_):
            assert bridge.status.state == "connected"
            raise OSError("refused SYNTHETICSECRET")

        async def cancel_senders():
            cleanup.set()
            await release.wait()

        async def sleep(_):
            retry.set()
            await asyncio.Event().wait()

        bridge._messages, bridge._cancel_senders, bridge._sleep = (
            messages,
            cancel_senders,
            sleep,
        )
        await bridge.start()
        try:
            await asyncio.wait_for(cleanup.wait(), 2)
            status = bridge.status
            assert status.state == "retrying" and status.retry_at is None
            release.set()
            await asyncio.wait_for(retry.wait(), 2)
            assert (
                bridge.status.reason == "connection refused"
                and bridge.status.retry_at is not None
            )
            assert bridge.status.since == status.since
        finally:
            release.set()
            await bridge.stop()

    asyncio.run(scenario())


def test_status_reports_connection_and_ha_announcements(broker, tmp_path):
    async def scenario():
        now = 1000
        bridge = HaBridge(
            MqttSettings(broker.url),
            Devices([]),
            AtomicStore(tmp_path),
            clock=lambda: now,
        )
        await bridge.start()
        try:
            await wait_until(lambda: bridge.status.state == "connected")
            status = bridge.status
            target = urlsplit(broker.url)
            assert status.endpoint == f"{target.hostname}:{target.port}"
            assert (
                status.since == 1000
                and status.reason is status.retry_at is status.ha_state is None
            )
            async with client(broker.url) as observer:
                for payload in (b"online", b"offline"):
                    now += 1
                    await observer.publish("homeassistant/status", payload, qos=1)
                    await wait_until(
                        lambda payload=payload: (
                            bridge.status.ha_state == payload.decode()
                        )
                    )
                    assert bridge.status.ha_seen_at == now
                    assert (
                        bridge.status.state == "connected"
                        and bridge.status.since == status.since
                    )
                accepted = bridge.status
                for payload in (b"ONLINE", b"", b"arbitrary"):
                    await observer.publish("homeassistant/status", payload, qos=1)
                # A valid later announcement is an ordered processing barrier.
                now += 1
                await observer.publish("homeassistant/status", b"online", qos=1)
                await wait_until(lambda: bridge.status.ha_seen_at == now)
                assert accepted.ha_state == "offline" and accepted.ha_seen_at == 1002
                assert (
                    bridge.status.ha_state == "online" and bridge.status.since == 1000
                )
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_status_captures_retained_ha_announcement_on_subscribe(broker, tmp_path):
    async def scenario():
        bridge = HaBridge(
            MqttSettings(broker.url),
            Devices([]),
            AtomicStore(tmp_path),
            clock=lambda: 1234,
        )
        async with client(broker.url) as observer:
            await observer.publish(
                "homeassistant/status", b"offline", qos=1, retain=True
            )
            await bridge.start()
            try:
                await wait_until(lambda: bridge.status.ha_state == "offline")
                assert bridge.status.ha_seen_at == 1234
            finally:
                await bridge.stop()
                await observer.publish("homeassistant/status", b"", qos=1, retain=True)

    asyncio.run(scenario())


def test_status_reports_broker_loss_then_recovery(broker, tmp_path):
    async def scenario():
        retry, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def sleep(_):
            captured.append(bridge.status)
            retry.set()
            await release.wait()

        bridge = HaBridge(
            MqttSettings(broker.url),
            Devices([]),
            AtomicStore(tmp_path),
            sleep=sleep,
            jitter=lambda: 0,
        )
        await bridge.start()
        try:
            await wait_until(lambda: bridge.status.state == "connected")
            async with client(broker.url) as observer:
                await observer.publish("homeassistant/status", b"online", qos=1)
                await wait_until(lambda: bridge.status.ha_state == "online")
            connected = bridge.status
            await broker.restart()
            await asyncio.wait_for(retry.wait(), 10)
            status = captured[0]
            assert (
                status.state == "retrying"
                and status.reason
                and status.retry_at is not None
            )
            assert status.since > connected.since
            assert (
                status.ha_state == "online"
                and status.ha_seen_at == connected.ha_seen_at
            )
            release.set()
            await wait_until(lambda: bridge.status.state == "connected", timeout=30)
            assert bridge.status.reason is bridge.status.retry_at is None
            assert bridge.status.since > status.since
        finally:
            release.set()
            await bridge.stop()

    asyncio.run(scenario())


def test_ha_observation_precedes_flush_and_ignores_unrecognized_payloads(tmp_path):
    from types import SimpleNamespace

    async def scenario():
        now = 1000
        bridge = HaBridge(
            MqttSettings("mqtt://broker"),
            Devices([]),
            AtomicStore(tmp_path),
            clock=lambda: now,
        )
        bridge._transition("connected")
        flushes = []

        async def flush():
            flushes.append(bridge.status)
            assert (
                bridge.status.ha_state == "online" and bridge.status.ha_seen_at == now
            )
            await asyncio.sleep(0)

        async def messages():
            nonlocal now
            for payload in (
                b"online",
                b"offline",
                b"ONLINE",
                b"",
                b"arbitrary",
                b"online",
                b"online",
            ):
                before = bridge.status
                now += 1
                yield SimpleNamespace(
                    topic="homeassistant/status", payload=payload, retain=True
                )
                if payload not in (b"online", b"offline"):
                    assert bridge.status is before
                else:
                    assert bridge.status.ha_state == payload.decode()
                    assert bridge.status.ha_seen_at == now
                    assert bridge.status.since == 1000

        bridge._flush = flush
        try:
            await asyncio.wait_for(
                bridge._messages(SimpleNamespace(messages=messages())), 2
            )
            assert [s.ha_seen_at for s in flushes] == [1001, 1006, 1007]
            assert bridge.status.ha_seen_at == 1007
        finally:
            await bridge.stop()

    asyncio.run(scenario())
