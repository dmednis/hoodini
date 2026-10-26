"""Dashboard projections use confirmed state and escape every dynamic value."""

from dataclasses import replace

import pytest
from hoodini.api.admin_ui import device_card, snapshot
from hoodini.devices.models import DeviceView
from hoodini.protocol.profiles import resolve_profile


def view(**kwargs):
    return replace(
        DeviceView(
            "synthetic-hood",
            {},
            resolve_profile({}, None),
            None,
            {},
            {96: 40, 110: 2, 64: 1},
            {96, 110, 64},
            -71,
            1720000000,
            40,
            2,
            "ready",
            True,
            "synthetic-session",
        ),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("changes", "status", "fan", "light"),
    [
        ({}, "Online", "Speed 2", "40%"),
        ({"caps": {96: 0, 110: 0, 64: 1}}, "Online", "Off", "Off"),
        ({"caps": {110: 0, 64: 5}}, "Online", "Boost", "Not reported"),
        (
            {"online": False},
            "Waiting for first status",
            "Awaiting fresh status",
            "Awaiting fresh status",
        ),
        (
            {"online": False, "session_state": "none"},
            "Disconnected",
            "Awaiting fresh status",
            "Awaiting fresh status",
        ),
        ({"session_state": "none"}, "Reconnecting", "Speed 2", "40%"),
    ],
)
def test_card_status_and_confirmed_readings(changes, status, fan, light):
    card = device_card(view(**changes), "synthetic-csrf")
    assert f">{status}</span>" in card
    assert f"<dd>{fan}</dd>" in card
    assert f"<dd>{light}</dd>" in card
    assert "UTC" in card


@pytest.mark.parametrize("override", [None, "GENERIC", "HIDDEN_ADVANCE"])
def test_selected_profile_override(override):
    card = device_card(view(profile_override=override), "synthetic-csrf")
    assert f'<option value="{override or ""}" selected>' in card
    assert card.count(" selected>") == 1
    assert "data-confirm=" in card
    assert '<input type="checkbox" required>' in card


def test_escape_all_projections_and_keep_private_metadata_out():
    card = device_card(
        view(cuid='<script>"&', info={"SERIAL": "private", "PRF": "private"}), '<bad>"'
    )
    assert "<script>" not in card and "<bad>" not in card
    assert "&lt;script&gt;&quot;&amp;" in card and "&lt;bad&gt;&quot;" in card
    assert "private" not in card and "SERIAL" not in card


def test_snapshot_empty_state_and_bounded_newest_first_diagnostics():
    class Devices:
        def list(self):
            return []

    class Diagnostics:
        def __init__(self):
            self.history = []

    diagnostics = Diagnostics()
    result = snapshot(Devices(), diagnostics, "synthetic-csrf")
    assert "No hoods paired yet" in result["devices"]
    assert result["summary"] == "0 paired · 0 online"
    assert "No diagnostic traffic" in result["diagnostics"]
    diagnostics.history = [
        {
            "kind": "mqtt",
            "direction": "in",
            "packet_type": 3,
            "length": n,
            "timestamp": n,
            "hex": '<script>"',
        }
        for n in range(100)
    ]
    result = snapshot(Devices(), diagnostics, "synthetic-csrf")
    assert (
        result["diagnostic_summary"]
        == "Latest 50 of 100 buffered records. Newest first."
    )
    assert result["diagnostics"].count('class="diagnostic"') == 50
    assert "<script>" not in result["diagnostics"]
    assert result["diagnostics"].index("99 bytes") < result["diagnostics"].index(
        "50 bytes"
    )
    assert "PUBLISH" in result["diagnostics"]
