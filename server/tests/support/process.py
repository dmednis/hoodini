"""Own one installed CLI process and its loopback-only test ports."""

import asyncio
import os
import signal
import socket
import subprocess
import time
from pathlib import Path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerProcess:
    def __init__(self, directory: Path, broker_url: str):
        self.directory = directory
        self.directory.mkdir(exist_ok=True)
        self.gateway_port, self.api_port, self.admin_port = (
            free_port(),
            free_port(),
            free_port(),
        )
        assert len({self.gateway_port, self.api_port, self.admin_port}) == 3
        self.config = directory / "test.toml"
        self.config.write_text(
            '[gateway]\nbind = "127.0.0.1"\n'
            f"[api]\nport = {self.api_port}\n"
            f'[admin]\nbind = "127.0.0.1:{self.admin_port}"\n'
            f'[mqtt]\nurl = "{broker_url}"\n'
        )
        self.data = directory / "data"
        self.log = directory / "server.stderr"
        self.process = None
        self._stream = None

    async def start(self):
        assert self.process is None
        self._stream = self.log.open("ab")
        env = os.environ.copy()
        env.pop("HOODINI_MODE", None)
        env["HOODINI_TEST_GATEWAY_PORT"] = str(self.gateway_port)
        executable = Path(__file__).resolve().parents[3] / ".venv/bin/hoodini"
        self.process = await asyncio.to_thread(
            subprocess.Popen,
            [
                str(executable),
                "serve",
                "--config",
                str(self.config),
                "--data-dir",
                str(self.data),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stream,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(
                    f"CLI exited {self.process.returncode}: {self.log.read_text()}"
                )
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.api_port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.05)
        raise AssertionError(f"CLI not ready: {self.log.read_text()}")

    async def stop(self, *, crash=False):
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGKILL if crash else signal.SIGTERM)
            await asyncio.wait_for(asyncio.to_thread(process.wait), 10)
        finally:
            self.process = None
            self._stream.close()
            self._stream = None
