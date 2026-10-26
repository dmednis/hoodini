"""Capability write permission is profile- and observation-scoped."""

import pytest
from hoodini.protocol.profiles import (
    Profile,
    ValidationError,
    resolve_profile,
    validate_caps,
)


def test_prf_profile() -> None:
    assert resolve_profile({"PRF": "PRF0182867A"}, None).key == "HIDDEN_ADVANCE"


def test_override_takes_precedence_over_prf() -> None:
    assert resolve_profile({"PRF": "PRF0182867A"}, "GENERIC") == Profile(
        "GENERIC", 3, frozenset({1})
    )
    assert resolve_profile({}, "HIDDEN_ADVANCE") == Profile(
        "HIDDEN_ADVANCE", 3, frozenset({1, 5})
    )


def test_unrecognized_prf_is_generic() -> None:
    assert resolve_profile({"PRF": "other"}, None) == Profile(
        "GENERIC", 3, frozenset({1})
    )


def test_unknown_override_rejected() -> None:
    with pytest.raises(ValidationError):
        resolve_profile({}, "UNKNOWN")


@pytest.mark.parametrize("caps", [{96: 100}, {110: 3, 64: 1}, {64: 5}])
def test_hidden_advance_accepts_observed_writes(caps: dict[int, int]) -> None:
    assert (
        validate_caps(resolve_profile({"PRF": "PRF0182867A"}, None), set(caps), caps)
        == caps
    )


def test_generic_accepts_light_speed_and_normal_mode() -> None:
    profile = resolve_profile({}, None)
    assert validate_caps(profile, {96, 110, 64}, {96: 0, 110: 3, 64: 1}) == {
        96: 0,
        110: 3,
        64: 1,
    }


@pytest.mark.parametrize(
    ("known", "caps"),
    [
        ({71}, {71: 1}),  # reserved remote command
        ({45}, {45: 0}),  # diagnostic/config, despite being observed
        ({11}, {11: 3}),  # cooktop type
        ({97}, {97: 0}),  # color temperature
        ({68}, {68: 5}),  # timer
        ({128}, {128: 100}),  # filter
        ({250}, {250: 1}),  # unknown even if known by a caller
        (set(), {96: 30}),  # allowed ID not observed by this device
        ({96}, {}),
        ({96}, {96: -1}),
        ({96}, {96: 101}),
        ({110}, {110: 4}),
        ({64}, {64: 4}),
        ({96}, {96: True}),
        ({96}, {96: 0.5}),
        ({True}, {True: 1}),
        ({96}, {96.0: 1}),
        ({96}, {96: 1, 71: 1}),
    ],
)
def test_reject_unauthorized_caps(known: set[int], caps: dict[int, int]) -> None:
    with pytest.raises(ValidationError):
        validate_caps(Profile("HIDDEN_ADVANCE", 3, frozenset({1, 5})), known, caps)


def test_generic_rejects_boost() -> None:
    with pytest.raises(ValidationError):
        validate_caps(resolve_profile({}, None), {64}, {64: 5})


def test_validation_preserves_recipe_order_without_adding_mode() -> None:
    caps = {110: 2, 64: 1}
    result = validate_caps(resolve_profile({}, None), {110, 64}, caps)
    assert list(result.items()) == [(110, 2), (64, 1)]
    assert validate_caps(resolve_profile({}, None), {110}, {110: 0}) == {110: 0}
