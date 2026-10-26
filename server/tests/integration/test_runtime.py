"""Controlled loopback runtime lifecycle with synthetic local state."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import ClientSession
from hoodini import app
from hoodini.config import MqttSettings, load_settings
from hoodini.devices.models import DeviceView
from hoodini.gateway.server import Gateway
from hoodini.ha_bridge.client import BrokerStatus
from hoodini.protocol.profiles import resolve_profile
from hoodini.storage import StoredDevice

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.hood import Hood


def loopback(monkeypatch):
    monkeypatch.setattr(app.ifaddr, "get_adapters", list)
    original_site = app.web.TCPSite

    def local_site(runner, host, port):
        assert (host, port) in {("0.0.0.0", 8765), ("127.0.0.1", 8099)}
        return original_site(runner, "127.0.0.1", 0)

    monkeypatch.setattr(app.web, "TCPSite", local_site)

    def build(bind, port, tls, trust, diagnostics, on_event):
        assert port == 8883
        return Gateway("127.0.0.1", 0, tls, trust, diagnostics, on_event)

    monkeypatch.setattr(app, "Gateway", build)


def test_repeated_start_stop_and_persisted_state(tmp_path, monkeypatch):
    loopback(monkeypatch)

    async def scenario():
        settings = replace(load_settings(None), data_dir=tmp_path)
        runtime = app.Runtime(settings)
        for cycle in range(2):
            await runtime.start()
            assert runtime.gateway.port != 8883
            assert runtime.devices is not None
            assert runtime.api is not None and runtime.bonjour is not None
            assert runtime.admin is not None and runtime.bridge is None
            if cycle == 0:
                peer = await Hood.open(runtime.gateway.port)
                try:
                    assert await peer.connect() == b"\x20\x02\x00\x00"
                    assert await peer.subscribe("connected") == b"\x90\x03\x00\x01\x00"
                    assert await peer.subscribe("time", 3) == b"\x90\x03\x00\x03\x00"
                    assert await peer.subscribe("sync", 4) == b"\x90\x03\x00\x04\x00"
                    await peer.publish(
                        "connect", b"\x01\x01\x02\x00\x03\x07", packet_id=3
                    )
                    assert await peer.packet() == b"\x40\x02\x00\x03"
                    assert runtime.devices.get("hood1") is not None
                    assert b"v1/device/hood1/connected" in await peer.packet()
                    assert b"v1/device/hood1/time" in await peer.packet()
                finally:
                    await peer.close()
            await runtime.stop()
        runtime.store.update(
            lambda snapshot: snapshot.devices.update(
                {"synthetic": StoredDevice("synthetic", {}, None, {}, {}, set(), None)}
            )
        )
        certificate = (tmp_path / "server.crt").read_bytes()
        token = runtime.token
        second = app.Runtime(settings)
        await second.start()
        try:
            assert second.token == token
            assert (tmp_path / "server.crt").read_bytes() == certificate
            assert second.devices.get("synthetic") is not None
        finally:
            await second.stop()

    asyncio.run(scenario())


def test_runtime_http_command_roundtrip_over_real_loopback_tls_gateway(
    tmp_path, monkeypatch
):
    loopback(monkeypatch)

    class FakeBonjour:
        async def start(self, addresses, port):
            assert addresses == [] and port == 8765

        async def stop(self):
            pass

    monkeypatch.setattr(app, "Bonjour", FakeBonjour)

    async def scenario():
        runtime = app.Runtime(replace(load_settings(None), data_dir=tmp_path))
        await runtime.start()
        hood = None
        try:
            site = next(iter(runtime.api.sites))
            port = site._server.sockets[0].getsockname()[1]
            url = f"http://127.0.0.1:{port}/api/v1/devices/hood1"
            hood = await Hood.open(runtime.gateway.port)
            assert await hood.connect(keepalive=0) == b"\x20\x02\x00\x00"
            assert await hood.subscribe("op/mode") == b"\x90\x03\x00\x01\x00"
            await hood.publish("connect", b"\x01\x01\x02\x00\x03\x07")
            await hood.packet()  # connected
            await hood.packet()  # time
            await hood.publish(
                "status", b"\x00\x00\x00\x01\xd8\x75\x30\x00\x60\x32\x00\x00\x10"
            )
            async with ClientSession() as client:
                for _ in range(100):
                    async with client.get(url) as response:
                        assert response.status == 200
                        before = await response.json()
                    if before["session_state"] == "ready" and before["caps"] == {
                        "96": 50
                    }:
                        break
                    await asyncio.sleep(0)
                else:
                    pytest.fail("telemetry did not establish readiness")
                command = asyncio.create_task(
                    client.post(
                        url + "/commands",
                        json={"caps": {"96": 5}},
                        headers={"Authorization": "Bearer " + runtime.token},
                    )
                )
                assert await asyncio.wait_for(hood.packet(), 2) == (
                    b"\x30\x2a\x00\x17v1/device/hood1/op/mode"
                    b"\x00\x2a\x8d\x00\x00\x00\x75\x30\x00\x60\x05\x00\x47\x01\x00\x00\x09"
                )
                async with client.get(url) as response:
                    assert (await response.json())["caps"] == {"96": 50}  # no optimism
                await hood.publish("ack", b"\x00\x2a\x8d\x00\x01\x00\x00\xb8")
                response = await asyncio.wait_for(command, 2)
                async with response:
                    assert response.status == 200
                    assert await response.json() == {
                        "status": "ok",
                        "seq": 0,
                        "ack_result": 1,
                    }
                async with client.get(url) as response:
                    assert (await response.json())["caps"] == {"96": 50}
        finally:
            if hood is not None:
                await hood.close()
            await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("configured", [False, True])
def test_bonjour_failure_rolls_back_http_gateway_and_service(
    tmp_path, monkeypatch, configured
):
    loopback(monkeypatch)
    owners = []
    bridges = []

    class ProbeBridge(app.HaBridge):
        def __init__(self, *args):
            super().__init__(*args)
            bridges.append(self)

        async def start(self):
            pytest.fail("Bridge must not start before Bonjour succeeds")

    monkeypatch.setattr(app, "HaBridge", ProbeBridge)

    class RefusingBonjour:
        def __init__(self):
            self.stopped = False
            owners.append(self)

        async def start(self, addresses, port):
            assert addresses == [] and port == 8765
            raise OSError("synthetic-private-bonjour")

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(app, "Bonjour", RefusingBonjour)

    async def scenario():
        runtime = app.Runtime(
            replace(
                load_settings(None),
                data_dir=tmp_path,
                mqtt=MqttSettings("mqtt://broker" if configured else None),
            )
        )
        with pytest.raises(app.RuntimeError) as error:
            await runtime.start()
        assert error.value.__context__ is None
        assert "synthetic-private-bonjour" not in repr(error.value)
        assert owners[0].stopped
        assert len(bridges) == int(configured)
        if configured:
            assert bridges[0].status.state == "stopped"
            assert bridges[0]._task is bridges[0]._subscription is None
        assert (
            runtime.gateway is None and runtime.devices is None and runtime.api is None
        )
        await runtime.stop()

    asyncio.run(scenario())


def test_startup_bind_failure_rolls_back_and_reports_fixed_port(tmp_path, monkeypatch):
    class RefusingGateway:
        def __init__(self, bind, port, tls, trust, diagnostics, on_event):
            assert port == 8883
            self.stopped = False
            instances.append(self)

        async def start(self):
            raise OSError("synthetic-secret-bind")

        async def stop(self):
            self.stopped = True

    instances = []
    monkeypatch.setattr(app, "Gateway", RefusingGateway)
    monkeypatch.setattr(app.ifaddr, "get_adapters", list)

    async def scenario():
        runtime = app.Runtime(replace(load_settings(None), data_dir=tmp_path))
        with pytest.raises(app.RuntimeError) as error:
            await runtime.start()
        assert all(
            word in str(error.value)
            for word in ("8883", "Mosquitto", "remap", "client")
        )
        assert "synthetic-secret-bind" not in repr(error.value)
        assert error.value.__context__ is None
        assert instances[0].stopped
        assert runtime.devices is None and runtime.gateway is None
        await runtime.stop()
        loopback(monkeypatch)
        await runtime.start()
        assert runtime.gateway.port != 8883
        await runtime.stop()

    asyncio.run(scenario())


def test_admin_binding_preflight_and_failed_admin_site_rollback(tmp_path, monkeypatch):
    loopback(monkeypatch)
    bridges = []

    class ProbeBridge(app.HaBridge):
        def __init__(self, *args):
            super().__init__(*args)
            bridges.append(self)

    monkeypatch.setattr(app, "HaBridge", ProbeBridge)
    original_site = app.web.TCPSite

    def failing_admin(runner, host, port):
        if (host, port) == ("127.0.0.1", 8099):
            raise OSError("synthetic-admin-bind")
        return original_site(runner, host, port)

    monkeypatch.setattr(app.web, "TCPSite", failing_admin)

    async def scenario():
        runtime = app.Runtime(
            replace(
                load_settings(None),
                data_dir=tmp_path,
                mqtt=MqttSettings("mqtt://127.0.0.1:1"),
            )
        )
        with pytest.raises(app.RuntimeError) as error:
            await runtime.start()
        assert error.value.__context__ is None
        assert "synthetic-admin-bind" not in str(error.value)
        assert (
            runtime.gateway is runtime.devices is runtime.api is runtime.admin is None
        )
        assert bridges[0].status.state == "stopped"
        assert bridges[0]._task is bridges[0]._subscription is None
        await runtime.stop()
        monkeypatch.setattr(app.web, "TCPSite", original_site)
        await runtime.start()
        try:
            assert runtime.admin is not None
            site = next(iter(runtime.admin.sites))
            assert site._server.sockets[0].getsockname()[0] == "127.0.0.1"
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_addon_without_trusted_context_rejects_before_network(tmp_path, monkeypatch):
    monkeypatch.setenv("HOODINI_MODE", "addon")

    async def scenario():
        runtime = app.Runtime(replace(load_settings(None), data_dir=tmp_path))
        with pytest.raises(app.RuntimeError, match="Trusted ingress context required"):
            await runtime.start()
        assert (
            runtime.gateway is runtime.devices is runtime.api is runtime.admin is None
        )
        assert not (tmp_path / "api_token").exists()

    asyncio.run(scenario())


def test_admin_combined_bind_parser():
    assert app._admin_binding("localhost:8099") == ("localhost", 8099)
    assert app._admin_binding("[::1]:8099") == ("::1", 8099)
    for value in ("::1:8099", "host:0", "host:65536", "host:bad", "", "host:8099:80"):
        with pytest.raises(app.RuntimeError, match="Invalid admin.bind"):
            app._admin_binding(value)


def test_runtime_stop_drains_admin_forget_before_service_stop(tmp_path, monkeypatch):
    loopback(monkeypatch)

    async def scenario():
        runtime = app.Runtime(replace(load_settings(None), data_dir=tmp_path))
        await runtime.start()
        sample = DeviceView(
            "hood1",
            {},
            resolve_profile({}, None),
            None,
            {},
            {},
            set(),
            None,
            None,
            None,
            None,
            "offline",
            False,
            None,
        )
        runtime.devices.list = lambda: [sample]
        runtime.devices.get = lambda cuid: sample if cuid == "hood1" else None
        entered = asyncio.Event()
        release = asyncio.Event()
        stopped = asyncio.Event()
        original_stop = runtime.devices.stop

        async def forget(cuid):
            entered.set()
            await release.wait()

        async def stop_service():
            assert release.is_set()
            stopped.set()
            await original_stop()

        runtime.devices.forget = forget
        runtime.devices.stop = stop_service
        site = next(iter(runtime.admin.sites))
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"
        async with ClientSession() as client:
            async with client.get(url) as response:
                import re

                csrf = re.search(
                    r'name="csrf" value="([^"]+)"', await response.text()
                ).group(1)
            request = asyncio.create_task(
                client.post(
                    url + "forget",
                    data={"csrf": csrf, "cuid": "hood1"},
                    headers={"Origin": url[:-1]},
                    allow_redirects=False,
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            shutting_down = asyncio.create_task(runtime.stop())
            await asyncio.sleep(0.02)
            assert not shutting_down.done() and not stopped.is_set()
            release.set()
            await asyncio.wait_for(shutting_down, 2)
            response = await asyncio.gather(request, return_exceptions=True)
            if not isinstance(response[0], Exception):
                response[0].close()
            assert stopped.is_set()

    asyncio.run(scenario())


def test_runtime_bridge_stops_before_bonjour(tmp_path, monkeypatch):
    loopback(monkeypatch)
    calls = []

    class FakeBridge:
        def __init__(self, settings, devices, store):
            calls.append("created")
            self.status = BrokerStatus("connecting", "127.0.0.1:18883")

        async def start(self):
            calls.append("started")

        async def stop(self):
            calls.append("bridge stopped")

    class FakeBonjour:
        async def start(self, addresses, port):
            calls.append("bonjour started")

        async def stop(self):
            calls.append("bonjour stopped")

    monkeypatch.setattr(app, "HaBridge", FakeBridge)
    monkeypatch.setattr(app, "Bonjour", FakeBonjour)

    async def scenario():
        from hoodini.config import MqttSettings

        settings = replace(
            load_settings(None),
            data_dir=tmp_path,
            mqtt=MqttSettings("mqtt://127.0.0.1:18883"),
        )
        runtime = app.Runtime(settings)
        await runtime.start()
        assert runtime.bridge is not None
        assert calls == ["created", "bonjour started", "started"]
        await runtime.start()
        assert calls == ["created", "bonjour started", "started"]
        site = next(iter(runtime.admin.sites))
        async with (
            ClientSession() as client,
            client.get(
                f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/snapshot"
            ) as response,
        ):
            card = (await response.json())["broker"]
            assert "127.0.0.1:18883" in card and "Configured broker" in card
        await runtime.stop()
        assert calls[-2:] == ["bridge stopped", "bonjour stopped"]
        assert runtime.bridge is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cancel", [False, True], ids=["observe-before-start", "cancel-unstarted"]
)
def test_runtime_admin_observes_bridge_before_bridge_start(
    tmp_path, monkeypatch, cancel
):
    loopback(monkeypatch)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        bridges, sites, calls = [], [], []
        original_site = app.web.TCPSite

        def capture_site(runner, host, port):
            site = original_site(runner, host, port)
            if port == 8099:
                sites.append(site)
            return site

        class ProbeBridge(app.HaBridge):
            def __init__(self, *args):
                super().__init__(*args)
                bridges.append(self)

            async def start(self):
                calls.append("bridge started")
                await super().start()

            async def stop(self):
                calls.append("bridge stopped")
                await super().stop()

        class GatedBonjour:
            async def start(self, *_):
                entered.set()
                await release.wait()

            async def stop(self):
                calls.append("bonjour stopped")

        monkeypatch.setattr(app, "HaBridge", ProbeBridge)
        monkeypatch.setattr(app, "Bonjour", GatedBonjour)
        monkeypatch.setattr(app.web, "TCPSite", capture_site)
        runtime = app.Runtime(
            replace(
                load_settings(None),
                data_dir=tmp_path,
                mqtt=MqttSettings("mqtt://127.0.0.1:1"),
            )
        )
        task = asyncio.create_task(runtime.start())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert bridges[0]._task is bridges[0]._subscription is None
            site = sites[0]
            url = (
                f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/snapshot"
            )
            async with ClientSession() as client, client.get(url) as response:
                card = (await response.json())["broker"]
                assert ">Connecting</span>" in card and "<dd>Starting</dd>" in card
                assert "127.0.0.1:1" in card and "Configured broker" in card
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, 2)
                assert caught.value.__context__ is caught.value.__cause__ is None
                assert calls == ["bridge stopped", "bonjour stopped"]
                assert bridges[0].status.state == "stopped"
                assert bridges[0]._task is bridges[0]._subscription is None
                assert not site._runner.sites
                assert runtime.bridge is runtime.gateway is runtime.admin is None
            else:
                release.set()
                await asyncio.wait_for(task, 2)
                assert calls == ["bridge started"]
                assert runtime.bridge is bridges[0]
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.stop()

    asyncio.run(scenario())


def test_runtime_bridge_start_failure_rolls_back_all_owners(tmp_path, monkeypatch):
    loopback(monkeypatch)
    calls, bridges = [], []

    class FailingBridge(app.HaBridge):
        def __init__(self, *args):
            super().__init__(*args)
            bridges.append(self)

        async def start(self):
            await super().start()
            raise OSError("SYNTHETICSECRET")

        async def stop(self):
            calls.append("bridge stopped")
            await super().stop()

    class ProbeBonjour:
        async def start(self, *_):
            calls.append("bonjour started")

        async def stop(self):
            assert bridges[0]._task is None
            calls.append("bonjour stopped")

    monkeypatch.setattr(app, "HaBridge", FailingBridge)
    monkeypatch.setattr(app, "Bonjour", ProbeBonjour)

    async def scenario():
        runtime = app.Runtime(
            replace(
                load_settings(None),
                data_dir=tmp_path,
                mqtt=MqttSettings("mqtt://broker"),
            )
        )
        with pytest.raises(app.RuntimeError) as caught:
            await runtime.start()
        assert "SYNTHETICSECRET" not in str(caught.value)
        assert caught.value.__context__ is caught.value.__cause__ is None
        assert calls == ["bonjour started", "bridge stopped", "bonjour stopped"]
        assert bridges[0]._task is bridges[0]._subscription is None
        assert (
            runtime.bridge
            is runtime.gateway
            is runtime.devices
            is runtime.api
            is runtime.admin
            is runtime.bonjour
            is None
        )
        await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("rollback", [False, True])
def test_runtime_cleanup_failure_retains_bridge_for_stop_retry(
    tmp_path, monkeypatch, rollback
):
    loopback(monkeypatch)
    calls, bridges = [], []

    class RetryBridge(app.HaBridge):
        def __init__(self, *args):
            super().__init__(*args)
            bridges.append(self)
            self.stops = 0

        async def stop(self):
            self.stops += 1
            calls.append("bridge stop")
            if self.stops == 1:
                raise OSError("SYNTHETICSECRET")
            await super().stop()

    class ProbeBonjour:
        async def start(self, *_):
            if rollback:
                raise OSError("SYNTHETICSECRET")

        async def stop(self):
            calls.append("bonjour stop")

    monkeypatch.setattr(app, "HaBridge", RetryBridge)
    monkeypatch.setattr(app, "Bonjour", ProbeBonjour)

    async def scenario():
        runtime = app.Runtime(
            replace(
                load_settings(None),
                data_dir=tmp_path,
                mqtt=MqttSettings("mqtt://127.0.0.1:1"),
            )
        )
        try:
            if rollback:
                with pytest.raises(
                    app.RuntimeError, match="clean up runtime startup safely"
                ):
                    await runtime.start()
            else:
                await runtime.start()
                with pytest.raises(app.RuntimeError, match="stop local runtime safely"):
                    await runtime.stop()
            assert calls == ["bridge stop"]
            assert runtime.bridge is bridges[0]
            assert (
                runtime.gateway is not None
                and runtime.admin.sites
                and runtime.api.sites
            )
            await runtime.stop()
            assert calls == ["bridge stop", "bridge stop", "bonjour stop"]
            assert runtime.bridge is runtime.admin is runtime.gateway is None
        finally:
            await runtime.stop()

    asyncio.run(scenario())
