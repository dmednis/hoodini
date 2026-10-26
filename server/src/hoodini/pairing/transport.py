"""Fakeable BLE provisioning transport."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from hoodini.protocol.ble import ProvisioningError

SERVICE_UUID = "018c3a17-8449-837d-ab88-ad92b8d9efdd"
WRITE_UUID = "0000bb21-8449-837d-ab88-ad92b8d9efdd"
NOTIFY_UUID = "0000aa21-8449-837d-ab88-ad92b8d9efdd"


@dataclass(frozen=True)
class BleTarget:
    id: str
    name: str


class BleTransport(Protocol):
    async def discover(self, timeout: float) -> list[BleTarget]: ...
    async def connect(self, target: BleTarget) -> None: ...
    async def rediscover(self, target: BleTarget, timeout: float) -> None: ...
    async def write(self, frame: bytes) -> None: ...
    async def read_notification(self) -> bytes: ...
    async def disconnect(self) -> None: ...
    def max_write_size(self) -> int: ...


class BleakTransport:
    def __init__(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            raise ProvisioningError(
                "Install hoodini[pairing] for BLE pairing"
            ) from None
        self._client_type = BleakClient
        self._scanner = BleakScanner
        self._client = None
        # Scanned device objects: on macOS, connecting by address string forces a
        # second scan inside connect(), so reuse the scan result.
        self._devices: dict[str, object] = {}
        self._notifications: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def discover(self, timeout: float) -> list[BleTarget]:
        scan_timeout = min(timeout, 15)
        devices = await asyncio.wait_for(
            self._scanner.discover(timeout=scan_timeout), timeout=scan_timeout + 5
        )
        hoods = [
            device
            for device in devices
            if device.name and device.name.startswith("ELC_")
        ]
        self._devices.update((device.address, device) for device in hoods)
        return [BleTarget(device.address, device.name) for device in hoods]

    async def rediscover(self, target: BleTarget, timeout: float) -> None:
        device = await self._scanner.find_device_by_address(target.id, timeout=timeout)
        if device is not None:
            self._devices[target.id] = device

    async def connect(self, target: BleTarget) -> None:
        notifications: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._notifications = notifications
        client = self._client_type(
            self._devices.get(target.id, target.id),
            # Discovering only our service avoids the hood dropping the link during
            # full service discovery.
            services=[SERVICE_UUID],
            timeout=15,
            disconnected_callback=lambda _client: notifications.put_nowait(None),
        )
        self._client = client
        await client.connect()
        await client.start_notify(
            NOTIFY_UUID,
            lambda _sender, data: notifications.put_nowait(bytes(data)),
        )

    async def write(self, frame: bytes) -> None:
        await self._client.write_gatt_char(WRITE_UUID, frame, response=True)

    async def read_notification(self) -> bytes:
        chunk = await self._notifications.get()
        if chunk is None:
            raise ConnectionError("BLE connection lost")
        return chunk

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.disconnect()

    def max_write_size(self) -> int:
        if self._client is None:
            raise ProvisioningError("BLE transport is not connected")
        return max(0, self._client.mtu_size - 3)
