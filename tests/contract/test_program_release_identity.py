from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.metric import METRIC_ID
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.identity import compute_execution_identity, execution_envelope
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION

# The one pin over code-owned Program behavior (#314). It is a named constant and not a bare literal
# inside an assertion on purpose: `rg NEWS_EXECUTION_ENVELOPE_SHA256` has to find every place that claims
# to know this value, which is the rule an anonymous `== 8` broke on the last identity bump.
NEWS_EXECUTION_ENVELOPE_SHA256 = "2be3327309268a66b3c4e58e1c5497374751ae4cfe49cd8aa22088c57ecd99d5"

# The prompt bytes the provider is sent, pinned separately because they have a separate author: a human
# edits `seed.py` and GEPA proposes a replacement, and both move this without touching the envelope.
NEWS_PREDICTOR_INSTRUCTION_SHA256 = "e1a1b65b061feabc6291760b74575c3e803ac2b0252aa527359e37f6a4b21dc5"

NEWS_STABLE_PROGRAM_SHA256 = "c71bd9041f26d8ee75f055dc0997a92a2b44c1fbdb0d00d1a2e9ecb18ee675a4"


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
        "program_version": "news_semantic_program_v5",
        "policy_version": "news_triage_policy_v10",
        "review_rubric_version": "news_review_v4",
        "metric_id": "tracefold.news.production_action_trade_relevance_v5",
        "program_sha256": NEWS_STABLE_PROGRAM_SHA256,
    }


def test_current_predictor_bytes_keep_the_reviewed_instruction_identity() -> None:
    """The prompt the provider is sent, pinned.

    Unchanged across #314 by construction: the artifact lost a `factory_id` field, which moved
    `program_sha256`, and this hash covers only the two instruction texts — so its staying still is the
    evidence that no seed byte moved with the identity.
    """

    artifact = load_stable_program_artifact()
    bound = {
        predictor: artifact.predictor_state(predictor).instruction for predictor in ("event_semantics", "reader_card")
    }

    assert canonical_sha(bound) == NEWS_PREDICTOR_INSTRUCTION_SHA256


def test_the_envelope_names_every_code_owned_surface_it_claims_to_cover() -> None:
    """The material list, in one readable place, because a pin is only as good as what it hashes.

    A pin cannot catch material that was never included: dropping a surface fails the hash loudly, but
    forgetting to add one in the first place is silent forever. So the list is asserted rather than left
    implicit, and adding a knob to the route or a mode to the request table has to be a visible edit here.
    """

    envelope = execution_envelope()

    assert set(envelope) == {"identity_schema", "requests", "model_visible_input", "route"}
    assert set(envelope["requests"]) == set(envelope["model_visible_input"]) == {"event_semantics", "reader_card"}
    for predictor, modes in envelope["requests"].items():
        assert set(modes) == {"json_schema", "json_object"}, predictor
        for mode, request in modes.items():
            # The whole wire envelope, not a summary of it: the system message carries the output
            # contract, the user message carries the field order and headings, and `response_format`
            # carries the schema the provider is held to.
            assert set(request) == {
                "model",
                "messages",
                "temperature",
                "max_tokens",
                "stream",
                "response_format",
            }, (predictor, mode)
            assert [message["role"] for message in request["messages"]] == ["system", "user"]
    assert set(envelope["route"]) == {
        "model_binding_slots",
        "wire_model_prefix",
        "json_object_only_model_prefixes",
        "deadline_seconds",
        "primary_breaker_failures",
        "primary_breaker_open_seconds",
        "truncated_finish_reasons",
        "retryable_status",
        "retryable_markers",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e["route"].__setitem__("deadline_seconds", 999), id="route_deadline"),
        pytest.param(lambda e: e["route"].__setitem__("primary_breaker_failures", 999), id="breaker"),
        pytest.param(lambda e: e["route"]["json_object_only_model_prefixes"].append("qwen"), id="capability_table"),
        pytest.param(lambda e: e["route"]["model_binding_slots"].pop(), id="binding_slots"),
        pytest.param(
            lambda e: e["requests"]["event_semantics"]["json_schema"].__setitem__("max_tokens", 999),
            id="predictor_token_ceiling",
        ),
        pytest.param(
            lambda e: e["requests"]["reader_card"]["json_schema"]["messages"][0].__setitem__("content", "rewritten"),
            id="output_contract_text",
        ),
        pytest.param(
            lambda e: e["requests"]["reader_card"]["json_schema"]["messages"][1].__setitem__("content", "reordered"),
            id="user_message_field_order",
        ),
        pytest.param(
            lambda e: e["requests"]["event_semantics"]["json_schema"]["response_format"].__setitem__("type", "other"),
            id="response_format",
        ),
        pytest.param(
            lambda e: e["model_visible_input"]["event_semantics"].__setitem__("open", "<other>"),
            id="untrusted_delimiters",
        ),
        pytest.param(
            lambda e: e["model_visible_input"]["reader_card"]["schema"].__setitem__("title", "Other"),
            id="model_visible_schema",
        ),
    ],
)
def test_every_material_surface_actually_moves_the_identity(mutate: Any) -> None:
    """Sensitivity, one surface at a time: a pin nothing can move is decoration."""

    mutated = execution_envelope()
    mutate(mutated)

    assert canonical_sha(mutated) != NEWS_EXECUTION_ENVELOPE_SHA256
