"""The content-addressed `ProgramStrategyArtifactV1`, its codec and its registry.

An artifact is one complete instruction per Predictor. `program_sha256` is the canonical hash of those two
texts and the schema version, and nothing else, which is why two compiles that arrive at the same two
instructions are the same running Program however much they cost, whoever launched them, and whatever
trajectory they took.

Everything else the Program needs — the graph, the schemas, the normalizer, the assembler, the model route
and the execution budget — is code, and `identity.compute_execution_identity` hashes what that code
renders (#314). It used to be declared here instead, as a `factory_id` literal somebody had to remember to
bump; the artifact carried the literal and hashed it, which made a forgotten bump indistinguishable from
no change at all.

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
    _MODEL_BINDING_SLOTS,
    _UNTRUSTED_EVENT_CLOSE,
    _UNTRUSTED_EVENT_OPEN,
    _VISIBLE_INPUT,
    PROGRAM_INSTRUCTION_MAX_BYTES,
    PROGRAM_INSTRUCTION_MAX_ESTIMATED_TOKENS,
    PROGRAM_PREDICTOR_MAX_TOKENS,
    PROGRAM_SCHEMA_VERSION,
    PredictorName,
    _estimated_tokens,
    _ExactModel,
    _reject_nonfinite_json,
    _require_nfc,
)
from .seed import seed_instruction


def validate_program_instruction(value: str) -> str:
    """The bounds one complete Predictor instruction must satisfy to be optimizable.

    Three of them, and each one is here because the optimization loop needs it, not because a text could
    be hostile (#319). NFC because two encodings of the same characters hash differently and the whole
    cohort model rests on that hash. The byte and token ceilings because every call pays for this text and
    an unbounded instruction breaks the context and the budget. Non-empty because there is no such thing as
    a Predictor with no prompt.

    What went with #319: an injection-marker blacklist (`{{`, `<script`, `://`, "ignore previous") and a
    credential-shape scan. Both policed a text authored by the operator or proposed by GEPA in a system
    with one human and no second principal to be injected *into*, and the blacklist's real effect was to
    refuse ordinary editorial prose — a URL in an example, a brace in a JSON illustration. What decides
    whether a proposed instruction is good here is the metric and the canary, not a substring table.

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
    """The complete write-set, and the whole of the *learnable* part of Program identity.

    Two texts, and since #306 Phase 2 each one is the whole prompt for its Predictor rather than an
    addendum to a rendered stack. #314 removed the third value: a `factory_id` literal naming the code
    around them. Code identity is computed from the code (`identity.EXECUTION_ENVELOPE_SHA256`), so
    carrying a declaration of it here only meant an artifact could claim a factory it was not running
    under. What is left is exactly what a human or an optimizer can write.
    """

    schema_version: Literal["news_program_strategy_artifact_v1"] = "news_program_strategy_artifact_v1"
    event_semantics_instruction: str
    reader_card_instruction: str
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def issue(cls, *, event_semantics_instruction: str, reader_card_instruction: str) -> ProgramStrategyArtifactV1:
        payload = {
            "schema_version": PROGRAM_SCHEMA_VERSION,
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
    """The one artifact representation: canonical JSON out, ordinary JSON in.

    Writing stays canonical because `program_sha256` is a hash of the document and a hash needs one
    serialization. Reading no longer *enforces* canonicality, rejects duplicate keys, or re-checks that a
    parse round-trips (#319): those defended against a document somebody tampered with, and in a
    single-operator system with no adversary the artifact on disk is the one this repository shipped.

    What survives is the check that carries business weight: the hash. `ProgramStrategyArtifactV1`
    recomputes it on validation, so a file whose bytes do not match its identity still fails to load —
    that is not tamper-proofing, it is the property the whole cohort model rests on.
    """

    @classmethod
    def _json_object(cls, document: str | bytes, *, kind: str) -> dict[str, Any]:
        try:
            text = document.decode("utf-8") if isinstance(document, bytes) else document
            raw = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"news_program_{kind}_json_invalid") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"news_program_{kind}_must_be_object")
        # A non-finite float has no canonical JSON form, so it would break the hash rather than attack
        # anything. Kept for that reason alone.
        _reject_nonfinite_json(raw)
        return raw

    @classmethod
    def decode(cls, document: str | bytes) -> ProgramStrategyArtifactV1:
        raw = cls._json_object(document, kind="artifact")
        if raw.get("schema_version") != PROGRAM_SCHEMA_VERSION:
            raise ValueError("news_program_artifact_version_unsupported")
        try:
            return ProgramStrategyArtifactV1.model_validate(raw)
        except ValidationError as exc:
            raise ValueError("news_program_artifact_schema_invalid") from exc

    @staticmethod
    def encode(artifact: ProgramStrategyArtifactV1) -> str:
        payload = artifact.model_dump(mode="json")
        _reject_nonfinite_json(payload)
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        return canonical_json(payload) + "\n"

    @classmethod
    def load(cls, path: str | None = None) -> ProgramStrategyArtifactV1:
        if path is None:
            return load_stable_program_artifact()
        # The path armouring went, but its error *contract* has to stay: the CLI catches
        # `(ValueError, PermissionError, RuntimeError)` and turns a coded failure into exit 2 with a named
        # error. A bare `read_text` on a candidate whose artifact root was cleaned out would escape as
        # `FileNotFoundError` and surface as a traceback instead.
        try:
            document = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("news_program_artifact_path_invalid") from exc
        return cls.decode(document)


def load_stable_program_artifact() -> ProgramStrategyArtifactV1:
    """Load and re-verify the immutable code-owned stable artifact."""

    registry = _load_program_registry()
    return load_program_artifact(str(registry["stable"]))


def _programs_resource_root() -> Any:
    """The package's own resources directory.

    #319 removed the symlink, `..` and `resolve(strict=True)` armouring that used to wrap this. It
    defended against a planted path inside the application's own installed package — an attacker who
    already had write access to the code being run.
    """

    return importlib.resources.files("tracefold.news.program").joinpath("resources")


def _load_program_registry() -> dict[str, Any]:
    registry_resource = _programs_resource_root().joinpath("registry.json")
    try:
        document = registry_resource.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("news_program_registry_path_invalid") from exc
    raw = ProgramStrategyArtifactCodec._json_object(document, kind="registry")
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
    image = _programs_resource_root().joinpath(f"{identity}.json")
    try:
        document = image.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("news_program_artifact_path_invalid") from exc
    artifact = ProgramStrategyArtifactCodec.decode(document)
    if artifact.program_sha256 != identity:
        raise ValueError("news_program_artifact_file_identity_mismatch")
    return artifact


def write_program_candidate_artifact(artifact: ProgramStrategyArtifactV1, *, artifact_root: Path) -> str:
    """Persist one already trusted/applied artifact document atomically."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    document = ProgramStrategyArtifactCodec.encode(artifact)
    destination = root / f"{artifact.program_sha256}.json"
    if destination.exists():
        # Write verification, not tamper defence, and #319's own criterion keeps it: a truncated or
        # older-encoder `<sha>.json` already in the artifact root would otherwise be reported as a
        # successful write and stamped into the candidate manifest, surfacing much later as an opaque
        # schema error against a file this run believed it had produced.
        if destination.read_text(encoding="utf-8") != document:
            raise ValueError("news_program_compile_artifact_collision")
        return str(destination)
    temporary = root / f".{artifact.program_sha256}.{uuid.uuid4().hex}.tmp"
    try:
        _write_exclusive(temporary, document)
        os.rename(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if destination.read_text(encoding="utf-8") != document:
        raise ValueError("news_program_compile_artifact_write_verification_failed")
    return str(destination)


def _write_exclusive(path: Path, document: str) -> None:
    """Create and write one file, refusing to open an existing one.

    `O_NOFOLLOW` went with #319; `O_EXCL` stays, but not for the reason an earlier version of this
    docstring gave. It claimed exclusive creation is what stops two concurrent compilers corrupting one
    artifact — that was wrong, and review caught it. Every caller passes a uuid-unique temporary that
    cannot collide, and the destination is published by `os.rename`, which overwrites silently. The
    property that actually protects the destination is the content verification in
    `write_program_candidate_artifact`, which this commit restores.

    What `O_EXCL` does here is narrower and still worth its one flag: it refuses to write into a
    temporary that somehow already exists rather than truncating it.
    """

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        encoded = document.encode("utf-8")
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
