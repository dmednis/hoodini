"""Background MQTT owner for retained Home Assistant discovery and controls."""

import asyncio
import logging
import random
import ssl
import time
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlsplit

import aiomqtt

from hoodini.config import MqttSettings
from hoodini.ha_bridge.commands import ha_command
from hoodini.ha_bridge.discovery import (
    COMMAND_SUBSCRIPTION,
    HA_STATUS_TOPIC,
    Publication,
    availability_publication,
    bridge_publication,
    desired_components,
    discovery,
    forget_publications,
    parse_command_topic,
    state_publication,
)

_LOG = logging.getLogger(__name__)


# Fixed, actionable reasons only: broker URLs and error text may carry credentials.
_TEXT_REASONS = (
    ("timed out", "connection timed out"),
    ("refused", "connection refused"),
    ("connect call failed", "connection refused"),
    ("nodename nor servname", "broker host name could not be resolved"),
    ("name or service not known", "broker host name could not be resolved"),
    ("name resolution", "broker host name could not be resolved"),
    ("unreachable", "network unreachable"),
    ("certificate", "TLS certificate rejected"),
    ("ssl", "TLS handshake failed"),
)


def broker_failure_reason(error: BaseException) -> str:
    """Classify a broker failure without echoing any part of the original error."""
    if isinstance(error, aiomqtt.MqttCodeError):
        rc = error.rc
        if isinstance(rc, int):
            return f"broker rejected the connection (code {rc})"
        return "broker rejected the connection"
    if isinstance(error, ssl.SSLError):
        return "TLS handshake failed"
    if isinstance(error, ValueError):
        return "invalid broker URL"
    text = str(error).lower()
    for needle, reason in _TEXT_REASONS:
        if needle in text:
            return reason
    return "connection failed"


BrokerState = Literal["connecting", "connected", "retrying", "stopped"]
HaState = Literal["online", "offline"]


@dataclass(frozen=True)
class BrokerStatus:
    """Display-safe snapshot: no credentials or raw exception text."""

    state: BrokerState
    endpoint: str | None = None
    since: float | None = None
    reason: str | None = None
    retry_at: float | None = None
    ha_state: HaState | None = None
    ha_seen_at: float | None = None


def _target(settings: MqttSettings) -> tuple[str, int, bool]:
    invalid = False
    try:
        url = urlsplit(settings.url)
        if url.scheme not in ("mqtt", "mqtts") or not url.hostname:
            raise ValueError
        tls = settings.tls or url.scheme == "mqtts"
        port = url.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        # An explicit empty port is not a valid broker port either.
        if url.netloc.endswith(":"):
            raise ValueError
        return url.hostname, port if port is not None else (8883 if tls else 1883), tls
    except (ValueError, TypeError):
        invalid = True
    if invalid:
        raise ValueError("invalid MQTT bridge URL")


def _endpoint(settings: MqttSettings) -> str | None:
    try:
        host, port, _ = _target(settings)
    except ValueError:
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class HaBridge:
    def __init__(
        self, settings, devices, store, *, sleep=None, jitter=None, clock=None
    ):
        self.settings, self.devices, self.store = settings, devices, store
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or random.random
        self._clock = clock or time.time
        self._status = BrokerStatus("connecting", endpoint=_endpoint(settings))
        self._task = None
        self._subscription = None
        self._client = None
        self._changed = asyncio.Event()
        self._dirty: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._configs: dict[str, bytes] = {}
        self._online = False
        self._stopping = False
        # Latest HA command per (cuid, entity); one sender task per device.
        self._commands: dict[tuple[str, str], bytes] = {}
        self._senders: dict[str, asyncio.Task] = {}

    @property
    def status(self) -> BrokerStatus:
        return self._status

    def _transition(
        self,
        state: BrokerState,
        *,
        reason: str | None = None,
        retry_at: float | None = None,
    ) -> None:
        since = self._status.since
        if state != self._status.state or since is None:
            since = self._clock()
        self._status = replace(
            self._status, state=state, since=since, reason=reason, retry_at=retry_at
        )

    async def start(self):
        if self._task is not None:
            return
        self._stopping = False
        self._transition("connecting")
        self._subscription = self.devices.subscribe(self._on_event)
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stopping = True
        self._transition("stopped")
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        # The client is closed by _run only after a best-effort offline birth.
        self._task = None
        self._subscription()
        await self._subscription.wait_closed()
        self._subscription = None
        self._dirty.clear()

    def _on_event(self, event):
        if event.kind == "forgotten":
            self.store.update(lambda state: state.pending_removals.add(event.cuid))
        self._dirty[event.cuid] = event.kind
        self._changed.set()

    async def _publish(self, publication):
        await self._client.publish(
            publication.topic,
            publication.payload,
            qos=publication.qos,
            retain=publication.retain,
        )

    async def _forget(self, cuid):
        for publication in forget_publications(cuid):
            await self._publish(publication)
        self._configs.pop(cuid, None)
        self.store.update(
            lambda snapshot: (
                snapshot.pending_removals.discard(cuid),
                snapshot.discovery_components.pop(cuid, None),
            )
        )

    async def _pending(self):
        for cuid in sorted(self.store.load().pending_removals):
            # An old retained config must be removed before a replacement is born.
            await self._forget(cuid)

    async def _publish_view(self, view, *, force_discovery=False):
        previous = self.store.load().discovery_components.get(view.cuid, set())
        publications = discovery(view, previous)
        config = publications[-1].payload
        if force_discovery or self._configs.get(view.cuid) != config:
            for publication in publications:
                await self._publish(publication)
            components = set(desired_components(view))
            self.store.update(
                lambda state: state.discovery_components.__setitem__(
                    view.cuid, components
                )
            )
            self._configs[view.cuid] = config
        await self._publish(state_publication(view))
        await self._publish(availability_publication(view))

    async def _flush(self):
        async with self._lock:
            await self._pending()
            for view in self.devices.list():
                await self._publish_view(view, force_discovery=True)

    async def _changes(self):
        while True:
            await self._changed.wait()
            self._changed.clear()
            changes, self._dirty = self._dirty, {}
            try:
                for cuid, kind in changes.items():
                    if kind == "forgotten" or self.devices.get(cuid) is None:
                        self.store.update(
                            lambda state, cuid=cuid: state.pending_removals.add(cuid)
                        )
                if self._client is not None and self._online:
                    async with self._lock:
                        await self._pending()
                        for cuid, kind in changes.items():
                            view = self.devices.get(cuid)
                            if view is not None:
                                await self._publish_view(
                                    view, force_discovery=kind in ("added", "metadata")
                                )
            except (OSError, aiomqtt.MqttError):
                self._dirty.update(changes)
                self._changed.set()
                raise
            except Exception:  # noqa: BLE001 - no producer exception graphs or secrets
                _LOG.warning("HA bridge state update failed")
                self._dirty.update(changes)
                await self._sleep(1)
                self._changed.set()

    async def _messages(self, client):
        async for message in client.messages:
            topic = str(message.topic)
            if topic == HA_STATUS_TOPIC:
                if message.payload in (b"online", b"offline"):
                    self._status = replace(
                        self._status,
                        ha_state="online"
                        if message.payload == b"online"
                        else "offline",
                        ha_seen_at=self._clock(),
                    )
                if message.payload == b"online":
                    await self._flush()
                continue
            if message.retain:
                continue
            parsed = parse_command_topic(topic)
            if parsed is None:
                continue
            cuid, entity = parsed
            if self.devices.get(cuid) is None:
                continue
            # A newer value supersedes any not-yet-sent one for the same entity, so
            # a slow hood never replays every intermediate slider or click value.
            self._commands.pop((cuid, entity), None)
            self._commands[(cuid, entity)] = bytes(message.payload)
            sender = self._senders.get(cuid)
            if sender is None or sender.done():
                self._senders[cuid] = asyncio.create_task(self._send_commands(cuid))

    async def _send_commands(self, cuid):
        while True:
            key = next((key for key in self._commands if key[0] == cuid), None)
            if key is None:
                return
            payload = self._commands.pop(key)
            view = self.devices.get(cuid)
            if view is None:
                continue
            try:
                # Recomputed at send time from the latest confirmed state.
                caps = ha_command(view, key[1], payload)
                result = await self.devices.set_caps(cuid, caps)
                if result.status in ("offline", "timeout", "rejected"):
                    _LOG.warning("HA bridge command %s", result.status)
                elif result.status != "ok":
                    _LOG.warning("HA bridge command failed")
            except Exception:  # noqa: BLE001 - safe fixed warning
                _LOG.warning("HA bridge command failed")

    async def _cancel_senders(self):
        senders, self._senders = list(self._senders.values()), {}
        self._commands.clear()
        for sender in senders:
            sender.cancel()
        await asyncio.gather(*senders, return_exceptions=True)

    async def _connection(self):
        host, port, tls = _target(self.settings)
        async with aiomqtt.Client(
            host,
            port,
            username=self.settings.username,
            password=self.settings.password,
            tls_context=ssl.create_default_context() if tls else None,
            will=aiomqtt.Will("hoodini/bridge/state", b"offline", qos=1, retain=True),
        ) as client:
            self._client = client
            self._configs.clear()
            try:
                await client.subscribe(HA_STATUS_TOPIC, qos=1)
                await client.subscribe(COMMAND_SUBSCRIPTION, qos=1)
                for view in self.devices.list():
                    await self._publish(
                        Publication(
                            availability_publication(view).topic, b"offline", True, 1
                        )
                    )
                async with self._lock:
                    await self._pending()
                    await self._publish(bridge_publication(True))
                    self._online = True
                    self._transition("connected")
                await self._flush()
                await self._messages(client)
            finally:
                if not self._stopping and self.status.state == "connected":
                    self._transition("retrying")
                # Commands queued for a lost broker session must not run later.
                await self._cancel_senders()
                if self._client is client:
                    self._client = None
                try:
                    if self._stopping:
                        for view in self.devices.list():
                            availability = availability_publication(view)
                            await client.publish(
                                availability.topic,
                                availability.payload,
                                qos=1,
                                retain=True,
                                timeout=2,
                            )
                    await client.publish(
                        "hoodini/bridge/state",
                        b"offline",
                        qos=1,
                        retain=True,
                        timeout=2,
                    )
                except (OSError, aiomqtt.MqttError, asyncio.CancelledError):
                    pass

    async def _run(self):
        delay = 1
        while True:
            self._online = False
            self._transition("connecting", reason=self.status.reason)
            changes = asyncio.create_task(self._changes())
            closed = asyncio.create_task(self._subscription.wait_closed())
            connection = asyncio.create_task(self._connection())
            try:
                done, _ = await asyncio.wait(
                    (connection, changes, closed), return_when=asyncio.FIRST_COMPLETED
                )
                if closed in done:
                    self._subscription = self.devices.subscribe(self._on_event)
                    self._changed.set()
                reason = None
                for task in (connection, changes):
                    if task in done and not task.cancelled():
                        error = task.exception()
                        if error is not None:
                            classified = broker_failure_reason(error)
                            _LOG.warning("HA bridge broker unavailable: %s", classified)
                            if reason is None:
                                reason = classified
                if not self._stopping:
                    self._transition("retrying", reason=reason)
            finally:
                for task in (connection, changes, closed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    connection, changes, closed, return_exceptions=True
                )
            if self._online:
                delay = 1
            wait = delay + self._jitter() * min(delay, 1)
            self._transition(
                "retrying", reason=self.status.reason, retry_at=self._clock() + wait
            )
            await self._sleep(wait)
            delay = min(delay * 2, 60)
