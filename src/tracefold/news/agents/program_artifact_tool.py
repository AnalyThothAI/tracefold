"""Build reviewed ProgramArtifact-v2 roots without any v1 runtime loader.

The default command replaces the package registry with the sole D stable
root.  The rollback profile requires an explicit non-package output directory
and writes a standalone Docker-overlay bundle; it never edits production
registry state.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Literal

from ..artifact_identity import canonical_json, canonical_sha
from .semantic_program import (
    PROGRAM_FACTORY_ID,
    PROGRAM_SCHEMA_VERSION,
    PROGRAM_VERSION,
    ProgramArtifact,
    ProgramArtifactCodec,
    build_code_owned_program_artifact_v2,
)

_LEGACY_PROGRAM_V3_SHA256 = "49643db931211aee7f1d4f5b7124345d45e18132b10628b85843c55e05dff8d5"
_LEGACY_PROGRAM_V3_STATE_SHA256 = "49c628d4168a57b96bc856dcee57f8f82b80c81e6c64e983402872f6b9bf2f71"
_ROLLBACK_PROFILE_SCHEMA = "tracefold_news_program_rollback_bundle_v1"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"news_program_duplicate_key:{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"news_program_json_nonfinite:{value}")


def _read_canonical_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"news_program_identity_file_invalid:{path.name}")
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"news_program_json_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"news_program_json_object_required:{path.name}")
    canonical = canonical_json(value)
    if text not in {canonical, canonical + "\n"}:
        raise ValueError(f"news_program_json_noncanonical:{path.name}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _require_sha256_identity(identity: str) -> None:
    if len(identity) != 64 or any(character not in "0123456789abcdef" for character in identity):
        raise ValueError("news_program_artifact_identity_invalid")


def _verify_raw_image(root: Path, identity: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_sha256_identity(identity)
    image = root / identity
    if image.is_symlink() or not image.is_dir():
        raise ValueError("news_program_stable_image_missing")
    if {path.name for path in image.iterdir()} != {"manifest.json", "state.json"}:
        raise ValueError("news_program_artifact_files_invalid")
    manifest = _read_canonical_object(image / "manifest.json")
    state = _read_canonical_object(image / "state.json")
    if manifest.get("program_sha256") != identity:
        raise ValueError("news_program_artifact_directory_identity_mismatch")
    if manifest.get("state_sha256") != canonical_sha(state):
        raise ValueError("news_program_state_hash_mismatch")
    if canonical_sha({key: value for key, value in manifest.items() if key != "program_sha256"}) != identity:
        raise ValueError("news_program_artifact_hash_mismatch")
    schema = manifest.get("schema_version")
    if schema != PROGRAM_SCHEMA_VERSION:
        raise ValueError("news_program_artifact_version_unsupported")
    return manifest, state


def _write_image(root: Path, artifact: ProgramArtifact) -> Path:
    manifest_document, state_document = ProgramArtifactCodec.encode(artifact)
    ProgramArtifactCodec.decode(manifest_document, state_document)
    image = root / artifact.program_sha256
    if image.exists():
        if image.is_symlink() or not image.is_dir():
            raise ValueError("news_program_artifact_path_invalid")
        existing = {path.name for path in image.iterdir()}
        if existing != {"manifest.json", "state.json"}:
            raise ValueError("news_program_artifact_files_invalid")
        if (image / "manifest.json").read_text(encoding="utf-8") != manifest_document or (
            image / "state.json"
        ).read_text(encoding="utf-8") != state_document:
            raise ValueError("news_program_artifact_existing_image_mismatch")
        return image
    image.mkdir()
    try:
        (image / "manifest.json").write_text(manifest_document, encoding="utf-8")
        (image / "state.json").write_text(state_document, encoding="utf-8")
        ProgramArtifactCodec.decode(
            (image / "manifest.json").read_text(encoding="utf-8"),
            (image / "state.json").read_text(encoding="utf-8"),
        )
    except Exception:
        for filename in ("manifest.json", "state.json"):
            candidate = image / filename
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
        image.rmdir()
        raise
    return image


def regenerate_stable_program_artifact(*, programs_root: Path | None = None) -> str:
    """Hard-cut the sole package stable root to the reviewed D v2 root."""

    module_dir = Path(__file__).resolve().parent
    root = (programs_root or module_dir / "programs").resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("news_program_registry_path_invalid")
    registry_path = root / "registry.json"
    registry = _read_canonical_object(registry_path)
    if set(registry) != {"stable", "images"} or not isinstance(registry["images"], list):
        raise ValueError("news_program_registry_schema_invalid")
    old_sha = str(registry["stable"])
    if [str(value) for value in registry["images"]] != [old_sha]:
        raise ValueError("news_program_regenerate_with_candidates_forbidden")
    old_manifest, _old_state = _verify_raw_image(root, old_sha)

    artifact = build_code_owned_program_artifact_v2(profile="d_stable")
    if old_sha == artifact.program_sha256:
        if old_manifest.get("schema_version") != PROGRAM_SCHEMA_VERSION:
            raise ValueError("news_program_stable_identity_schema_collision")
        manifest, state = ProgramArtifactCodec.encode(artifact)
        ProgramArtifactCodec.decode(manifest, state)
        return old_sha

    new_image = _write_image(root, artifact)
    _atomic_json(registry_path, {"images": [artifact.program_sha256], "stable": artifact.program_sha256})
    try:
        registered = _read_canonical_object(registry_path)
        if registered != {"images": [artifact.program_sha256], "stable": artifact.program_sha256}:
            raise ValueError("news_program_registry_switch_failed")
        ProgramArtifactCodec.decode(
            (new_image / "manifest.json").read_text(encoding="utf-8"),
            (new_image / "state.json").read_text(encoding="utf-8"),
        )
    except Exception:
        _atomic_json(registry_path, registry)
        raise

    old_image = root / old_sha
    for filename in ("manifest.json", "state.json"):
        (old_image / filename).unlink()
    old_image.rmdir()
    return artifact.program_sha256


def _rollback_profile(stable: ProgramArtifact, rollback: ProgramArtifact) -> dict[str, Any]:
    semantic_equivalence = {
        "profile": "reviewed_program_v3_semantic_state_v1",
        "legacy_program_sha256": _LEGACY_PROGRAM_V3_SHA256,
        "legacy_state_sha256": _LEGACY_PROGRAM_V3_STATE_SHA256,
        "legacy_event_semantics_instruction_sha256": canonical_sha(rollback.rule_packs[0].body),
        "legacy_reader_card_instruction_sha256": canonical_sha(rollback.rule_packs[1].body),
        "rollback_rule_pack_root_sha256": rollback.rule_pack_root_sha256,
        "rollback_learned_strategy_root_sha256": canonical_sha(
            [strategy.model_dump(mode="json") for strategy in rollback.learned_strategies]
        ),
    }
    return {
        "schema_version": _ROLLBACK_PROFILE_SCHEMA,
        "stable_program_sha256": stable.program_sha256,
        "rollback_program_sha256": rollback.program_sha256,
        "factory_id": PROGRAM_FACTORY_ID,
        "artifact_schema_version": PROGRAM_SCHEMA_VERSION,
        "program_version": PROGRAM_VERSION,
        "factory_source_sha256": rollback.quality_kernel.factory_source_sha256,
        "dependency_lock_sha256": rollback.quality_kernel.dependency_lock_sha256,
        "semantic_equivalence_profile_sha256": canonical_sha(semantic_equivalence),
    }


def generate_rollback_program_bundle(*, output_dir: Path) -> str:
    """Write a v2-only rollback registry for an explicit deployment overlay."""

    package_programs = (Path(__file__).resolve().parent / "programs").resolve()
    if output_dir.is_symlink():
        raise ValueError("news_program_rollback_output_invalid")
    requested = output_dir.resolve()
    if ".." in output_dir.parts or requested == package_programs or package_programs in requested.parents:
        raise ValueError("news_program_rollback_output_must_be_external")
    stable = build_code_owned_program_artifact_v2(profile="d_stable")
    rollback = build_code_owned_program_artifact_v2(profile="program_v3_rollback")
    if stable.program_sha256 == rollback.program_sha256:
        raise ValueError("news_program_rollback_identity_not_distinct")
    profile = _rollback_profile(stable, rollback)
    expected_children = {"registry.json", "profile.json", rollback.program_sha256}

    if requested.exists():
        if not requested.is_dir() or {path.name for path in requested.iterdir()} != expected_children:
            raise ValueError("news_program_rollback_output_not_empty")
    else:
        requested.mkdir(parents=True)
    image = _write_image(requested, rollback)
    _atomic_json(requested / "registry.json", {"images": [rollback.program_sha256], "stable": rollback.program_sha256})
    _atomic_json(requested / "profile.json", profile)
    manifest = _read_canonical_object(image / "manifest.json")
    if manifest.get("schema_version") != PROGRAM_SCHEMA_VERSION or "artifact_v1" in canonical_json(manifest):
        raise ValueError("news_program_rollback_contains_v1_manifest")
    if _read_canonical_object(requested / "profile.json") != profile:
        raise ValueError("news_program_rollback_profile_verification_failed")
    return rollback.program_sha256


def verify_rollback_program_bundle(*, input_dir: Path) -> str:
    """Read-only verification for the rollback Docker build overlay."""

    if input_dir.is_symlink():
        raise ValueError("news_program_rollback_input_invalid")
    root = input_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("news_program_rollback_input_invalid")
    registry = _read_canonical_object(root / "registry.json")
    profile = _read_canonical_object(root / "profile.json")
    if set(registry) != {"images", "stable"} or not isinstance(registry["images"], list):
        raise ValueError("news_program_rollback_registry_invalid")
    identity = str(registry["stable"])
    if [str(value) for value in registry["images"]] != [identity]:
        raise ValueError("news_program_rollback_registry_invalid")
    _require_sha256_identity(identity)
    if {path.name for path in root.iterdir()} != {"registry.json", "profile.json", identity}:
        raise ValueError("news_program_rollback_bundle_files_invalid")
    manifest, state = _verify_raw_image(root, identity)
    if manifest.get("schema_version") != PROGRAM_SCHEMA_VERSION or "artifact_v1" in canonical_json(
        {"manifest": manifest, "state": state, "profile": profile}
    ):
        raise ValueError("news_program_rollback_contains_v1_manifest")
    artifact = ProgramArtifactCodec.decode(canonical_json(manifest), canonical_json(state))
    stable = build_code_owned_program_artifact_v2(profile="d_stable")
    expected_rollback = build_code_owned_program_artifact_v2(profile="program_v3_rollback")
    if artifact.program_sha256 != expected_rollback.program_sha256:
        raise ValueError("news_program_rollback_root_not_current")
    if profile != _rollback_profile(stable, expected_rollback):
        raise ValueError("news_program_rollback_profile_mismatch")
    return artifact.program_sha256


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("d_stable", "program_v3_rollback"))
    parser.add_argument("--verify-profile", choices=("program_v3_rollback",))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    if args.verify_profile is not None:
        if args.profile is not None or args.output is not None or args.input is None:
            parser.error("--verify-profile requires --input and cannot be combined with --profile/--output")
        print(verify_rollback_program_bundle(input_dir=args.input))
        return
    profile: Literal["d_stable", "program_v3_rollback"] = args.profile or "d_stable"
    if profile == "d_stable":
        if args.output is not None or args.input is not None:
            parser.error("--output is only valid with --profile program_v3_rollback")
        print(regenerate_stable_program_artifact())
        return
    if args.output is None or args.input is not None:
        parser.error("--output is required with --profile program_v3_rollback")
    print(generate_rollback_program_bundle(output_dir=args.output))


if __name__ == "__main__":
    main()


__all__ = [
    "generate_rollback_program_bundle",
    "regenerate_stable_program_artifact",
    "verify_rollback_program_bundle",
]
