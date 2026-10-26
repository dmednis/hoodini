"""Sole live session authority, durable device projection and isolated subscribers."""

import asyncio
import copy
import inspect
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from hoodini.devices.models import DeviceEvent, DeviceView
from hoodini.gateway.events import (
    AckReceived,
    CommandReady,
    SessionClosed,
    SessionOpened,
    StatusReceived,
)
from hoodini.protocol.profiles import ValidationError, resolve_profile
from hoodini.storage import AtomicStore, AtomicWriteError, StoredDevice

PROFILE_UNSET = object()
# Subscribers are cooperative/nonblocking. CPU-bound or cancellation-hostile callbacks
# cannot be isolated by an asyncio task and are not supported.
SUBSCRIBER_QUEUE_SIZE = 32
SUBSCRIBER_TIMEOUT = 2.0
OFFLINE_GRACE_SECONDS = 20
CONFIRMED_RECONNECT_SECONDS = 60


def _valid_json(value, ancestors=None):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) not in (dict, list):
        return False
    if ancestors is None:
        ancestors = set()
    if id(value) in ancestors:
        return False
    ancestors.add(id(value))
    try:
        if type(value) is dict:
            return all(
                type(k) is str and _valid_json(v, ancestors) for k, v in value.items()
            )
        return all(_valid_json(item, ancestors) for item in value)
    finally:
        ancestors.remove(id(value))


@dataclass
class _Live:
    session: str | None = None
    ready: bool = False
    online: bool = False
    confirmed: bool = False
    session_confirmed: bool = False
    retired_at: float | None = None
    rssi: int | None = None
    generation: int = 0
    timer: object | None = None
    deadline: int = 0
    # Gateway retires old sessions before admitting replacements; this is bounded defence in depth.
    seen_sessions: deque[str] = field(default_factory=lambda: deque(maxlen=64))


class _Subscription:
    def __init__(self, owner, listener):
        self.owner = owner
        self.listener = listener
        self.queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.closed = asyncio.get_running_loop().create_future()
        self.worker = asyncio.create_task(self._deliver())

    async def _deliver(self):
        try:
            while not self.closed.done():
                event = await self.queue.get()
                result = self.listener(event)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, SUBSCRIBER_TIMEOUT)
        except asyncio.CancelledError:
            if not self.closed.done():
                self._close("failed")
        except TimeoutError:
            self._close("timeout")
        except Exception:  # noqa: BLE001 - sanitize arbitrary user callback errors
            self._close("failed")

    def _close(self, reason):
        if not self.closed.done():
            self.closed.set_result(reason)
            self.owner._subscribers.discard(self)
            self.queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
            if asyncio.current_task() is not self.worker:
                self.worker.cancel()

    def __call__(self):
        self._close("unsubscribed")

    async def wait_closed(self):
        reason = await asyncio.shield(self.closed)
        caller = asyncio.current_task()
        if caller is not self.worker:
            pending_cancellations = caller.cancelling()
            try:
                await asyncio.shield(self.worker)
            except asyncio.CancelledError:
                if (
                    caller.cancelling() > pending_cancellations
                    or not self.worker.cancelled()
                ):
                    raise
        return reason


class DeviceState:
    def __init__(self, store: AtomicStore, *, clock=None, epoch=None):
        self._store = store
        self._clock = clock
        self._epoch = epoch or time.time
        self._devices: dict[str, StoredDevice] = {}
        self._live: dict[str, _Live] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._fences: dict[str, tuple[object, asyncio.Task]] = {}
        self._subscribers: set[_Subscription] = set()
        self._invalidations: set[Callable[[str], None]] = set()
        self._running = False

    def _lock(self, cuid):
        return self._locks.setdefault(cuid, asyncio.Lock())

    def _now(self):
        return (
            self._clock.now()
            if self._clock is not None
            else asyncio.get_running_loop().time()
        )

    def _call_at(self, when, callback):
        if self._clock is not None:
            return self._clock.call_at(when, callback)
        return asyncio.get_running_loop().call_at(when, callback)

    def _view(self, cuid):
        record = self._devices.get(cuid)
        if record is None:
            return None
        live = self._live.get(cuid, _Live())
        raw_prf = record.info.get("PRF")
        public_info = (
            {"PRF": "PRF0182867A"}
            if type(raw_prf) is str and raw_prf == "PRF0182867A"
            else {}
        )
        return DeviceView(
            cuid,
            public_info,
            resolve_profile(record.info, record.profile_override),
            record.profile_override,
            copy.deepcopy(record.firmware),
            dict(record.caps),
            set(record.known_ids),
            live.rssi,
            record.last_seen,
            record.last_light,
            record.last_fan,
            "ready" if live.ready else "handshaking" if live.session else "none",
            live.online,
            live.session,
        )

    def get(self, cuid: str) -> DeviceView | None:
        return self._view(cuid)

    def list(self) -> list[DeviceView]:
        return [self._view(cuid) for cuid in self._devices]

    def generation(self, cuid: str) -> int:
        return self._live.setdefault(cuid, _Live()).generation

    def command_snapshot(self, cuid: str) -> tuple[DeviceView | None, int]:
        return self.get(cuid), self.generation(cuid)

    def is_command_current(
        self, cuid: str, connection_id: str, generation: int
    ) -> bool:
        live = self._live.get(cuid)
        return bool(
            self._running
            and cuid not in self._fences
            and live is not None
            and live.ready
            and live.session == connection_id
            and live.generation == generation
            and cuid in self._devices
        )

    def subscribe_invalidations(self, listener: Callable[[str], None]):
        self._invalidations.add(listener)
        return lambda: self._invalidations.discard(listener)

    def subscribe(self, listener: Callable[[DeviceEvent], object]):
        subscription = _Subscription(self, listener)
        self._subscribers.add(subscription)
        return subscription

    def _emit(self, kind, cuid):
        for subscriber in tuple(self._subscribers):
            if subscriber.queue.full():
                subscriber._close("overflow")
            else:
                subscriber.queue.put_nowait(
                    DeviceEvent(
                        kind, cuid, None if kind == "forgotten" else self._view(cuid)
                    )
                )

    def _invalidate(self, cuid):
        self._live.setdefault(cuid, _Live()).generation += 1
        for listener in tuple(self._invalidations):
            listener(cuid)

    def _cancel_timer(self, live):
        live.deadline += 1
        if live.timer is not None:
            live.timer.cancel()
            live.timer = None

    def _retire(self, cuid):
        live = self._live.setdefault(cuid, _Live())
        if live.session is not None:
            if live.session not in live.seen_sessions:
                live.seen_sessions.append(live.session)
            self._invalidate(cuid)
            live.retired_at = self._now()
        live.session = None
        live.ready = False
        live.rssi = None
        if live.online and live.timer is None:
            live.deadline += 1
            token = live.deadline

            def expire():
                if (
                    self._running
                    and self._live.get(cuid) is live
                    and live.deadline == token
                    and not live.ready
                ):
                    live.timer = None
                    live.online = False
                    if cuid in self._devices:
                        self._emit("availability", cuid)

            live.timer = self._call_at(self._now() + OFFLINE_GRACE_SECONDS, expire)

    async def start(self):
        snapshot = self._store.load()
        invalid_profile = False
        for record in snapshot.devices.values():
            try:
                resolve_profile(record.info, record.profile_override)
            except ValidationError:
                invalid_profile = True
                break
        if invalid_profile:
            raise ValidationError("invalid device profile")
        self._devices = snapshot.devices
        self._live = {cuid: _Live() for cuid in self._devices}
        self._running = True

    async def stop(self):
        self._running = False
        for cuid, live in self._live.items():
            self._invalidate(cuid)
            self._cancel_timer(live)
            live.session = None
            live.ready = False
            live.online = False
            live.rssi = None
        subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber._close("stopped")
        await asyncio.gather(*(s.worker for s in subscribers), return_exceptions=True)

    def _write(self, cuid, change, kind):
        proposed = None

        def mutate(snapshot):
            nonlocal proposed
            change(snapshot)
            proposed = copy.deepcopy(snapshot.devices.get(cuid))

        try:
            self._store.update(mutate)
        except AtomicWriteError as exc:
            if exc.committed:
                self._project(cuid, proposed, kind)
            raise
        self._project(cuid, proposed, kind)

    def _project(self, cuid, record, kind):
        if record is None:
            self._devices.pop(cuid, None)
            live = self._live.get(cuid)
            if live is not None:
                self._cancel_timer(live)
                live.session = None
                live.ready = False
                live.online = False
                live.confirmed = False
                live.session_confirmed = False
                live.retired_at = None
                live.rssi = None
                # Retain one bounded fence per CUID to reject a forgotten session.
        else:
            self._devices[cuid] = record
        if kind is not None:
            self._emit(kind, cuid)

    async def handle_gateway(self, event):
        cuid = event.cuid
        if not self._running or cuid in self._fences or isinstance(event, AckReceived):
            return
        # Retire synchronously before waiting behind any persistence lock.
        if isinstance(event, SessionClosed):
            live = self._live.get(cuid)
            if live is None or live.session != event.session_id:
                return
            self._retire(cuid)
            if cuid in self._devices:
                self._emit("availability", cuid)
            return
        if isinstance(event, SessionOpened):
            live = self._live.setdefault(cuid, _Live())
            if (
                event.session_id == live.session
                or event.session_id in live.seen_sessions
            ):
                return
            if live.session is not None:
                self._retire(cuid)
            live.session_confirmed = False
            self._invalidate(cuid)
            live.session = event.session_id
            live.ready = False
            live.rssi = None
        async with self._lock(cuid):
            if cuid in self._fences or not self._running:
                return
            live = self._live.get(cuid)
            if live is None or live.session != event.session_id:
                return
            if isinstance(event, SessionOpened):
                existed = cuid in self._devices

                def change(snapshot):
                    record = snapshot.devices.setdefault(
                        cuid, StoredDevice(cuid, {}, None, {}, {}, set(), None)
                    )
                    record.firmware = dict(event.firmware)

                self._write(cuid, change, "metadata" if existed else "added")
            elif isinstance(event, CommandReady):
                if not live.ready:
                    live.ready = True
                    live.confirmed = bool(
                        live.session_confirmed
                        or (
                            live.confirmed
                            and live.retired_at is not None
                            and self._now() - live.retired_at
                            <= CONFIRMED_RECONNECT_SECONDS
                        )
                    )
                    live.online = live.confirmed
                    self._cancel_timer(live)
                    if cuid in self._devices:
                        self._emit("availability", cuid)
            elif isinstance(event, StatusReceived) and cuid in self._devices:
                caps = dict(event.status.caps)
                seen = self._epoch()

                def change(snapshot):
                    record = snapshot.devices[cuid]
                    record.caps.update(caps)
                    record.known_ids.update(caps)
                    record.last_seen = seen
                    if 1 <= caps.get(96, 0) <= 100:
                        record.last_light = caps[96]
                    if 1 <= caps.get(110, 0) <= 3:
                        record.last_fan = caps[110]

                previous_rssi = live.rssi
                previously_confirmed = live.confirmed
                previous_session_confirmed = live.session_confirmed
                previously_online = live.online
                live.rssi = event.status.rssi
                live.confirmed = True
                live.session_confirmed = True
                if live.ready:
                    live.online = True
                try:
                    self._write(cuid, change, "state")
                except AtomicWriteError as exc:
                    if not exc.committed:
                        live.rssi = previous_rssi
                        live.confirmed = previously_confirmed
                        live.session_confirmed = previous_session_confirmed
                        live.online = previously_online
                    elif live.online != previously_online:
                        self._cancel_timer(live)
                        self._emit("availability", cuid)
                    raise
                except Exception:
                    live.rssi = previous_rssi
                    live.confirmed = previously_confirmed
                    live.session_confirmed = previous_session_confirmed
                    live.online = previously_online
                    raise
                if live.online != previously_online:
                    self._cancel_timer(live)
                    self._emit("availability", cuid)

    async def update(
        self, cuid: str, info: dict | None = None, profile=PROFILE_UNSET
    ) -> DeviceView:
        if cuid in self._fences:
            raise KeyError(cuid)
        async with self._lock(cuid):
            if cuid in self._fences or cuid not in self._devices:
                raise KeyError(cuid)
            if info is not None:
                try:
                    valid = type(info) is dict and _valid_json(info)
                except RecursionError:
                    valid = False
                if not valid:
                    raise ValidationError("invalid device info") from None
            merged = copy.deepcopy(self._devices[cuid].info)
            if info is not None:
                merged.update(copy.deepcopy(info))
            override = (
                self._devices[cuid].profile_override
                if profile is PROFILE_UNSET
                else profile
            )
            try:
                resolve_profile(merged, override)
                invalid_profile = False
            except ValidationError:
                invalid_profile = True
            if invalid_profile:
                raise ValidationError("invalid device profile")
            current = self._devices[cuid]
            if merged == current.info and override == current.profile_override:
                return self.get(cuid)

            def change(snapshot):
                record = snapshot.devices[cuid]
                record.info = merged
                record.profile_override = override

            self._write(cuid, change, "metadata")
            return self.get(cuid)

    def begin_forget(self, cuid: str) -> object | None:
        if cuid in self._fences:
            return None
        token = object()
        self._fences[cuid] = (token, asyncio.current_task())
        self._invalidate(cuid)
        self._retire(cuid)
        return token

    def end_forget(self, cuid: str, token: object) -> None:
        fence = self._fences.get(cuid)
        if (
            fence is not None
            and fence[0] is token
            and fence[1] is asyncio.current_task()
        ):
            del self._fences[cuid]

    def _require_fence(self, cuid):
        if (
            cuid not in self._fences
            or self._fences[cuid][1] is not asyncio.current_task()
        ):
            raise RuntimeError("forget operation not owned")

    async def prepare_forget(self, cuid: str) -> None:
        self._require_fence(cuid)
        async with self._lock(cuid):
            self._require_fence(cuid)
            if cuid not in self._devices:
                raise KeyError(cuid)
            self._write(
                cuid, lambda snapshot: snapshot.pending_removals.add(cuid), None
            )

    async def finish_forget(self, cuid: str) -> None:
        self._require_fence(cuid)
        async with self._lock(cuid):
            self._require_fence(cuid)
            self._write(cuid, lambda snapshot: snapshot.devices.pop(cuid), "forgotten")
