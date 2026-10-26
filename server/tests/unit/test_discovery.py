"""Device discovery, retained publications, and report-driven state."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hoodini.devices.models import DeviceView
from hoodini.ha_bridge.discovery import (
    availability_publication,
    bridge_publication,
    command_topic,
    config_topic,
    desired_components,
    discovery,
    forget_publications,
    parse_command_topic,
    state_payload,
    state_publication,
)
from hoodini.protocol.profiles import resolve_profile

FIXTURES = Path(__file__).parents[1] / "fixtures" / "discovery"
CAPS = {5: 1, 6: 0, 64: 5, 69: 7, 96: 37, 110: 0, 128: 82, 129: 61, 131: 3, 245: 8}


def view(profile="HIDDEN_ADVANCE", **overrides):
    baseline = DeviceView(
        "abc",
        {"PRF": "PRF0182867A"} if profile == "HIDDEN_ADVANCE" else {},
        resolve_profile({}, profile),
        None,
        {1: 12},
        CAPS,
        set(CAPS),
        -57,
        None,
        37,
        2,
        "ready",
        True,
        None,
    )
    return replace(baseline, **overrides)


@pytest.mark.parametrize("profile", ["hidden_advance", "generic"])
def test_discovery_snapshot(profile):
    data = json.loads(discovery(view(profile.upper()), set())[0].payload)
    assert data == json.loads((FIXTURES / f"{profile}.json").read_text())
    assert data["availability"] == [
        {"topic": "hoodini/bridge/state"},
        {"topic": "hoodini/abc/availability"},
    ]
    assert data["availability_mode"] == "all"
    assert data["o"] == {"name": "Hoodini"}
    assert data["cmps"]["fan"]["speed_range_min"] == 1
    assert data["cmps"]["fan"]["speed_range_max"] == 3
    assert data["cmps"]["light"]["brightness_scale"] == 100
    assert data["cmps"]["light"]["on_command_type"] == "first"
    assert "object_id" not in data["cmps"]["light"]
    assert data["cmps"]["cap_245"]["enabled_by_default"] is False
    assert data["cmps"]["cap_245"]["entity_category"] == "diagnostic"
    assert data["cmps"]["fan"]["command_template"] == ('{"state":{{ value | tojson }}}')
    assert data["cmps"]["fault"]["p"] == "binary_sensor"
    assert data["cmps"]["fault"]["default_entity_id"] == (
        "binary_sensor.hoodini_abc_fault"
    )
    assert data["cmps"]["fault"]["device_class"] == "problem"
    assert data["cmps"]["fault"]["entity_category"] == "diagnostic"


def test_unknown_metadata_is_omitted_and_unique_ids_survive_override():
    generic = view("GENERIC", info={}, firmware={}, profile_override="GENERIC")
    overridden = view(
        "HIDDEN_ADVANCE", info={}, firmware={}, profile_override="HIDDEN_ADVANCE"
    )
    initial = json.loads(discovery(generic, set())[0].payload)
    updated = json.loads(
        discovery(overridden, set(desired_components(generic)))[0].payload
    )
    assert "model" not in initial["dev"]
    assert "sw_version" not in initial["dev"]
    assert initial["cmps"]["fan"]["unique_id"] == updated["cmps"]["fan"]["unique_id"]


def test_cold_start_no_broken_controls():
    cold = view(known_ids=set(), caps={}, rssi=None)
    assert desired_components(cold) == {}
    assert json.loads(discovery(cold, set())[0].payload)["cmps"] == {}
    assert len(discovery(cold, set())) == 1


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        pytest.param({6: 4}, "ON", id="6-only-nonzero"),
        pytest.param({5: 0, 6: 0}, "OFF", id="both-zero"),
        pytest.param({5: 2}, "ON", id="5-only-nonzero"),
    ],
)
def test_fault_problem_uses_either_warning(caps, expected):
    device = view(known_ids=set(caps), caps=caps, rssi=None)
    payload = json.loads(discovery(device, set())[0].payload)
    fault = payload["cmps"]["fault"]
    assert fault["p"] == "binary_sensor"
    assert fault["device_class"] == "problem"
    assert {f"cap_{cap_id}" for cap_id in caps} <= payload["cmps"].keys()
    assert fault["value_template"] == (
        "{% if value_json.fault_warnings_main | int(0) != 0 or "
        "value_json.fault_warnings_UI | int(0) != 0 %}ON{% else %}OFF{% endif %}"
    )
    reported = json.loads(state_payload(device))
    assert (
        "ON"
        if reported.get("fault_warnings_main", 0) != 0
        or reported.get("fault_warnings_UI", 0) != 0
        else "OFF"
    ) == expected


def test_fault_removal_uses_binary_sensor_platform():
    previously_known = view(known_ids={5}, caps={5: 1}, rssi=None)
    disappeared = view(known_ids=set(), caps={}, rssi=None)
    publications = discovery(disappeared, set(desired_components(previously_known)))
    assert len(publications) == 2
    assert json.loads(publications[0].payload)["cmps"]["fault"] == {
        "p": "binary_sensor"
    }
    assert "fault" not in json.loads(publications[1].payload)["cmps"]


def test_growth_and_charcoal_bit_changes():
    cold = view(known_ids=set(), caps={}, rssi=None)
    grown = view(known_ids={64, 110, 96, 129, 131}, caps={131: 3})
    assert "fan" not in desired_components(cold)
    assert {"fan", "light", "charcoal_filter"} <= desired_components(grown).keys()
    removed = view(caps={**CAPS, 131: 1})
    assert "charcoal_filter" not in desired_components(removed)
    messages = discovery(removed, set(desired_components(view())))
    assert len(messages) == 2
    assert json.loads(messages[0].payload)["cmps"]["charcoal_filter"] == {"p": "sensor"}
    assert "charcoal_filter" not in json.loads(messages[1].payload)["cmps"]


def test_profile_change_preserves_identity_and_preset():
    hidden = json.loads(discovery(view(), set())[0].payload)
    generic = json.loads(discovery(view("GENERIC"), set())[0].payload)
    assert hidden["cmps"]["fan"]["unique_id"] == generic["cmps"]["fan"]["unique_id"]
    assert hidden["cmps"]["fan"]["preset_modes"] == ["boost"]
    assert "preset_modes" not in generic["cmps"]["fan"]
    assert len(discovery(view("GENERIC"), set(desired_components(view())))) == 1


def test_report_state_and_retained_topics():
    assert state_payload(view(caps={96: 37}, rssi=-57)) == (
        b'{"cooktop_light_intensity":37,"rssi":-57}'
    )
    state = json.loads(state_payload(view()))
    assert state["fan_speed"] == 0 and state["fan_mode1"] == 5
    assert state["cap_245"] == 8 and state["rssi"] == -57
    assert state_publication(view()).topic == "hoodini/abc/state"
    assert availability_publication(view(online=False)).payload == b"offline"
    assert bridge_publication(True).payload == b"online"
    assert all(p.retain and p.qos == 1 for p in forget_publications("abc"))
    assert [p.payload for p in forget_publications("abc")] == [b"", b"", b""]
    assert [p.topic for p in forget_publications("abc")] == [
        "homeassistant/device/hoodini_abc/config",
        "hoodini/abc/state",
        "hoodini/abc/availability",
    ]


@pytest.mark.parametrize(
    "topic",
    [
        "hoodini/+/set/fan",
        "hoodini/abc/set/fan/more",
        "hoodini/abc/set/unknown",
        "hoodini/#/set/light",
        "hoodini//set/fan",
        "other/abc/set/fan",
    ],
)
def test_parse_command_topic_rejects_invalid(topic):
    assert parse_command_topic(topic) is None


def test_command_topic_roundtrip():
    assert parse_command_topic(command_topic("abc", "fan")) == ("abc", "fan")
    assert config_topic("abc") == "homeassistant/device/hoodini_abc/config"


def test_raw_capabilities_and_rssi_persist_after_disconnect():
    disconnected = view(rssi=None)
    components = desired_components(disconnected)
    assert "rssi" in components
    assert {f"cap_{cap_id}" for cap_id in CAPS} <= components.keys()
    assert json.loads(state_payload(disconnected))["rssi"] is None
