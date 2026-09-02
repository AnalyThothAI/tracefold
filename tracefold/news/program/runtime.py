"""Runtime primitives and the code-owned execution contract of `tracefold.news.program`.

Exact-by-default Pydantic configuration, the JSON-state safety rules that keep a loadable artifact free of
duplicate keys, non-finite numbers, JSON constants and credential-shaped state, and the numbers the graph
runs on: model route ceilings, the route deadline and the primary breaker.

Those numbers are code, not artifact state, and nobody declares a version for them: `identity.py` renders
what they compose and hashes the render, so changing the graph, the schemas, the rules, the normalizer,
the route or the execution budget moves the Program's code identity by construction. Nothing here knows
what a Program is; it is the floor every other `tracefold.news.program` module stands on.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from .contracts import ModelVisibleCardInput, ModelVisibleSemanticsInput, ModelVisibleTaxonomyInput


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_nfc(value: str, *, code: str) -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        raise ValueError(code)
    return value


def _estimated_tokens(value: str) -> int:
    return (len(value.encode("utf-8")) + 3) // 4


def _reject_nonfinite_json(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"news_program_json_nonfinite:{path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_nonfinite_json(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_nonfinite_json(child, path=f"{path}[{index}]")


def _safe_json_state(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _safe_json_state(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _safe_json_state(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_state(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"news_program_compiled_state_type_invalid:{type(value).__name__}")


# The one budget a Predictor instruction has, applied to the whole text. #306 Phase 2 retired the separate
# 8 KiB advisory ceiling with the layering it bounded: there is no longer an outer instruction and an inner
# addendum to bound differently, and a human editing `seed.py` is held to exactly what a GEPA proposal is.
PROGRAM_INSTRUCTION_MAX_BYTES: Final[int] = 32_768

PROGRAM_INSTRUCTION_MAX_ESTIMATED_TOKENS: Final[int] = 8_192

PROGRAM_SCHEMA_VERSION: Final[str] = "news_program_strategy_artifact_v1"

# v9 (#501): taxonomy is its own Predictor between EventSemantics and ReaderCard, so a v8 recording of two
# calls per route is not a replay of this executor and the three-instruction artifact is a new write-set.
# #344 (v8) made the same kind of cut when requests started being rendered by DSPy.
PROGRAM_VERSION: Final[str] = "news_semantic_program_v9"

# The route ceilings, deadline and breaker the graph executes under. They used to be copied into every
# Artifact and then hashed there, which made an operator-visible budget look like optimizer-writable state.
# They are material of `identity.compute_execution_identity` instead.
PROGRAM_EVENT_SEMANTICS_MAX_TOKENS: Final[int] = 1_200

# Four enum axes and at most three qcodes: ~80 output tokens under grammar-constrained JSON, so 400 is
# headroom, not a budget a candidate can spend on prose.
PROGRAM_TAXONOMY_MAX_TOKENS: Final[int] = 400

PROGRAM_READER_CARD_MAX_TOKENS: Final[int] = 600

PROGRAM_ROUTE_DEADLINE_SECONDS: Final[int] = 20

PROGRAM_PRIMARY_BREAKER_FAILURES: Final[int] = 3

PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS: Final[int] = 60

# A JSON-schema-capable Predictor may make its initial call plus one stock
# JSONAdapter format fallback.  There are three serial Predictors per route and
# two complete routes in the primary -> fallback chain.
PROGRAM_PREDICTOR_MAX_CALLS: Final[int] = 2

PROGRAM_ROUTE_MAX_CALLS: Final[int] = 6

PROGRAM_JUDGMENT_MAX_CALLS: Final[int] = 12

# These exact public DSPy failures may open the primary breaker.  Routing turns
# the names into classes; execution identity addresses the same tuple so a
# policy change cannot silently retain the old release identity.
PROGRAM_RETRYABLE_LM_ERROR_TYPES: Final[tuple[str, ...]] = (
    "LMRateLimitError",
    "LMServerError",
    "LMTimeoutError",
    "LMTransportError",
)

_UNTRUSTED_EVENT_OPEN: Final[str] = "<tracefold-untrusted-event-json-v1>"

_UNTRUSTED_EVENT_CLOSE: Final[str] = "</tracefold-untrusted-event-json-v1>"

_MODEL_BINDING_SLOTS: Final[frozenset[str]] = frozenset(
    {
        "event_semantics.primary",
        "event_semantics.fallback",
        "taxonomy.primary",
        "taxonomy.fallback",
        "reader_card.primary",
        "reader_card.fallback",
    }
)


PredictorName = Literal["event_semantics", "taxonomy", "reader_card"]

# Execution order. Sequential on purpose (#501 D1): the production slot is one llama.cpp server where
# concurrency saves no wall clock, and recording call indices are assigned in append order, so a parallel
# taxonomy call would make record/replay nondeterministic.
PREDICTOR_NAMES: Final[tuple[PredictorName, ...]] = ("event_semantics", "taxonomy", "reader_card")

ModelSlotName = Literal[
    "event_semantics.primary",
    "event_semantics.fallback",
    "taxonomy.primary",
    "taxonomy.fallback",
    "reader_card.primary",
    "reader_card.fallback",
]

PROGRAM_PREDICTOR_MAX_TOKENS: Final[dict[PredictorName, int]] = {
    "event_semantics": PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
    "taxonomy": PROGRAM_TAXONOMY_MAX_TOKENS,
    "reader_card": PROGRAM_READER_CARD_MAX_TOKENS,
}

_VISIBLE_INPUT: Final[dict[str, type[BaseModel]]] = {
    "event_semantics": ModelVisibleSemanticsInput,
    "taxonomy": ModelVisibleTaxonomyInput,
    "reader_card": ModelVisibleCardInput,
}
