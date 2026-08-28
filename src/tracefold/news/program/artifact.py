"""The content-addressed `ProgramStrategyArtifactV1`, its codec and its registry.

An artifact is one complete instruction per Predictor, under a named factory. `program_sha256` is the
canonical hash of those four values and nothing else, which is why two compiles that arrive at the same
two instructions are the same running Program however much they cost, whoever launched them, and whatever
trajectory they took.

Everything else the Program needs — the graph, the schemas, the normalizer, the assembler, the model route
and the execution budget — is code, versioned by `factory_id`. Copying it into the artifact and hashing it
there proved nothing this package did not already own.

Since #306 Phase 2 the instruction *is* the whole prompt rather than an advisory appended to one. There is
no renderer left: `seed.py` holds the reviewed baseline text, an artifact carries whatever text is current,
and `PredictorState.instruction` is that text unchanged. A human editing the seed and an optimizer
proposing a replacement are the same operation on the same string, held to the same bounds by
`validate_program_instruction` and released through the same candidate/canary pipeline.

`graph.py` executes an artifact; this module decides what a legal artifact *is*.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from .runtime import (
    _HIGH_CONFIDENCE_SECRET_PATTERNS,
    _MODEL_BINDING_SLOTS,
    _UNTRUSTED_EVENT_CLOSE,
    _UNTRUSTED_EVENT_OPEN,
    _VISIBLE_INPUT,
    PROGRAM_FACTORY_ID,
    PROGRAM_INSTRUCTION_MAX_BYTES,
    PROGRAM_INSTRUCTION_MAX_ESTIMATED_TOKENS,
    PROGRAM_PREDICTOR_MAX_TOKENS,
    PROGRAM_SCHEMA_VERSION,
    PredictorName,
    _estimated_tokens,
    _ExactModel,
    _reject_duplicate_keys,
    _reject_json_constant,
    _reject_nonfinite_json,
    _reject_unsafe_state,
    _require_nfc,
)
from .seed import seed_instruction

# Injection and credential shapes, not authority claims. #306 Phase 2 retired the authority patterns with
# the layering they policed: with one text per Predictor there is no lower-authority section for a sentence
# to claim to outrank, and the patterns' real effect was to refuse ordinary editorial prose — "never emit
# push for a scheduled item" is exactly the kind of rule a reviewed instruction is made of. What is left is
# the set of things that are never editorial content at any authority: a template engine, a script tag, a
# URL, a credential header, and a prompt-injection opener.
_FORBIDDEN_INSTRUCTION_MARKERS: tuple[str, ...] = (
    "{{",
    "{%",
    "{#",
    "<script",
    "api_key",
    "authorization:",
    "bearer ",
    "://",
    "ignore previous",
    "ignore all previous",
    "disregard previous",
)


def validate_program_instruction(value: str) -> str:
    """Apply the code-owned safety bounds to one complete Predictor instruction.

    The same function for both authors, deliberately. A human editing `seed.py` and an optimizer proposing
    a replacement are writing the same string, and the instruction proposer calls this while the model that
    wrote the text is still in the loop; a second implementation there would let the two drift.
    """

    _require_nfc(value, code="news_program_instruction_unicode_noncanonical")
    if not value.strip():
        raise ValueError("news_program_instruction_empty")
    if (
        len(value.encode("utf-8")) > PROGRAM_INSTRUCTION_MAX_BYTES
        or _estimated_tokens(value) > PROGRAM_INSTRUCTION_MAX_ESTIMATED_TOKENS
    ):
        raise ValueError("news_program_instruction_too_large")
    folded = value.casefold()
    if any(marker in folded for marker in _FORBIDDEN_INSTRUCTION_MARKERS):
        raise ValueError("news_program_instruction_unsafe")
    if any(pattern.search(value) for pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS):
        raise ValueError("news_program_instruction_secret")
    return value


class PredictorModelBindings(_ExactModel):
    primary: str
    fallback: str

    @field_validator("primary", "fallback")
    @classmethod
    def _known_slot(cls, value: str) -> str:
        if value not in _MODEL_BINDING_SLOTS:
            raise ValueError("news_program_model_binding_unknown")
        return value


class PredictorState(_ExactModel):
    """The derived, ready-to-execute state of one Predictor. Never stored; always rendered."""

    name: Literal["event_semantics", "reader_card"]
    instruction: str = Field(min_length=1, max_length=PROGRAM_INSTRUCTION_MAX_BYTES)
    model_bindings: PredictorModelBindings
    max_tokens: int = Field(ge=64, le=4096)

    @model_validator(mode="after")
    def _instruction_is_bounded(self) -> PredictorState:
        if len(self.instruction.encode("utf-8")) > PROGRAM_INSTRUCTION_MAX_BYTES:
            raise ValueError(f"news_program_{self.name}_instruction_too_large")
        return self


class ProgramStrategyArtifactV1(_ExactModel):
    """The complete write-set, and the whole of Program behavior identity.

    Two texts, and since #306 Phase 2 each one is the whole prompt for its Predictor rather than an
    addendum to a rendered stack. Nothing else about the shape changed: the same two fields, the same
    canonical hash over them and the factory, the same closed patch crossing the compiler boundary.
    """

    schema_version: Literal["news_program_strategy_artifact_v1"] = "news_program_strategy_artifact_v1"
    factory_id: Literal["tracefold.news.program.factory_v8"] = "tracefold.news.program.factory_v8"
    event_semantics_instruction: str
    reader_card_instruction: str
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def issue(cls, *, event_semantics_instruction: str, reader_card_instruction: str) -> ProgramStrategyArtifactV1:
        payload = {
            "schema_version": PROGRAM_SCHEMA_VERSION,
            "factory_id": PROGRAM_FACTORY_ID,
            "event_semantics_instruction": event_semantics_instruction,
            "reader_card_instruction": reader_card_instruction,
        }
        return cls(**payload, program_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _instructions_are_safe_and_identity_is_exact(self) -> ProgramStrategyArtifactV1:
        for predictor in ("event_semantics", "reader_card"):
            validate_program_instruction(self.instruction_for(predictor))
        if self.program_sha256 != self.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        return self

    def computed_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json", exclude={"program_sha256"}))

    def instruction_for(self, predictor: PredictorName) -> str:
        return self.event_semantics_instruction if predictor == "event_semantics" else self.reader_card_instruction

    def predictor_state(self, predictor: PredictorName) -> PredictorState:
        return build_predictor_state(predictor, self.instruction_for(predictor))

    @property
    def event_semantics(self) -> PredictorState:
        return self.predictor_state("event_semantics")

    @property
    def reader_card(self) -> PredictorState:
        return self.predictor_state("reader_card")


class ProgramStrategyPatchV1(_ExactModel):
    """The complete and exclusive optimizer write-set crossing the compiler boundary."""

    schema_version: Literal["news_program_strategy_patch_v1"] = "news_program_strategy_patch_v1"
    parent_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_semantics_instruction: str
    reader_card_instruction: str

    @classmethod
    def issue(
        cls,
        *,
        parent: ProgramStrategyArtifactV1,
        event_semantics_instruction: str,
        reader_card_instruction: str,
    ) -> ProgramStrategyPatchV1:
        return cls(
            parent_program_sha256=parent.program_sha256,
            event_semantics_instruction=event_semantics_instruction,
            reader_card_instruction=reader_card_instruction,
        )

    @model_validator(mode="after")
    def _write_set_is_safe(self) -> ProgramStrategyPatchV1:
        for predictor in ("event_semantics", "reader_card"):
            validate_program_instruction(self.instruction_for(predictor))
        return self

    def instruction_for(self, predictor: PredictorName) -> str:
        return self.event_semantics_instruction if predictor == "event_semantics" else self.reader_card_instruction


def build_predictor_state(predictor: PredictorName, instruction: str) -> PredictorState:
    """Bind one Predictor's instruction to its route and budget.

    There is no rendering step left. What the artifact carries is what the provider is sent, which is why
    the "optimized bytes equal production bytes" property is now structural rather than something a
    refactor-baseline test had to keep proving.
    """

    return PredictorState(
        name=predictor,
        instruction=validate_program_instruction(instruction),
        model_bindings=PredictorModelBindings(
            primary=f"{predictor}.primary",
            fallback=f"{predictor}.fallback",
        ),
        max_tokens=PROGRAM_PREDICTOR_MAX_TOKENS[predictor],
    )


def render_model_evidence_json(payload: Mapping[str, Any], *, predictor: PredictorName) -> str:
    """Canonicalize and visibly delimit the untrusted Event payload for exactly one Predictor.

    ``ModelVisibleCardInput`` forbids extra fields and has no ``event_status``, so a ReaderCard payload or
    recording that carries told history is rejected here rather than being caught by review later.
    """

    visible = _VISIBLE_INPUT[predictor].model_validate(payload).model_dump(mode="json")
    return f"{_UNTRUSTED_EVENT_OPEN}\n{canonical_json(visible)}\n{_UNTRUSTED_EVENT_CLOSE}"


def build_code_owned_program_artifact() -> ProgramStrategyArtifactV1:
    """Build the reviewed baseline root from the seed texts; callers decide where it may be stored."""

    return ProgramStrategyArtifactV1.issue(
        event_semantics_instruction=seed_instruction("event_semantics"),
        reader_card_instruction=seed_instruction("reader_card"),
    )


def apply_program_patch(
    parent: ProgramStrategyArtifactV1,
    patch: ProgramStrategyPatchV1,
) -> ProgramStrategyArtifactV1:
    """Apply an untrusted patch through the trusted, closed write-set."""

    active = load_stable_program_artifact()
    if parent.program_sha256 != active.program_sha256:
        raise ValueError("news_program_patch_parent_not_active_stable")
    if patch.parent_program_sha256 != parent.program_sha256:
        raise ValueError("news_program_patch_parent_identity_mismatch")
    return ProgramStrategyArtifactV1.issue(
        event_semantics_instruction=patch.event_semantics_instruction,
        reader_card_instruction=patch.reader_card_instruction,
    )


class ProgramStrategyArtifactCodec:
    """Strict codec for the sole supported artifact representation: one canonical JSON document."""

    @classmethod
    def _json_object(cls, document: str | bytes, *, kind: str) -> dict[str, Any]:
        try:
            text = document.decode("utf-8") if isinstance(document, bytes) else document
            raw = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"news_program_{kind}_json_invalid") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"news_program_{kind}_must_be_object")
        canonical_document = canonical_json(raw)
        if text not in {canonical_document, canonical_document + "\n"}:
            raise ValueError(f"news_program_{kind}_json_noncanonical")
        _reject_nonfinite_json(raw)
        _reject_unsafe_state(raw)
        return raw

    @classmethod
    def decode(cls, document: str | bytes) -> ProgramStrategyArtifactV1:
        raw = cls._json_object(document, kind="artifact")
        if raw.get("schema_version") != PROGRAM_SCHEMA_VERSION:
            raise ValueError("news_program_artifact_version_unsupported")
        try:
            artifact = ProgramStrategyArtifactV1.model_validate(raw)
        except ValidationError as exc:
            raise ValueError("news_program_artifact_schema_invalid") from exc
        if canonical_json(raw) != canonical_json(artifact.model_dump(mode="json")):
            raise ValueError("news_program_artifact_round_trip_mismatch")
        return artifact

    @staticmethod
    def encode(artifact: ProgramStrategyArtifactV1) -> str:
        payload = artifact.model_dump(mode="json")
        _reject_nonfinite_json(payload)
        _reject_unsafe_state(payload)
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        return canonical_json(payload) + "\n"

    @classmethod
    def load(cls, path: str | None = None) -> ProgramStrategyArtifactV1:
        if path is None:
            return load_stable_program_artifact()
        requested = Path(path)
        if ".." in requested.parts:
            raise ValueError("news_program_artifact_path_invalid")
        try:
            candidate = requested.resolve(strict=True)
        except OSError as exc:
            raise ValueError("news_program_artifact_path_invalid") from exc
        if requested.absolute() != candidate or requested.is_symlink() or not candidate.is_file():
            raise ValueError("news_program_artifact_path_invalid")
        artifact = cls.decode(candidate.read_text(encoding="utf-8"))
        if candidate.name != f"{artifact.program_sha256}.json":
            raise ValueError("news_program_artifact_file_identity_mismatch")
        return artifact


def load_stable_program_artifact() -> ProgramStrategyArtifactV1:
    """Load and re-verify the immutable code-owned stable artifact."""

    registry = _load_program_registry()
    return load_program_artifact(str(registry["stable"]))


def _programs_resource_root() -> Any:
    package_root = importlib.resources.files("tracefold.news.program")
    root = package_root.joinpath("resources")
    if not isinstance(root, Path):
        # Zip/importlib Traversables have no filesystem symlink surface.  Their
        # bytes still pass the same strict registry and artifact codec below.
        return root
    if not isinstance(package_root, Path):
        raise ValueError("news_program_registry_path_invalid")
    try:
        resolved_package_root = package_root.resolve(strict=True)
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("news_program_registry_path_invalid") from exc
    if (
        package_root.is_symlink()
        or root.is_symlink()
        or resolved.parent != resolved_package_root
        or not resolved.is_dir()
    ):
        raise ValueError("news_program_registry_path_invalid")
    return resolved


def _verified_resource_file(root: Any, name: str) -> Any:
    child = root.joinpath(name)
    if not isinstance(root, Path) or not isinstance(child, Path):
        return child
    try:
        resolved_root = root.resolve(strict=True)
        resolved_child = child.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("news_program_artifact_path_invalid") from exc
    if (
        child.is_symlink()
        or child.absolute() != resolved_child
        or resolved_child.parent != resolved_root
        or not resolved_child.is_file()
    ):
        raise ValueError("news_program_artifact_path_invalid")
    return resolved_child


def _load_program_registry() -> dict[str, Any]:
    root = _programs_resource_root()
    registry_resource = _verified_resource_file(root, "registry.json")
    if not registry_resource.is_file():
        raise ValueError("news_program_registry_path_invalid")
    raw = ProgramStrategyArtifactCodec._json_object(registry_resource.read_text(encoding="utf-8"), kind="registry")
    if set(raw) != {"stable", "images"} or not isinstance(raw["images"], list):
        raise ValueError("news_program_registry_schema_invalid")
    images = [str(value) for value in raw["images"]]
    if str(raw["stable"]) not in images or len(images) != len(set(images)):
        raise ValueError("news_program_registry_identity_invalid")
    if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in images):
        raise ValueError("news_program_registry_sha_invalid")
    return {"stable": str(raw["stable"]), "images": tuple(images)}


def load_program_artifact(program_sha256: str) -> ProgramStrategyArtifactV1:
    """Resolve one immutable image from the code-owned registry, never from a user path."""

    identity = str(program_sha256)
    registry = _load_program_registry()
    if identity not in registry["images"]:
        raise ValueError("news_program_artifact_not_registered")
    root = _programs_resource_root()
    image = _verified_resource_file(root, f"{identity}.json")
    if not image.is_file():
        raise ValueError("news_program_artifact_path_invalid")
    artifact = ProgramStrategyArtifactCodec.decode(image.read_text(encoding="utf-8"))
    if artifact.program_sha256 != identity:
        raise ValueError("news_program_artifact_file_identity_mismatch")
    return artifact


def write_program_candidate_artifact(artifact: ProgramStrategyArtifactV1, *, artifact_root: Path) -> str:
    """Persist one already trusted/applied artifact document atomically."""

    requested_root = Path(artifact_root)
    if ".." in requested_root.parts or requested_root.is_symlink():
        raise ValueError("news_program_compile_artifact_root_invalid")
    requested_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("news_program_compile_artifact_root_invalid") from exc
    if not root.is_dir() or requested_root.absolute().resolve() != root:
        raise ValueError("news_program_compile_artifact_root_invalid")
    document = ProgramStrategyArtifactCodec.encode(artifact)
    destination = root / f"{artifact.program_sha256}.json"
    if destination.exists():
        if ProgramStrategyArtifactCodec.load(str(destination)) != artifact:
            raise ValueError("news_program_compile_artifact_collision")
        return str(destination)
    temporary = root / f".{artifact.program_sha256}.{uuid.uuid4().hex}.tmp"
    try:
        _write_exclusive(temporary, document)
        os.rename(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if ProgramStrategyArtifactCodec.load(str(destination)) != artifact:
        raise ValueError("news_program_compile_artifact_write_verification_failed")
    return str(destination)


def _write_exclusive(path: Path, document: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        encoded = document.encode("utf-8")
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
