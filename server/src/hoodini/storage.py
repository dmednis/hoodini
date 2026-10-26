"""Versioned device snapshots and the shared crash-safe file writer."""

import copy
import json
import math
import os
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StoredDevice:
    cuid: str
    info: dict
    profile_override: str | None
    firmware: dict[int, int]
    caps: dict[int, int]
    known_ids: set[int]
    last_seen: float | None
    last_light: int = 100
    last_fan: int = 1


@dataclass
class StoreSnapshot:
    devices: dict[str, StoredDevice] = field(default_factory=dict)
    discovery_components: dict[str, set[str]] = field(default_factory=dict)
    pending_removals: set[str] = field(default_factory=set)


class AtomicWriteError(OSError):
    """A sanitized write failure; committed indicates whether replace completed."""

    def __init__(self, committed: bool) -> None:
        self.committed = committed
        super().__init__(
            "atomic write durability failure" if committed else "atomic write failed"
        )


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace a file via a private same-directory temporary and durable rename.

    A directory-sync error after replacement raises AtomicWriteError(committed=True):
    the new file is visible, but its survival across a crash is uncertain.
    """
    temporary = None
    committed = False
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        committed = True
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise AtomicWriteError(committed) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _invalid(field: str) -> ValueError:
    return ValueError(f"invalid device state: {field}")


def _object(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise _invalid(label)
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(label)
    return value


def _integer(value: object, maximum: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise _invalid(label)
    return value


def _ids(values: object, label: str) -> set[int]:
    if not isinstance(values, set):
        raise _invalid(label)
    return {_integer(value, 255, label) for value in values}


def _decoded_ids(values: object) -> set[int]:
    if not isinstance(values, list):
        raise _invalid("known_ids")
    result = {_integer(value, 255, "known_ids") for value in values}
    if len(result) != len(values):
        raise _invalid("known_ids")
    return result


def _mapping(values: object, label: str, from_json: bool) -> dict[int, int]:
    if not isinstance(values, dict):
        raise _invalid(label)
    result = {}
    for key, value in values.items():
        if from_json:
            if (
                not isinstance(key, str)
                or not key.isascii()
                or not key.isdecimal()
                or str(int(key)) != key
            ):
                raise _invalid(label)
            key = int(key)
        result[_integer(key, 255, label)] = _integer(value, 65535, label)
    return result


def _json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if type(value) in (int, float):
        return type(value) is int or math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    return isinstance(value, dict) and all(
        isinstance(key, str) and _json_value(item) for key, item in value.items()
    )


def _encode(snapshot: StoreSnapshot) -> bytes:
    if not isinstance(snapshot, StoreSnapshot) or not isinstance(
        snapshot.devices, dict
    ):
        raise _invalid("devices")
    devices = {}
    for cuid, device in snapshot.devices.items():
        _string(cuid, "cuid")
        if not isinstance(device, StoredDevice) or device.cuid != cuid:
            raise _invalid("devices")
        if not isinstance(device.info, dict) or not _json_value(device.info):
            raise _invalid("info")
        if device.profile_override is not None:
            _string(device.profile_override, "profile_override")
        firmware = _mapping(device.firmware, "firmware", False)
        caps = _mapping(device.caps, "caps", False)
        known_ids = _ids(device.known_ids, "known_ids") | caps.keys()
        if device.last_seen is not None and (
            type(device.last_seen) not in (int, float)
            or not math.isfinite(device.last_seen)
            or device.last_seen < 0
        ):
            raise _invalid("last_seen")
        devices[cuid] = {
            "cuid": cuid,
            "info": device.info,
            "profile_override": device.profile_override,
            "firmware": {str(k): v for k, v in firmware.items()},
            "caps": {str(k): v for k, v in caps.items()},
            "known_ids": sorted(known_ids),
            "last_seen": device.last_seen,
            "last_light": _integer(device.last_light, 100, "last_light"),
            "last_fan": _integer(device.last_fan, 3, "last_fan"),
        }
    if not isinstance(snapshot.discovery_components, dict):
        raise _invalid("discovery_components")
    components = {}
    for cuid, items in snapshot.discovery_components.items():
        _string(cuid, "discovery_components")
        if not isinstance(items, set):
            raise _invalid("discovery_components")
        components[cuid] = sorted(
            _string(item, "discovery_components") for item in items
        )
    if not isinstance(snapshot.pending_removals, set):
        raise _invalid("pending_removals")
    removals = sorted(
        _string(cuid, "pending_removals") for cuid in snapshot.pending_removals
    )
    return json.dumps(
        {
            "version": 1,
            "devices": devices,
            "discovery_components": components,
            "pending_removals": removals,
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("schema")
        result[key] = value
    return result


def _decode(data: bytes) -> StoreSnapshot:
    try:
        raw = json.loads(data, object_pairs_hook=_unique_object)
        _object(
            raw,
            {"version", "devices", "discovery_components", "pending_removals"},
            "schema",
        )
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise _invalid("version")
        if not isinstance(raw["devices"], dict):
            raise _invalid("devices")
        devices = {}
        fields = {
            "cuid",
            "info",
            "profile_override",
            "firmware",
            "caps",
            "known_ids",
            "last_seen",
            "last_light",
            "last_fan",
        }
        for cuid, record in raw["devices"].items():
            _string(cuid, "cuid")
            _object(record, fields, "devices")
            if record["cuid"] != cuid:
                raise _invalid("cuid")
            devices[cuid] = StoredDevice(
                cuid,
                record["info"],
                record["profile_override"],
                _mapping(record["firmware"], "firmware", True),
                _mapping(record["caps"], "caps", True),
                _decoded_ids(record["known_ids"]),
                record["last_seen"],
                record["last_light"],
                record["last_fan"],
            )
        if not isinstance(raw["discovery_components"], dict):
            raise _invalid("discovery_components")
        components = {}
        for cuid, items in raw["discovery_components"].items():
            _string(cuid, "discovery_components")
            if not isinstance(items, list) or any(
                not isinstance(i, str) for i in items
            ):
                raise _invalid("discovery_components")
            components[cuid] = set(items)
            if len(components[cuid]) != len(items):
                raise _invalid("discovery_components")
        removals = raw["pending_removals"]
        if not isinstance(removals, list) or any(
            not isinstance(i, str) for i in removals
        ):
            raise _invalid("pending_removals")
        snapshot = StoreSnapshot(devices, components, set(removals))
        if len(snapshot.pending_removals) != len(removals):
            raise _invalid("pending_removals")
        _encode(snapshot)  # Validate all fields with the same rules as updates.
        return snapshot
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise _invalid("schema") from None


_registry_lock = threading.Lock()
_store_locks: dict[Path, threading.RLock] = {}


class AtomicStore:
    """Serialized, detached read/modify/write of devices.json only."""

    def __init__(self, data_dir: Path) -> None:
        self._directory = data_dir
        self._path = data_dir / "devices.json"
        with _registry_lock:
            self._lock = _store_locks.setdefault(
                self._path.resolve(), threading.RLock()
            )
        self._state = StoreSnapshot()

    def _read(self) -> StoreSnapshot:
        try:
            data = self._path.read_bytes()
        except FileNotFoundError:
            return StoreSnapshot()
        try:
            return _decode(data)
        except ValueError:
            aside = self._directory / f"devices.json.corrupt.{uuid.uuid4().hex}"
            os.replace(self._path, aside)
            return StoreSnapshot()

    def load(self) -> StoreSnapshot:
        with self._lock:
            self._state = self._read()
            return copy.deepcopy(self._state)

    def update(self, change: Callable[[StoreSnapshot], None]) -> StoreSnapshot:
        with self._lock:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._state = self._read()
            working = copy.deepcopy(self._state)
            change(working)
            for cuid, device in working.devices.items():
                previous = self._state.devices.get(cuid)
                if isinstance(device, StoredDevice) and isinstance(
                    device.known_ids, set
                ):
                    device.known_ids |= (
                        device.caps.keys() if isinstance(device.caps, dict) else set()
                    )
                    if previous is not None:
                        device.known_ids |= previous.known_ids | previous.caps.keys()
            payload = _encode(working)
            try:
                atomic_write(self._path, payload)
            except AtomicWriteError as exc:
                if exc.committed:
                    self._state = copy.deepcopy(working)
                raise
            self._state = copy.deepcopy(working)
            return copy.deepcopy(self._state)
