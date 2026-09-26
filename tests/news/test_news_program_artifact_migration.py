"""A schema cut preserves the old image and publishes only verified new state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.news.artifact_identity import canonical_json, canonical_sha
from tracefold.news.program import artifact_tool
from tracefold.news.program.artifact import build_code_owned_program_state, encode_program_state
from tracefold.news.program.artifact_history import historical_program_document


def _history(root: Path) -> tuple[str, str]:
    # A real v1 projection, deliberately not the current code's Signature shape.
    raw = {
        "schema_version": "news_program_state_v1",
        "evidence_input_version": "news_evidence_input_v2",
        "dspy_version": "3.4.0",
        "predictors": ["retired_predictor"],
        "state": {"retired_predictor": {"signature": {"instructions": "Historical text"}, "lm": None}},
    }
    material = {**raw, "state": {"retired_predictor": {"signature": {"instructions": "Historical text"}}}}
    sha = canonical_sha(material)
    document = canonical_json({**raw, "program_sha256": sha}) + "\n"
    (root / f"{sha}.json").write_text(document)
    (root / "registry.json").write_text(canonical_json({"images": [sha], "stable": sha}) + "\n")
    return sha, document


def test_regeneration_preserves_history_but_only_registers_current_graph(tmp_path: Path) -> None:
    old_sha, document = _history(tmp_path)
    sha = artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    state = build_code_owned_program_state()
    assert sha == state.program_sha256 != old_sha
    assert (tmp_path / f"{old_sha}.json").read_text() == document
    assert (tmp_path / f"{sha}.json").read_text() == encode_program_state(state)
    assert json.loads((tmp_path / "registry.json").read_text()) == {"images": [sha], "stable": sha}
    assert historical_program_document(document, expected_sha=old_sha)["predictors"] == ["retired_predictor"]
    assert artifact_tool.regenerate_stable_program_state(programs_root=tmp_path) == sha


@pytest.mark.parametrize("damage", ["wrong_hash", "changed_instruction", "unsupported_schema", "malformed_state"])
def test_invalid_history_cannot_switch_registry(tmp_path: Path, damage: str) -> None:
    sha, document = _history(tmp_path)
    raw = json.loads(document)
    if damage == "wrong_hash":
        raw["program_sha256"] = "0" * 64
    elif damage == "changed_instruction":
        raw["state"]["retired_predictor"]["signature"]["instructions"] = "Changed"
    elif damage == "unsupported_schema":
        raw["schema_version"] = "unknown"
    else:
        raw["state"] = []
    (tmp_path / f"{sha}.json").write_text(canonical_json(raw))
    before = (tmp_path / "registry.json").read_bytes()
    with pytest.raises(ValueError, match="news_program_previous_image"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert (tmp_path / "registry.json").read_bytes() == before
    assert {path.name for path in tmp_path.iterdir()} == {"registry.json", f"{sha}.json"}


def test_new_image_failure_does_not_switch_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _history(tmp_path)
    before = (tmp_path / "registry.json").read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise ValueError("image verification failed")

    monkeypatch.setattr(artifact_tool, "_write_image", fail)
    with pytest.raises(ValueError, match="image verification failed"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert (tmp_path / "registry.json").read_bytes() == before


def test_failed_atomic_replace_keeps_registry_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(tmp_path)
    path = tmp_path / "registry.json"
    before = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk write failed")

    monkeypatch.setattr(artifact_tool.os, "replace", fail)
    with pytest.raises(OSError, match="disk write failed"):
        artifact_tool._atomic_json(path, {"images": ["new"], "stable": "new"})
    assert path.read_bytes() == before
    assert not tuple(tmp_path.glob(".*.tmp"))
