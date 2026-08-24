"""Runtime primitives and frozen identity constants shared across `tracefold.news.program`.

Exact-by-default Pydantic configuration, and the JSON-state safety rules that keep a loadable artifact
free of duplicate keys, non-finite numbers, JSON constants and credential-shaped state. Nothing here
knows what a Program is; it is the floor every other `tracefold.news.program` module stands on.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from ..artifact_identity import canonical_sha
from ..told_context import NEWS_RETRIEVAL_SHA256
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


PROGRAM_DEMOS_MAX: Final[int] = 32

PROGRAM_DEMOS_MAX_ESTIMATED_TOKENS: Final[int] = 32_768

PROGRAM_DEMO_JSON_MAX_BYTES: Final[int] = 32_768

PROGRAM_INSTRUCTION_MAX_BYTES: Final[int] = 32_768

PROGRAM_RULE_PACK_MAX: Final[int] = 9

PROGRAM_RULE_PACK_BODY_MAX_BYTES: Final[int] = 16_384

PROGRAM_LEARNED_STRATEGY_MAX_BYTES: Final[int] = 8_192

PROGRAM_LEARNED_STRATEGY_MAX_ESTIMATED_TOKENS: Final[int] = 2_048

PROGRAM_DEMO_BANK_MAX: Final[int] = 64

PROGRAM_DEMO_BANK_MAX_BYTES: Final[int] = 262_144

PROGRAM_DEPENDENCY_LOCK_SHA256: Final[str] = "defdd610578ecd1f1f667f5eaf0ebf0b94ae866b16fd5cdd41ba3fc793ab4b37"

PROGRAM_SCHEMA_VERSION: Final[str] = "news_semantic_program_artifact_v2"

PROGRAM_FACTORY_ID: Final[str] = "tracefold.news.program.factory_v5"

PROGRAM_VERSION: Final[str] = "news_semantic_program_v5"

PROGRAM_LEARNING_EPOCH: Final[str] = "program_v7"

PROGRAM_TOPOLOGY_SHA256: Final[str] = canonical_sha(
    {
        "nodes": ["event_semantics", "semantic_normalizer", "reader_card", "verdict_assembler"],
        "edges": [[0, 1], [1, 2], [2, 3]],
    }
)

PROGRAM_ADAPTER_SHA256: Final[str] = canonical_sha(
    {
        "adapter": "predictor_adapter_v3",
        "cache": False,
        "history": False,
        "hidden_retry": False,
        "metadata": "exact_provider_response",
        "request_identity": "runtime_model_binding",
    }
)

PROGRAM_ASSEMBLER_SHA256: Final[str] = canonical_sha(
    {
        "assembler": "verdict_editorial_assembler_v3",
        "semantic_normalizer": "semantic_normalizer_v2",
        "model_intent": "trade_relevance.reader_value",
        "decision_projection": {
            "escalate": "escalate",
            "realtime": "push",
            "background": "drop",
            "none": "drop",
        },
        "actionable_projection": "direct_or_second_order_with_nonempty_channels_and_markets",
        "reader_card_semantics": "ReaderCardSemanticView",
        "non_restatement_index": "normalize_to_minus_one",
        "restatement_index": "strict",
        "title_sentinel": "always_empty",
    }
)

PROGRAM_INPUT_CONTRACT_SHA256: Final[str] = canonical_sha(
    {
        "context": "tracefold.news.TriageContext.v4",
        # EventSemantics sees the selected history; ReaderCard sees only the Event.
        "event_semantics_payload": "bounded_with_selected_told_context.v2",
        "reader_card_payload": "bounded_evidence_only.v2",
        "reader_card_semantic_view": "ReaderCardSemanticView.v1",
        "news_retrieval": NEWS_RETRIEVAL_SHA256,
        "untrusted_delimiter": "tracefold-untrusted-event-json-v1",
    }
)

PROGRAM_RENDERER_SHA256: Final[str] = canonical_sha(
    {
        "renderer": "d_generation_instruction_renderer_v2",
        "order": [
            "quality_kernel",
            "rule_packs",
            "learned_strategy",
            "canonical_demos",
            "final_authority_seal",
            "untrusted_input",
        ],
        "unicode": "NFC",
    }
)

PROGRAM_CONTEXT_RENDERER_SHA256: Final[str] = canonical_sha(
    {
        "renderer": "triage_context_per_predictor_payload_v2",
        "event_semantics": "TriageContext.event_semantics_payload",
        "reader_card": "TriageContext.reader_card_payload",
        "canonical_json": True,
        "audit_and_queue_hints": "excluded",
        "untrusted_delimiter": "tracefold-untrusted-event-json-v1",
    }
)

PROGRAM_UNTRUSTED_DELIMITER_SHA256: Final[str] = canonical_sha(
    {"open": "<tracefold-untrusted-event-json-v1>", "close": "</tracefold-untrusted-event-json-v1>"}
)

PROGRAM_SEMANTIC_VALIDATOR_SHA256: Final[str] = canonical_sha(
    {
        "validator": "event_semantics_context_v2",
        "restatement_index": "visible_told_only",
        "trade_relevance": "typed_and_consistent",
    }
)

PROGRAM_NORMALIZER_SHA256: Final[str] = canonical_sha(
    {
        "normalizer": "semantic_normalizer_v2",
        "non_restatement_restates": -1,
        "trade_code_sets": "deduplicate_then_code_owned_order",
    }
)

_UNTRUSTED_EVENT_OPEN: Final[str] = "<tracefold-untrusted-event-json-v1>"

_UNTRUSTED_EVENT_CLOSE: Final[str] = "</tracefold-untrusted-event-json-v1>"

_LEARNED_STRATEGY_AUTHORITY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(?:disregard|ignore|override|bypass|supersede|weaken|replace)\b.{0,96}"
        r"\b(?:earlier|previous|prior|above|requirements?|instructions?|rules?|rulepacks?|"
        r"qualitykernel|kernel|policy)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:rules?|rulepacks?|qualitykernel|kernel|requirements?|instructions?|policy)\b.{0,96}"
        r"\b(?:optional|advisory|ignore|override|bypass|supersede|weaken|replace)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:always|never)\b.{0,48}\b(?:emit|return|choose|set)\b.{0,32}"
        r"\b(?:push|drop|escalate)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)

_DEMO_FIELDS: Final[dict[str, frozenset[str]]] = {
    "event_semantics": frozenset({"evidence_json", "semantics"}),
    "reader_card": frozenset({"evidence_json", "semantics_json", "card"}),
}

_MODEL_BINDING_SLOTS: Final[frozenset[str]] = frozenset(
    {
        "event_semantics.primary",
        "event_semantics.fallback",
        "reader_card.primary",
        "reader_card.fallback",
    }
)

_FACTORY_SOURCE_RESOURCES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("news/artifact_identity.py", ("artifact_identity.py",)),
    ("news/reader_history.py", ("reader_history.py",)),
    ("news/told_context.py", ("told_context.py",)),
    ("news/program/runtime.py", ("program", "runtime.py")),
    ("news/program/dspy_adapter.py", ("program", "dspy_adapter.py")),
    ("news/program/contracts.py", ("program", "contracts.py")),
    ("news/program/signatures.py", ("program", "signatures.py")),
    ("news/program/artifact.py", ("program", "artifact.py")),
    ("news/program/quality_baseline.py", ("program", "quality_baseline.py")),
    ("news/program/graph.py", ("program", "graph.py")),
)


def _runtime_factory_source_sha256() -> str:
    """Digest every package-owned source that can change Program behavior.

    The resources are read from the installed ``tracefold.news`` package, so a
    wheel never searches upward for a repository checkout.
    """

    package_root = importlib.resources.files("tracefold.news")
    identities = {
        logical_name: hashlib.sha256(package_root.joinpath(*parts).read_bytes()).hexdigest()
        for logical_name, parts in _FACTORY_SOURCE_RESOURCES
    }
    return canonical_sha(identities)


def _runtime_dependency_lock_sha256() -> str:
    """Return the lock identity carried by every installed package.

    A wheel has no repository root or ``uv.lock``.  The generated constant is
    therefore part of the trusted application package, while a drift test and
    the artifact maintenance tool require it to match the source lock exactly.
    """

    return PROGRAM_DEPENDENCY_LOCK_SHA256


PredictorName = Literal["event_semantics", "reader_card"]

ModelSlotName = Literal[
    "event_semantics.primary",
    "event_semantics.fallback",
    "reader_card.primary",
    "reader_card.fallback",
]

_VISIBLE_INPUT: Final[dict[str, type[BaseModel]]] = {
    "event_semantics": ModelVisibleSemanticsInput,
    "reader_card": ModelVisibleCardInput,
}
