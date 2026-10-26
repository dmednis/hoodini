"""Local Hoodini commands (no implicit broker or add-on supervisor startup)."""

import argparse
import asyncio
import logging
import os
import signal
from dataclasses import replace
from pathlib import Path

from hoodini import __version__
from hoodini.app import Runtime, RuntimeError
from hoodini.config import ConfigError, load_settings
from hoodini.security import SecurityError, ensure_api_token
from hoodini.supervisor import SupervisorClient, SupervisorError, resolve_mqtt_service


async def _serve(settings):
    ingress_context = None
    if os.environ.get("HOODINI_MODE") == "addon":
        async with SupervisorClient() as supervisor:
            ingress_context = await supervisor.self_info()
            settings = await resolve_mqtt_service(settings, supervisor)
    # Test-only loopback gateway port; the user-facing gateway remains fixed at 8883.
    test_port = os.environ.get("HOODINI_TEST_GATEWAY_PORT")
    if test_port is not None and (
        settings.gateway.bind != "127.0.0.1"
        or not test_port.isascii()
        or not test_port.isdecimal()
        or not 1 <= int(test_port) <= 65535
    ):
        raise ConfigError("Invalid test-only loopback gateway port.")
    runtime = Runtime(
        settings,
        ingress_context=ingress_context,
        gateway_port=int(test_port) if test_port is not None else 8883,
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopping.set)
    try:
        await runtime.start()
        try:
            await stopping.wait()
        finally:
            await runtime.stop()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    parser = argparse.ArgumentParser(prog="hoodini")
    parser.add_argument("--version", action="version", version=f"hoodini {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run local TLS gateway")
    serve.add_argument("--config", type=Path)
    serve.add_argument("--data-dir", type=Path)
    token = commands.add_parser("token", help="print the private local API token")
    token.add_argument("--data-dir", type=Path)
    pairing = commands.add_parser("pair", help="provision a hood on the local server")
    pairing.add_argument("--server", help="IPv4 address (skips Bonjour)")
    pairing.add_argument("--ssid", help="intended Wi-Fi SSID")
    pairing.add_argument(
        "--verbose",
        action="store_true",
        help="log BLE frames (Wi-Fi password and setup key are redacted)",
    )
    pairing.add_argument(
        "--token",
        help="API token (may leak via shell history/process list; prefer the prompt)",
    )
    pairing.add_argument("--config-dir", type=Path)
    extraction = commands.add_parser(
        "extract-key", help="save setup key from an APK (hoodini[extract])"
    )
    extraction.add_argument("apk", type=Path)
    extraction.add_argument("--config-dir", type=Path)
    args = parser.parse_args()
    mode = os.environ.get("HOODINI_MODE", "standalone")
    if mode not in ("standalone", "addon"):
        parser.error("Invalid HOODINI_MODE; choose standalone or addon.")
    try:
        if args.command in ("pair", "extract-key"):
            from hoodini.pairing.cli import PairCommandError, extract, pair

            try:
                if args.command == "pair":
                    if args.verbose:
                        logging.basicConfig(
                            format="%(asctime)s %(name)s: %(message)s", force=True
                        )
                        logging.getLogger("hoodini.pairing").setLevel(logging.DEBUG)
                    asyncio.run(pair(args))
                else:
                    extract(args)
            except PairCommandError as error:
                parser.exit(1, f"hoodini: {error}\n")
        elif args.command == "token":
            directory = (
                args.data_dir
                if args.data_dir is not None
                else load_settings(None, addon=mode == "addon").data_dir
            )
            print(ensure_api_token(directory))
        else:
            if mode == "addon" and (
                args.config is not None or args.data_dir is not None
            ):
                raise ConfigError(
                    "Add-on configuration is read from /data/options.json."
                )
            settings = load_settings(
                Path("/data/options.json") if mode == "addon" else args.config,
                addon=mode == "addon",
            )
            if mode == "addon":
                settings = replace(settings, data_dir=Path("/data"))
            elif args.data_dir is not None:
                settings = replace(settings, data_dir=args.data_dir)
            logging.basicConfig(
                level=settings.log_level,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                force=True,
            )
            asyncio.run(_serve(settings))
    except (ConfigError, SecurityError, RuntimeError, SupervisorError) as error:
        parser.exit(1, f"hoodini: {error}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
