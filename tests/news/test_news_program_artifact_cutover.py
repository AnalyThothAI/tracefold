"""Current-image cutover, with no historical-schema compatibility requirement."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.program import artifact_tool
from tracefold.news.program.artifact import build_code_owned_program_state, encode_program_state


def test_new_image_does_not_need_a_decodable_old_image(tmp_path: Path) -> None:
    old = tmp_path / ("a" * 64 + ".json")
    old.write_text("an obsolete format, deliberately not JSON", encoding="utf-8")
    (tmp_path / "registry.json").write_text('{"obsolete": true}', encoding="utf-8")
    note = tmp_path / "note.json"
    note.write_text('{"unrelated": true}', encoding="utf-8")
    current = build_code_owned_program_state()

    assert artifact_tool.regenerate_stable_program_state(programs_root=tmp_path) == current.program_sha256
    assert not old.exists()
    assert note.exists()
    assert json.loads((tmp_path / "registry.json").read_text()) == {
        "stable": current.program_sha256, "images": [current.program_sha256],
    }
    assert (tmp_path / f"{current.program_sha256}.json").read_text() == encode_program_state(current)


def test_build_failure_does_not_remove_old_files_or_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text('{"old": true}', encoding="utf-8")
    old = tmp_path / ("a" * 64 + ".json")
    old.write_text("old", encoding="utf-8")

    def failed() -> Any:
        raise ValueError("new_program_invalid")

    monkeypatch.setattr(artifact_tool, "build_code_owned_program_state", failed)
    with pytest.raises(ValueError, match="new_program_invalid"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert registry.read_text() == '{"old": true}'
    assert old.read_text() == "old"


def test_publish_failure_keeps_old_root_and_cleans_temporaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text('{"old": true}', encoding="utf-8")
    old = tmp_path / ("a" * 64 + ".json")
    old.write_text("old", encoding="utf-8")
    replace = artifact_tool.os.replace

    def fail(source: Any, destination: Any) -> None:
        if Path(destination) == registry:
            raise OSError("publish_failed")
        replace(source, destination)

    monkeypatch.setattr(artifact_tool.os, "replace", fail)
    with pytest.raises(OSError, match="publish_failed"):
        artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    assert registry.read_text() == '{"old": true}'
    assert old.read_text() == "old"
    assert not list(tmp_path.glob(".*.tmp"))


def test_current_image_regeneration_is_idempotent(tmp_path: Path) -> None:
    first = artifact_tool.regenerate_stable_program_state(programs_root=tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert artifact_tool.regenerate_stable_program_state(programs_root=tmp_path) == first
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
