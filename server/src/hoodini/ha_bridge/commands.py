"""Strict Home Assistant command-to-capability translation."""

import json

from hoodini.devices.models import DeviceView


class HaCommandError(ValueError):
    """Invalid Home Assistant command (no untrusted input in messages)."""


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HaCommandError("invalid HA command")
        result[key] = value
    return result


def ha_command(view: DeviceView, entity: str, payload: bytes) -> dict[int, int]:
    """Return a report-independent, profile-bounded command recipe."""
    if entity not in {"fan", "light"}:
        raise HaCommandError("invalid HA entity")
    try:
        command = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise HaCommandError("invalid HA command") from exc
    if not isinstance(command, dict) or len(command) != 1:
        raise HaCommandError("invalid HA command")
    key, value = next(iter(command.items()))
    if key == "state" and value in ("ON", "OFF") and type(value) is str:
        if entity == "fan":
            return {110: (view.last_fan or 1) if value == "ON" else 0, 64: 1}
        return {96: (view.last_light or 100) if value == "ON" else 0}
    if entity == "fan":
        if key == "speed" and type(value) is int and 0 <= value <= view.profile.fan_max:
            return {110: value, 64: 1}
        if key == "preset_mode" and value == "boost" and 5 in view.profile.fan_modes:
            return {64: 5}
    elif key == "brightness" and type(value) is int and 0 <= value <= 100:
        return {96: value}
    raise HaCommandError("invalid HA command")
