"""Supervisor v1 contract against a loopback-only synthetic server."""

import asyncio
import json
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from aiohttp import web
from hoodini.config import load_settings
from hoodini.supervisor import SupervisorClient, resolve_mqtt_service


def test_options_json_maps_settings_without_exposing_password(tmp_path, caplog):
    path = tmp_path / "options.json"
    path.write_text(
        json.dumps(
            {
                "mqtt": {
                    "url": "mqtt://broker:1883",
                    "username": "u",
                    "password": "synthetic-secret",
                    "tls": False,
                },
                "capture": {"synthetic-secret": True},
                "api": {
                    "port": 8821,
                    "interfaces": ["en0"],
                    "addresses": ["192.168.1.10"],
                },
                "log_level": "WARNING",
            }
        )
    )
    with caplog.at_level(logging.INFO):
        settings = load_settings(path, addon=True)
    assert settings.mqtt.url == "mqtt://broker:1883"
    assert settings.mqtt.password == "synthetic-secret"
    assert "synthetic-secret" not in repr(settings)
    assert settings.data_dir == Path("/data")
    assert settings.api.port == 8821 and settings.api.interfaces == ("en0",)
    assert settings.log_level == "WARNING"
    assert not hasattr(settings, "capture")
    assert caplog.messages == ["Legacy capture option ignored."]
    assert "synthetic-secret" not in caplog.text


def test_missing_service_requires_restart(monkeypatch, caplog):
    async def scenario():
        calls = []
        service = None

        async def handler(request):
            assert (
                request.headers["Authorization"] == "Bearer synthetic-supervisor-token"
            )
            calls.append(request.path)
            if request.path == "/addons/self/info":
                return web.json_response(
                    {
                        "result": "ok",
                        "data": {
                            "ingress_port": 9012,
                            "ingress_entry": "/api/hassio_ingress/synthetic",
                            "ip_address": "172.30.32.1",
                        },
                    }
                )
            if service is None:
                return web.json_response(
                    {"result": "error", "message": "unavailable"}, status=400
                )
            return web.json_response({"result": "ok", "data": service})

        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-supervisor-token")
        try:
            async with SupervisorClient(base_url=f"http://127.0.0.1:{port}") as client:
                ctx = await client.self_info()
                assert (ctx.bind, ctx.port, ctx.prefix) == (
                    "172.30.32.1",
                    9012,
                    "/api/hassio_ingress/synthetic",
                )
                settings = await resolve_mqtt_service(
                    load_settings(None, addon=True), client
                )
                assert settings.mqtt.url is None
                assert calls == ["/addons/self/info", "/services/mqtt"]
                await asyncio.sleep(0.02)
                assert len(calls) == 2  # no background polling
                assert (
                    "restart the add-on after installing/configuring MQTT"
                    in caplog.text
                )
                service = {
                    "host": "broker",
                    "port": 1883,
                    "username": "u",
                    "password": "synthetic-password",
                    "ssl": False,
                }
            async with SupervisorClient(
                base_url=f"http://127.0.0.1:{port}"
            ) as restarted:
                settings = await resolve_mqtt_service(
                    load_settings(None, addon=True), restarted
                )
                assert settings.mqtt.url == "mqtt://broker:1883"
                assert settings.mqtt.password == "synthetic-password"
            assert calls == ["/addons/self/info", "/services/mqtt", "/services/mqtt"]
            assert "synthetic-supervisor-token" not in caplog.text
            assert "synthetic-password" not in caplog.text
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_supervisor_failures_are_sanitized_and_mqtt_keeps_core(monkeypatch, caplog):
    from hoodini.supervisor import SupervisorError

    async def scenario():
        statuses = [401, 403, 503, 200]

        async def handler(request):
            assert (
                request.headers["Authorization"] == "Bearer synthetic-supervisor-token"
            )
            status = statuses.pop(0)
            if status == 200:
                await asyncio.sleep(0.05)
            return web.json_response(
                {"result": "error", "message": "synthetic-password"}, status=status
            )

        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-supervisor-token")
        try:
            with caplog.at_level(logging.WARNING):
                async with SupervisorClient(
                    base_url=f"http://127.0.0.1:{port}"
                ) as client:
                    for _ in range(3):
                        settings = await resolve_mqtt_service(
                            load_settings(None, addon=True), client
                        )
                        assert settings.mqtt.url is None
                async with SupervisorClient(
                    base_url=f"http://127.0.0.1:{port}", timeout=0.01
                ) as client:
                    settings = await resolve_mqtt_service(
                        load_settings(None, addon=True), client
                    )
                    assert settings.mqtt.url is None
                assert not statuses
                assert (
                    caplog.text.count(
                        "restart the add-on after installing/configuring MQTT"
                    )
                    == 4
                )
                statuses.append(403)
                async with SupervisorClient(
                    base_url=f"http://127.0.0.1:{port}"
                ) as client:
                    with pytest.raises(SupervisorError, match="lookup unavailable"):
                        await client.self_info()
                assert "synthetic-supervisor-token" not in caplog.text
                assert "synthetic-password" not in caplog.text
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_options_overrides_service_values(tmp_path):

    path = tmp_path / "options.json"
    path.write_text(
        json.dumps(
            {
                "mqtt": {
                    "url": "mqtt://explicit:1884",
                    "username": "explicit",
                    "password": "synthetic-override",
                    "tls": False,
                }
            }
        )
    )
    settings = load_settings(path, addon=True)
    client = AsyncMock()
    client._get.return_value = {
        "host": "service",
        "port": 1883,
        "username": "service",
        "password": "synthetic-service",
        "ssl": True,
    }
    result = asyncio.run(resolve_mqtt_service(settings, client))
    assert (
        result.mqtt.url,
        result.mqtt.username,
        result.mqtt.password,
        result.mqtt.tls,
    ) == ("mqtt://explicit:1884", "explicit", "synthetic-override", False)
    client._get.assert_not_awaited()


def test_explicit_url_never_receives_service_credentials(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"mqtt": {"url": "mqtt://other-broker:1883"}}))
    settings = load_settings(path, addon=True)
    client = AsyncMock()
    client._get.return_value = {
        "host": "core-mosquitto",
        "port": 1883,
        "username": "service",
        "password": "synthetic-service",
        "ssl": True,
    }
    result = asyncio.run(resolve_mqtt_service(settings, client))
    assert (
        result.mqtt.url,
        result.mqtt.username,
        result.mqtt.password,
        result.mqtt.tls,
    ) == ("mqtt://other-broker:1883", None, None, False)
    client._get.assert_not_awaited()


def test_addon_metadata_and_supervisor_options_schema():
    root = Path(__file__).resolve().parents[3]
    repo = yaml.safe_load((root / "repository.yaml").read_text())
    config = yaml.safe_load((root / "addon/hoodini/config.yaml").read_text())
    assert repo["name"] and repo["url"] and repo["maintainer"]
    assert config["slug"] == "hoodini"
    assert config["version"] == "0.1.0"
    assert config["arch"] == ["amd64", "aarch64"]
    assert config["image"] == "ghcr.io/dmednis/hoodini"
    assert config["init"] is False
    assert config["host_network"] is True and config["ingress"] is True
    assert config["ingress_port"] == 0
    assert config["panel_icon"] == "mdi:chef-hat"
    assert config["services"] == ["mqtt:want"]
    assert config["environment"] == {"HOODINI_MODE": "addon"}
    # /addons/self/info and /services/mqtt bypass API role checks for an
    # installed app token (Supervisor v1 middleware/security.py at recorded revision).
    assert config.get("hassio_api", False) is False
    assert config.get("hassio_role", "default") == "default"
    assert config["options"]["mqtt"] == {}
    assert config["schema"]["mqtt"]["password"] == "password?"
    assert config["schema"]["api"]["addresses"] == ["str"]
    assert re.search(
        r'^version = "0.1.0"$',
        (root / "server/pyproject.toml").read_text(),
        re.MULTILINE,
    )
    # Validate schema element spellings against the Supervisor source grammar at
    # revision 87984b62bd7caa16b1960d4da5590a9b5a366174 (apps/options.py).
    element = re.compile(r"^(?:str|password|bool|port|list\([^()]+\))\??$")

    def validate(node):
        if isinstance(node, dict):
            for value in node.values():
                validate(value)
        elif isinstance(node, list):
            assert len(node) == 1
            validate(node[0])
        else:
            assert element.fullmatch(node)

    validate(config["schema"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("ingress_port", 0),
        ("ingress_port", 65536),
        ("ip_address", "0.0.0.0:80"),
        ("ingress_entry", "http://evil.example"),
    ],
)
def test_self_info_rejects_invalid_ingress_context(monkeypatch, field, value):
    from hoodini.supervisor import SupervisorError

    async def scenario():
        data = {
            "ingress_port": 9012,
            "ingress_entry": "/api/hassio_ingress/synthetic",
            "ip_address": "172.30.32.1",
        }
        data[field] = value

        async def handler(request):
            return web.json_response({"result": "ok", "data": data})

        app = web.Application()
        app.router.add_get("/addons/self/info", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-supervisor-token")
        try:
            async with SupervisorClient(base_url=f"http://127.0.0.1:{port}") as client:
                with pytest.raises(
                    SupervisorError, match="Invalid Supervisor ingress context"
                ):
                    await client.self_info()
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_supervisor_missing_token_fails_closed(monkeypatch):
    from hoodini.supervisor import SupervisorError

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    async def scenario():
        with pytest.raises(SupervisorError, match="token unavailable"):
            async with SupervisorClient():
                pytest.fail("missing token admitted")

    asyncio.run(scenario())


def test_explicit_broker_survives_service_outage(tmp_path, caplog):
    from hoodini.supervisor import SupervisorError

    options = tmp_path / "options.json"
    options.write_text(json.dumps({"mqtt": {"url": "mqtt://explicit:1883"}}))
    settings = load_settings(options, addon=True)
    client = AsyncMock()
    client._get.side_effect = SupervisorError("Supervisor lookup unavailable.")
    assert (
        asyncio.run(resolve_mqtt_service(settings, client)).mqtt.url
        == "mqtt://explicit:1883"
    )
    assert "restart the add-on" not in caplog.text


@pytest.mark.parametrize("failure", ["text", "malformed", "status", "timeout"])
def test_supervisor_exception_graph_drops_request_headers(monkeypatch, failure):
    from hoodini.supervisor import SupervisorError

    async def scenario():
        async def handler(request):
            if failure == "malformed":
                return web.Response(text="{", content_type="application/json")
            if failure == "status":
                return web.Response(status=503, text="unavailable")
            if failure == "timeout":
                await asyncio.sleep(1)
            return web.Response(text="not json", content_type="text/plain")

        app = web.Application()
        app.router.add_get("/addons/self/info", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv("SUPERVISOR_TOKEN", "SYNTHETIC-SECRET-MARKER")
        try:
            async with SupervisorClient(
                base_url=f"http://127.0.0.1:{port}", timeout=0.2
            ) as client:
                with pytest.raises(SupervisorError) as caught:
                    await client.self_info()
            seen, pending = set(), [caught.value]
            while pending:
                error = pending.pop()
                if error is None or id(error) in seen:
                    continue
                seen.add(id(error))
                info = getattr(error, "request_info", None)
                assert "SYNTHETIC-SECRET-MARKER" not in repr(error)
                assert "SYNTHETIC-SECRET-MARKER" not in str(
                    getattr(info, "headers", "")
                )
                pending += [error.__context__, error.__cause__]
            assert caught.value.__context__ is None
            assert caught.value.__cause__ is None
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
