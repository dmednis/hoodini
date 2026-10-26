"""All dashboard routes share the existing admin boundary; assets are whitelisted."""

import asyncio
from unittest.mock import Mock

from aiohttp import ClientSession, web
from aiohttp.test_utils import make_mocked_request
from hoodini.api.admin import IngressContext, create_admin_app

_ROUTES = (
    "/",
    "/snapshot",
    "/token",
    "/assets/pico.min.css",
    "/assets/admin.css",
    "/assets/admin.js",
)


class Devices:
    def list(self):
        return []


class Diagnostics:
    @property
    def history(self):
        return []


def test_dashboard_routes_assets_and_host_boundary():
    async def scenario():
        app = create_admin_app(Devices(), "synthetic-token", Diagnostics(), addon=False)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                for path in _ROUTES:
                    async with client.get(base + path) as response:
                        assert response.status == 200
                        assert response.headers["Cache-Control"] == "no-store"
                        assert response.headers["X-Content-Type-Options"] == "nosniff"
                        assert "Access-Control-Allow-Origin" not in response.headers
                        body = await response.text()
                        assert ("synthetic-token" in body) == (path == "/token")
                        if path.endswith(".css"):
                            assert response.content_type == "text/css"
                        if path.endswith(".js"):
                            assert response.content_type == "text/javascript"
                        if path.endswith("pico.min.css"):
                            assert "Pico CSS" in body and "v2.1.1" in body
                    async with client.get(
                        base + path, headers={"Host": "evil.example"}
                    ) as response:
                        assert response.status == 403
                        assert "synthetic-token" not in await response.text()
                for path in (
                    "/assets/missing",
                    "/assets/PICO-LICENSE.md",
                    "/assets/admin.html",
                ):
                    async with client.get(base + path) as response:
                        assert response.status == 404
                async with client.post(
                    base + "/token", headers={"Origin": base}
                ) as response:
                    assert response.status == 405
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_every_dashboard_route_uses_trusted_ingress_peer_boundary():
    async def scenario():
        app = create_admin_app(
            Devices(),
            "synthetic-token",
            Diagnostics(),
            addon=True,
            ingress_context=IngressContext(
                "172.30.32.1", 8099, "/api/hassio_ingress/synthetic/"
            ),
        )
        boundary = app.middlewares[0]
        for path in _ROUTES:
            for peer in ("172.30.32.2", "127.0.0.1"):
                transport = Mock()
                transport.get_extra_info.side_effect = lambda key, peer=peer: (
                    (peer, 1234) if key == "peername" else None
                )
                request = make_mocked_request(
                    "GET",
                    path,
                    headers={
                        "Host": "172.30.32.1:8099",
                        "X-Forwarded-For": "172.30.32.2",
                        "X-Forwarded-Host": "ha.example",
                        "X-Forwarded-Proto": "https",
                        "X-Ingress-Path": "/api/hassio_ingress/synthetic/",
                    },
                    transport=transport,
                )
                match = await app.router.resolve(request)
                request._match_info = match
                if peer == "172.30.32.2":
                    response = await boundary(request, match.handler)
                    assert response.status == 200
                else:
                    try:
                        await boundary(request, match.handler)
                    except web.HTTPForbidden:
                        pass
                    else:
                        raise AssertionError("untrusted peer admitted")

    asyncio.run(scenario())
