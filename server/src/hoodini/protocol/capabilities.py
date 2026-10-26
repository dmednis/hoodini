"""Observed capability names and verification status; not write permissions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    name: str
    verification: str


CAPABILITIES: dict[int, Capability] = {
    5: Capability("fault_warnings_main", "inferred"),
    6: Capability("fault_warnings_UI", "inferred"),
    11: Capability("cooktop_type", "mapping_unknown"),
    45: Capability("sensor_calibration", "inferred"),
    48: Capability("motor_profile_level", "inferred"),
    53: Capability("actuator_command_status", "inferred"),
    64: Capability("fan_mode1", "verified"),
    68: Capability("fan_timeout1", "inferred"),
    69: Capability("fan_timeout2", "inferred"),
    71: Capability("remote_cmd", "verified"),
    72: Capability("remote_enabled", "inferred"),
    96: Capability("cooktop_light_intensity", "verified"),
    97: Capability("cooktop_light_color_temp", "physical_selector"),
    99: Capability("unknown", "unknown"),
    110: Capability("fan_speed", "verified"),
    128: Capability("grease_filter_level_pct", "inferred"),
    129: Capability("charcoal_filter_level_pct", "inferred"),
    131: Capability("filter_enabler", "inferred"),
}
