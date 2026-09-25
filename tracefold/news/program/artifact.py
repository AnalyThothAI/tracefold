"""The content-addressed `NewsProgramStateV1`, its one loader and its registry.

A Program image is now exactly what DSPy itself calls the state of a `dspy.Module`: the document
`NativeNewsProgram.dump_state()` returns, keyed by `named_predictors()` name, carrying each Predictor's
instruction, its demos and its Signature state. `program_sha256` is the canonical hash of that document
without the `lm` entries, plus the schema and the pinned DSPy version — which is why two compiles that
arrive at the same instructions *and the same demos* are the same running Program however much they cost,
whoever launched them, and whatever trajectory they took.

Before #651 the image was three instruction strings and nothing else. That shape could not represent a
GEPA candidate at all: the optimizer is allowed to attach few-shot demos to a Predictor, and an image with
no place to put them meant every such candidate had to be refused before it could be evaluated. The native
state document has a place for them, so the release pipeline now carries what the optimizer actually
produces instead of a projection of it.

Two things the envelope refuses on the way in, and both are business rules rather than tamper defence:

- an `lm` entry that is not null. Model routes are operator-owned configuration resolved by
  `tracefold.app.learning_runtime`; a route baked into a released image would be a second, stale answer to
  "which endpoint does this Predictor call".
- a demo whose fields are not the Signature's, an unknown or missing Predictor name, or an instruction
  outside `validate_program_instruction`'s bounds. `Signature.load_state` zips saved fields against code
  fields positionally and ignores the tail, so a document the loader did not check would load silently
  wrong.

Loading is `NativeNewsProgram(state)`: construct the three Predictors from the code-owned seed defaults,
`load_state` the envelope over them, then re-read `named_predictors()` and refuse anything the round trip
did not reproduce. There is no second loader and no second representation.

Everything else the Program needs — the native Module, schemas, normalizer, assembler, model route and the
execution budget — is code, and `identity.compute_execution_identity` hashes what that code renders (#314).

`module.py` executes a state; this module decides what a legal state *is*.
"""

from __future__ import annotations

import copy
import importlib.resources
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import Field, ValidationError, field_validator, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from ..evidence import EVIDENCE_INPUT_VERSION
from .runtime import (
    _MODEL_BINDING_SLOTS,
    _UNTRUSTED_EVENT_CLOSE,
    _UNTRUSTED_EVENT_OPEN,
    _VISIBLE_INPUT,
    PREDICTOR_NAMES,
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
from .signatures import EventSemanticsSignature, EventTaxonomySignature, ReaderCardSignature

# The exact DSPy whose `dump_state` shape this envelope is. Pinned rather than read at validation time: a
# state written by another version is a different document, and discovering that at load is the point.
DSPY_STATE_VERSION: Final[Literal["3.4.0"]] = "3.4.0"

# Exactly the keys `dspy.Predict.dump_state()` emits in 3.4.0.
_PREDICTOR_DOCUMENT_KEYS: Final[frozenset[str]] = frozenset({"traces", "train", "demos", "signature", "lm"})

_SIGNATURE_DOCUMENT_KEYS: Final[frozenset[str]] = frozenset({"instructions", "fields"})

_SIGNATURE_FIELD_KEYS: Final[frozenset[str]] = frozenset({"prefix", "description"})

PREDICTOR_SIGNATURES: Final[dict[PredictorName, Any]] = {
    "event_semantics": EventSemanticsSignature,
    "taxonomy": EventTaxonomySignature,
    "reader_card": ReaderCardSignature,
}


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

    name: PredictorName
    instruction: str = Field(min_length=1, max_length=PROGRAM_INSTRUCTION_MAX_BYTES)
    model_bindings: PredictorModelBindings
    max_tokens: int = Field(ge=64, le=4096)

    @model_validator(mode="after")
    def _instruction_is_bounded(self) -> PredictorState:
        if len(self.instruction.encode("utf-8")) > PROGRAM_INSTRUCTION_MAX_BYTES:
            raise ValueError(f"news_program_{self.name}_instruction_too_large")
        return self


def predictor_document(
    predictor: PredictorName,
    *,
    instruction: str,
    demos: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Render one Predictor's native state document through DSPy's own `dump_state`.

    Never hand-assembled: the document has to be what `dspy.Predict.dump_state()` emits, or the loader's
    round trip is comparing this repository's guess against DSPy's reality.
    """

    predict = dspy.Predict(
        PREDICTOR_SIGNATURES[predictor].with_instructions(validate_program_instruction(instruction)),
        max_tokens=PROGRAM_PREDICTOR_MAX_TOKENS[predictor],
    )
    predict.demos = [dict(demo) for demo in demos]
    return dict(predict.dump_state())


class NewsProgramStateV1(_ExactModel):
    """The complete write-set, and the whole of the *learnable* part of Program identity.

    One native DSPy state document plus the three declarations a reader of the file needs to know what it
    is: the schema, the DSPy version whose `dump_state` shape it carries, and the Predictor names in
    execution order. Everything an optimizer can write — instructions and demos — is inside `state`;
    everything code owns is outside it and hashed by `identity.EXECUTION_ENVELOPE_SHA256`.
    """

    schema_version: Literal["news_program_state_v1"] = "news_program_state_v1"
    evidence_input_version: Literal["news_evidence_input_v2"]
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dspy_version: Literal["3.4.0"] = DSPY_STATE_VERSION
    predictors: tuple[PredictorName, ...]
    state: dict[str, Any]

    @classmethod
    def issue(cls, *, state: Mapping[str, Any]) -> NewsProgramStateV1:
        payload = {
            "schema_version": PROGRAM_SCHEMA_VERSION,
            "evidence_input_version": EVIDENCE_INPUT_VERSION,
            "dspy_version": DSPY_STATE_VERSION,
            "predictors": list(PREDICTOR_NAMES),
            "state": copy.deepcopy(dict(state)),
        }
        return cls(**payload, program_sha256=canonical_sha(_identity_material(payload)))

    @classmethod
    def from_instructions(cls, instructions: Mapping[PredictorName, str]) -> NewsProgramStateV1:
        """Build a demo-free state from one complete instruction per Predictor."""

        return cls.issue(
            state={
                predictor: predictor_document(predictor, instruction=instructions[predictor])
                for predictor in PREDICTOR_NAMES
            }
        )

    @model_validator(mode="after")
    def _state_is_loadable_and_identity_is_exact(self) -> NewsProgramStateV1:
        if tuple(self.predictors) != PREDICTOR_NAMES:
            raise ValueError("news_program_state_predictors_invalid")
        if set(self.state) != set(PREDICTOR_NAMES):
            raise ValueError("news_program_state_predictor_set_invalid")
        for predictor in PREDICTOR_NAMES:
            _validate_predictor_document(predictor, self.state[predictor])
        if self.program_sha256 != self.computed_sha256():
            raise ValueError("news_program_state_hash_mismatch")
        return self

    def computed_sha256(self) -> str:
        return canonical_sha(_identity_material(self.model_dump(mode="json", exclude={"program_sha256"})))

    def predictor_document(self, predictor: PredictorName) -> dict[str, Any]:
        return copy.deepcopy(dict(self.state[predictor]))

    def predictor_documents(self) -> dict[str, Any]:
        """The native document `dspy.Module.load_state` consumes, copied so a loader cannot mutate it."""

        return {predictor: self.predictor_document(predictor) for predictor in PREDICTOR_NAMES}

    def instruction_for(self, predictor: PredictorName) -> str:
        return str(self.state[predictor]["signature"]["instructions"])

    def demos_for(self, predictor: PredictorName) -> tuple[dict[str, Any], ...]:
        return tuple(dict(demo) for demo in self.state[predictor]["demos"])

    def predictor_state(self, predictor: PredictorName) -> PredictorState:
        return build_predictor_state(predictor, self.instruction_for(predictor))

    def with_predictor_document(
        self,
        predictor: PredictorName,
        document: Mapping[str, Any],
    ) -> NewsProgramStateV1:
        """Replace exactly one Predictor's native state, keeping the other two byte-identical.

        The one merge the optimizer performs: a GEPA run optimizes a single Predictor, and what it returns
        is that Predictor's `dump_state()`. Merging it here rather than in the optimizer keeps "which bytes
        moved" a property of the document instead of a claim in a receipt.
        """

        merged: dict[str, Any] = {name: self.predictor_document(name) for name in PREDICTOR_NAMES}
        merged[predictor] = dict(document)
        return NewsProgramStateV1.issue(state=merged)

    def changed_predictors(self, parent: NewsProgramStateV1) -> tuple[PredictorName, ...]:
        """Exactly which Predictors this state rewrites relative to `parent`, in Program order."""

        return tuple(
            predictor
            for predictor in PREDICTOR_NAMES
            if self.predictor_document(predictor) != parent.predictor_document(predictor)
        )

    @property
    def event_semantics(self) -> PredictorState:
        return self.predictor_state("event_semantics")

    @property
    def taxonomy(self) -> PredictorState:
        return self.predictor_state("taxonomy")

    @property
    def reader_card(self) -> PredictorState:
        return self.predictor_state("reader_card")


def _identity_material(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The hashed projection: everything but the `lm` routes, which are operator configuration."""

    state = dict(payload["state"])
    return {
        "schema_version": payload["schema_version"],
        "evidence_input_version": payload["evidence_input_version"],
        "dspy_version": payload["dspy_version"],
        "predictors": list(payload["predictors"]),
        "state": {
            name: {key: value for key, value in dict(document).items() if key != "lm"}
            for name, document in state.items()
        },
    }


def _validate_predictor_document(predictor: PredictorName, document: Any) -> None:
    if not isinstance(document, Mapping) or set(document) != _PREDICTOR_DOCUMENT_KEYS:
        raise ValueError(f"news_program_state_predictor_document_invalid:{predictor}")
    _reject_nonfinite_json(dict(document), path=f"state.{predictor}")
    if document["lm"] is not None:
        raise ValueError(f"news_program_state_lm_route_forbidden:{predictor}")
    # `traces` and `train` are per-call execution scratch that `Predict.reset()` empties. A released image
    # carrying either would make two identical Programs hash differently for a reason nobody authored.
    for key in ("traces", "train"):
        if document[key] != []:
            raise ValueError(f"news_program_state_scratch_not_empty:{predictor}.{key}")
    signature = document["signature"]
    if not isinstance(signature, Mapping) or set(signature) != _SIGNATURE_DOCUMENT_KEYS:
        raise ValueError(f"news_program_state_signature_invalid:{predictor}")
    validate_program_instruction(str(signature["instructions"]))
    code_fields = list(PREDICTOR_SIGNATURES[predictor].fields)
    fields = signature["fields"]
    # `Signature.load_state` zips saved fields against code fields positionally with `strict=False`, so a
    # document with a different field count loads a partially-applied Signature and says nothing.
    if not isinstance(fields, list) or len(fields) != len(code_fields):
        raise ValueError(f"news_program_state_signature_fields_invalid:{predictor}")
    for field in fields:
        if not isinstance(field, Mapping) or set(field) != _SIGNATURE_FIELD_KEYS:
            raise ValueError(f"news_program_state_signature_fields_invalid:{predictor}")
    demos = document["demos"]
    if not isinstance(demos, list):
        raise ValueError(f"news_program_state_demos_invalid:{predictor}")
    allowed = set(code_fields)
    for demo in demos:
        if not isinstance(demo, Mapping) or not set(demo) <= allowed:
            raise ValueError(f"news_program_state_demo_fields_invalid:{predictor}")


def build_predictor_state(predictor: PredictorName, instruction: str) -> PredictorState:
    """Bind one Predictor's instruction to its route and budget.

    There is no rendering step left. What the state carries is what the provider is sent, which is why
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


def build_code_owned_program_state() -> NewsProgramStateV1:
    """Build the reviewed baseline root from the seed texts, with no demos; callers decide where it goes."""

    return NewsProgramStateV1.from_instructions(
        {predictor: seed_instruction(predictor) for predictor in PREDICTOR_NAMES}
    )


def _json_object(document: str | bytes, *, kind: str) -> dict[str, Any]:
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


def decode_program_state(document: str | bytes) -> NewsProgramStateV1:
    """The one reader: ordinary JSON in, a re-verified state envelope out.

    Reading does not *enforce* canonicality, reject duplicate keys, or re-check that a parse round-trips
    (#319): those defended against a document somebody tampered with, and in a single-operator system with
    no adversary the image on disk is the one this repository shipped. What survives is the check that
    carries business weight — the hash — plus the loadability rules `NewsProgramStateV1` applies, because a
    document DSPy would load wrong is not a Program.
    """

    raw = _json_object(document, kind="state")
    if raw.get("schema_version") != PROGRAM_SCHEMA_VERSION:
        raise ValueError("news_program_state_version_unsupported")
    if raw.get("dspy_version") != DSPY_STATE_VERSION:
        raise ValueError("news_program_state_dspy_version_unsupported")
    try:
        return NewsProgramStateV1.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("news_program_state_schema_invalid") from exc


def encode_program_state(state: NewsProgramStateV1) -> str:
    """Canonical JSON out. Writing stays canonical because `program_sha256` hashes this document."""

    payload = state.model_dump(mode="json")
    _reject_nonfinite_json(payload)
    if state.program_sha256 != state.computed_sha256():
        raise ValueError("news_program_state_hash_mismatch")
    return canonical_json(payload) + "\n"


def read_program_state_document(path: str | None = None) -> NewsProgramStateV1:
    """Decode one state document from an operator path, or the packaged stable image when none is given."""

    if path is None:
        return load_stable_program_state()
    # The path armouring went, but its error *contract* has to stay: the CLI catches
    # `(ValueError, PermissionError, RuntimeError)` and turns a coded failure into exit 2 with a named
    # error. A bare `read_text` on a candidate whose artifact root was cleaned out would escape as
    # `FileNotFoundError` and surface as a traceback instead.
    try:
        document = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("news_program_state_path_invalid") from exc
    return decode_program_state(document)


def load_stable_program_state() -> NewsProgramStateV1:
    """Load and re-verify the immutable code-owned stable state."""

    registry = _load_program_registry()
    return load_program_state(str(registry["stable"]))


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
    raw = _json_object(document, kind="registry")
    if set(raw) != {"stable", "images"} or not isinstance(raw["images"], list):
        raise ValueError("news_program_registry_schema_invalid")
    images = [str(value) for value in raw["images"]]
    if str(raw["stable"]) not in images or len(images) != len(set(images)):
        raise ValueError("news_program_registry_identity_invalid")
    if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in images):
        raise ValueError("news_program_registry_sha_invalid")
    return {"stable": str(raw["stable"]), "images": tuple(images)}


def load_program_state(program_sha256: str) -> NewsProgramStateV1:
    """Resolve one immutable image from the code-owned registry, never from a user path."""

    identity = str(program_sha256)
    registry = _load_program_registry()
    if identity not in registry["images"]:
        raise ValueError("news_program_state_not_registered")
    image = _programs_resource_root().joinpath(f"{identity}.json")
    try:
        document = image.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("news_program_state_path_invalid") from exc
    state = decode_program_state(document)
    if state.program_sha256 != identity:
        raise ValueError("news_program_state_file_identity_mismatch")
    return state


def write_program_candidate_state(state: NewsProgramStateV1, *, artifact_root: Path) -> str:
    """Persist one already trusted state document atomically."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    document = encode_program_state(state)
    destination = root / f"{state.program_sha256}.json"
    if destination.exists():
        # Write verification, not tamper defence, and #319's own criterion keeps it: a truncated or
        # older-encoder `<sha>.json` already in the artifact root would otherwise be reported as a
        # successful write and stamped into the candidate manifest, surfacing much later as an opaque
        # schema error against a file this run believed it had produced.
        if destination.read_text(encoding="utf-8") != document:
            raise ValueError("news_program_compile_artifact_collision")
        return str(destination)
    temporary = root / f".{state.program_sha256}.{uuid.uuid4().hex}.tmp"
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
    `write_program_candidate_state`, which this commit restores.

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


__all__ = [
    "DSPY_STATE_VERSION",
    "PREDICTOR_SIGNATURES",
    "NewsProgramStateV1",
    "PredictorModelBindings",
    "PredictorState",
    "build_code_owned_program_state",
    "build_predictor_state",
    "decode_program_state",
    "encode_program_state",
    "load_program_state",
    "load_stable_program_state",
    "predictor_document",
    "read_program_state_document",
    "render_model_evidence_json",
    "validate_program_instruction",
    "write_program_candidate_state",
]
