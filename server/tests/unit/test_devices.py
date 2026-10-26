"""State projection contracts on synthetic gateway sessions and real local storage."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hoodini.devices import PROFILE_UNSET, DeviceState
from hoodini.devices.commands import CommandCoordinator
from hoodini.gateway.events import (
    AckReceived,
    CommandReady,
    SessionClosed,
    SessionOpened,
    StatusReceived,
)
from hoodini.protocol.frames import Ack, Status
from hoodini.protocol.profiles import ValidationError
from hoodini.storage import AtomicStore, AtomicWriteError, StoredDevice
from support.clock import Clock


@pytest.fixture
def environment(tmp_path):
    clock = Clock()
    store = AtomicStore(tmp_path)
    state = DeviceState(store, clock=clock, epoch=lambda: 1234.0)
    return state, store, clock


async def opened(state, session="one", firmware=None):
    await state.handle_gateway(SessionOpened("hood", session, firmware or {}))


async def status(state, session="one", caps=None, rssi=-42):
    await state.handle_gateway(
        StatusReceived("hood", session, Status(8, rssi, caps or {}))
    )


def test_status_merge_known_union_persists_without_live_session(environment):
    async def scenario():
        state, store, _ = environment
        await state.start()
        await opened(state)
        await status(state, caps={96: 70})
        await status(state, caps={110: 2})
        view = state.get("hood")
        assert view.caps == {96: 70, 110: 2}
        assert view.known_ids == {96, 110}
        assert view.last_seen == 1234.0
        assert store.load().devices["hood"].known_ids == {96, 110}
        await state.stop()
        await state.start()
        view = state.get("hood")
        assert (view.session_state, view.online, view.connection_id, view.rssi) == (
            "none",
            False,
            None,
            None,
        )
        assert view.caps == {96: 70, 110: 2}
        await state.stop()

    asyncio.run(scenario())


def test_ready_grace_reconnect_and_stale_sessions(environment):
    async def scenario():
        state, _, clock = environment
        await state.start()
        await opened(state)
        assert state.get("hood").session_state == "handshaking"
        await state.handle_gateway(CommandReady("hood", "one"))
        await status(state, caps={96: 70})
        generation = state.generation("hood")
        assert state.is_command_current("hood", "one", generation)
        await state.handle_gateway(SessionClosed("hood", "one", "closed"))
        assert not state.is_command_current("hood", "one", generation)
        assert state.get("hood").online
        await clock.advance(11)
        await opened(state, "two")
        await state.handle_gateway(SessionClosed("hood", "one", "late"))
        await status(state, "one", {96: 99})
        await clock.advance(9)
        assert not state.get("hood").online
        await state.handle_gateway(CommandReady("hood", "two"))
        assert state.get("hood").online
        await state.handle_gateway(SessionClosed("hood", "two", "closed"))
        await clock.advance(11)
        await opened(state, "three")
        await state.handle_gateway(CommandReady("hood", "three"))
        await clock.advance(10)
        assert state.get("hood").online
        await state.stop()

    asyncio.run(scenario())


def test_persisted_caps_are_offline_until_current_status_but_commands_still_work(
    environment,
):
    async def scenario():
        state, store, clock = environment
        store.update(
            lambda snapshot: snapshot.devices.update(
                {"hood": StoredDevice("hood", {}, None, {}, {96: 100}, {96}, 1234.0)}
            )
        )
        await state.start()
        events = []
        subscription = state.subscribe(events.append)
        sent = []

        async def send(cuid, session, seq, caps):
            sent.append((cuid, session, seq, caps))
            return True

        commands = CommandCoordinator(state, send, clock=clock)
        await commands.start()
        await opened(state)
        await state.handle_gateway(CommandReady("hood", "one"))
        view = state.get("hood")
        assert (view.online, view.session_state, view.caps) == (
            False,
            "ready",
            {96: 100},
        )
        command = asyncio.create_task(commands.set_caps("hood", {96: 0}))
        for _ in range(10):
            await asyncio.sleep(0)
            if sent:
                break
        assert sent == [("hood", "one", 0, {96: 0})]
        commands.handle_ack(AckReceived("hood", "one", Ack(0, 1)))
        assert (await command).status == "ok"
        await clock.advance(20)
        assert not state.get("hood").online
        await status(state, caps={96: 0})
        assert state.get("hood").online
        await asyncio.sleep(0)
        assert [e.kind for e in events] == [
            "metadata",
            "availability",
            "state",
            "availability",
        ]
        subscription()
        await subscription.wait_closed()
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_confirmed_reconnect_inside_and_beyond_grace_within_60_seconds(environment):
    async def scenario():
        state, _, clock = environment
        await state.start()
        await opened(state)
        await state.handle_gateway(CommandReady("hood", "one"))
        await status(state, caps={96: 70})
        await state.handle_gateway(SessionClosed("hood", "one", "closed"))
        await clock.advance(10)
        await opened(state, "two")
        await state.handle_gateway(CommandReady("hood", "two"))
        assert state.get("hood").online
        await state.handle_gateway(SessionClosed("hood", "two", "closed"))
        await clock.advance(60)
        assert not state.get("hood").online
        await opened(state, "three")
        await state.handle_gateway(CommandReady("hood", "three"))
        assert state.get("hood").online
        await state.stop()

    asyncio.run(scenario())


def test_confirmed_reconnect_after_60_seconds_requires_new_status(environment):
    async def scenario():
        state, _, clock = environment
        await state.start()
        await opened(state)
        await state.handle_gateway(CommandReady("hood", "one"))
        await status(state, caps={96: 70})
        await state.handle_gateway(SessionClosed("hood", "one", "closed"))
        await clock.advance(60.1)
        await opened(state, "two")
        await state.handle_gateway(CommandReady("hood", "two"))
        assert state.get("hood").session_state == "ready"
        assert not state.get("hood").online
        await clock.advance(20)
        assert not state.get("hood").online
        await status(state, "one", {96: 99})
        assert state.get("hood").caps == {96: 70}
        assert not state.get("hood").online
        await status(state, "two", {110: 2})
        assert state.get("hood").caps == {96: 70, 110: 2}
        assert state.get("hood").online
        await state.stop()

    asyncio.run(scenario())


def test_status_before_ready_confirms_current_session(environment):
    async def scenario():
        state, _, clock = environment
        await state.start()
        await opened(state)
        await status(state, caps={96: 70})
        assert not state.get("hood").online
        await clock.advance(61)
        await state.handle_gateway(CommandReady("hood", "one"))
        assert state.get("hood").online
        await state.stop()

    asyncio.run(scenario())


def test_never_reported_device_stays_offline_and_known_ids_gate_commands(environment):
    async def scenario():
        state, _, clock = environment
        await state.start()
        await opened(state)
        await state.handle_gateway(CommandReady("hood", "one"))
        assert not state.get("hood").online
        assert state.get("hood").known_ids == set()
        commands = CommandCoordinator(state, lambda *_: None, clock=clock)
        await commands.start()
        with pytest.raises(ValidationError):
            await commands.set_caps("hood", {96: 1})
        await status(state, caps={96: 1})
        assert state.get("hood").online
        await commands.stop()
        await state.stop()

    asyncio.run(scenario())


def test_metadata_override_and_confirmed_memories_are_detached(environment):
    async def scenario():
        state, store, _ = environment
        await state.start()
        await opened(state, firmware={1: 27})
        await status(state, caps={96: 75, 110: 2})
        await status(state, caps={96: 0, 110: 0})
        await state.handle_gateway(AckReceived("hood", "one", Ack(1, 1)))
        view = await state.update(
            "hood", info={"PRF": "PRF0182867A", "nested": {"x": [1]}}
        )
        assert view.profile.key == "HIDDEN_ADVANCE"
        assert (view.last_light, view.last_fan) == (75, 2)
        view.info["PRF"] = "changed"
        view.firmware[1] = 99
        assert state.get("hood").info == {"PRF": "PRF0182867A"}
        assert store.load().devices["hood"].info["nested"] == {"x": [1]}
        assert state.get("hood").firmware == {1: 27}
        await state.update("hood", profile="GENERIC")
        await state.update("hood", info={"extra": True}, profile=PROFILE_UNSET)
        assert state.get("hood").profile_override == "GENERIC"
        await state.update("hood", profile=None)
        assert state.get("hood").profile.key == "HIDDEN_ADVANCE"
        assert store.load().devices["hood"].last_light == 75
        with pytest.raises(KeyError):
            await state.update("missing")
        with pytest.raises(ValidationError):
            await state.update("hood", profile="unknown")
        await state.stop()

    asyncio.run(scenario())


def test_forget_intent_and_finalization_preserve_bridge_bookkeeping(environment):
    async def scenario():
        state, store, _ = environment
        await state.start()
        await opened(state)
        await status(state, caps={96: 70})
        store.update(
            lambda snapshot: snapshot.discovery_components.update({"hood": {"light"}})
        )
        token = state.begin_forget("hood")
        assert token is not None
        assert not state.is_command_current("hood", "one", state.generation("hood"))
        await state.prepare_forget("hood")
        assert "hood" in store.load().pending_removals
        assert state.get("hood") is not None
        await state.finish_forget("hood")
        assert state.get("hood") is None
        assert store.load().pending_removals == {"hood"}
        assert store.load().discovery_components == {"hood": {"light"}}
        state.end_forget("hood", token)
        await opened(state, "new")
        assert state.get("hood") is not None
        await state.stop()

    asyncio.run(scenario())


def test_observers_are_isolated_and_signal_closure(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        waiting = asyncio.Event()
        healthy = []

        async def slow(_event):
            await waiting.wait()

        blocked = state.subscribe(slow)
        continuing = state.subscribe(healthy.append)
        await asyncio.wait_for(opened(state), 0.5)
        await asyncio.sleep(0)
        assert [event.kind for event in healthy] == ["added"]
        blocked()
        assert await blocked.wait_closed() == "unsubscribed"
        continuing()
        assert await continuing.wait_closed() == "unsubscribed"
        await state.stop()

    asyncio.run(scenario())


def test_invalid_override_rejects_entire_loaded_registry(tmp_path):
    async def scenario():
        store = AtomicStore(tmp_path)
        store.update(
            lambda s: s.devices.update(
                {"hood": StoredDevice("hood", {}, "unknown", {}, {}, set(), None)}
            )
        )
        state = DeviceState(store)
        with pytest.raises(ValidationError):
            await state.start()
        assert state.list() == []

    asyncio.run(scenario())


def test_old_opened_cannot_replace_newer_session(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        await opened(state, "old")
        await opened(state, "new")
        await opened(state, "old")
        assert state.get("hood").connection_id == "new"
        await state.stop()

    asyncio.run(scenario())


def test_subscription_overflow_and_timeout_report_fixed_reasons(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        gate = asyncio.Event()

        async def blocked(_event):
            await gate.wait()

        slow = state.subscribe(blocked)
        await opened(state)
        for _ in range(40):
            await status(state, caps={96: 3})
        assert await slow.wait_closed() == "overflow"
        assert not state._subscribers
        await state.stop()

    asyncio.run(scenario())


def test_forget_fence_rejects_admission_and_does_not_release_other_owner(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        await opened(state)
        token = state.begin_forget("hood")
        assert state.begin_forget("hood") is None
        state.end_forget("hood", object())
        await opened(state, "replacement")
        assert state.get("hood").connection_id is None
        state.end_forget("hood", token)
        await opened(state, "replacement")
        assert state.get("hood").connection_id == "replacement"
        await state.stop()

    asyncio.run(scenario())


def test_precommit_failure_preserves_cached_metadata_and_committed_failure_projects_it(
    environment, monkeypatch
):
    async def scenario():
        state, store, _ = environment
        await state.start()
        await opened(state)
        await status(state, caps={96: 4})
        from hoodini import storage

        original = storage.atomic_write

        def fail_before(*_args):
            raise AtomicWriteError(False)

        monkeypatch.setattr(storage, "atomic_write", fail_before)
        with pytest.raises(AtomicWriteError) as failure:
            await state.update("hood", info={"PRF": "PRF0182867A"})
        assert not failure.value.committed
        assert state.get("hood").info == {}
        assert store.load().devices["hood"].info == {}

        def fail_after(*args):
            original(*args)
            raise AtomicWriteError(True)

        monkeypatch.setattr(storage, "atomic_write", fail_after)
        with pytest.raises(AtomicWriteError) as failure:
            await state.update("hood", info={"PRF": "PRF0182867A"})
        assert failure.value.committed
        assert state.get("hood").profile.key == "HIDDEN_ADVANCE"
        assert store.load().devices["hood"].info == {"PRF": "PRF0182867A"}
        await state.stop()

    asyncio.run(scenario())


def test_retired_session_fence_is_bounded_and_survives_forget(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        for cycle in range(3):
            for index in range(70):
                await opened(state, f"s{cycle}-{index}")
            assert len(state._live["hood"].seen_sessions) <= 64
            retired = f"s{cycle}-68"
            token = state.begin_forget("hood")
            await state.prepare_forget("hood")
            await state.finish_forget("hood")
            state.end_forget("hood", token)
            await opened(state, retired)
            assert state.get("hood") is None
            assert len(state._live["hood"].seen_sessions) <= 64
        await opened(state, "fresh")
        assert state.get("hood").connection_id == "fresh"
        await state.stop()

    asyncio.run(scenario())


def test_forgotten_old_session_cannot_reenroll_after_fence_release(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        await opened(state, "old")
        token = state.begin_forget("hood")
        await state.prepare_forget("hood")
        await state.finish_forget("hood")
        state.end_forget("hood", token)
        await opened(state, "old")
        assert state.get("hood") is None
        await opened(state, "fresh")
        assert state.get("hood").connection_id == "fresh"
        await state.stop()

    asyncio.run(scenario())


def test_forget_commit_phase_reconciles_intent_and_visible_deletion(
    environment, monkeypatch
):
    async def scenario():
        from hoodini import storage

        state, store, _ = environment
        await state.start()
        await opened(state)
        token = state.begin_forget("hood")
        original = storage.atomic_write

        def fail_before(*_args):
            raise AtomicWriteError(False)

        monkeypatch.setattr(storage, "atomic_write", fail_before)
        with pytest.raises(AtomicWriteError) as failure:
            await state.prepare_forget("hood")
        assert failure.value.committed is False
        assert "hood" not in store.load().pending_removals
        assert state.get("hood") is not None

        def fail_after(*args):
            original(*args)
            raise AtomicWriteError(True)

        monkeypatch.setattr(storage, "atomic_write", fail_after)
        with pytest.raises(AtomicWriteError) as failure:
            await state.prepare_forget("hood")
        assert failure.value.committed is True
        assert "hood" in store.load().pending_removals
        with pytest.raises(AtomicWriteError) as failure:
            await state.finish_forget("hood")
        assert failure.value.committed is True
        assert state.get("hood") is None
        assert store.load().devices == {}
        assert store.load().pending_removals == {"hood"}
        state.end_forget("hood", token)
        await state.stop()

    asyncio.run(scenario())


def test_observer_failure_and_timeout_close_without_leaking_exception(
    environment, monkeypatch
):
    async def scenario():
        from hoodini.devices import state as module

        state, _, _ = environment
        await state.start()
        monkeypatch.setattr(module, "SUBSCRIBER_TIMEOUT", 0.01)
        failure = state.subscribe(
            lambda _event: (_ for _ in ()).throw(ValueError("private"))
        )
        wait = asyncio.Event()

        async def hanging(_event):
            await wait.wait()

        timeout = state.subscribe(hanging)
        await opened(state)
        assert await asyncio.wait_for(failure.wait_closed(), 0.5) == "failed"
        assert await asyncio.wait_for(timeout.wait_closed(), 0.5) == "timeout"
        await state.stop()

    asyncio.run(scenario())


def test_unsubscribe_wait_closed_waits_for_cooperative_callback_cleanup(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def listener(_event):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        handle = state.subscribe(listener)
        await opened(state)
        await entered.wait()
        handle()
        assert await handle.wait_closed() == "unsubscribed"
        assert cleaned.is_set()
        await state.stop()

    asyncio.run(scenario())


def test_grace_deadline_is_not_extended_by_repeated_close_or_handshake(environment):
    async def scenario():
        state, _, clock = environment
        await state.start()
        await opened(state)
        await state.handle_gateway(CommandReady("hood", "one"))
        await status(state, caps={96: 70})
        await state.handle_gateway(SessionClosed("hood", "one", "closed"))
        await clock.advance(11)
        await opened(state, "two")
        await state.handle_gateway(SessionClosed("hood", "two", "closed"))
        await clock.advance(8.9)
        assert state.get("hood").online
        await state.handle_gateway(SessionClosed("hood", "one", "late"))
        await clock.advance(0.1)
        assert not state.get("hood").online
        await state.stop()

    asyncio.run(scenario())


def test_continuing_observer_sees_forgotten_before_readd_and_detached_events(
    environment,
):
    async def scenario():
        state, _, _ = environment
        await state.start()
        received = []
        handle = state.subscribe(received.append)
        await opened(state)
        token = state.begin_forget("hood")
        await state.prepare_forget("hood")
        await state.finish_forget("hood")
        state.end_forget("hood", token)
        await opened(state, "fresh")
        await asyncio.sleep(0)
        assert [event.kind for event in received] == ["added", "forgotten", "added"]
        assert received[1].view is None
        received[0].view.info["private"] = [1]
        assert state.get("hood").info == {}
        handle()
        await handle.wait_closed()
        await state.stop()

    asyncio.run(scenario())


def test_noop_metadata_update_does_not_emit_or_rewrite(environment):
    async def scenario():
        state, store, _ = environment
        await state.start()
        await opened(state)
        events = []
        handle = state.subscribe(events.append)
        before = store.load()
        assert (await state.update("hood")).info == {}
        await asyncio.sleep(0)
        assert events == []
        assert store.load() == before
        handle()
        await handle.wait_closed()
        await state.stop()

    asyncio.run(scenario())


def test_committed_status_failure_projects_received_rssi_and_caps(
    environment, monkeypatch
):
    async def scenario():
        from hoodini import storage

        state, store, _ = environment
        await state.start()
        await opened(state)
        original = storage.atomic_write

        def fail_after(*args):
            original(*args)
            raise AtomicWriteError(True)

        monkeypatch.setattr(storage, "atomic_write", fail_after)
        with pytest.raises(AtomicWriteError) as failure:
            await status(state, caps={96: 54}, rssi=-37)
        assert failure.value.committed
        assert state.get("hood").caps == {96: 54}
        assert state.get("hood").rssi == -37
        assert store.load().devices["hood"].caps == {96: 54}
        await state.stop()

    asyncio.run(scenario())


def test_status_write_failure_only_confirms_when_committed(environment, monkeypatch):
    async def scenario():
        from hoodini import storage

        state, store, _ = environment
        await state.start()
        await opened(state)
        await state.handle_gateway(CommandReady("hood", "one"))
        events = []
        subscription = state.subscribe(events.append)
        original = storage.atomic_write

        def fail_before(*_args):
            raise AtomicWriteError(False)

        monkeypatch.setattr(storage, "atomic_write", fail_before)
        with pytest.raises(AtomicWriteError):
            await status(state, caps={96: 70})
        assert not state.get("hood").online
        assert state.get("hood").caps == {}

        def fail_after(*args):
            original(*args)
            raise AtomicWriteError(True)

        monkeypatch.setattr(storage, "atomic_write", fail_after)
        with pytest.raises(AtomicWriteError):
            await status(state, caps={96: 70})
        assert state.get("hood").online
        assert store.load().devices["hood"].caps == {96: 70}
        await asyncio.sleep(0)
        assert [event.kind for event in events] == ["state", "availability"]
        subscription()
        await subscription.wait_closed()
        await state.stop()

    asyncio.run(scenario())


def test_legacy_private_info_is_not_reflected_on_any_view_surface(tmp_path):
    async def scenario():
        store = AtomicStore(tmp_path)
        private = {
            "PRF": "PRF0182867A",
            "serial": "sample",
            "token": "synthetic",
            "nested": {"x": [1]},
        }
        store.update(
            lambda s: s.devices.update(
                {"hood": StoredDevice("hood", private, None, {}, {}, set(), None)}
            )
        )
        state = DeviceState(store)
        await state.start()
        first = state.get("hood")
        assert first.info == {"PRF": "PRF0182867A"}
        assert first.profile.key == "HIDDEN_ADVANCE"
        assert state.list()[0].info == {"PRF": "PRF0182867A"}
        assert state.command_snapshot("hood")[0].info == {"PRF": "PRF0182867A"}
        events_a, events_b = [], []
        a = state.subscribe(events_a.append)
        b = state.subscribe(events_b.append)
        result = await state.update("hood", info={"nested": {"x": [2]}, "innocuous": 7})
        assert result.info == {"PRF": "PRF0182867A"}
        await asyncio.sleep(0)
        assert events_a[0].view.info == {"PRF": "PRF0182867A"}
        assert events_b[0].view.info == {"PRF": "PRF0182867A"}
        events_a[0].view.info["PRF"] = "changed"
        assert events_b[0].view.info == {"PRF": "PRF0182867A"}
        assert state.get("hood").info == {"PRF": "PRF0182867A"}
        assert store.load().devices["hood"].info == {
            "PRF": "PRF0182867A",
            "serial": "sample",
            "token": "synthetic",
            "nested": {"x": [2]},
            "innocuous": 7,
        }
        a()
        b()
        await a.wait_closed()
        await b.wait_closed()
        await state.stop()

    asyncio.run(scenario())


def test_unknown_and_non_base_string_prf_never_reflect(tmp_path):
    async def scenario():
        store = AtomicStore(tmp_path)
        infos = {
            "unknown": {"PRF": "unrecognized"},
            "numeric": {"PRF": 123},
            "missing": {"PRF": None},
            "empty": {"other": "value"},
        }
        store.update(
            lambda s: s.devices.update(
                {
                    key: StoredDevice(key, info, "HIDDEN_ADVANCE", {}, {}, set(), None)
                    for key, info in infos.items()
                }
            )
        )
        state = DeviceState(store)
        await state.start()
        assert all(state.get(key).info == {} for key in infos)
        assert all(state.get(key).profile.key == "HIDDEN_ADVANCE" for key in infos)
        assert {view.cuid: view.info for view in state.list()} == {
            key: {} for key in infos
        }
        await state.stop()

    asyncio.run(scenario())


def test_invalid_new_metadata_fails_fixed_error_without_mutation(environment):
    async def scenario():
        state, store, _ = environment
        await state.start()
        await opened(state)
        for invalid in (
            [],
            {"token": object()},
            {"nested": {1: "value"}},
            {"float": float("nan")},
            {"tuple": (1,)},
            {"cycle": None},
        ):
            if isinstance(invalid, dict) and "cycle" in invalid:
                invalid["cycle"] = invalid
            with pytest.raises(ValidationError) as failure:
                await state.update("hood", info=invalid)
            assert str(failure.value) == "invalid device info"
            assert failure.value.__cause__ is None
            assert state.get("hood").info == {}
            assert store.load().devices["hood"].info == {}
        await state.stop()

    asyncio.run(scenario())


def test_cancelled_closure_monitor_does_not_corrupt_stop_or_surviving_monitor(
    environment,
):
    async def scenario():
        state, _, _ = environment
        await state.start()
        handle = state.subscribe(lambda _event: None)
        cancelled = asyncio.create_task(handle.wait_closed())
        survivor = asyncio.create_task(handle.wait_closed())
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await asyncio.wait_for(state.stop(), 0.5)
        assert await asyncio.wait_for(survivor, 0.5) == "stopped"
        assert await asyncio.wait_for(handle.wait_closed(), 0.5) == "stopped"

    asyncio.run(scenario())


def test_synchronous_self_unsubscribe_finishes_worker_and_preserves_other_observer(
    environment,
):
    async def scenario():
        state, _, _ = environment
        await state.start()
        continuing = []
        other = state.subscribe(continuing.append)
        calls = []
        handle = None

        def self_unsubscribe(event):
            calls.append(event.kind)
            handle()

        handle = state.subscribe(self_unsubscribe)
        await opened(state)
        assert await asyncio.wait_for(handle.wait_closed(), 0.5) == "unsubscribed"
        assert handle.worker.done()
        await status(state, caps={96: 70})
        await asyncio.sleep(0)
        assert calls == ["added"]
        assert [event.kind for event in continuing] == ["added", "state"]
        await asyncio.wait_for(state.stop(), 0.5)
        assert await other.wait_closed() == "stopped"

    asyncio.run(scenario())


def test_callback_cancelled_future_terminates_subscription_as_failed(environment):
    async def scenario():
        state, _, _ = environment
        await state.start()
        cancelled = asyncio.get_running_loop().create_future()
        cancelled.cancel("synthetic private callback reason")
        healthy = []

        async def listener(_event):
            await cancelled

        broken = state.subscribe(listener)
        continuing = state.subscribe(healthy.append)
        await opened(state)
        assert await asyncio.wait_for(broken.wait_closed(), 0.5) == "failed"
        assert broken.worker.done()
        await status(state, caps={96: 70})
        await asyncio.sleep(0)
        assert [event.kind for event in healthy] == ["added", "state"]
        await state.stop()
        assert await continuing.wait_closed() == "stopped"

    asyncio.run(scenario())


def test_invalid_profile_update_has_fixed_error_without_private_exception_graph(
    environment,
):
    async def scenario():
        state, store, _ = environment
        await state.start()
        await opened(state)
        marker = "SYNTHETIC_PRIVATE_OVERRIDE_MARKER"
        with pytest.raises(ValidationError) as failure:
            await state.update("hood", info={"private": "synthetic"}, profile=marker)
        error = failure.value
        assert error.args == ("invalid device profile",)
        assert error.__context__ is None
        assert error.__cause__ is None
        assert marker not in repr(error)
        assert state.get("hood").profile_override is None
        assert store.load().devices["hood"].info == {}
        await state.stop()

    asyncio.run(scenario())


def test_invalid_loaded_override_fails_whole_start_with_fixed_error_graph(tmp_path):
    async def scenario():
        marker = "SYNTHETIC_PRIVATE_LOADED_OVERRIDE_MARKER"
        store = AtomicStore(tmp_path)
        store.update(
            lambda s: s.devices.update(
                {
                    "valid": StoredDevice("valid", {}, None, {}, {}, set(), None),
                    "invalid": StoredDevice("invalid", {}, marker, {}, {}, set(), None),
                }
            )
        )
        state = DeviceState(store)
        with pytest.raises(ValidationError) as failure:
            await state.start()
        error = failure.value
        assert error.args == ("invalid device profile",)
        assert error.__context__ is None
        assert error.__cause__ is None
        assert marker not in repr(error)
        assert state.list() == []
        assert store.load().devices["invalid"].profile_override == marker

    asyncio.run(scenario())
