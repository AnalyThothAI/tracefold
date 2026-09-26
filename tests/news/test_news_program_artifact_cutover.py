"""Program cutover preserves the executable root on failure and retains history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.artifact_identity import canonical_json, canonical_sha
from tracefold.news.program import artifact_tool
from tracefold.news.program.artifact import build_code_owned_program_state, encode_program_state


def _historical_root(root: Path) -> tuple[str, str]:
    # An old graph cannot be decoded with today's Predictor set. Its declared
    # content identity can still be verified without executing it.
    state: dict[str, Any] = {
        "retired_predictor": {
            "traces": [],
            "train": [],
            "demos": [],
            "signature": {"instructions": "Historical instruction.", "fields": []},
            "lm": None,
        }
    }
    material = {
        "schema_version": "news_program_state_v1",
        "evidence_input_version": "news_evidence_input_v2",
        "dspy_version": "3.4.0",
        "predictors": ["retired_predictor"],
        "state": {
            name: {key: value for key, value in document.items() if key != "lm"}
            for name, document in state.items()
        },
    }
    identity = canonical_sha(material)
    document = canonical_json({**material, "state": state, "program_sha256": identity}) + "\n"
    (root / f"{identity}.json").write_text(document, encoding="utf-8")
    (root / "registry.json").write_text(
        canonical_json({"images": [identity], "stable": identity}) + "\n", encoding="utf-8"
    )
    return identity, document


def test_cutover_checks_history_without_loading_it_and_preserves_the_original(tmp_path: Path) -> None:
    previous, original = _historical_root(tmp_path)
    current = build_code_owned_program_state()

    assert artifact_tool.regenerate_stable_program_state(programs_root=tmp_path) == current.program_sha256
    assert json.loads((tmp_path / "registry.json").read_text()) == {
        "stable": current.program_sha256,
        "images": [current.program_sha256],
    }
    assert (tmp_path / f"{previous}.json").read_text() == original
    assert (tmp_path / f"{current.program_sha256}.json").read_text() == encode_program_state(current)


def test_bad_historical_hash_does_not_publish_or_build_a_new_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, _ = _historical_root(tmp_path)
    registry = (tmp_path / "registry.json").read_bytes()
    path = tmp_path / f"{previous}.json"
    raw = json.loads(path.read_text())
    raw["state"]["retired_predictor"]["signature"]["instructions"] = "Different content, unchanged hash."
    path.write_text(json.dumps(raw))

    def unexpected_build() -> Any:
        raise AssertionError("the old image must be checked before building")

    monkeypatch.setattr(artifact_tool, "build_code_owned_program_state", unexpected_build)
    with pytest.raises(ValueError, match="news_program_previous_image_identity_invalid"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert (tmp_path / "registry.json").read_bytes() == registry
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([f"{previous}.json", "registry.json"])


def test_build_failure_leaves_registry_and_history_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    previous, original = _historical_root(tmp_path)
    registry = (tmp_path / "registry.json").read_bytes()

    def failed_build() -> Any:
        raise ValueError("new_program_invalid")

    monkeypatch.setattr(artifact_tool, "build_code_owned_program_state", failed_build)
    with pytest.raises(ValueError, match="new_program_invalid"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert (tmp_path / "registry.json").read_bytes() == registry
    assert (tmp_path / f"{previous}.json").read_text() == original


def test_registry_publish_failure_keeps_the_old_root_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, original = _historical_root(tmp_path)
    registry_path = tmp_path / "registry.json"
    registry = registry_path.read_bytes()
    replace = artifact_tool.os.replace

    def fail_registry(source: Any, destination: Any) -> None:
        if Path(destination) == registry_path:
            raise OSError("registry_publish_failed")
        replace(source, destination)

    monkeypatch.setattr(artifact_tool.os, "replace", fail_registry)
    with pytest.raises(OSError, match="registry_publish_failed"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert registry_path.read_bytes() == registry
    assert (tmp_path / f"{previous}.json").read_text() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_regenerating_the_same_program_is_idempotent(tmp_path: Path) -> None:
    _historical_root(tmp_path)
    identity = artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert artifact_tool.regenerate_stable_program_state(programs_root=tmp_path) == identity
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_unknown_historical_identity_format_does_not_publish_a_new_registry(tmp_path: Path) -> None:
    previous, _ = _historical_root(tmp_path)
    registry = (tmp_path / "registry.json").read_bytes()
    path = tmp_path / f"{previous}.json"
    raw = json.loads(path.read_text())
    raw["schema_version"] = "news_program_state_unknown"
    path.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="news_program_previous_image_identity_invalid"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert (tmp_path / "registry.json").read_bytes() == registry
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([f"{previous}.json", "registry.json"])
