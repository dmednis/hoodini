"""The installed console entry point has no optional dependency requirement."""

import logging
import subprocess
import sys
from pathlib import Path

import pytest
from hoodini import cli
from hoodini.config import Settings


def test_cli_version() -> None:
    result = subprocess.run(
        ["hoodini", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hoodini 0.1.0"


@pytest.mark.parametrize(
    ("mode", "environment", "argument", "expected"),
    [
        ("standalone", None, None, Path("./data")),
        ("addon", None, None, Path("/data")),
        ("standalone", "/image-data", None, Path("/image-data")),
        ("addon", "/image-data", None, Path("/image-data")),
        ("standalone", "/image-data", "/explicit", Path("/explicit")),
    ],
)
def test_token_data_dir_precedence(
    monkeypatch, capsys, mode, environment, argument, expected
):
    observed = []
    monkeypatch.setenv("HOODINI_MODE", mode)
    if environment is None:
        monkeypatch.delenv("HOODINI_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("HOODINI_DATA_DIR", environment)
    monkeypatch.setattr(
        cli, "ensure_api_token", lambda path: observed.append(path) or "synthetic-token"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["hoodini", "token"] + (["--data-dir", argument] if argument else []),
    )
    cli.main()
    assert observed == [expected]
    assert capsys.readouterr().out == "synthetic-token\n"


def test_serve_configures_log_level_before_start(monkeypatch):
    observed = []

    async def fake_serve(settings):
        root = logging.getLogger()
        handler = root.handlers[0]
        observed.append((root.level, handler.level, handler.stream is sys.stderr))

    monkeypatch.setattr(cli, "_serve", fake_serve)
    monkeypatch.setattr(
        cli, "load_settings", lambda *args, **kwargs: Settings(log_level="DEBUG")
    )
    monkeypatch.setattr(sys, "argv", ["hoodini", "serve"])
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        cli.main()
        assert observed == [(logging.DEBUG, logging.NOTSET, True)]
    finally:
        root.handlers = handlers
        root.setLevel(level)
