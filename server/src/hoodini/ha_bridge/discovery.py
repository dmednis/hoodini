"""Pure Home Assistant MQTT device discovery and reported state."""

import json
from dataclasses import dataclass

from hoodini.devices.models import DeviceView
from hoodini.protocol.capabilities import CAPABILITIES

BRIDGE_STATE_TOPIC = "hoodini/bridge/state"
HA_STATUS_TOPIC = "homeassistant/status"
COMMAND_SUBSCRIPTION = "hoodini/+/set/+"


@dataclass(frozen=True)
class Publication:
    topic: str
    payload: bytes
    retain: bool
    qos: int


def _json(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _publish(topic: str, payload: bytes) -> Publication:
    return Publication(topic, payload, True, 1)


def config_topic(cuid: str) -> str:
    return f"homeassistant/device/hoodini_{cuid}/config"


def state_topic(cuid: str) -> str:
    return f"hoodini/{cuid}/state"


def availability_topic(cuid: str) -> str:
    return f"hoodini/{cuid}/availability"


def command_topic(cuid: str, entity: str) -> str:
    return f"hoodini/{cuid}/set/{entity}"


def parse_command_topic(topic: str) -> tuple[str, str] | None:
    parts = topic.split("/")
    if (
        len(parts) != 4
        or parts[0] != "hoodini"
        or not parts[1]
        or any(char in parts[1] for char in "+#")
        or parts[2] != "set"
        or parts[3] not in {"fan", "light"}
    ):
        return None
    return parts[1], parts[3]


def platform_for(key: str) -> str:
    if key == "fan":
        return "fan"
    if key == "light":
        return "light"
    if key == "fault":
        return "binary_sensor"
    if key in {"rssi", "boost_remaining"} or key.startswith("cap_"):
        return "sensor"
    if key in {"grease_filter", "charcoal_filter"}:
        return "sensor"
    raise ValueError("unknown HA component")


def desired_components(view: DeviceView) -> dict[str, str]:
    known = view.known_ids
    keys: list[str] = []
    if {110, 64} <= known:
        keys.append("fan")
    if 96 in known:
        keys.append("light")
    if 128 in known:
        keys.append("grease_filter")
    if 129 in known and 131 in known and view.caps.get(131, 0) & 2:
        keys.append("charcoal_filter")
    if 5 in known or 6 in known:
        keys.append("fault")
    if known:
        keys.append("rssi")
    if 69 in known:
        keys.append("boost_remaining")
    keys.extend(f"cap_{cap_id}" for cap_id in sorted(known))
    return {key: platform_for(key) for key in keys}


def _value(key: str) -> str:
    return "{{ value_json['" + key + "'] }}"


def _component(view: DeviceView, key: str, platform: str) -> dict:
    cuid = view.cuid
    data: dict = {
        "p": platform,
        "unique_id": f"hoodini_{cuid}_{key}",
        "default_entity_id": f"{platform}.hoodini_{cuid}_{key}",
        "name": key.replace("_", " ").title(),
        "state_topic": state_topic(cuid),
    }
    if key == "fan":
        data.update(
            command_topic=command_topic(cuid, key),
            command_template='{"state":{{ value | tojson }}}',
            state_value_template="{% if value_json.fan_speed | int(0) > 0 or value_json.fan_mode1 | int(0) == 5 %}ON{% else %}OFF{% endif %}",
            percentage_command_topic=command_topic(cuid, key),
            percentage_command_template='{"speed":{{ value | int }}}',
            percentage_state_topic=state_topic(cuid),
            percentage_value_template=_value("fan_speed"),
            speed_range_min=1,
            speed_range_max=view.profile.fan_max,
        )
        if 5 in view.profile.fan_modes:
            data.update(
                preset_modes=["boost"],
                preset_mode_command_topic=command_topic(cuid, key),
                preset_mode_command_template='{"preset_mode":{{ value | tojson }}}',
                preset_mode_state_topic=state_topic(cuid),
                preset_mode_value_template="{% if value_json.fan_mode1 | int(0) == 5 %}boost{% else %}None{% endif %}",
            )
    elif key == "light":
        data.update(
            schema="basic",
            command_topic=command_topic(cuid, key),
            payload_on='{"state":"ON"}',
            payload_off='{"state":"OFF"}',
            on_command_type="first",
            state_value_template="{% if value_json.cooktop_light_intensity | int(0) > 0 %}"
            + '{"state":"ON"}'
            + "{% else %}"
            + '{"state":"OFF"}'
            + "{% endif %}",
            brightness_state_topic=state_topic(cuid),
            brightness_value_template=_value("cooktop_light_intensity"),
            brightness_command_topic=command_topic(cuid, key),
            brightness_command_template='{"brightness":{{ value | int }}}',
            brightness_scale=100,
        )
    elif key == "fault":
        data.update(
            device_class="problem",
            entity_category="diagnostic",
            value_template="{% if value_json.fault_warnings_main | int(0) != 0 or "
            "value_json.fault_warnings_UI | int(0) != 0 %}ON{% else %}OFF{% endif %}",
        )
    else:
        source = {
            "grease_filter": 128,
            "charcoal_filter": 129,
            "boost_remaining": 69,
        }.get(key)
        if key == "rssi":
            field = "rssi"
        elif source is not None:
            field = CAPABILITIES[source].name
        else:
            cap_id = int(key.removeprefix("cap_"))
            field = CAPABILITIES[cap_id].name if cap_id in CAPABILITIES else key
        data["value_template"] = _value(field)
        if key in {"grease_filter", "charcoal_filter"}:
            data["unit_of_measurement"] = "%"
        else:
            data["entity_category"] = "diagnostic"
            if key.startswith("cap_"):
                data["enabled_by_default"] = False
            if key == "rssi":
                data["unit_of_measurement"] = "dBm"
            if key == "boost_remaining":
                data["name"] = "Boost remaining (inferred)"
                data["unit_of_measurement"] = "min"
    return data


def _config(view: DeviceView, components: dict[str, dict]) -> dict:
    device: dict = {
        "identifiers": [f"hoodini_{view.cuid}"],
        "manufacturer": "Elica",
        "name": f"Hoodini {view.cuid}",
    }
    if prf := view.info.get("PRF"):
        device["model"] = prf
    if view.firmware:
        device["sw_version"] = ", ".join(
            f"{key}:{value}" for key, value in sorted(view.firmware.items())
        )
    return {
        "dev": device,
        "o": {"name": "Hoodini"},
        "availability": [
            {"topic": BRIDGE_STATE_TOPIC},
            {"topic": availability_topic(view.cuid)},
        ],
        "availability_mode": "all",
        "cmps": components,
    }


def discovery(view: DeviceView, previous_components: set[str]) -> list[Publication]:
    desired = desired_components(view)
    components = {
        key: _component(view, key, platform) for key, platform in desired.items()
    }
    clean = _publish(config_topic(view.cuid), _json(_config(view, components)))
    removed = previous_components - desired.keys()
    if not removed:
        return [clean]
    with_removals = dict(components)
    with_removals.update({key: {"p": platform_for(key)} for key in sorted(removed)})
    return [
        _publish(config_topic(view.cuid), _json(_config(view, with_removals))),
        clean,
    ]


def state_payload(view: DeviceView) -> bytes:
    state = {
        CAPABILITIES[cap_id].name if cap_id in CAPABILITIES else f"cap_{cap_id}": value
        for cap_id, value in view.caps.items()
    }
    state["rssi"] = view.rssi
    return _json(state)


def state_publication(view: DeviceView) -> Publication:
    return _publish(state_topic(view.cuid), state_payload(view))


def availability_publication(view: DeviceView) -> Publication:
    return _publish(
        availability_topic(view.cuid), b"online" if view.online else b"offline"
    )


def bridge_publication(online: bool) -> Publication:
    return _publish(BRIDGE_STATE_TOPIC, b"online" if online else b"offline")


def forget_publications(cuid: str) -> list[Publication]:
    return [
        _publish(topic, b"")
        for topic in (config_topic(cuid), state_topic(cuid), availability_topic(cuid))
    ]
