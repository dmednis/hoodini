"""User-facing provisioning workflow; probes before opening a BLE connection."""

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import time
from getpass import getpass
from pathlib import Path
from urllib.request import Request, urlopen

from hoodini.api.bonjour import browse_servers
from hoodini.pairing.client import PairingClient, PairingError
from hoodini.pairing.keys import (
    ExtractionError,
    extract_setup_key,
    load_token,
    resolve_setup_key,
    save_setup_key,
    save_token,
)
from hoodini.pairing.transport import BleakTransport
from hoodini.protocol.ble import ProvisioningError

FIRST_STATUS_WAIT = 420


class PairCommandError(Exception):
    """Safe user-facing diagnostic (never contains secret values)."""


def _request(server, path, *, token=None, info=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"http://{server}:8765/api/v1/{path}",
        data=json.dumps({"info": info}).encode() if info is not None else None,
        headers=headers,
        method="PATCH" if info is not None else "GET",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def probe(server):
    # The server is self-signed: this context only proves the handshake works; never reuse it.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((server, 8883), timeout=5) as connection,
        context.wrap_socket(connection, server_hostname=server),
    ):
        pass  # Handshake complete; close without sending or reading application data.
    records = _request(server, "devices")
    if not isinstance(records, list):
        raise TypeError("Unexpected API response")
    return records


def device(server, cuid):
    return _request(server, f"devices/{cuid}")


def upload(server, cuid, info, token):
    return _request(server, f"devices/{cuid}", token=token, info=info)


def _choose(options, label):
    if len(options) == 1:
        return options[0]
    for index, option in enumerate(options, 1):
        print(f"{index}. {label(option)}")
    try:
        number = int(input("Select number: "))
        if 1 <= number <= len(options):
            return options[number - 1]
    except ValueError:
        pass
    raise PairCommandError("No valid selection")


async def _wait(server, cuid, baseline, *, status=False):
    # First full status was observed 215-302 s after readiness on real hardware.
    deadline = time.monotonic() + (FIRST_STATUS_WAIT if status else 120)
    next_progress = time.monotonic()
    while True:
        try:
            record = await asyncio.to_thread(device, server, cuid)
        except Exception:  # noqa: BLE001 - sanitize external errors before display
            record = {}
        if status:
            if record.get("known_ids"):
                return True
            if time.monotonic() >= next_progress:
                print(
                    "Waiting for first full status (usually 3.5-5 minutes); HA entities appear then"
                )
                next_progress = time.monotonic() + 30
        elif (
            record.get("session_state") == "ready"
            and record.get("connection_id") is not None
            and record["connection_id"] != baseline
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(min(3, max(0, deadline - time.monotonic())))


async def pair(args):
    if args.server:
        try:
            address = ipaddress.ip_address(args.server)
            if address.version != 4:
                raise ValueError
        except ValueError:
            raise PairCommandError("--server must be an IPv4 literal") from None
        server = str(address)
    else:
        try:
            servers = await browse_servers()
        except Exception:  # noqa: BLE001 - sanitize external errors before display
            raise PairCommandError("Bonjour browse failed") from None
        if not servers:
            raise PairCommandError("No Hoodini servers found; use --server IP")
        server = _choose(servers, str)
    if input(f"Use server {server}? [y/N] ").lower() not in ("y", "yes"):
        raise PairCommandError("Server IP not confirmed")
    print("Recommend a DHCP reservation: the hood stores this IP.")
    config_dir = (
        args.config_dir
        or Path(os.environ.get("HOODINI_CONFIG_DIR", "~/.config/hoodini")).expanduser()
    )
    try:
        key = resolve_setup_key(config_dir)
    except ValueError as error:
        raise PairCommandError(str(error)) from None
    ssid = args.ssid or input("Wi-Fi SSID: ")
    psk = getpass("Wi-Fi password: ")
    try:
        records = await asyncio.to_thread(probe, server)
        baseline = {record["cuid"]: record.get("connection_id") for record in records}
    except Exception:  # noqa: BLE001 - sanitize external errors before display
        raise PairCommandError(
            "Server probe failed (TLS 8883 or LAN API 8765); check firewall/IP"
        ) from None
    print("Laptop reachability does not prove reachability from the hood's network.")
    try:
        transport = BleakTransport()
    except ProvisioningError:
        raise PairCommandError("Install hoodini[pairing] for BLE pairing") from None
    try:
        client = PairingClient(transport)
        targets = [
            target
            for target in await transport.discover(15)
            if target.name.startswith("ELC_")
        ]
        if not targets:
            raise PairCommandError(
                "No hood found: hold T6 for 6 s to reset Wi-Fi, then 6 s again; setup mode lasts 5 minutes"
            )
        target = _choose(targets, lambda target: target.name)
        async with client.session(target):
            networks = await client.networks(target)
            matches = [
                network for network in networks if network.ssid == ssid.encode("utf-8")
            ]
            if not matches:
                raise PairCommandError("SSID not seen (2.4 GHz only)")
            strongest = max(network.rssi for network in matches)
            network = _choose(
                [network for network in matches if network.rssi == strongest],
                lambda network: f"{network.ssid!r} ({network.auth})",
            )
            result = await client.pair(target, network, psk, server, key)
    except PairingError as error:
        raise PairCommandError(str(error)) from None
    except ProvisioningError as error:
        raise PairCommandError(str(error)) from None
    except PairCommandError:
        raise
    except Exception:  # noqa: BLE001 - sanitize external errors before display
        raise PairCommandError("BLE pairing failed") from None
    if not await _wait(server, result.cuid, baseline.get(result.cuid)):
        raise PairCommandError("server never saw the hood (firewall, wrong IP)")
    if not await _wait(server, result.cuid, baseline.get(result.cuid), status=True):
        print(
            f"Warning: first full status not received within {FIRST_STATUS_WAIT} s; HA entities may appear later"
        )
    endpoint = f"{server}:8765"
    token = args.token or load_token(endpoint, config_dir)
    if not token:
        other_tokens = list(config_dir.glob("token-*"))
        if other_tokens:
            print(
                "Token stored for another server endpoint; reuse only if you trust this server."
            )
            if len(other_tokens) == 1 and input(
                "Reuse it for this server? [y/N] "
            ).lower() in ("y", "yes"):
                token = other_tokens[0].read_text(encoding="utf-8")
        if not token:
            token = getpass("API token (Enter to skip metadata): ")
        if token and input("Save token for this server? [y/N] ").lower() in (
            "y",
            "yes",
        ):
            save_token(endpoint, token, config_dir)
    if not token:
        print("paired ✓ (generic profile: metadata not uploaded)")
        return
    try:
        await asyncio.to_thread(upload, server, result.cuid, result.info, token)
    except Exception:  # noqa: BLE001 - sanitize external errors before display
        raise PairCommandError("pairing succeeded, metadata upload failed") from None
    print("paired ✓")


def extract(args):
    config_dir = (
        args.config_dir
        or Path(os.environ.get("HOODINI_CONFIG_DIR", "~/.config/hoodini")).expanduser()
    )
    try:
        save_setup_key(extract_setup_key(args.apk), config_dir)
    except ExtractionError as error:
        raise PairCommandError(str(error)) from None
    print(f"setup key saved to {config_dir / 'setup_key'}")
