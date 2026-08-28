"""The computed identity of everything the Program's code decides about a model call.

`program_sha256` says which two instructions are running. This module says what the code around them
does: the exact request bytes a Predictor composes, the output contract and schema the provider is held
to, the fields the model is shown, the route budget, the breaker, and — since the #314 review found the
hole — what the code decides once an answer arrives: the `reader_value` to delivery-decision map, the
`actionable` conjunction, and the restatement index rule. Together they are the whole of Program behavior
identity, and neither is a claim anybody has to remember to update.

What it does not cover, and the distinction is worth keeping sharp. Three things are covered by *siblings*
in the same bundle rather than by gaps here: operator policy (`policy_sha256`), the retrieval and
told-selection contract (`retrieval_sha256`), and the model slots (`runtime_model_bindings_sha256`). A
change to any of them still opens a new epoch, because the bundle is the compatibility unit and this hash
is one of its four parts.

One thing is a genuine residue: the *sequencing* of `graph._judge` — which route is attempted in what
order, and how the degraded path is chosen. Its decision points are rendered (`fast_retry`,
`normalize_restates`, and the assembly surface), but the order in which they are consulted is imperative
flow that this document does not reproduce. Re-ordering the primary and fallback attempts would change
which model answers without moving any hash. Naming it here rather than letting the docstring imply
otherwise: the first draft of this module claimed total coverage, review found `_assemble` outside it, and
the honest lesson is that a claim of completeness needs a list, not an adjective.

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
from .assembly import (
    READER_VALUE_DECISION,
    is_actionable,
    is_fast_retryable,
    normalize_restates,
    restatement_index_error,
)
from .contracts import TRADE_CHANNEL_ORDER
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
_STRUCTURED_OUTPUT_MODES: Final[tuple[StructuredOutputMode, ...]] = (
    "json_schema",
    "json_object",
    "prompt_json",
)


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


# Rendered as an outcome table rather than described, for the same reason the requests are: a rule stated
# in prose can drift from the rule that runs. `restatement_index_error` reads three inputs and
# `is_actionable` reads three; enumerating both is small enough to read in a diff and total enough that no
# edit to either can leave the hash still.
_NOVELTIES: Final[tuple[str, ...]] = ("new_fact", "progression", "restatement")
_TRADABILITIES: Final[tuple[str, ...]] = ("direct", "second_order", "contextual", "none")


def _assembly_surface() -> dict[str, Any]:
    """Every decision the code makes about a model's answer, enumerated over the inputs that decide it."""

    return {
        "reader_value_decision": dict(READER_VALUE_DECISION),
        "actionable": {
            f"{tradability}|channels={int(channels)}|markets={int(markets)}": is_actionable(
                tradability=tradability,  # type: ignore[arg-type]
                has_channels=channels,
                has_affected_markets=markets,
            )
            for tradability in _TRADABILITIES
            for channels in (False, True)
            for markets in (False, True)
        },
        "restatement_index": {
            f"{novelty}|restates={restates}|told={told}": restatement_index_error(
                novelty=novelty, restates=restates, told_count=told
            )
            for novelty in _NOVELTIES
            for restates in (-1, 0, 1)
            for told in (0, 1, 2)
        },
        "normalize_restates": {
            f"{novelty}|restates={restates}": normalize_restates(novelty=novelty, restates=restates)
            for novelty in _NOVELTIES
            for restates in (-1, 0, 1)
        },
        # Which model answers an Event depends on this, so it decides behavior rather than describing it.
        "fast_retry": {
            f"retryable={int(r)}|output_failure={int(o)}|truncated={int(t)}": is_fast_retryable(
                retryable=r, output_failure=o, truncated_finish=t
            )
            for r in (False, True)
            for o in (False, True)
            for t in (False, True)
        },
        # The code-owned enum the model chooses from; adding or reordering a channel changes what a
        # judgment can say, and the canonical order decides how a set is hashed for gold and replay.
        "trade_channel_order": list(TRADE_CHANNEL_ORDER),
    }


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
        "assembly": _assembly_surface(),
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
