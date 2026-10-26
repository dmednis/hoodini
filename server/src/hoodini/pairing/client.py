"""Bounded provisioning conversation."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from hoodini.pairing.transport import BleTarget, BleTransport
from hoodini.protocol.ble import (
    BleReassembler,
    NetworkInput,
    ProvisioningError,
    build_frame,
    encode_network,
)

logger = logging.getLogger("hoodini.pairing")


def _connect_reason(error: BaseException) -> str:
    """Fixed BLE connect failure categories; never echo adapter text."""
    if isinstance(error, TimeoutError):
        return "connect timed out"
    text = str(error).lower()
    if "disconnected" in text:
        return "hood dropped the connection"
    if "not found" in text or "was not found" in text:
        return "hood not found while connecting"
    if "bluetooth" in text and (
        "off" in text or "unauthorized" in text or "denied" in text
    ):
        return "Bluetooth unavailable or not permitted"
    return f"BLE error {type(error).__name__}"


class PairingError(ProvisioningError):
    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(message)


@dataclass(frozen=True)
class Network:
    ssid: bytes
    auth: str
    rssi: int


@dataclass(frozen=True)
class PairResult:
    cuid: str
    info: dict


class PairingClient:
    def __init__(
        self,
        transport: BleTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self._reassembler = BleReassembler()
        self._session_target: BleTarget | None = None

    @asynccontextmanager
    async def session(self, target: BleTarget):
        if self._session_target is not None:
            raise PairingError("connect", "Pairing session already active")
        try:
            await self._connect(target)
            self._session_target = target
            yield self
        finally:
            self._session_target = None
            await self.transport.disconnect()

    async def _within(self, operation, seconds: float, stage: str):
        if seconds <= 0:
            operation.close()
            raise PairingError(stage, f"{stage.replace('_', ' ')} timed out")
        task = asyncio.create_task(operation)
        timer = asyncio.create_task(self.sleep(seconds))
        try:
            done, _ = await asyncio.wait(
                (task, timer), return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                try:
                    return task.result()
                except Exception as error:  # noqa: BLE001 - third-party failures must not expose secrets
                    if stage == "connect":
                        reason = _connect_reason(error)
                        raise PairingError(
                            "connect", f"Connection failed ({reason})"
                        ) from None
                    raise PairingError(
                        "connection_lost", "Connection lost during pairing"
                    ) from None
            raise PairingError(stage, f"{stage.replace('_', ' ')} timed out")
        finally:
            for pending in (task, timer):
                if not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass

    async def _connect(self, target: BleTarget) -> None:
        reason = "connect timed out"
        for attempt in range(3):
            if attempt:
                # Pause, then refresh the scanned device before retrying.
                await self.sleep(2)
                try:
                    await self._within(
                        self.transport.rediscover(target, 10), 15, "rediscover"
                    )
                except PairingError:
                    pass
            try:
                await self._within(self.transport.connect(target), 20, "connect")
                self._reassembler.reset()
                return
            except PairingError as error:
                if error.stage == "connect" and "(" in str(error):
                    reason = str(error).split("(", 1)[1].rstrip(")")
                await self.transport.disconnect()
        raise PairingError("connect", f"Connection failed after 3 attempts ({reason})")

    async def _request(
        self, command: int, payload: bytes = b"", *, deadline: float, stage: str
    ) -> dict:
        frame = build_frame(command, payload)
        if len(frame) > self.transport.max_write_size():
            raise PairingError(
                "unsupported_transport",
                "Unsupported transport: frame exceeds write-with-response size",
            )
        if command == 6:
            safe = json.loads(payload)
            safe["PWD"] = safe["AUTH_PWD"] = "***"
            logger.debug("Outbound NETWORK_PAR length=%d redacted=%s", len(frame), safe)
        else:
            logger.debug("Outbound BLE frame %s", frame.hex())
        await self._within(self.transport.write(frame), deadline - self.clock(), stage)
        while True:
            chunk = await self._within(
                self.transport.read_notification(), deadline - self.clock(), stage
            )
            logger.debug("Inbound BLE notification %s", chunk.hex())
            for reply in self._reassembler.feed(chunk):
                if reply.command != command:
                    continue
                try:
                    data = json.loads(reply.payload)
                except (UnicodeError, ValueError):
                    raise PairingError(stage, "Invalid BLE reply JSON") from None
                if not isinstance(data, dict):
                    raise PairingError(stage, "Invalid BLE reply JSON")
                return data

    async def networks(self, target: BleTarget) -> list[Network]:
        if self._session_target is not None and self._session_target != target:
            raise PairingError("connect", "Different pairing target")
        try:
            if self._session_target != target:
                await self._connect(target)
            await self._request(2, deadline=self.clock() + 10, stage="get_info")
            end = self.clock() + 180
            while self.clock() < end:
                data = await self._request(
                    4, deadline=min(end, self.clock() + 30), stage="ssid_list"
                )
                try:
                    networks = [
                        Network(
                            item["SSID_NAME"].encode("utf-8"),
                            item["SSID_AUTH"],
                            item["SSID_RSSI"],
                        )
                        for item in data["SSID_LIST"]
                    ]
                except (KeyError, TypeError, AttributeError, UnicodeError):
                    raise PairingError("ssid_list", "Invalid SSID list reply") from None
                if networks:
                    return networks
                await self.sleep(min(3, max(0, end - self.clock())))
            raise PairingError(
                "ssid_list", "SSID list stayed empty before setup deadline"
            )
        finally:
            if self._session_target != target:
                await self.transport.disconnect()

    async def pair(
        self,
        target: BleTarget,
        network: Network,
        psk: str,
        server_ip: str,
        auth_pwd: str,
    ) -> PairResult:
        payload = encode_network(
            NetworkInput(network.ssid, network.auth, psk, server_ip, auth_pwd)
        )
        frame = build_frame(6, payload)
        if self._session_target is not None and self._session_target != target:
            raise PairingError("connect", "Different pairing target")
        try:
            if self._session_target != target:
                await self._connect(target)
            if len(frame) > self.transport.max_write_size():
                raise PairingError(
                    "unsupported_transport",
                    "Unsupported transport: frame exceeds write-with-response size",
                )
            end = self.clock() + 180
            response = await self._request(
                6, payload, deadline=min(end, self.clock() + 30), stage="network_par"
            )
            self._check_error(response)
            if response.get("RESULT") != "OK":
                raise PairingError("network_par", "Network parameters rejected")
            while self.clock() < end:
                info = await self._request(
                    2, deadline=min(end, self.clock() + 10), stage="get_info"
                )
                self._check_error(info)
                if info.get("WIFI STATUS") == "CLOUD CONNECTED":
                    cuid = info.get("CUID")
                    if not isinstance(cuid, str) or not cuid:
                        raise PairingError("get_info", "Missing CUID in GET_INFO")
                    return PairResult(cuid, info)
                await self.sleep(min(3, max(0, end - self.clock())))
            raise PairingError("cloud", "Hood never reached cloud connection")
        finally:
            if self._session_target != target:
                await self.transport.disconnect()

    @staticmethod
    def _check_error(data: dict) -> None:
        error = data.get("ERROR")
        if error == "WIFI WRONG PASSWORD":
            raise PairingError("wifi", "Wi-Fi wrong password")
        if error == "AUTH_PWD_WRONG":
            raise PairingError(
                "network_par", "Provisioning authentication password rejected"
            )
