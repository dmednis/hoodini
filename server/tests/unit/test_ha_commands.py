"""Command recipes and strict input parsing."""

from dataclasses import replace

import pytest
from hoodini.devices.models import DeviceView
from hoodini.ha_bridge.commands import HaCommandError, ha_command
from hoodini.protocol.profiles import resolve_profile


def view(profile="HIDDEN_ADVANCE"):
    return DeviceView(
        "abc",
        {},
        resolve_profile({}, profile),
        None,
        {},
        {},
        {64, 96, 110},
        None,
        None,
        0,
        0,
        "ready",
        True,
        None,
    )


def test_fan_off_recipe():
    assert ha_command(view(), "fan", b'{"state":"OFF"}') == {110: 0, 64: 1}


def test_light_restore():
    assert ha_command(replace(view(), last_light=37), "light", b'{"state":"ON"}') == {
        96: 37
    }


@pytest.mark.parametrize(
    ("entity", "payload", "expected"),
    [
        ("fan", b'{"state":"ON"}', {110: 1, 64: 1}),
        ("fan", b'{"speed":0}', {110: 0, 64: 1}),
        ("fan", b'{"speed":2}', {110: 2, 64: 1}),
        ("fan", b'{"preset_mode":"boost"}', {64: 5}),
        ("light", b'{"state":"OFF"}', {96: 0}),
        ("light", b'{"brightness":0}', {96: 0}),
        ("light", b'{"brightness":100}', {96: 100}),
    ],
)
def test_command_recipes(entity, payload, expected):
    assert ha_command(view(), entity, payload) == expected


def test_fan_restores_confirmed_speed():
    assert ha_command(replace(view(), last_fan=3), "fan", b'{"state":"ON"}') == {
        110: 3,
        64: 1,
    }


@pytest.mark.parametrize(
    ("entity", "payload"),
    [
        ("fan", b"\xff"),
        ("fan", b"[]"),
        ("fan", b"{}"),
        ("fan", b'{"speed":1,"speed":2}'),
        ("fan", b'{"speed":1,"state":"ON"}'),
        ("fan", b'{"other":1}'),
        ("fan", b'{"speed":true}'),
        ("fan", b'{"speed":1.0}'),
        ("fan", b'{"speed":"1"}'),
        ("fan", b'{"speed":4}'),
        ("fan", b'{"speed":-1}'),
        ("fan", b'{"state":"on"}'),
        ("fan", b'{"preset_mode":"other"}'),
        ("light", b'{"brightness":101}'),
        ("light", b'{"brightness":false}'),
        ("light", b'{"state":"ON","brightness":50}'),
        ("other", b'{"state":"ON"}'),
    ],
)
def test_reject_malformed_command(entity, payload):
    with pytest.raises(HaCommandError) as exc:
        ha_command(view(), entity, payload)
    assert str(exc.value) in {"invalid HA command", "invalid HA entity"}
    assert "brightness" not in str(exc.value)


def test_generic_rejects_boost():
    with pytest.raises(HaCommandError):
        ha_command(view("GENERIC"), "fan", b'{"preset_mode":"boost"}')
