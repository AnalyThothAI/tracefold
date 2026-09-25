"""Readable, side-effect-free identity for the News execution contract.

The program state has its own content hash.  This envelope describes the code
owned input and routing contract around it; deployed source and image identity
remain in the existing runtime manifest.  Constructing it never calls a model,
opens a file, scans Python source, or needs a Git checkout.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any, Final, Literal

from ..artifact_identity import canonical_sha
from ..models import FACT_KINDS
from ..taxonomy import (
    IPTC_CODEBOOK_SHA256,
    SOURCE_AUTHORITY_CLASSIFIER_VERSION,
    SOURCE_AUTHORITY_REGISTRY_SHA256,
)
from .runtime import (
    _MODEL_BINDING_SLOTS,
    _UNTRUSTED_EVENT_CLOSE,
    _UNTRUSTED_EVENT_OPEN,
    _VISIBLE_INPUT,
    PROGRAM_CONTEXT_UPPER_TOKENS,
    PROGRAM_JUDGMENT_MAX_CALLS,
    PROGRAM_PREDICTOR_MAX_CALLS,
    PROGRAM_PREDICTOR_MAX_TOKENS,
    PROGRAM_PRIMARY_BREAKER_FAILURES,
    PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
    PROGRAM_RETRYABLE_LM_ERROR_TYPES,
    PROGRAM_ROUTE_DEADLINE_SECONDS,
    PROGRAM_ROUTE_MAX_CALLS,
)
from .seed import seed_instruction
from .signatures import EventSemanticsSignature, EventTaxonomySignature, ReaderCardSignature

EXECUTION_IDENTITY_SCHEMA: Final[str] = "tracefold.news.program.execution_envelope.v9"

_SIGNATURES: Final[dict[Literal["event_semantics", "taxonomy", "reader_card"], Any]] = {
    "event_semantics": EventSemanticsSignature,
    "taxonomy": EventTaxonomySignature,
    "reader_card": ReaderCardSignature,
}


def execution_envelope() -> dict[str, Any]:
    """Return stable material that identifies the current execution semantics."""

    signatures = {
        name: signature.with_instructions(seed_instruction(name)).dump_state()
        for name, signature in _SIGNATURES.items()
    }
    return {
        "identity_schema": EXECUTION_IDENTITY_SCHEMA,
        "framework": {
            "dspy": importlib.metadata.version("dspy"),
            "litellm": importlib.metadata.version("litellm"),
            "gepa": importlib.metadata.version("gepa"),
            "request_contract": "dspy.lm15.Request/Response",
            "adapter": "dspy.JSONAdapter(use_native_function_calling=False)",
        },
        "seed_signatures_sha256": canonical_sha(signatures),
        "seed_signatures": signatures,
        "model_visible_input": {
            name: {
                "open": _UNTRUSTED_EVENT_OPEN,
                "close": _UNTRUSTED_EVENT_CLOSE,
                "schema": _VISIBLE_INPUT[name].model_json_schema(),
            }
            for name in _SIGNATURES
        },
        "domain_vocabulary": {
            "fact_kinds": list(FACT_KINDS),
            "iptc_codebook_sha256": IPTC_CODEBOOK_SHA256,
            "source_authority_classifier_version": SOURCE_AUTHORITY_CLASSIFIER_VERSION,
            "source_authority_registry_sha256": SOURCE_AUTHORITY_REGISTRY_SHA256,
        },
        "route": {
            "model_binding_slots": sorted(_MODEL_BINDING_SLOTS),
            "order": ["primary", "fallback"],
            "predictor_order": ["event_semantics", "taxonomy", "reader_card"],
            "taxonomy_known_failure": "unavailable_then_continue",
            "other_stage_failure": "full_fallback_restart",
            "deadline_seconds": PROGRAM_ROUTE_DEADLINE_SECONDS,
            "primary_breaker": {
                "failures": PROGRAM_PRIMARY_BREAKER_FAILURES,
                "open_seconds": PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
                "retryable_lm_error_types": list(PROGRAM_RETRYABLE_LM_ERROR_TYPES),
            },
            "call_limits": {
                "predictor": PROGRAM_PREDICTOR_MAX_CALLS,
                "route": PROGRAM_ROUTE_MAX_CALLS,
                "judgment": PROGRAM_JUDGMENT_MAX_CALLS,
            },
            "predictor_max_tokens": dict(PROGRAM_PREDICTOR_MAX_TOKENS),
            "context_upper_tokens": PROGRAM_CONTEXT_UPPER_TOKENS,
        },
    }


def compute_execution_identity() -> str:
    return canonical_sha(execution_envelope())


EXECUTION_ENVELOPE_SHA256: Final[str] = compute_execution_identity()

__all__ = [
    "EXECUTION_ENVELOPE_SHA256",
    "EXECUTION_IDENTITY_SCHEMA",
    "compute_execution_identity",
    "execution_envelope",
]
