"""Single owner of the local gateway and device service lifecycle."""

import asyncio
import ipaddress
import os
import ssl

import ifaddr
from aiohttp import web

from hoodini.api.admin import IngressContext, create_admin_app, validate_ingress_context
from hoodini.api.bonjour import Bonjour, select_addresses
from hoodini.api.lan import create_lan_app
from hoodini.certs import ensure_cert
from hoodini.devices.service import DeviceService
from hoodini.diagnostics import DiagnosticSink
from hoodini.gateway.server import Gateway
from hoodini.ha_bridge.client import HaBridge
from hoodini.security import TrustStore, ensure_api_token
from hoodini.storage import AtomicStore

_BIND_ERROR = (
    "Cannot bind fixed TLS gateway port 8883. Mosquitto reserves 8883 by default "
    "even without TLS; manually blank or remap its port, accounting for existing "
    "client connections. No network mapping was changed."
)


class RuntimeError(Exception):
    """Safe-to-display startup/shutdown failure without original exception graph."""


class Runtime:
    def __init__(
        self,
        settings,
        *,
        ingress_context: IngressContext | None = None,
        gateway_port=8883,
    ):
        self.gateway_port = gateway_port
        self.settings = settings
        self.ingress_context = ingress_context
        self.store: AtomicStore | None = None
        self.trust: TrustStore | None = None
        self.token: str | None = None
        self.diagnostics: DiagnosticSink | None = None
        self.gateway: Gateway | None = None
        self.devices: DeviceService | None = None
        self.api: web.AppRunner | None = None
        self.bonjour: Bonjour | None = None
        self.admin: web.AppRunner | None = None
        self.bridge = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self.gateway is not None:
                return
            gateway = None
            devices = None
            api = None
            bonjour = None
            admin = None
            bridge = None
            failure = None
            binding_failure = False
            mode = os.environ.get("HOODINI_MODE", "standalone")
            if mode not in ("standalone", "addon"):
                raise RuntimeError("Invalid runtime mode.")
            addon = mode == "addon"
            if addon:
                if self.ingress_context is None:
                    raise RuntimeError(
                        "Trusted ingress context required before startup."
                    )
                try:
                    validate_ingress_context(self.ingress_context)
                except ValueError:
                    raise RuntimeError("Invalid trusted ingress context.") from None
                admin_host, admin_port = (
                    self.ingress_context.bind,
                    self.ingress_context.port,
                )
            else:
                admin_host, admin_port = _admin_binding(self.settings.admin.bind)
            try:
                store = AtomicStore(self.settings.data_dir)
                trust = TrustStore(self.settings.data_dir)
                token = ensure_api_token(self.settings.data_dir)
                cert, key = ensure_cert(self.settings.data_dir)
                diagnostics = DiagnosticSink()
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.load_cert_chain(cert, key)
                devices = DeviceService(store)
                gateway = Gateway(
                    self.settings.gateway.bind,
                    self.gateway_port,
                    tls,
                    trust,
                    diagnostics,
                    devices.handle_gateway,
                )
                devices.attach_gateway(gateway)
                await devices.start()  # State is loaded before peers can be admitted.
                try:
                    await gateway.start()
                except OSError:
                    binding_failure = True
                if not binding_failure:
                    api = web.AppRunner(create_lan_app(devices, token))
                    await api.setup()
                    await web.TCPSite(api, "0.0.0.0", self.settings.api.port).start()
                    if self.settings.mqtt.url is not None:
                        bridge = HaBridge(self.settings.mqtt, devices, store)
                    broker_source = self.settings.mqtt_source
                    if self.settings.mqtt.url is not None and broker_source is None:
                        broker_source = "explicit"
                    admin = web.AppRunner(
                        create_admin_app(
                            devices,
                            token,
                            diagnostics,
                            addon=addon,
                            bridge=bridge,
                            broker_source=broker_source,
                            ingress_context=self.ingress_context
                            if addon
                            else IngressContext(admin_host, admin_port, "/"),
                        )
                    )
                    await admin.setup()
                    await web.TCPSite(admin, admin_host, admin_port).start()
                    selected = select_addresses(
                        ifaddr.get_adapters(),
                        self.settings.api.interfaces,
                        self.settings.api.addresses,
                    )
                    bonjour = Bonjour()
                    await bonjour.start(selected, self.settings.api.port)
                    if bridge is not None:
                        await bridge.start()
                    self.bridge = bridge
                    self.store, self.trust, self.token = store, trust, token
                    self.diagnostics, self.devices, self.gateway = (
                        diagnostics,
                        devices,
                        gateway,
                    )
                    self.api, self.admin, self.bonjour = api, admin, bonjour
                    return
            except BaseException as error:  # noqa: BLE001 - cancellation also needs rollback
                failure = error
            # Outside exception handlers: no parser, OS or TLS exception chain crosses boundary.
            cleanup_failed = False
            for owner, action in (
                (bridge, "stop"),
                (bonjour, "stop"),
                (admin, "cleanup"),
                (api, "cleanup"),
                (gateway, "stop"),
                (devices, "stop"),
            ):
                if owner is not None:
                    try:
                        await getattr(owner, action)()
                    except BaseException:  # noqa: BLE001 - retain ownership for retry
                        cleanup_failed = True
                        break  # Never stop service while HTTP operations may still own it.
            if cleanup_failed:
                self.gateway, self.devices = gateway, devices
                self.api, self.admin, self.bonjour, self.bridge = (
                    api,
                    admin,
                    bonjour,
                    bridge,
                )
                raise RuntimeError("Cannot clean up runtime startup safely.")
            if isinstance(failure, asyncio.CancelledError):
                raise asyncio.CancelledError() from None
            if binding_failure:
                raise RuntimeError(_BIND_ERROR)
            raise RuntimeError(
                "Cannot start local runtime; inspect private data and configuration."
            )

    async def stop(self):
        async with self._lock:
            if (
                self.gateway is None
                and self.api is None
                and self.admin is None
                and self.bonjour is None
                and self.bridge is None
            ):
                return
            failed = False
            for owner, action in (
                (self.bridge, "stop"),
                (self.bonjour, "stop"),
                (self.admin, "cleanup"),
                (self.api, "cleanup"),
                (self.gateway, "stop"),
                (self.devices, "stop"),
            ):
                if owner is not None:
                    try:
                        await getattr(owner, action)()
                    except BaseException:  # noqa: BLE001 - retain references for retry
                        failed = True
                        break  # Preserve reverse lifecycle dependencies on failure.
            if failed:
                raise RuntimeError("Cannot stop local runtime safely.")
            self.gateway = self.devices = self.api = self.admin = self.bonjour = (
                self.bridge
            ) = None


def _admin_binding(value):
    """Parse the existing combined standalone admin.bind without implicit widening."""
    try:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError
        if value.startswith("["):
            host, separator, port_text = value[1:].partition("]:")
            if not separator:
                raise ValueError
            ipaddress.IPv6Address(host)
        else:
            host, separator, port_text = value.rpartition(":")
            if not separator or ":" in host or not host:
                raise ValueError
        port = int(port_text)
        if (
            not port_text.isascii()
            or not port_text.isdecimal()
            or not 1 <= port <= 65535
        ):
            raise ValueError
        return host, port
    except (ValueError, TypeError):
        raise RuntimeError("Invalid admin.bind configuration.") from None
