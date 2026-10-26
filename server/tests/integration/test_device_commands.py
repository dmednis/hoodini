"""Scripted sender integration with actual device state and profile validation."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hoodini.devices.commands import CommandCoordinator
from hoodini.devices.models import CommandResult
from hoodini.devices.state import DeviceState
from hoodini.gateway.events import (
    AckReceived,
    CommandReady,
    SessionClosed,
    SessionOpened,
    StatusReceived,
)
from hoodini.protocol.frames import Ack, Status
from hoodini.protocol.profiles import ValidationError
from hoodini.storage import AtomicStore
from support.clock import Clock


async def ready(state, cuid="hood", sid="one"):
    await state.handle_gateway(SessionOpened(cuid, sid, {}))
    await state.handle_gateway(StatusReceived(cuid, sid, Status(1, -40, {96: 50})))
    await state.handle_gateway(CommandReady(cuid, sid))


async def tick():
    for _ in range(5):
        await asyncio.sleep(0)


def test_one_deadline_includes_blocked_send_and_cleans_owned_task(tmp_path):
    async def scenario():
        clock = Clock()
        state = DeviceState(AtomicStore(tmp_path), clock=clock)
        await state.start()
        await ready(state)
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def send(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        task = asyncio.create_task(commands.set_caps("hood", {96: 20}))
        await entered.wait()
        await clock.advance(2.9)
        assert not task.done()
        await clock.advance(0.1)
        assert await task == CommandResult("timeout", 0)
        assert cleaned.is_set()
        commands.handle_ack(AckReceived("hood", "one", Ack(0, 1)))
        await commands.stop()
        await state.stop()
        assert not any(not timer.cancelled for _, _, timer in clock.pending)

    asyncio.run(scenario())


def test_early_ack_false_wins_and_slow_send_has_no_second_budget(tmp_path):
    async def scenario():
        clock = Clock()
        state = DeviceState(AtomicStore(tmp_path), clock=clock)
        await state.start()
        await ready(state)
        release = asyncio.Event()
        entered = asyncio.Event()
        commands = None

        async def send(cuid, sid, seq, caps):
            commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
            entered.set()
            await release.wait()
            return False

        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        task = asyncio.create_task(commands.set_caps("hood", {96: 10}))
        await entered.wait()
        await clock.advance(2.9)
        release.set()
        assert await task == CommandResult("offline", 0)
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_invalidation_retires_active_and_queued_without_retiring_other_cuid(tmp_path):
    async def scenario():
        state = DeviceState(AtomicStore(tmp_path))
        await state.start()
        await ready(state)
        await ready(state, "other")
        entered = asyncio.Event()
        canceled = asyncio.Event()
        commands = None

        async def send(cuid, sid, seq, caps):
            if cuid == "hood":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    canceled.set()
            commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
            return True

        commands = CommandCoordinator(state, send)
        await commands.start()
        active = asyncio.create_task(commands.set_caps("hood", {96: 10}))
        await entered.wait()
        queued = asyncio.create_task(commands.set_caps("hood", {96: 20}))
        await tick()
        await state.handle_gateway(SessionClosed("hood", "one", "closed"))
        await ready(state, "hood", "two")
        assert await active == CommandResult("offline", 0)
        assert await queued == CommandResult("offline")
        assert canceled.is_set()
        assert await commands.set_caps("other", {96: 10}) == CommandResult("ok", 0, 1)
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_caller_cancellation_and_stop_release_waiters_and_restart(tmp_path):
    async def scenario():
        clock = Clock()
        state = DeviceState(AtomicStore(tmp_path), clock=clock)
        await state.start()
        await ready(state)
        entered = asyncio.Event()
        canceled = asyncio.Event()
        commands = None

        async def send(cuid, sid, seq, caps):
            entered.set()
            if caps[96] == 40:
                commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
                return True
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        active = asyncio.create_task(commands.set_caps("hood", {96: 10}))
        await entered.wait()
        queued = asyncio.create_task(commands.set_caps("hood", {96: 20}))
        await tick()
        queued.cancel()
        try:
            await queued
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("caller cancellation swallowed")
        await commands.stop()
        assert await active == CommandResult("offline", 0)
        assert canceled.is_set()
        entered.clear()
        canceled.clear()
        await commands.start()
        replacement = asyncio.create_task(commands.set_caps("hood", {96: 30}))
        await entered.wait()  # a fresh sender actually entered, not the first event
        replacement.cancel()
        try:
            await replacement
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("active cancellation swallowed")
        assert canceled.is_set()
        assert await commands.set_caps("hood", {96: 40}) == CommandResult("ok", 2, 1)
        await commands.stop()
        await state.stop()
        assert not any(not timer.cancelled for _, _, timer in clock.pending)

    asyncio.run(scenario())


def test_queued_command_budget_starts_after_lock_and_ack_uses_remaining_time(tmp_path):
    async def scenario():
        clock = Clock()
        state = DeviceState(AtomicStore(tmp_path), clock=clock)
        await state.start()
        await ready(state)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()
        commands = None

        async def send(cuid, sid, seq, caps):
            if seq == 0:
                first_entered.set()
                await release_first.wait()
                commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
            else:
                second_entered.set()
            return True

        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        first = asyncio.create_task(commands.set_caps("hood", {96: 10}))
        await first_entered.wait()
        queued = asyncio.create_task(commands.set_caps("hood", {96: 20}))
        await tick()
        await clock.advance(2.9)
        assert not queued.done()
        release_first.set()
        assert await first == CommandResult("ok", 0, 1)
        await second_entered.wait()
        await clock.advance(2.9)
        assert not queued.done()
        await clock.advance(0.11)  # cross the floating-point deadline
        assert await queued == CommandResult("timeout", 1)
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_forget_begin_retires_active_and_queued_without_new_send(tmp_path):
    async def scenario():
        state = DeviceState(AtomicStore(tmp_path))
        await state.start()
        await ready(state)
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        sent = []

        async def send(cuid, sid, seq, caps):
            sent.append(seq)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        commands = CommandCoordinator(state, send)
        await commands.start()
        active = asyncio.create_task(commands.set_caps("hood", {96: 10}))
        await entered.wait()
        queued = asyncio.create_task(commands.set_caps("hood", {96: 20}))
        await tick()
        token = state.begin_forget("hood")
        assert token is not None
        assert await active == CommandResult("offline", 0)
        assert await queued == CommandResult("offline")
        assert cleaned.is_set()
        assert sent == [0]
        state.end_forget("hood", token)
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_sender_entry_never_uses_retired_generation_from_scheduling_gap(tmp_path):
    async def scenario():
        clock = Clock()
        state = DeviceState(AtomicStore(tmp_path), clock=clock)
        await state.start()
        await ready(state)
        entered = []
        retired = asyncio.Event()
        release = asyncio.Event()

        async def send(cuid, sid, seq, caps):
            entered.append(state.is_command_current(cuid, sid, generation))
            return False

        _, generation = state.command_snapshot("hood")
        original_call_at = clock.call_at

        def retire_at_schedule(when, callback):
            async def retire():
                token = state.begin_forget("hood")
                retired.set()
                try:
                    await release.wait()
                finally:
                    state.end_forget("hood", token)

            asyncio.create_task(retire())
            return original_call_at(when, callback)

        clock.call_at = retire_at_schedule
        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        assert (await commands.set_caps("hood", {96: 20})).status == "offline"
        assert not entered or entered == [True]
        await retired.wait()
        release.set()
        await tick()
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_sender_entry_never_uses_revoked_profile_from_scheduling_gap(tmp_path):
    async def scenario():
        clock = Clock()
        state = DeviceState(AtomicStore(tmp_path), clock=clock)
        await state.start()
        await ready(state)
        await state.handle_gateway(
            StatusReceived("hood", "one", Status(2, -40, {64: 1}))
        )
        await state.update("hood", profile="HIDDEN_ADVANCE")
        entered = []
        changed = asyncio.Event()
        commands = None

        async def send(cuid, sid, seq, caps):
            entered.append(state.get(cuid).profile.key)
            commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
            return True

        original_call_at = clock.call_at

        def change_at_schedule(when, callback):
            async def change():
                await state.update("hood", profile="GENERIC")
                changed.set()

            asyncio.create_task(change())
            return original_call_at(when, callback)

        clock.call_at = change_at_schedule
        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        try:
            await commands.set_caps("hood", {64: 5})
        except ValidationError:
            pass  # profile changed before the direct sender boundary
        await changed.wait()
        assert not entered or entered == ["HIDDEN_ADVANCE"]
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("retirement", ["timeout", "disconnect"])
def test_external_cancel_while_internal_send_cleanup_is_pending_propagates(
    tmp_path, retirement
):
    async def scenario():
        clock = Clock()
        state = DeviceState(AtomicStore(tmp_path), clock=clock)
        await state.start()
        await ready(state)
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def send(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()

        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        caller = asyncio.create_task(commands.set_caps("hood", {96: 10}))
        await entered.wait()
        if retirement == "timeout":
            await clock.advance(3)
        else:
            await state.handle_gateway(SessionClosed("hood", "one", "closed"))
        await cleaning.wait()
        caller.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished.is_set()
        await commands.stop()
        await state.stop()  # also releases State's presentation-grace timer
        assert not any(not timer.cancelled for _, _, timer in clock.pending)

    asyncio.run(scenario())


def test_sequence_wrap_via_real_commands(tmp_path):
    async def scenario():
        state = DeviceState(AtomicStore(tmp_path))
        await state.start()
        await ready(state)
        commands = None

        async def send(cuid, sid, seq, caps):
            commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
            return True

        commands = CommandCoordinator(state, send)
        await commands.start()
        for n in range(258):
            assert await commands.set_caps("hood", {96: 10}) == CommandResult(
                "ok", n % 256, 1
            )
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())
