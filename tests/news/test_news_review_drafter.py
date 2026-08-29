"""#148: model-drafted rubrics are proposals, never evidence."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.review.desk import EventRubricSubmission
from tracefold.news.review.drafter import (
    DRAFTER_ID,
    ConfiguredDrafterLM,
    ReviewDraft,
    ReviewDrafter,
    build_draft_batch,
    build_drafter_lm,
    submission_payload,
)

_GOOD = {
    "should_push": "should_push",
    "dimensions": {
        "factual_fidelity": "pass",
        "headline_fidelity": "pass",
        "magnitude": "fail",
        "timeliness": "not_applicable",
    },
    "novelty": {"judgment": "new_fact", "duplicate_of": ""},
    "expected": {"magnitude": 2},
    "expected_correction": "产能承诺属于公司自身产品线变化，应为 magnitude 2",
    "confidence": 0.8,
    "reasoning": "卡片把一项产能承诺记成了例行更新",
}


class _ScriptedDrafterLM(dspy.BaseLM):  # type: ignore[misc]
    def __init__(self, payload: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        super().__init__(model="scripted/drafter")
        self._payload = payload if payload is not None else _GOOD
        self._fail = fail
        self.calls = 0

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        self.calls += 1
        if self._fail:
            raise RuntimeError("provider unavailable")
        return [json.dumps({"draft": self._payload})]


class _RequestSpyDrafterLM(ConfiguredDrafterLM):
    forward_contract = "typed_lm"

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.requests: list[dspy.LMRequest] = []
        super().__init__(model, **kwargs)

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        return dspy.LMResponse.from_text(json.dumps({"draft": _GOOD}), model=self.model)


def _tasks(n: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "task_id": f"evt.{i:064d}.1",
            "task_version": f"{i:064d}",
            "event_id": f"{i:064d}",
            "headline_zh": f"公司 {i} 承诺新增产能",
            "evidence_json": "{}",
            "card_json": "{}",
            "told_json": "[]",
        }
        for i in range(1, n + 1)
    ]


def test_a_draft_becomes_a_valid_submission_without_hand_reshaping() -> None:
    """The accept step must not have to massage model output; the rubric's validators decide."""

    draft = ReviewDraft.model_validate(_GOOD)
    submission = EventRubricSubmission(**submission_payload(draft))
    assert submission.should_push == "should_push"
    assert submission.expected is not None and submission.expected.magnitude == 2
    assert submission.dimensions["magnitude"] == "fail"


def test_gold_on_a_passed_dimension_is_refused_by_the_rubric_not_by_the_drafter() -> None:
    """The safety property lives in one place. A draft that violates it simply cannot be submitted."""

    draft = ReviewDraft.model_validate(
        {**_GOOD, "dimensions": {"factual_fidelity": "pass", "magnitude": "pass"}, "expected": {"magnitude": 2}}
    )
    with pytest.raises(ValueError, match="news_review_expected_requires_failed_dimension:magnitude"):
        EventRubricSubmission(**submission_payload(draft))


def test_a_failed_dimension_carries_evidence_refs() -> None:
    """The rubric refuses a `fail` without one, and the drafter cannot invent an operator's citation."""

    payload = submission_payload(ReviewDraft.model_validate(_GOOD))
    assert f"draft:{DRAFTER_ID}" in payload["evidence_refs"]
    payload_all_pass = submission_payload(
        ReviewDraft.model_validate({**_GOOD, "dimensions": {"factual_fidelity": "pass"}, "expected": None})
    )
    assert "evidence_refs" not in payload_all_pass


def test_one_failed_event_does_not_end_the_batch() -> None:
    lm = _ScriptedDrafterLM(fail=True)
    batch = build_draft_batch(ReviewDrafter(lm), _tasks(3))
    assert len(batch.drafts) == 3
    assert all(entry.error for entry in batch.drafts)
    assert batch.drafter["failures"] == 3
    assert all(entry.draft.should_push == "uncertain" for entry in batch.drafts)


def test_duplicate_task_fails_before_any_model_call() -> None:
    lm = _ScriptedDrafterLM()
    tasks = _tasks(2)
    tasks.append(dict(tasks[0]))

    with pytest.raises(ValueError, match="news_review_drafter_duplicate_task"):
        build_draft_batch(ReviewDrafter(lm), tasks)

    assert lm.calls == 0


def test_batch_names_the_drafter_and_disclaims_authority() -> None:
    batch = build_draft_batch(ReviewDrafter(_ScriptedDrafterLM()), _tasks(2))
    assert batch.drafter["drafter_id"] == DRAFTER_ID
    assert batch.drafter["model"] == "scripted/drafter"
    assert batch.drafter["dspy_version"] == "3.3.1"
    assert len(batch.drafter["signature_sha256"]) == 64
    assert len(batch.drafter["adapter_render_sha256"]) == 64
    assert batch.drafter["structured_output_capability"]["source"] == "configured_endpoint.structured_output"
    assert "proposal_only" in batch.drafter["authority"]
    assert batch.batch_sha256


def test_prompt_json_drafter_uses_configured_capability_in_the_actual_request() -> None:
    lm = build_drafter_lm(
        model_name="openai/MiniMax-M3",
        api_key="test-key",
        api_base="https://minimax.test/v1",
        model_kwargs={"extra_body": {"thinking": {"type": "disabled"}}},
        structured_output="prompt_json",
        temperature=1.0,
        lm_type=_RequestSpyDrafterLM,
    )

    result = ReviewDrafter(lm).draft(evidence_json="{}", card_json="{}", told_json="[]")

    assert isinstance(result, ReviewDraft)
    assert len(lm.requests) == 1
    assert lm.requests[0].config.response_format is None
    assert lm.requests[0].config.extensions["extra_body"] == {"thinking": {"type": "disabled"}}


def test_the_drafter_writes_nothing_to_the_review_plane() -> None:
    """A draft is not a review.

    `ReviewDesk.submit` appends an acceptance row unconditionally, so anything this module could put through
    that path would be accepted release evidence the instant it landed. The guarantee is structural: the
    drafter imports no review plane, no repository and no database seam, so it has nothing to write with.
    """

    import ast

    import tracefold.news.review.drafter as drafter_module

    tree = ast.parse(pathlib.Path(drafter_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(str(node.module or ""))
    assert not any("review" in name for name in imported), imported
    assert not any(name.endswith(("repository", "repositories")) for name in imported), imported
    assert "tracefold.platform.postgres.client" not in imported, imported
    assert not any("psycopg" in name for name in imported), imported


def test_submission_payload_keeps_the_labels_the_rubric_requires() -> None:
    """`timeliness` is `not_applicable` on most Events, and a push submission is invalid without it."""

    payload = submission_payload(ReviewDraft.model_validate(_GOOD))
    assert payload["dimensions"]["timeliness"] == "not_applicable"
    assert EventRubricSubmission(**payload).should_push == "should_push"


def test_the_drafter_cannot_judge_the_dimensions_it_disagrees_with_humans_on() -> None:
    """Measured, not assumed: over 25 Events both saw, agreement was 43%/42% on `why_*` against 70-88%
    elsewhere, and those two produced 27 of the 46 "human passed it, the draft failed it" disagreements."""

    from tracefold.news.review.drafter import DRAFTABLE_DIMENSIONS

    assert "why_support" not in DRAFTABLE_DIMENSIONS
    assert "why_value" not in DRAFTABLE_DIMENSIONS
    # A model that answers anyway must not reach the submission.
    draft = ReviewDraft.model_validate(
        {**_GOOD, "dimensions": {**_GOOD["dimensions"], "why_support": "fail", "why_value": "fail"}}
    )
    payload = submission_payload(draft)
    assert "why_support" not in payload["dimensions"]
    assert "why_value" not in payload["dimensions"]
    assert payload["dimensions"]["magnitude"] == "fail", "the draftable failures still come through"


def test_novelty_is_normalised_so_the_rubric_can_accept_it() -> None:
    """`NoveltyJudgment` requires `duplicate_of` on a restatement and forbids it elsewhere. A draft that
    breaks either rule would be unacceptable with no repair path."""

    named_but_new = ReviewDraft.model_validate({**_GOOD, "novelty": {"judgment": "new_fact", "duplicate_of": "a" * 64}})
    payload = submission_payload(named_but_new)
    assert payload["novelty"]["duplicate_of"] == ""
    assert EventRubricSubmission(**payload).novelty.judgment == "new_fact"

    unnamed_restatement = ReviewDraft.model_validate(
        {**_GOOD, "novelty": {"judgment": "restatement", "duplicate_of": ""}}
    )
    payload = submission_payload(unnamed_restatement)
    # Downgraded, not dropped: an unverifiable duplicate claim must not block the whole draft.
    assert payload["novelty"] == {"judgment": "uncertain", "duplicate_of": ""}
    assert EventRubricSubmission(**payload).novelty.judgment == "uncertain"

    proper = ReviewDraft.model_validate({**_GOOD, "novelty": {"judgment": "restatement", "duplicate_of": "b" * 64}})
    assert EventRubricSubmission(**submission_payload(proper)).novelty.duplicate_of == "b" * 64
