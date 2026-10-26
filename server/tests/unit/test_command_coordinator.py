"""Command validation and lifecycle against real state and synthetic sessions."""

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
from hoodini.storage import AtomicStore, StoredDevice


async def session(state, cuid="hood", sid="one", caps=None, ready=True):
    await state.handle_gateway(SessionOpened(cuid, sid, {}))
    await state.handle_gateway(
        StatusReceived(cuid, sid, Status(1, -40, caps or {96: 50}))
    )
    if ready:
        await state.handle_gateway(CommandReady(cuid, sid))


async def tick():
    for _ in range(4):
        await asyncio.sleep(0)


def test_validation_precedes_readiness_and_missing_record_is_offline(tmp_path):
    async def scenario():
        state = DeviceState(AtomicStore(tmp_path))
        await state.start()
        calls = []

        async def send(*args):
            calls.append(args)
            return True

        commands = CommandCoordinator(state, send)
        await commands.start()
        assert await commands.set_caps("absent", {96: 1}) == CommandResult("offline")
        await session(state, caps={96: 50, 110: 2, 64: 1, 71: 1}, ready=False)
        for caps in ({}, {71: 1}, {96: 101}, {96: True}, {97: 1}, {64: 5}, {110: 4}):
            with pytest.raises(ValidationError):
                await commands.set_caps("hood", caps)
        assert await commands.set_caps("hood", {96: 50}) == CommandResult("offline")
        await state.handle_gateway(CommandReady("hood", "one"))
        await state.handle_gateway(SessionClosed("hood", "one", "closed"))
        assert state.get("hood").online  # presentation grace does not allow writes
        assert await commands.set_caps("hood", {96: 50}) == CommandResult("offline")
        assert calls == []
        await commands.stop()
        await state.stop()
        await state.start()
        await commands.start()
        assert await commands.set_caps("hood", {96: 50}) == CommandResult("offline")
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_ack_before_sender_returns_and_wrong_session_or_seq_are_ignored(tmp_path):
    async def scenario():
        state = DeviceState(AtomicStore(tmp_path))
        await state.start()
        await session(state)
        commands = None

        async def send(cuid, sid, seq, caps):
            assert (cuid, sid, seq, caps) == ("hood", "one", 0, {96: 20})
            commands.handle_ack(AckReceived(cuid, "old", Ack(seq, 1)))
            commands.handle_ack(AckReceived(cuid, sid, Ack(1, 1)))
            commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 2)))
            return True

        commands = CommandCoordinator(state, send)
        await commands.start()
        result = await commands.set_caps("hood", {96: 20})
        assert result == CommandResult("rejected", 0, 2)
        assert state.get("hood").caps == {96: 50}
        assert state.get("hood").last_light == 50
        commands.handle_ack(AckReceived("hood", "one", Ack(0, 1)))
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_queued_inputs_are_copied_and_profile_revalidated(tmp_path):
    async def scenario():
        state = DeviceState(AtomicStore(tmp_path))
        await state.start()
        await session(state, caps={64: 1, 96: 50})
        await state.update("hood", profile="HIDDEN_ADVANCE")
        entered = asyncio.Event()
        release = asyncio.Event()
        sent = []
        commands = None

        async def send(cuid, sid, seq, caps):
            sent.append((seq, caps))
            if seq == 0:
                entered.set()
                await release.wait()
            commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
            return True

        commands = CommandCoordinator(state, send)
        await commands.start()
        first = asyncio.create_task(commands.set_caps("hood", {96: 10}))
        await entered.wait()
        requested = {96: 20}
        second = asyncio.create_task(commands.set_caps("hood", requested))
        await tick()
        requested[96] = 99
        release.set()
        assert await first == CommandResult("ok", 0, 1)
        assert await second == CommandResult("ok", 1, 1)
        assert sent == [(0, {96: 10}), (1, {96: 20})]
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_queued_profile_change_refuses_new_send(tmp_path):
    async def scenario():
        state = DeviceState(AtomicStore(tmp_path))
        await state.start()
        await session(state, caps={64: 1})
        await state.update("hood", profile="HIDDEN_ADVANCE")
        gate = asyncio.Event()
        entered = asyncio.Event()
        sent = []
        commands = None

        async def send(cuid, sid, seq, caps):
            sent.append(seq)
            if seq == 0:
                entered.set()
                await gate.wait()
            commands.handle_ack(AckReceived(cuid, sid, Ack(seq, 1)))
            return True

        commands = CommandCoordinator(state, send)
        await commands.start()
        first = asyncio.create_task(commands.set_caps("hood", {64: 5}))
        await entered.wait()
        queued = asyncio.create_task(commands.set_caps("hood", {64: 5}))
        await tick()
        await state.update("hood", profile="GENERIC")
        gate.set()
        assert await first == CommandResult("ok", 0, 1)
        with pytest.raises(ValidationError):
            await queued
        assert sent == [0]
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_loaded_offline_row_validates_before_returning_offline(tmp_path):
    async def scenario():
        store = AtomicStore(tmp_path)
        store.update(
            lambda s: s.devices.update(
                {"hood": StoredDevice("hood", {}, None, {}, {96: 50}, {96}, None)}
            )
        )
        state = DeviceState(store)
        await state.start()

        async def send(*args):
            raise AssertionError("offline send")

        commands = CommandCoordinator(state, send)
        await commands.start()
        with pytest.raises(ValidationError):
            await commands.set_caps("hood", {71: 1})
        assert await commands.set_caps("hood", {96: 25}) == CommandResult("offline")
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())
