"""Guard the synthetic public fixtures against accidental private data reuse."""
import json
from pathlib import Path


def test_public_examples_are_synthetic_and_private_config_is_absent():
    export = json.loads(Path("examples/desktop-export.json").read_text())
    assert export["id"] == 1
    assert [m["id"] for m in export["messages"]] == list(range(1, 9))
    assert all(m["from_id"] in {f"user{i}" for i in range(101,106)} for m in export["messages"])
    assert json.loads(Path("config/source_profiles.json").read_text())["profiles"] == []
    assert not Path("config/export_fingerprint.json").exists()
