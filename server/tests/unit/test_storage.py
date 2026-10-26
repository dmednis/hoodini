"""Device state persistence contract, using only synthetic temporary directories."""

import json
import os
import stat
import threading

import pytest
from hoodini.storage import AtomicStore, AtomicWriteError, StoredDevice, atomic_write


def device() -> StoredDevice:
    return StoredDevice(
        cuid="synthetic-1",
        info={"model": "demo", "nested": {"label": "desk"}},
        profile_override="generic",
        firmware={7: 65535},
        caps={96: 20},
        known_ids={7, 96, 110},
        last_seen=123.5,
        last_light=42,
        last_fan=2,
    )


def test_versioned_round_trip_and_private_permissions(tmp_path):
    directory = tmp_path / "new" / "data"
    store = AtomicStore(directory)

    def change(state):
        state.devices["synthetic-1"] = device()
        state.discovery_components["synthetic-1"] = {"light", "fan"}
        state.pending_removals.add("old-cuid")

    store.update(change)
    path = directory / "devices.json"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "version": 1,
        "devices": {
            "synthetic-1": {
                "cuid": "synthetic-1",
                "info": {"model": "demo", "nested": {"label": "desk"}},
                "profile_override": "generic",
                "firmware": {"7": 65535},
                "caps": {"96": 20},
                "known_ids": [7, 96, 110],
                "last_seen": 123.5,
                "last_light": 42,
                "last_fan": 2,
            }
        },
        "discovery_components": {"synthetic-1": ["fan", "light"]},
        "pending_removals": ["old-cuid"],
    }
    restored = AtomicStore(directory).load()
    assert restored.devices["synthetic-1"] == device()
    assert restored.discovery_components == {"synthetic-1": {"fan", "light"}}
    assert restored.pending_removals == {"old-cuid"}


def test_interleaved_updates_keep_latest_state_and_known_union(tmp_path):
    left = AtomicStore(tmp_path)
    right = AtomicStore(tmp_path)
    left.update(lambda s: s.devices.update({"synthetic-1": device()}))
    right.update(lambda s: s.pending_removals.add("removed-cuid"))
    right.update(lambda s: s.devices["synthetic-1"].caps.update({110: 3}))
    left.update(lambda s: setattr(s.devices["synthetic-1"], "caps", {96: 18}))
    result = AtomicStore(tmp_path).load()
    assert result.devices["synthetic-1"].known_ids == {7, 96, 110}
    assert result.devices["synthetic-1"].caps == {96: 18}
    assert result.pending_removals == {"removed-cuid"}


def test_separate_instances_serialize_callbacks_against_latest_state(tmp_path):
    first = AtomicStore(tmp_path)
    second = AtomicStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    errors = []

    def slow(state):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("synthetic test timeout")
        state.pending_removals.add("first")

    def run(store, change):
        try:
            store.update(change)
        except (OSError, ValueError, TimeoutError) as exc:
            errors.append(exc)

    one = threading.Thread(target=run, args=(first, slow))

    def second_change(state):
        second_entered.set()
        state.pending_removals.add("second")

    two = threading.Thread(target=run, args=(second, second_change))
    one.start()
    assert entered.wait(2)
    two.start()
    try:
        assert not second_entered.wait(0.1)
    finally:
        release.set()
    one.join(3)
    two.join(3)
    assert not one.is_alive() and not two.is_alive()
    assert errors == []
    assert AtomicStore(tmp_path).load().pending_removals == {"first", "second"}


def test_returned_views_and_callback_input_are_detached(tmp_path):
    store = AtomicStore(tmp_path)
    original = device()
    saved = store.update(lambda s: s.devices.update({"synthetic-1": original}))
    original.info["nested"]["label"] = "changed"
    saved.devices["synthetic-1"].info["nested"]["label"] = "changed"
    saved.devices.clear()
    loaded = store.load()
    loaded.devices["synthetic-1"].known_ids.clear()
    assert store.load().devices["synthetic-1"].info["nested"]["label"] == "desk"
    assert store.load().devices["synthetic-1"].known_ids == {7, 96, 110}


def test_callback_error_preserves_file_and_memory(tmp_path):
    store = AtomicStore(tmp_path)
    store.update(lambda s: s.devices.update({"synthetic-1": device()}))
    before = (tmp_path / "devices.json").read_bytes()

    def fail(state):
        state.pending_removals.add("removed-cuid")
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        store.update(fail)
    assert store._state.pending_removals == set()  # Before load can refresh the cache.
    assert (tmp_path / "devices.json").read_bytes() == before
    assert AtomicStore(tmp_path).load().pending_removals == set()


def test_atomic_write_failure_preserves_previous_bytes_and_cleans_temp(
    tmp_path, monkeypatch
):
    path = tmp_path / "devices.json"
    atomic_write(path, b"old")
    real_replace = os.replace

    def fail_replace(source, target):
        if target == path:
            raise OSError("synthetic replacement failure")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(AtomicWriteError, match="atomic write failed") as error:
        atomic_write(path, b"new")
    assert error.value.committed is False
    assert error.value.__cause__ is None
    assert path.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["devices.json"]


def test_directory_fsync_failure_commits_visible_file_and_memory(tmp_path, monkeypatch):
    store = AtomicStore(tmp_path)
    store.update(lambda s: s.devices.update({"synthetic-1": device()}))
    real_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(AtomicWriteError, match="durability") as error:
        store.update(lambda s: s.pending_removals.add("removed-cuid"))
    assert error.value.committed is True
    assert error.value.__cause__ is None
    assert store._state.pending_removals == {"removed-cuid"}  # Before reloading.
    assert AtomicStore(tmp_path).load().pending_removals == {"removed-cuid"}
    assert json.loads((tmp_path / "devices.json").read_text())["pending_removals"] == [
        "removed-cuid"
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["devices.json"]


def test_store_write_failure_preserves_previous_snapshot(tmp_path, monkeypatch):
    store = AtomicStore(tmp_path)
    store.update(lambda s: s.devices.update({"synthetic-1": device()}))
    before = (tmp_path / "devices.json").read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("hoodini.storage.atomic_write", fail)
    with pytest.raises(OSError, match="synthetic disk failure"):
        store.update(lambda s: s.pending_removals.add("removed-cuid"))
    assert store._state.pending_removals == set()  # Before load can refresh the cache.
    assert (tmp_path / "devices.json").read_bytes() == before
    assert AtomicStore(tmp_path).load().pending_removals == set()


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'{"version":2,"version":1,"devices":{},"discovery_components":{},"pending_removals":[]}',
        b'{"version":2,"devices":{},"discovery_components":{},"pending_removals":[]}',
        b'{"version":1,"devices":{"x":{"cuid":"x"}},"discovery_components":{},"pending_removals":[]}',
        b'{"version":1,"devices":{"x":{"cuid":"x","info":{},"profile_override":null,"firmware":{},"caps":{"96":true},"known_ids":[],"last_seen":null,"last_light":100,"last_fan":1}},"discovery_components":{},"pending_removals":[]}',
        b'{"version":1,"devices":{},"discovery_components":{},"pending_removals":[9]}',
    ],
)
def test_corrupt_schema_quarantines_without_echo(tmp_path, payload):
    path = tmp_path / "devices.json"
    path.write_bytes(payload)
    store = AtomicStore(tmp_path)
    assert store.load().devices == {}
    assert not path.exists()
    aside = list(tmp_path.glob("devices.json.corrupt.*"))
    assert len(aside) == 1
    assert aside[0].read_bytes() == payload
    store.update(lambda s: s.pending_removals.add("clean-cuid"))
    assert AtomicStore(tmp_path).load().pending_removals == {"clean-cuid"}


@pytest.mark.parametrize("in_info", [True, False])
def test_deeply_nested_corrupt_json_quarantined_without_echo(tmp_path, capsys, in_info):
    nested = b"[" * 4000 + b"0" + b"]" * 4000
    if in_info:
        payload = (
            b'{"version":1,"devices":{"synthetic-1":{"cuid":"synthetic-1",'
            b'"info":{"nested":' + nested + b'},"profile_override":null,'
            b'"firmware":{},"caps":{},"known_ids":[],"last_seen":null,'
            b'"last_light":100,"last_fan":1}},"discovery_components":{},'
            b'"pending_removals":[]}'
        )
    else:
        payload = nested
    path = tmp_path / "devices.json"
    path.write_bytes(payload)

    assert AtomicStore(tmp_path).load().devices == {}
    assert not path.exists()
    aside = list(tmp_path.glob("devices.json.corrupt.*"))
    assert len(aside) == 1
    assert aside[0].read_bytes() == payload
    assert capsys.readouterr() == ("", "")


def test_known_ids_list_update_rejected_without_losing_historical_ids(tmp_path):
    store = AtomicStore(tmp_path)
    store.update(lambda s: s.devices.update({"synthetic-1": device()}))
    before = (tmp_path / "devices.json").read_bytes()

    with pytest.raises(ValueError, match="known_ids"):
        store.update(lambda s: setattr(s.devices["synthetic-1"], "known_ids", []))

    assert store._state.devices["synthetic-1"].known_ids == {7, 96, 110}
    assert type(store._state.devices["synthetic-1"].known_ids) is set
    assert (tmp_path / "devices.json").read_bytes() == before
    assert AtomicStore(tmp_path).load().devices["synthetic-1"].known_ids == {7, 96, 110}


def test_invalid_update_rejects_bool_numeric_without_changing_file(tmp_path):
    store = AtomicStore(tmp_path)
    store.update(lambda s: s.devices.update({"synthetic-1": device()}))
    before = (tmp_path / "devices.json").read_bytes()
    with pytest.raises(ValueError, match="caps"):
        store.update(lambda s: s.devices["synthetic-1"].caps.update({96: True}))
    assert (tmp_path / "devices.json").read_bytes() == before
    assert store.load().devices["synthetic-1"].caps == {96: 20}
