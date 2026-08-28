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
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from .contracts import ModelVisibleCardInput, ModelVisibleSemanticsInput

_FORBIDDEN_STATE_KEY_PARTS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {
        ("api",),
        ("auth",),
        ("authorization",),
        ("base", "url"),
        ("callback",),
        ("credential",),
        ("credentials",),
        ("endpoint",),
        ("header",),
        ("headers",),
        ("history",),
        ("model", "list"),
        ("password",),
        ("secret",),
        ("token",),
    }
)

_SAFE_SECRET_FREE_IDENTITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "metric_judge_endpoint_identity_sha256",
        "reflection_endpoint_identity_sha256",
        "task_endpoint_identity_sha256",
    }
)

_HIGH_CONFIDENCE_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_nfc(value: str, *, code: str) -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        raise ValueError(code)
    return value


def _estimated_tokens(value: str) -> int:
    return (len(value.encode("utf-8")) + 3) // 4


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"news_program_duplicate_key:{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"news_program_json_nonfinite:{value}")


def _reject_nonfinite_json(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"news_program_json_nonfinite:{path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_nonfinite_json(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_nonfinite_json(child, path=f"{path}[{index}]")


def _state_key_parts(raw_key: object) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(raw_key))
    return tuple(part for part in re.split(r"[^a-z0-9]+", separated.casefold()) if part)


def _unsafe_state_key(raw_key: object) -> bool:
    parts = _state_key_parts(raw_key)
    for forbidden in _FORBIDDEN_STATE_KEY_PARTS:
        width = len(forbidden)
        if any(parts[index : index + width] == forbidden for index in range(len(parts) - width + 1)):
            return True
    return False


def _reject_unsafe_state(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if any(pattern.search(str(raw_key)) for pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS):
                raise ValueError(f"news_program_secret_value:{path}.<key>")
            if _unsafe_state_key(raw_key) and str(raw_key) not in _SAFE_SECRET_FREE_IDENTITY_KEYS:
                raise ValueError(f"news_program_unsafe_state_key:{path}.{raw_key}")
            _reject_unsafe_state(child, path=f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_unsafe_state(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and any(pattern.search(value) for pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS):
        raise ValueError(f"news_program_secret_value:{path}")


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

PROGRAM_VERSION: Final[str] = "news_semantic_program_v5"

# The route ceilings, deadline and breaker the graph executes under. They used to be copied into every
# Artifact and then hashed there, which made an operator-visible budget look like optimizer-writable state.
# They are material of `identity.compute_execution_identity` instead.
PROGRAM_EVENT_SEMANTICS_MAX_TOKENS: Final[int] = 1_200

PROGRAM_READER_CARD_MAX_TOKENS: Final[int] = 600

PROGRAM_ROUTE_DEADLINE_SECONDS: Final[int] = 20

PROGRAM_PRIMARY_BREAKER_FAILURES: Final[int] = 3

PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS: Final[int] = 60

_UNTRUSTED_EVENT_OPEN: Final[str] = "<tracefold-untrusted-event-json-v1>"

_UNTRUSTED_EVENT_CLOSE: Final[str] = "</tracefold-untrusted-event-json-v1>"

_MODEL_BINDING_SLOTS: Final[frozenset[str]] = frozenset(
    {
        "event_semantics.primary",
        "event_semantics.fallback",
        "reader_card.primary",
        "reader_card.fallback",
    }
)


PredictorName = Literal["event_semantics", "reader_card"]

ModelSlotName = Literal[
    "event_semantics.primary",
    "event_semantics.fallback",
    "reader_card.primary",
    "reader_card.fallback",
]

PROGRAM_PREDICTOR_MAX_TOKENS: Final[dict[PredictorName, int]] = {
    "event_semantics": PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
    "reader_card": PROGRAM_READER_CARD_MAX_TOKENS,
}

_VISIBLE_INPUT: Final[dict[str, type[BaseModel]]] = {
    "event_semantics": ModelVisibleSemanticsInput,
    "reader_card": ModelVisibleCardInput,
}
