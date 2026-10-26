"""Validated local runtime configuration; broker credentials never enter repr/errors."""

import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MqttSource = Literal["explicit", "supervisor", "unavailable"]


class ConfigError(ValueError):
    """Safe-to-display configuration failure."""


@dataclass(frozen=True, repr=False)
class MqttSettings:
    url: str | None = None
    username: str | None = None
    password: str | None = None
    tls: bool = False

    def __repr__(self) -> str:
        return "MqttSettings(<redacted>)"


@dataclass(frozen=True)
class GatewaySettings:
    bind: str = "0.0.0.0"


@dataclass(frozen=True)
class ApiSettings:
    port: int = 8765
    interfaces: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdminSettings:
    bind: str = "127.0.0.1:8099"


@dataclass(frozen=True, repr=False)
class Settings:
    mqtt: MqttSettings = field(default_factory=MqttSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    admin: AdminSettings = field(default_factory=AdminSettings)
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    mqtt_overrides: frozenset[str] = frozenset()
    mqtt_source: MqttSource | None = None

    def __repr__(self) -> str:
        return "Settings(<redacted>)"


def _section(raw: dict, name: str, allowed: set[str]) -> dict:
    value = raw.get(name, {})
    if type(value) is not dict or set(value) - allowed:
        raise ConfigError(f"Invalid {name} configuration.")
    return value


def _text(value: object, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value:
        raise ConfigError(f"Invalid {name} configuration.")
    return value


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"Invalid {name} configuration.")
    return value


def _list(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not list or any(
        type(item) is not str or not item for item in value
    ):
        raise ConfigError(f"Invalid {name} configuration.")
    return tuple(value)


def load_settings(config_path: Path | None, *, addon: bool = False) -> Settings:
    """Load standalone TOML or add-on JSON options without reflecting secrets."""
    if type(addon) is not bool:
        raise ConfigError("Invalid runtime mode.")
    raw = {}
    if config_path is not None:
        try:
            text = config_path.read_text(encoding="utf-8")
            raw = json.loads(text) if addon else tomllib.loads(text)
        except (OSError, UnicodeError, ValueError):
            # Parser errors may embed source lines and userinfo: exit the handler.
            raw = None
        if raw is None:
            raise ConfigError("Cannot read valid configuration.")
        if type(raw) is not dict:
            raise ConfigError("Invalid runtime configuration.")
    if set(raw) - {
        "mqtt",
        "gateway",
        "api",
        "admin",
        "data_dir",
        "log_level",
        "capture",
    }:
        raise ConfigError("Invalid runtime configuration.")
    if "capture" in raw:
        logging.getLogger(__name__).warning("Legacy capture option ignored.")
    mqtt = _section(raw, "mqtt", {"url", "username", "password", "tls"})
    gateway = _section(raw, "gateway", {"bind"})
    api = _section(raw, "api", {"port", "interfaces", "addresses"})
    admin = _section(raw, "admin", {"bind"})
    port = api.get("port", 8765)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("Invalid api.port configuration.")
    data_dir = _text(
        raw.get(
            "data_dir",
            os.environ.get("HOODINI_DATA_DIR", "/data" if addon else "./data"),
        ),
        "data_dir",
    )
    log_level = _text(raw.get("log_level", "INFO"), "log_level")
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("Invalid log_level configuration.")
    url = _text(mqtt.get("url"), "mqtt.url", optional=True)
    return Settings(
        mqtt=MqttSettings(
            url,
            _text(mqtt.get("username"), "mqtt.username", optional=True),
            _text(mqtt.get("password"), "mqtt.password", optional=True),
            _bool(mqtt.get("tls", False), "mqtt.tls"),
        ),
        gateway=GatewaySettings(_text(gateway.get("bind", "0.0.0.0"), "gateway.bind")),
        api=ApiSettings(
            port,
            _list(api.get("interfaces", []), "api.interfaces"),
            _list(api.get("addresses", []), "api.addresses"),
        ),
        admin=AdminSettings(_text(admin.get("bind", "127.0.0.1:8099"), "admin.bind")),
        data_dir=Path(data_dir),
        log_level=log_level,
        mqtt_overrides=frozenset(mqtt) if addon else frozenset(),
        mqtt_source="explicit" if url is not None else None,
    )
