"""Persistent local TOFU device trust and API token."""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path

from hoodini.storage import atomic_write

# Format v1 freezes the cost and sizes; no untrusted KDF parameters are loaded.
_SCRYPT = {"n": 16384, "r": 8, "p": 1, "dklen": 32, "maxmem": 32 * 1024 * 1024}
_CORRUPT_TRUST = (
    "Invalid trust.json: restore a trusted backup or perform an explicit enrollment "
    "reset before accepting devices."
)
_registry_lock = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


class SecurityError(ValueError):
    """A safe-to-display security data error, without secret or path details."""


def _lock_for(path: Path) -> threading.RLock:
    with _registry_lock:
        return _path_locks.setdefault(path.resolve(), threading.RLock())


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def _hex(value: object, length: int) -> bool:
    return (
        type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None
    )


def _read_trust(path: Path) -> dict:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return {}
    except OSError:
        raise SecurityError(
            "Cannot read trust.json; check the private data directory."
        ) from None
    if not stat.S_ISREG(mode):
        raise SecurityError(_CORRUPT_TRUST)
    try:
        payload = path.read_bytes()
    except OSError:
        raise SecurityError(
            "Cannot read trust.json; check the private data directory."
        ) from None
    try:
        raw = json.loads(
            payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        if type(raw) is not dict or set(raw) != {"version", "devices"}:
            raise ValueError
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise ValueError
        devices = raw["devices"]
        if type(devices) is not dict:
            raise ValueError
        for cuid, record in devices.items():
            if not isinstance(cuid, str) or not cuid or type(record) is not dict:
                raise ValueError
            if set(record) != {"salt", "hash"}:
                raise ValueError
            if not _hex(record["salt"], 32) or not _hex(record["hash"], 64):
                raise ValueError
        return devices
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise SecurityError(_CORRUPT_TRUST) from None


def _persist_trust(path: Path, devices: dict) -> None:
    payload = json.dumps(
        {"version": 1, "devices": devices}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    atomic_write(path, payload)


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise SecurityError("Cannot create private data directory.") from None


class TrustStore:
    """Process-local serialized read/modify/write; no cached trust to go stale."""

    def __init__(self, data_dir: Path) -> None:
        self._directory = data_dir
        self._path = data_dir / "trust.json"
        self._lock = _lock_for(self._path)

    def check_or_enroll(self, cuid: str, password: bytes | str) -> bool:
        if (
            not isinstance(cuid, str)
            or not cuid
            or type(password) not in (bytes, str)
            or not password
        ):
            raise SecurityError("A nonempty CUID and password are required.")
        if isinstance(password, str):
            try:
                password_bytes = password.encode("utf-8")
            except UnicodeError:
                raise SecurityError("Invalid password encoding.") from None
        else:
            password_bytes = password
        with self._lock:
            devices = _read_trust(self._path)
            record = devices.get(cuid)
            if record is not None:
                digest = hashlib.scrypt(
                    password_bytes, salt=bytes.fromhex(record["salt"]), **_SCRYPT
                )
                return hmac.compare_digest(digest, bytes.fromhex(record["hash"]))
            salt = os.urandom(16)
            digest = hashlib.scrypt(password_bytes, salt=salt, **_SCRYPT)
            devices[cuid] = {"salt": salt.hex(), "hash": digest.hex()}
            _private_directory(self._directory)
            _persist_trust(self._path, devices)
            return True

    def forget(self, cuid: str) -> None:
        if not isinstance(cuid, str) or not cuid:
            raise SecurityError("A nonempty CUID is required.")
        with self._lock:
            devices = _read_trust(self._path)
            if cuid in devices:
                del devices[cuid]
                _persist_trust(self._path, devices)


def ensure_api_token(data_dir: Path) -> str:
    """Return the existing private local bearer token or create one once."""
    path = data_dir / "api_token"
    with _lock_for(path):
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise SecurityError(
                    "API token file must be a private regular file (0600)."
                )
            token = path.read_bytes().decode("ascii")
        except FileNotFoundError:
            _private_directory(data_dir)
            token = secrets.token_hex(32)
            atomic_write(path, token.encode("ascii"))
            return token
        except (OSError, UnicodeError):
            raise SecurityError("Cannot read API token file safely.") from None
        if not _hex(token, 64):
            raise SecurityError("Invalid API token file; restore a trusted backup.")
        return token
