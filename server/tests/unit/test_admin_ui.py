"""Dashboard projections use confirmed state and escape every dynamic value."""

from dataclasses import replace

import pytest
from hoodini.api.admin_ui import broker_card, device_card, snapshot
from hoodini.config import MqttSettings
from hoodini.devices.models import DeviceView
from hoodini.ha_bridge.client import BrokerStatus, HaBridge
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
    assert set(result) == {
        "devices",
        "diagnostics",
        "broker",
        "summary",
        "diagnostic_summary",
    }
    assert ">Not configured</span>" in result["broker"]
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


SINCE = "2024-07-03 09:46:40 UTC"


@pytest.mark.parametrize(
    "status,source,badge,tone,texts",
    [
        (
            None,
            None,
            "Not configured",
            "muted",
            [
                "Add an [mqtt] section to the configuration and restart Hoodini.",
                "<dd>None</dd>",
                "<dd>Not configured</dd>",
                "HA announcements can be observed only while a broker connection is running.",
            ],
        ),
        (
            None,
            "unavailable",
            "Unavailable",
            "error",
            [
                "could not obtain the Supervisor MQTT service at startup",
                "restart Hoodini",
                "Supervisor MQTT service (lookup failed)",
            ],
        ),
        (
            BrokerStatus("connecting", "b:1883"),
            "supervisor",
            "Connecting",
            "waiting",
            [
                "<dd>Starting</dd>",
                "Connecting to the MQTT broker.",
                "Supervisor MQTT service",
            ],
        ),
        (
            BrokerStatus("connecting", "b:1883", since=1720000000),
            "explicit",
            "Connecting",
            "waiting",
            [SINCE, "Configured broker"],
        ),
        (
            BrokerStatus(
                "connecting", "b:1883", since=1720000000, reason="connection refused"
            ),
            "explicit",
            "Connecting",
            "waiting",
            ["Previous connection failed: connection refused. Trying again.", SINCE],
        ),
        (
            BrokerStatus("connected", "b:1883", since=1720000000),
            "explicit",
            "Connected",
            "success",
            [
                "Connected to the MQTT broker. Hoodini can publish discovery and state and receive commands.",
                SINCE,
                "Not announced yet",
            ],
        ),
        (
            BrokerStatus(
                "retrying", "b:1883", reason="connection refused", retry_at=1720000000.2
            ),
            "explicit",
            "Disconnected",
            "error",
            [
                "Broker connection failed: connection refused. Retrying in about 1 s.",
                "Not available",
            ],
        ),
        (
            BrokerStatus("retrying", retry_at=1720000000),
            None,
            "Disconnected",
            "error",
            ["Connection closed. Retrying shortly.", "Invalid broker URL"],
        ),
        (
            BrokerStatus("retrying", retry_at=1),
            None,
            "Disconnected",
            "error",
            ["Retrying shortly."],
        ),
        (
            BrokerStatus("retrying", reason="TLS handshake failed"),
            None,
            "Disconnected",
            "error",
            ["Broker connection failed: TLS handshake failed. Preparing to retry."],
        ),
        (
            BrokerStatus("retrying"),
            None,
            "Disconnected",
            "error",
            ["Connection closed. Preparing to retry."],
        ),
        (
            BrokerStatus("stopped"),
            None,
            "Stopped",
            "muted",
            [
                "The bridge is shutting down or stopped. Automatic retry is paused.",
                "Not available",
            ],
        ),
    ],
)
def test_broker_card_states(status, source, badge, tone, texts):
    card = broker_card(status, source, now=1720000000)
    assert f'class="badge {tone}">{badge}</span>' in card
    for text in texts:
        assert text in card


@pytest.mark.parametrize("state", ["connected", "retrying"])
@pytest.mark.parametrize("ha", [None, "online", "offline"])
def test_broker_card_ha_seen_is_last_observation(state, ha):
    card = broker_card(
        BrokerStatus(state, ha_state=ha, ha_seen_at=1720000000 if ha else None),
        "explicit",
        now=0,
    )
    assert f">{'Connected' if state == 'connected' else 'Disconnected'}</span>" in card
    if ha:
        assert f"{ha.capitalize()} — last received {SINCE}" in card
        assert "not a live availability check" in card
        assert "Retained announcements may describe an earlier event." in card
        assert ("It cannot be refreshed" in card) == (state != "connected")
        assert "Online since" not in card and "Offline since" not in card
    else:
        assert "Not announced yet" in card
        assert "No announcement yet is normal; entities may still work." in card


@pytest.mark.parametrize(
    "deadline,expected",
    [
        (0.01, "in about 1 s."),
        (1.01, "in about 2 s."),
        (0, "shortly."),
        (-1, "shortly."),
        (None, "Preparing to retry."),
    ],
)
def test_broker_card_retry_countdown_rounds_up_and_clamps(deadline, expected):
    assert expected in broker_card(
        BrokerStatus("retrying", retry_at=deadline), None, now=0
    )


def test_broker_card_escapes_every_dynamic_projection():
    marker = '<script>"&'
    card = broker_card(
        BrokerStatus("retrying", endpoint=marker, reason=marker), marker, now=0
    )
    assert marker not in card
    assert card.count("&lt;script&gt;&quot;&amp;") == 2
    assert "<dd>Not configured</dd>" in card


def test_broker_card_unknown_state_is_neutral_and_not_echoed():
    state = 'unexpected<script>"&'
    card = broker_card(BrokerStatus(state), None)
    assert 'class="badge muted">Unknown</span>' in card
    assert "Broker connection status is not available." in card
    assert "unexpected" not in card


def test_broker_card_never_discloses_connection_secrets():
    status = HaBridge(
        MqttSettings(
            "mqtt://SYNTHETICUSER:SYNTHETICURLSECRET@broker:1884/SYNTHETICPATH?SYNTHETICQUERY#SYNTHETICFRAGMENT",
            "SYNTHETICUSERNAME",
            "SYNTHETICPASSWORD",
        ),
        None,
        None,
    ).status
    card = broker_card(status, "explicit")
    assert "<dd>broker:1884</dd>" in card
    assert "SYNTHETIC" not in card + repr(status)
