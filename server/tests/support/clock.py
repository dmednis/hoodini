"""Deterministic monotonic timer for listener deadline tests."""

import asyncio
import heapq


class Clock:
    def __init__(self) -> None:
        self.time = 0.0
        self.pending: list[tuple[float, int, Timer]] = []
        self.serial = 0

    def now(self) -> float:
        return self.time

    def call_at(self, when: float, callback) -> "Timer":
        timer = Timer(callback)
        self.serial += 1
        heapq.heappush(self.pending, (when, self.serial, timer))
        return timer

    async def advance(self, seconds: float) -> None:
        target = self.time + seconds
        while self.pending and self.pending[0][0] <= target:
            when, _, timer = heapq.heappop(self.pending)
            self.time = when
            if not timer.cancelled:
                timer.callback()
                await asyncio.sleep(0)
        self.time = target
        await asyncio.sleep(0)


class Timer:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
