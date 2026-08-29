"""Content-addressed Program artifact contracts retained across the #344 runtime hard cut."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.news.program.artifact import (
    ProgramStrategyArtifactCodec,
    ProgramStrategyArtifactV1,
    ProgramStrategyPatchV1,
    apply_program_patch,
    load_program_artifact,
    load_stable_program_artifact,
    write_program_candidate_artifact,
)


def _candidate(*, event_suffix: str = "\nCandidate event rule.", card_suffix: str = "") -> ProgramStrategyArtifactV1:
    stable = load_stable_program_artifact()
    return ProgramStrategyArtifactV1.issue(
        event_semantics_instruction=stable.event_semantics_instruction + event_suffix,
        reader_card_instruction=stable.reader_card_instruction + card_suffix,
    )


def test_program_identity_is_exactly_schema_and_two_instructions() -> None:
    stable = load_stable_program_artifact()

    assert set(stable.model_dump()) == {
        "schema_version",
        "event_semantics_instruction",
        "reader_card_instruction",
        "program_sha256",
    }
    assert stable.program_sha256 == stable.computed_sha256()
    assert _candidate().program_sha256 != stable.program_sha256
    assert _candidate(event_suffix="", card_suffix="\nCandidate card rule.").program_sha256 != stable.program_sha256


def test_artifact_codec_round_trips_canonical_json() -> None:
    artifact = _candidate()
    document = ProgramStrategyArtifactCodec.encode(artifact)

    assert document.endswith("\n")
    assert ProgramStrategyArtifactCodec.decode(document) == artifact
    assert json.loads(document)["program_sha256"] == artifact.program_sha256


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update(schema_version="news_program_strategy_artifact_v2"), "version_unsupported"),
        (lambda value: value.update(event_semantics_instruction="changed"), "schema_invalid"),
        (lambda value: value.update(extra="forbidden"), "schema_invalid"),
    ],
)
def test_artifact_codec_fails_closed_on_version_identity_or_extra_fields(mutate: object, code: str) -> None:
    raw = load_stable_program_artifact().model_dump(mode="json")
    mutate(raw)  # type: ignore[operator]

    with pytest.raises(ValueError, match=f"news_program_artifact_{code}"):
        ProgramStrategyArtifactCodec.decode(json.dumps(raw))


def test_artifact_codec_rejects_nonfinite_json() -> None:
    raw = load_stable_program_artifact().model_dump(mode="json")
    raw["not_a_contract_field"] = float("nan")

    with pytest.raises(ValueError, match="nonfinite"):
        ProgramStrategyArtifactCodec.decode(json.dumps(raw))


def test_patch_application_has_only_two_instruction_writes() -> None:
    stable = load_stable_program_artifact()
    patch = ProgramStrategyPatchV1.issue(
        parent=stable,
        event_semantics_instruction=stable.event_semantics_instruction + "\nCandidate event rule.",
        reader_card_instruction=stable.reader_card_instruction + "\nCandidate card rule.",
    )

    applied = apply_program_patch(stable, patch)

    assert applied.event_semantics_instruction == patch.event_semantics_instruction
    assert applied.reader_card_instruction == patch.reader_card_instruction
    assert applied.program_sha256 == applied.computed_sha256()


def test_patch_parent_mismatch_is_rejected() -> None:
    stable = load_stable_program_artifact()
    other = _candidate()
    patch = ProgramStrategyPatchV1.issue(
        parent=other,
        event_semantics_instruction=other.event_semantics_instruction,
        reader_card_instruction=other.reader_card_instruction,
    )

    with pytest.raises(ValueError, match="news_program_patch_parent_not_active_stable"):
        apply_program_patch(other, patch)
    with pytest.raises(ValueError, match="news_program_patch_parent_identity_mismatch"):
        apply_program_patch(stable, patch)


def test_registry_refuses_an_unregistered_identity() -> None:
    with pytest.raises(ValueError, match="news_program_artifact_not_registered"):
        load_program_artifact("f" * 64)


def test_missing_candidate_path_has_a_stable_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="news_program_artifact_path_invalid"):
        ProgramStrategyArtifactCodec.load(str(tmp_path / "missing.json"))


def test_candidate_write_is_idempotent_but_refuses_same_identity_different_document(tmp_path: Path) -> None:
    artifact = _candidate()
    path = Path(write_program_candidate_artifact(artifact, artifact_root=tmp_path))

    assert Path(write_program_candidate_artifact(artifact, artifact_root=tmp_path)) == path
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="news_program_compile_artifact_collision"):
        write_program_candidate_artifact(artifact, artifact_root=tmp_path)
