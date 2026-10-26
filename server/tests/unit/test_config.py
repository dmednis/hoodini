"""Safe runtime configuration and CLI behavior."""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hoodini.config import ConfigError, load_settings


def test_defaults_and_optional_broker():
    native = load_settings(None)
    assert native.data_dir == Path("./data")
    assert native.gateway.bind == "0.0.0.0"
    assert native.api.port == 8765
    assert native.admin.bind == "127.0.0.1:8099"
    assert native.mqtt.url is None and native.mqtt_source is None
    assert load_settings(None, addon=True).data_dir == Path("/data")
    assert load_settings(None, addon=True).mqtt_source is None


def test_toml_nested_settings_and_redaction(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'data_dir = "elsewhere"\nlog_level = "DEBUG"\ncapture = true\n'
        '[mqtt]\nurl = "mqtt://user:secret@broker"\nusername = "synthetic-user"\n'
        'password = "synthetic-password"\ntls = true\n[gateway]\nbind = "127.0.0.1"\n'
        '[api]\nport = 9000\ninterfaces = ["en0"]\naddresses = ["192.0.2.1"]\n'
        '[admin]\nbind = "127.0.0.1:8000"\n'
    )
    settings = load_settings(path)
    assert settings.mqtt.password == "synthetic-password"
    assert settings.mqtt_source == "explicit"
    assert settings.api.interfaces == ("en0",)
    assert settings.api.addresses == ("192.0.2.1",)
    assert settings.gateway.bind == "127.0.0.1"
    assert settings.api.port == 9000
    assert settings.data_dir == Path("elsewhere")
    assert settings.log_level == "DEBUG"
    assert "secret" not in repr(settings)
    assert "synthetic-user" not in repr(settings)


@pytest.mark.parametrize(
    "body",
    [
        '[mqtt]\npassword = "synthetic-secret"\nurl = "bad',
        "[api]\nport = true",
        "[api]\nport = 65536",
        "[gateway]\nport = 0",
        "[mqtt]\ntls = 1",
    ],
)
def test_invalid_config_errors_have_no_input_or_cause(tmp_path, body):
    path = tmp_path / "synthetic-secret.toml"
    path.write_text(body)
    with pytest.raises(ConfigError) as error:
        load_settings(path)
    assert "synthetic-secret" not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize("value", ["true", '"synthetic-secret"', "[1, 2]"])
def test_legacy_capture_toml_ignored_once(tmp_path, caplog, value):
    path = tmp_path / "config.toml"
    path.write_text(f"capture = {value}\n")
    with caplog.at_level(logging.INFO):
        settings = load_settings(path)
    assert not hasattr(settings, "capture")
    assert caplog.messages == ["Legacy capture option ignored."]
    assert "synthetic-secret" not in caplog.text


def test_cli_path_override_preserves_nested_config(tmp_path, monkeypatch):
    from hoodini import cli

    path = tmp_path / "settings.toml"
    path.write_text(
        'data_dir = "not-used"\ncapture = "legacy"\n[api]\nport = 9000\n[mqtt]\nurl = "mqtt://synthetic-broker"\n'
    )
    seen = []

    async def capture(settings):
        seen.append(settings)

    monkeypatch.setattr(cli, "_serve", capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hoodini",
            "serve",
            "--config",
            str(path),
            "--data-dir",
            str(tmp_path / "override"),
        ],
    )
    monkeypatch.setenv("HOODINI_MODE", "standalone")
    cli.main()
    assert seen[0].data_dir == tmp_path / "override"
    assert seen[0].api.port == 9000
    assert seen[0].mqtt_source == "explicit"


def test_cli_mode_and_token_synthetic_only(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('data_dir = "not-used"\n[api]\nport = 9000\n')
    env = {**os.environ, "HOODINI_MODE": "invalid"}
    result = subprocess.run(
        [sys.executable, "-m", "hoodini.cli", "serve", "--config", str(path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # The entrypoint main is invoked explicitly below; -m must support the same behavior.
    assert result.returncode != 0
    assert "Invalid HOODINI_MODE" in result.stderr
    assert "synthetic-secret" not in result.stderr
    token_dir = tmp_path / "synthetic-token"
    result = subprocess.run(
        [sys.executable, "-m", "hoodini.cli", "token", "--data-dir", str(token_dir)],
        env={**os.environ, "HOODINI_MODE": "standalone"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == (token_dir / "api_token").read_text()


@pytest.mark.parametrize("addon", [False, True])
def test_mqtt_source_is_internal_not_a_config_option(tmp_path, addon):
    path = tmp_path / ("options.json" if addon else "config.toml")
    path.write_text(
        '{"mqtt_source": "synthetic-secret"}'
        if addon
        else 'mqtt_source = "synthetic-secret"\n'
    )
    with pytest.raises(ConfigError) as caught:
        load_settings(path, addon=addon)
    assert str(caught.value) == "Invalid runtime configuration."
    assert caught.value.__cause__ is caught.value.__context__ is None
