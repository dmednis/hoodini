import json
import stat
from pathlib import Path

import pytest
from hoodini.pairing import keys

FIXTURE = Path(__file__).parents[1] / "fixtures/extractor/methods.json"


def test_unique_literal_method():
    methods = json.loads(FIXTURE.read_text())["classes"]
    assert keys._literal_from_classes(methods) == "SYNTHETIC-SETUP-KEY"


@pytest.mark.parametrize("change", ["missing", "ambiguous", "nonliteral"])
def test_extraction_fails_closed(change):
    classes = json.loads(FIXTURE.read_text())["classes"]
    if change == "missing":
        classes[0]["name"] = "Lsynthetic/Other;"
    elif change == "ambiguous":
        classes.append(classes[0].copy())
    else:
        classes[0]["methods"][0]["instructions"] = [
            ["invoke-static", "v0"],
            ["return-object", "v0"],
        ]
    with pytest.raises(keys.ExtractionError):
        keys._literal_from_classes(classes)


def test_private_atomic_storage(tmp_path):
    directory = tmp_path / "config"
    keys.save_setup_key("SYNTHETIC-KEY", directory)
    keys.save_token("192.0.2.1:8765", "SYNTHETIC-TOKEN", directory)
    assert keys.load_setup_key(directory) == "SYNTHETIC-KEY"
    assert keys.load_token("192.0.2.1:8765", directory) == "SYNTHETIC-TOKEN"
    assert keys.load_token("192.0.2.2:8765", directory) is None
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())
