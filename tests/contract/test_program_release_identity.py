"""Release identity pins for the DSPy 3.4 News program."""

from __future__ import annotations

from copy import deepcopy

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import COMPILE_EPISODE_PROJECTION_SCHEMA
from tracefold.news.learning.metric import METRIC_ID
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_state
from tracefold.news.program.identity import compute_execution_identity, execution_envelope
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION

NEWS_EXECUTION_ENVELOPE_SHA256 = "80fac18a43fa85e8b7891a306d4c8a593cd6cc94a5afe807addd84bc29d7c1a7"
NEWS_PREDICTOR_INSTRUCTION_SHA256 = "ae90195509063bf98de4f021fe4e3c2b6161627d2d312883431666d1439e7033"
NEWS_STABLE_PROGRAM_SHA256 = "3f2e9a72386c2b0518d8f8e07fdf549f6a6da6fd12755a4d9b6e332bb0e56cc1"
NEWS_COMPILE_EPISODE_PROJECTION_SCHEMA = "tracefold.news.development_compile_episode.v8"


def test_execution_envelope_is_content_addressed_and_pinned() -> None:
    envelope = execution_envelope()
    assert envelope["identity_schema"] == "tracefold.news.program.execution_envelope.v9"
    assert envelope["framework"]["dspy"] == "3.4.0"
    assert envelope["framework"]["request_contract"] == "dspy.lm15.Request/Response"
    assert set(envelope["seed_signatures"]) == {"event_semantics", "taxonomy", "reader_card"}
    assert envelope["route"]["predictor_order"] == ["event_semantics", "taxonomy", "reader_card"]
    assert canonical_sha(envelope) == compute_execution_identity() == NEWS_EXECUTION_ENVELOPE_SHA256


def test_execution_identity_changes_with_material_contract() -> None:
    envelope = execution_envelope()
    changed = deepcopy(envelope)
    changed["route"]["deadline_seconds"] += 1
    assert canonical_sha(changed) != NEWS_EXECUTION_ENVELOPE_SHA256
    changed = deepcopy(envelope)
    changed["seed_signatures"]["taxonomy"]["instructions"] += " changed"
    assert canonical_sha(changed) != NEWS_EXECUTION_ENVELOPE_SHA256


def test_current_news_release_identity_is_byte_exact() -> None:
    assert {
        "program_version": PROGRAM_VERSION,
        "policy_version": TRIAGE_POLICY_VERSION,
        "review_rubric_version": REVIEW_RUBRIC_VERSION,
        "metric_id": METRIC_ID,
        "program_sha256": load_stable_program_state().program_sha256,
    } == {
        "program_version": "news_semantic_program_v13",
        "policy_version": "news_triage_policy_v17",
        "review_rubric_version": "news_review_v8",
        "metric_id": "tracefold.news.production_action_fact_kind_v12",
        "program_sha256": NEWS_STABLE_PROGRAM_SHA256,
    }


def test_current_predictor_bytes_keep_reviewed_instruction_identity() -> None:
    artifact = load_stable_program_state()
    bound = {
        predictor: artifact.predictor_state(predictor).instruction
        for predictor in ("event_semantics", "taxonomy", "reader_card")
    }
    assert canonical_sha(bound) == NEWS_PREDICTOR_INSTRUCTION_SHA256


def test_compile_episode_projection_identity_is_pinned() -> None:
    assert COMPILE_EPISODE_PROJECTION_SCHEMA == NEWS_COMPILE_EPISODE_PROJECTION_SCHEMA
