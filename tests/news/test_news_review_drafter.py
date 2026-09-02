"""#148: model-drafted rubrics are proposals, never evidence. #501: taxonomy Gold is drafted blind, twice."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.program.artifact import render_model_evidence_json
from tracefold.news.program.contracts import TriageContext
from tracefold.news.review.desk import EventRubricSubmission
from tracefold.news.review.drafter import (
    DRAFT_SCHEMA,
    DRAFTER_ID,
    TAXONOMY_BLIND_DRAFTER_ID,
    TAXONOMY_DIMENSIONS,
    ConfiguredDrafterLM,
    ReviewDraft,
    ReviewDrafter,
    RubricDraft,
    TaxonomyBlindDrafter,
    build_draft_batch,
    build_drafter_lm,
    submission_payload,
    taxonomy_dimensions,
)
from tracefold.news.taxonomy import ModelTaxonomyV1

NEWS_REVIEW_DRAFTER_ID = "tracefold.news.review_drafter_v7"
NEWS_REVIEW_DRAFT_BATCH_SCHEMA = "tracefold.news.review_draft_batch.v5"

_TAXONOMY = {
    "subject_codes": ["medtop:20000199"],
    "event_family": "product_service_change",
    "change_state": "announced",
    "assertion_status": "confirmed",
}
_OTHER_TAXONOMY = {
    "subject_codes": [],
    "event_family": "other",
    "change_state": "unknown",
    "assertion_status": "unknown",
}
_TAXONOMY_DIMENSIONS_PASS = dict.fromkeys(TAXONOMY_DIMENSIONS, "pass")

# What the rubric model emits: no taxonomy, no taxonomy_* dimensions.
_RUBRIC = {
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
# The code-assembled draft a reviewer reads and the accept step submits.
_GOOD = {
    **_RUBRIC,
    "dimensions": {**_RUBRIC["dimensions"], **_TAXONOMY_DIMENSIONS_PASS},
    "taxonomy": _TAXONOMY,
    "taxonomy_drafts": {"scripted/blind-a": _TAXONOMY, "scripted/blind-b": _TAXONOMY},
    "taxonomy_disagreement": False,
}
_TOLD_HEADLINE = "这条历史卡片只允许评审草稿者看到"


class _ScriptedDrafterLM(dspy.BaseLM):  # type: ignore[misc]
    def __init__(self, payload: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        super().__init__(model="scripted/drafter")
        self._payload = payload if payload is not None else _RUBRIC
        self._fail = fail
        self.calls = 0
        self.requests: list[str] = []

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        self.calls += 1
        self.requests.append(json.dumps(messages, ensure_ascii=False))
        if self._fail:
            raise RuntimeError("provider unavailable")
        return [json.dumps({"draft": self._payload})]


class _ScriptedBlindLM(dspy.BaseLM):  # type: ignore[misc]
    def __init__(self, model: str, taxonomy: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        super().__init__(model=model)
        self._taxonomy = taxonomy if taxonomy is not None else _TAXONOMY
        self._fail = fail
        self.calls = 0
        self.requests: list[str] = []

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        self.calls += 1
        self.requests.append(json.dumps(messages, ensure_ascii=False))
        if self._fail:
            raise RuntimeError("provider unavailable")
        return [json.dumps({"taxonomy": self._taxonomy})]


class _RequestSpyDrafterLM(ConfiguredDrafterLM):
    forward_contract = "typed_lm"

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.requests: list[dspy.LMRequest] = []
        super().__init__(model, **kwargs)

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        return dspy.LMResponse.from_text(json.dumps({"draft": _RUBRIC}), model=self.model)


def _blind_pair(
    a: dict[str, Any] | None = None,
    b: dict[str, Any] | None = None,
) -> tuple[TaxonomyBlindDrafter, TaxonomyBlindDrafter]:
    return (
        TaxonomyBlindDrafter(_ScriptedBlindLM("scripted/blind-a", a)),
        TaxonomyBlindDrafter(_ScriptedBlindLM("scripted/blind-b", b)),
    )


def _context() -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-1",
            "evidence_version": 1,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-1",
            "reporting_origin": "Reuters",
            "provenance": ["1018"],
            "leader_title": "Company commits new capacity",
            "raw_first_line": "Company commits new capacity",
            "leader_description": "A new production line.",
            "opened_at_ms": 1_000_000,
            "member_count": 1,
            "dedupe_family": "general",
            "provider_metadata": {},
            "queue_priority": "normal",
            "asset_class": "us_equity",
            "grounded_assets": ["TSLA"],
            "storyline_key": "asset:TSLA",
        },
        watchlist=(),
        told_rows=[
            {
                "event_id": "old-event",
                "at_ms": 900_000,
                "storyline_key": "asset:TSLA",
                "dedupe_family": "general",
                "comparison_title": "Company commits capacity",
                "comparison_fingerprint": "f" * 64,
                "magnitude": 1,
                "direction": "bullish",
                "headline_zh": _TOLD_HEADLINE,
                "why_zh": "历史卡片原因",
                "grounded_assets": ["TSLA"],
            }
        ],
        now_ms=1_010_000,
        queue_lag_ms=0,
    )


def _tasks(n: int = 2, *, stable: dict[str, Any] | None = _TAXONOMY) -> list[dict[str, Any]]:
    context = _context()
    return [
        {
            "task_id": f"evt.{i:064d}.1",
            "task_version": f"{i:064d}",
            "event_id": f"{i:064d}",
            "headline_zh": f"公司 {i} 承诺新增产能",
            "taxonomy_evidence_json": render_model_evidence_json(context.taxonomy_payload(), predictor="taxonomy"),
            "stable_taxonomy": stable,
            "evidence_json": render_model_evidence_json(context.event_semantics_payload(), predictor="event_semantics"),
            "card_json": json.dumps({"verdict": {"headline_zh": "公司承诺新增产能"}}, ensure_ascii=False),
            "told_json": json.dumps([{"headline_zh": _TOLD_HEADLINE}], ensure_ascii=False),
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


def test_the_rubric_model_output_carries_no_taxonomy_and_no_taxonomy_dimensions() -> None:
    """The model never labels taxonomy (#501 D8); code assembles it from the blind drafts."""

    assert "taxonomy" not in RubricDraft.model_fields
    with pytest.raises(ValueError, match="taxonomy"):
        RubricDraft.model_validate({**_RUBRIC, "taxonomy": _TAXONOMY})
    schema = RubricDraft.model_json_schema()
    assert "taxonomy_subject_codes" not in schema["$defs"]["DraftDimensions"]["properties"]
    assert "required" not in schema["$defs"]["DraftDimensions"]
    # The submission still needs all five; the code-written labels supply them.
    rubric_only = ReviewDraft.model_validate({**_GOOD, "dimensions": _RUBRIC["dimensions"]})
    with pytest.raises(ValueError, match="news_review_taxonomy_dimension_required:taxonomy_"):
        EventRubricSubmission(**submission_payload(rubric_only))


def test_model_copied_source_authority_cannot_enter_the_taxonomy_labels() -> None:
    draft = ReviewDraft.model_validate({**_GOOD, "taxonomy": {**_TAXONOMY, "source_authority": "unknown"}})

    assert draft.taxonomy.model_dump(mode="json") == _TAXONOMY


def test_gold_on_a_passed_dimension_is_refused_by_the_rubric_not_by_the_drafter() -> None:
    """The safety property lives in one place. A draft that violates it simply cannot be submitted."""

    draft = ReviewDraft.model_validate(
        {
            **_GOOD,
            "dimensions": {**_GOOD["dimensions"], "magnitude": "pass"},
            "expected": {"magnitude": 2},
        }
    )
    with pytest.raises(ValueError, match="news_review_expected_requires_failed_dimension:magnitude"):
        EventRubricSubmission(**submission_payload(draft))


def test_a_failed_dimension_carries_evidence_refs() -> None:
    """The rubric refuses a `fail` without one, and the drafter cannot invent an operator's citation."""

    payload = submission_payload(ReviewDraft.model_validate(_GOOD))
    assert f"draft:{DRAFTER_ID}" in payload["evidence_refs"]
    payload_all_pass = submission_payload(
        ReviewDraft.model_validate(
            {
                **_GOOD,
                "dimensions": {"factual_fidelity": "pass", **_TAXONOMY_DIMENSIONS_PASS},
                "expected": None,
            }
        )
    )
    assert "evidence_refs" not in payload_all_pass


def test_a_failed_dimension_names_the_batch_drafter_that_actually_proposed_it() -> None:
    payload = submission_payload(ReviewDraft.model_validate(_GOOD), draft_author="teacher/qwen:thinking")

    assert "draft:teacher/qwen:thinking" in payload["evidence_refs"]
    assert f"draft:{DRAFTER_ID}" not in payload["evidence_refs"]


def test_taxonomy_provenance_names_the_blind_drafters_and_carries_both_drafts() -> None:
    """`taxonomy_review` is authored by the blind pair, never by the rubric model, and keeps both labels."""

    draft = ReviewDraft.model_validate(
        {**_GOOD, "taxonomy_drafts": {"scripted/blind-b": _OTHER_TAXONOMY, "scripted/blind-a": _TAXONOMY}}
    )
    payload = submission_payload(draft, draft_author="teacher/qwen:thinking")

    review = payload["taxonomy_review"]
    assert review["label_source"] == "model_draft"
    assert review["draft_author"] == "scripted/blind-a+scripted/blind-b"
    assert review["drafts"] == {"scripted/blind-a": _TAXONOMY, "scripted/blind-b": _OTHER_TAXONOMY}
    assert review["draft_taxonomy"]["event_family"] == "product_service_change"
    submission = EventRubricSubmission(**payload)
    assert submission.taxonomy_review.drafts is not None
    assert submission.taxonomy_review.drafts["scripted/blind-b"].event_family == "other"

    # Without blind drafts (a hand-assembled draft) the rubric author stands.
    solo = submission_payload(ReviewDraft.model_validate({**_GOOD, "taxonomy_drafts": {}}), draft_author="human/x")
    assert solo["taxonomy_review"]["draft_author"] == "human/x"
    assert "drafts" not in solo["taxonomy_review"]


def test_taxonomy_dimensions_are_written_by_code_from_stable_against_the_blind_draft() -> None:
    draft = ModelTaxonomyV1.model_validate(_TAXONOMY)
    stable = {**_TAXONOMY, "change_state": "effective", "source_authority": "reputable_secondary"}

    labels = taxonomy_dimensions(stable, draft)

    assert labels == {
        "taxonomy_subject_codes": "pass",
        "taxonomy_event_family": "pass",
        "taxonomy_change_state": "fail",
        "taxonomy_assertion_status": "pass",
        "taxonomy_source_authority": "pass",
    }
    assert taxonomy_dimensions(None, draft) == dict.fromkeys(TAXONOMY_DIMENSIONS, "not_applicable")


def test_the_blind_drafters_read_only_the_program_taxonomy_input() -> None:
    """The bytes the blind drafter reads are the Predictor's: no card, no Stable label, no told, no review."""

    rubric_lm = _ScriptedDrafterLM()
    blind_a = _ScriptedBlindLM("scripted/blind-a")
    blind_b = _ScriptedBlindLM("scripted/blind-b")
    tasks = _tasks(1)
    assert _TOLD_HEADLINE in tasks[0]["evidence_json"]

    batch = build_draft_batch(
        ReviewDrafter(rubric_lm),
        tasks,
        taxonomy_drafters=(TaxonomyBlindDrafter(blind_a), TaxonomyBlindDrafter(blind_b)),
    )

    assert batch.drafts[0].error is None
    for blind in (blind_a, blind_b):
        assert len(blind.requests) == 1
        rendered = blind.requests[0]
        assert "Company commits new capacity" in rendered
        assert _TOLD_HEADLINE not in rendered
        assert "event_status" not in rendered
        assert "公司承诺新增产能" not in rendered  # the card
        assert "product_service_change / announced" not in rendered.split("</tracefold-untrusted-event-json-v1>")[-1]
        assert "card_json" not in rendered and "told_json" not in rendered
        assert "# TRACEFOLD NEWS - TAXONOMY" in rendered
    assert _TOLD_HEADLINE in rubric_lm.requests[0]
    assert "公司承诺新增产能" in rubric_lm.requests[0]


def test_agreeing_blind_drafts_become_the_draft_and_disagreement_takes_a() -> None:
    agreed = build_draft_batch(ReviewDrafter(_ScriptedDrafterLM()), _tasks(1), taxonomy_drafters=_blind_pair())
    entry = agreed.drafts[0]
    assert entry.error is None
    assert entry.draft.taxonomy.model_dump(mode="json") == _TAXONOMY
    assert entry.draft.taxonomy_disagreement is False
    assert entry.draft.taxonomy_drafts == {
        "scripted/blind-a": ModelTaxonomyV1.model_validate(_TAXONOMY),
        "scripted/blind-b": ModelTaxonomyV1.model_validate(_TAXONOMY),
    }
    assert {name: entry.draft.dimensions[name] for name in TAXONOMY_DIMENSIONS} == _TAXONOMY_DIMENSIONS_PASS  # type: ignore[literal-required]
    assert agreed.taxonomy_drafters["agreement_rate"] == 1.0
    assert agreed.taxonomy_drafters["disagreement_task_ids"] == []
    assert agreed.taxonomy_drafters["stable_agreement_rate"] == {"scripted/blind-a": 1.0, "scripted/blind-b": 1.0}

    split = build_draft_batch(
        ReviewDrafter(_ScriptedDrafterLM()),
        _tasks(2, stable=_OTHER_TAXONOMY),
        taxonomy_drafters=_blind_pair(_TAXONOMY, _OTHER_TAXONOMY),
    )
    for entry in split.drafts:
        assert entry.draft.taxonomy.model_dump(mode="json") == _TAXONOMY
        assert entry.draft.taxonomy_disagreement is True
        assert entry.draft.dimensions["taxonomy_event_family"] == "fail"
        assert entry.draft.dimensions["taxonomy_source_authority"] == "pass"
    assert split.taxonomy_drafters["models"] == ["scripted/blind-a", "scripted/blind-b"]
    assert split.taxonomy_drafters["agreement_rate"] == 0.0
    assert split.taxonomy_drafters["disagreement_task_ids"] == [task["task_id"] for task in _tasks(2)]
    assert split.taxonomy_drafters["stable_agreement_rate"] == {"scripted/blind-a": 0.0, "scripted/blind-b": 1.0}
    assert split.taxonomy_drafters["identities"][0]["drafter_id"] == TAXONOMY_BLIND_DRAFTER_ID
    assert len(split.taxonomy_drafters["identities"][0]["instruction_sha256"]) == 64
    # Every entry is submittable as-is; the drafts travel with it.
    payload = submission_payload(split.drafts[0].draft)
    assert set(payload["taxonomy_review"]["drafts"]) == {"scripted/blind-a", "scripted/blind-b"}
    assert EventRubricSubmission(**payload).taxonomy.event_family == "product_service_change"


def test_identical_blind_drafter_models_are_refused_before_any_model_call() -> None:
    lm_a = _ScriptedBlindLM("scripted/same")
    lm_b = _ScriptedBlindLM("scripted/same")

    with pytest.raises(ValueError, match="news_review_taxonomy_drafters_must_differ"):
        build_draft_batch(
            ReviewDrafter(_ScriptedDrafterLM()),
            _tasks(1),
            taxonomy_drafters=(TaxonomyBlindDrafter(lm_a), TaxonomyBlindDrafter(lm_b)),
        )

    assert lm_a.calls == lm_b.calls == 0


def test_one_failed_event_does_not_end_the_batch() -> None:
    lm = _ScriptedDrafterLM(fail=True)
    batch = build_draft_batch(ReviewDrafter(lm), _tasks(3), taxonomy_drafters=_blind_pair())
    assert len(batch.drafts) == 3
    assert all(entry.error for entry in batch.drafts)
    assert batch.drafter["failures"] == 3
    assert all(entry.draft.should_push == "uncertain" for entry in batch.drafts)

    blind_failed = build_draft_batch(
        ReviewDrafter(_ScriptedDrafterLM()),
        _tasks(2),
        taxonomy_drafters=(
            TaxonomyBlindDrafter(_ScriptedBlindLM("scripted/blind-a", fail=True)),
            TaxonomyBlindDrafter(_ScriptedBlindLM("scripted/blind-b")),
        ),
    )
    assert all(str(entry.error).startswith("taxonomy_drafting_failed") for entry in blind_failed.drafts)
    assert blind_failed.taxonomy_drafters["failures"] == 2
    assert blind_failed.taxonomy_drafters["labelled_n"] == 0
    assert blind_failed.taxonomy_drafters["agreement_rate"] is None


def test_duplicate_task_fails_before_any_model_call() -> None:
    lm = _ScriptedDrafterLM()
    tasks = _tasks(2)
    tasks.append(dict(tasks[0]))

    with pytest.raises(ValueError, match="news_review_drafter_duplicate_task"):
        build_draft_batch(ReviewDrafter(lm), tasks, taxonomy_drafters=_blind_pair())

    assert lm.calls == 0


def test_batch_names_the_drafter_and_disclaims_authority() -> None:
    batch = build_draft_batch(ReviewDrafter(_ScriptedDrafterLM()), _tasks(2), taxonomy_drafters=_blind_pair())
    assert batch.drafter["drafter_id"] == DRAFTER_ID == NEWS_REVIEW_DRAFTER_ID
    assert batch.schema_id == DRAFT_SCHEMA == NEWS_REVIEW_DRAFT_BATCH_SCHEMA
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

    assert isinstance(result, RubricDraft)
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
    with pytest.raises(ValueError, match="why_support"):
        ReviewDraft.model_validate(
            {**_GOOD, "dimensions": {**_GOOD["dimensions"], "why_support": "fail", "why_value": "fail"}}
        )


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
