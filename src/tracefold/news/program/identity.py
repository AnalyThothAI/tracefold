"""The computed identity of everything the Program's code decides about a model call.

`program_sha256` says which two instructions are running. This module says what the code around them
does: the exact request bytes a Predictor composes, the output contract and schema the provider is held
to, the fields the model is shown, the route budget, and the breaker. Together they are the whole of
Program behavior identity, and neither is a claim anybody has to remember to update.

It replaced a declared `factory_id` literal (#314). A hand-written version has one failure mode, and it
is not that somebody picks the wrong string — it is that a change lands and nobody bumps anything. Three
identity-clearing incidents in four days were exactly that, and the pin net that grew to catch them
(nine epoch counts, four byte-equality tests, a mirrored constant module, five documents) was guarding
the declaration rather than the behavior. `compute_execution_identity()` renders the behavior and hashes
what it rendered, so "the deployment changed what the model sees but the identity did not move" is not a
mistake that can be made.

One contract test pins the value. Editing any material below turns it red, and the re-pinned line in
that test is the signature on the identity migration.
"""

from __future__ import annotations

from typing import Any, Final

from ..artifact_identity import canonical_sha
from .runtime import (
    _MODEL_BINDING_SLOTS,
    _UNTRUSTED_EVENT_CLOSE,
    _UNTRUSTED_EVENT_OPEN,
    _VISIBLE_INPUT,
    PROGRAM_PREDICTOR_MAX_TOKENS,
    PROGRAM_PRIMARY_BREAKER_FAILURES,
    PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
    PROGRAM_ROUTE_DEADLINE_SECONDS,
    PredictorName,
)
from .signatures import PREDICTOR_INPUT_FIELDS, PREDICTOR_OUTPUT
from .transport import (
    _JSON_OBJECT_ONLY_MODEL_PREFIXES,
    _RETRYABLE_MARKERS,
    _RETRYABLE_STATUS,
    _TRUNCATED_FINISH_REASONS,
    _WIRE_MODEL_PREFIX,
    StructuredOutputMode,
    chat_request_body,
)

EXECUTION_IDENTITY_SCHEMA: Final[str] = "tracefold.news.program.execution_envelope.v1"

# Rendered, not described. The instruction and the field values are sentinels because they are the two
# things this identity is deliberately blind to: an instruction edit moves `program_sha256`, and an
# Event's bytes are the question, not the envelope. Everything else in the request is real — the model
# name goes through `wire_model_name`, the contract through `system_message`, the schema through
# `response_format` — so a change to any of them moves the hash without anyone deciding that it should.
_GOLDEN_MODEL: Final[str] = f"{_WIRE_MODEL_PREFIX}tracefold-execution-identity"
_GOLDEN_INSTRUCTION: Final[str] = "<golden-instruction>"
_STRUCTURED_OUTPUT_MODES: Final[tuple[StructuredOutputMode, ...]] = ("json_schema", "json_object")


def _golden_request(predictor: PredictorName, mode: StructuredOutputMode) -> dict[str, Any]:
    """One Predictor's complete wire envelope, in one structured-output mode, with sentinel content."""

    output_field, output_model = PREDICTOR_OUTPUT[predictor]
    fields = PREDICTOR_INPUT_FIELDS[predictor]
    return chat_request_body(
        model=_GOLDEN_MODEL,
        instruction=_GOLDEN_INSTRUCTION,
        field_order=fields,
        values={field: f"<{field}>" for field in fields},
        output_field=output_field,
        output_model=output_model,
        max_tokens=PROGRAM_PREDICTOR_MAX_TOKENS[predictor],
        structured_output=mode,
    )


def execution_envelope() -> dict[str, Any]:
    """The complete material `compute_execution_identity` hashes, as a readable document.

    Public so a re-pin is reviewable: when the contract test goes red the diff of this document says what
    moved, which is the difference between signing an identity migration and pasting a hash.
    """

    return {
        "identity_schema": EXECUTION_IDENTITY_SCHEMA,
        "requests": {
            predictor: {mode: _golden_request(predictor, mode) for mode in _STRUCTURED_OUTPUT_MODES}
            for predictor in PREDICTOR_INPUT_FIELDS
        },
        "model_visible_input": {
            predictor: {
                "open": _UNTRUSTED_EVENT_OPEN,
                "close": _UNTRUSTED_EVENT_CLOSE,
                "schema": _VISIBLE_INPUT[predictor].model_json_schema(),
            }
            for predictor in PREDICTOR_INPUT_FIELDS
        },
        "route": {
            "model_binding_slots": sorted(_MODEL_BINDING_SLOTS),
            "wire_model_prefix": _WIRE_MODEL_PREFIX,
            "json_object_only_model_prefixes": list(_JSON_OBJECT_ONLY_MODEL_PREFIXES),
            "deadline_seconds": PROGRAM_ROUTE_DEADLINE_SECONDS,
            "primary_breaker_failures": PROGRAM_PRIMARY_BREAKER_FAILURES,
            "primary_breaker_open_seconds": PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
            "truncated_finish_reasons": sorted(_TRUNCATED_FINISH_REASONS),
            "retryable_status": sorted(_RETRYABLE_STATUS),
            "retryable_markers": list(_RETRYABLE_MARKERS),
        },
    }


def compute_execution_identity() -> str:
    """The one intent gate over code-owned Program behavior."""

    return canonical_sha(execution_envelope())


EXECUTION_ENVELOPE_SHA256: Final[str] = compute_execution_identity()

__all__ = [
    "EXECUTION_ENVELOPE_SHA256",
    "EXECUTION_IDENTITY_SCHEMA",
    "compute_execution_identity",
    "execution_envelope",
]
