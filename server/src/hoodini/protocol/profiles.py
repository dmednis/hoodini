"""Model-specific write permissions for observed capabilities."""

from dataclasses import dataclass


class ValidationError(ValueError):
    """A requested capability write is not permitted."""


@dataclass(frozen=True)
class Profile:
    key: str
    fan_max: int
    fan_modes: frozenset[int]


_PROFILES = {
    "GENERIC": Profile("GENERIC", 3, frozenset({1})),
    "HIDDEN_ADVANCE": Profile("HIDDEN_ADVANCE", 3, frozenset({1, 5})),
}


def resolve_profile(info: dict, override: str | None) -> Profile:
    key = (
        override
        if override is not None
        else ("HIDDEN_ADVANCE" if info.get("PRF") == "PRF0182867A" else "GENERIC")
    )
    try:
        return _PROFILES[key]
    except (KeyError, TypeError) as exc:
        raise ValidationError("unknown profile override") from exc


def validate_caps(
    profile: Profile, known: set[int], caps: dict[int, int]
) -> dict[int, int]:
    if not caps:
        raise ValidationError("empty command")
    for cap_id, value in caps.items():
        if type(cap_id) is not int or type(value) is not int:
            raise ValidationError("capability ID and value must be integers")
        if cap_id not in known:
            raise ValidationError("capability not observed on device")
        if cap_id == 96:
            allowed = 0 <= value <= 100
        elif cap_id == 110:
            allowed = 0 <= value <= profile.fan_max
        elif cap_id == 64:
            allowed = value in profile.fan_modes
        else:
            allowed = False
        if not allowed:
            raise ValidationError("capability write not permitted by profile")
    return dict(caps)
