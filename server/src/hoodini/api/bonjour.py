"""Advertise only eligible selected LAN interfaces, never default-all adapters."""

import ipaddress

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf


async def browse_servers(timeout: float = 5) -> list[str]:
    """Find IPv4 LAN API addresses advertised by Hoodini, within a bounded window."""
    import asyncio

    zc = AsyncZeroconf()
    names: set[str] = set()

    def on_service(zeroconf, service_type, name, state_change):
        if state_change.name != "Removed":
            names.add(name)

    browser = AsyncServiceBrowser(
        zc.zeroconf, "_hoodini._tcp.local.", handlers=[on_service]
    )
    try:
        await asyncio.sleep(timeout)
        addresses = set()

        async def resolve(name):
            return await zc.async_get_service_info(
                "_hoodini._tcp.local.", name, timeout=1000
            )

        infos = await asyncio.wait_for(
            asyncio.gather(*(resolve(name) for name in names)), timeout=2
        )
        for info in infos:
            if info and info.port == 8765:
                addresses.update(info.parsed_addresses())
        return sorted(
            address
            for address in addresses
            if ipaddress.ip_address(address).version == 4
        )
    finally:
        await browser.async_cancel()
        await zc.async_close()


_EXCLUDED = (
    "lo",
    "docker",
    "br-",
    "veth",
    "podman",
    "virbr",
    "vmnet",
    "utun",
    "tun",
    "tap",
    "wg",
    "tailscale",
    "zt",
    "cni",
    "flannel",
)


def select_addresses(adapters, interfaces=(), addresses=()):
    """Deterministic eligible interface addresses; explicit choices cannot bypass filtering."""
    v4, v6 = set(), set()
    for adapter in adapters:
        names = (adapter.name, adapter.nice_name)
        if adapter.name.lower().startswith(_EXCLUDED):
            continue
        if interfaces and not any(name in interfaces for name in names):
            continue
        for ip in adapter.ips:
            raw = ip.ip
            if isinstance(raw, tuple):  # ifaddr IPv6: (address, flowinfo, scopeid)
                raw = raw[0]
            try:
                parsed = ipaddress.ip_address(raw.split("%", 1)[0])
            except (ValueError, AttributeError):
                continue
            if (
                parsed.is_loopback
                or parsed.is_unspecified
                or parsed.is_multicast
                or parsed.is_link_local
                and parsed.version == 4
            ):
                continue
            if addresses and raw not in addresses:
                continue
            (v4 if parsed.version == 4 else v6).add(raw)
    return sorted(v4) + sorted(v6)


class Bonjour:
    def __init__(self):
        self._zc = None
        self._info = None

    async def start(self, addresses: list[str], port: int):
        if self._zc is not None:
            return
        if not addresses:
            return
        packed = [
            ipaddress.ip_address(address.split("%", 1)[0]).packed
            for address in addresses
        ]
        info = ServiceInfo(
            "_hoodini._tcp.local.",
            "Hoodini._hoodini._tcp.local.",
            addresses=packed,
            port=port,
            properties={"api": "1"},
        )
        # Explicit interface addresses only: never Zeroconf's all-interface default.
        zc = AsyncZeroconf(interfaces=addresses)
        self._zc, self._info = zc, info
        try:
            # Another Hoodini may already own the name; mDNS renames this one.
            await zc.async_register_service(info, allow_name_change=True)
        except BaseException:
            try:
                await zc.async_close()
            finally:
                self._zc = self._info = None
            raise

    async def stop(self):
        zc, info = self._zc, self._info
        if zc is None:
            return
        try:
            await zc.async_unregister_service(info)
        finally:
            await zc.async_close()
            self._zc = self._info = None
