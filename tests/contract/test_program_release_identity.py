from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import COMPILE_EPISODE_PROJECTION_SCHEMA
from tracefold.news.learning.metric import METRIC_ID
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.identity import (
    _material_module_ast_sha,
    _material_symbol_ast_sha,
    compute_execution_identity,
    execution_envelope,
)
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION

# The one pin over code-owned Program behavior (#314). It is a named constant and not a bare literal
# inside an assertion on purpose: `rg NEWS_EXECUTION_ENVELOPE_SHA256` has to find every place that claims
# to know this value, which is the rule an anonymous `== 8` broke on the last identity bump.
NEWS_EXECUTION_ENVELOPE_SHA256 = "2a3d7306a902b4f687c177bbb491dae6c60fb7ee67b188a7d597f84d805b228f"

# The prompt bytes the provider is sent, pinned separately because they have a separate author: a human
# edits `seed.py` and GEPA proposes a replacement, and both move this without touching the envelope.
NEWS_PREDICTOR_INSTRUCTION_SHA256 = "360340899edeb66c079ba0f7b6294fed623d4eda37629c636c54902e235d5997"

NEWS_STABLE_PROGRAM_SHA256 = "63e5b438f7419e02621e419f3a3ad9860dfcc54bf2eea86c0896bcc04ebb4c64"

# #437 changes Gold projection. It remains release evidence after #453 moves taxonomy Gold into the one
# development Objective and Metric: a behavior edit must visibly re-pin this name.
NEWS_COMPILE_EPISODE_PROJECTION_SCHEMA = "tracefold.news.development_compile_episode.v6"


def test_execution_envelope_identity_is_pinned() -> None:
    """The intent gate over everything the code decides about a model call.

    Editing the request envelope, either output contract, an output schema, the visible-input shape, the
    route budget, the breaker or the endpoint-capability table turns this red. Re-pinning the line below
    *is* the identity migration: there is no `factory_id` to bump, no epoch migration to write and no
    count to keep in step, because the epoch is opened by the deployment that runs under this value and
    named after the bundle that carries it.

    When it fails, diff `execution_envelope()` before deciding. A hash says something moved; the document
    says what, and that is the difference between signing a release and pasting a value.
    """

    assert compute_execution_identity() == NEWS_EXECUTION_ENVELOPE_SHA256


def test_execution_envelope_document_addresses_the_pinned_identity() -> None:
    """The readable document and the hash are the same fact, so a reviewer may trust the diff."""

    assert canonical_sha(execution_envelope()) == NEWS_EXECUTION_ENVELOPE_SHA256


def test_current_news_release_identity_is_byte_exact() -> None:
    """The small release identity beside the envelope: the versions a report prints and a row carries."""

    assert {
        "program_version": PROGRAM_VERSION,
        "policy_version": TRIAGE_POLICY_VERSION,
        "review_rubric_version": REVIEW_RUBRIC_VERSION,
        "metric_id": METRIC_ID,
        "program_sha256": load_stable_program_artifact().program_sha256,
    } == {
        "program_version": "news_semantic_program_v8",
        "policy_version": "news_triage_policy_v11",
        "review_rubric_version": "news_review_v6",
        "metric_id": "tracefold.news.production_action_trade_relevance_v8",
        "program_sha256": NEWS_STABLE_PROGRAM_SHA256,
    }


def test_current_predictor_bytes_keep_the_reviewed_instruction_identity() -> None:
    """The prompt the provider is sent, pinned.

    #117 intentionally moves the EventSemantics instruction because taxonomy is now a required production
    output. This separate pin proves later identity-only edits cannot silently move either instruction.
    """

    artifact = load_stable_program_artifact()
    bound = {
        predictor: artifact.predictor_state(predictor).instruction for predictor in ("event_semantics", "reader_card")
    }

    assert canonical_sha(bound) == NEWS_PREDICTOR_INSTRUCTION_SHA256


def test_compile_episode_projection_identity_is_pinned() -> None:
    assert COMPILE_EPISODE_PROJECTION_SCHEMA == NEWS_COMPILE_EPISODE_PROJECTION_SCHEMA


def test_the_envelope_names_every_code_owned_surface_it_claims_to_cover() -> None:
    """The material list, in one readable place, because a pin is only as good as what it hashes.

    A pin cannot catch material that was never included: dropping a surface fails the hash loudly, but
    forgetting to add one in the first place is silent forever. So the list is asserted rather than left
    implicit, and adding a knob to the route or a mode to the request table has to be a visible edit here.
    """

    envelope = execution_envelope()

    assert set(envelope) == {
        "identity_schema",
        "framework",
        "implementation_ast_sha256",
        "signatures",
        "requests",
        "capabilities",
        "model_visible_input",
        "adapter_output_semantics",
        "request_identity",
        "assembly",
        "route",
    }
    assert envelope["framework"] == {
        "dspy": "3.3.1",
        "litellm": "1.86.2",
        "gepa": "0.1.4",
        "public_api_only": True,
        "adapter": "dspy.JSONAdapter(use_native_function_calling=False)",
    }
    assert set(envelope["requests"]) == set(envelope["model_visible_input"]) == {"event_semantics", "reader_card"}
    assert set(envelope["signatures"]) == {"event_semantics", "reader_card"}
    assert set(envelope["implementation_ast_sha256"]) == {
        "artifact.render_model_evidence_json",
        "assembly.normalize_restates",
        "assembly.restatement_index_error",
        "contracts.EditorialEnvelope",
        "contracts.ProgramTrace",
        "contracts.TriageContext",
        "contracts.TradeRelevanceV1",
        "contracts._canonical_code_set",
        "contracts.aggregate_program_usage",
        "lm.AuditedConfiguredLM",
        "lm.LMCallLedger",
        "lm.LMCallReceipt",
        "lm.LMDelegateProgramError",
        "lm.RecordedLM",
        "lm.RuntimeModelIdentity",
        "lm._LedgerParseCallback",
        "lm._RecordedErrorModel",
        "lm._RecordedResponseModel",
        "lm._RecordedUsageModel",
        "lm._RecordingModel",
        "lm._RequestIdentityModel",
        "lm._cost_microusd",
        "lm._error_code",
        "lm._recorded_error",
        "lm._recorded_response",
        "lm._recording",
        "lm._reject_secret_shaped_config",
        "lm._replayed_error",
        "lm._safe_config_projection",
        "lm._safe_extra_body",
        "lm._safe_retry_after",
        "lm._safe_status",
        "lm._sanitized_lm_error",
        "lm._scrub_detail",
        "lm._stable_error_code",
        "lm._usage_values",
        "lm._validate_request_defaults",
        "lm.lm_request_identity",
        "lm.lm_request_projection",
        "lm.lm_request_sha256",
        "lm.mark_active_domain_failure",
        "lm.program_json_adapter",
        "lm.structured_output_capability",
        "module.NativeNewsProgram",
        "module._assemble",
        "module._normalize_and_validate_semantics",
        "module._prepare",
        "module._reader_card_semantic_view",
        "module._rejected",
        "module._relevance_normalizations",
        "routing.__module__",
        "signatures.EventSemantics",
        "signatures.ReaderCard",
        "taxonomy.ModelTaxonomyV1",
        "taxonomy.NewsTaxonomyV1",
        "taxonomy.source_authority",
        "taxonomy.source_authority_from_evidence",
    }
    for predictor, modes in envelope["requests"].items():
        assert set(modes) == {"json_schema", "json_object", "prompt_json"}, predictor
        for mode, path in modes.items():
            request = path["initial"]
            assert set(request) == {"schema", "model", "messages", "tools", "config"}, (predictor, mode)
            assert [message["role"] for message in request["messages"]] == ["system", "user"]
            assert ("response_format" in request["config"]) == (mode != "prompt_json")
            assert (path["format_fallback"] is not None) == (mode == "json_schema")
            if path["format_fallback"] is not None:
                assert path["format_fallback"]["config"]["response_format"] == {"type": "json_object"}
    assert set(envelope["assembly"]) == {
        "normalization_capture",
        "restatement_index",
        "normalize_restates",
        "trade_channel_order",
        "trade_affected_market_order",
        "taxonomy",
    }
    assert set(envelope["assembly"]["taxonomy"]) == {
        "codebook_sha256",
        "subject_codes",
        "source_authority_classifier_version",
        "source_authority_registry_sha256",
        "source_authority_golden",
    }
    assert set(envelope["route"]) == {
        "model_binding_slots",
        "order",
        "route_graph",
        "fallback_restart",
        "deadline_seconds",
        "primary_breaker",
        "call_ceiling",
        "transitions",
    }


def test_material_ast_identity_ignores_prose_and_unrelated_symbols_but_moves_on_behavior() -> None:
    base = 'def material(value):\n    """prose"""\n    return value + 1\n\ndef unrelated():\n    return 1\n'
    prose_and_unrelated = (
        'def material(value):\n    """rewritten prose"""\n    return value + 1\n\ndef unrelated():\n    return 999\n'
    )
    behavior = "def material(value):\n    return value + 2\n"

    expected = _material_symbol_ast_sha(base, module="fixture", symbol="material")
    assert _material_symbol_ast_sha(prose_and_unrelated, module="fixture", symbol="material") == expected
    assert _material_symbol_ast_sha(behavior, module="fixture", symbol="material") != expected


@pytest.mark.parametrize(
    "mutation",
    [
        ("event_semantics: object", "event_semantics: str"),
        ("self.retryable = retryable", "self.retryable = False"),
        ("except Exception as exc:", "except RuntimeError as exc:"),
        ("self._primary_failures += 1", "self._primary_failures += 2"),
    ],
)
def test_routing_owner_module_identity_moves_for_transitive_behavior(mutation: tuple[str, str]) -> None:
    source = """
class RouteLMs:
    event_semantics: object

class _RouteFailure(Exception):
    def __init__(self, retryable):
        self.retryable = retryable

class RoutedSemanticJudge:
    def run(self):
        try:
            self.work()
        except Exception as exc:
            raise _RouteFailure(True) from exc

    def breaker(self):
        self._primary_failures += 1
"""
    before, after = mutation
    mutated = source.replace(before, after)

    assert mutated != source
    assert _material_module_ast_sha(mutated, module="routing") != _material_module_ast_sha(
        source,
        module="routing",
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e["route"].__setitem__("deadline_seconds", 999), id="route_deadline"),
        pytest.param(lambda e: e["route"]["primary_breaker"].__setitem__("failures", 999), id="breaker"),
        pytest.param(
            lambda e: e["capabilities"]["event_semantics.fallback"]["json_object"].__setitem__(
                "supports_response_schema", True
            ),
            id="capability_mapping",
        ),
        pytest.param(lambda e: e["route"]["model_binding_slots"].pop(), id="binding_slots"),
        pytest.param(
            lambda e: e["requests"]["event_semantics"]["json_schema"]["initial"]["config"].__setitem__(
                "max_tokens", 999
            ),
            id="predictor_token_ceiling",
        ),
        pytest.param(
            lambda e: e["requests"]["reader_card"]["json_schema"]["initial"]["messages"][0].__setitem__(
                "content", "rewritten"
            ),
            id="output_contract_text",
        ),
        pytest.param(
            lambda e: e["requests"]["reader_card"]["json_schema"]["initial"]["messages"][1].__setitem__(
                "content", "reordered"
            ),
            id="user_message_field_order",
        ),
        pytest.param(
            lambda e: e["requests"]["event_semantics"]["json_schema"]["format_fallback"]["config"][
                "response_format"
            ].__setitem__("type", "other"),
            id="response_format",
        ),
        pytest.param(lambda e: e["framework"].__setitem__("dspy", "9.9.9"), id="dspy_version"),
        pytest.param(
            lambda e: e["adapter_output_semantics"].__setitem__("outer_envelope_unknown_siblings", "reject"),
            id="outer_envelope_semantics",
        ),
        pytest.param(lambda e: e["route"]["call_ceiling"].__setitem__("judgment", 9), id="call_ceiling"),
        pytest.param(
            lambda e: e["route"]["transitions"].__setitem__("output_truncated", "format_retry"),
            id="truncation_transition",
        ),
        pytest.param(
            lambda e: e["model_visible_input"]["event_semantics"].__setitem__("open", "<other>"),
            id="untrusted_delimiters",
        ),
        pytest.param(
            lambda e: e["model_visible_input"]["reader_card"]["schema"].__setitem__("title", "Other"),
            id="model_visible_schema",
        ),
        pytest.param(
            lambda e: e["assembly"]["restatement_index"].__setitem__("restatement|restates=0|told=0", None),
            id="restatement_index_rule",
        ),
        pytest.param(lambda e: e["assembly"]["trade_channel_order"].reverse(), id="trade_channel_order"),
        pytest.param(
            lambda e: e["assembly"]["taxonomy"].__setitem__("source_authority_registry_sha256", "0" * 64),
            id="source_authority_registry",
        ),
        # Found one level up from `_assemble`: this decides what is stored.
        pytest.param(
            lambda e: e["assembly"]["normalize_restates"].__setitem__("progression|restates=0", 0),
            id="normalize_restates_rule",
        ),
    ],
)
def test_every_material_surface_actually_moves_the_identity(mutate: Any) -> None:
    """Sensitivity, one surface at a time: a pin nothing can move is decoration."""

    mutated = execution_envelope()
    mutate(mutated)

    assert canonical_sha(mutated) != NEWS_EXECUTION_ENVELOPE_SHA256
