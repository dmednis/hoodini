"""Command bytes from distinct observed transcriptions and constructed recipes."""

import json
from pathlib import Path

import pytest
from hoodini.protocol.frames import FrameError, encode_command
from hoodini.protocol.profiles import resolve_profile, validate_caps

_FIXTURES = Path(__file__).resolve().parents[3] / "protocol-fixtures"


@pytest.mark.parametrize(
    ("source", "evidence", "count"),
    [
        ("observed-mqtt.json", "observed_transcription", 2),
        ("supplemental-mqtt.json", "recipe_instantiation", 6),
    ],
)
def test_command_fixture_bytes(source: str, evidence: str, count: int) -> None:
    fixtures = json.loads((_FIXTURES / source).read_text())["fixtures"]
    vectors = [entry for entry in fixtures if entry["kind"] == "command"]
    assert len(vectors) == count
    for entry in vectors:
        assert entry["evidence"] == evidence, entry["id"]
        caps = dict(entry["expected"]["caps"])
        validated = validate_caps(
            resolve_profile({"PRF": "PRF0182867A"}, None), set(caps), caps
        )
        assert encode_command(entry["expected"]["seq"], validated) == bytes.fromhex(
            entry["payload_hex"]
        ), entry["id"]


def test_literal_trailer_once_and_max_sequence() -> None:
    assert encode_command(255, {96: 100}) == bytes.fromhex(
        "002a8dff00007530006064004701000067"
    )


@pytest.mark.parametrize("seq", [-1, 256, True, 1.5])
def test_reject_non_byte_sequence(seq: int) -> None:
    with pytest.raises(FrameError):
        encode_command(seq, {96: 1})


@pytest.mark.parametrize(
    "caps",
    [
        {},
        {71: 1},
        {96: -1},
        {96: 65536},
        {256: 1},
        {-1: 1},
        {True: 1},
        {96.0: 1},
        {96: True},
        {96: 1.5},
    ],
)
def test_reject_invalid_wire_entries(caps: dict[int, int]) -> None:
    with pytest.raises(FrameError):
        encode_command(0, caps)
