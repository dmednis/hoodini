"""One-shot Supervisor v1 startup lookups; never include request credentials in errors."""

import ipaddress
import logging
import os
from dataclasses import replace

from aiohttp import ClientError, ClientSession, ClientTimeout

from hoodini.api.admin import IngressContext, validate_ingress_context
from hoodini.config import MqttSettings, Settings

_LOG = logging.getLogger(__name__)
_RESTART = "MQTT unavailable; restart the add-on after installing/configuring MQTT."


class SupervisorError(Exception):
    """Sanitized Supervisor startup failure."""


class SupervisorClient:
    def __init__(self, *, base_url="http://supervisor", timeout=3):
        # base_url override is for loopback-only synthetic tests; production uses fixed URL.
        self._base_url = base_url
        self._timeout = timeout
        self._session = None

    async def __aenter__(self):
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token or "\r" in token or "\n" in token:
            raise SupervisorError("Supervisor token unavailable.")
        self._session = ClientSession(
            headers={"Authorization": f"Bearer {token}"},
            timeout=ClientTimeout(total=self._timeout),
        )
        return self

    async def __aexit__(self, *_):
        await self._session.close()
        self._session = None

    async def _get(self, path):
        failed = False
        try:
            async with self._session.get(self._base_url + path) as response:
                if response.status != 200:
                    raise SupervisorError("Supervisor lookup unavailable.")
                payload = await response.json()
            if (
                type(payload) is not dict
                or payload.get("result") != "ok"
                or type(payload.get("data")) is not dict
            ):
                raise SupervisorError("Invalid Supervisor response.")
            return payload["data"]
        except (ClientError, TimeoutError, ValueError, TypeError):
            failed = True
        if failed:
            raise SupervisorError("Supervisor lookup unavailable.")

    async def self_info(self):
        data = await self._get("/addons/self/info")
        invalid = False
        try:
            bind = data["ip_address"]
            if str(ipaddress.IPv4Address(bind)) != bind:
                raise ValueError
            context = IngressContext(bind, data["ingress_port"], data["ingress_entry"])
            validate_ingress_context(context)
            return context
        except (KeyError, ValueError, TypeError):
            invalid = True
        if invalid:
            raise SupervisorError("Invalid Supervisor ingress context.")


async def resolve_mqtt_service(
    settings: Settings, client: SupervisorClient
) -> Settings:
    """Try once; explicit option values win; no service means no bridge until restart."""
    if settings.mqtt.url is not None:
        # An explicit broker is configured in full by the user; never send it the
        # Supervisor service's credentials.
        return replace(settings, mqtt_source="explicit")
    try:
        data = await client._get("/services/mqtt")
        host, port = data["host"], data["port"]
        if (
            type(host) is not str
            or not host
            or any(c in host for c in "/:@?#, \\\r\n")
            or type(port) is not int
            or not 1 <= port <= 65535
            or type(data.get("ssl", False)) is not bool
        ):
            raise ValueError
        mqtt = MqttSettings(
            url=f"mqtt://{host}:{port}",
            username=settings.mqtt.username
            if "username" in settings.mqtt_overrides
            or settings.mqtt.username is not None
            else data.get("username"),
            password=settings.mqtt.password
            if "password" in settings.mqtt_overrides
            or settings.mqtt.password is not None
            else data.get("password"),
            tls=settings.mqtt.tls
            if "tls" in settings.mqtt_overrides
            else settings.mqtt.tls or data.get("ssl", False),
        )
        if any(
            value is not None and type(value) is not str
            for value in (mqtt.username, mqtt.password)
        ):
            raise ValueError
        return replace(settings, mqtt=mqtt, mqtt_source="supervisor")
    except (SupervisorError, KeyError, ValueError, TypeError):
        if settings.mqtt.url is None:
            _LOG.warning(_RESTART)
        return replace(settings, mqtt_source="unavailable")
