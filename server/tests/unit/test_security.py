"""Synthetic-only persistent security contract tests."""

import concurrent.futures
import hashlib
import json
import os
import re
import stat
import threading

import pytest
from hoodini.security import SecurityError, TrustStore, ensure_api_token
from hoodini.storage import AtomicWriteError


def test_enrollment_accepts_only_matching_password_and_persists_private_state(tmp_path):
    directory = tmp_path / "new" / "data"
    store = TrustStore(directory)
    assert store.check_or_enroll("synthetic-a", "synthetic-password-1") is True
    assert store.check_or_enroll("synthetic-a", "synthetic-password-1") is True
    assert (
        TrustStore(directory).check_or_enroll("synthetic-a", "synthetic-password-2")
        is False
    )
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    path = directory / "trust.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert set(data) == {"version", "devices"}
    record = data["devices"]["synthetic-a"]
    assert set(record) == {"salt", "hash"}
    assert re.fullmatch(r"[0-9a-f]{32}", record["salt"])
    assert re.fullmatch(r"[0-9a-f]{64}", record["hash"])
    assert bytes.fromhex(record["hash"]) == hashlib.scrypt(
        b"synthetic-password-1",
        salt=bytes.fromhex(record["salt"]),
        n=16384,
        r=8,
        p=1,
        dklen=32,
        maxmem=32 * 1024 * 1024,
    )
    assert b"synthetic-password" not in path.read_bytes()


def test_binary_password_enrolls_raw_bytes_and_verifies_without_decoding(tmp_path):
    store = TrustStore(tmp_path)
    password = b"synthetic-secret-\xff\x00"
    assert store.check_or_enroll("synthetic-a", password)
    record = json.loads((tmp_path / "trust.json").read_text())["devices"]["synthetic-a"]
    assert bytes.fromhex(record["hash"]) == hashlib.scrypt(
        password,
        salt=bytes.fromhex(record["salt"]),
        n=16384,
        r=8,
        p=1,
        dklen=32,
        maxmem=32 * 1024 * 1024,
    )
    assert TrustStore(tmp_path).check_or_enroll("synthetic-a", password)
    assert not store.check_or_enroll("synthetic-a", b"synthetic-secret-\xfe\x00")


def test_utf8_string_and_equal_bytes_verify_in_both_enrollment_directions(tmp_path):
    store = TrustStore(tmp_path)
    assert store.check_or_enroll("synthetic-a", "café")
    assert store.check_or_enroll("synthetic-a", b"caf\xc3\xa9")
    assert store.check_or_enroll("synthetic-b", b"caf\xc3\xa9")
    assert store.check_or_enroll("synthetic-b", "café")


@pytest.mark.parametrize("password", [b"", "", None, 42, bytearray(b"secret")])
def test_password_must_be_nonempty_bytes_or_string(tmp_path, password):
    with pytest.raises(SecurityError):
        TrustStore(tmp_path).check_or_enroll("synthetic-a", password)
    assert not (tmp_path / "trust.json").exists()


def test_independent_cuids_use_independent_salts_and_forget_reenrolls(tmp_path):
    store = TrustStore(tmp_path)
    assert store.check_or_enroll("synthetic-a", "synthetic-password")
    assert store.check_or_enroll("synthetic-b", "synthetic-password")
    records = json.loads((tmp_path / "trust.json").read_text())["devices"]
    assert records["synthetic-a"]["salt"] != records["synthetic-b"]["salt"]
    assert records["synthetic-a"]["hash"] != records["synthetic-b"]["hash"]
    store.forget("synthetic-a")
    assert TrustStore(tmp_path).check_or_enroll("synthetic-b", "synthetic-password")
    assert store.check_or_enroll("synthetic-a", "new-synthetic-password")
    assert not TrustStore(tmp_path).check_or_enroll("synthetic-a", "synthetic-password")
    assert (
        json.loads((tmp_path / "trust.json").read_text())["devices"]["synthetic-a"][
            "salt"
        ]
        != records["synthetic-a"]["salt"]
    )


def test_separate_instances_serialize_concurrent_enrollments(tmp_path):
    barrier = threading.Barrier(8)

    def enroll(index):
        barrier.wait(timeout=5)
        return TrustStore(tmp_path).check_or_enroll(
            f"synthetic-{index}", f"synthetic-password-{index}"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(enroll, range(8))) == [True] * 8
    assert len(json.loads((tmp_path / "trust.json").read_text())["devices"]) == 8
    for index in range(8):
        assert TrustStore(tmp_path).check_or_enroll(
            f"synthetic-{index}", f"synthetic-password-{index}"
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b'{"version":1,"version":1,"devices":{}}',
        b'{"version":2,"devices":{}}',
        b'{"version":true,"devices":{}}',
        b'{"version":1,"devices":[]}',
        b'{"version":1,"devices":{"synthetic-a":{"salt":"00","hash":"00"}}}',
        b'{"version":1,"devices":{"synthetic-a":{"salt":42,"hash":"00"}}}',
        b'{"version":1,"devices":{"synthetic-a":{"salt":"gggggggggggggggggggggggggggggggg","hash":"00"}}}',
        b'{"version":1,"devices":{"synthetic-a":{},"synthetic-a":{}}}',
        b'{"version":1,"devices":{},"extra":null}',
        b'{"version":1,"devices":{},"extra":NaN}',
        b'{"version":1,"devices":' + b"[" * 4000 + b"0" + b"]" * 4000 + b"}",
    ],
    ids=[
        "empty",
        "not-json",
        "duplicate-root",
        "version",
        "boolean-version",
        "devices-type",
        "short-record",
        "salt-type",
        "bad-hex",
        "duplicate-cuid",
        "extra",
        "nan",
        "recursive",
    ],
)
def test_malformed_trust_fails_closed_without_modifying_file(tmp_path, payload, capsys):
    path = tmp_path / "trust.json"
    path.write_bytes(payload)
    store = TrustStore(tmp_path)
    for action in (
        lambda: store.check_or_enroll("synthetic-b", "synthetic-password"),
        lambda: store.forget("synthetic-a"),
    ):
        with pytest.raises(SecurityError, match=r"restore|reset") as error:
            action()
        assert "synthetic-password" not in str(error.value)
        assert error.value.__cause__ is None
        assert path.read_bytes() == payload
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trust.json"]
    assert capsys.readouterr() == ("", "")


def test_dangling_trust_symlink_is_not_treated_as_missing_trust(tmp_path):
    path = tmp_path / "trust.json"
    path.symlink_to(tmp_path / "missing-synthetic-target")
    with pytest.raises(SecurityError, match="restore"):
        TrustStore(tmp_path).check_or_enroll("synthetic-a", "synthetic-password")
    assert path.is_symlink()
    assert not (tmp_path / "missing-synthetic-target").exists()


def test_existing_valid_record_with_bad_hex_length_fails_closed(tmp_path):
    store = TrustStore(tmp_path)
    assert store.check_or_enroll("synthetic-a", "synthetic-password")
    path = tmp_path / "trust.json"
    data = json.loads(path.read_text())
    data["devices"]["synthetic-a"]["hash"] = "0" * 63
    path.write_text(json.dumps(data))
    with pytest.raises(SecurityError, match="restore"):
        TrustStore(tmp_path).check_or_enroll("synthetic-b", "synthetic-password")


def test_pre_replace_failure_preserves_previous_trust_and_allows_retry(
    tmp_path, monkeypatch
):
    store = TrustStore(tmp_path)
    assert store.check_or_enroll("synthetic-a", "synthetic-password")
    path = tmp_path / "trust.json"
    before = path.read_bytes()
    real_replace = os.replace

    def fail_replace(source, target):
        if target == path:
            raise OSError("synthetic-secret-marker")
        return real_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(AtomicWriteError) as error:
            store.check_or_enroll("synthetic-b", "synthetic-password")
    assert error.value.committed is False
    assert "synthetic-secret-marker" not in str(error.value)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".trust.json.*"))
    assert store.check_or_enroll("synthetic-b", "synthetic-password")


def test_post_replace_failure_propagates_and_next_read_sees_committed_trust(
    tmp_path, monkeypatch
):
    store = TrustStore(tmp_path)
    assert store.check_or_enroll("synthetic-a", "synthetic-password")
    real_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic-secret-marker")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory_fsync)
        with pytest.raises(AtomicWriteError) as error:
            store.check_or_enroll("synthetic-b", "synthetic-password")
    assert error.value.committed is True
    assert "synthetic-secret-marker" not in str(error.value)
    records = json.loads((tmp_path / "trust.json").read_text())["devices"]
    assert set(records) == {"synthetic-a", "synthetic-b"}
    assert store.check_or_enroll("synthetic-b", "synthetic-password")
    assert not store.check_or_enroll("synthetic-b", "different-password")
    assert not list(tmp_path.glob(".trust.json.*"))


def test_forget_post_replace_failure_sees_removed_record_on_next_call(
    tmp_path, monkeypatch
):
    store = TrustStore(tmp_path)
    store.check_or_enroll("synthetic-a", "old-password")
    real_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic sync failure")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory_fsync)
        with pytest.raises(AtomicWriteError) as error:
            store.forget("synthetic-a")
    assert error.value.committed is True
    assert json.loads((tmp_path / "trust.json").read_text())["devices"] == {}
    assert store.check_or_enroll("synthetic-a", "new-password")
    assert not store.check_or_enroll("synthetic-a", "old-password")


def test_api_token_is_private_random_persisted_reused_and_never_logged(
    tmp_path, caplog
):
    directory = tmp_path / "new" / "data"
    first = ensure_api_token(directory)
    second = ensure_api_token(directory)
    other = ensure_api_token(tmp_path / "other")
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert second == first
    assert other != first
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    path = directory / "api_token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == first
    assert first not in caplog.text
    assert first not in repr(TrustStore(directory))


def test_invalid_unicode_password_does_not_echo_secret_in_errors(tmp_path):
    password = "synthetic-secret-marker\ud800"
    with pytest.raises(SecurityError) as error:
        TrustStore(tmp_path).check_or_enroll("synthetic-a", password)
    assert "synthetic-secret-marker" not in str(error.value)
    assert error.value.__cause__ is None
    assert not (tmp_path / "trust.json").exists()


def test_token_post_replace_failure_propagates_and_reuses_visible_file(
    tmp_path, monkeypatch
):
    real_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic-secret-marker")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory_fsync)
        with pytest.raises(AtomicWriteError) as error:
            ensure_api_token(tmp_path)
    assert error.value.committed is True
    assert "synthetic-secret-marker" not in str(error.value)
    assert ensure_api_token(tmp_path) == (tmp_path / "api_token").read_text()


def test_concurrent_token_creation_reuses_single_file(tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: ensure_api_token(tmp_path), range(8)))
    assert len(set(tokens)) == 1
    assert (tmp_path / "api_token").read_text() == tokens[0]


def test_malformed_token_fails_without_replacing_it(tmp_path):
    path = tmp_path / "api_token"
    path.write_text("synthetic-invalid-token")
    path.chmod(0o600)
    with pytest.raises(SecurityError) as error:
        ensure_api_token(tmp_path)
    assert "synthetic-invalid-token" not in str(error.value)
    assert path.read_text() == "synthetic-invalid-token"
