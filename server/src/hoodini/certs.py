"""Persisted local TLS certificate and private key."""

import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID, SignatureAlgorithmOID

from hoodini.storage import atomic_write

_INVALID = "Invalid TLS certificate or private key; restore a trusted pair explicitly."
_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


class CertificateError(ValueError):
    """Safe-to-display invalid or inaccessible TLS material."""


def _private_file(path: Path) -> bytes | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError:
        raise CertificateError(_INVALID) from None
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise CertificateError(_INVALID)
    try:
        return path.read_bytes()
    except OSError:
        raise CertificateError(_INVALID) from None


def _validate(cert_bytes: bytes, key_bytes: bytes) -> None:
    try:
        cert = x509.load_pem_x509_certificate(cert_bytes)
        key = serialization.load_pem_private_key(key_bytes, password=None)
        public = cert.public_key()
        now = datetime.now(UTC)
        if (
            not isinstance(key, rsa.RSAPrivateKey)
            or key.key_size != 2048
            or not isinstance(public, rsa.RSAPublicKey)
            or public.key_size != 2048
            or public.public_numbers() != key.public_key().public_numbers()
            or cert.issuer != cert.subject
            or cert.signature_algorithm_oid != SignatureAlgorithmOID.RSA_WITH_SHA256
            or not isinstance(cert.signature_hash_algorithm, hashes.SHA256)
            or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc
        ):
            raise CertificateError(_INVALID)
        public.verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception:  # noqa: BLE001 - crypto errors can contain private input
        # Cryptographic parsers can include input fragments in their errors.
        raise CertificateError(_INVALID) from None


def ensure_cert(data_dir: Path) -> tuple[Path, Path]:
    """Return persisted (certificate, private key) paths; never repair a partial pair.

    Filenames are fixed at server.crt and server.key within data_dir. A failure
    after one atomic replacement is propagated, not treated as a transaction.
    """
    cert_path, key_path = data_dir / "server.crt", data_dir / "server.key"
    with _locks_guard:
        lock = _locks.setdefault(cert_path.resolve(), threading.Lock())
    with lock:
        certificate = _private_file(cert_path)
        private_key = _private_file(key_path)
        if certificate is not None or private_key is not None:
            if certificate is None or private_key is None:
                raise CertificateError(_INVALID)
            _validate(certificate, private_key)
            return cert_path, key_path

        try:
            data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise CertificateError(
                "Cannot create private TLS data directory."
            ) from None
        try:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "hoodini.local")]
            )
            now = datetime.now(UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=3650))
                .sign(key, hashes.SHA256())
            )
            cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
            key_bytes = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        except Exception:  # noqa: BLE001 - never expose crypto backend errors
            raise CertificateError("Cannot generate TLS certificate.") from None
        atomic_write(cert_path, cert_bytes)
        atomic_write(key_path, key_bytes)
        return cert_path, key_path
