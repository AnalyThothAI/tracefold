"""#501 D8: two accepted blind-draft reviews freeze into a corpus whose receipt reports inter-drafter κ."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.integration.test_news_candidate_evaluator import (
    NOW,
    PRINCIPAL,
    ReviewDesk,
    _arm,
    _datasets,
    _open_event,
    _rubric,
)
from tests.integration.test_news_candidate_evaluator import conn as conn  # noqa: PLC0414 - the fixture
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.learning.contracts import ClosedWindow
from tracefold.news.learning.dataset import DatasetSpec
from tracefold.news.review.desk import DeskQuery, EventRubricSubmission, TaskRef
from tracefold.news.review.drafter import ReviewDraft, submission_payload

pytestmark = pytest.mark.integration

_DRAFTERS = ("openai/deepseek-v4-pro", "openai/qwen3.8-27b:thinking")
_PRODUCT = {
    "subject_codes": ["medtop:20000205"],
    "event_family": "product_service_change",
    "change_state": "effective",
    "assertion_status": "confirmed",
}


def _accept_blind_drafts(conn, *, hit_id: int, title: str, label_b: dict[str, object]) -> str:
    """Accept one review through the drafter's own `submission_payload`, exactly as `accept-drafts` does."""

    stable = _arm()
    event_id = _open_event(
        conn,
        bundle_sha=stable.bundle_sha,
        program_version=stable.program_version,
        program_sha256=stable.program_sha256,
        hit_id=hit_id,
        title=title,
    )
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    rubric = _rubric(why="pass").model_dump(mode="json")
    draft = ReviewDraft.model_validate(
        {
            "should_push": rubric["should_push"],
            # The drafter never judges `why_*`; the accepting reviewer does. Code writes the taxonomy_* five.
            "dimensions": {
                name: label for name, label in rubric["dimensions"].items() if name not in {"why_support", "why_value"}
            },
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
            "confidence": 0.9,
            "taxonomy": _PRODUCT,
            "taxonomy_drafts": {_DRAFTERS[0]: _PRODUCT, _DRAFTERS[1]: label_b},
            "taxonomy_disagreement": label_b != _PRODUCT,
        }
    )
    # The fixture Event is Reuters-sourced; code derives the authority and the desk refuses any other.
    payload = submission_payload(draft, source_authority="reputable_secondary")
    submission = EventRubricSubmission.model_validate(payload)
    assert submission.taxonomy_review.draft_author == "+".join(_DRAFTERS)
    with repositories_for_connection(conn).transaction():
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            submission,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    return event_id


def test_blind_dual_drafts_are_accepted_and_the_freeze_reports_kappa(conn) -> None:
    stable = _arm()
    _accept_blind_drafts(conn, hit_id=112301, title="Coinbase custody service is now live", label_b=_PRODUCT)
    _accept_blind_drafts(
        conn,
        hit_id=112302,
        title="Kraken lists a new perpetual market today",
        label_b={**_PRODUCT, "change_state": "announced", "assertion_status": "claimed"},
    )

    manifest = asyncio.run(
        _datasets(conn, stable).freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )

    calibration = manifest.counts["calibration"]
    assert calibration["schema"] == "tracefold.news.dataset_calibration_receipt.v2"
    assert calibration["cluster_n"] == calibration["dual_labeled_n"] == 2
    assert calibration["drafter_models"] == sorted(_DRAFTERS)
    assert set(calibration["kappa"]) == {"event_family", "change_state", "assertion_status"}
    # Both drafters agree on family everywhere and disagree on state/assertion for one of two clusters.
    assert calibration["kappa"]["event_family"] == 1.0
    assert calibration["kappa"]["change_state"] < 1.0
    assert calibration["subject_mean_set_f1"] == 1.0
    assert manifest.calibration == calibration
    export = _datasets(conn, stable).development_compile_export(manifest.artifact_sha)
    provenance = [episode["accepted_review"]["taxonomy_review"] for episode in export.episodes]
    assert all(set(item["drafts"]) == set(_DRAFTERS) for item in provenance)
    assert all(item["label_source"] == "model_draft" for item in provenance)
