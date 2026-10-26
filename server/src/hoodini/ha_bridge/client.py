"""Background MQTT owner for retained Home Assistant discovery and controls."""

import asyncio
import logging
import random
import ssl
from urllib.parse import urlsplit

import aiomqtt

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
        name = str(rc)
        if name.replace(" ", "").isalnum():
            return f"broker rejected the connection ({name})"
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


class HaBridge:
    def __init__(self, settings, devices, store, *, sleep=None, jitter=None):
        self.settings, self.devices, self.store = settings, devices, store
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or random.random
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

    async def start(self):
        if self._task is not None:
            return
        self._stopping = False
        self._subscription = self.devices.subscribe(self._on_event)
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task is None:
            return
        self._stopping = True
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
        url = urlsplit(self.settings.url)
        if url.scheme not in ("mqtt", "mqtts") or not url.hostname:
            raise ValueError("invalid MQTT bridge URL")
        tls = (
            ssl.create_default_context()
            if self.settings.tls or url.scheme == "mqtts"
            else None
        )
        async with aiomqtt.Client(
            url.hostname,
            url.port or (8883 if tls else 1883),
            username=self.settings.username,
            password=self.settings.password,
            tls_context=tls,
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
                await self._flush()
                await self._messages(client)
            finally:
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
                if connection in done and connection.exception() is not None:
                    _LOG.warning(
                        "HA bridge broker unavailable: %s",
                        broker_failure_reason(connection.exception()),
                    )
                if changes in done and changes.exception() is not None:
                    _LOG.warning(
                        "HA bridge broker unavailable: %s",
                        broker_failure_reason(changes.exception()),
                    )
            finally:
                for task in (connection, changes, closed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    connection, changes, closed, return_exceptions=True
                )
            if self._online:
                delay = 1
            await self._sleep(delay + self._jitter() * min(delay, 1))
            delay = min(delay * 2, 60)
