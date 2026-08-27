from __future__ import annotations

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.metric import METRIC_ID
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import (
    ProgramStrategyArtifactCodec,
    load_stable_program_artifact,
    render_predictor_instruction,
)
from tracefold.news.program.runtime import PROGRAM_FACTORY_ID, PROGRAM_LEARNING_EPOCH, PROGRAM_VERSION
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION


def test_current_news_release_identity_is_byte_exact() -> None:
    """Protect the small release identity after retiring the historical refactor ledger."""

    artifact = load_stable_program_artifact()
    assert ProgramStrategyArtifactCodec.encode(artifact) == (
        '{"event_semantics_instruction":"","factory_id":"tracefold.news.program.factory_v6",'
        '"program_sha256":"e54c8d69b9606b7306e0e829a09994dd525743b5c12ec9e549a7f67ef6a2ea06",'
        '"reader_card_instruction":"","schema_version":"news_program_strategy_artifact_v1"}\n'
    )
    assert {
        "factory_id": PROGRAM_FACTORY_ID,
        "program_version": PROGRAM_VERSION,
        "learning_epoch": PROGRAM_LEARNING_EPOCH,
        "policy_version": TRIAGE_POLICY_VERSION,
        "review_rubric_version": REVIEW_RUBRIC_VERSION,
        "metric_id": METRIC_ID,
    } == {
        "factory_id": "tracefold.news.program.factory_v6",
        "program_version": "news_semantic_program_v5",
        "learning_epoch": "program_v7",
        "policy_version": "news_triage_policy_v10",
        "review_rubric_version": "news_review_v4",
        "metric_id": "tracefold.news.production_action_trade_relevance_v4",
    }


def test_current_rendered_predictor_bytes_keep_the_reviewed_factory_identity() -> None:
    artifact = load_stable_program_artifact()
    rendered = {
        predictor: render_predictor_instruction(predictor, artifact.instruction_for(predictor))
        for predictor in ("event_semantics", "reader_card")
    }
    assert canonical_sha(rendered) == "3fe3cb009977e316e10a094e7115b0889529e53d9213602f37e625920b09f779"
