"""TLS material integration checks use only fresh synthetic temporary directories."""

import os
import socket
import ssl
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from hoodini.certs import ensure_cert
from hoodini.storage import AtomicWriteError


def test_ensure_cert_persists_private_rsa_material_and_reuses_pair(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "new-data"
    cert_path, key_path = ensure_cert(directory)
    assert cert_path.parent == directory
    assert key_path.parent == directory
    assert cert_path != key_path
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    cert_bytes, key_bytes = cert_path.read_bytes(), key_path.read_bytes()
    cert = x509.load_pem_x509_certificate(cert_bytes)
    key = serialization.load_pem_private_key(key_bytes, password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 2048
    assert isinstance(cert.public_key(), rsa.RSAPublicKey)
    assert cert.public_key().key_size == 2048
    assert isinstance(cert.signature_hash_algorithm, hashes.SHA256)
    assert cert.issuer == cert.subject
    assert cert.not_valid_before_utc <= datetime.now(UTC) < cert.not_valid_after_utc
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    cert.public_key().verify(
        cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), hashes.SHA256()
    )
    before = (cert_path.stat().st_mtime_ns, key_path.stat().st_mtime_ns)
    assert ensure_cert(directory) == (cert_path, key_path)
    assert (cert_path.read_bytes(), key_path.read_bytes()) == (cert_bytes, key_bytes)
    assert (cert_path.stat().st_mtime_ns, key_path.stat().st_mtime_ns) == before


def test_ensure_cert_supports_local_tls_handshake(tmp_path: Path) -> None:
    cert_path, key_path = ensure_cert(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert_path), str(key_path))
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = (
        ssl.CERT_NONE
    )  # This is only a synthetic self-signed test.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve() -> bytes:
            with listener.accept()[0] as connection:
                connection.settimeout(5)
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    return tls.recv(4)

        with ThreadPoolExecutor(max_workers=1) as executor:
            received = executor.submit(serve)
            with (
                socket.create_connection(
                    listener.getsockname(), timeout=5
                ) as connection,
                client_context.wrap_socket(
                    connection, server_hostname="hoodini.local"
                ) as tls,
            ):
                tls.sendall(b"test")
            assert received.result(timeout=5) == b"test"


@pytest.mark.parametrize("missing", ["certificate", "key"])
def test_ensure_cert_rejects_partial_material(tmp_path: Path, missing: str) -> None:
    cert_path, key_path = ensure_cert(tmp_path)
    (cert_path if missing == "certificate" else key_path).unlink()
    with pytest.raises(ValueError, match="certificate") as error:
        ensure_cert(tmp_path)
    assert error.value.__cause__ is None
    assert not list(tmp_path.glob("*.corrupt*"))


def test_ensure_cert_rejects_malformed_and_mismatched_material_without_leaking_input(
    tmp_path: Path,
) -> None:
    cert_path, key_path = ensure_cert(tmp_path / "first")
    _, other_key = ensure_cert(tmp_path / "second")
    original = key_path.read_bytes()
    for invalid in (b"SECRET_SYNTHETIC_BAD_PEM", other_key.read_bytes()):
        key_path.write_bytes(invalid)
        with pytest.raises(ValueError, match="certificate") as error:
            ensure_cert(cert_path.parent)
        assert "SECRET_SYNTHETIC_BAD_PEM" not in str(error.value)
        assert error.value.__cause__ is None
        assert key_path.read_bytes() == invalid
    key_path.write_bytes(original)
    cert_path.write_bytes(b"SECRET_SYNTHETIC_BAD_CERT")
    with pytest.raises(ValueError, match="certificate") as error:
        ensure_cert(cert_path.parent)
    assert "SECRET_SYNTHETIC_BAD_CERT" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("target", ["certificate", "key"])
def test_ensure_cert_rejects_insecure_existing_file(
    tmp_path: Path, target: str
) -> None:
    cert_path, key_path = ensure_cert(tmp_path)
    path = cert_path if target == "certificate" else key_path
    path.chmod(0o644)
    with pytest.raises(ValueError, match="certificate"):
        ensure_cert(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_ensure_cert_rejects_symlinked_material(tmp_path: Path) -> None:
    cert_path, key_path = ensure_cert(tmp_path)
    key_path.unlink()
    key_path.symlink_to(cert_path)
    with pytest.raises(ValueError, match="certificate"):
        ensure_cert(tmp_path)


def test_ensure_cert_pre_replace_failure_does_not_publish_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hoodini import storage

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("SECRET_SYNTHETIC_WRITE_FAILURE")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(AtomicWriteError) as error:
        ensure_cert(tmp_path)
    assert error.value.committed is False
    assert "SECRET_SYNTHETIC_WRITE_FAILURE" not in str(error.value)
    assert error.value.__cause__ is None
    assert not list(tmp_path.iterdir())


def test_ensure_cert_post_replace_failure_reports_visible_uncertain_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hoodini import storage

    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("SECRET_SYNTHETIC_DURABILITY_FAILURE")
        real_fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", fail_directory_fsync)
    with pytest.raises(AtomicWriteError) as error:
        ensure_cert(tmp_path)
    assert error.value.committed is True
    assert "SECRET_SYNTHETIC_DURABILITY_FAILURE" not in str(error.value)
    assert error.value.__cause__ is None
    assert len(list(tmp_path.iterdir())) == 1
    with pytest.raises(ValueError, match="certificate"):
        ensure_cert(tmp_path)
