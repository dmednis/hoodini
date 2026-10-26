"""Explicit, detached public LAN projection of an authoritative DeviceView."""


def as_public_dict(view):
    return {
        "cuid": view.cuid,
        "info": {"PRF": "PRF0182867A"} if view.info.get("PRF") == "PRF0182867A" else {},
        "profile": {
            "key": view.profile.key,
            "fan_max": view.profile.fan_max,
            "fan_modes": sorted(view.profile.fan_modes),
        },
        "profile_override": view.profile_override,
        "firmware": {str(key): value for key, value in view.firmware.items()},
        "caps": {str(key): value for key, value in view.caps.items()},
        "known_ids": sorted(view.known_ids),
        "rssi": view.rssi,
        "last_seen": view.last_seen,
        "last_light": view.last_light,
        "last_fan": view.last_fan,
        "session_state": view.session_state,
        "online": view.online,
        "connection_id": view.connection_id,
    }
