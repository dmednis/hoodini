"""Loopback admin wire checks and synthetic ingress peer gates."""

import asyncio
import re

from aiohttp import ClientSession, web
from hoodini.api.admin import IngressContext, create_admin_app
from hoodini.devices.models import DeviceView
from hoodini.protocol.profiles import resolve_profile


class Devices:
    def __init__(self):
        self.calls = []

    def list(self):
        return [self.get("hood1")]

    def get(self, cuid):
        if cuid != "hood1":
            return None
        return DeviceView(
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

    async def update(self, cuid, **kwargs):
        self.calls.append(("update", cuid, kwargs))

    async def forget(self, cuid):
        self.calls.append(("forget", cuid))


class Diagnostics:
    def __init__(self):
        self.history = []


def test_admin_bootstrap_origin_csrf_and_ownership():
    async def scenario():
        devices = Devices()
        runner = web.AppRunner(
            create_admin_app(devices, "synthetic-token", Diagnostics(), addon=False)
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/"
        try:
            async with ClientSession() as client:
                async with client.get(url) as response:
                    assert response.status == 200
                    assert response.headers["Cache-Control"] == "no-store"
                    page = await response.text()
                    assert "synthetic-token" not in page
                    assert 'href="./assets/pico.min.css"' in page
                    assert 'id="token-panel" hidden' in page
                    assert (
                        "frame-ancestors 'self'"
                        in response.headers["Content-Security-Policy"]
                    )
                    assert 'action="./profile"' in page
                    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
                async with client.get(
                    url, headers={"Host": "evil.example"}
                ) as response:
                    assert response.status == 403
                    assert "synthetic-token" not in await response.text()
                for headers, data in (
                    ({}, {"csrf": csrf, "cuid": "hood1", "profile": "GENERIC"}),
                    (
                        {"Origin": "https://evil.example"},
                        {"csrf": csrf, "cuid": "hood1", "profile": "GENERIC"},
                    ),
                    (
                        {"Origin": url[:-1]},
                        {"csrf": "wrong", "cuid": "hood1", "profile": "GENERIC"},
                    ),
                ):
                    async with client.post(
                        url + "profile",
                        headers=headers,
                        data=data,
                        allow_redirects=False,
                    ) as response:
                        assert response.status == 403
                assert devices.calls == []
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_addon_hostile_peer_rejects_forged_proxy_headers():
    async def scenario():
        runner = web.AppRunner(
            create_admin_app(
                Devices(),
                "synthetic-token",
                Diagnostics(),
                addon=True,
                ingress_context=IngressContext(
                    "127.0.0.1", 8099, "/api/hassio_ingress/id/"
                ),
            )
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            async with (
                ClientSession() as client,
                client.get(
                    f"http://127.0.0.1:{port}/",
                    headers={
                        "X-Forwarded-For": "172.30.32.2",
                        "X-Forwarded-Host": "ha.example",
                        "X-Forwarded-Proto": "https",
                        "Host": "ha.example",
                    },
                ) as response,
            ):
                assert response.status == 403
                assert "synthetic-token" not in await response.text()
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_admin_valid_profile_forget_and_escaped_projection():
    async def scenario():
        devices = Devices()
        devices.list = lambda: [devices.get('<script>"')]
        devices.get = lambda cuid: (
            DeviceView(
                cuid,
                {"SERIAL": "private"},
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
            if cuid == '<script>"'
            else None
        )
        runner = web.AppRunner(
            create_admin_app(devices, 'token<&"', Diagnostics(), addon=False)
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"
        try:
            async with ClientSession() as client:
                async with client.get(url) as response:
                    page = await response.text()
                    assert response.status == 200
                    assert '<script>"' not in page and "SERIAL" not in page
                    assert "token&lt;&amp;&quot;" not in page
                    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
                async with client.get(url + "token") as response:
                    assert response.status == 200
                    assert (await response.json()) == {"token": 'token<&"'}
                    assert response.headers["Cache-Control"] == "no-store"
                async with client.get(url + "snapshot") as response:
                    data = await response.json()
                    assert '<script>"' not in data["devices"]
                    assert "&lt;script&gt;&quot;" in data["devices"]
                    assert "SERIAL" not in data["devices"]
                    assert "token" not in data
                for action, data in (("profile", {"profile": ""}), ("forget", {})):
                    async with client.post(
                        url + action,
                        data={"csrf": csrf, "cuid": '<script>"', **data},
                        headers={"Origin": url[:-1]},
                        allow_redirects=False,
                    ) as response:
                        assert response.status == 303
                        assert response.headers["Location"] == "./"
                assert devices.calls == [
                    ("update", '<script>"', {"profile": None}),
                    ("forget", '<script>"'),
                ]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_trusted_proxy_context_synthetic_transport_only():
    from unittest.mock import Mock

    from aiohttp.test_utils import make_mocked_request

    async def scenario():
        context = IngressContext("172.30.32.1", 8099, "/api/hassio_ingress/id/")
        application = create_admin_app(
            Devices(),
            "synthetic-token",
            Diagnostics(),
            addon=True,
            ingress_context=context,
        )
        boundary = application.middlewares[0]

        async def handler(request):
            return web.Response(text="admitted")

        def request(
            peer,
            host="172.30.32.1:8099",
            forwarded="ha.example",
            proto="https",
            origin="https://ha.example",
            method="POST",
            ingress_path="/api/hassio_ingress/id/",
            extra_headers=(),
        ):
            transport = Mock()
            transport.get_extra_info.side_effect = lambda key: (
                (peer, 1234) if key == "peername" else None
            )
            headers = {
                "Host": host,
                "Origin": origin,
                "X-Forwarded-Host": forwarded,
                "X-Forwarded-Proto": proto,
                "X-Forwarded-For": "172.30.32.2",
                "X-Ingress-Path": ingress_path,
            }
            return make_mocked_request(
                method,
                "/profile",
                headers=[
                    (key, value) for key, value in headers.items() if value is not None
                ]
                + list(extra_headers),
                transport=transport,
            )

        for forwarded, proto, origin in (
            ("ha.example", "https", "https://ha.example"),
            ("192.168.1.10:8123", "http", "http://192.168.1.10:8123"),
        ):
            assert (
                await boundary(
                    request(
                        "172.30.32.2",
                        host="browser.example",
                        forwarded=forwarded,
                        proto=proto,
                        origin=origin,
                    ),
                    handler,
                )
            ).text == "admitted"
        assert (
            await boundary(request("172.30.32.2", method="GET"), handler)
        ).text == "admitted"
        for candidate in (
            request("127.0.0.1"),
            request("172.30.32.2", forwarded="192.168.1.10:8123", proto="http"),
            request("172.30.32.2", forwarded="ha.example,evil.example"),
            request("172.30.32.2", proto="https,http"),
            request("172.30.32.2", forwarded=None),
            request("172.30.32.2", proto=None),
            request("172.30.32.2", forwarded=""),
            request("172.30.32.2", proto=""),
            request("172.30.32.2", extra_headers=[("X-Forwarded-Host", "ha.example")]),
            request("172.30.32.2", extra_headers=[("X-Forwarded-Proto", "https")]),
            request("172.30.32.2", ingress_path="/wrong/"),
        ):
            try:
                await boundary(candidate, handler)
            except web.HTTPForbidden:
                pass
            else:
                raise AssertionError("untrusted request admitted")

    asyncio.run(scenario())


def test_admin_form_split_across_reads_is_accepted():
    async def post_split(port, body, chunked):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        framing = (
            "Transfer-Encoding: chunked\r\n"
            if chunked
            else f"Content-Length: {len(body)}\r\n"
        )
        head = (
            f"POST /profile HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            f"{framing}Connection: close\r\n\r\n"
        ).encode()
        first, second = body[:10], body[10:]
        if chunked:
            first = b"%x\r\n%s\r\n" % (len(first), first)
            second = b"%x\r\n%s\r\n0\r\n\r\n" % (len(second), second)
        writer.write(head + first)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.write(second)
        await writer.drain()
        status = (await reader.readline()).decode()
        writer.close()
        return status

    async def scenario():
        devices = Devices()
        runner = web.AppRunner(
            create_admin_app(devices, "synthetic-token", Diagnostics(), addon=False)
        )
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as session:
                page = await (await session.get(f"http://127.0.0.1:{port}/")).text()
            csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
            body = f"csrf={csrf}&cuid=hood1&profile=GENERIC".encode()
            for chunked in (False, True):
                status = await post_split(port, body, chunked)
                assert " 303 " in status, (chunked, status)
            assert [call[0] for call in devices.calls] == ["update", "update"]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
