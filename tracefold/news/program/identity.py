"""Computed identity of the native DSPy News execution envelope.

``program_sha256`` still names only the three reviewed instruction texts. This
module addresses everything code-owned around those texts: exact framework
versions, public Signature state, the JSONAdapter requests it actually renders,
capability mapping, route order and budgets, failure transitions, and the pure
News normalize/assemble surface. Runtime endpoints remain a sibling identity;
credentials never enter either document.
"""

from __future__ import annotations

import ast
import copy
import importlib.metadata
import logging
from pathlib import Path
from typing import Any, Final

from ..artifact_identity import canonical_json, canonical_sha
from ..taxonomy import (
    IPTC_CODEBOOK_SHA256,
    IPTC_SUBJECT_CODES,
    SOURCE_AUTHORITY_CLASSIFIER_VERSION,
    SOURCE_AUTHORITY_REGISTRY_SHA256,
    source_authority_from_evidence,
)
from .assembly import normalize_restates, restatement_index_error
from .contracts import TRADE_AFFECTED_MARKET_ORDER, TRADE_CHANNEL_ORDER, TriageContext
from .lm import (
    LM_REQUEST_IDENTITY_SCHEMA,
    LM_REQUEST_PROJECTION_SCHEMA,
    ScriptedLM,
    StructuredOutputMode,
    lm_request_projection,
    program_json_adapter,
    structured_output_capability,
)
from .module import _prepare, _reader_card_semantic_view
from .runtime import (
    _MODEL_BINDING_SLOTS,
    _UNTRUSTED_EVENT_CLOSE,
    _UNTRUSTED_EVENT_OPEN,
    _VISIBLE_INPUT,
    PROGRAM_JUDGMENT_MAX_CALLS,
    PROGRAM_PREDICTOR_MAX_CALLS,
    PROGRAM_PREDICTOR_MAX_TOKENS,
    PROGRAM_PRIMARY_BREAKER_FAILURES,
    PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
    PROGRAM_RETRYABLE_LM_ERROR_TYPES,
    PROGRAM_ROUTE_DEADLINE_SECONDS,
    PROGRAM_ROUTE_MAX_CALLS,
    PredictorName,
)
from .signatures import EventSemantics, EventSemanticsSignature, EventTaxonomySignature, ReaderCardSignature

# v5 (#501): a third Predictor, `taxonomy`, between EventSemantics and ReaderCard.
EXECUTION_IDENTITY_SCHEMA: Final[str] = "tracefold.news.program.execution_envelope.v5"

_GOLDEN_MODEL: Final[str] = "openai/tracefold-execution-identity"
_GOLDEN_INSTRUCTION: Final[str] = "<golden-instruction>"
_STRUCTURED_OUTPUT_MODES: Final[tuple[StructuredOutputMode, ...]] = (
    "json_schema",
    "json_object",
    "prompt_json",
)
_SIGNATURES: Final[dict[PredictorName, Any]] = {
    "event_semantics": EventSemanticsSignature,
    "taxonomy": EventTaxonomySignature,
    "reader_card": ReaderCardSignature,
}
_GOLDEN_OUTPUTS: Final[dict[PredictorName, dict[str, Any]]] = {
    "event_semantics": {
        "semantics": {
            "novelty": "new_fact",
            "restates": -1,
            "assets": [],
            "direction": "neutral",
            "scope": "single_name",
            "magnitude": 0,
            "confidence": 1.0,
            "audience": "none",
            "relevance": {
                "impact_breadth": "none",
                "tradability": "none",
                "surprise": "unknown",
                "development_delta": "color_only",
                "channels": [],
                "affected_markets": [],
                "reader_value": "none",
            },
        }
    },
    "taxonomy": {
        "taxonomy": {
            "subject_codes": [],
            "event_family": "other",
            "change_state": "unknown",
            "assertion_status": "unknown",
        }
    },
    "reader_card": {"card": {"headline_zh": "示例标题", "why_zh": "示例机制说明一句话。"}},
}
_MATERIAL_IMPLEMENTATION_SYMBOLS: Final[dict[str, tuple[str, ...]]] = {
    "artifact.py": ("render_model_evidence_json",),
    "assembly.py": ("normalize_restates", "restatement_index_error"),
    "contracts.py": (
        "EditorialEnvelope",
        "ProgramTrace",
        "TriageContext",
        "TradeRelevanceV1",
        "_canonical_code_set",
        "aggregate_program_usage",
    ),
    "lm.py": (
        "LMCallLedger",
        "LMCallReceipt",
        "LMDelegateProgramError",
        "RuntimeModelIdentity",
        "AuditedConfiguredLM",
        "RecordedLM",
        "_LedgerParseCallback",
        "_RecordingModel",
        "_RecordedErrorModel",
        "_RecordedResponseModel",
        "_RecordedUsageModel",
        "_RequestIdentityModel",
        "_recording",
        "_recorded_error",
        "_recorded_response",
        "_replayed_error",
        "_reject_secret_shaped_config",
        "_error_code",
        "_cost_microusd",
        "_safe_retry_after",
        "_safe_status",
        "_safe_config_projection",
        "_safe_extra_body",
        "_sanitized_lm_error",
        "_scrub_detail",
        "_stable_error_code",
        "_usage_values",
        "_validate_request_defaults",
        "lm_request_identity",
        "lm_request_projection",
        "lm_request_sha256",
        "mark_active_domain_failure",
        "program_json_adapter",
        "structured_output_capability",
    ),
    "module.py": (
        "NativeNewsProgram",
        "_assemble",
        "_normalize_and_validate_semantics",
        "_prepare",
        "_reader_card_semantic_view",
        "_rejected",
        "_relevance_normalizations",
    ),
    "signatures.py": ("EventSemantics", "EventTaxonomySignature", "ReaderCard"),
    "taxonomy.py": (
        "ASSERTION_STATUS_DEFINITIONS",
        "CHANGE_STATE_DEFINITIONS",
        "EVENT_FAMILY_DEFINITIONS",
        "ModelTaxonomyV1",
        "NewsTaxonomyV1",
        "TAXONOMY_PRECEDENCE_RULES",
        "render_taxonomy_seed_instruction",
        "source_authority",
        "source_authority_from_evidence",
    ),
}
_MATERIAL_IMPLEMENTATION_MODULES: Final[tuple[str, ...]] = ("routing.py",)


def _golden_inputs(predictor: PredictorName) -> dict[str, str]:
    context = TriageContext.from_card(
        {
            "event_id": "golden-event",
            "evidence_version": 1,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "golden-fact",
            "reporting_origin": "wire",
            "provenance": ["golden"],
            "leader_title": "Golden event",
            "raw_first_line": "Golden event",
            "leader_description": "Golden evidence",
            "opened_at_ms": 1_000,
            "member_count": 1,
            "dedupe_family": "general",
            "provider_metadata": {},
            "queue_priority": "normal",
            "asset_class": "none",
            "grounded_assets": [],
            "storyline_key": "golden",
        },
        watchlist=(),
        told_rows=(),
        now_ms=2_000,
        queue_lag_ms=1_000,
    )
    prepared = _prepare(context)
    if predictor == "event_semantics":
        return {"evidence_json": prepared.semantics_evidence_json}
    if predictor == "taxonomy":
        return {"evidence_json": prepared.taxonomy_evidence_json}
    semantics = EventSemantics.model_validate(_GOLDEN_OUTPUTS["event_semantics"]["semantics"])
    return {
        "evidence_json": prepared.card_evidence_json,
        "semantics_json": canonical_json(_reader_card_semantic_view(semantics).model_dump(mode="json")),
    }


def _capture_requests(predictor: PredictorName, mode: StructuredOutputMode) -> dict[str, Any]:
    """Exercise public JSONAdapter/BaseLM APIs and retain their typed requests."""

    signature = _SIGNATURES[predictor].with_instructions(_GOLDEN_INSTRUCTION)
    inputs = _golden_inputs(predictor)
    steps: list[Any] = [_GOLDEN_OUTPUTS[predictor]]
    if mode == "json_schema":
        # Stock JSONAdapter may make exactly one json_object fallback after a
        # schema answer that cannot parse. Capturing both proves the real path.
        steps.insert(0, "not-json")
    lm = ScriptedLM(steps, model=_GOLDEN_MODEL, structured_output=mode)
    adapter_logger = logging.getLogger("dspy.adapters.json_adapter")
    previous_level = adapter_logger.level
    try:
        adapter_logger.setLevel(logging.ERROR)
        program_json_adapter()(
            lm,
            {"max_tokens": PROGRAM_PREDICTOR_MAX_TOKENS[predictor]},
            signature,
            [],
            inputs,
        )
    finally:
        adapter_logger.setLevel(previous_level)
    requests = [lm_request_projection(request) for request in lm.requests]
    expected = 2 if mode == "json_schema" else 1
    if len(requests) != expected:
        raise RuntimeError("news_program_identity_adapter_path_unexpected")
    return {
        "initial": requests[0],
        "format_fallback": requests[1] if len(requests) == 2 else None,
    }


_GOLDEN_REQUESTS: Final[dict[PredictorName, dict[str, dict[str, Any]]]] = {
    predictor: {mode: _capture_requests(predictor, mode) for mode in _STRUCTURED_OUTPUT_MODES}
    for predictor in _SIGNATURES
}

_NOVELTIES: Final[tuple[str, ...]] = ("new_fact", "progression", "restatement")


def _assembly_surface() -> dict[str, Any]:
    return {
        "normalization_capture": {
            "source": "typed_event_semantics_pre_validation_code_order",
            "fields": ["channels", "affected_markets", "restates"],
        },
        "restatement_index": {
            f"{novelty}|restates={restates}|told={told}": restatement_index_error(
                novelty=novelty,
                restates=restates,
                told_count=told,
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
        "trade_channel_order": list(TRADE_CHANNEL_ORDER),
        "trade_affected_market_order": list(TRADE_AFFECTED_MARKET_ORDER),
        "taxonomy": {
            "codebook_sha256": IPTC_CODEBOOK_SHA256,
            "subject_codes": list(IPTC_SUBJECT_CODES),
            "source_authority_classifier_version": SOURCE_AUTHORITY_CLASSIFIER_VERSION,
            "source_authority_registry_sha256": SOURCE_AUTHORITY_REGISTRY_SHA256,
            "source_authority_golden": source_authority_from_evidence({"source": "wire"}),
        },
    }


def _capability_mapping() -> dict[str, Any]:
    modes = {mode: structured_output_capability(mode) for mode in _STRUCTURED_OUTPUT_MODES}
    return {slot: copy.deepcopy(modes) for slot in sorted(_MODEL_BINDING_SLOTS)}


class _WithoutDocstrings(ast.NodeTransformer):
    """Remove non-executable prose before material symbol identity is computed."""

    @staticmethod
    def _strip(node: Any) -> Any:
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        return self.generic_visit(self._strip(node))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return self.generic_visit(self._strip(node))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return self.generic_visit(self._strip(node))

    def visit_Module(self, node: ast.Module) -> Any:
        return self.generic_visit(self._strip(node))


def _implementation_ast_identities() -> dict[str, str]:
    program_root = Path(__file__).resolve().parent
    identities: dict[str, str] = {}
    for filename in _MATERIAL_IMPLEMENTATION_MODULES:
        module = filename.removesuffix(".py")
        source = (program_root / filename).read_text()
        identities[f"{module}.__module__"] = _material_module_ast_sha(source, module=module)
    for filename, symbols in _MATERIAL_IMPLEMENTATION_SYMBOLS.items():
        in_program = filename != "taxonomy.py"
        module = filename.removesuffix(".py")
        source = ((program_root if in_program else program_root.parent) / filename).read_text()
        identity_module = module
        for symbol in symbols:
            identities[f"{identity_module}.{symbol}"] = _material_symbol_ast_sha(
                source, module=identity_module, symbol=symbol
            )
    return dict(sorted(identities.items()))


def _material_module_ast_sha(source: str, *, module: str) -> str:
    normalized = _WithoutDocstrings().visit(ast.parse(source))
    return canonical_sha(
        {
            "module": f"tracefold.news.program.{module}",
            "ast": ast.dump(normalized, annotate_fields=True, include_attributes=False),
        }
    )


def _symbol_name(node: ast.stmt) -> str | None:
    """The name a top-level statement defines: a class, a function, or one module constant (#501)."""

    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def _material_symbol_ast_sha(source: str, *, module: str, symbol: str) -> str:
    tree = ast.parse(source)
    node = next(
        (child for child in tree.body if _symbol_name(child) == symbol),
        None,
    )
    if node is None:
        raise RuntimeError(f"news_program_identity_material_symbol_missing:{module}.{symbol}")
    normalized = _WithoutDocstrings().visit(copy.deepcopy(node))
    return canonical_sha(
        {
            "module": (f"tracefold.news.{module}" if module == "taxonomy" else f"tracefold.news.program.{module}"),
            "symbol": symbol,
            "ast": ast.dump(normalized, annotate_fields=True, include_attributes=False),
        }
    )


def execution_envelope() -> dict[str, Any]:
    """Readable material addressed by :func:`compute_execution_identity`."""

    return {
        "identity_schema": EXECUTION_IDENTITY_SCHEMA,
        "framework": {
            "dspy": importlib.metadata.version("dspy"),
            "litellm": importlib.metadata.version("litellm"),
            "gepa": importlib.metadata.version("gepa"),
            "public_api_only": True,
            "adapter": "dspy.JSONAdapter(use_native_function_calling=False)",
        },
        "implementation_ast_sha256": _implementation_ast_identities(),
        "signatures": {
            predictor: signature.with_instructions(_GOLDEN_INSTRUCTION).dump_state()
            for predictor, signature in _SIGNATURES.items()
        },
        "requests": copy.deepcopy(_GOLDEN_REQUESTS),
        "capabilities": _capability_mapping(),
        "model_visible_input": {
            predictor: {
                "open": _UNTRUSTED_EVENT_OPEN,
                "close": _UNTRUSTED_EVENT_CLOSE,
                "schema": _VISIBLE_INPUT[predictor].model_json_schema(),
            }
            for predictor in _SIGNATURES
        },
        "adapter_output_semantics": {
            "business_model_extra": "forbid",
            "outer_envelope_unknown_siblings": "filtered_by_dspy_json_adapter",
            "schema_parse_failure": "one_json_object_format_fallback",
            "provider_lm_error": "no_format_fallback",
            "truncation": "typed_lm_error_no_format_fallback",
        },
        "request_identity": {
            "allowlist": [
                "endpoint_fingerprint",
                "model_binding",
                "model",
                "messages",
                "tools",
                "response_format",
                "safe_config",
            ],
            "excluded": [
                "credential",
                "raw_endpoint_url",
                "secret_headers",
                "reasoning",
                "provider_error_body",
            ],
            "address_schema": LM_REQUEST_IDENTITY_SCHEMA,
            "projection_schema": LM_REQUEST_PROJECTION_SCHEMA,
        },
        "assembly": _assembly_surface(),
        "route": {
            "model_binding_slots": sorted(_MODEL_BINDING_SLOTS),
            "order": ["primary", "fallback"],
            "route_graph": ["event_semantics", "normalize_validate", "taxonomy", "reader_card", "assemble"],
            "fallback_restart": "event_semantics",
            "deadline_seconds": PROGRAM_ROUTE_DEADLINE_SECONDS,
            "primary_breaker": {
                "failures": PROGRAM_PRIMARY_BREAKER_FAILURES,
                "open_seconds": PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
                "retryable_provider_failure": "increment",
                "parse_domain_truncation_failure": "do_not_increment",
                "success": "reset",
                "open": "zero_physical_primary_calls_then_fallback",
                "retryable_lm_error_types": list(PROGRAM_RETRYABLE_LM_ERROR_TYPES),
            },
            "call_ceiling": {
                "common_success": len(_SIGNATURES),
                "predictor": PROGRAM_PREDICTOR_MAX_CALLS,
                "route": PROGRAM_ROUTE_MAX_CALLS,
                "judgment": PROGRAM_JUDGMENT_MAX_CALLS,
            },
            "transitions": {
                "provider_error_retryable": "fallback_and_primary_breaker",
                "provider_error_nonretryable": "fallback",
                "adapter_parse_error_after_format_fallback": "fallback_output_failure",
                "domain_validation_error": "fallback_output_failure",
                "output_truncated": "fallback_output_failure_no_format_retry",
                "timeout_cancelled": "fallback_and_primary_breaker",
                "late_completion": "fallback_and_primary_breaker",
                "dual_route_failure": "SemanticJudgeError",
            },
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
