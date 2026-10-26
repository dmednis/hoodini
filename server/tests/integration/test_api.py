"""Synthetic LAN wire-contract checks, exclusively loopback ephemeral sockets."""

import asyncio
import json

from aiohttp import ClientSession, web
from hoodini.api import bonjour, lan
from hoodini.api.bonjour import select_addresses
from hoodini.api.lan import create_lan_app
from hoodini.api.schemas import as_public_dict
from hoodini.devices.models import CommandResult, DeviceView
from hoodini.protocol.profiles import ValidationError, resolve_profile


def view():
    return DeviceView(
        "hood1",
        {"PRF": "PRF0182867A", "SERIAL": "private"},
        resolve_profile({"PRF": "PRF0182867A"}, None),
        None,
        {2: 3},
        {110: 2},
        {110},
        -40,
        42.0,
        1,
        2,
        "ready",
        True,
        "opaque-session",
    )


class Devices:
    def __init__(self):
        self.device = view()
        self.calls = []
        self.result = CommandResult("ok", 7, 1)

    def list(self):
        return [self.device] if self.device else []

    def get(self, cuid):
        return self.device if cuid == "hood1" else None

    async def set_caps(self, cuid, caps):
        self.calls.append(("command", cuid, caps))
        return self.result

    async def update(self, cuid, info=None, profile="omitted"):
        self.calls.append(("update", cuid, info, profile))
        return self.device

    async def forget(self, cuid):
        self.calls.append(("forget", cuid))
        self.device = None


def test_explicit_detached_public_projection():
    dto = as_public_dict(view())
    assert dto == {
        "cuid": "hood1",
        "info": {"PRF": "PRF0182867A"},
        "profile": {"key": "HIDDEN_ADVANCE", "fan_max": 3, "fan_modes": [1, 5]},
        "profile_override": None,
        "firmware": {"2": 3},
        "caps": {"110": 2},
        "known_ids": [110],
        "rssi": -40,
        "last_seen": 42.0,
        "last_light": 1,
        "last_fan": 2,
        "session_state": "ready",
        "online": True,
        "connection_id": "opaque-session",
    }
    dto["info"]["PRF"] = "changed"
    assert view().info["PRF"] == "PRF0182867A"


def test_wire_authorization_validation_and_removed_events_route():
    async def scenario():
        devices = Devices()
        runner = web.AppRunner(create_lan_app(devices, "synthetic-token"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        root = f"http://127.0.0.1:{port}"
        try:
            async with ClientSession() as client:
                async with client.get(root + "/") as response:
                    assert response.status == 404
                    assert "synthetic-token" not in await response.text()
                async with client.get(root + "/api/v1/devices") as response:
                    assert response.status == 200
                    assert (await response.json())[0]["info"] == {"PRF": "PRF0182867A"}
                async with client.get(root + "/api/v1/devices/missing") as response:
                    assert response.status == 404
                url = root + "/api/v1/devices/hood1/commands"
                async with client.post(url, json={"caps": {"110": 2}}) as response:
                    assert response.status == 401
                headers = {"Authorization": "Bearer synthetic-token"}
                for body in (
                    {"caps": {"110": True}},
                    {"caps": {"99999999999999999999": 1}},
                    {"caps": {}},
                    {"caps": {"110": 2}, "other": 1},
                ):
                    async with client.post(url, json=body, headers=headers) as response:
                        assert response.status == 422
                async with client.post(
                    url, json={"caps": {"110": 2}}, headers=headers
                ) as response:
                    assert response.status == 200
                    assert await response.json() == {
                        "status": "ok",
                        "seq": 7,
                        "ack_result": 1,
                    }
                assert devices.calls[-1] == ("command", "hood1", {110: 2})
                devices.result = CommandResult("offline")
                async with client.post(
                    url, json={"caps": {"110": 2}}, headers=headers
                ) as response:
                    assert response.status == 409
                devices.result = CommandResult("timeout")
                async with client.post(
                    url, json={"caps": {"110": 2}}, headers=headers
                ) as response:
                    assert response.status == 504
                async with client.patch(
                    root + "/api/v1/devices/hood1",
                    json={"info": {"SERIAL": "private"}},
                    headers=headers,
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["info"] == {"PRF": "PRF0182867A"}
                assert devices.calls[-1][:3] == (
                    "update",
                    "hood1",
                    {"SERIAL": "private"},
                )
                assert devices.calls[-1][3] == "omitted"
                async with client.patch(
                    root + "/api/v1/devices/hood1",
                    json={"profile": None},
                    headers=headers,
                ) as response:
                    assert response.status == 200
                assert devices.calls[-1] == ("update", "hood1", None, None)
                async with client.get(root + "/api/v1/events") as response:
                    assert response.status == 404
                async with client.delete(
                    root + "/api/v1/devices/hood1", headers=headers
                ) as response:
                    assert response.status == 204
                assert devices.calls[-1] == ("forget", "hood1")
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_fragmented_content_length_and_chunked_json_wait_for_complete_body():
    async def scenario():
        devices = Devices()
        runner = web.AppRunner(create_lan_app(devices, "synthetic-token"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        body = b'{"caps":{"110":2}}'
        try:
            for chunked in (False, True):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                header = (
                    b"POST /api/v1/devices/hood1/commands HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nAuthorization: Bearer synthetic-token\r\n"
                    + (
                        b"Transfer-Encoding: chunked\r\n"
                        if chunked
                        else b"Content-Length: 18\r\n"
                    )
                    + b"Connection: close\r\n\r\n"
                )
                first, second = body[:9], body[9:]
                writer.write(
                    header + (b"9\r\n" + first + b"\r\n" if chunked else first)
                )
                await writer.drain()
                await asyncio.sleep(0.02)  # release partial TCP payload to server first
                writer.write(
                    b"9\r\n" + second + b"\r\n0\r\n\r\n" if chunked else second
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 2)
                assert response.startswith(b"HTTP/1.1 200 "), response.split(
                    b"\r\n", 1
                )[0]
                assert b'"status": "ok"' in response
                writer.close()
                await writer.wait_closed()
            assert devices.calls == [
                ("command", "hood1", {110: 2}),
                ("command", "hood1", {110: 2}),
            ]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_invalid_json_and_service_validation_have_fixed_graph_free_errors(monkeypatch):
    async def scenario():
        devices = Devices()
        recorded = []
        original = web.HTTPUnprocessableEntity

        class RecordedInvalid(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                recorded.append(self)

        monkeypatch.setattr(lan.web, "HTTPUnprocessableEntity", RecordedInvalid)
        runner = web.AppRunner(create_lan_app(devices, "synthetic-token"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/api/v1/devices/hood1/commands"
        marker = "synthetic-private-json-marker"

        async def invalid_service(cuid, caps):
            raise ValidationError("synthetic-private-service-marker")

        try:
            async with ClientSession() as client:
                headers = {"Authorization": "Bearer synthetic-token"}
                async with client.post(
                    url, data=b'{"caps":' + marker.encode(), headers=headers
                ) as response:
                    assert response.status == 422
                    assert await response.text() == "Invalid request."
                devices.set_caps = invalid_service
                async with client.post(
                    url, json={"caps": {"110": 2}}, headers=headers
                ) as response:
                    assert response.status == 422
                    assert await response.text() == "Invalid request."
            assert len(recorded) == 2
            for error in recorded:
                assert error.__context__ is None and error.__cause__ is None
                assert marker not in repr(error)
                assert "synthetic-private-service-marker" not in repr(error)
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_deep_metadata_and_oversize_body_reject_422_before_mutation():
    async def scenario():
        devices = Devices()
        runner = web.AppRunner(create_lan_app(devices, "synthetic-token"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/api/v1/devices/hood1"
        headers = {"Authorization": "Bearer synthetic-token"}
        try:
            async with ClientSession() as client:
                for depth in (600, 1200):
                    raw = (
                        b'{"info":{"nested":'
                        + b"[" * depth
                        + b"0"
                        + b"]" * depth
                        + b"}}"
                    )
                    assert len(raw) < 65536
                    async with client.patch(url, data=raw, headers=headers) as response:
                        assert response.status == 422
                        assert await response.text() == "Invalid request."
                    assert devices.calls == []
                async with client.patch(
                    url, data=b'{"info":{"a":' + b"0" * 65536 + b"}}", headers=headers
                ) as response:
                    assert response.status == 422

                async def chunks():
                    yield b'{"info":{"a":"' + b"x" * 32000
                    yield b"x" * 34000 + b'"}}'

                async with client.patch(
                    url, data=chunks(), headers=headers
                ) as response:
                    assert response.status == 422
                assert devices.calls == []
                async with client.patch(
                    url,
                    data=json.dumps(
                        {"info": {"nested": [1, {"ok": True}], "float": 1.5}}
                    ),
                    headers=headers,
                ) as response:
                    assert response.status == 200
                assert devices.calls[-1][:3] == (
                    "update",
                    "hood1",
                    {"nested": [1, {"ok": True}], "float": 1.5},
                )
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_http_cleanup_drains_owned_forget_after_disconnected_caller():
    async def scenario():
        devices = Devices()
        entered, release = asyncio.Event(), asyncio.Event()

        async def forget(cuid):
            entered.set()
            await release.wait()
            devices.device = None

        devices.forget = forget
        runner = web.AppRunner(create_lan_app(devices, "synthetic-token"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with ClientSession() as client:
            request = asyncio.create_task(
                client.delete(
                    f"http://127.0.0.1:{port}/api/v1/devices/hood1",
                    headers={"Authorization": "Bearer synthetic-token"},
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            stopping = asyncio.create_task(runner.cleanup())
            await asyncio.sleep(0)
            assert not stopping.done() and devices.device is not None
            release.set()
            await asyncio.wait_for(stopping, 2)
            assert devices.device is None

    asyncio.run(scenario())


def test_synthetic_selected_lan_interfaces_only():
    class IP:
        def __init__(self, ip):
            self.ip = ip

    class Adapter:
        def __init__(self, name, *ips):
            self.nice_name = name
            self.name = name
            self.ips = [IP(ip) for ip in ips]

    adapters = [
        Adapter("lo0", "127.0.0.1"),
        Adapter("docker0", "172.17.0.1"),
        Adapter("utun1", "10.1.1.1"),
        Adapter("en0", "192.168.1.2", "fe80::1%en0"),
        Adapter("wlan0", "10.0.0.2"),
    ]
    assert select_addresses(adapters) == ["10.0.0.2", "192.168.1.2", "fe80::1%en0"]
    assert select_addresses(adapters, interfaces=("wlan0",)) == ["10.0.0.2"]
    assert select_addresses(adapters, addresses=("10.1.1.1",)) == []


def test_bonjour_selected_addresses_txt_and_cleanup(monkeypatch):
    created = []

    class FakeZeroconf:
        def __init__(self, *, interfaces):
            assert interfaces == ["192.168.1.2"]
            self.actions = []
            created.append(self)

        async def async_register_service(self, info, *, allow_name_change=False):
            assert allow_name_change
            assert info.type == "_hoodini._tcp.local."
            assert info.port == 8765
            assert info.properties == {b"api": b"1"}
            assert info.parsed_addresses() == ["192.168.1.2"]
            self.actions.append("register")

        async def async_unregister_service(self, info):
            self.actions.append("unregister")

        async def async_close(self):
            self.actions.append("close")

    monkeypatch.setattr(bonjour, "AsyncZeroconf", FakeZeroconf)

    async def scenario():
        owner = bonjour.Bonjour()
        await owner.start([], 8765)
        assert not created
        for _ in range(2):
            await owner.start(["192.168.1.2"], 8765)
            await owner.stop()
        assert [zc.actions for zc in created] == [
            ["register", "unregister", "close"],
            ["register", "unregister", "close"],
        ]

    asyncio.run(scenario())


def test_browser_origin_on_all_lan_mutations_without_breaking_bearer_clients():
    async def scenario():
        devices = Devices()
        runner = web.AppRunner(create_lan_app(devices, "synthetic-token"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        root = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        headers = {"Authorization": "Bearer synthetic-token"}
        try:
            async with ClientSession() as client:
                for method, url, body in (
                    (
                        "POST",
                        root + "/api/v1/devices/hood1/commands",
                        {"caps": {"110": 2}},
                    ),
                    ("PATCH", root + "/api/v1/devices/hood1", {"profile": None}),
                    ("DELETE", root + "/api/v1/devices/hood1", None),
                ):
                    for origin in (
                        "null",
                        "https://foreign.example",
                        "http://127.0.0.1, http://evil.example",
                    ):
                        async with client.request(
                            method,
                            url,
                            json=body,
                            headers={
                                **headers,
                                "Origin": origin,
                                "X-Forwarded-Host": "foreign.example",
                            },
                        ) as response:
                            assert response.status == 403
                    assert not devices.calls
                async with client.patch(
                    root + "/api/v1/devices/hood1",
                    json={"profile": None},
                    headers=headers,
                ) as response:
                    assert response.status == 200
                assert devices.calls[-1][0] == "update"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
