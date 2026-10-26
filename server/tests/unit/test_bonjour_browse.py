"""Bonjour browse handler contract against zeroconf's real keyword dispatch."""

import asyncio
from types import SimpleNamespace

from hoodini.api import bonjour
from zeroconf import ServiceStateChange
from zeroconf._services import Signal


def test_browse_handler_accepts_zeroconf_keyword_dispatch(monkeypatch):
    class FakeZeroconf:
        def __init__(self):
            self.zeroconf = object()

        async def async_get_service_info(self, service_type, name, timeout):
            assert name == "Hoodini._hoodini._tcp.local."
            return SimpleNamespace(port=8765, parsed_addresses=lambda: ["192.0.2.10"])

        async def async_close(self):
            pass

    class FakeBrowser:
        def __init__(self, zc, service_type, handlers):
            signal = Signal()
            for handler in handlers:
                signal.registration_interface.register_handler(handler)
            # zeroconf invokes handlers only with these keyword arguments.
            signal.fire(
                zeroconf=zc,
                service_type=service_type,
                name="Hoodini._hoodini._tcp.local.",
                state_change=ServiceStateChange.Added,
            )

        async def async_cancel(self):
            pass

    monkeypatch.setattr(bonjour, "AsyncZeroconf", FakeZeroconf)
    monkeypatch.setattr(bonjour, "AsyncServiceBrowser", FakeBrowser)
    assert asyncio.run(bonjour.browse_servers(timeout=0)) == ["192.0.2.10"]


def test_advertisement_tolerates_an_existing_hoodini_name(monkeypatch):
    """A second Hoodini on the same LAN must still start."""
    calls = []

    class FakeZeroconf:
        def __init__(self, interfaces):
            pass

        async def async_register_service(self, info, **kwargs):
            calls.append(kwargs)

        async def async_unregister_service(self, info):
            pass

        async def async_close(self):
            pass

    monkeypatch.setattr(bonjour, "AsyncZeroconf", FakeZeroconf)

    async def scenario():
        advertiser = bonjour.Bonjour()
        await advertiser.start(["192.0.2.10"], 8765)
        await advertiser.stop()

    asyncio.run(scenario())
    assert calls == [{"allow_name_change": True}]
