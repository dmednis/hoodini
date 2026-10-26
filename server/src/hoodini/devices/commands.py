"""Acknowledged capability command coordination."""

import asyncio
from collections import defaultdict

from hoodini.devices.models import CommandResult
from hoodini.protocol.profiles import validate_caps


async def _drain(task):
    """Cancel owned work and finish its cooperative cleanup despite caller cancellation."""
    if not task.done():
        task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
    if not task.cancelled():
        task.exception()  # retrieve an error overtaken by retirement or timeout


class CommandCoordinator:
    def __init__(self, state, send, *, clock=None):
        self._state = state
        self._send = send
        self._clock = clock
        self._locks = {}
        self._next_seq = {}
        self._requests = defaultdict(set)
        self._waiters = {}
        self._unsubscribe = None

    async def start(self):
        if self._unsubscribe is None:
            self._unsubscribe = self._state.subscribe_invalidations(self.invalidate)

    async def stop(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for cuid in tuple(self._requests):
            self.invalidate(cuid)
        while self._requests:
            await asyncio.sleep(0)

    def invalidate(self, cuid):
        for retired in tuple(self._requests.get(cuid, ())):
            if not retired.done():
                retired.set_result(None)

    def handle_ack(self, event):
        key = (event.cuid, event.session_id, event.ack.seq)
        waiter = self._waiters.get(key)
        if waiter is not None and not waiter.done():
            waiter.set_result(event.ack.result)

    async def set_caps(self, cuid, caps) -> CommandResult:
        view, generation = self._state.command_snapshot(cuid)
        if view is None:
            return CommandResult("offline")
        requested = validate_caps(view.profile, view.known_ids, dict(caps))
        sid = view.connection_id
        if (
            self._unsubscribe is None
            or sid is None
            or not self._state.is_command_current(cuid, sid, generation)
        ):
            return CommandResult("offline")

        loop = asyncio.get_running_loop()
        retired = loop.create_future()
        self._requests[cuid].add(retired)
        lock = self._locks.setdefault(cuid, asyncio.Lock())
        acquire = asyncio.create_task(lock.acquire())
        try:
            await asyncio.wait((acquire, retired), return_when=asyncio.FIRST_COMPLETED)
            if (
                retired.done()
                or self._unsubscribe is None
                or not self._state.is_command_current(cuid, sid, generation)
            ):
                return CommandResult("offline")
            return await self._attempt(cuid, sid, generation, requested, retired)
        finally:
            if not acquire.done():
                await _drain(acquire)
            # A cancelled caller may have lost its wakeup just after acquire
            # completed; task result, not the caller's local flag, owns the lock.
            if not acquire.cancelled() and acquire.exception() is None:
                lock.release()
            requests = self._requests[cuid]
            requests.discard(retired)
            if not requests:
                del self._requests[cuid]
                self._locks.pop(cuid, None)
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError

    async def _attempt(self, cuid, sid, generation, caps, retired):
        loop = asyncio.get_running_loop()
        expired = loop.create_future()
        allocation = []
        timer = None
        key = None

        async def run():
            nonlocal timer, key
            # No task switch is permitted between this final authorization and
            # directly entering the sender coroutine. The send budget starts here.
            if (
                retired.done()
                or self._unsubscribe is None
                or not self._state.is_command_current(cuid, sid, generation)
            ):
                return CommandResult("offline")
            now = self._clock.now if self._clock is not None else loop.time
            call_at = self._clock.call_at if self._clock is not None else loop.call_at
            timer = call_at(
                now() + 3,
                lambda: expired.set_result(None) if not expired.done() else None,
            )
            # Scheduler callbacks may reenter state synchronously; the last
            # check and sequence allocation follow timer registration.
            current, _ = self._state.command_snapshot(cuid)
            if current is None:
                return CommandResult("offline")
            validate_caps(current.profile, current.known_ids, caps)
            if (
                retired.done()
                or self._unsubscribe is None
                or not self._state.is_command_current(cuid, sid, generation)
            ):
                return CommandResult("offline")
            seq = self._next_seq.get(cuid, 0)
            self._next_seq[cuid] = (seq + 1) % 256
            allocation.append(seq)
            ack = loop.create_future()
            key = (cuid, sid, seq)
            self._waiters[key] = ack
            sent = await self._send(cuid, sid, seq, caps)
            if not sent:
                return CommandResult("offline", seq)
            result = await ack
            if retired.done() or not self._state.is_command_current(
                cuid, sid, generation
            ):
                return CommandResult("offline", seq)
            return CommandResult("ok" if result == 1 else "rejected", seq, result)

        runner = asyncio.create_task(run())
        try:
            await asyncio.wait(
                (runner, retired, expired), return_when=asyncio.FIRST_COMPLETED
            )
            seq = allocation[0] if allocation else None
            if (
                retired.done()
                or self._unsubscribe is None
                or (
                    seq is not None
                    and not self._state.is_command_current(cuid, sid, generation)
                )
            ):
                return CommandResult("offline", seq)
            if expired.done():
                return CommandResult("timeout", seq)
            return runner.result()
        finally:
            await _drain(runner)
            if timer is not None:
                timer.cancel()
            if key is not None:
                self._waiters.pop(key, None)
