from __future__ import annotations

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.metric import METRIC_ID
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.runtime import PROGRAM_FACTORY_ID, PROGRAM_LEARNING_EPOCH, PROGRAM_VERSION
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION


def test_current_news_release_identity_is_byte_exact() -> None:
    """Protect the small release identity after retiring the historical refactor ledger."""

    assert {
        "factory_id": PROGRAM_FACTORY_ID,
        "program_version": PROGRAM_VERSION,
        "learning_epoch": PROGRAM_LEARNING_EPOCH,
        "policy_version": TRIAGE_POLICY_VERSION,
        "review_rubric_version": REVIEW_RUBRIC_VERSION,
        "metric_id": METRIC_ID,
        "program_sha256": load_stable_program_artifact().program_sha256,
    } == {
        "factory_id": "tracefold.news.program.factory_v9",
        "program_version": "news_semantic_program_v5",
        "learning_epoch": "program_v9",
        "policy_version": "news_triage_policy_v10",
        "review_rubric_version": "news_review_v4",
        "metric_id": "tracefold.news.production_action_trade_relevance_v5",
        "program_sha256": "23bb047c1ca2e2caef2b713154f7d0fe5eabe98bfdaddb4417aa7a889982b754",
    }


def test_current_predictor_bytes_keep_the_reviewed_factory_identity() -> None:
    """The prompt the provider is sent, pinned.

    Until #306 Phase 2 this hashed the *rendered* instruction — kernel plus RulePacks plus advisory plus
    seal — because the artifact carried only the advisory and the bytes were assembled elsewhere. There is
    no renderer any more, so this hashes what a Predictor is bound to, which is the artifact's own text.
    The pin is still worth having for the reason it always was: a prompt edit is a release event, and an
    accidental one has to fail a test rather than reach a reader.
    """

    artifact = load_stable_program_artifact()
    bound = {
        predictor: artifact.predictor_state(predictor).instruction for predictor in ("event_semantics", "reader_card")
    }

    assert canonical_sha(bound) == "e1a1b65b061feabc6291760b74575c3e803ac2b0252aa527359e37f6a4b21dc5"
